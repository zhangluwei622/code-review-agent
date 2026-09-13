"""Presentation-only relationships. Missing audit facts stay missing."""

import json
from datetime import datetime

from review_agent.contracts import AgentError


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return value if isinstance(value, list) else []


def parsed_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return obj(json.loads(value))
        except (ValueError, RecursionError):
            pass
    return {}


def unique(index, key, value):
    if not isinstance(key, str) or not key:
        raise AgentError("VIEW_INVALID_TRACE")
    if key in index and index[key] != value:
        raise AgentError("VIEW_CONFLICTING_ID")
    index[key] = value


def request_payload(call):
    request = obj(obj(call.get("request")).get("data"))
    if "messages" in request:
        messages = rows(request.get("messages"))
        return parsed_object(obj(messages[1]).get("content")) if len(messages) > 1 else {}
    return request


def evidence_lines(call):
    """Only the finding's actual request and tool inputs, never the whole task."""
    index = {}

    def add(hunk, line, origin, node_id):
        if not isinstance(hunk, str):
            return
        for side in ("old", "new"):
            number = line.get(f"{side}_lineno")
            if type(number) is int and number > 0:
                ref = f"{hunk}:{side}:{number}"
                item = {"origin": origin, "node_id": node_id, "line": line}
                if item not in index.setdefault(ref, []):
                    index[ref].append(item)

    for hunk in rows(request_payload(call).get("hunks")):
        hunk = obj(hunk)
        for line in rows(hunk.get("lines")):
            add(
                hunk.get("hunk_id"),
                obj(line),
                "本轮冻结请求",
                obj(call.get("attempt")).get("attempt_id"),
            )
    for tool in rows(call.get("tool_inputs")):
        tool = obj(tool)
        tool_id = obj(tool.get("call")).get("tool_call_id")
        for line in rows(obj(tool.get("result")).get("records")):
            line = obj(line)
            add(line.get("hunk_id"), line, "本轮已输入的工具结果", tool_id)
    return index


def _timing(value):
    value = obj(value)
    result = {
        "dispatched_at": value.get("dispatched_at"),
        "completed_at": value.get("completed_at"),
        "elapsed_ms": None,
    }
    try:
        start = datetime.fromisoformat(result["dispatched_at"])
        end = datetime.fromisoformat(result["completed_at"])
        if start.tzinfo and end.tzinfo and end >= start:
            result["elapsed_ms"] = round((end - start).total_seconds() * 1000, 3)
    except (ValueError, TypeError):
        pass
    return result


def project(trace):
    for key in ("task_id", "trace_id", "execution_mode", "config_digest"):
        if not isinstance(trace.get(key), str) or not trace[key]:
            raise AgentError("VIEW_INVALID_TRACE")
    calls, tools, decisions = {}, {}, {}

    def collect(call):
        call = obj(call)
        if not call.get("attempt"):
            return
        record = {k: v for k, v in call.items() if k != "source_call"}
        unique(calls, obj(record["attempt"]).get("attempt_id"), record)
        for item in rows(call.get("tool_inputs")):
            unique(tools, obj(obj(item).get("call")).get("tool_call_id"), item)
        for item in rows(call.get("retry_decisions")):
            unique(decisions, obj(item).get("decision_id"), item)
        if call.get("source_call"):
            collect(call["source_call"])

    if "attempt" in trace:
        keys = (
            "attempt",
            "operation",
            "quote",
            "operation_context",
            "request",
            "result",
            "tool_inputs",
            "retry_decisions",
            "source_call",
        )
        collect({k: trace[k] for k in keys if k in trace})
    for call in rows(trace.get("calls")):
        collect(call)
    for item in rows(trace.get("tool_calls")):
        unique(tools, obj(obj(item).get("call")).get("tool_call_id"), item)
    timings = {
        obj(t).get("attempt_id"): t for t in rows(obj(trace.get("pricing")).get("attempt_timings"))
    }
    nodes, results = {}, {}
    for key, call in calls.items():
        attempt, operation = obj(call.get("attempt")), obj(call.get("operation"))
        context = obj(call.get("operation_context"))
        result_ref = attempt.get("result_ref")
        if result_ref:
            unique(results, result_ref, key)
        nodes[key] = {
            "id": key,
            "kind": context.get("kind", "REVIEW"),
            "unit": operation.get("unit_id"),
            "turn": context.get("turn_no"),
            "attempt_no": attempt.get("attempt_no"),
            "status": attempt.get("call_status"),
            "fee_status": attempt.get("fee_status"),
            "result_ref": result_ref,
            "operation_id": operation.get("operation_id"),
            "request_ref": operation.get("request_ref"),
            "timing": _timing(timings.get(key)),
            "raw": call,
        }
    for key, detail in tools.items():
        call = obj(detail.get("call"))
        if key in nodes:
            raise AgentError("VIEW_CONFLICTING_ID")
        nodes[key] = {
            "id": key,
            "kind": "TOOL",
            "unit": call.get("unit_id"),
            "turn": None,
            "attempt_no": None,
            "status": call.get("status"),
            "name": obj(detail.get("request")).get("name"),
            "result_ref": call.get("result_ref"),
            "request_ref": call.get("request_ref"),
            "timing": _timing(None),
            "raw": detail,
        }
    for item in rows(trace.get("unattempted_operations")):
        item = obj(item)
        operation = obj(item.get("operation"))
        key = operation.get("operation_id")
        unique(
            nodes,
            key,
            {
                "id": key,
                "kind": "PREPARED",
                "unit": operation.get("unit_id"),
                "status": "NO_ATTEMPT",
                "request_ref": operation.get("request_ref"),
                "result_ref": None,
                "timing": _timing(None),
                "raw": item,
            },
        )
    if len(nodes) > 10000:
        raise AgentError("VIEW_TOO_COMPLEX")
    relations = []

    def link(kind, source, target, ref, detail=None):
        item = {
            "kind": kind,
            "from": source if source in nodes else None,
            "to": target if target in nodes else None,
            "ref": ref,
            "detail": detail,
        }
        if item not in relations:
            relations.append(item)

    for key, call in calls.items():
        context = obj(call.get("operation_context"))
        if context.get("kind") == "REPAIR":
            ref = context.get("source_result_ref")
            source = results.get(ref)
            if source and nodes[source]["operation_id"] != context.get("source_operation_id"):
                raise AgentError("VIEW_CONFLICTING_REFERENCE")
            link("REPAIR", source, key, ref, context)
        tool_id = context.get("input_tool_call_id")
        if tool_id:
            if tool_id in tools and nodes[tool_id]["result_ref"] != context.get(
                "input_tool_result_ref"
            ):
                raise AgentError("VIEW_CONFLICTING_REFERENCE")
            link("TOOL_CONTINUATION", tool_id, key, context.get("input_tool_result_ref"))
    for key, detail in tools.items():
        call = obj(detail.get("call"))
        ref = call.get("source_result_ref")
        source = results.get(ref)
        if source and nodes[source]["operation_id"] != call.get("source_operation_id"):
            raise AgentError("VIEW_CONFLICTING_REFERENCE")
        link("TOOL_REQUEST", source, key, ref)
    for item in decisions.values():
        link(
            "RETRY",
            item.get("source_attempt_id"),
            item.get("bound_attempt_id"),
            item.get("decision_id"),
            item,
        )

    # Dependency order is not wall-clock order. Preserve export order for unrelated nodes.
    incoming = {key: set() for key in nodes}
    for relation in relations:
        if relation["from"] and relation["to"]:
            incoming[relation["to"]].add(relation["from"])
    ordered = []
    while incoming:
        ready = next((key for key, parents in incoming.items() if not parents), None)
        if ready is None:
            raise AgentError("VIEW_RELATION_CYCLE")
        ordered.append(nodes[ready])
        del incoming[ready]
        for parents in incoming.values():
            parents.discard(ready)

    findings = []
    finding_rows = rows(trace.get("findings"))
    if isinstance(trace.get("finding"), dict):
        finding_rows = [trace["finding"], *finding_rows]
    seen = {}
    for finding in finding_rows:
        finding = obj(finding)
        key = finding.get("finding_id")
        if key in seen and seen[key] == finding:
            continue
        unique(seen, key, finding)
        data = parsed_object(finding.get("data"))
        owner = results.get(finding.get("result_ref"))
        available = evidence_lines(calls[owner]) if owner else {}
        evidence = []
        anchor = None
        if all(k in data for k in ("hunk_id", "side", "line")):
            anchor = f"{data['hunk_id']}:{data['side']}:{data['line']}"
            evidence.append({"role": "anchor", "ref": anchor, "matches": available.get(anchor, [])})
        for item in rows(trace.get("evidence")):
            if obj(item).get("finding_id") == key:
                ref = item.get("evidence_ref")
                evidence.append(
                    {"role": item.get("role"), "ref": ref, "matches": available.get(ref, [])}
                )
        validation = next(
            (
                v.get("data")
                for v in rows(trace.get("validations"))
                if obj(v).get("artifact_id") == finding.get("validation_ref")
            ),
            None,
        )
        if obj(trace.get("finding")).get("finding_id") == key:
            validation = trace.get("validation")
        findings.append(
            {
                "id": key,
                "node_id": owner,
                "data": data,
                "raw": finding,
                "evidence": evidence,
                "validation": validation,
            }
        )

    return {
        "version": 1,
        "nodes": ordered,
        "relations": relations,
        "findings": findings,
        "scope": trace.get("scope"),
        "sections_present": list(trace),
        "marked_sends": sum(
            n["status"] in ("DISPATCHED", "COMPLETED", "UNKNOWN")
            for n in ordered
            if n["kind"] not in ("TOOL", "PREPARED")
        ),
        "tool_count": len(tools),
        "attempt_count": len(calls),
        "recovery_events_available": False,
        "task_status_available": False,
    }
