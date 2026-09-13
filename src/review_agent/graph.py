from functools import wraps

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from review_agent.contracts import AgentError, GraphState, StoredResult, stable_id
from review_agent.gateway import Gateway
from review_agent.providers import ProviderPort
from review_agent.review import make_repair_request, make_request, validate_result
from review_agent.storage import Storage


def safe_node(fn):
    @wraps(fn)
    def run(state):
        try:
            return fn(state)
        except (AgentError, GraphInterrupt):
            raise
        except Exception:
            # LangGraph can persist exceptions as pending writes.
            raise AgentError("NODE_FAILED") from None

    return run


def build_graph(store: Storage, provider: ProviderPort, gateway: Gateway, saver):
    if store.config().schema_version >= 5:
        from review_agent.tool_graph import build_tool_graph

        return build_tool_graph(store, provider, gateway, saver, safe_node)
    @safe_node
    def select_unit(state: GraphState):
        index = state.get("unit_index", 0)
        units, task = store.units(), store.task()
        if index >= len(units):
            return {"route": "finalize"}
        unit = units[index]
        if task["send_block_reason"]:
            op_id = store.unit_operation_id(unit["unit_id"])
            op = store.find_operation(op_id)
            if (
                op
                and op["result_ref"]
                and unit["validation_ref"] != stable_id("validation", op["result_ref"])
            ):
                return {
                    "unit_id": unit["unit_id"],
                    "last_result_ref": op["result_ref"],
                    "route": "validate_unit",
                }
            return {"route": "finalize"}
        return {"unit_id": unit["unit_id"], "last_result_ref": None, "route": "review_agent"}

    @safe_node
    def review_agent(state: GraphState):
        unit = store.units()[state["unit_index"]]
        if unit["status"] == "PARTIAL_LIMIT":
            return {"unit_index": state["unit_index"] + 1, "route": "select_unit"}

        operation_id = store.unit_operation_id(unit["unit_id"])
        review_id = store.review_operation_id(unit["unit_id"])
        source_id = review_id if operation_id != review_id else None

        def request():
            limit = provider.output_limit(store.config())
            if source_id:
                source = store.find_operation(source_id)
                result = StoredResult.model_validate(store.artifact(source["result_ref"]))
                return make_repair_request(
                    unit, store.snapshot(), limit, source_id, source["result_ref"], result
                )
            return make_request(unit, store.snapshot(), limit)

        try:
            ref = gateway.run(
                unit["unit_id"], request, operation_id=operation_id, source_operation_id=source_id
            )
            return {"last_result_ref": ref, "operation_id": operation_id, "route": "validate_unit"}
        except AgentError as error:
            if error.code in ("BUDGET_EXHAUSTED", "PROVIDER_UNKNOWN"):
                return {"pause_reason": error.code, "operation_id": operation_id, "route": "pause"}
            if error.code == "BUDGET_BOUND_VIOLATION":
                return {"route": "finalize"}
            if error.code in (
                "UNSAFE_REQUEST",
                "SAFETY_SCAN_FAILED",
                "UNSAFE_CONTROL_CHARACTER",
                "UNCLOSED_PRIVATE_KEY",
            ):
                store.mark_unit(unit["unit_id"], "BLOCKED_SECURITY", error.code)
                return {"route": "finalize"}
            if error.code == "ROUND_LIMIT":
                store.mark_unit(unit["unit_id"], "PARTIAL_LIMIT", error.code)
                return {"unit_index": state["unit_index"] + 1, "route": "select_unit"}
            raise

    @safe_node
    def validate_unit(state: GraphState):
        unit = store.units()[state["unit_index"]]
        effective_id = store.unit_operation_id(unit["unit_id"])
        effective = store.find_operation(effective_id)
        if (
            effective
            and effective["result_ref"]
            and effective["result_ref"] != state["last_result_ref"]
        ):
            store.record_reuse(effective_id)
            return {"last_result_ref": effective["result_ref"], "route": "validate_unit"}
        ref = state["last_result_ref"]
        result = StoredResult.model_validate(store.artifact(ref))
        operation = store.one("SELECT * FROM operations WHERE result_ref=?", (ref,))
        validation = validate_result(result, unit, store.snapshot())
        stop = validation["status"] == "BLOCKED_SECURITY" or store.task()["send_block_reason"]
        repair = store.repair_eligible(operation["operation_id"]) and not stop
        store.save_validation(unit["unit_id"], ref, validation, publish=not repair)
        gateway.fault("after_validation")
        if repair:
            return {
                "operation_id": store.repair_operation_id(operation["operation_id"]),
                "route": "review_agent",
            }
        return {
            "unit_index": state["unit_index"] + 1,
            "route": "finalize" if stop else "select_unit",
        }

    @safe_node
    def pause(state: GraphState):
        op_id = store.unit_operation_id(state["unit_id"])
        if store.task()["send_block_reason"]:
            return {"route": "finalize"}
        if store.units()[state["unit_index"]]["status"] == "PARTIAL_LIMIT":
            return {"unit_index": state["unit_index"] + 1, "route": "select_unit"}
        if store.rows("SELECT 1 FROM attempts WHERE operation_id=?", (op_id,)):
            current = store.attempt(op_id)
            if current["call_status"] in ("RESERVED", "COMPLETED"):
                return {"route": "review_agent", "pause_reason": None}
        store.pause(state["unit_id"], state["pause_reason"])
        gateway.fault("after_pause")
        interrupt(
            {
                "task_id": state["task_id"],
                "unit_id": state["unit_id"],
                "reason": state["pause_reason"],
            }
        )
        return {"route": "review_agent"}

    @safe_node
    def finalize(state: GraphState):
        store.finalize()
        return {"route": "end"}

    graph = StateGraph(GraphState)
    nodes = {
        "select_unit": select_unit,
        "review_agent": review_agent,
        "validate_unit": validate_unit,
        "pause": pause,
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "select_unit")
    for name in ("select_unit", "review_agent", "validate_unit", "pause"):
        graph.add_conditional_edges(name, lambda state: state["route"], {key: key for key in nodes})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=saver)
