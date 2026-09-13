import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote as urlquote

from pydantic import ValidationError

from review_agent.budget import cost
from review_agent.config import TaskConfig, package_text
from review_agent.contracts import (
    AgentError,
    BudgetQuote,
    StoredResult,
    digest,
    json_text,
    stable_id,
)
from review_agent.pricing import cost_review, historical_deepseek_pricing, require_current_pricing
from review_agent.safety import Safety


class Storage:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path, self.readonly = path, readonly
        uri = "file:" + urlquote(str(path.resolve()), safe="/") + "?mode=ro"
        self.conn = sqlite3.connect(
            uri if readonly else path,
            uri=readonly,
            isolation_level=None,
            check_same_thread=False,
            timeout=5,
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.safety = Safety()
        self.fault = lambda _: None
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(package_text("schema.sql"))

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self, *, read: bool = False):
        with self.lock:
            self.conn.execute("BEGIN" if read else "BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def rows(self, sql, args=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args)]

    def one(self, sql, args=()) -> dict:
        rows = self.rows(sql, args)
        if len(rows) != 1:
            raise AgentError("LEDGER_INTEGRITY")
        return rows[0]

    def task(self) -> dict:
        return self.one("SELECT * FROM tasks")

    def config(self) -> TaskConfig:
        try:
            from review_agent.config import (
                DeliveryTaskConfig,
                EvaluationTaskConfig,
                SourceTaskConfig,
            )

            values = json.loads(self.task()["config"])
            model = {
                6: EvaluationTaskConfig,
                7: DeliveryTaskConfig,
                8: SourceTaskConfig,
            }.get(values.get("schema_version"), TaskConfig)
            return model.model_validate(values)
        except ValidationError:
            # Configuration data must never escape inside a Pydantic exception.
            raise AgentError("CONFIG_VERSION_MISMATCH") from None

    def artifact(self, ref: str) -> dict:
        return json.loads(
            self.one("SELECT data FROM artifacts WHERE artifact_id=?", (ref,))["data"]
        )

    def _artifact(self, ref: str, kind: str, value: dict):
        self.safety.require_safe(value)
        data = json_text(value)
        old = self.rows("SELECT data FROM artifacts WHERE artifact_id=?", (ref,))
        if old:
            if old[0]["data"] != data:
                raise AgentError("ARTIFACT_CONFLICT")
            return
        self.conn.execute("INSERT INTO artifacts VALUES (?,?,?)", (ref, kind, data))

    def _span(self, span_id, kind, status, *, operation=None, attempt=None, unit=None, data=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO trace_spans VALUES (?,?,?,?,?,?,?,?)",
            (
                span_id,
                self.task()["trace_id"],
                kind,
                operation,
                attempt,
                unit,
                status,
                json_text(data or {}),
            ),
        )

    def create_task(
        self, task_id: str, config: TaskConfig, snapshot: dict, units: list[dict], *, source=None
    ):
        self.safety.require_safe(config.model_dump())
        self.safety.require_safe(snapshot)
        if (config.schema_version == 8) != (source is not None):
            raise AgentError("SOURCE_SNAPSHOT_INTEGRITY")
        if source is not None:
            self.safety.require_safe(source)
            if (
                digest(source) != config.source_digest
                or source["safe_diff_digest"] != snapshot["snapshot_id"]
            ):
                raise AgentError("SOURCE_SNAPSHOT_INTEGRITY")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO snapshots VALUES (?,?)", (snapshot["snapshot_id"], json_text(snapshot))
            )
            self.conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                (
                    task_id,
                    config.model_dump_json(),
                    config.fingerprint,
                    snapshot["snapshot_id"],
                    stable_id("trace", task_id),
                    datetime.now(UTC).isoformat(),
                    "RUNNING",
                ),
            )
            for unit in units:
                self.conn.execute(
                    "INSERT INTO review_units(unit_id,ordinal,data) VALUES (?,?,?)",
                    (unit["unit_id"], unit["ordinal"], json_text(unit)),
                )
            if source is not None:
                self.conn.execute(
                    "INSERT INTO source_snapshots VALUES (?,?,?)",
                    (task_id, config.source_digest, json_text(source)),
                )
                self.fault("before_source_task_commit")
        if source is not None:
            self.fault("after_source_task_commit")

    def source(self):
        config = self.config()
        if config.schema_version != 8:
            return None
        from review_agent.sources.contracts import FrozenSource, SourceManifest
        from review_agent.sources.service import prepare_source

        try:
            row = self.one(
                "SELECT * FROM source_snapshots WHERE task_id=?", (self.task()["task_id"],)
            )
            source = json.loads(row["data"])
            if (
                row["source_digest"] != config.source_digest
                or digest(source) != config.source_digest
                or digest(json.loads(self.task()["config"])) != self.task()["config_digest"]
                or source["safe_diff_digest"] != self.task()["snapshot_id"]
                or source["policy_digest"] != config.policy_digest
            ):
                raise ValueError
            snapshot = self.snapshot()
            if snapshot["redactions"] != source["redactions"]:
                raise ValueError
            regenerated, units = prepare_source(
                FrozenSource(
                    manifest=SourceManifest.model_validate(source),
                    manifest_digest=config.source_digest,
                    safe_diff=snapshot["safe_diff"],
                ),
                config,
                self.safety,
                require_current_policy=False,
            )
            if regenerated != snapshot or units != [
                json.loads(row["data"])
                for row in self.rows("SELECT data FROM review_units ORDER BY ordinal")
            ]:
                raise ValueError
            return source
        except Exception:
            raise AgentError("SOURCE_SNAPSHOT_INTEGRITY") from None

    def snapshot(self) -> dict:
        return json.loads(self.one("SELECT data FROM snapshots")["data"])

    def units(self) -> list[dict]:
        return [
            {**json.loads(row["data"]), **{k: v for k, v in row.items() if k != "data"}}
            for row in self.rows("SELECT * FROM review_units ORDER BY ordinal")
        ]

    def totals(self) -> dict:
        return self.one("""
            SELECT COALESCE(SUM(CASE WHEN fee_status='SETTLED' THEN actual_tokens END),0)
                     AS settled_tokens,
                   COALESCE(SUM(CASE WHEN fee_status='SETTLED' THEN actual_cost_nusd END),0)
                     AS settled_cost_nusd,
                   COALESCE(SUM(CASE WHEN fee_status='HELD' THEN quote_tokens END),0)
                     AS held_tokens,
                   COALESCE(SUM(CASE WHEN fee_status='HELD' THEN quote_cost_nusd END),0)
                     AS held_cost_nusd FROM attempts
        """)

    def find_operation(self, operation_id: str, request_digest: str | None = None):
        found = self.rows("SELECT * FROM operations WHERE operation_id=?", (operation_id,))
        if found and request_digest and found[0]["request_digest"] != request_digest:
            raise AgentError("REQUEST_FINGERPRINT_MISMATCH")
        return found[0] if found else None

    def review_operation_id(self, unit_id: str) -> str:
        return stable_id("operation", self.task()["task_id"], unit_id, 0, "REVIEW", 0)

    def attempt_by_id(self, attempt_id: str) -> dict:
        return self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))

    def attempt(self, operation_id: str) -> dict:
        """Resolve the only authorized successor chain, never a 'latest attempt'."""
        attempts = self.rows("SELECT * FROM attempts WHERE operation_id=?", (operation_id,))
        roots = [a for a in attempts if a["attempt_no"] == 1]
        if len(roots) != 1:
            raise AgentError("LEDGER_INTEGRITY")
        row, visited = roots[0], set()
        while True:
            if row["attempt_id"] in visited:
                raise AgentError("LEDGER_INTEGRITY")
            visited.add(row["attempt_id"])
            decision = self.retry_decision(row["attempt_id"])
            if not decision or decision["status"] == "PENDING":
                break
            target = self.attempt_by_id(decision["bound_attempt_id"])
            if (
                row["call_status"] != "UNKNOWN"
                or target["operation_id"] != operation_id
                or target["attempt_no"] != row["attempt_no"] + 1
            ):
                raise AgentError("LEDGER_INTEGRITY")
            row = target
        if len(visited) != len(attempts):
            raise AgentError("LEDGER_INTEGRITY")
        op = self.find_operation(operation_id)
        if op["result_ref"] != row["result_ref"]:
            raise AgentError("LEDGER_INTEGRITY")
        return row

    def retry_decision(self, source_attempt_id: str):
        if not self._table_exists("retry_decisions"):
            return None
        rows = self.rows(
            "SELECT * FROM retry_decisions WHERE source_attempt_id=?", (source_attempt_id,)
        )
        return rows[0] if rows else None

    def retry_decisions(self) -> list[dict]:
        if not self._table_exists("retry_decisions"):
            return []
        return self.rows("SELECT * FROM retry_decisions ORDER BY rowid")

    def _retry_source(self, source_attempt_id: str) -> dict:
        if self.config().schema_version < 4:
            raise AgentError("RETRY_NOT_SUPPORTED")
        source = self.attempt_by_id(source_attempt_id)
        if (
            source["call_status"] != "UNKNOWN"
            or self.attempt(source["operation_id"])["attempt_id"] != source_attempt_id
            or self.find_operation(source["operation_id"])["result_ref"]
        ):
            raise AgentError("RETRY_SOURCE_NOT_ELIGIBLE")
        return source

    def record_retry_decision(self, source_attempt_id: str) -> dict:
        with self.transaction():
            # Lookup MUST precede eligibility: a repeated choice returns its binding,
            # even after the authorized successor has completed or become UNKNOWN.
            old = self.retry_decision(source_attempt_id)
            if old:
                return old
            self._retry_source(source_attempt_id)
            task = self.task()
            if task["send_block_reason"]:
                raise AgentError("BUDGET_BOUND_VIOLATION")
            if task["status"] not in ("RUNNING", "PAUSED_UNKNOWN", "PAUSED_BUDGET"):
                raise AgentError("TASK_NOT_RUNNING")
            require_current_pricing(self.config())
            decision_id = stable_id("decision", task["task_id"], source_attempt_id, "RETRY")
            self.conn.execute(
                "INSERT INTO retry_decisions VALUES (?,?,'RETRY','PENDING',NULL,?)",
                (decision_id, source_attempt_id, datetime.now(UTC).isoformat()),
            )
            self._span(decision_id, "retry_decision", "PENDING", attempt=source_attempt_id)
        self.fault("after_decision")
        return self.retry_decision(source_attempt_id)

    def bind_retry_decision(self, decision_id: str, quote: BudgetQuote) -> dict:
        with self.transaction():
            decision = self.one("SELECT * FROM retry_decisions WHERE decision_id=?", (decision_id,))
            if decision["status"] == "BOUND":
                return self.attempt_by_id(decision["bound_attempt_id"])
            source = self._retry_source(decision["source_attempt_id"])
            task, config = self.task(), self.config()
            if task["send_block_reason"]:
                raise AgentError("BUDGET_BOUND_VIOLATION")
            if task["status"] not in ("RUNNING", "PAUSED_UNKNOWN", "PAUSED_BUDGET"):
                raise AgentError("TASK_NOT_RUNNING")
            require_current_pricing(config)
            op = self.find_operation(source["operation_id"])
            self.saved_request(op["operation_id"])
            unit = self.one("SELECT * FROM review_units WHERE unit_id=?", (op["unit_id"],))
            if unit["sends"] >= config.max_sends_per_unit:
                raise AgentError("ROUND_LIMIT")
            self._check_budget(quote)
            attempt_id = stable_id("attempt", source["operation_id"], decision_id)
            row = self._insert_attempt(op, attempt_id, source["attempt_no"] + 1, quote, retry=True)
            self.conn.execute(
                "UPDATE retry_decisions SET status='BOUND',bound_attempt_id=? "
                "WHERE decision_id=? AND status='PENDING'",
                (attempt_id, decision_id),
            )
            self.fault("after_retry_binding")
            self.conn.execute("UPDATE tasks SET status='RUNNING'")
            self.conn.execute(
                "UPDATE review_units SET status='RUNNING',stop_reason=NULL WHERE unit_id=?",
                (op["unit_id"],),
            )
            self._span(
                stable_id("binding", decision_id),
                "retry_binding",
                "BOUND",
                operation=op["operation_id"],
                attempt=attempt_id,
                unit=op["unit_id"],
                data={"decision_id": decision_id, "source_attempt_id": source["attempt_id"]},
            )
        self.fault("after_retry_bound")
        return row

    def saved_request(self, operation_id: str) -> dict:
        op = self.find_operation(operation_id)
        if not op:
            raise AgentError("LEDGER_INTEGRITY")
        row = self.one("SELECT * FROM artifacts WHERE artifact_id=?", (op["request_ref"],))
        request = json.loads(row["data"])
        if row["kind"] != "request" or digest(request) != op["request_digest"]:
            raise AgentError("REQUEST_FINGERPRINT_MISMATCH")
        self.safety.require_safe(request)
        return request

    def stored_result(self, ref: str) -> StoredResult:
        artifact = self.one("SELECT * FROM artifacts WHERE artifact_id=?", (ref,))
        attempt = self.one("SELECT * FROM attempts WHERE result_ref=?", (ref,))
        if artifact["kind"] != "result" or attempt["call_status"] != "COMPLETED":
            raise AgentError("LEDGER_INTEGRITY")
        return StoredResult.model_validate_json(artifact["data"])

    def check_operations(self):
        self.source()
        for op in self.rows("SELECT * FROM operations"):
            self.saved_request(op["operation_id"])
            self.operation_context(op["operation_id"])
            if self.rows("SELECT 1 FROM attempts WHERE operation_id=?", (op["operation_id"],)):
                self.attempt(op["operation_id"])
            if op["result_ref"]:
                self.stored_result(op["result_ref"])
        for unit in self.units():
            self.unit_operation_id(unit["unit_id"])
        if self.config().schema_version >= 5:
            from review_agent.tool_loop import ToolLoop

            ToolLoop(self).check()

    def operation_context(self, operation_id: str) -> dict:
        if self.config().schema_version >= 5:
            return self.one("SELECT * FROM model_turns WHERE operation_id=?", (operation_id,))
        if self.config().schema_version < 4:
            op = self.find_operation(operation_id)
            return {
                "operation_id": operation_id,
                "unit_id": op["unit_id"],
                "kind": "REVIEW",
                "ordinal": 0,
                "source_operation_id": None,
                "source_result_ref": None,
                "prompt_digest": self.config().prompt_digest,
            }
        return self.one("SELECT * FROM operation_context WHERE operation_id=?", (operation_id,))

    def repair_operation_id(self, source_operation_id: str) -> str:
        return stable_id("operation", source_operation_id, "REPAIR", 1)

    def repair_eligible(self, operation_id: str) -> bool:
        if self.config().schema_version >= 5:
            from review_agent.tool_loop import ToolLoop

            return ToolLoop(self).repair_eligible(operation_id)
        config = self.config()
        if config.schema_version < 4 or not config.max_repairs_per_unit:
            return False
        op = self.find_operation(operation_id)
        if (
            not op
            or not op["result_ref"]
            or self.operation_context(operation_id)["kind"] != "REVIEW"
        ):
            return False
        result = StoredResult.model_validate(self.artifact(op["result_ref"]))
        return (
            result.result_status == "FORMAT_INVALID"
            and result.completion_state == "COMPLETE"
            and bool(result.safe_body)
        )

    def unit_operation_id(self, unit_id: str) -> str:
        if self.config().schema_version >= 5:
            from review_agent.tool_loop import ToolLoop

            return ToolLoop(self).cursor(unit_id)["operation_id"]
        review_id = self.review_operation_id(unit_id)
        if self.config().schema_version < 4:
            return review_id
        repair_id = self.repair_operation_id(review_id)
        if self.find_operation(repair_id):
            context = self.operation_context(repair_id)
            source = self.find_operation(review_id)
            if (
                not self.repair_eligible(review_id)
                or context["kind"] != "REPAIR"
                or context["unit_id"] != unit_id
                or context["source_operation_id"] != review_id
                or context["source_result_ref"] != source["result_ref"]
            ):
                raise AgentError("LEDGER_INTEGRITY")
            return repair_id
        source = self.find_operation(review_id)
        if self.repair_eligible(review_id) and not self.task()["send_block_reason"]:
            validation_ref = stable_id("validation", source["result_ref"])
            if self.rows(
                "SELECT 1 FROM artifacts WHERE artifact_id=? AND kind='validation'",
                (validation_ref,),
            ):
                return repair_id
        return review_id

    def _insert_context(self, operation_id, unit_id, source_operation_id=None):
        config = self.config()
        if config.schema_version >= 5:
            from review_agent.tool_loop import ToolLoop

            ToolLoop(self).insert_context(operation_id, unit_id, source_operation_id)
            return
        if config.schema_version < 4:
            if source_operation_id:
                raise AgentError("REPAIR_NOT_SUPPORTED")
            return
        if source_operation_id:
            source = self.find_operation(source_operation_id)
            if (
                not self.repair_eligible(source_operation_id)
                or source["unit_id"] != unit_id
                or operation_id != self.repair_operation_id(source_operation_id)
            ):
                raise AgentError("REPAIR_SOURCE_NOT_ELIGIBLE")
            kind, ordinal, ref, prompt = (
                "REPAIR",
                1,
                source["result_ref"],
                config.repair_prompt_digest,
            )
        else:
            if operation_id != self.review_operation_id(unit_id):
                raise AgentError("LEDGER_INTEGRITY")
            kind, ordinal, ref, prompt = "REVIEW", 0, None, config.prompt_digest
        self.conn.execute(
            "INSERT INTO operation_context VALUES (?,?,?,?,?,?,?)",
            (operation_id, unit_id, kind, ordinal, source_operation_id, ref, prompt),
        )

    def persist_request(
        self, operation_id: str, unit_id: str, request: dict, *, source_operation_id=None
    ) -> dict:
        with self.transaction():
            old = self.find_operation(operation_id, digest(request))
            if old:
                if old["unit_id"] != unit_id:
                    raise AgentError("LEDGER_INTEGRITY")
                return old
            self._admit()
            ref = stable_id("request", operation_id)
            self._artifact(ref, "request", request)
            self.conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?,NULL)",
                (operation_id, unit_id, digest(request), ref),
            )
            self._insert_context(operation_id, unit_id, source_operation_id)
            return self.find_operation(operation_id)

    def _admit(self):
        task = self.task()
        if task["send_block_reason"]:
            raise AgentError("BUDGET_BOUND_VIOLATION")
        if task["status"] != "RUNNING":
            raise AgentError("TASK_NOT_RUNNING")

    def _check_budget(self, quote: BudgetQuote):
        config, used = self.config(), self.totals()
        if (
            used["settled_tokens"] + used["held_tokens"] + quote.tokens > config.max_tokens
            or used["settled_cost_nusd"] + used["held_cost_nusd"] + quote.cost_nusd
            > config.max_cost_nusd
        ):
            raise AgentError("BUDGET_EXHAUSTED")

    def _insert_attempt(self, op, attempt_id, attempt_no, quote, *, retry=False):
        span_id = stable_id("span", attempt_id)
        self.conn.execute(
            """INSERT INTO attempts(attempt_id,operation_id,attempt_no,call_status,fee_status,
               quote,quote_tokens,quote_cost_nusd,span_id)
               VALUES (?,?,?,'RESERVED','HELD',?,?,?,?)""",
            (
                attempt_id,
                op["operation_id"],
                attempt_no,
                quote.model_dump_json(),
                quote.tokens,
                quote.cost_nusd,
                span_id,
            ),
        )
        if retry:
            self.fault("after_retry_attempt")
        self.conn.execute(
            "INSERT INTO budget_events VALUES (?,'RESERVE',?,?)",
            (attempt_id, quote.tokens, quote.cost_nusd),
        )
        if retry:
            self.fault("after_retry_reserve")
        self._span(
            span_id,
            "model",
            "RESERVED",
            operation=op["operation_id"],
            attempt=attempt_id,
            unit=op["unit_id"],
        )
        return self.attempt_by_id(attempt_id)

    def reserve_attempt(self, operation_id: str, unit_id: str, request: dict, quote: BudgetQuote):
        with self.transaction():
            old = self.find_operation(operation_id, digest(request))
            if old:
                if old["unit_id"] != unit_id:
                    raise AgentError("LEDGER_INTEGRITY")
                if self.rows("SELECT 1 FROM attempts WHERE operation_id=?", (operation_id,)):
                    return self.attempt(operation_id)
            self._admit()
            if self.config().schema_version >= 4:
                require_current_pricing(self.config())
            self._check_budget(quote)
            if not old:
                ref = stable_id("request", operation_id)
                self._artifact(ref, "request", request)
                self.conn.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,NULL)",
                    (operation_id, unit_id, digest(request), ref),
                )
                self._insert_context(operation_id, unit_id)
            op = self.find_operation(operation_id)
            return self._insert_attempt(op, stable_id("attempt", operation_id, 1), 1, quote)

    def mark_dispatched(self, attempt_id: str) -> bool:
        with self.transaction():
            self._admit()
            if self.config().schema_version >= 4:
                require_current_pricing(self.config())
            row = self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
            if row["call_status"] != "RESERVED":
                return False
            op = self.find_operation(row["operation_id"])
            if self.attempt(row["operation_id"])["attempt_id"] != attempt_id:
                raise AgentError("LEDGER_INTEGRITY")
            self.saved_request(op["operation_id"])
            unit = self.one("SELECT * FROM review_units WHERE unit_id=?", (op["unit_id"],))
            if unit["sends"] >= self.config().max_sends_per_unit:
                raise AgentError("ROUND_LIMIT")
            self.conn.execute(
                "UPDATE attempts SET call_status='DISPATCHED' WHERE attempt_id=?", (attempt_id,)
            )
            self.fault("after_dispatch_status")
            self.conn.execute(
                "INSERT INTO attempt_timing(attempt_id,dispatched_at) VALUES (?,?)",
                (attempt_id, datetime.now(UTC).isoformat()),
            )
            self.conn.execute(
                "UPDATE review_units SET sends=sends+1,status='RUNNING' WHERE unit_id=?",
                (op["unit_id"],),
            )
            self.conn.execute(
                "UPDATE trace_spans SET status='DISPATCHED' WHERE span_id=?", (row["span_id"],)
            )
            self.fault("before_dispatch_commit")
            return True

    def complete_attempt(self, attempt_id: str, result: StoredResult) -> str:
        with self.transaction():
            row = self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
            if row["call_status"] == "COMPLETED":
                if self.artifact(row["result_ref"]) != result.model_dump():
                    raise AgentError("RESULT_CONFLICT")
                return row["result_ref"]
            if row["call_status"] != "DISPATCHED":
                raise AgentError("INVALID_ATTEMPT_TRANSITION")
            if self.attempt(row["operation_id"])["attempt_id"] != attempt_id:
                raise AgentError("LEDGER_INTEGRITY")
            ref = stable_id("result", attempt_id)
            self._artifact(ref, "result", result.model_dump())
            self.fault("after_result_artifact")
            tokens = result.usage.total_tokens if result.usage else None
            config = self.config()
            amount = cost(result.usage, config) if result.usage else None
            fee = "SETTLED" if result.usage else "HELD"
            completed_at = datetime.now(UTC).isoformat()
            self.conn.execute(
                """
                UPDATE attempts SET call_status='COMPLETED',result_status=?,fee_status=?,
                actual_tokens=?,actual_cost_nusd=?,result_ref=?,error_code=? WHERE attempt_id=?
            """,
                (result.result_status, fee, tokens, amount, ref, result.error_code, attempt_id),
            )
            self.fault("after_result_status")
            self.conn.execute(
                "UPDATE operations SET result_ref=? WHERE operation_id=?",
                (ref, row["operation_id"]),
            )
            if result.usage:
                self.conn.execute(
                    "INSERT INTO budget_events VALUES (?,'SETTLE',?,?)",
                    (attempt_id, tokens, amount),
                )
                self.fault("after_settle")
                if config.execution_mode == "deepseek" and config.schema_version >= 3:
                    timing = self.one(
                        "SELECT * FROM attempt_timing WHERE attempt_id=?", (attempt_id,)
                    )
                    review = cost_review(
                        result.usage,
                        config.deepseek_pricing,
                        review_kind="SETTLEMENT_ESTIMATE",
                        recorded_at=completed_at,
                        assumed_at=timing["dispatched_at"],
                        completed_at=completed_at,
                        assumed_at_kind="local_dispatched_at",
                        original_cost_nusd=None,
                        ledger_cost_nusd=amount,
                        uncertainties=[
                            "DeepSeek does not document which timestamp selects the price tier.",
                            "The ledger uses the peak-rate estimate; it is not a provider bill "
                            "reconciliation.",
                        ],
                    )
                    self._insert_pricing_review(attempt_id, review)
                if tokens > row["quote_tokens"] or amount > row["quote_cost_nusd"]:
                    self.conn.execute(
                        """
                        UPDATE tasks SET send_block_reason='BUDGET_BOUND_VIOLATION',
                        send_block_attempt_id=?,status='STOPPED_BUDGET_BOUND'
                        WHERE send_block_reason IS NULL
                    """,
                        (attempt_id,),
                    )
            self.conn.execute(
                "UPDATE attempt_timing SET completed_at=? WHERE attempt_id=?",
                (completed_at, attempt_id),
            )
            self.conn.execute(
                "UPDATE trace_spans SET status='COMPLETED',data=? WHERE span_id=?",
                (json_text({"result_ref": ref, "fee_status": fee}), row["span_id"]),
            )
            self.fault("before_complete_commit")
            return ref

    def _insert_pricing_review(self, attempt_id: str, review: dict):
        self.safety.require_safe(review)
        pricing_version = review["pricing"]["version"]
        review_id = stable_id("pricing_review", attempt_id, review["review_kind"], pricing_version)
        old = self.rows("SELECT data FROM pricing_reviews WHERE review_id=?", (review_id,))
        if old:
            return review_id
        self.conn.execute(
            "INSERT INTO pricing_reviews VALUES (?,?,?,?,?,?)",
            (
                review_id,
                attempt_id,
                review["review_kind"],
                pricing_version,
                review["recorded_at"],
                json_text(review),
            ),
        )
        return review_id

    def append_historical_pricing_reviews(self) -> list[dict]:
        config = self.config()
        if config.execution_mode != "deepseek" or config.schema_version >= 3:
            raise AgentError("HISTORICAL_PRICING_REVIEW_NOT_APPLICABLE")
        pricing = historical_deepseek_pricing()
        task = self.task()
        recorded_at = datetime.now(UTC).isoformat()
        with self.transaction():
            for attempt in self.rows(
                "SELECT * FROM attempts WHERE call_status='COMPLETED' AND fee_status='SETTLED'"
            ):
                result = StoredResult.model_validate(self.artifact(attempt["result_ref"]))
                if result.usage is None:
                    continue
                review = cost_review(
                    result.usage,
                    pricing,
                    review_kind="HISTORICAL_REVIEW",
                    recorded_at=recorded_at,
                    assumed_at=task["created_at"],
                    completed_at=None,
                    assumed_at_kind="task_created_at_proxy",
                    original_cost_nusd=attempt["actual_cost_nusd"],
                    ledger_cost_nusd=attempt["actual_cost_nusd"],
                    uncertainties=[
                        "The historical task did not persist dispatch or completion timestamps.",
                        "Task creation time is only a proxy for price-tier classification.",
                        "DeepSeek does not document which timestamp selects the price tier.",
                        "The estimate has not been reconciled with the provider bill.",
                    ],
                )
                self._insert_pricing_review(attempt["attempt_id"], review)
        return self.pricing_reviews()

    def _table_exists(self, name: str) -> bool:
        return bool(self.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def pricing_reviews(self) -> list[dict]:
        if not self._table_exists("pricing_reviews"):
            return []
        return [
            {**row, "data": json.loads(row["data"])}
            for row in self.rows("SELECT * FROM pricing_reviews ORDER BY rowid")
        ]

    def attempt_timings(self) -> list[dict]:
        if not self._table_exists("attempt_timing"):
            return []
        return self.rows("SELECT * FROM attempt_timing ORDER BY rowid")

    def mark_unknown(self, attempt_id: str):
        with self.transaction():
            row = self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
            if row["call_status"] == "UNKNOWN":
                return
            if row["call_status"] != "DISPATCHED":
                raise AgentError("INVALID_ATTEMPT_TRANSITION")
            self.conn.execute(
                """
                UPDATE attempts SET call_status='UNKNOWN',error_code='PROVIDER_UNKNOWN'
                WHERE attempt_id=?
            """,
                (attempt_id,),
            )
            self.conn.execute(
                "UPDATE trace_spans SET status='UNKNOWN' WHERE span_id=?", (row["span_id"],)
            )
        self.fault("after_unknown")

    def cancel_reserved(self, attempt_id: str):
        with self.transaction():
            self._cancel(attempt_id)

    def _cancel(self, attempt_id):
        row = self.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        if row["call_status"] == "CANCELLED":
            return
        if row["call_status"] != "RESERVED":
            raise AgentError("INVALID_ATTEMPT_TRANSITION")
        self.conn.execute(
            "UPDATE attempts SET call_status='CANCELLED',fee_status='RELEASED' WHERE attempt_id=?",
            (attempt_id,),
        )
        self.conn.execute(
            "INSERT INTO budget_events VALUES (?,'RELEASE',?,?)",
            (attempt_id, row["quote_tokens"], row["quote_cost_nusd"]),
        )
        self.conn.execute(
            "UPDATE trace_spans SET status='CANCELLED' WHERE span_id=?", (row["span_id"],)
        )

    def record_reuse(self, operation_id: str):
        with self.transaction():
            row = self.attempt(operation_id)
            self._span(
                stable_id("reuse", operation_id),
                "reuse",
                "REUSED",
                operation=operation_id,
                attempt=row["attempt_id"],
                data={"result_ref": row["result_ref"]},
            )

    def mark_unit(self, unit_id: str, status: str, reason: str | None):
        with self.transaction():
            self.conn.execute(
                "UPDATE review_units SET status=?,stop_reason=? WHERE unit_id=?",
                (status, reason, unit_id),
            )

    def finish_limited_unit(self, unit_id: str):
        with self.transaction():
            if self.task()["send_block_reason"]:
                raise AgentError("BUDGET_BOUND_VIOLATION")
            self.conn.execute(
                "UPDATE review_units SET status='PARTIAL_LIMIT',stop_reason='ROUND_LIMIT' "
                "WHERE unit_id=?",
                (unit_id,),
            )
            self.conn.execute("UPDATE tasks SET status='RUNNING'")

    def pause(self, unit_id: str, reason: str):
        state = "PAUSED_UNKNOWN" if reason == "PROVIDER_UNKNOWN" else "PAUSED_BUDGET"
        with self.transaction():
            self.conn.execute(
                "UPDATE review_units SET status=?,stop_reason=? WHERE unit_id=?",
                (state, reason, unit_id),
            )
            self.conn.execute("UPDATE tasks SET status=?", (state,))
            self._span(stable_id("pause", unit_id, reason), "pause", state, unit=unit_id)

    def save_validation(self, unit_id: str, ref: str, validation: dict, *, publish=True):
        validation_ref = stable_id("validation", ref)
        with self.transaction():
            self._artifact(validation_ref, "validation", validation)
            self.fault("after_validation_artifact")
            for entry in validation["findings"]:
                finding_id = stable_id("finding", ref, entry["candidate_index"])
                self.conn.execute(
                    "INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?)",
                    (
                        finding_id,
                        unit_id,
                        ref,
                        validation_ref,
                        entry["candidate_index"],
                        json_text(entry),
                    ),
                )
                for role, refs in (
                    ("evidence", entry["evidence"]),
                    ("expectation", entry["expectation_evidence"]),
                ):
                    for evidence in refs:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO finding_evidence VALUES (?,?,?)",
                            (finding_id, evidence, role),
                        )
            # Validation artifacts are facts about one result. Publishing the current
            # unit outcome is separate: old checkpoint replay cannot replace a repair.
            operation = self.one("SELECT * FROM operations WHERE result_ref=?", (ref,))
            if operation["unit_id"] != unit_id:
                raise AgentError("LEDGER_INTEGRITY")
            if publish and self.unit_operation_id(unit_id) == operation["operation_id"]:
                self.conn.execute(
                    "UPDATE review_units SET status=?,stop_reason=?,validation_ref=? "
                    "WHERE unit_id=?",
                    (validation["status"], validation["reason"], validation_ref, unit_id),
                )
            self._span(
                validation_ref,
                "validation",
                validation["status"],
                unit=unit_id,
                data={"result_ref": ref},
            )
            self.fault("before_validation_commit")
        return validation_ref

    def finalize(self):
        with self.transaction():
            states = [u["status"] for u in self.units()]
            task = self.task()
            if "BLOCKED_SECURITY" in states:
                status = "BLOCKED_SECURITY"
            elif task["send_block_reason"]:
                status = "STOPPED_BUDGET_BOUND"
            elif states and all(s == "DONE" for s in states) and not self.snapshot()["excluded"]:
                status = "COMPLETED"
            else:
                status = "PARTIAL"
            self.conn.execute("UPDATE tasks SET status=?", (status,))
            reason = task["send_block_reason"] or (
                "BLOCKED_SECURITY" if status == "BLOCKED_SECURITY" else None
            )
            if reason:
                self.conn.execute(
                    "UPDATE review_units SET stop_reason=? WHERE status='PENDING'", (reason,)
                )
            for row in self.rows("SELECT attempt_id FROM attempts WHERE call_status='RESERVED'"):
                self._cancel(row["attempt_id"])

    def report_snapshot(self) -> dict:
        with self.transaction(read=True):
            artifacts = self.rows("SELECT * FROM artifacts")
            return {
                **({"source": self.source()} if self.config().schema_version == 8 else {}),
                "task": self.task(),
                "config": self.config().model_dump(),
                "snapshot": self.snapshot(),
                "units": self.units(),
                "totals": self.totals(),
                "attempts": self.rows("SELECT * FROM attempts ORDER BY rowid"),
                "operations": self.rows("SELECT * FROM operations ORDER BY rowid"),
                "operation_contexts": (
                    self.rows("SELECT * FROM model_turns ORDER BY rowid")
                    if self.config().schema_version >= 5
                    else self.rows("SELECT * FROM operation_context ORDER BY rowid")
                    if self._table_exists("operation_context")
                    else []
                ),
                "retry_decisions": self.retry_decisions(),
                "tool_calls": (
                    self.rows("SELECT * FROM tool_calls ORDER BY rowid")
                    if self._table_exists("tool_calls")
                    else []
                ),
                "budget_events": self.rows("SELECT * FROM budget_events ORDER BY rowid"),
                "attempt_timings": self.attempt_timings(),
                "pricing_reviews": self.pricing_reviews(),
                "findings": self.rows("SELECT * FROM findings ORDER BY unit_id,candidate_index"),
                "evidence": self.rows("SELECT * FROM finding_evidence ORDER BY finding_id,role"),
                "spans": self.rows("SELECT * FROM trace_spans ORDER BY rowid"),
                "artifacts": {row["artifact_id"]: json.loads(row["data"]) for row in artifacts},
                "artifact_kinds": {row["artifact_id"]: row["kind"] for row in artifacts},
            }
