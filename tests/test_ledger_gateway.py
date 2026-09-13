import sqlite3
from pathlib import Path

import pytest

from review_agent.contracts import AgentError, StoredResult, Usage, stable_id
from review_agent.providers import FixtureProvider
from review_agent.review import make_request


def reserve(store, index=0):
    unit = store.units()[index]
    provider = FixtureProvider(Path(store.config().fixture_path))
    request = make_request(unit, store.snapshot(), provider.output_limit(store.config()))
    operation = stable_id("operation", store.task()["task_id"], unit["unit_id"], 0, "REVIEW", 0)
    return store.reserve_attempt(
        operation, unit["unit_id"], request, provider.quote(request, store.config())
    )


@pytest.mark.parametrize("tokens,amount", [(599, 10_000_000), (5000, 799999)])
def test_budget_refuses_without_reservation(harness, tokens, amount):
    harness.create(tokens=tokens, amount=amount)
    data = harness.run()
    assert data["task"]["status"] == "PAUSED_BUDGET"
    assert not data["attempts"] and not harness.calls
    harness.run()
    assert not harness.calls


def test_actual_cost_and_idempotent_settlement(harness):
    harness.create()
    data = harness.run()
    row = data["attempts"][0]
    with harness.store() as store:
        result = StoredResult.model_validate(store.artifact(row["result_ref"]))
        store.complete_attempt(row["attempt_id"], result)
        store.complete_attempt(row["attempt_id"], result)
        assert len(store.rows("SELECT * FROM budget_events WHERE event_type='SETTLE'")) == 1
        with pytest.raises(AgentError, match="RESULT_CONFLICT"):
            store.complete_attempt(row["attempt_id"], result.model_copy(update={"safe_body": "x"}))
    assert data["totals"] == {
        "settled_tokens": 300,
        "settled_cost_nusd": 350000,
        "held_tokens": 0,
        "held_cost_nusd": 0,
    }


def test_request_persisted_before_reservation_is_reused(harness):
    harness.create()
    harness.crash("after_request")
    with harness.store() as store:
        assert len(store.rows("SELECT * FROM operations")) == 1
        assert len(store.rows("SELECT * FROM artifacts WHERE kind='request'")) == 1
        assert not store.rows("SELECT * FROM attempts")
    data = harness.run()
    assert len(data["operations"]) == 1
    assert len(data["attempts"]) == 1
    assert len(harness.calls) == 1


def test_cancel_only_reserved(harness):
    harness.create(diff="two-files")
    with harness.store() as store:
        first, second = reserve(store, 0), reserve(store, 1)
        store.cancel_reserved(first["attempt_id"])
        store.cancel_reserved(first["attempt_id"])
        assert len(store.rows("SELECT * FROM budget_events WHERE event_type='RELEASE'")) == 1
        assert store.mark_dispatched(second["attempt_id"])
        assert not store.mark_dispatched(second["attempt_id"])
        for _ in range(2):
            with pytest.raises(AgentError, match="INVALID_ATTEMPT_TRANSITION"):
                store.cancel_reserved(second["attempt_id"])
            store.mark_unknown(second["attempt_id"])
        assert store.totals()["held_tokens"] == 600
        assert store.units()[1]["sends"] == 1


def test_not_sent_exception_stays_unknown(harness):
    harness.create(spec={"default": {"error": "not_sent"}})
    data = harness.run()
    assert data["attempts"][0]["call_status"] == "UNKNOWN"
    assert data["attempts"][0]["fee_status"] == "HELD"
    harness.run()
    assert len(harness.calls) == 1


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 500, "output_tokens": 250, "total_tokens": 750},
        {"input_tokens": 650, "output_tokens": 50, "total_tokens": 700},  # tokens only
        {"input_tokens": 100, "output_tokens": 450, "total_tokens": 550},  # money only
    ],
)
@pytest.mark.parametrize("crash", [False, True])
def test_quote_overrun_blocks_later_units_and_resume(harness, success_spec, usage, crash):
    success_spec["responses"]["0:0:REVIEW:1"]["usage"] = usage
    harness.create(spec=success_spec, diff="two-files")
    if crash:
        harness.crash("after_completed")
        # Block and actual charge already exist before graph checkpoint advances.
        with harness.store() as store:
            assert store.task()["send_block_reason"] == "BUDGET_BOUND_VIOLATION"
            assert store.totals()["settled_tokens"] == usage["total_tokens"]
    data = harness.run()
    for _ in range(2):
        data = harness.run()
    assert len(harness.calls) == 1
    assert data["task"]["status"] == "STOPPED_BUDGET_BOUND"
    assert [u["status"] for u in data["units"]] == ["DONE", "PENDING"]
    assert data["units"][1]["stop_reason"] == "BUDGET_BOUND_VIOLATION"
    assert 5000 - data["totals"]["settled_tokens"] > 600
    assert 10_000_000 - data["totals"]["settled_cost_nusd"] > 800000
    with harness.store() as store:
        assert len(store.rows("SELECT * FROM budget_events WHERE event_type='SETTLE'")) == 1
        with pytest.raises(AgentError, match="BUDGET_BOUND_VIOLATION"):
            reserve(store, 1)
        with pytest.raises(sqlite3.IntegrityError, match="TASK_SEND_BLOCK_IMMUTABLE"):
            store.conn.execute("UPDATE tasks SET send_block_reason=NULL,send_block_attempt_id=NULL")


def test_existing_reserved_cannot_send_after_task_block(harness):
    harness.create(diff="two-files")
    with harness.store() as store:
        first, second = reserve(store, 0), reserve(store, 1)
        store.mark_dispatched(first["attempt_id"])
        result = StoredResult(
            result_status="VALID",
            completion_state="COMPLETE",
            usage=Usage(input_tokens=500, output_tokens=250, total_tokens=750),
        )
        store.complete_attempt(first["attempt_id"], result)
        with pytest.raises(AgentError, match="BUDGET_BOUND_VIOLATION"):
            store.mark_dispatched(second["attempt_id"])
        assert store.attempt(second["operation_id"])["call_status"] == "RESERVED"
        store.finalize()
        assert store.attempt(second["operation_id"])["fee_status"] == "RELEASED"


def test_overrun_transaction_failure_rolls_back_result_and_charge(harness):
    harness.create("quote-overrun")
    with harness.store() as store:
        store.conn.executescript("""
            CREATE TRIGGER test_fail_block BEFORE UPDATE OF send_block_reason ON tasks
            BEGIN SELECT RAISE(ABORT, 'INJECTED_TRANSACTION_FAILURE'); END;
        """)
    with pytest.raises(AgentError, match="NODE_FAILED"):
        harness.run()
    with harness.store() as store:
        attempt = store.rows("SELECT * FROM attempts")[0]
        assert attempt["call_status"] == "DISPATCHED"
        assert attempt["result_ref"] is None
        assert not store.rows("SELECT * FROM budget_events WHERE event_type='SETTLE'")
        assert store.task()["send_block_reason"] is None
        store.conn.execute("DROP TRIGGER test_fail_block")
    assert harness.run()["task"]["status"] == "PAUSED_UNKNOWN"
    assert len(harness.calls) == 1


def test_fingerprint_and_send_limit(harness):
    harness.create()
    with harness.store() as store:
        attempt = reserve(store)
        with pytest.raises(AgentError, match="REQUEST_FINGERPRINT_MISMATCH"):
            store.find_operation(attempt["operation_id"], "wrong")
        store.conn.execute("UPDATE review_units SET sends=6")
        with pytest.raises(AgentError, match="ROUND_LIMIT"):
            store.mark_dispatched(attempt["attempt_id"])
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_LIMIT"
    assert data["attempts"][0]["fee_status"] == "RELEASED"
    assert not harness.calls


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"input_tokens": True, "output_tokens": 0, "total_tokens": 1},
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 9},
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "source": "deepseek",
            "cache_hit_input_tokens": 0,
            "cache_miss_input_tokens": 1,
        },
    ],
)
def test_unreliable_usage_remains_held(harness, success_spec, raw):
    success_spec["responses"]["0:0:REVIEW:1"]["usage"] = raw
    harness.create(spec=success_spec)
    data = harness.run()
    assert data["attempts"][0]["fee_status"] == "HELD"
    assert data["totals"]["settled_tokens"] == 0
    assert data["totals"]["held_tokens"] == 600
