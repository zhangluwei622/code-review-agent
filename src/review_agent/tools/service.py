"""Transaction boundaries for one logical local tool call, independent of provider fees."""

from review_agent.contracts import AgentError, digest, stable_id
from review_agent.tools.registry import ToolRegistry
from review_agent.tools.runner import ToolRunner, outcome


class ToolService:
    def __init__(self, store, registry=None):
        self.store, self.registry = store, registry

    def calls(self, unit_id=None):
        if unit_id is None:
            return self.store.rows("SELECT * FROM tool_calls ORDER BY rowid")
        return self.store.rows(
            "SELECT * FROM tool_calls WHERE unit_id=? ORDER BY slot_no", (unit_id,)
        )

    def find(self, source_result_ref):
        rows = self.store.rows(
            "SELECT * FROM tool_calls WHERE source_result_ref=?", (source_result_ref,)
        )
        return rows[0] if rows else None

    def request(self, call):
        store = self.store
        artifact = store.one("SELECT * FROM artifacts WHERE artifact_id=?", (call["request_ref"],))
        request = store.artifact(call["request_ref"])
        op = store.find_operation(call["source_operation_id"])
        result = store.stored_result(call["source_result_ref"])
        if (
            artifact["kind"] != "tool_request"
            or digest(request) != call["request_digest"]
            or op["result_ref"] != call["source_result_ref"]
            or op["unit_id"] != call["unit_id"]
            or result.result_status != "VALID"
            or not result.decision
            or result.decision["action"] != "request_tool"
            or request != self._request(call["unit_id"], op, result.decision)
            or call["tool_call_id"] != request["tool_call_id"]
            or call["request_ref"] != stable_id("tool_request", call["tool_call_id"])
        ):
            raise AgentError("TOOL_LEDGER_INTEGRITY")
        store.safety.require_safe(request)
        return request

    def result(self, call):
        self.request(call)
        row = self.store.one("SELECT * FROM artifacts WHERE artifact_id=?", (call["result_ref"],))
        result = self.store.artifact(call["result_ref"])
        if (
            row["kind"] != "tool_result"
            or call["result_ref"] != stable_id("tool_result", call["tool_call_id"])
            or digest(result) != call["result_digest"]
            or result["tool_call_id"] != call["tool_call_id"]
            or result["tool_request_ref"] != call["request_ref"]
            or result["status"] != call["status"]
        ):
            raise AgentError("TOOL_LEDGER_INTEGRITY")
        self.store.safety.require_safe(result)
        return result

    def _request(self, unit_id, op, decision):
        frozen = self.store.config().tool_registry
        entry = frozen["entries"].get(decision["name"])
        return {
            "tool_call_id": stable_id(
                "tool_call", self.store.task()["task_id"], unit_id, op["result_ref"], 0
            ),
            "unit_id": unit_id,
            "snapshot_id": self.store.task()["snapshot_id"],
            "source_operation_id": op["operation_id"],
            "source_result_ref": op["result_ref"],
            "source_request_ref": op["request_ref"],
            "source_request_digest": op["request_digest"],
            "name": decision["name"],
            "arguments": decision["arguments"],
            "registry_digest": digest(frozen),
            "tool_identity": entry,
        }

    def register(self, unit_id, source_result_ref):
        store = self.store
        with store.transaction():
            old = self.find(source_result_ref)
            if old:
                self.request(old)
                return old
            store._admit()
            op = store.one("SELECT * FROM operations WHERE result_ref=?", (source_result_ref,))
            result = store.stored_result(source_result_ref)
            if (
                op["unit_id"] != unit_id
                or result.result_status != "VALID"
                or result.decision["action"] != "request_tool"
                or store.unit_operation_id(unit_id) != op["operation_id"]
            ):
                raise AgentError("TOOL_SOURCE_NOT_ELIGIBLE")
            count = len(self.calls(unit_id))
            if count >= store.config().max_tools_per_unit:
                raise AgentError("TOOL_LIMIT")
            request = self._request(unit_id, op, result.decision)
            call_id = request["tool_call_id"]
            ref, span = stable_id("tool_request", call_id), stable_id("tool_span", call_id)
            store._artifact(ref, "tool_request", request)
            store.fault("after_tool_request_artifact")
            store.conn.execute(
                "INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,'PENDING',NULL,NULL,?)",
                (
                    call_id,
                    unit_id,
                    op["operation_id"],
                    source_result_ref,
                    ref,
                    digest(request),
                    count + 1,
                    span,
                ),
            )
            store._span(
                span,
                "tool",
                "PENDING",
                operation=op["operation_id"],
                unit=unit_id,
                data={"tool_call_id": call_id, "tool_request_ref": ref},
            )
            store.fault("before_tool_register_commit")
        store.fault("after_tool_registered")
        return self.find(source_result_ref)

    def finish(self, call, result):
        store = self.store
        with store.transaction():
            current = self.find(call["source_result_ref"])
            if current["result_ref"]:
                saved = self.result(current)
                expected = {
                    **result,
                    "tool_call_id": call["tool_call_id"],
                    "tool_request_ref": call["request_ref"],
                }
                if saved != expected:
                    raise AgentError("TOOL_RESULT_CONFLICT")
                return saved
            if current["status"] != "RUNNING":
                raise AgentError("INVALID_TOOL_TRANSITION")
            ref = stable_id("tool_result", call["tool_call_id"])
            value = {
                **result,
                "tool_call_id": call["tool_call_id"],
                "tool_request_ref": call["request_ref"],
            }
            store._artifact(ref, "tool_result", value)
            store.fault("after_tool_result_artifact")
            store.conn.execute(
                "UPDATE tool_calls SET status=?,result_ref=?,result_digest=? WHERE tool_call_id=?",
                (result["status"], ref, digest(value), call["tool_call_id"]),
            )
            store.conn.execute(
                "UPDATE trace_spans SET status=? WHERE span_id=?",
                (result["status"], call["span_id"]),
            )
            store.fault("before_tool_complete_commit")
        store.fault("after_tool_completed")
        return value

    def recover(self):
        for call in self.calls():
            if call["status"] == "RUNNING":
                self.finish(call, outcome("INTERRUPTED", "TOOL_INTERRUPTED"))

    def run(self, unit_id, source_result_ref):
        call = self.register(unit_id, source_result_ref)
        if call["result_ref"]:
            return self.result(call)
        if call["status"] == "RUNNING":
            return self.finish(call, outcome("INTERRUPTED", "TOOL_INTERRUPTED"))
        request = self.request(call)
        registry = self.registry or ToolRegistry()
        registry.require_snapshot(self.store.config().tool_registry)
        with self.store.transaction():
            self.store._admit()
            self.store.conn.execute(
                "UPDATE tool_calls SET status='RUNNING' WHERE tool_call_id=? AND status='PENDING'",
                (call["tool_call_id"],),
            )
            self.store.fault("before_tool_running_commit")
        self.store.fault("after_tool_running")
        unit = next(u for u in self.store.units() if u["unit_id"] == unit_id)
        result = ToolRunner(registry, self.store.safety).run(
            request["name"],
            request["arguments"],
            self.store.snapshot(),
            unit,
            self.store.config().tool_registry,
        )
        self.store.fault("after_tool_output")
        return self.finish(call, result)
