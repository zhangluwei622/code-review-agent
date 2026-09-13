import os

import pytest
from test_process_recovery import sends, worker
from test_tool_loop import ZERO, reply, spec, tool

from review_agent import app

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGKILL suite requires POSIX")


@pytest.mark.parametrize(
    "point,ordinal,state,tool_state,executions,model_sends",
    [
        ("before_tool_register_commit", 1, "COMPLETED", "SUCCEEDED", 1, 2),
        ("after_tool_registered", 1, "COMPLETED", "SUCCEEDED", 1, 2),
        ("before_tool_running_commit", 1, "COMPLETED", "SUCCEEDED", 1, 2),
        ("after_tool_running", 1, "COMPLETED", "INTERRUPTED", 0, 2),
        ("after_tool_output", 1, "COMPLETED", "INTERRUPTED", 1, 2),
        ("before_tool_complete_commit", 1, "COMPLETED", "INTERRUPTED", 1, 2),
        ("after_tool_completed", 1, "COMPLETED", "SUCCEEDED", 1, 2),
        ("after_request", 2, "COMPLETED", "SUCCEEDED", 1, 2),
        ("after_dispatched", 2, "PAUSED_UNKNOWN", "SUCCEEDED", 1, 1),
        ("after_completed", 2, "COMPLETED", "SUCCEEDED", 1, 2),
    ],
)
def test_sigkill_tool_and_followup_boundaries(
    harness, tmp_path, point, ordinal, state, tool_state, executions, model_sends
):
    harness.create(spec=spec(tool(), ZERO), schema=5)
    calls, tool_calls = tmp_path / "sends.jsonl", tmp_path / "tools.jsonl"
    worker(harness, calls, point=point, ordinal=ordinal, tool_calls=tool_calls)
    for _ in range(2):
        worker(harness, calls, tool_calls=tool_calls)
    data = harness.read()
    assert data["task"]["status"] == state
    assert len(data["tool_calls"]) == 1 and data["tool_calls"][0]["slot_no"] == 1
    assert data["tool_calls"][0]["status"] == tool_state
    assert sends(calls) == model_sends and sends(tool_calls) == executions
    assert len(data["attempts"]) == 2
    assert data["totals"]["held_tokens"] == (600 if state == "PAUSED_UNKNOWN" else 0)
    app.trace(data)


@pytest.mark.parametrize(
    "point,state,count",
    [
        ("after_request", "COMPLETED", 3),
        ("after_dispatched", "PAUSED_UNKNOWN", 2),
        ("after_completed", "COMPLETED", 3),
    ],
)
def test_sigkill_later_round_repair(harness, tmp_path, point, state, count):
    value = spec(tool(), reply('{"action":'))
    value["responses"]["0:1:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=5, repairs=1)
    calls, tool_calls = tmp_path / "sends.jsonl", tmp_path / "tools.jsonl"
    worker(harness, calls, point=point, ordinal=3, tool_calls=tool_calls)
    worker(harness, calls, tool_calls=tool_calls)
    data = harness.read()
    assert data["task"]["status"] == state
    assert sends(calls) == count and sends(tool_calls) == 1
    assert (
        data["operation_contexts"][-1]["source_result_ref"] == data["operations"][1]["result_ref"]
    )
    assert data["operation_contexts"][-1]["turn_no"] == 1
    app.trace(data)


def test_sigkill_followup_retry_binding_plain_resume(harness, tmp_path):
    value = spec(tool(), {"error": "timeout"})
    value["responses"]["0:1:REVIEW:2"] = ZERO
    harness.create(spec=value, schema=5)
    calls, tool_calls = tmp_path / "sends.jsonl", tmp_path / "tools.jsonl"
    worker(harness, calls, tool_calls=tool_calls)
    source = harness.read()["attempts"][-1]["attempt_id"]
    worker(harness, calls, point="after_retry_bound", retry=source, tool_calls=tool_calls)
    worker(harness, calls, tool_calls=tool_calls)
    worker(harness, calls, retry=source, tool_calls=tool_calls)
    data = harness.read()
    assert data["task"]["status"] == "COMPLETED"
    assert sends(calls) == 3 and sends(tool_calls) == 1
    assert len(data["retry_decisions"]) == len(data["tool_calls"]) == 1
    assert data["totals"]["held_tokens"] == 600
