"""v5 immutable model-turn links. All cursors are derived from business facts."""

import json

from review_agent.config import package_text, review_prompt_path
from review_agent.contracts import AgentError, stable_id
from review_agent.review import decision_schema, evidence_index, make_request
from review_agent.tools.service import ToolService


def payload(request, mode):
    return request if mode == "fixture" else json.loads(request["messages"][1]["content"])


class ToolLoop:
    def __init__(self, store):
        self.store = store
        self.tools = ToolService(store)

    def context(self, unit_id, turn_no=0, previous=None):
        task, config = self.store.task(), self.store.config()
        return {
            "operation_id": stable_id("operation", task["task_id"], unit_id, turn_no, "REVIEW", 0),
            "unit_id": unit_id,
            "kind": "REVIEW",
            "ordinal": 0,
            "turn_no": turn_no,
            "source_operation_id": None,
            "source_result_ref": None,
            "prompt_digest": config.prompt_digest,
            "input_tool_call_id": previous["tool_call_id"] if previous else None,
            "input_tool_result_ref": previous["result_ref"] if previous else None,
        }

    def repair_eligible(self, operation_id):
        store = self.store
        op = store.find_operation(operation_id)
        if not op or not op["result_ref"] or not store.config().max_repairs_per_unit:
            return False
        context = store.operation_context(operation_id)
        if context["kind"] != "REVIEW":
            return False
        existing = store.rows(
            "SELECT * FROM model_turns WHERE unit_id=? AND kind='REPAIR'", (op["unit_id"],)
        )
        if existing and existing[0]["source_operation_id"] != operation_id:
            return False
        result = store.stored_result(op["result_ref"])
        return (
            result.result_status == "FORMAT_INVALID"
            and result.completion_state == "COMPLETE"
            and bool(result.safe_body)
        )

    def cursor(self, unit_id):
        store = self.store
        context = self.context(unit_id)
        for _ in range(16):  # 6 sends, 4 tools, one repair: corrupt cycles never run forever.
            op = store.find_operation(context["operation_id"])
            if not op or not op["result_ref"]:
                return context
            saved = store.operation_context(op["operation_id"])
            if saved != context:
                raise AgentError("TURN_LEDGER_INTEGRITY")
            repair_id = store.repair_operation_id(op["operation_id"])
            repaired = store.find_operation(repair_id)
            validation_ref = stable_id("validation", op["result_ref"])
            has_validation = store.rows(
                "SELECT 1 FROM artifacts WHERE artifact_id=? AND kind='validation'",
                (validation_ref,),
            )
            if repaired or (
                self.repair_eligible(op["operation_id"])
                and has_validation
                and not store.task()["send_block_reason"]
            ):
                if not self.repair_eligible(op["operation_id"]):
                    raise AgentError("TURN_LEDGER_INTEGRITY")
                context = {
                    **context,
                    "operation_id": repair_id,
                    "kind": "REPAIR",
                    "ordinal": 1,
                    "source_operation_id": op["operation_id"],
                    "source_result_ref": op["result_ref"],
                    "prompt_digest": store.config().repair_prompt_digest,
                }
                continue
            result = store.stored_result(op["result_ref"])
            call = self.tools.find(op["result_ref"])
            if call:
                self.tools.request(call)
                if call["result_ref"]:
                    self.tools.result(call)
                if call["result_ref"] and call["status"] != "BLOCKED_SECURITY":
                    context = self.context(unit_id, context["turn_no"] + 1, call)
                    continue
            if call and (
                result.result_status != "VALID" or result.decision["action"] != "request_tool"
            ):
                raise AgentError("TURN_LEDGER_INTEGRITY")
            return context
        raise AgentError("TURN_LEDGER_INTEGRITY")

    def insert_context(self, operation_id, unit_id, source_operation_id):
        context = self.cursor(unit_id)
        if (
            context["operation_id"] != operation_id
            or context["source_operation_id"] != source_operation_id
        ):
            raise AgentError("TURN_LEDGER_INTEGRITY")
        request = self.store.saved_request(operation_id)
        data = payload(request, self.store.config().execution_mode)
        self.check_input(context, data)
        keys = tuple(context)
        self.store.conn.execute(
            f"INSERT INTO model_turns ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
            tuple(context.values()),
        )

    def history(self, context):
        history, current = [], context
        while current["input_tool_call_id"]:
            if len(history) >= 4:
                raise AgentError("TURN_LEDGER_INTEGRITY")
            call = self.store.one(
                "SELECT * FROM tool_calls WHERE tool_call_id=?", (current["input_tool_call_id"],)
            )
            request, result = self.tools.request(call), self.tools.result(call)
            if (
                call["unit_id"] != context["unit_id"]
                or call["result_ref"] != current["input_tool_result_ref"]
            ):
                raise AgentError("TURN_LEDGER_INTEGRITY")
            history.append(
                {
                    "tool_call_id": call["tool_call_id"],
                    "tool_request_ref": call["request_ref"],
                    "source_request_ref": request["source_request_ref"],
                    "source_result_ref": call["source_result_ref"],
                    "name": request["name"],
                    "arguments": request["arguments"],
                    "result_ref": call["result_ref"],
                    "result": result,
                }
            )
            previous = self.store.operation_context(call["source_operation_id"])
            if previous["turn_no"] != current["turn_no"] - 1:
                raise AgentError("TURN_LEDGER_INTEGRITY")
            current = previous
        if current["turn_no"] != 0:
            raise AgentError("TURN_LEDGER_INTEGRITY")
        return list(reversed(history))

    def check_input(self, context, data):
        loop = data["loop"]
        if (
            loop["turn_no"] != context["turn_no"]
            or loop["input_tool_call_id"] != context["input_tool_call_id"]
            or loop["input_tool_result_ref"] != context["input_tool_result_ref"]
            or loop["tool_history"] != self.history(context)
        ):
            raise AgentError("TURN_INPUT_MISMATCH")
        if context["kind"] == "REPAIR":
            original = payload(
                self.store.saved_request(context["source_operation_id"]),
                self.store.config().execution_mode,
            )
            result = self.store.stored_result(context["source_result_ref"])
            if (
                loop != original["loop"]
                or data["hunks"] != original["hunks"]
                or data["repair"]
                != {
                    "source_operation_id": context["source_operation_id"],
                    "source_result_ref": context["source_result_ref"],
                    "previous_response": result.safe_body,
                    "error_codes": ["INVALID_REVIEW_FORMAT"],
                }
            ):
                raise AgentError("REPAIR_SOURCE_MISMATCH")

    def check(self):
        store = self.store
        for call in self.tools.calls():
            self.tools.request(call)
            if call["result_ref"]:
                self.tools.result(call)
        for unit in store.units():
            self.cursor(unit["unit_id"])
            calls = self.tools.calls(unit["unit_id"])
            if [c["slot_no"] for c in calls] != list(range(1, len(calls) + 1)):
                raise AgentError("TOOL_LEDGER_INTEGRITY")
            if len(calls) > store.config().max_tools_per_unit:
                raise AgentError("TOOL_LEDGER_INTEGRITY")
        for op in store.rows("SELECT * FROM operations"):
            context = store.operation_context(op["operation_id"])
            previous = (
                store.one(
                    "SELECT * FROM tool_calls WHERE tool_call_id=?",
                    (context["input_tool_call_id"],),
                )
                if context["input_tool_call_id"]
                else None
            )
            expected = self.context(op["unit_id"], context["turn_no"], previous)
            if context["kind"] == "REPAIR":
                source = store.find_operation(context["source_operation_id"])
                if not self.repair_eligible(source["operation_id"]):
                    raise AgentError("TURN_LEDGER_INTEGRITY")
                if store.operation_context(source["operation_id"]) != expected:
                    raise AgentError("TURN_LEDGER_INTEGRITY")
                expected.update(
                    operation_id=store.repair_operation_id(source["operation_id"]),
                    kind="REPAIR",
                    ordinal=1,
                    source_operation_id=source["operation_id"],
                    source_result_ref=source["result_ref"],
                    prompt_digest=store.config().repair_prompt_digest,
                )
            if context != expected:
                raise AgentError("TURN_LEDGER_INTEGRITY")
            data = payload(store.saved_request(op["operation_id"]), store.config().execution_mode)
            self.check_input(context, data)


def preview_hunks(unit, snapshot, radius):
    result = []
    for hunk in snapshot["hunks"]:
        if hunk["hunk_id"] not in unit["hunk_ids"]:
            continue
        indices = {
            j
            for i, line in enumerate(hunk["lines"])
            if line["kind"] in ("+", "-")
            for j in range(max(0, i - radius), min(len(hunk["lines"]), i + radius + 1))
        }
        result.append(
            {
                **hunk,
                "lines": [line for i, line in enumerate(hunk["lines"]) if i in indices],
                "omitted_context_lines": len(hunk["lines"]) - len(indices),
            }
        )
    return result


def make_loop_request(store, context, output_limit):
    config = store.config()
    if context["kind"] == "REPAIR":
        original = payload(
            store.saved_request(context["source_operation_id"]), config.execution_mode
        )
        request = {
            **original,
            "system": package_text("prompts/repair-tools.md"),
            "max_output_tokens": output_limit,
        }
        if config.execution_mode != "fixture":
            request["tools"] = request.pop("available_tools")
        result = store.stored_result(context["source_result_ref"])
        request["repair"] = {
            "source_operation_id": context["source_operation_id"],
            "source_result_ref": context["source_result_ref"],
            "previous_response": result.safe_body,
            "error_codes": ["INVALID_REVIEW_FORMAT"],
        }
        return request
    unit = next(u for u in store.units() if u["unit_id"] == context["unit_id"])
    final_only = (
        len(ToolService(store).calls(unit["unit_id"])) >= config.max_tools_per_unit
        or unit["sends"] >= config.max_sends_per_unit - 1
    )
    request = make_request(unit, store.snapshot(), output_limit)
    request["system"] = package_text(review_prompt_path(config.schema_version))
    request["hunks"] = preview_hunks(unit, store.snapshot(), config.initial_context_lines)
    request["output_schema"] = decision_schema(final_only)
    request["tools"] = (
        [] if final_only else [entry["spec"] for entry in config.tool_registry["entries"].values()]
    )
    request["loop"] = {
        "turn_no": context["turn_no"],
        "final_only": final_only,
        "input_tool_call_id": context["input_tool_call_id"],
        "input_tool_result_ref": context["input_tool_result_ref"],
        "tool_history": ToolLoop(store).history(context),
        "context_scope": "FROZEN_DIFF_ONLY",
        "outside_diff_context": "UNAVAILABLE",
    }
    return request


def visible_evidence(store, operation_id):
    data = payload(store.saved_request(operation_id), store.config().execution_mode)
    return supplied_evidence(store.snapshot(), data)


def supplied_evidence(snapshot, data):
    """Generic tool data is opaque; only exact snapshot lines establish code visibility."""
    index, visible = evidence_index(snapshot), set()
    records = [(h["hunk_id"], line) for h in data["hunks"] for line in h["lines"]]
    for item in data["loop"]["tool_history"]:
        for record in item["result"]["records"]:
            if isinstance(record, dict) and isinstance(record.get("hunk_id"), str):
                records.append((record["hunk_id"], record))
    for hunk_id, line in records:
        for side in ("old", "new"):
            number = line.get(f"{side}_lineno")
            if type(number) is not int or number <= 0:
                continue
            ref = f"{hunk_id}:{side}:{number}"
            canonical = index.get(ref)
            if canonical and all(
                line.get(key) == canonical[key] for key in ("kind", "text", "redacted")
            ):
                visible.add(ref)
    return visible
