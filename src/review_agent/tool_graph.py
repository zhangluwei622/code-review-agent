"""Single-agent v5 graph; v1-v4 retain their accepted graph protocol."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from review_agent.contracts import AgentError
from review_agent.review import validate_result
from review_agent.tool_loop import ToolLoop, make_loop_request, payload, visible_evidence
from review_agent.tools.service import ToolService

TERMINAL = {
    "DONE",
    "ABSTAINED",
    "PARTIAL_LIMIT",
    "PARTIAL_INVALID_RESULT",
    "PARTIAL_TRUNCATED",
    "BLOCKED_SECURITY",
}


def build_tool_graph(store, provider, gateway, saver, safe_node):
    from review_agent.contracts import GraphState

    loop, tools = ToolLoop(store), ToolService(store)

    def unit(state):
        return store.units()[state["unit_index"]]

    def advance(state):
        return {
            "unit_index": state["unit_index"] + 1,
            "last_result_ref": None,
            "route": "select_unit",
        }

    @safe_node
    def select_unit(state):
        if state["unit_index"] >= len(store.units()):
            return {"route": "finalize"}
        current = unit(state)
        if current["status"] in TERMINAL:
            if current["status"] == "BLOCKED_SECURITY":
                return {"route": "finalize"}
            return advance(state)
        return {"unit_id": current["unit_id"], "route": "review_agent"}

    @safe_node
    def review_agent(state):
        current = unit(state)
        if current["status"] in TERMINAL:
            return (
                {"route": "finalize"} if current["status"] == "BLOCKED_SECURITY" else advance(state)
            )
        context = loop.cursor(current["unit_id"])
        op = store.find_operation(context["operation_id"])
        if store.task()["send_block_reason"]:
            if op and op["result_ref"]:
                return {"last_result_ref": op["result_ref"], "route": "validate_unit"}
            return {"route": "finalize"}
        try:
            ref = gateway.run(
                current["unit_id"],
                lambda: make_loop_request(store, context, provider.output_limit(store.config())),
                operation_id=context["operation_id"],
                source_operation_id=context["source_operation_id"],
            )
            return {
                "last_result_ref": ref,
                "operation_id": context["operation_id"],
                "route": "validate_unit",
            }
        except AgentError as error:
            if error.code in ("BUDGET_EXHAUSTED", "PROVIDER_UNKNOWN"):
                return {
                    "pause_reason": error.code,
                    "operation_id": context["operation_id"],
                    "route": "pause",
                }
            if error.code == "BUDGET_BOUND_VIOLATION":
                return {"route": "finalize"}
            if error.code == "ROUND_LIMIT":
                store.finish_limited_unit(current["unit_id"])
                return advance(state)
            if error.code in (
                "UNSAFE_REQUEST",
                "SAFETY_SCAN_FAILED",
                "UNSAFE_CONTROL_CHARACTER",
                "UNCLOSED_PRIVATE_KEY",
            ):
                store.mark_unit(current["unit_id"], "BLOCKED_SECURITY", error.code)
                return {"route": "finalize"}
            raise

    @safe_node
    def validate_unit(state):
        current = unit(state)
        if current["status"] in TERMINAL:
            return (
                {"route": "finalize"} if current["status"] == "BLOCKED_SECURITY" else advance(state)
            )
        context = loop.cursor(current["unit_id"])
        op = store.find_operation(context["operation_id"])
        if not op or not op["result_ref"]:
            return {"route": "review_agent"}
        ref = op["result_ref"]
        result = store.stored_result(ref)
        stopped = bool(store.task()["send_block_reason"])
        if result.result_status == "VALID" and result.decision["action"] == "request_tool":
            request = payload(
                store.saved_request(op["operation_id"]), store.config().execution_mode
            )
            if stopped:
                store.mark_unit(
                    current["unit_id"], "PARTIAL_LIMIT", store.task()["send_block_reason"]
                )
                return {"route": "finalize"}
            if (
                request["loop"]["final_only"]
                or current["sends"] >= store.config().max_sends_per_unit
            ):
                store.save_validation(
                    current["unit_id"],
                    ref,
                    {
                        "status": "PARTIAL_LIMIT",
                        "reason": "SUMMARY_REQUIRED",
                        "findings": [],
                        "rejected": [],
                    },
                )
                return advance(state)
            return {"last_result_ref": ref, "route": "execute_tool"}
        validation = validate_result(
            result,
            current,
            store.snapshot(),
            allowed_evidence=visible_evidence(store, op["operation_id"]),
        )
        repair = not stopped and store.repair_eligible(op["operation_id"])
        store.save_validation(current["unit_id"], ref, validation, publish=not repair)
        gateway.fault("after_validation")
        if stopped or validation["status"] == "BLOCKED_SECURITY":
            return {"route": "finalize"}
        return {"route": "review_agent"} if repair else advance(state)

    @safe_node
    def execute_tool(state):
        current = unit(state)
        if current["status"] in TERMINAL:
            return {"route": "select_unit"}
        if store.task()["send_block_reason"]:
            return {"route": "finalize"}
        context = loop.cursor(current["unit_id"])
        op = store.find_operation(context["operation_id"])
        if not op or op["result_ref"] != state["last_result_ref"]:
            return {"route": "review_agent"}
        result = tools.run(current["unit_id"], op["result_ref"])
        if result["status"] == "BLOCKED_SECURITY":
            store.mark_unit(current["unit_id"], "BLOCKED_SECURITY", result["error_code"])
            return {"route": "finalize"}
        return {"route": "review_agent"}

    @safe_node
    def pause(state):
        current = unit(state)
        if current["status"] in TERMINAL:
            return {"route": "select_unit"}
        if store.task()["send_block_reason"]:
            return {"route": "finalize"}
        op_id = loop.cursor(current["unit_id"])["operation_id"]
        if store.rows("SELECT 1 FROM attempts WHERE operation_id=?", (op_id,)):
            if store.attempt(op_id)["call_status"] in ("RESERVED", "COMPLETED"):
                return {"route": "review_agent"}
        store.pause(current["unit_id"], state["pause_reason"])
        gateway.fault("after_pause")
        interrupt(
            {
                "task_id": state["task_id"],
                "unit_id": current["unit_id"],
                "reason": state["pause_reason"],
            }
        )
        return {"route": "review_agent"}

    @safe_node
    def finalize(state):
        store.finalize()
        return {"route": "end"}

    graph = StateGraph(GraphState)
    nodes = {
        "select_unit": select_unit,
        "review_agent": review_agent,
        "validate_unit": validate_unit,
        "execute_tool": execute_tool,
        "pause": pause,
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "select_unit")
    for name in nodes.keys() - {"finalize"}:
        graph.add_conditional_edges(name, lambda state: state["route"], {key: key for key in nodes})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=saver)
