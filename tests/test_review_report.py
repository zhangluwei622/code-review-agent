import json
import re

import pytest
from test_ledger_gateway import reserve

from review_agent import app
from review_agent.contracts import AgentError, StoredResult
from review_agent.report import exit_code, render, render_audit, render_review, write_report


@pytest.mark.parametrize(
    "scenario,status,code",
    [
        ("success", "DONE", 0),
        ("no-findings", "DONE", 0),
        ("abstain", "ABSTAINED", 2),
        ("malformed", "PARTIAL_INVALID_RESULT", 2),
        ("truncated-json", "PARTIAL_TRUNCATED", 2),
        ("truncated-valid", "PARTIAL_TRUNCATED", 2),
    ],
)
def test_report_states(harness, scenario, status, code):
    harness.create(scenario)
    data = harness.run()
    assert data["units"][0]["status"] == status
    assert exit_code(data) == code
    report = render(data)
    assert "fixture 模式" in report and "调用账本" in report
    if status != "DONE":
        assert "部分完成" in report
        assert "完整完成单元：0/1" in report


@pytest.mark.parametrize("body_kind", ["valid", "empty", "abstain", "malformed"])
@pytest.mark.parametrize("with_usage", [True, False])
def test_output_limit_always_partial_and_reusable(harness, success_spec, body_kind, with_usage):
    reply = success_spec["responses"]["0:0:REVIEW:1"]
    if body_kind == "empty":
        reply["body"] = '{"action":"submit_review","findings":[]}'
    elif body_kind == "abstain":
        reply["body"] = '{"action":"abstain","findings":[],"reason":"missing context"}'
    elif body_kind == "malformed":
        reply["body"] = '{"action":'
    reply["finish"] = "OUTPUT_LIMIT"
    if not with_usage:
        reply["usage"] = None
    harness.create(spec=success_spec)
    harness.crash("after_completed")
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_TRUNCATED"
    assert data["attempts"][0]["result_status"] == "TRUNCATED"
    assert data["attempts"][0]["fee_status"] == ("SETTLED" if with_usage else "HELD")
    assert len(data["findings"]) == (1 if body_kind == "valid" else 0)
    assert len(harness.calls) == 1
    assert "完整完成单元：0/1" in render(data)


@pytest.mark.parametrize(
    "finish,complete,size,status,result_status",
    [
        ("COMPLETE", True, 65537, "PARTIAL_TRUNCATED", "TRUNCATED"),
        ("COMPLETE", False, 65537, "PAUSED_UNKNOWN", None),
        ("UNCONFIRMED", True, 0, "PARTIAL_INVALID_RESULT", "UNUSABLE"),
    ],
)
def test_local_limit_and_transport_uncertainty(
    harness, finish, complete, size, status, result_status
):
    harness.create(
        spec={
            "default": {
                "body": "x" * size if size else '{"action":"submit_review","findings":[]}',
                "finish": finish,
                "response_complete": complete,
                "usage": {"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
            }
        }
    )
    data = harness.run()
    assert data["units"][0]["status"] == status
    assert data["attempts"][0]["result_status"] == result_status
    assert "x" * 100 not in json.dumps(data)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("location", "PARTIAL_INVALID_RESULT"),
        ("expectation", "DONE"),
    ],
)
def test_comment_location_and_confidence_gates(harness, success_spec, mutation, expected):
    reply = success_spec["responses"]["0:0:REVIEW:1"]
    decision = json.loads(reply["body"])
    if mutation == "location":
        decision["findings"][0]["line"] = 9999
    else:
        decision["findings"][0]["expectation_evidence"] = []
    reply["body"] = json.dumps(decision)
    harness.create(spec=success_spec)
    data = harness.run()
    assert data["units"][0]["status"] == expected
    if mutation == "expectation":
        finding = json.loads(data["findings"][0]["data"])
        assert finding["confidence"] == "reference"
        assert finding["confidence_reason"] == "MISSING_EXPECTATION_EVIDENCE"
    else:
        assert not data["findings"]


def test_all_four_held_states_are_reported_without_mutation(harness):
    from conftest import ROOT

    text = (ROOT / "examples/diffs/empty-list.diff").read_text()
    harness.create(text="".join(text.replace("stats.py", f"file_{i}.py") for i in range(4)))
    with harness.store() as store:
        rows = [reserve(store, index) for index in range(4)]
        for row in rows[1:]:
            store.mark_dispatched(row["attempt_id"])
        store.mark_unknown(rows[2]["attempt_id"])
        store.complete_attempt(
            rows[3]["attempt_id"],
            StoredResult(
                result_status="UNUSABLE",
                completion_state="COMPLETE",
                error_code="EMPTY_REPLY",
            ),
        )
    before = harness.read()
    output = render(before)
    assert before["totals"]["held_tokens"] == 2400
    assert before["totals"]["held_cost_nusd"] == 3200000
    for row in rows:
        assert row["attempt_id"] in output
    for state in ("RESERVED", "DISPATCHED", "UNKNOWN", "COMPLETED"):
        assert state in output
    assert "OPEN" in output
    assert harness.read() == before


def test_trace_report_determinism_and_atomic_export(harness, tmp_path, monkeypatch):
    harness.create()
    data = harness.run()
    before = harness.read()
    text = render(data)
    assert text == render(harness.read())
    refs = re.findall(r"\]\(#([^)]+)\)", text)
    assert refs and all("### " + ref in text for ref in refs)
    trace = app.finding_trace(data, data["findings"][0]["finding_id"])
    assert trace["attempt"]["result_ref"] == data["findings"][0]["result_ref"]
    assert trace["evidence"] and trace["spans"]
    output = tmp_path / "report.md"
    write_report(output, text)
    with monkeypatch.context() as ctx:

        def fail(*args):
            raise OSError("not copied to user logs")

        ctx.setattr("review_agent.report.os.replace", fail)
        with pytest.raises(AgentError, match="REPORT_WRITE_FAILED"):
            write_report(output, "replacement")
    assert output.read_text() == text
    write_report(output, text)
    assert harness.read() == before
    assert len(harness.calls) == 1


def test_report_escapes_untrusted_links_and_html(harness, success_spec):
    reply = success_spec["responses"]["0:0:REVIEW:1"]
    decision = json.loads(reply["body"])
    decision["findings"][0]["title"] = (
        '<img src="https://example.invalid/">![x](https://bad.invalid)'
    )
    reply["body"] = json.dumps(decision)
    harness.create(spec=success_spec)
    report = render(harness.run())
    assert "<img" not in report
    assert "https://" not in report
    assert "&#58;//" in report


def test_intentional_exception_fixture_is_not_a_quality_claim(harness):
    harness.create("no-findings", diff="intentional-error-test")
    data = harness.run()
    assert data["units"][0]["status"] == "DONE"
    assert not data["findings"]
    assert "不代表模型审阅质量" in render(data)


@pytest.mark.parametrize("scenario", [
    "success", "no-findings", "abstain", "truncated-valid", "malformed",
])
def test_concise_review_separates_code_from_audit_and_preserves_status(harness, scenario):
    harness.create(scenario)
    data = harness.run()
    review, audit = render_review(data), render_audit(data)
    assert review.startswith("# 代码审阅报告")
    for operational in ("调用账本", "Trace", "tokens", "USD", "任务与覆盖", "HELD"):
        assert operational not in review
    assert "调用账本" in audit and "任务与覆盖" in audit
    assert "执行与审计明细" in audit
    if scenario == "success":
        assert "stats.py · 变更前第 3 行" in review
        assert review.index("stats.py") < review.index("- 问题：") < review.index("- 修改建议：")
        assert "严重性：中；置信度：高" in review
    elif scenario == "no-findings":
        assert "已完成本轮审阅" in review and "本次未报告正式代码问题" in review
    else:
        assert "审阅尚未完整结束" in review
    if scenario == "truncated-valid":
        assert "本次未报告正式代码问题" in review and not data["findings"]
    assert harness.read() == data


def test_concise_review_keeps_reference_distinct_and_escapes_text(harness, success_spec):
    reply = success_spec["responses"]["0:0:REVIEW:1"]
    decision = json.loads(reply["body"])
    finding = decision["findings"][0]
    finding["expectation_evidence"] = []
    finding["title"] = '<img src="https://bad.invalid/">'
    finding["suggestion"] = "[click](https://bad.invalid/)"
    reply["body"] = json.dumps(decision)
    harness.create(spec=success_spec)
    data = harness.run()
    review = render_review(data)
    assert "本次未报告正式代码问题" in review
    assert "仅供参考（尚未确认为缺陷）" in review
    assert "<img" not in review and "https://" not in review
    assert "&#58;//" in review
    assert harness.read() == data


def test_concise_review_keeps_accepted_truncated_finding_without_promoting_completion(
    harness, success_spec,
):
    success_spec["responses"]["0:0:REVIEW:1"]["finish"] = "OUTPUT_LIMIT"
    harness.create(spec=success_spec)
    data = harness.run()
    review = render_review(data)
    assert "stats.py · 变更前第 3 行" in review
    assert "该条来自截断回复" in review and "审阅尚未完整结束" in review
    assert len(data["findings"]) == 1 and harness.read() == data
