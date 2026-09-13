import json
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from review_agent import app
from review_agent.contracts import AgentError


@pytest.mark.parametrize(
    "point,expected_calls,status",
    [
        ("after_reserved", 1, "COMPLETED"),
        ("after_dispatched", 0, "PAUSED_UNKNOWN"),
        ("after_reply", 1, "PAUSED_UNKNOWN"),
        ("after_completed", 1, "COMPLETED"),
    ],
)
def test_restart_from_real_checkpoints(harness, point, expected_calls, status):
    harness.create()
    harness.crash(point)
    # Resume must use the stored snapshot, even if the original diff changes.
    harness.diff.write_text("changed after interruption")
    before = harness.read()["attempts"][0]
    data = harness.run()
    assert data["task"]["status"] == status
    assert data["attempts"][0]["attempt_id"] == before["attempt_id"]
    harness.run()
    assert len(harness.calls) == expected_calls
    with harness.store() as store:
        assert len(store.rows("SELECT * FROM budget_events WHERE event_type='RESERVE'")) == 1
    if status == "COMPLETED":
        assert len(data["findings"]) == 1
        assert data["totals"]["settled_tokens"] == 300
    else:
        assert data["totals"]["held_tokens"] == 600
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        saver = SqliteSaver(connection)
        checkpoints = list(saver.list({"configurable": {"thread_id": harness.task_id}}))
        assert len(checkpoints) > 2
        for cp in checkpoints:
            state = cp.checkpoint["channel_values"]
            assert "safe_diff" not in state and "messages" not in state


def test_saved_invalid_result_reused_before_checkpoint(harness):
    harness.create("malformed")
    harness.crash("after_completed")
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    assert data["attempts"][0]["result_status"] == "FORMAT_INVALID"
    assert data["attempts"][0]["fee_status"] == "SETTLED"
    assert len(harness.calls) == 1


def test_pause_has_real_interrupt_and_repeat_resume_does_not_send(harness):
    harness.create("timeout")
    harness.run()
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        cp = SqliteSaver(connection).get_tuple({"configurable": {"thread_id": harness.task_id}})
        assert any(channel == "__interrupt__" for _, channel, _ in cp.pending_writes)
    for _ in range(3):
        assert harness.run()["task"]["status"] == "PAUSED_UNKNOWN"
    assert len(harness.calls) == 1


@pytest.mark.parametrize("change", ["fixture", "config"])
def test_resume_refuses_changed_configuration(harness, change):
    harness.create()
    harness.crash("after_reserved")
    if change == "fixture":
        harness.fixture.write_text('{"default":{"error":"timeout"}}')
        expected = "FIXTURE_FINGERPRINT_MISMATCH"
    else:
        with harness.store() as store:
            config = store.config().model_dump()
            config["schema_version"] = 999
            store.conn.execute("UPDATE tasks SET config=?", (json.dumps(config),))
        expected = "CONFIG_VERSION_MISMATCH"
    with pytest.raises(AgentError, match=expected):
        harness.run()
    assert not harness.calls
