"""Read-only tool/turn provenance checks, using saved facts only."""

import json

from review_agent.contracts import AgentError, digest, stable_id


def require(condition):
    if not condition:
        raise AgentError("AUDIT_INTEGRITY_ERROR")


def artifact(snapshot, ref, kind):
    require(snapshot["artifact_kinds"].get(ref) == kind)
    return snapshot["artifacts"][ref]


def tool_call(snapshot, call):
    config = json.loads(snapshot["task"]["config"])
    request = artifact(snapshot, call["request_ref"], "tool_request")
    require(digest(request) == call["request_digest"])
    source = next(
        op for op in snapshot["operations"] if op["operation_id"] == call["source_operation_id"]
    )
    source_request = artifact(snapshot, source["request_ref"], "request")
    require(digest(source_request) == source["request_digest"])
    source_result = artifact(snapshot, call["source_result_ref"], "result")
    require(source["result_ref"] == call["source_result_ref"])
    require(source_result["result_status"] == "VALID")
    decision = source_result["decision"]
    require(decision["action"] == "request_tool")
    expected = {
        "tool_call_id": stable_id(
            "tool_call", snapshot["task"]["task_id"], call["unit_id"], source["result_ref"], 0
        ),
        "unit_id": call["unit_id"],
        "snapshot_id": snapshot["task"]["snapshot_id"],
        "source_operation_id": source["operation_id"],
        "source_result_ref": source["result_ref"],
        "source_request_ref": source["request_ref"],
        "source_request_digest": source["request_digest"],
        "name": decision["name"],
        "arguments": decision["arguments"],
        "registry_digest": digest(config["tool_registry"]),
        "tool_identity": config["tool_registry"]["entries"].get(decision["name"]),
    }
    require(request == expected and source["unit_id"] == call["unit_id"])
    require(request["tool_call_id"] == call["tool_call_id"])
    result = None
    if call["result_ref"]:
        result = artifact(snapshot, call["result_ref"], "tool_result")
        require(digest(result) == call["result_digest"])
        require(result["tool_call_id"] == call["tool_call_id"])
        require(result["tool_request_ref"] == call["request_ref"])
        require(result["status"] == call["status"])
    else:
        require(call["status"] in ("PENDING", "RUNNING"))
    return {"call": call, "request": request, "result": result}


def inputs(snapshot, context, data):
    history, details, current = [], [], context
    while current["input_tool_call_id"]:
        require(len(history) < 4)
        call = next(
            c for c in snapshot["tool_calls"] if c["tool_call_id"] == current["input_tool_call_id"]
        )
        detail = tool_call(snapshot, call)
        require(call["unit_id"] == context["unit_id"])
        require(
            call["result_ref"] == current["input_tool_result_ref"] and detail["result"] is not None
        )
        request = detail["request"]
        history.append(
            {
                "tool_call_id": call["tool_call_id"],
                "tool_request_ref": call["request_ref"],
                "source_request_ref": request["source_request_ref"],
                "source_result_ref": call["source_result_ref"],
                "name": request["name"],
                "arguments": request["arguments"],
                "result_ref": call["result_ref"],
                "result": detail["result"],
            }
        )
        details.append(detail)
        previous = next(
            c
            for c in snapshot["operation_contexts"]
            if c["operation_id"] == call["source_operation_id"]
        )
        require(previous["turn_no"] == current["turn_no"] - 1)
        current = previous
    require(current["turn_no"] == 0)
    loop = data["loop"]
    require(loop["tool_history"] == list(reversed(history)))
    require(loop["turn_no"] == context["turn_no"])
    require(loop["input_tool_call_id"] == context["input_tool_call_id"])
    require(loop["input_tool_result_ref"] == context["input_tool_result_ref"])
    return list(reversed(details))
