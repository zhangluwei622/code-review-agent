import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from review_agent import app, cli
from review_agent.contracts import AgentError
from review_agent.viewer import export_view
from review_agent.viewer.export import load_trace, render_html
from review_agent.viewer.projection import project

ROOT = Path(__file__).resolve().parents[1]
TRACE_DIR = ROOT / "tests/fixtures/traces"


def trace(name="success"):
    return json.loads((TRACE_DIR / (name + ".json")).read_text())


def payload(html):
    return json.loads(
        re.search(r'<script type="application/json" id="trace-data">(.*?)</script>', html, re.S)[1]
    )


def written(tmp_path, value):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


@pytest.mark.parametrize("path", sorted(TRACE_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_historical_exports_preserve_exact_values_and_do_not_need_database(path, tmp_path):
    before = path.read_bytes()
    result = export_view(path, tmp_path / "view.html")
    bundle = payload((tmp_path / "view.html").read_text())
    assert bundle["trace"] == json.loads(before)
    assert result["input_sha256"] == hashlib.sha256(before).hexdigest()
    assert result["model_calls"] == 0 and result["database_access"] is False
    assert path.read_bytes() == before
    assert bundle["view"]["recovery_events_available"] is False
    assert bundle["view"]["task_status_available"] is False


def test_repair_is_bound_to_specific_result_and_nested_source_is_not_a_send():
    data = trace("phase-3-repair")
    view = project(data)
    assert len(view["nodes"]) == view["marked_sends"] == 2
    repair = next(n for n in view["nodes"] if n["kind"] == "REPAIR")
    relation = next(r for r in view["relations"] if r["kind"] == "REPAIR")
    assert relation["to"] == repair["id"]
    assert relation["ref"] == repair["raw"]["operation_context"]["source_result_ref"]
    source = next(n for n in view["nodes"] if n["id"] == relation["from"])
    assert source["result_ref"] == relation["ref"]
    assert view["findings"][0]["node_id"] == repair["id"]


def test_retry_decision_deduplicated_and_unknown_held_not_released():
    data = trace("phase-3-retry")
    original = copy.deepcopy(data)
    view = project(data)
    retries = [r for r in view["relations"] if r["kind"] == "RETRY"]
    assert len(retries) == 1 and retries[0]["detail"]["status"] == "BOUND"
    source = next(n for n in view["nodes"] if n["id"] == retries[0]["from"])
    assert source["status"] == "UNKNOWN" and source["fee_status"] == "HELD"
    assert view["marked_sends"] == 2
    assert data == original
    assert data["task_totals"]["held_tokens"] == source["raw"]["attempt"]["quote_tokens"]


def test_tool_cycle_is_dependency_ordered_and_reused_inputs_count_once():
    data = trace("phase-4-tools")
    view = project(data)
    assert view["tool_count"] == 4 and view["marked_sends"] == 6
    assert len(view["nodes"]) == 10
    positions = {n["id"]: i for i, n in enumerate(view["nodes"])}
    for r in view["relations"]:
        if r["from"] and r["to"]:
            assert positions[r["from"]] < positions[r["to"]]
    assert view["nodes"][0]["kind"] == "REVIEW"
    assert view["nodes"][1]["kind"] == "TOOL"
    assert all(n["timing"]["elapsed_ms"] is None for n in view["nodes"])


def test_scoped_finding_preserves_task_totals_and_anchor_roles():
    data = trace()
    view = project(data)
    assert view["scope"]["finding_id"] == data["finding"]["finding_id"]
    finding = view["findings"][0]
    assert finding["node_id"] == data["attempt"]["attempt_id"]
    assert {e["role"] for e in finding["evidence"]} == {"anchor", "evidence", "expectation"}
    assert all(e["matches"] for e in finding["evidence"])
    assert all(
        m["node_id"] == finding["node_id"] for e in finding["evidence"] for m in e["matches"]
    )


def test_evidence_does_not_borrow_from_other_calls_or_unconsumed_tools():
    data = trace()
    original_call = {k: data[k] for k in ("attempt", "operation", "request", "result", "quote")}
    original_call = copy.deepcopy(original_call)
    original_call["attempt"]["attempt_id"] = "other_attempt"
    original_call["attempt"]["result_ref"] = "other_result"
    data["calls"] = [original_call]
    data["request"]["data"]["hunks"] = []
    view = project(data)
    assert all(not e["matches"] for e in view["findings"][0]["evidence"])


def test_consumed_tool_evidence_is_distinct_from_first_request():
    data = trace()
    data["request"]["data"]["hunks"] = []
    tool_data = trace("phase-4-tool-retry")["tool_calls"][0]
    data["tool_inputs"] = [tool_data]
    view = project(data)
    finding = view["findings"][0]
    expected = next(e for e in finding["evidence"] if e["role"] == "expectation")
    assert expected["matches"][0]["origin"] == "本轮已输入的工具结果"
    assert expected["matches"][0]["node_id"] == tool_data["call"]["tool_call_id"]


def test_partial_pricing_export_keeps_unknowns_and_no_synthetic_nodes():
    data = trace("deepseek-live-pricing-review")
    data = {
        k: data[k]
        for k in ("task_id", "trace_id", "config_digest", "execution_mode", "scope", "pricing")
    }
    view = project(data)
    assert view["nodes"] == [] and view["findings"] == []
    assert "task_totals" not in view["sections_present"]
    assert view["task_status_available"] is False


def test_reserved_cancelled_unattempted_and_pending_decision_are_not_sends():
    data = trace("phase-3-retry")
    for call, status in zip(data["calls"], ["RESERVED", "CANCELLED"], strict=True):
        call["attempt"]["call_status"] = status
        for decision in call["retry_decisions"]:
            decision.update(status="PENDING", bound_attempt_id=None)
    data["unattempted_operations"] = [
        {"operation": {"operation_id": "new_op", "unit_id": "u1"}, "request": {"data": {}}}
    ]
    view = project(data)
    assert view["marked_sends"] == 0
    assert any(n["kind"] == "PREPARED" for n in view["nodes"])
    assert next(r for r in view["relations"] if r["kind"] == "RETRY")["to"] is None


def test_conflicting_duplicate_attempt_is_rejected():
    data = trace("phase-3-repair")
    data["calls"][1]["source_call"]["attempt"]["actual_tokens"] = 999
    with pytest.raises(AgentError, match="VIEW_CONFLICTING_ID"):
        project(data)


def test_repair_wrong_operation_reference_rejected():
    data = trace("phase-3-repair")
    data["calls"][1]["operation_context"]["source_operation_id"] = "wrong"
    with pytest.raises(AgentError, match="VIEW_CONFLICTING_REFERENCE"):
        project(data)


def test_tool_continuation_must_name_the_input_result():
    data = trace("phase-4-tools")
    data["calls"][1]["operation_context"]["input_tool_result_ref"] = "different_result"
    with pytest.raises(AgentError, match="VIEW_CONFLICTING_REFERENCE"):
        project(data)


def test_overflowing_json_number_rejected(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"amount": 1e999}')
    with pytest.raises(AgentError, match="VIEW_INVALID_JSON"):
        load_trace(path)


def test_missing_scoped_source_is_visible_not_synthesized():
    data = trace("phase-3-repair")
    repair = data["calls"][1]
    repair.pop("source_call")
    data["calls"] = [repair]
    view = project(data)
    assert view["marked_sends"] == 1
    assert view["relations"][0]["from"] is None


def test_timestamp_difference_only_when_recorded_not_zero_for_missing():
    data = trace("phase-3-retry")
    key = data["calls"][0]["attempt"]["attempt_id"]
    data["pricing"]["attempt_timings"] = [
        {
            "attempt_id": key,
            "dispatched_at": "2026-09-13T10:00:00+00:00",
            "completed_at": "2026-09-13T10:00:02+00:00",
        }
    ]
    view = project(data)
    assert view["nodes"][0]["timing"]["elapsed_ms"] == 2000
    assert view["nodes"][1]["timing"]["elapsed_ms"] is None


@pytest.mark.parametrize(
    "body",
    [
        '{"findings":[],"findings":[{}]}',
        "{invalid",
        '</script><img src="https://attacker.invalid/x" onerror="alert(1)">',
        "<script>globalThis.pwned=1</script> & \u2028 \u2029",
    ],
)
def test_external_body_is_inert_and_original_preserved(body, tmp_path):
    data = trace()
    data["result"]["safe_body"] = body
    export_view(written(tmp_path, data), tmp_path / "out.html")
    html = (tmp_path / "out.html").read_text()
    assert html.count("<script") == 2
    assert payload(html)["trace"]["result"]["safe_body"] == body
    embedded = re.search(r'id="trace-data">(.*?)</script>', html, re.S)[1]
    assert "<" not in embedded
    assert "default-src 'none'" in html and "connect-src 'none'" in html
    assert "innerHTML" not in html and "eval(" not in html


@pytest.mark.parametrize(
    "unsafe",
    ["ghp_" + "x" * 20, 'password="dummy-sensitive"', "\x00", "-----BEGIN PRIVATE KEY-----"],
)
def test_unsafe_export_refused_without_rewriting_input(unsafe, tmp_path):
    data = trace()
    data["result"]["safe_body"] = unsafe
    path = written(tmp_path, data)
    before = path.read_bytes()
    with pytest.raises(AgentError, match="VIEW_UNSAFE_TRACE"):
        export_view(path, tmp_path / "out.html")
    assert path.read_bytes() == before and not (tmp_path / "out.html").exists()


@pytest.mark.parametrize(
    "text,code",
    [
        ('{"a":1,"a":2}', "VIEW_DUPLICATE_KEY"),
        ('{"a":NaN}', "VIEW_INVALID_JSON"),
        ("[1]", "VIEW_INVALID_TRACE"),
        ('{"nested":{"a":1,"a":2}}', "VIEW_DUPLICATE_KEY"),
        ("{}", "VIEW_INVALID_TRACE"),
    ],
)
def test_invalid_or_duplicate_outer_json_refused(text, code, tmp_path):
    p = tmp_path / "input.json"
    p.write_text(text)
    with pytest.raises(AgentError, match=code):
        export_view(p, tmp_path / "out.html")


def test_input_size_and_depth_bounded(tmp_path, monkeypatch):
    import review_agent.viewer.export as module

    p = written(tmp_path, trace())
    monkeypatch.setattr(module, "MAX_TRACE_BYTES", 32)
    with pytest.raises(AgentError, match="VIEW_INPUT_TOO_LARGE"):
        load_trace(p)
    monkeypatch.setattr(module, "MAX_TRACE_BYTES", 1000000)
    monkeypatch.setattr(module, "MAX_DEPTH", 3)
    with pytest.raises(AgentError, match="VIEW_TOO_COMPLEX"):
        load_trace(p)


def test_output_never_overwrites_existing_input_or_symlink(tmp_path):
    p = written(tmp_path, trace())
    before = p.read_bytes()
    with pytest.raises(AgentError, match="VIEW_OUTPUT_EXISTS"):
        export_view(p, p)
    link = tmp_path / "link.html"
    link.symlink_to(p)
    with pytest.raises(AgentError, match="VIEW_OUTPUT_EXISTS"):
        export_view(p, link)
    assert p.read_bytes() == before


def test_cli_does_not_access_agent_database_credentials_or_provider(tmp_path, monkeypatch, capsys):
    def deny(*a, **kw):
        raise AssertionError("EXECUTION_OR_DATABASE_FORBIDDEN")

    for method in ("create_task", "execute", "read_task", "review_historical_costs"):
        monkeypatch.setattr(app, method, deny)
    import os

    original_getenv = os.getenv

    def guarded_getenv(key, default=None):
        if key in {"DEEPSEEK_API_KEY", "GITHUB_TOKEN", "GITLAB_TOKEN"}:
            return deny()
        return original_getenv(key, default)

    monkeypatch.setattr(os, "getenv", guarded_getenv)
    p = written(tmp_path, trace())
    monkeypatch.setattr(
        "sys.argv",
        ["review-agent", "view", "--trace", str(p), "--output", str(tmp_path / "view.html")],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0


def test_csp_hashes_match_embedded_code():
    import base64

    html = render_html(trace(), "f" * 64)
    css = re.search(r"<style>(.*?)</style>", html, re.S)[1]
    js = re.search(r"<script>(.*?)</script>", html, re.S)[1]
    for content in (css, js):
        expected = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
        assert "'sha256-" + expected + "'" in html
