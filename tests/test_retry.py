import copy
import json
import sqlite3

import pytest
from test_cli import cli

from review_agent import app
from review_agent.contracts import AgentError, digest
from review_agent.report import render


def retry_spec(success_spec, *, unknowns=1):
    spec = copy.deepcopy(success_spec)
    success = spec["responses"]["0:0:REVIEW:1"]
    spec["responses"] = {f"0:0:REVIEW:{n}": {"error": "timeout"} for n in range(1, unknowns + 1)}
    spec["responses"][f"0:0:REVIEW:{unknowns + 1}"] = success
    return spec


def crash_at(point):
    def fault(event):
        if event == point:
            raise AgentError("TEST_CRASH")

    return fault


def test_retry_chain_requires_new_choice_for_new_unknown(harness, success_spec):
    harness.create(spec=retry_spec(success_spec, unknowns=2))
    first = harness.run()["attempts"][0]["attempt_id"]
    data = harness.run(retry_unknown=first)
    second = data["attempts"][1]["attempt_id"]
    assert len(harness.calls) == 2
    assert [a["fee_status"] for a in data["attempts"]] == ["HELD", "HELD"]
    for _ in range(3):
        assert harness.run(retry_unknown=first)["task"]["status"] == "PAUSED_UNKNOWN"
        harness.run()
    assert len(harness.calls) == 2
    assert len(harness.read()["retry_decisions"]) == 1
    data = harness.run(retry_unknown=second)
    assert data["task"]["status"] == "COMPLETED"
    assert len(data["operations"]) == 1
    assert [a["attempt_no"] for a in data["attempts"]] == [1, 2, 3]
    assert data["units"][0]["sends"] == 3
    assert data["totals"]["held_tokens"] == 1200
    assert data["totals"]["settled_tokens"] == 300
    assert [a["call_status"] for a in data["attempts"]] == ["UNKNOWN", "UNKNOWN", "COMPLETED"]
    for source in (first, second):
        harness.run(retry_unknown=source)
        trace = app.trace(harness.read(), attempt_id=source)
        assert trace["result"] is None
        assert trace["operation"]["result_ref"] is not None
    assert len(harness.calls) == 3
    assert harness.calls[0] == harness.calls[1] == harness.calls[2]
    assert len(app.trace(data)["calls"]) == 3
    assert "账务状态：OPEN" in render(data)
    assert "人工重试选择" in render(data)


@pytest.mark.parametrize(
    "point,expected_attempts,bound",
    [
        ("after_decision", 1, False),
        ("after_retry_attempt", 1, False),
        ("after_retry_reserve", 1, False),
        ("after_retry_binding", 1, False),
        ("after_retry_bound", 2, True),
        ("after_reserved", 2, True),
        ("after_completed", 2, True),
    ],
)
def test_plain_resume_consumes_saved_authorization_once(
    harness, success_spec, point, expected_attempts, bound
):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at(point))
    before = harness.read()
    decision = before["retry_decisions"][0]
    assert (decision["status"] == "BOUND") is bound
    assert len(before["attempts"]) == expected_attempts
    assert (
        len([e for e in before["budget_events"] if e["event_type"] == "RESERVE"])
        == expected_attempts
    )
    data = harness.run()
    for _ in range(3):
        harness.run()
        harness.run(retry_unknown=source)
    assert len(harness.calls) == 2
    assert len(data["attempts"]) == 2
    assert data["task"]["status"] == "COMPLETED"
    assert data["retry_decisions"][0]["decision_id"] == decision["decision_id"]
    assert data["retry_decisions"][0]["bound_attempt_id"] == data["attempts"][1]["attempt_id"]
    assert len([e for e in data["budget_events"] if e["event_type"] == "SETTLE"]) == 1
    assert data["units"][0]["sends"] == 2


@pytest.mark.parametrize("point,calls", [("after_dispatched", 1), ("after_reply", 2)])
def test_retry_dispatch_without_saved_result_requires_new_choice(
    harness, success_spec, point, calls
):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at(point))
    for _ in range(3):
        data = harness.run(retry_unknown=source)
        harness.run()
    assert len(harness.calls) == calls
    assert data["units"][0]["sends"] == 2
    assert data["totals"]["held_tokens"] == 1200
    assert data["task"]["status"] == "PAUSED_UNKNOWN"
    assert len(data["retry_decisions"]) == 1


@pytest.mark.parametrize("point", ["after_pause", "after_dispatched", "after_reply"])
def test_retry_can_resume_before_interrupt_checkpoint(harness, success_spec, point):
    spec = retry_spec(success_spec)
    if point == "after_reply":
        spec["responses"]["0:0:REVIEW:1"] = spec["responses"]["0:0:REVIEW:2"]
    harness.create(spec=spec)
    harness.crash(point)
    data = harness.run()
    source = data["attempts"][0]["attempt_id"]
    data = harness.run(retry_unknown=source)
    assert data["task"]["status"] == "COMPLETED"
    assert len(data["attempts"]) == 2
    assert data["totals"]["held_tokens"] == 600


@pytest.mark.parametrize("tokens,amount", [(900, 10_000_000), (5000, 1_000_000)])
def test_retry_budget_counts_original_held_and_leaves_pending(
    harness, success_spec, tokens, amount
):
    harness.create(spec=retry_spec(success_spec), tokens=tokens, amount=amount)
    source = harness.run()["attempts"][0]["attempt_id"]
    for _ in range(2):
        data = harness.run(retry_unknown=source)
        harness.run()
    assert data["task"]["status"] == "PAUSED_BUDGET"
    assert data["retry_decisions"][0]["status"] == "PENDING"
    assert len(data["attempts"]) == len(harness.calls) == 1
    assert len(data["budget_events"]) == 1


def test_retries_do_not_reset_send_limit(harness):
    harness.create("timeout", tokens=10000, amount=100_000_000)
    data = harness.run()
    for _ in range(6):
        data = harness.run(retry_unknown=data["attempts"][-1]["attempt_id"])
    assert len(harness.calls) == len(data["attempts"]) == 6
    assert data["units"][0]["sends"] == 6
    assert data["units"][0]["status"] == "PARTIAL_LIMIT"
    assert data["totals"]["held_tokens"] == 3600
    assert harness.run()["task"]["status"] == "PARTIAL"
    assert len(harness.calls) == 6


def test_decision_lookup_precedes_eligibility_and_binding_is_immutable(
    harness, success_spec, monkeypatch
):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    data = harness.run(retry_unknown=source)
    with harness.store() as store:

        def forbidden(*_):
            raise AssertionError("ELIGIBILITY_MUST_NOT_RUN_FOR_EXISTING_DECISION")

        monkeypatch.setattr(store, "_retry_source", forbidden)
        assert store.record_retry_decision(source) == data["retry_decisions"][0]
        for sql in (
            "UPDATE retry_decisions SET bound_attempt_id=NULL,status='PENDING'",
            "DELETE FROM retry_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="RETRY_DECISION_IMMUTABLE"):
                store.conn.execute(sql)


def test_legacy_task_cannot_enable_retry(harness):
    harness.create("timeout")
    with harness.store() as store:
        config = json.loads(store.task()["config"])
        config["schema_version"] = 3
        store.conn.execute(
            "UPDATE tasks SET config=?,config_digest=?", (json.dumps(config), digest(config))
        )
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="RETRY_NOT_SUPPORTED"):
        harness.run(retry_unknown=source)
    assert not harness.read()["retry_decisions"]
    assert len(harness.calls) == 1


def test_retry_rejects_completed_and_foreign_source(harness):
    harness.create()
    source = harness.run()["attempts"][0]["attempt_id"]
    for attempt_id, error in (
        (source, "RETRY_SOURCE_NOT_ELIGIBLE"),
        ("attempt_" + "0" * 24, "LEDGER_INTEGRITY"),
    ):
        with pytest.raises(AgentError, match=error):
            harness.run(retry_unknown=attempt_id)
    assert not harness.read()["retry_decisions"]


def test_retry_cli_and_plain_resume(harness, success_spec):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    args = ("resume", "--task", harness.task_id, "--state-dir", harness.state)
    for _ in range(2):
        result = cli(*args, "--retry-unknown", source)
        assert result.returncode == 0, result.stderr
        assert len(json.loads(result.stdout)["attempts"]) == 2
    assert cli(*args).returncode == 0
    assert "--retry-unknown" in cli("resume", "--help").stdout
    bad = cli(*args, "--retry-unknown", "synthetic-invalid-sensitive-argument")
    assert bad.returncode == 1
    assert "synthetic-invalid-sensitive-argument" not in bad.stderr


def test_retry_overrun_stops_other_units_across_resume(harness, success_spec):
    spec = retry_spec(success_spec)
    spec["responses"]["0:0:REVIEW:2"]["usage"] = {
        "input_tokens": 500,
        "output_tokens": 250,
        "total_tokens": 750,
    }
    harness.create(spec=spec, diff="two-files")
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_completed"))
    for _ in range(3):
        data = harness.run(retry_unknown=source)
    assert data["task"]["status"] == "STOPPED_BUDGET_BOUND"
    assert [u["status"] for u in data["units"]] == ["DONE", "PENDING"]
    assert data["totals"]["held_tokens"] == 600
    assert data["totals"]["settled_tokens"] == 750
    assert len(harness.calls) == 2


def test_retry_deepseek_reuses_exact_wire_request(tmp_path, monkeypatch):
    import httpx
    from conftest import ROOT
    from test_deepseek_provider import API_KEY, response

    from review_agent.providers import DeepSeekProvider
    from review_agent.safety import Safety

    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    state = tmp_path / "state"
    task = app.create_task(
        ROOT / "examples/diffs/empty-list.diff",
        None,
        state,
        provider_name="deepseek",
        max_tokens=50000,
        max_cost_nusd=100_000_000,
    )
    bodies = []

    def handler(request):
        bodies.append(request.content)
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(500 if len(bodies) == 1 else 200, json=response())

    def provider(config, observer=None):
        return (
            DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler)),
            Safety((API_KEY,)),
        )

    monkeypatch.setattr(app, "_provider", provider)
    first = app.execute(task, state)["attempts"][0]["attempt_id"]
    data = app.execute(task, state, retry_unknown=first)
    app.execute(task, state, retry_unknown=first)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    trace = app.trace(data)
    for call in trace["calls"]:
        assert call["request"]["data"] == json.loads(bodies[0])
    assert data["attempts"][0]["quote"] == data["attempts"][1]["quote"]
    assert data["attempts"][0]["fee_status"] == "HELD"
    assert data["attempts"][1]["fee_status"] == "SETTLED"
    assert API_KEY not in json.dumps(data)
