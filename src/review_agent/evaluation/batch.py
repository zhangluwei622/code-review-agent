import json
import sqlite3
from pathlib import Path

from filelock import FileLock, Timeout

from review_agent import app
from review_agent.contracts import AgentError, digest
from review_agent.ingest import read_bounded

from .artifacts import export, save_json, write_once
from .collect import collect
from .data import Dataset, Observation, Observations, load_dataset

TOTALS = ("settled_tokens", "settled_cost_nusd", "held_tokens", "held_cost_nusd")


def runtime_digest():
    root = Path(__file__).resolve().parents[1]
    return digest(
        {
            str(p.relative_to(root)): digest(p.read_bytes().hex())
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in (".py", ".md", ".json", ".toml", ".sql")
        }
    )


def run_fixture(
    dataset_path,
    directory,
    *,
    split=None,
    max_tokens=None,
    max_cost_nusd=None,
    observer=None,
    fault=None,
):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(directory / "batch.lock"), timeout=0, mode=0o600):
            return _run(dataset_path, directory, split, max_tokens, max_cost_nusd, observer, fault)
    except Timeout:
        raise AgentError("EVALUATION_BATCH_LOCKED") from None


def _run(dataset_path, directory, split, max_tokens, max_cost_nusd, observer, fault):
    dataset, bundles, fingerprint = load_dataset(dataset_path)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(read_bounded(manifest_path, 4 * 1024 * 1024))
        if (
            manifest["mode"] != "fixture"
            or manifest["dataset_digest"] != fingerprint
            or manifest["runtime_digest"] != runtime_digest()
            or any(
                value is not None and value != manifest[key]
                for key, value in (
                    ("split", split),
                    ("case_max_tokens", max_tokens),
                    ("case_max_cost_nusd", max_cost_nusd),
                )
            )
        ):
            raise AgentError("EVALUATION_BATCH_MISMATCH")
    else:
        # A half-created batch must never be mistaken for a fresh allocation.
        if any(p.name != "batch.lock" for p in directory.iterdir()):
            raise AgentError("EVALUATION_BATCH_INCOMPLETE")
        selected_split = split or "development"
        if selected_split not in ("development", "holdout", "all"):
            raise AgentError("INVALID_EVALUATION_SPLIT")
        splits = {g.group_id: g.split for g in dataset.groups}
        selected = [
            c.case_id for c in dataset.cases if selected_split in ("all", splits[c.group_id])
        ]
        tokens = 5000 if max_tokens is None else max_tokens
        amount = 10_000_000 if max_cost_nusd is None else max_cost_nusd
        if (
            not selected
            or len(selected) > 12
            or not 0 <= tokens <= 100000
            or not 0 <= amount <= 50_000_000
        ):
            raise AgentError("INVALID_EVALUATION_BUDGET")
        manifest = {
            "schema_version": 1,
            "mode": "fixture",
            "paid_calls_authorized": False,
            "dataset_digest": fingerprint,
            "dataset": dataset.model_dump(),
            "runtime_digest": runtime_digest(),
            "split": selected_split,
            "selected": selected,
            "case_max_tokens": tokens,
            "case_max_cost_nusd": amount,
            "batch_max_sends": len(selected) * 6,
            "batch_max_tokens": len(selected) * tokens,
            "batch_max_cost_nusd": len(selected) * amount,
        }
        save_json(manifest_path, manifest)
    conn = sqlite3.connect(directory / "batch.sqlite")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch_cases(case_id TEXT PRIMARY KEY, task_id TEXT UNIQUE)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS batch_meta(manifest_digest TEXT NOT NULL)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS immutable_binding
        BEFORE UPDATE ON batch_cases WHEN OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id
        BEGIN SELECT RAISE(ABORT, 'EVALUATION_BINDING_IMMUTABLE'); END""")
    try:
        old = conn.execute("SELECT manifest_digest FROM batch_meta").fetchall()
        if old and old != [(digest(manifest),)]:
            raise AgentError("EVALUATION_BATCH_MISMATCH")
        if not old:
            # Existing task directories with a missing batch database are not a new batch.
            if (directory / "cases").exists():
                raise AgentError("EVALUATION_BATCH_INCOMPLETE")
            with conn:
                conn.execute("INSERT INTO batch_meta VALUES (?)", (digest(manifest),))
        by_id = {c.case_id: c for c in dataset.cases}
        # Read every existing ledger before resuming any case. An overrun in a later
        # case also blocks earlier paused cases; neither fees nor HELD can disappear.
        prior = batch_observations(directory).observations
        if any(o.source.get("send_block", {}).get("reason") for o in prior):
            return finish(directory, manifest)
        for case_id in manifest["selected"]:
            case = by_id[case_id]
            with conn:
                conn.execute("INSERT OR IGNORE INTO batch_cases VALUES (?,NULL)", (case_id,))
            if fault:
                fault("after_case_started")
            case_dir = directory / "cases" / case_id
            write_once(case_dir / "input.diff", bundles[case_id]["safe_diff"])
            write_once(case_dir / "fixture.json", bundles[case_id]["fixture"])
            state = case_dir / "state"
            saved = conn.execute(
                "SELECT task_id FROM batch_cases WHERE case_id=?", (case_id,)
            ).fetchone()[0]
            candidates = sorted(state.glob("task_*"))
            if saved:
                if [p.name for p in candidates] != [saved]:
                    raise AgentError("EVALUATION_TASK_BINDING_INVALID")
                task_id = saved
            elif candidates:
                if len(candidates) != 1:
                    raise AgentError("EVALUATION_TASK_BINDING_INVALID")
                task_id = candidates[0].name
            else:
                task_id = app.create_task(
                    case_dir / "input.diff",
                    case_dir / "fixture.json",
                    state,
                    max_tokens=manifest["case_max_tokens"],
                    max_cost_nusd=manifest["case_max_cost_nusd"],
                    provider_name="fixture",
                    schema_version=5,  # Preserve the accepted offline evaluation protocol.
                )
                if fault:
                    fault("after_case_task_created")
            data = app.read_task(task_id, state)
            config = data["config"]
            # No online task, changed allocation, or substituted input may reach execute().
            if (
                config["execution_mode"] != "fixture"
                or config["schema_version"] != 5
                or config["max_tokens"] != manifest["case_max_tokens"]
                or config["max_cost_nusd"] != manifest["case_max_cost_nusd"]
                or config["fixture_digest"] != case.fixture_digest
                or data["snapshot"]["snapshot_id"] != case.input_digest
                or config["fixture_path"] != str((case_dir / "fixture.json").resolve())
                or config["max_sends_per_unit"] != 6
                or config["max_tools_per_unit"] != 4
                or config["max_output_tokens"] != 512
                or config["max_repairs_per_unit"] != 1
                or data["task"]["task_id"] != task_id
            ):
                raise AgentError("EVALUATION_TASK_BINDING_INVALID")
            with conn:
                conn.execute("UPDATE batch_cases SET task_id=? WHERE case_id=?", (task_id, case_id))
            if fault:
                fault("after_case_bound")
            data = app.execute(task_id, state, observer=observer, fault=fault)
            if fault:
                fault("after_case_executed")
            # A real quote overrun must also stop later cases, including after batch resume.
            if data["task"]["send_block_reason"]:
                break
        return finish(directory, manifest)
    finally:
        conn.close()


def summarize(manifest, observations):
    return {
        "mode": "fixture",
        "purpose": "PIPELINE_VALIDATION_ONLY",
        "paid_api_calls": 0,
        "planned": len(manifest["selected"]),
        "started": len(observations),
        "completed": sum(o.unit_states == ["DONE"] for o in observations),
        "sends": sum(o.sends for o in observations),
        "send_limit": manifest["batch_max_sends"],
        "totals": {key: sum(o.totals[key] for o in observations) for key in TOTALS},
        "model_quality_score": None,
    }


def finish(directory, manifest):
    observations = batch_observations(directory)
    path = export(directory / "exports", observations.model_dump(), "observations")
    return {"observations": str(path), **summarize(manifest, observations.observations)}


def batch_observations(directory):
    directory = Path(directory)
    manifest = json.loads(read_bounded(directory / "manifest.json", 4 * 1024 * 1024))
    dataset = Dataset.model_validate(manifest["dataset"])
    # This is a read-only view; scoring never invokes resume or creates a provider.
    from urllib.parse import quote

    conn = sqlite3.connect(
        "file:" + quote(str((directory / "batch.sqlite").resolve())) + "?mode=ro", uri=True
    )
    try:
        bindings = dict(conn.execute("SELECT case_id,task_id FROM batch_cases"))
        if conn.execute("SELECT manifest_digest FROM batch_meta").fetchall() != [
            (digest(manifest),)
        ]:
            raise AgentError("EVALUATION_BATCH_MISMATCH")
        splits = {g.group_id: g.split for g in dataset.groups}
        batch_stop = None
        if manifest["mode"] == "baseline_live":
            stopped = conn.execute("SELECT reason FROM batch_stop WHERE id=1").fetchone()
            batch_stop = stopped[0] if stopped else None
        if set(bindings) - set(manifest["selected"]):
            raise AgentError("EVALUATION_TASK_BINDING_INVALID")
        result = []
        for case in dataset.cases:
            if case.case_id not in bindings:
                if (directory / "cases" / case.case_id).exists():
                    raise AgentError("EVALUATION_TASK_BINDING_INVALID")
                continue
            state = directory / "cases" / case.case_id / "state"
            task_id = bindings[case.case_id]
            if not task_id:
                candidates = sorted(state.glob("task_*"))
                if len(candidates) == 1:
                    task_id = candidates[0].name
                elif not candidates:
                    result.append(
                        Observation(
                            observation_id="setup-" + case.case_id,
                            case_id=case.case_id,
                            group_id=case.group_id,
                            split=splits[case.group_id],
                            input_digest=case.input_digest,
                            execution_mode=manifest["mode"],
                            started=True,
                            task_id=None,
                            unit_states=["PENDING"],
                            reasons=["CASE_SETUP_INCOMPLETE"],
                            findings=[],
                            tools=[],
                            retrieved_evidence=[],
                            consumed_evidence=[],
                            sends=0,
                            totals={key: 0 for key in TOTALS},
                            model_identity={},
                            pricing={},
                            source={"kind": "batch_case_started_without_task"},
                        )
                    )
                    continue
                else:
                    raise AgentError("EVALUATION_CASE_NOT_COLLECTABLE")
            observation = collect(app.read_task(task_id, state), case, splits[case.group_id])
            if batch_stop:
                observation = observation.model_copy(
                    update={
                        "source": {**observation.source, "batch_stop_reason": batch_stop},
                        "reasons": sorted(set(observation.reasons + [batch_stop]))
                        if any(s in ("PENDING", "RUNNING") for s in observation.unit_states)
                        else observation.reasons,
                    }
                )
            result.append(observation)
        return Observations(observations=result)
    finally:
        conn.close()


def read_batch(directory):
    directory = Path(directory)
    try:
        with FileLock(str(directory / "batch.lock"), timeout=0, mode=0o600):
            return batch_observations(directory)
    except Timeout:
        raise AgentError("EVALUATION_BATCH_LOCKED") from None
