import json
import sqlite3

import pytest
from test_retry import crash_at, retry_spec

from review_agent import app
from review_agent.contracts import AgentError


@pytest.mark.parametrize("point", ["after_dispatch_status", "before_dispatch_commit"])
def test_dispatch_transaction_rolls_back_before_any_send(harness, point):
    harness.create()
    harness.crash(point)
    data = harness.read()
    assert not harness.calls
    assert data["attempts"][0]["call_status"] == "RESERVED"
    assert data["units"][0]["sends"] == 0
    assert not data["attempt_timings"]
    assert data["spans"][0]["status"] == "RESERVED"
    assert len(data["budget_events"]) == 1
    assert harness.run()["task"]["status"] == "COMPLETED"
    assert len(harness.calls) == 1


@pytest.mark.parametrize(
    "point",
    ["after_result_artifact", "after_result_status", "after_settle", "before_complete_commit"],
)
def test_result_cost_and_send_block_roll_back_together(harness, point):
    harness.create("quote-overrun")
    harness.crash(point)
    data = harness.read()
    assert data["attempts"][0]["call_status"] == "DISPATCHED"
    assert data["attempts"][0]["result_ref"] is None
    assert data["operations"][0]["result_ref"] is None
    assert data["attempts"][0]["actual_tokens"] is None
    assert data["attempt_timings"][0]["completed_at"] is None
    assert len(data["budget_events"]) == 1
    assert "result" not in data["artifact_kinds"].values()
    assert not data["task"]["send_block_reason"]
    assert harness.run()["task"]["status"] == "PAUSED_UNKNOWN"
    assert len(harness.calls) == 1
    assert harness.read()["totals"]["held_tokens"] == 600


@pytest.mark.parametrize("point", ["after_validation_artifact", "before_validation_commit"])
def test_validation_transaction_rolls_back_without_losing_paid_result(harness, point):
    harness.create()
    harness.crash(point)
    data = harness.read()
    assert data["attempts"][0]["call_status"] == "COMPLETED"
    assert data["totals"]["settled_tokens"] == 300
    assert not data["findings"] and not data["evidence"]
    assert "validation" not in data["artifact_kinds"].values()
    assert data["units"][0]["validation_ref"] is None
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert len(data["findings"]) == len(harness.calls) == 1


def test_cancelled_retry_binding_is_returned_without_new_attempt(harness, success_spec):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_retry_bound"))
    with harness.store() as store:
        bound = store.retry_decision(source)["bound_attempt_id"]
        store.cancel_reserved(bound)
        store.cancel_reserved(bound)
    for _ in range(3):
        data = harness.run(retry_unknown=source)
    assert data["retry_decisions"][0]["bound_attempt_id"] == bound
    assert data["attempts"][1]["fee_status"] == "RELEASED"
    assert len(data["attempts"]) == 2
    assert len(harness.calls) == 1
    assert data["totals"]["held_tokens"] == 600
    assert len([e for e in data["budget_events"] if e["event_type"] == "RELEASE"]) == 1


def test_bound_retry_reserved_cannot_bypass_task_stop(harness, success_spec):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_retry_bound"))
    with harness.store() as store:
        store.conn.execute(
            "UPDATE tasks SET send_block_reason='BUDGET_BOUND_VIOLATION',"
            "send_block_attempt_id=?,status='STOPPED_BUDGET_BOUND'",
            (source,),
        )
    for _ in range(2):
        data = harness.run(retry_unknown=source)
    assert data["task"]["status"] == "STOPPED_BUDGET_BOUND"
    assert data["attempts"][1]["call_status"] == "CANCELLED"
    assert data["totals"]["held_tokens"] == 600
    assert len(harness.calls) == 1


def test_missing_checkpoint_result_stops_before_authorization_binding(harness, success_spec):
    # A result referenced by the ledger and earlier checkpoints must remain available.
    spec = retry_spec(success_spec)
    spec["responses"]["0:0:REVIEW:1"] = success_spec["responses"]["0:0:REVIEW:1"]
    spec["responses"]["1:0:REVIEW:1"] = {"error": "timeout"}
    harness.create(spec=spec, diff="two-files")
    data = harness.run()
    source = data["attempts"][1]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_decision"))
    # Intentionally corrupt only this disposable test DB, retaining checkpoint rows.
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        connection.execute(
            "DELETE FROM artifacts WHERE artifact_id=?", (data["attempts"][0]["result_ref"],)
        )
    with pytest.raises(AgentError, match="LEDGER_INTEGRITY"):
        harness.run()
    after = harness.read()
    assert after["retry_decisions"][0]["status"] == "PENDING"
    assert len(after["attempts"]) == len(harness.calls) == 2


def test_broken_binding_cannot_be_replaced_by_another_attempt(harness, success_spec):
    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_retry_bound"))
    bound = harness.read()["retry_decisions"][0]["bound_attempt_id"]
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        connection.execute("DELETE FROM attempts WHERE attempt_id=?", (bound,))
    with pytest.raises(AgentError, match="LEDGER_INTEGRITY"):
        harness.run(retry_unknown=source)
    assert len(harness.calls) == 1
    assert harness.read()["retry_decisions"][0]["bound_attempt_id"] == bound


def test_late_result_cannot_settle_original_unknown_after_retry(harness, success_spec):
    from review_agent.contracts import StoredResult

    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    data = harness.run(retry_unknown=source)
    completed = data["attempts"][1]
    with harness.store() as store:
        before = json.dumps(store.report_snapshot(), sort_keys=True)
        result = StoredResult.model_validate(store.artifact(completed["result_ref"]))
        with pytest.raises(AgentError, match="INVALID_ATTEMPT_TRANSITION"):
            store.complete_attempt(source, result)
        assert json.dumps(store.report_snapshot(), sort_keys=True) == before


def test_retry_send_limit_finishes_only_its_unit(harness):
    spec = {
        "default": {"error": "timeout"},
        "responses": {
            "1:0:REVIEW:1": {
                "body": '{"action":"submit_review","findings":[]}',
                "usage": {"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
            }
        },
    }
    harness.create(spec=spec, diff="two-files", tokens=10000)
    data = harness.run()
    for _ in range(6):
        data = harness.run(retry_unknown=data["attempts"][-1]["attempt_id"])
    assert [u["status"] for u in data["units"]] == ["PARTIAL_LIMIT", "DONE"]
    assert [u["sends"] for u in data["units"]] == [6, 1]
    assert len(harness.calls) == 7
    assert data["totals"]["held_tokens"] == 3600
    assert data["task"]["status"] == "PARTIAL"
    harness.run()
    assert len(harness.calls) == 7


def test_latest_checkpoint_missing_result_ref_blocks_pending_choice(harness, success_spec):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from review_agent.gateway import Gateway
    from review_agent.graph import build_graph
    from review_agent.providers import FixtureProvider
    from review_agent.safety import Safety

    harness.create(spec=retry_spec(success_spec))
    source = harness.run()["attempts"][0]["attempt_id"]
    with pytest.raises(AgentError, match="TEST_CRASH"):
        harness.run(retry_unknown=source, fault=crash_at("after_decision"))
    with (
        harness.store() as store,
        sqlite3.connect(store.path, check_same_thread=False) as connection,
    ):
        saver = SqliteSaver(connection)
        port = FixtureProvider(harness.fixture)
        graph = build_graph(store, port, Gateway(store, port, Safety()), saver)
        graph.update_state(
            {"configurable": {"thread_id": harness.task_id}},
            {"last_result_ref": "result_" + "0" * 24},
            as_node="review_agent",
        )
    with pytest.raises(AgentError, match="LEDGER_INTEGRITY"):
        harness.run()
    data = harness.read()
    assert data["retry_decisions"][0]["status"] == "PENDING"
    assert len(data["attempts"]) == len(harness.calls) == 1
