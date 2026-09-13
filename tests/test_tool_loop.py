import copy
import json
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from review_agent import app
from review_agent.contracts import AgentError
from review_agent.report import render
from review_agent.tools.runner import ToolRunner

CONTEXT_DIFF = """diff --git a/context.py b/context.py
--- a/context.py
+++ b/context.py
@@ -1,8 +1,8 @@
 # callers guarantee a nonempty sequence
 # omitted context must be fetched explicitly
 def avg(values):
     total = sum(values)
-    count = max(1, len(values))
+    count = len(values)
     return total / count
 # trailer
 # context outside the first preview
"""


def reply(decision):
    return {
        "body": json.dumps(decision) if isinstance(decision, dict) else decision,
        "usage": {"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
    }


def tool(name="read_hunk", **arguments):
    return reply(
        {"action": "request_tool", "name": name, "arguments": arguments or {"hunk_id": "h0001"}}
    )


ZERO = reply({"action": "submit_review", "findings": []})


def spec(*responses):
    return {"responses": {f"0:{i}:REVIEW:1": r for i, r in enumerate(responses)}}


def test_read_hunk_adds_context_and_bidirectional_request_links(harness):
    harness.create(text=CONTEXT_DIFF, spec=spec(tool(), ZERO), schema=5)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    first, second = harness.calls
    assert first["hunks"][0]["omitted_context_lines"] > 0
    assert "callers guarantee" not in json.dumps(first)
    assert "callers guarantee" in json.dumps(second["loop"]["tool_history"])
    assert len(data["tool_calls"]) == 1 and len(data["attempts"]) == 2
    call = data["tool_calls"][0]
    request = data["artifacts"][call["request_ref"]]
    assert request["source_request_ref"] == data["operations"][0]["request_ref"]
    assert second["loop"]["input_tool_result_ref"] == call["result_ref"]
    trace = app.trace(data, attempt_id=data["attempts"][-1]["attempt_id"])
    assert trace["tool_inputs"][0]["call"]["tool_call_id"] == call["tool_call_id"]
    assert "工具调用与上下文" in render(data)
    for _ in range(2):
        assert harness.run()["tool_calls"] == data["tool_calls"]
    assert len(harness.calls) == 2


@pytest.mark.parametrize("final", [ZERO, reply({"action": "abstain", "reason": "missing context"})])
def test_four_tools_then_summary_without_reset(harness, final):
    harness.create(spec=spec(tool(), tool(), tool(), tool(), final), schema=5)
    data = harness.run()
    assert len(data["tool_calls"]) == 4 and len(harness.calls) == 5
    assert harness.calls[-1]["tools"] == [] and harness.calls[-1]["loop"]["final_only"]
    assert data["units"][0]["status"] in ("DONE", "ABSTAINED")
    assert [c["slot_no"] for c in data["tool_calls"]] == [1, 2, 3, 4]
    assert len({c["tool_call_id"] for c in data["tool_calls"]}) == 4
    assert data["totals"]["settled_tokens"] == 1500


def test_tools_disabled_initially_still_allows_summary(harness):
    harness.create(spec=spec(ZERO), schema=5, tools=0)
    assert harness.run()["task"]["status"] == "COMPLETED"
    assert harness.calls[0]["tools"] == []


def test_ignored_summary_only_is_bounded(harness):
    harness.create(spec=spec(tool(), tool(), tool(), tool(), tool()), schema=5)
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_LIMIT"
    assert len(data["tool_calls"]) == 4 and len(harness.calls) == 5


def test_parameter_failure_consumes_slot_without_format_repair(harness):
    harness.create(spec=spec(tool(path="/tmp/forbidden"), ZERO), schema=5, repairs=1)
    data = harness.run()
    assert data["tool_calls"][0]["status"] == "REJECTED"
    assert data["tool_calls"][0]["slot_no"] == 1
    assert len(data["operations"]) == 2
    assert all(c["kind"] == "REVIEW" for c in data["operation_contexts"])


def test_repair_binds_later_round_and_keeps_identical_tool_inputs(harness):
    value = spec(tool(), reply('{"action":'))
    value["responses"]["0:1:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=5, repairs=1)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    original, repaired = data["operations"][1:]
    context = data["operation_contexts"][-1]
    assert context["turn_no"] == 1 and context["kind"] == "REPAIR"
    assert context["source_operation_id"] == original["operation_id"]
    assert context["source_result_ref"] == original["result_ref"]
    assert repaired["result_ref"] != original["result_ref"]
    assert harness.calls[1]["loop"] == harness.calls[2]["loop"]
    assert data["totals"]["settled_tokens"] == 900
    trace = app.trace(data, attempt_id=data["attempts"][-1]["attempt_id"])
    assert trace["source_call"]["operation_context"]["turn_no"] == 1
    assert len(trace["tool_inputs"]) == 1


def test_repair_allowance_does_not_reset_on_next_turn(harness):
    value = spec(reply('{"action":'), reply('{"action":'))
    value["responses"]["0:0:REPAIR:1"] = tool()
    harness.create(spec=value, schema=5, repairs=1)
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    assert len(harness.calls) == 3 and len(data["tool_calls"]) == 1
    assert sum(c["kind"] == "REPAIR" for c in data["operation_contexts"]) == 1


@pytest.mark.parametrize(
    "point,physical,status",
    [
        ("after_tool_request_artifact", 1, "SUCCEEDED"),
        ("before_tool_register_commit", 1, "SUCCEEDED"),
        ("after_tool_registered", 1, "SUCCEEDED"),
        ("before_tool_running_commit", 1, "SUCCEEDED"),
        ("after_tool_running", 0, "INTERRUPTED"),
        ("after_tool_output", 1, "INTERRUPTED"),
        ("after_tool_result_artifact", 1, "INTERRUPTED"),
        ("before_tool_complete_commit", 1, "INTERRUPTED"),
        ("after_tool_completed", 1, "SUCCEEDED"),
    ],
)
def test_tool_transaction_checkpoint_gaps(harness, monkeypatch, point, physical, status):
    harness.create(spec=spec(tool(), ZERO), schema=5)
    actual = []
    original = ToolRunner.run

    def observe(self, *args):
        actual.append(1)
        return original(self, *args)

    monkeypatch.setattr(ToolRunner, "run", observe)
    harness.crash(point)
    data = harness.run()
    harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert len(actual) == physical and len(harness.calls) == 2
    assert len(data["tool_calls"]) == 1 and data["tool_calls"][0]["status"] == status
    assert len(data["budget_events"]) == 4
    with harness.store() as store:
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_followup_unknown_reuses_authorization_and_keeps_tool_result(harness):
    value = spec(tool(), {"error": "timeout"})
    value["responses"]["0:1:REVIEW:2"] = ZERO
    harness.create(spec=value, schema=5)
    data = harness.run()
    unknown = data["attempts"][-1]["attempt_id"]
    with harness.store() as store:
        store.record_retry_decision(unknown)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert len(data["tool_calls"]) == 1 and len(harness.calls) == 3
    assert harness.calls[1] == harness.calls[2]
    assert data["totals"]["held_tokens"] == 600
    harness.run(retry_unknown=unknown)
    assert len(harness.calls) == 3


def test_summary_budget_shortage_does_not_reset_tools(harness):
    harness.create(spec=spec(tool(), ZERO), schema=5, tokens=800)
    for _ in range(2):
        data = harness.run()
    assert data["task"]["status"] == "PAUSED_BUDGET"
    assert len(data["tool_calls"]) == len(harness.calls) == 1


def test_quote_overrun_blocks_tools_and_later_units_across_resume(harness):
    first = tool()
    first["usage"] = {"input_tokens": 600, "output_tokens": 50, "total_tokens": 650}
    harness.create(spec=spec(first, ZERO), schema=5, diff="two-files")
    for _ in range(2):
        data = harness.run()
    assert data["task"]["status"] == "STOPPED_BUDGET_BOUND"
    assert not data["tool_calls"] and len(harness.calls) == 1
    assert data["units"][1]["status"] == "PENDING"


def test_tool_association_tampering_is_rejected_before_resume(harness):
    harness.create(spec=spec(tool(), ZERO), schema=5)
    data = harness.run()
    broken = copy.deepcopy(data)
    broken["artifacts"][data["tool_calls"][0]["result_ref"]]["records"] = []
    with pytest.raises(AgentError, match="AUDIT_INTEGRITY_ERROR"):
        app.trace(broken)
    with harness.store() as store:
        store.conn.execute(
            "UPDATE artifacts SET data='{}' WHERE artifact_id=?",
            (data["tool_calls"][0]["result_ref"],),
        )
    with pytest.raises(AgentError, match="TOOL_LEDGER_INTEGRITY"):
        harness.run()
    assert len(harness.calls) == 2


def test_checkpoint_has_only_tool_and_model_references(harness):
    harness.create(text=CONTEXT_DIFF, spec=spec(tool(), ZERO), schema=5)
    harness.run()
    with sqlite3.connect(app.task_path(harness.state, harness.task_id)) as connection:
        for cp in SqliteSaver(connection).list({"configurable": {"thread_id": harness.task_id}}):
            values = cp.checkpoint["channel_values"]
            assert "tool_history" not in values and "callers guarantee" not in repr(values)
            assert "tool_registry" not in values


def test_last_model_send_is_reserved_for_summary_after_retry(harness):
    value = spec({"error": "timeout"}, tool(), tool(), tool(), ZERO)
    value["responses"]["0:0:REVIEW:2"] = tool()
    harness.create(spec=value, schema=5)
    first = harness.run()["attempts"][0]["attempt_id"]
    data = harness.run(retry_unknown=first)
    assert data["task"]["status"] == "COMPLETED"
    assert len(harness.calls) == 6 and len(data["tool_calls"]) == 4
    assert harness.calls[-1]["loop"]["final_only"] and not harness.calls[-1]["tools"]
    assert data["totals"]["held_tokens"] == 600


def test_final_summary_can_use_remaining_repair_slot(harness):
    value = spec(tool(), tool(), tool(), tool(), reply('{"action":'))
    value["responses"]["0:4:REPAIR:1"] = ZERO
    harness.create(spec=value, schema=5, repairs=1)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert len(harness.calls) == 6 and len(data["tool_calls"]) == 4
    assert harness.calls[-1]["loop"]["final_only"]
    assert harness.calls[-1]["tools"] == []
    assert data["operation_contexts"][-1]["turn_no"] == 4


def test_unseen_evidence_rejected_until_context_is_read(harness, success_spec):
    finding = json.loads(success_spec["responses"]["0:0:REVIEW:1"]["body"])["findings"][0]
    finding.update(
        hunk_id="h0001",
        side="new",
        line=5,
        evidence=["h0001:new:5"],
        expectation_evidence=["h0001:new:1"],
    )
    summary = reply({"action": "submit_review", "findings": [finding]})
    harness.create(text=CONTEXT_DIFF, spec=spec(summary), schema=5)
    assert harness.run()["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    harness.create(text=CONTEXT_DIFF, spec=spec(tool(), summary), schema=5)
    data = harness.run()
    assert data["units"][0]["status"] == "DONE" and len(data["findings"]) == 1
    trace = app.trace(data, finding_id=data["findings"][0]["finding_id"])
    assert len(trace["tool_inputs"]) == 1


def test_later_round_repair_evidence_failure_is_terminal(harness, success_spec):
    decision = json.loads(success_spec["responses"]["0:0:REVIEW:1"]["body"])
    decision["findings"][0]["evidence"] = ["h9999:new:123"]
    value = spec(tool(), reply('{"action":'))
    value["responses"]["0:1:REPAIR:1"] = reply(decision)
    harness.create(spec=value, schema=5, repairs=1)
    data = harness.run()
    assert data["units"][0]["status"] == "PARTIAL_INVALID_RESULT"
    assert data["units"][0]["stop_reason"] == "INVALID_FINDING_EVIDENCE"
    assert len(harness.calls) == 3 and data["totals"]["settled_tokens"] == 900


def test_saved_tool_result_is_reused_after_registry_changes(harness, monkeypatch):
    from review_agent.tools.registry import ToolRegistry
    from review_agent.tools.service import ToolService

    harness.create(spec=spec(tool(), ZERO), schema=5)
    harness.crash("after_tool_completed")
    data = harness.read()

    def mismatch(self, frozen):
        raise AgentError("TOOL_REGISTRY_MISMATCH")

    monkeypatch.setattr(ToolRegistry, "require_snapshot", mismatch)
    with harness.store() as store:
        call = data["tool_calls"][0]
        result = ToolService(store).run(call["unit_id"], call["source_result_ref"])
        assert result == data["artifacts"][call["result_ref"]]


def test_pending_tool_stops_on_registry_changes(harness, monkeypatch):
    from review_agent.tools.registry import ToolRegistry

    harness.create(spec=spec(tool(), ZERO), schema=5)
    harness.crash("after_tool_registered")

    def mismatch(self, frozen):
        raise AgentError("TOOL_REGISTRY_MISMATCH")

    monkeypatch.setattr(ToolRegistry, "require_snapshot", mismatch)
    with pytest.raises(AgentError, match="TOOL_REGISTRY_MISMATCH"):
        harness.run()
    assert harness.read()["tool_calls"][0]["status"] == "PENDING"
    assert len(harness.calls) == 1


def test_old_checkpoint_cannot_repeat_tool_or_regress_summary(harness):
    from review_agent.gateway import Gateway
    from review_agent.graph import build_graph
    from review_agent.providers import FixtureProvider
    from review_agent.safety import Safety

    harness.create(spec=spec(tool(), ZERO), schema=5)
    data = harness.run()
    with (
        harness.store() as store,
        sqlite3.connect(store.path, check_same_thread=False) as connection,
    ):
        saver = SqliteSaver(connection)
        provider = FixtureProvider(harness.fixture, observer=harness.calls.append)
        graph = build_graph(store, provider, Gateway(store, provider, Safety()), saver)
        old = next(
            cp
            for cp in saver.list({"configurable": {"thread_id": harness.task_id}})
            if cp.checkpoint["channel_values"].get("route") == "execute_tool"
        )
        graph.invoke(None, {**old.config, "recursion_limit": 64}, durability="sync")
        assert store.report_snapshot()["tool_calls"] == data["tool_calls"]
        assert store.units()[0]["validation_ref"] == data["units"][0]["validation_ref"]
    assert len(harness.calls) == 2


def test_deepseek_tool_round_and_repair_archive_quote_wire_agree(tmp_path, monkeypatch):
    import httpx
    from test_deepseek_provider import API_KEY, response

    from review_agent.providers import DeepSeekProvider
    from review_agent.safety import Safety

    path = tmp_path / "context.diff"
    path.write_text(CONTEXT_DIFF)
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    task = app.create_task(
        path,
        None,
        tmp_path / "state",
        provider_name="deepseek",
        max_tokens=120000,
        max_cost_nusd=1_000_000_000,
    )
    bodies, quotes = [], []
    replies = [tool()["body"], '{"action":', ZERO["body"]]

    def handler(request):
        bodies.append(json.loads(request.content))
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(200, json=response(body=replies[len(bodies) - 1]))

    def provider(config, observer=None):
        port = DeepSeekProvider(config, API_KEY, transport=httpx.MockTransport(handler))
        quote = port.quote

        def capture(request, config):
            quotes.append(copy.deepcopy(request))
            return quote(request, config)

        port.quote = capture
        return port, Safety((API_KEY,))

    monkeypatch.setattr(app, "_provider", provider)
    data = app.execute(task, tmp_path / "state")
    assert data["task"]["status"] == "COMPLETED"
    assert len(bodies) == 3 and bodies == quotes
    assert [data["artifacts"][op["request_ref"]] for op in data["operations"]] == bodies
    second, repair = [json.loads(body["messages"][1]["content"]) for body in bodies[1:]]
    assert second["loop"] == repair["loop"]
    assert second["loop"]["input_tool_result_ref"] == data["tool_calls"][0]["result_ref"]
    assert repair["repair"]["source_result_ref"] == data["operations"][1]["result_ref"]
    trace = app.trace(data, attempt_id=data["attempts"][-1]["attempt_id"])
    assert API_KEY not in json.dumps(data) + json.dumps(trace) + render(data)


def test_terminal_tool_result_cannot_be_replaced(harness):
    from review_agent.tools.runner import outcome
    from review_agent.tools.service import ToolService

    harness.create(spec=spec(tool(), ZERO), schema=5)
    data = harness.run()
    with harness.store() as store:
        call = data["tool_calls"][0]
        with pytest.raises(AgentError, match="TOOL_RESULT_CONFLICT"):
            ToolService(store).finish(call, outcome("SUCCEEDED", records=[]))
        with pytest.raises(sqlite3.IntegrityError, match="TOOL_CALL_IMMUTABLE"):
            store.conn.execute("UPDATE tool_calls SET status='PENDING'")
        assert store.report_snapshot()["tool_calls"] == data["tool_calls"]


def test_report_does_not_count_unconsumed_tool_context_as_model_coverage(harness):
    from review_agent.report import context_coverage

    harness.create(text=CONTEXT_DIFF, spec=spec(tool(), ZERO), schema=5)
    harness.crash("after_tool_completed")
    before = harness.read()
    seen, total = context_coverage(before, before["units"][0])
    assert seen < total
    after = harness.run()
    assert context_coverage(after, after["units"][0]) == (total, total)
    harness.create(text=CONTEXT_DIFF, spec=spec(ZERO), schema=5)
    assert "流程已完成；上下文覆盖不完整" in render(harness.run())


def test_sanitized_tool_context_never_executes_target_or_exports_secret(harness, tmp_path):
    secret = "ghp_" + "SyntheticContextOnly" * 3
    marker = tmp_path / "target-not-executed"
    text = CONTEXT_DIFF.replace(
        "# callers guarantee a nonempty sequence", f'API_KEY = "{secret}"'
    ).replace(
        "# omitted context must be fetched explicitly", f"open({str(marker)!r}, 'w').write('bad')"
    )
    harness.create(text=text, spec=spec(tool(), ZERO), schema=5)
    data = harness.run()
    exported = json.dumps(data) + json.dumps(app.trace(data)) + render(data)
    assert secret not in exported and not marker.exists()
    assert "[REDACTED" in json.dumps(harness.calls[-1]["loop"]["tool_history"])


def test_new_declared_tool_with_scalar_output_runs_through_unchanged_graph(
    harness, tmp_path, monkeypatch
):
    from test_tools import custom

    from review_agent.tools import registry, service

    directory = tmp_path / "trusted-tools"
    directory.mkdir()
    custom(directory, "def run(view, arguments, emit):\n    emit(7)\n")
    manifest = directory / "extra.json"
    definition = json.loads(manifest.read_text())
    definition["output_schema"] = {"type": "integer", "minimum": 0, "maximum": 100}
    manifest.write_text(json.dumps(definition))

    class ExtendedRegistry(registry.ToolRegistry):
        def __init__(self):
            super().__init__(directory)

    # Select an isolated host installation; no graph/action/provider mapping changes.
    monkeypatch.setattr(registry, "ToolRegistry", ExtendedRegistry)
    monkeypatch.setattr(service, "ToolRegistry", ExtendedRegistry)
    call = reply({"action": "request_tool", "name": "extra", "arguments": {}})
    harness.create(spec=spec(call, ZERO), schema=5)
    data = harness.run()
    assert data["task"]["status"] == "COMPLETED"
    assert harness.calls[1]["loop"]["tool_history"][0]["result"]["records"] == [7]
    app.trace(data)
    render(data)


def test_noncanonical_tool_records_do_not_claim_unseen_evidence(harness):
    from review_agent.tool_loop import supplied_evidence

    harness.create(text=CONTEXT_DIFF, spec=spec(ZERO), schema=5)
    data = harness.run()
    request = copy.deepcopy(harness.calls[0])
    request["loop"]["tool_history"] = [
        {
            "result": {
                "records": [
                    7,
                    "opaque",
                    {"hunk_id": "h0001", "new_lineno": [1]},
                    {
                        "hunk_id": "h0001",
                        "new_lineno": 1,
                        "text": "invented",
                        "kind": " ",
                        "redacted": False,
                    },
                ]
            }
        }
    ]
    assert "h0001:new:1" not in supplied_evidence(data["snapshot"], request)
