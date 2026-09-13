import json
from collections.abc import Callable

from review_agent.contracts import AgentError, json_text
from review_agent.pricing import require_current_pricing
from review_agent.providers import ProviderPort
from review_agent.review import decode_reply
from review_agent.safety import Safety
from review_agent.storage import Storage


class Gateway:
    def __init__(
        self,
        store: Storage,
        provider: ProviderPort,
        safety: Safety,
        fault: Callable[[str], None] | None = None,
        batch_guard=None,
    ):
        self.store, self.provider, self.safety = store, provider, safety
        self.fault = fault or (lambda _: None)
        self.batch_guard = batch_guard

    def run(
        self,
        unit_id: str,
        request: dict | Callable[[], dict],
        *,
        operation_id=None,
        source_operation_id=None,
    ) -> str:
        operation_id = operation_id or self.store.review_operation_id(unit_id)
        old = self.store.find_operation(operation_id)
        if old:
            if old["unit_id"] != unit_id:
                raise AgentError("LEDGER_INTEGRITY")
            persisted = self.store.saved_request(operation_id)
            self.safety.require_safe(persisted)
            if old["result_ref"]:
                self.store.record_reuse(operation_id)
                return old["result_ref"]
        else:
            if self.batch_guard:
                self.batch_guard(self.store, "reserve")
            require_current_pricing(self.store.config())
            request = request() if callable(request) else request
            prepared = json.loads(json_text(self.provider.prepare(request, self.store.config())))
            self.safety.require_safe(prepared)
            self.store.persist_request(
                operation_id, unit_id, prepared, source_operation_id=source_operation_id
            )
            persisted = self.store.saved_request(operation_id)
        require_current_pricing(self.store.config())
        if self.batch_guard:
            self.batch_guard(self.store, "reserve")
        self.fault("after_request")
        existing = self.store.rows("SELECT 1 FROM attempts WHERE operation_id=?", (operation_id,))
        row = (
            self.store.attempt(operation_id)
            if existing
            else self.store.reserve_attempt(
                operation_id,
                unit_id,
                persisted,
                self.provider.quote(persisted, self.store.config()),
            )
        )
        if row["call_status"] in ("UNKNOWN", "DISPATCHED"):
            raise AgentError("PROVIDER_UNKNOWN")
        if row["call_status"] != "RESERVED":
            raise AgentError("INVALID_ATTEMPT_TRANSITION")
        self.fault("after_reserved")
        if self.batch_guard:
            self.batch_guard(self.store, "dispatch")
        if not self.store.mark_dispatched(row["attempt_id"]):
            raise AgentError("PROVIDER_UNKNOWN")
        self.fault("after_dispatched")
        try:
            persisted = self.store.saved_request(operation_id)
            self.safety.require_safe(persisted)
            context = self.store.operation_context(operation_id)
            reply = self.provider.send(
                persisted,
                context={
                    **context,
                    "attempt_no": row["attempt_no"],
                    "attempt_id": row["attempt_id"],
                },
            )
        except Exception:
            self.store.mark_unknown(row["attempt_id"])
            raise AgentError("PROVIDER_UNKNOWN") from None
        self.fault("after_reply")
        if not reply.response_complete:
            self.store.mark_unknown(row["attempt_id"])
            raise AgentError("PROVIDER_UNKNOWN")
        result = decode_reply(reply, self.store.config(), self.safety)
        ref = self.store.complete_attempt(row["attempt_id"], result)
        self.fault("after_completed")
        if self.batch_guard:
            self.batch_guard(self.store, "completed")
        return ref
