import json
import os
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from review_agent.audit import build_trace
from review_agent.config import (
    DeliveryTaskConfig,
    SourceTaskConfig,
    TaskConfig,
    package_text,
    policy_versions,
    review_prompt_path,
)
from review_agent.contracts import AgentError, digest
from review_agent.gateway import Gateway
from review_agent.ingest import prepare_diff, prepare_text
from review_agent.pricing import current_deepseek_pricing, require_current_pricing
from review_agent.providers import DeepSeekProvider, FixtureProvider
from review_agent.report import render_audit, render_review, write_report
from review_agent.safety import Safety
from review_agent.storage import Storage


def task_path(state_dir: Path, task_id: str) -> Path:
    if not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
        raise AgentError("INVALID_TASK_ID")
    directory = state_dir / task_id
    path = directory / "task.sqlite"
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise AgentError("TASK_NOT_FOUND")
    return path


def create_task(
    diff_path: Path | None,
    fixture_path: Path | None,
    state_dir: Path,
    *,
    max_tokens: int,
    max_cost_nusd: int,
    max_output_tokens: int | None = None,
    provider_name: str = "fixture",
    max_repairs_per_unit: int = 1,
    schema_version: int = 7,
    max_tools_per_unit: int = 4,
    source_url: str | None = None,
    source_path: Path | None = None,
    source_auth: bool = False,
    source_http=None,
    fault=None,
    diff_text: str | None = None,
    api_key: str | None = None,
) -> str:
    if sum(value is not None for value in (diff_path, source_url, source_path, diff_text)) != 1:
        raise AgentError("INVALID_SOURCE_SELECTION")
    has_source = source_url is not None or source_path is not None
    if has_source and schema_version != 7:
        raise AgentError("CONFIG_VERSION_MISMATCH")
    if source_auth and not source_url:
        raise AgentError("INVALID_SOURCE_SELECTION")
    if schema_version not in (4, 5, 7):
        raise AgentError("CONFIG_VERSION_MISMATCH")
    if max_output_tokens is None:
        max_output_tokens = 1024 if schema_version == 7 else 512
    config_model = DeliveryTaskConfig if schema_version == 7 else TaskConfig
    versions = policy_versions()
    if schema_version >= 5:
        from review_agent.tools.registry import ToolRegistry

        versions.update(
            prompt_digest=digest(package_text(review_prompt_path(schema_version))),
            tool_registry=ToolRegistry().snapshot(),
        )
    repair_config = {
        "schema_version": schema_version,
        "max_tools_per_unit": max_tools_per_unit,
        "max_repairs_per_unit": max_repairs_per_unit,
        "repair_prompt_digest": digest(
            package_text("prompts/repair-tools.md" if schema_version >= 5 else "prompts/repair.md")
        ),
    }
    if provider_name == "fixture":
        if fixture_path is None:
            raise AgentError("FIXTURE_REQUIRED")
        provider = FixtureProvider(fixture_path)
        config = config_model(
            fixture_path=str(fixture_path.resolve()),
            fixture_digest=provider.fingerprint,
            max_tokens=max_tokens,
            max_cost_nusd=max_cost_nusd,
            max_output_tokens=max_output_tokens,
            **versions,
            **repair_config,
        )
        safety = Safety()
    elif provider_name == "deepseek":
        if fixture_path is not None:
            raise AgentError("FIXTURE_NOT_ALLOWED")
        api_key = _credential("DEEPSEEK_API_KEY", override=api_key)
        pricing = current_deepseek_pricing()
        config = config_model(
            execution_mode="deepseek",
            model="deepseek-flash",
            api_style="deepseek-chat-completions",
            provider_endpoint="https://api.deepseek.com/chat/completions",
            credential_env="DEEPSEEK_API_KEY",
            pricing_source=pricing.source_url,
            deepseek_pricing=pricing,
            max_tokens=max_tokens,
            max_cost_nusd=max_cost_nusd,
            max_output_tokens=max_output_tokens,
            **versions,
            **repair_config,
        )
        safety = Safety((api_key,))
    else:
        raise AgentError("INVALID_PROVIDER")
    safety.require_safe(config.model_dump())
    source = None
    if has_source:
        from review_agent.sources.service import fetch_source, load_source, prepare_source

        source = (
            fetch_source(source_url, authenticate=source_auth, http=source_http)
            if source_url is not None
            else load_source(source_path)
        )
        values = {
            **config.model_dump(),
            "schema_version": 8,
            "source_digest": source.manifest_digest,
        }
        config = SourceTaskConfig.model_validate(values)
        snapshot, units = prepare_source(source, config, safety)
    elif diff_text is not None:
        snapshot, units = prepare_text(diff_text, config, safety)
    else:
        snapshot, units = prepare_diff(diff_path, config, safety)
    task_id = "task_" + uuid4().hex
    directory = state_dir / task_id
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "task.sqlite"
    path.touch(mode=0o600)
    store = Storage(path)
    store.fault = fault or (lambda _: None)
    try:
        store.create_task(
            task_id,
            config,
            snapshot,
            units,
            source=source.manifest.model_dump() if source else None,
        )
    finally:
        store.close()
    return task_id


def _credential(name: str, *, override: str | None = None) -> str:
    # Optional local UI credential stays in memory, outside frozen task configuration.
    value = os.environ.get(name, "") if override is None else override
    if len(value) < 8:
        raise AgentError("PROVIDER_CREDENTIAL_MISSING")
    return value


def _provider(config: TaskConfig, *, observer=None, api_key: str | None = None):
    if config.execution_mode == "fixture":
        provider = FixtureProvider(Path(config.fixture_path), observer=observer)
        if provider.fingerprint != config.fixture_digest:
            raise AgentError("FIXTURE_FINGERPRINT_MISMATCH")
        return provider, Safety()
    if config.execution_mode == "deepseek":
        api_key = _credential(config.credential_env, override=api_key)
        return DeepSeekProvider(config, api_key, observer=observer), Safety((api_key,))
    raise AgentError("INVALID_PROVIDER")


def execute(
    task_id: str,
    state_dir: Path,
    *,
    observer=None,
    fault=None,
    retry_unknown=None,
    batch_guard=None,
    api_key: str | None = None,
) -> dict:
    path = task_path(state_dir, task_id)
    try:
        with FileLock(str(path.parent / "task.lock"), timeout=0, mode=0o600):
            return _execute(
                path,
                observer=observer,
                fault=fault,
                retry_unknown=retry_unknown,
                batch_guard=batch_guard,
                api_key=api_key,
            )
    except Timeout:
        raise AgentError("TASK_LOCKED") from None


def _execute(
    path: Path, *, observer=None, fault=None, retry_unknown=None, batch_guard=None,
    api_key: str | None = None,
) -> dict:
    # Disable automatic remote tracing before importing/constructing the graph.
    for name in ("LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        os.environ[name] = "false"
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from langsmith import tracing_context

    from review_agent.graph import build_graph

    store = Storage(path)
    store.fault = fault or (lambda _: None)
    saver_connection = None
    try:
        task, config = store.task(), store.config()
        if (
            config.schema_version not in (1, 2, 3, 4, 5, 6, 7, 8)
            or digest(json.loads(task["config"])) != task["config_digest"]
        ):
            raise AgentError("CONFIG_VERSION_MISMATCH")
        store.check_operations()
        if config.schema_version == 6:
            if batch_guard is None or retry_unknown is not None:
                raise AgentError("EVALUATION_BATCH_ENTRY_REQUIRED")
            batch_guard(store, "start")
        saver_connection = sqlite3.connect(path, check_same_thread=False)
        saver_connection.execute("PRAGMA synchronous=FULL")
        saver = SqliteSaver(saver_connection)
        saver.setup()
        graph_config = {
            "configurable": {"thread_id": task["task_id"]},
            "callbacks": [],
            "recursion_limit": max(64, len(store.units()) * 40 + 10),
        }
        initial = {
            "task_id": task["task_id"],
            "snapshot_id": task["snapshot_id"],
            "config_digest": task["config_digest"],
            "trace_id": task["trace_id"],
            "unit_index": 0,
        }
        # Check persisted references before consuming any pending authorization, also
        # for terminal tasks that can now return without constructing a provider.
        checkpoint = saver.get_tuple(graph_config)
        if checkpoint:
            values = checkpoint.checkpoint["channel_values"]
            values = values.get("__start__", values)
            if any(
                values.get(key) != initial[key]
                for key in ("task_id", "snapshot_id", "config_digest")
            ):
                raise AgentError("CHECKPOINT_IDENTITY_MISMATCH")
            if values.get("last_result_ref"):
                store.stored_result(values["last_result_ref"])
        versions = policy_versions()
        if config.schema_version >= 5:
            versions["prompt_digest"] = digest(
                package_text(review_prompt_path(config.schema_version))
            )
        if any(getattr(config, key) != value for key, value in versions.items()):
            raise AgentError("CONFIG_VERSION_MISMATCH")
        if (
            config.schema_version >= 4
            and config.max_repairs_per_unit
            and (
                config.repair_prompt_digest
                != digest(
                    package_text(
                        "prompts/repair-tools.md"
                        if config.schema_version >= 5
                        else "prompts/repair.md"
                    )
                )
            )
        ):
            raise AgentError("CONFIG_VERSION_MISMATCH")
        dispatched = store.rows(
            """
            SELECT attempts.attempt_id,operations.unit_id FROM attempts
            JOIN operations ON operations.operation_id=attempts.operation_id
            WHERE attempts.call_status='DISPATCHED'
            """
        )
        for row in dispatched:
            store.mark_unknown(row["attempt_id"])
            store.pause(row["unit_id"], "PROVIDER_UNKNOWN")
        if retry_unknown is not None:
            if not re.fullmatch(r"attempt_[0-9a-f]{24}", retry_unknown):
                raise AgentError("INVALID_ATTEMPT_ID")
            decision = store.record_retry_decision(retry_unknown)
            if (
                decision["status"] == "BOUND"
                and store.attempt_by_id(decision["bound_attempt_id"])["call_status"] == "CANCELLED"
            ):
                return store.report_snapshot()
        task = store.task()
        pending = [d for d in store.retry_decisions() if d["status"] == "PENDING"]
        if config.schema_version >= 4 and task["status"] in (
            "COMPLETED",
            "PARTIAL",
            "BLOCKED_SECURITY",
        ):
            return store.report_snapshot()
        if task["status"] in ("PAUSED_UNKNOWN", "PAUSED_BUDGET") and not pending:
            return store.report_snapshot()
        require_current_pricing(config)
        credentials = {"api_key": api_key} if api_key is not None else {}
        provider, safety = _provider(config, observer=observer, **credentials)
        store.safety = safety
        if config.schema_version >= 5:
            from review_agent.tools.service import ToolService

            ToolService(store).recover()
        for decision in pending:
            source = store.attempt_by_id(decision["source_attempt_id"])
            operation = store.find_operation(source["operation_id"])
            persisted = store.saved_request(source["operation_id"])
            try:
                store.bind_retry_decision(
                    decision["decision_id"], provider.quote(persisted, config)
                )
            except AgentError as error:
                if error.code == "BUDGET_EXHAUSTED":
                    store.pause(operation["unit_id"], error.code)
                    return store.report_snapshot()
                if error.code == "ROUND_LIMIT":
                    store.finish_limited_unit(operation["unit_id"])
                    continue
                raise
        task = store.task()
        gateway = Gateway(store, provider, safety, fault=fault, batch_guard=batch_guard)
        graph = build_graph(store, provider, gateway, saver)
        state = graph.get_state(graph_config)
        if state.values:
            for key in ("task_id", "snapshot_id", "config_digest"):
                if state.values.get(key) != initial[key]:
                    raise AgentError("CHECKPOINT_IDENTITY_MISMATCH")
            if state.values.get("last_result_ref"):
                store.artifact(state.values["last_result_ref"])
            if not state.next:
                return store.report_snapshot()
        with tracing_context(enabled=False):
            graph_input = None if state.values else initial
            if any(t.interrupts for t in state.tasks):
                graph_input = Command(resume={"task_id": task["task_id"]})
            graph.invoke(graph_input, graph_config, durability="sync")
        return store.report_snapshot()
    finally:
        if saver_connection:
            saver_connection.close()
        store.close()


def read_task(task_id: str, state_dir: Path) -> dict:
    store = Storage(task_path(state_dir, task_id), readonly=True)
    try:
        return store.report_snapshot()
    finally:
        store.close()


def review_historical_costs(task_id: str, state_dir: Path) -> dict:
    path = task_path(state_dir, task_id)
    try:
        with FileLock(str(path.parent / "task.lock"), timeout=0, mode=0o600):
            store = Storage(path)
            try:
                reviews = store.append_historical_pricing_reviews()
                return {"task_id": task_id, "pricing_reviews": reviews}
            finally:
                store.close()
    except Timeout:
        raise AgentError("TASK_LOCKED") from None


def export_report(snapshot: dict, output: Path):
    write_report(output, render_review(snapshot))


def export_audit(snapshot: dict, output: Path):
    write_report(output, render_audit(snapshot))


def summary(snapshot: dict) -> dict:
    return {
        "task_id": snapshot["task"]["task_id"],
        "status": snapshot["task"]["status"],
        "execution_mode": snapshot["config"]["execution_mode"],
        "schema_version": snapshot["config"]["schema_version"],
        **(
            {
                key: snapshot["config"][key]
                for key in ("review_version", "reply_protocol", "prompt_digest")
            }
            if snapshot["config"]["schema_version"] in (7, 8)
            else {}
        ),
        **({"source": snapshot["source"]} if "source" in snapshot else {}),
        "send_block_reason": snapshot["task"]["send_block_reason"],
        "units": [
            {"unit_id": u["unit_id"], "status": u["status"], "reason": u["stop_reason"]}
            for u in snapshot["units"]
        ],
        "totals": snapshot["totals"],
        "retry_decisions": snapshot.get("retry_decisions", []),
        "tool_calls": [
            {
                key: call[key]
                for key in (
                    "tool_call_id",
                    "unit_id",
                    "source_result_ref",
                    "slot_no",
                    "status",
                    "request_ref",
                    "result_ref",
                )
            }
            for call in snapshot.get("tool_calls", [])
        ],
        "attempts": [
            {
                k: a[k]
                for k in (
                    "attempt_id",
                    "operation_id",
                    "attempt_no",
                    "call_status",
                    "result_status",
                    "fee_status",
                    "quote_tokens",
                    "quote_cost_nusd",
                    "actual_tokens",
                    "actual_cost_nusd",
                )
            }
            for a in snapshot["attempts"]
        ],
        "findings": [f["finding_id"] for f in snapshot["findings"]],
    }


def finding_trace(snapshot: dict, finding_id: str) -> dict:
    return trace(snapshot, finding_id=finding_id)


def trace(snapshot: dict, *, finding_id=None, attempt_id=None, sections=None) -> dict:
    return build_trace(snapshot, finding_id=finding_id, attempt_id=attempt_id, sections=sections)
