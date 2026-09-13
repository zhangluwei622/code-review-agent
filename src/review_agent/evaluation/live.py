"""Approved, serial real baselines; preparation never constructs a live provider."""

import json
import platform
import sqlite3
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import Field

from review_agent import app
from review_agent.config import EvaluationTaskConfig, package_text
from review_agent.contracts import AgentError, StrictModel, digest
from review_agent.ingest import prepare_diff, read_bounded
from review_agent.pricing import current_deepseek_pricing
from review_agent.report import render
from review_agent.safety import Safety
from review_agent.storage import Storage
from review_agent.tools.registry import ToolRegistry

from .artifacts import export, save_json, write_once
from .batch import TOTALS, batch_observations, runtime_digest
from .data import load_dataset, read_model
from .scoring import AnnotationApproval


class RunApproval(StrictModel):
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_directory: str
    approved_by: str = Field(min_length=1)
    approved_at: str = Field(min_length=1)
    decision: Literal["approved"]


def _now():
    return datetime.now(UTC)


def _payload(dataset_path, annotation_path, batch_dir, created_at):
    dataset, _, fingerprint = load_dataset(dataset_path)
    annotation = read_model(annotation_path, AnnotationApproval)
    if annotation.dataset_digest != fingerprint:
        raise AgentError("EVALUATION_JUDGMENT_MISMATCH")
    pricing = current_deepseek_pricing()
    source_day = datetime.fromisoformat(pricing.source_checked_on).replace(tzinfo=UTC)
    root = Path(__file__).resolve().parents[3]
    groups = {g.group_id: g.split for g in dataset.groups}
    order = {
        stage: [c.case_id for c in dataset.cases if groups[c.group_id] == stage]
        for stage in ("development", "holdout")
    }
    if not order["development"] or not order["holdout"] or len(dataset.cases) > 12:
        raise AgentError("INVALID_BASELINE_SELECTION")
    return {
        "schema_version": 1,
        "mode": "baseline_live",
        "paid_calls_authorized": False,
        "created_at": created_at,
        "valid_until": (source_day + timedelta(days=2)).isoformat(),
        "dataset_path": str(Path(dataset_path).resolve()),
        "dataset_digest": fingerprint,
        "dataset": dataset.model_dump(),
        "annotation_path": str(Path(annotation_path).resolve()),
        "annotation_confirmation": annotation.model_dump(),
        "batch_directory": str(Path(batch_dir).resolve()),
        "runtime_digest": runtime_digest(),
        "code_revision": {"git_commit": None, "basis": "package source content digest"},
        "python_version": platform.python_version(),
        "runtime_package_versions": {
            name: version(name)
            for name in (
                "langgraph",
                "langgraph-checkpoint",
                "langgraph-checkpoint-sqlite",
                "langchain-core",
                "pydantic",
                "pydantic-core",
                "unidiff",
                "filelock",
                "httpx",
            )
        },
        "dependency_digests": {
            name: digest((root / name).read_bytes().hex()) for name in ("pyproject.toml", "uv.lock")
        },
        "prompt_digests": {
            name: digest(package_text("prompts/" + name))
            for name in ("review-tools.md", "repair-tools.md")
        },
        "tool_registry": ToolRegistry().snapshot(),
        "model": "deepseek-flash",
        "model_identity_policy": {
            "documented_version": "DeepSeek-V4.1-Flash",
            "source": pricing.source_url,
            "source_checked_on": pricing.source_checked_on,
            "accepted_response_models": [
                "deepseek-flash",
                "DeepSeek-V4.1-Flash",
                "deepseek-v4.1-flash",
            ],
            "alias_response": "Keep actual_model_version null; record documented mapping.",
            "missing_or_other_response_model": "STOP_BATCH",
        },
        "pricing_snapshot": pricing.model_dump(),
        "request_options": {
            "thinking": {"type": "disabled"},
            "stream": False,
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        },
        "max_output_tokens": 1024,
        "max_reply_bytes": 65536,
        "max_envelope_bytes": 262144,
        "transport_timeout_seconds": 30,
        "max_sends_per_case": 6,
        "max_tools_per_case": 4,
        "max_repairs_per_case": 1,
        "case_max_tokens": 100000,
        "case_max_cost_nusd": 50000000,
        "batch_max_tokens": 100000 * len(dataset.cases),
        "batch_max_cost_nusd": 50000000 * len(dataset.cases),
        "batch_max_sends": 6 * len(dataset.cases),
        "allocation_policy": "Fixed per-case allocations; no borrowing; SETTLED plus HELD.",
        "manual_unknown_retries": False,
        "stage_order": order,
        "selected": order["development"] + order["holdout"],
        "stop_policy": [
            "UNKNOWN or unresolved DISPATCHED",
            "completed call with HELD or missing usage",
            "quote overrun or budget exhaustion",
            "security rejection",
            "missing or unexpected response model",
            "integrity/configuration/approval mismatch",
        ],
        "judging": "Human judgments, frozen dataset and prompts; development and holdout separate.",
    }


def prepare_live(dataset_path, annotation_path, plan_dir, batch_dir):
    path = Path(plan_dir) / "manifest.json"
    created = read_manifest(path)["created_at"] if path.exists() else _now().isoformat()
    value = _payload(dataset_path, annotation_path, batch_dir, created)
    if _now() >= datetime.fromisoformat(value["valid_until"]):
        raise AgentError("BASELINE_PRICE_EVIDENCE_EXPIRED")
    save_json(path, value)
    fingerprint = digest(value)
    save_json(
        Path(plan_dir) / "approval.pending.json",
        {
            "manifest_digest": fingerprint,
            "batch_directory": value["batch_directory"],
            "approved_by": None,
            "approved_at": None,
            "decision": "pending",
        },
    )
    return {"manifest": str(path), "manifest_digest": fingerprint, "paid_calls_authorized": False}


def read_manifest(path):
    text = read_bounded(Path(path), 4 * 1024 * 1024)
    Safety().require_safe(text)
    try:
        value = json.loads(text)
        expected = _payload(
            value["dataset_path"],
            value["annotation_path"],
            value["batch_directory"],
            value["created_at"],
        )
        if value != expected:
            raise AgentError("BASELINE_MANIFEST_CHANGED")
    except (KeyError, TypeError, ValueError):
        raise AgentError("INVALID_BASELINE_MANIFEST") from None
    return value


def task_config(manifest, case_id):
    return EvaluationTaskConfig(
        api_style="deepseek-chat-completions",
        provider_endpoint="https://api.deepseek.com/chat/completions",
        credential_env="DEEPSEEK_API_KEY",
        pricing_source=manifest["pricing_snapshot"]["source_url"],
        deepseek_pricing=manifest["pricing_snapshot"],
        max_tokens=manifest["case_max_tokens"],
        max_cost_nusd=manifest["case_max_cost_nusd"],
        max_output_tokens=manifest["max_output_tokens"],
        max_sends_per_unit=manifest["max_sends_per_case"],
        max_repairs_per_unit=manifest["max_repairs_per_case"],
        max_tools_per_unit=manifest["max_tools_per_case"],
        policy_digest=digest(package_text("policies/secret-rules.toml")),
        prompt_digest=manifest["prompt_digests"]["review-tools.md"],
        repair_prompt_digest=manifest["prompt_digests"]["repair-tools.md"],
        tool_registry=manifest["tool_registry"],
        evaluation_batch_dir=manifest["batch_directory"],
        evaluation_manifest_digest=digest(manifest),
        evaluation_case_id=case_id,
    )


def _create_task(path, state, config):
    # Only reachable after explicit manifest approval; the key stays in Safety/transport.
    safety = Safety((app._credential("DEEPSEEK_API_KEY"),))
    snapshot, units = prepare_diff(path, config, safety)
    task_id = "task_" + uuid4().hex
    directory = state / task_id
    directory.mkdir(parents=True, mode=0o700)
    database = directory / "task.sqlite"
    database.touch(mode=0o600)
    store = Storage(database)
    try:
        store.create_task(task_id, config, snapshot, units)
    finally:
        store.close()
    return task_id


class LiveBatch:
    def __init__(self, manifest, conn):
        self.manifest, self.conn = manifest, conn
        self.directory = Path(manifest["batch_directory"])

    def stop(self, reason):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO batch_stop VALUES (1,?)", (reason,))
        raise AgentError("EVALUATION_BATCH_STOPPED")

    def reason(self):
        row = self.conn.execute("SELECT reason FROM batch_stop WHERE id=1").fetchone()
        return row[0] if row else None

    def tasks(self):
        tasks = []
        rows = self.conn.execute("SELECT case_id,task_id FROM batch_cases").fetchall()
        if any(
            p.name not in {row[0] for row in rows} for p in (self.directory / "cases").glob("*")
        ):
            self.stop("BATCH_BINDING_INVALID")
        for case_id, saved in rows:
            if case_id not in self.manifest["selected"]:
                self.stop("BATCH_BINDING_INVALID")
            state = self.directory / "cases" / case_id / "state"
            candidates = sorted(p.name for p in state.glob("task_*"))
            if saved and candidates != [saved] or not saved and len(candidates) > 1:
                self.stop("BATCH_BINDING_INVALID")
            if not candidates:
                continue
            task_id = saved or candidates[0]
            data = app.read_task(task_id, state)
            if data["config"] != task_config(self.manifest, case_id).model_dump():
                self.stop("BATCH_CONFIG_CHANGED")
            case = next(c for c in self.manifest["dataset"]["cases"] if c["case_id"] == case_id)
            if data["snapshot"]["snapshot_id"] != case["input_digest"]:
                self.stop("BATCH_INPUT_CHANGED")
            app.trace(data)  # Validate ledger/result/request associations, not only counters.
            tasks.append((case_id, task_id, state, data))
        return tasks

    def recover_uncertain(self):
        for _, task_id, state, data in self.tasks():
            if not any(a["call_status"] == "DISPATCHED" for a in data["attempts"]):
                continue
            path = app.task_path(state, task_id)
            with FileLock(str(path.parent / "task.lock"), timeout=0):
                store = Storage(path)
                try:
                    store.check_operations()
                    for attempt in store.rows(
                        "SELECT * FROM attempts WHERE call_status='DISPATCHED'"
                    ):
                        unit = store.find_operation(attempt["operation_id"])["unit_id"]
                        store.mark_unknown(attempt["attempt_id"])
                        store.pause(unit, "PROVIDER_UNKNOWN")
                finally:
                    store.close()

    def guard(self, store=None, phase="start"):
        if self.reason():
            raise AgentError("EVALUATION_BATCH_STOPPED")
        if _now() >= datetime.fromisoformat(self.manifest["valid_until"]):
            self.stop("PRICE_EVIDENCE_EXPIRED")
        if runtime_digest() != self.manifest["runtime_digest"]:
            self.stop("RUNTIME_CHANGED")
        tasks = self.tasks()
        if store is not None and not any(tid == store.task()["task_id"] for _, tid, _, _ in tasks):
            self.stop("BATCH_BINDING_INVALID")
        for _, _, _, data in tasks:
            if data["retry_decisions"]:
                self.stop("UNAPPROVED_RETRY_DECISION")
            if data["task"]["send_block_reason"]:
                self.stop(data["task"]["send_block_reason"])
            if any(u["status"] == "PAUSED_BUDGET" for u in data["units"]):
                self.stop("CASE_BUDGET_EXHAUSTED")
            if any(u["status"] == "BLOCKED_SECURITY" for u in data["units"]):
                self.stop("CASE_SECURITY_REJECTED")
            if any(c["status"] == "BLOCKED_SECURITY" for c in data["tool_calls"]):
                self.stop("TOOL_OUTPUT_SECURITY")
            for attempt in data["attempts"]:
                status = attempt["call_status"]
                if status == "UNKNOWN" or status == "DISPATCHED":
                    self.stop("PROVIDER_UNKNOWN")
                if status != "COMPLETED":
                    continue
                if attempt["fee_status"] == "HELD":
                    self.stop("USAGE_UNRESOLVED")
                result = data["artifacts"][attempt["result_ref"]]
                if result["result_status"] == "SAFETY_REJECTED":
                    self.stop("MODEL_OUTPUT_SECURITY")
                if (
                    result.get("provider_model")
                    not in self.manifest["model_identity_policy"]["accepted_response_models"]
                ):
                    self.stop("MODEL_IDENTITY_UNCONFIRMED")
        totals = {key: sum(data["totals"][key] for _, _, _, data in tasks) for key in TOTALS}
        sends = sum(sum(u["sends"] for u in data["units"]) for _, _, _, data in tasks)
        if totals["settled_tokens"] + totals["held_tokens"] > self.manifest["batch_max_tokens"]:
            self.stop("BATCH_TOKEN_LIMIT")
        if (
            totals["settled_cost_nusd"] + totals["held_cost_nusd"]
            > self.manifest["batch_max_cost_nusd"]
        ):
            self.stop("BATCH_COST_LIMIT")
        if sends > self.manifest["batch_max_sends"] or (
            phase == "dispatch" and sends >= self.manifest["batch_max_sends"]
        ):
            self.stop("BATCH_SEND_LIMIT")

    def finish(self):
        observations = batch_observations(self.directory)
        path = export(self.directory / "exports", observations.model_dump(), "observations")
        for case_id, _, _, data in self.tasks():
            directory = self.directory / "cases" / case_id / "exports"
            export(directory, app.trace(data), "trace")
            report = render(data)
            write_once(directory / ("report-" + digest(report) + ".md"), report)
        result = {
            "mode": "baseline_live",
            "manifest_digest": digest(self.manifest),
            "stop_reason": self.reason(),
            "observations": str(path),
            "planned": len(self.manifest["selected"]),
            "started": len(observations.observations),
            "sends": sum(o.sends for o in observations.observations),
            "send_limit": self.manifest["batch_max_sends"],
            "totals": {
                key: sum(o.totals[key] for o in observations.observations) for key in TOTALS
            },
        }
        summary = export(self.directory / "exports", result, "run-summary")
        return {**result, "summary": str(summary)}


def run_live(manifest_path, approval_path=None, *, stage="development", fault=None):
    manifest = read_manifest(manifest_path)
    if stage not in ("development", "holdout"):
        raise AgentError("INVALID_BASELINE_STAGE")
    directory = Path(manifest["batch_directory"])
    approval_file = Path(approval_path) if approval_path else directory / "approval.json"
    if not approval_file.is_file():
        raise AgentError("BASELINE_APPROVAL_REQUIRED")
    approval = read_model(approval_file, RunApproval)
    if approval.manifest_digest != digest(manifest) or approval.batch_directory != str(directory):
        raise AgentError("BASELINE_APPROVAL_MISMATCH")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with FileLock(str(directory / "batch.lock"), timeout=0, mode=0o600):
            return _run(manifest, approval, stage, fault or (lambda _: None))
    except Timeout:
        raise AgentError("EVALUATION_BATCH_LOCKED") from None


def _run(manifest, approval, stage, fault):
    directory = Path(manifest["batch_directory"])
    database = directory / "batch.sqlite"
    if not database.exists() and (directory / "cases").exists():
        raise AgentError("EVALUATION_BATCH_INCOMPLETE")
    save_json(directory / "manifest.json", manifest)
    save_json(directory / "approval.json", approval.model_dump())
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS batch_meta(manifest_digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_cases(case_id TEXT PRIMARY KEY,task_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS batch_stop(
            id INTEGER PRIMARY KEY CHECK(id=1),reason TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_binding BEFORE UPDATE ON batch_cases
        WHEN OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id
        BEGIN SELECT RAISE(ABORT,'EVALUATION_BINDING_IMMUTABLE'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_stop_update BEFORE UPDATE ON batch_stop
        BEGIN SELECT RAISE(ABORT,'EVALUATION_STOP_IMMUTABLE'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_stop_delete BEFORE DELETE ON batch_stop
        BEGIN SELECT RAISE(ABORT,'EVALUATION_STOP_IMMUTABLE'); END;
    """)
    batch = LiveBatch(manifest, conn)
    try:
        meta = conn.execute("SELECT manifest_digest FROM batch_meta").fetchall()
        if not meta:
            if (directory / "cases").exists():
                raise AgentError("EVALUATION_BATCH_INCOMPLETE")
            with conn:
                conn.execute("INSERT INTO batch_meta VALUES (?)", (digest(manifest),))
        elif meta != [(digest(manifest),)]:
            raise AgentError("BASELINE_MANIFEST_CHANGED")
        batch.recover_uncertain()
        batch.guard()
        if stage == "holdout":
            finished = {
                cid
                for cid, _, _, data in batch.tasks()
                if all(
                    u["status"]
                    in (
                        "DONE",
                        "ABSTAINED",
                        "PARTIAL_TRUNCATED",
                        "PARTIAL_INVALID_RESULT",
                        "PARTIAL_LIMIT",
                    )
                    for u in data["units"]
                )
            }
            if not set(manifest["stage_order"]["development"]) <= finished:
                raise AgentError("DEVELOPMENT_STAGE_REQUIRED")
        _, bundles, _ = load_dataset(manifest["dataset_path"])
        for case_id in manifest["stage_order"][stage]:
            batch.guard()
            with conn:
                conn.execute("INSERT OR IGNORE INTO batch_cases VALUES (?,NULL)", (case_id,))
            fault("after_case_started")
            case_dir = directory / "cases" / case_id
            write_once(case_dir / "input.diff", bundles[case_id]["safe_diff"])
            state = case_dir / "state"
            candidates = sorted(p.name for p in state.glob("task_*"))
            saved = conn.execute(
                "SELECT task_id FROM batch_cases WHERE case_id=?", (case_id,)
            ).fetchone()[0]
            if len(candidates) > 1 or saved and candidates != [saved]:
                batch.stop("BATCH_BINDING_INVALID")
            task_id = saved or (candidates[0] if candidates else None)
            if not task_id:
                task_id = _create_task(
                    case_dir / "input.diff", state, task_config(manifest, case_id)
                )
                fault("after_case_task_created")
            with conn:
                conn.execute("UPDATE batch_cases SET task_id=? WHERE case_id=?", (task_id, case_id))
            fault("after_case_bound")
            batch.guard()
            app.execute(task_id, state, fault=fault, batch_guard=batch.guard)
            fault("after_case_executed")
            batch.guard()
        return batch.finish()
    except AgentError as error:
        if error.code == "EVALUATION_BATCH_STOPPED":
            return batch.finish()
        raise
    finally:
        conn.close()
