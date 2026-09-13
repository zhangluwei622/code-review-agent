"""No sockets: real-entry tests use a synthetic key and httpx.MockTransport only."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import ROOT
from test_cli import cli

from review_agent import app
from review_agent.contracts import AgentError, digest
from review_agent.evaluation import live
from review_agent.evaluation.batch import batch_observations
from review_agent.evaluation.data import load_dataset
from review_agent.evaluation.scoring import evaluate
from review_agent.providers import DeepSeekProvider
from review_agent.safety import Safety

DATASET = ROOT / "examples/eval/phase-5/dataset-v2.json"
KEY = "synthetic-live-entry-test-key"
ZERO = {"action": "submit_review", "findings": []}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(live, "_now", lambda: datetime(2026, 9, 11, 3, tzinfo=UTC))


def plan(tmp_path, *, full=False):
    root = tmp_path / "dataset"
    shutil.copytree(DATASET.parent / "diffs", root / "diffs")
    shutil.copytree(DATASET.parent / "fixtures", root / "fixtures")
    data = json.loads(DATASET.read_text())
    if not full:
        data["cases"] = [
            c
            for c in data["cases"]
            if c["case_id"] in ("empty-guard-removed", "aggregation-overwritten")
        ]
        groups = {c["group_id"] for c in data["cases"]}
        data["groups"] = [g for g in data["groups"] if g["group_id"] in groups]
    data["cases"][0]["rationale"] += " GOLD_EVALUATOR_ONLY_MARKER"
    path = root / "dataset.json"
    save(path, data)
    _, _, fingerprint = load_dataset(path)
    annotation = tmp_path / "labels.json"
    save(
        annotation,
        {
            "dataset_digest": fingerprint,
            "reviewer": "synthetic test reviewer",
            "confirmed_at": "2026-09-11T03:00:00Z",
            "decision": "confirmed",
        },
    )
    result = live.prepare_live(path, annotation, tmp_path / "plan", tmp_path / "batch")
    manifest = Path(result["manifest"])
    approval = tmp_path / "mock-run-approved.json"
    save(
        approval,
        {
            "manifest_digest": result["manifest_digest"],
            "batch_directory": str(tmp_path / "batch"),
            "approved_by": "mock-only test",
            "approved_at": "2026-09-11T03:00:00Z",
            "decision": "approved",
        },
    )
    return manifest, approval


def envelope(body=ZERO, *, model="DeepSeek-V4.1-Flash", usage=True, tokens=100):
    value = {
        "id": "mock-live-request",
        "model": model,
        "system_fingerprint": "mock-fingerprint",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(body) if isinstance(body, dict) else body,
                },
            }
        ],
    }
    if usage:
        value["usage"] = {
            "prompt_tokens": tokens,
            "completion_tokens": 20,
            "total_tokens": tokens + 20,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": tokens,
        }
    return value


def transport(monkeypatch, replies=None):
    calls = []
    replies = list(replies or [envelope()])

    def handler(request):
        calls.append(json.loads(request.content))
        value = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, json=value)

    monkeypatch.setattr(app, "_credential", lambda _: KEY)
    monkeypatch.setattr(
        app,
        "_provider",
        lambda config, **kwargs: (
            DeepSeekProvider(config, KEY, transport=httpx.MockTransport(handler)),
            Safety((KEY,)),
        ),
    )
    return calls


def snapshot(tmp_path, case="empty-guard-removed"):
    state = tmp_path / "batch" / "cases" / case / "state"
    task_id = next(state.glob("task_*")).name
    return task_id, state, app.read_task(task_id, state)


def test_prepare_is_offline_idempotent_and_freezes_full_allocation(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("credential/provider must not be read in prepare")

    monkeypatch.setattr(app, "_credential", forbidden)
    monkeypatch.setattr(app, "_provider", forbidden)
    path, _ = plan(tmp_path, full=True)
    manifest = live.read_manifest(path)
    again = live.prepare_live(
        manifest["dataset_path"],
        manifest["annotation_path"],
        path.parent,
        manifest["batch_directory"],
    )
    assert again["manifest_digest"] == digest(manifest)
    assert manifest["batch_max_sends"] == 72
    assert manifest["batch_max_tokens"] == 1_200_000
    assert manifest["batch_max_cost_nusd"] == 600_000_000
    assert manifest["case_max_tokens"] == 100_000
    assert manifest["case_max_cost_nusd"] == 50_000_000
    assert len(manifest["stage_order"]["development"]) == 8
    assert len(manifest["stage_order"]["holdout"]) == 4
    assert manifest["pricing_snapshot"]["model_version"] == "DeepSeek-V4.1-Flash"
    assert manifest["pricing_snapshot"]["effective_at"] is None
    assert not manifest["paid_calls_authorized"]
    assert not (tmp_path / "batch").exists()


@pytest.mark.parametrize("kind", ["missing", "pending", "wrong_digest", "wrong_directory"])
def test_approval_fails_before_credentials_and_task_creation(tmp_path, monkeypatch, kind):
    path, approval = plan(tmp_path)
    if kind == "missing":
        approval = None
    elif kind == "pending":
        approval = path.parent / "approval.pending.json"
    else:
        value = json.loads(approval.read_text())
        value["manifest_digest" if kind == "wrong_digest" else "batch_directory"] = "0" * 64
        save(approval, value)

    def forbidden(*a, **kw):
        raise AssertionError("authorization must precede credentials")

    monkeypatch.setattr(app, "_credential", forbidden)
    with pytest.raises(AgentError):
        live.run_live(path, approval)
    assert not (tmp_path / "batch").exists()


@pytest.mark.parametrize("field", ["batch_max_sends", "max_output_tokens", "model"])
def test_manifest_edits_cannot_change_approved_execution(tmp_path, field):
    path, approval = plan(tmp_path)
    value = json.loads(path.read_text())
    value[field] = "changed"
    save(path, value)
    with pytest.raises(AgentError, match="BASELINE_MANIFEST_CHANGED"):
        live.run_live(path, approval)


def test_development_then_holdout_resume_and_direct_resume_gate(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    calls = transport(monkeypatch)
    with pytest.raises(AgentError, match="DEVELOPMENT_STAGE_REQUIRED"):
        live.run_live(path, approval, stage="holdout")
    assert not calls
    first = live.run_live(path, approval)
    assert first["sends"] == first["started"] == 1
    assert first["stop_reason"] is None
    assert live.run_live(path)["totals"] == first["totals"]
    task_id, state, _ = snapshot(tmp_path)
    with pytest.raises(AgentError, match="EVALUATION_BATCH_ENTRY_REQUIRED"):
        app.execute(task_id, state)
    with pytest.raises(AgentError, match="EVALUATION_BATCH_ENTRY_REQUIRED"):
        app.execute(task_id, state, retry_unknown="unauthorized", batch_guard=lambda *a: None)
    second = live.run_live(path, stage="holdout")
    assert second["sends"] == 2 and len(calls) == 2
    assert live.run_live(path, stage="holdout")["totals"] == second["totals"]
    assert len(calls) == 2
    observed = batch_observations(tmp_path / "batch").observations
    assert {o.execution_mode for o in observed} == {"baseline_live"}
    assert all(o.model_identity["actual_model_version"] == "DeepSeek-V4.1-Flash" for o in observed)
    assert all("GOLD_EVALUATOR_ONLY_MARKER" not in json.dumps(c) for c in calls)
    assert all(c["max_tokens"] == 1024 for c in calls)
    assert list((state.parent / "exports").glob("trace-*.json"))
    assert list((state.parent / "exports").glob("report-*.md"))


def test_tool_followup_and_bound_repair_are_charged_once(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    calls = transport(
        monkeypatch,
        [
            envelope(
                {"action": "request_tool", "name": "read_hunk", "arguments": {"hunk_id": "h0001"}}
            ),
            envelope("invalid JSON"),
            envelope(),
        ],
    )
    result = live.run_live(path, approval)
    assert result["sends"] == 3 and result["totals"]["settled_tokens"] == 360
    _, _, data = snapshot(tmp_path)
    assert len(data["tool_calls"]) == 1
    assert len(data["operations"]) == 3
    assert sum(o["kind"] == "REPAIR" for o in data["operation_contexts"]) == 1
    observation = batch_observations(tmp_path / "batch").observations[0]
    assert observation.tools[0]["consumed_by_requests"]
    assert observation.source["post_tool_settled_tokens"] == 240
    assert live.run_live(path)["totals"] == result["totals"]
    assert len(calls) == 3


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("timeout", "PROVIDER_UNKNOWN"),
        ("usage", "USAGE_UNRESOLVED"),
        ("overrun", "BUDGET_BOUND_VIOLATION"),
        ("identity", "MODEL_IDENTITY_UNCONFIRMED"),
        ("missing_identity", "MODEL_IDENTITY_UNCONFIRMED"),
    ],
)
def test_batch_stops_across_stages_and_preserves_held_and_cost(
    tmp_path, monkeypatch, failure, reason
):
    path, approval = plan(tmp_path)
    value = {
        "timeout": httpx.ReadTimeout("synthetic timeout"),
        "usage": envelope(usage=False),
        "overrun": envelope(tokens=200_000),
        "identity": envelope(model="unapproved-version"),
        "missing_identity": envelope(model=None),
    }[failure]
    calls = transport(monkeypatch, [value])
    first = live.run_live(path, approval)
    assert first["sends"] == 1 and first["started"] == 1
    assert first["stop_reason"] == reason
    observation = batch_observations(tmp_path / "batch").observations[0]
    assert observation.source["batch_stop_reason"] == reason
    assert reason in observation.reasons
    if failure in ("timeout", "usage"):
        assert first["totals"]["held_tokens"] > 0
        assert first["totals"]["held_cost_nusd"] > 0
    else:
        assert first["totals"]["settled_tokens"] > 0
    for stage in ("development", "holdout"):
        again = live.run_live(path, stage=stage)
        assert again["totals"] == first["totals"]
        assert again["sends"] == 1 and again["stop_reason"] == reason
    assert len(calls) == 1
    conn = sqlite3.connect(tmp_path / "batch/batch.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="EVALUATION_STOP_IMMUTABLE"):
        conn.execute("DELETE FROM batch_stop")
    conn.close()


@pytest.mark.parametrize(
    "point",
    [
        "after_case_started",
        "after_case_task_created",
        "after_case_bound",
        "after_request",
        "after_reserved",
        "after_dispatched",
        "after_reply",
        "after_completed",
        "after_case_executed",
    ],
)
def test_restart_windows_reuse_binding_reservation_and_completed_calls(
    tmp_path, monkeypatch, point
):
    path, approval = plan(tmp_path)
    calls = transport(monkeypatch)

    def crash(name):
        if name == point:
            raise AgentError("SYNTHETIC_PROCESS_CRASH")

    with pytest.raises(AgentError, match="SYNTHETIC_PROCESS_CRASH"):
        live.run_live(path, approval, fault=crash)
    before = batch_observations(tmp_path / "batch").observations[0]
    result = live.run_live(path)
    assert result["sends"] == 1
    if point in ("after_dispatched", "after_reply"):
        assert result["stop_reason"] == "PROVIDER_UNKNOWN"
        assert result["totals"] == before.totals
        assert result["totals"]["held_tokens"] > 0
        assert len(calls) == (point == "after_reply")
    else:
        assert result["stop_reason"] is None
        assert len(calls) == 1
        assert result["totals"]["settled_tokens"] == 120
    assert len(list((tmp_path / "batch/cases/empty-guard-removed/state").glob("task_*"))) == 1


def test_missing_batch_database_cannot_reset_allocations(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    calls = transport(monkeypatch)
    live.run_live(path, approval)
    (tmp_path / "batch/batch.sqlite").unlink()
    with pytest.raises(AgentError, match="EVALUATION_BATCH_INCOMPLETE"):
        live.run_live(path)
    assert len(calls) == 1


def test_expired_evidence_stops_before_reading_credentials(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    calls = transport(monkeypatch)
    monkeypatch.setattr(live, "_now", lambda: datetime(2026, 9, 13, tzinfo=UTC))
    result = live.run_live(path, approval)
    assert result["stop_reason"] == "PRICE_EVIDENCE_EXPIRED"
    assert result["sends"] == result["started"] == 0
    assert not calls


def test_alias_identity_is_not_claimed_as_observed_version(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    transport(monkeypatch, [envelope(model="deepseek-flash")])
    live.run_live(path, approval)
    observation = batch_observations(tmp_path / "batch").observations[0]
    assert observation.model_identity["actual_model_version"] is None
    assert observation.model_identity["documented_model_version"] == "DeepSeek-V4.1-Flash"
    assert observation.model_identity["version_basis"] == "official_mapping_only"
    result = evaluate(
        tmp_path / "dataset/dataset.json",
        batch_observations(tmp_path / "batch"),
        tmp_path / "scores",
        approval_path=tmp_path / "labels.json",
    )
    assert result


def test_six_sends_include_four_tools_and_repair_in_each_stage(tmp_path, monkeypatch):
    path, approval = plan(tmp_path)
    tool = envelope(
        {"action": "request_tool", "name": "read_hunk", "arguments": {"hunk_id": "h0001"}}
    )
    calls = transport(monkeypatch, ([tool] * 4 + [envelope("invalid JSON"), envelope()]) * 2)
    first = live.run_live(path, approval)
    assert first["sends"] == 6 and not first["stop_reason"]
    second = live.run_live(path, stage="holdout")
    assert second["sends"] == second["send_limit"] == len(calls) == 12
    assert second["totals"]["settled_tokens"] == 1440
    assert live.run_live(path, stage="holdout")["totals"] == second["totals"]
    assert len(calls) == 12


def test_case_budget_pause_is_a_started_miss_and_stops_next_case(tmp_path, monkeypatch):
    from review_agent.contracts import BudgetQuote

    path, approval = plan(tmp_path)
    calls = transport(monkeypatch)
    monkeypatch.setattr(
        DeepSeekProvider,
        "quote",
        lambda *a: BudgetQuote(
            input_tokens=100_000,
            output_tokens=1024,
            tokens=101_024,
            cost_nusd=31_228_800,
        ),
    )
    result = live.run_live(path, approval)
    assert result["stop_reason"] == "CASE_BUDGET_EXHAUSTED"
    assert result["started"] == 1 and result["sends"] == 0
    observations = batch_observations(tmp_path / "batch")
    scored = evaluate(
        tmp_path / "dataset/dataset.json",
        observations,
        tmp_path / "scores",
        approval_path=tmp_path / "labels.json",
    )
    data = json.loads(Path(scored["evaluation"]).read_text())
    assert data["cases"][0]["missed_issues"]
    assert not data["cases"][0]["complete"]
    assert live.run_live(path, stage="holdout")["started"] == 1
    assert not calls


def test_cli_pending_manifest_has_no_live_approval(tmp_path):
    path, _ = plan(tmp_path)
    result = cli("eval", "run-live", "--manifest", path)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "BASELINE_APPROVAL_REQUIRED"
    assert not (tmp_path / "batch").exists()
