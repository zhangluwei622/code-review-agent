import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import ROOT
from filelock import FileLock
from test_cli import cli

from review_agent import app
from review_agent.contracts import AgentError, digest
from review_agent.evaluation.batch import batch_observations, run_fixture
from review_agent.evaluation.data import (
    Observation,
    Observations,
    describe,
    load_dataset,
    read_model,
)
from review_agent.evaluation.scoring import (
    AnnotationApproval,
    evaluate,
    score,
    template,
)

DATASET = ROOT / "examples/eval/phase-5/dataset.json"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def subset(tmp_path, ids=None):
    root = tmp_path / "dataset"
    root.mkdir()
    for name in ("diffs", "fixtures"):
        shutil.copytree(DATASET.parent / name, root / name)
    value = json.loads(DATASET.read_text())
    if ids:
        value["cases"] = [c for c in value["cases"] if c["case_id"] in ids]
        groups = {c["group_id"] for c in value["cases"]}
        value["groups"] = [g for g in value["groups"] if g["group_id"] in groups]
    path = root / "dataset.json"
    save(path, value)
    return path, value


def set_fixture(path, value, case_index, fixture):
    target = path.parent / f"fixtures/custom-{case_index}.json"
    save(target, fixture)
    value["cases"][case_index]["fixture"] = str(target.relative_to(path.parent))
    value["cases"][case_index]["fixture_digest"] = digest(target.read_text())
    save(path, value)


def minimal_observation(case, *, state="DONE", started=True, mode="fixture", findings=None):
    return Observation(
        observation_id="observation-one",
        case_id=case.case_id,
        group_id=case.group_id,
        split="development",
        input_digest=case.input_digest,
        execution_mode=mode,
        started=started,
        task_id=None,
        unit_states=[state],
        reasons=[] if state == "DONE" else [state],
        findings=findings or [],
        tools=[],
        retrieved_evidence=[],
        consumed_evidence=[],
        sends=int(started),
        totals={"settled_tokens": 0, "settled_cost_nusd": 0, "held_tokens": 0, "held_cost_nusd": 0},
        model_identity={"actual_model_version": None},
        pricing={},
        source={},
    )


def test_dataset_split_and_pending_labels():
    result = describe(DATASET)
    assert result["groups"] == 6 and result["cases"] == 12
    assert result["splits"] == {"development": 8, "holdout": 4}
    assert result["annotation_status"] == "PENDING_USER_CONFIRMATION"
    assert not result["paid_calls_authorized"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("known_holdout", "INVALID_EVALUATION_DATA"),
        ("duplicate_id", "DUPLICATE_EVALUATION_ID"),
        ("cross_group_clone", "EVALUATION_SPLIT_LEAKAGE"),
        ("bad_evidence", "INVALID_EVALUATION_EVIDENCE"),
        ("outside_path", "EVALUATION_PATH_REJECTED"),
        ("changed_diff", "EVALUATION_INPUT_CHANGED"),
        ("changed_fixture", "EVALUATION_FIXTURE_CHANGED"),
        ("secret_label", "UNSAFE_REQUEST"),
    ],
)
def test_dataset_rejects_unsafe_or_inconsistent_labels(tmp_path, mutation, error):
    path, value = subset(tmp_path)
    first = value["cases"][0]
    if mutation == "known_holdout":
        value["groups"][0]["split"] = "holdout"
    elif mutation == "duplicate_id":
        value["cases"][1]["case_id"] = first["case_id"]
    elif mutation == "cross_group_clone":
        value["cases"][8].update({k: first[k] for k in ("diff", "input_digest")})
    elif mutation == "bad_evidence":
        first["issues"][0]["evidence"] = ["h9999:new:1"]
    elif mutation == "outside_path":
        first["diff"] = "../private.diff"
    elif mutation == "changed_diff":
        source = path.parent / first["diff"]
        source.write_text(source.read_text().replace("stats.py", "renamed.py"))
    elif mutation == "changed_fixture":
        target = path.parent / first["fixture"]
        target.write_text(target.read_text() + " ")
    else:
        first["rationale"] = "ghp_" + "SyntheticEvaluationOnly" * 3
    save(path, value)
    with pytest.raises(AgentError, match=error):
        load_dataset(path)


def test_dataset_symlink_is_not_followed(tmp_path):
    path, value = subset(tmp_path)
    target = path.parent / value["cases"][0]["diff"]
    target.unlink()
    target.symlink_to(ROOT / "examples/diffs/empty-list.diff")
    with pytest.raises(AgentError, match="EVALUATION_PATH_REJECTED"):
        load_dataset(path)


def test_all_fixture_cases_resume_without_reset_or_gold_leak(tmp_path, monkeypatch):
    path, value = subset(tmp_path)
    marker = "ANNOTATION_ONLY_MUST_NEVER_ENTER_PROVIDER_REQUEST"
    value["cases"][0]["rationale"] += marker
    save(path, value)

    def forbidden(*args, **kwargs):
        pytest.fail("offline evaluation tried to access an online provider or credential")

    monkeypatch.setattr(app, "_credential", forbidden)
    monkeypatch.setattr(app, "DeepSeekProvider", forbidden)
    calls = []
    batch = tmp_path / "batch"
    first = run_fixture(path, batch, split="all", observer=calls.append)
    assert first["started"] == 12 and first["sends"] == len(calls) == 14
    assert first["send_limit"] == 72 and first["model_quality_score"] is None
    assert first["paid_api_calls"] == 0
    assert first["totals"]["held_tokens"] == 600
    assert first["totals"]["held_cost_nusd"] == 800000
    assert marker not in json.dumps(calls)
    for database in batch.glob("cases/*/state/task_*/task.sqlite"):
        assert marker.encode() not in database.read_bytes()
    second = run_fixture(path, batch, observer=calls.append)
    assert second == first and len(calls) == 14
    observations = batch_observations(batch)
    contextual = next(
        o for o in observations.observations if o.case_id == "caller-contract-context"
    )
    assert "h0001:new:1" in contextual.retrieved_evidence
    assert "h0001:new:1" in contextual.consumed_evidence
    assert len(contextual.tools) == 2
    paused = next(o for o in observations.observations if o.case_id == "parse-error-unhandled")
    assert paused.sends == 0 and paused.unit_states == ["PAUSED_BUDGET"] and paused.started
    dataset, _, fingerprint = load_dataset(path)
    result = score(dataset, observations, template(fingerprint, observations))
    by_id = {row["case_id"]: row for row in result["cases"]}
    for cid in (
        "boundary-check-weakened",
        "parse-error-unhandled",
        "null-guard-removed",
        "return-statement-removed",
    ):
        assert len(by_id[cid]["missed_issues"]) == 1
    assert by_id["empty-guard-removed"]["pending_issues"]  # Semantic judgment is still pending.
    assert all(c["quality_metrics"] is None for c in result["cohorts"])
    for option in ({"max_tokens": 6000}, {"max_cost_nusd": 20000000}, {"split": "development"}):
        with pytest.raises(AgentError, match="EVALUATION_BATCH_MISMATCH"):
            run_fixture(path, batch, **option)


@pytest.mark.parametrize(
    "point,observed_sends,held",
    [
        ("after_case_started", 1, 0),
        ("after_case_task_created", 1, 0),
        ("after_case_bound", 1, 0),
        ("after_request", 1, 0),
        ("after_dispatched", 0, 600),
        ("after_completed", 1, 0),
        ("after_case_executed", 1, 0),
    ],
)
def test_batch_crash_reuses_task_and_ledgers(tmp_path, point, observed_sends, held):
    path, _ = subset(tmp_path, {"empty-guard-removed"})
    batch, calls = tmp_path / "batch", []

    def fault(event):
        if event == point:
            raise AgentError("TEST_CRASH")

    with pytest.raises(AgentError, match="TEST_CRASH"):
        run_fixture(path, batch, observer=calls.append, fault=fault)
    # Even a crash before task creation is represented as an already started case.
    before = batch_observations(batch)
    assert len(before.observations) == 1 and before.observations[0].started
    result = run_fixture(path, batch, observer=calls.append)
    again = run_fixture(path, batch, observer=calls.append)
    assert result == again and len(calls) == observed_sends
    assert result["sends"] == 1 and result["totals"]["held_tokens"] == held
    databases = list(batch.glob("cases/*/state/task_*/task.sqlite"))
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_missing_batch_ledger_cannot_reset_allocation(tmp_path):
    path, _ = subset(tmp_path, {"empty-guard-removed"})
    batch = tmp_path / "batch"
    run_fixture(path, batch)
    (batch / "batch.sqlite").unlink()
    with pytest.raises(AgentError, match="EVALUATION_BATCH_INCOMPLETE"):
        run_fixture(path, batch)
    assert len(list(batch.glob("cases/*/state/task_*"))) == 1


def test_later_case_overrun_stops_whole_batch_across_resume_and_keeps_held(tmp_path):
    path, value = subset(
        tmp_path, {"empty-guard-removed", "empty-contract-changed", "boundary-check-weakened"}
    )
    set_fixture(path, value, 0, {"default": {"error": "timeout"}})
    set_fixture(
        path,
        value,
        1,
        {
            "default": {
                "body": '{"action":"submit_review","findings":[]}',
                "usage": {"input_tokens": 650, "output_tokens": 50, "total_tokens": 700},
            }
        },
    )
    batch, calls = tmp_path / "batch", []
    first = run_fixture(path, batch, observer=calls.append)
    assert first["started"] == 2 and first["planned"] == 3
    assert first["totals"] == {
        "settled_tokens": 700,
        "settled_cost_nusd": 750000,
        "held_tokens": 600,
        "held_cost_nusd": 800000,
    }
    assert run_fixture(path, batch, observer=calls.append) == first
    assert len(calls) == 2


@pytest.mark.parametrize(
    "state",
    [
        "DONE",
        "ABSTAINED",
        "PARTIAL_TRUNCATED",
        "PAUSED_BUDGET",
        "PAUSED_UNKNOWN",
        "PARTIAL_INVALID_RESULT",
        "PENDING",
    ],
)
def test_started_defects_remain_in_e2e_denominator(state):
    dataset, _, fingerprint = load_dataset(DATASET)
    observations = Observations(observations=[minimal_observation(dataset.cases[0], state=state)])
    result = score(dataset, observations, template(fingerprint, observations))
    metric = result["cohorts"][0]["diagnostic_metrics"]["e2e_miss_rate"]
    assert metric == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert result["cases"][0]["unit_states"] == [state]


def test_unstarted_cases_are_not_silently_counted_as_completed():
    dataset, _, fingerprint = load_dataset(DATASET)
    observations = Observations(observations=[minimal_observation(dataset.cases[0], started=False)])
    result = score(dataset, observations, template(fingerprint, observations))
    cohort = result["cohorts"][0]
    assert cohort["started"] == cohort["completed"] == 0
    assert len(cohort["not_started_cases"]) == 8
    assert cohort["diagnostic_metrics"]["e2e_miss_rate"]["value"] is None


def test_explicit_matching_duplicate_and_reference_are_separate():
    dataset, _, fingerprint = load_dataset(DATASET)
    findings = [
        {"finding_id": fid, "confidence": level, "evidence": [], "expectation_evidence": []}
        for fid, level in (("one", "high"), ("two", "high"), ("three", "reference"))
    ]
    observations = Observations(
        observations=[
            minimal_observation(dataset.cases[0], state="PARTIAL_TRUNCATED", findings=findings)
        ]
    )
    judgments = template(fingerprint, observations)
    judgments = judgments.model_copy(
        update={
            "reviewer": "test-reviewer",
            "findings": [
                judgments.findings[0].model_copy(
                    update={
                        "decision": "match",
                        "issue_id": dataset.cases[0].issues[0].issue_id,
                        "rationale": "supported",
                    }
                ),
                judgments.findings[1].model_copy(
                    update={
                        "decision": "duplicate",
                        "duplicate_of": "one",
                        "rationale": "same issue",
                    }
                ),
                judgments.findings[2].model_copy(
                    update={"decision": "reference_supported", "rationale": "reference only"}
                ),
            ],
        }
    )
    approval = AnnotationApproval(
        dataset_digest=fingerprint,
        reviewer="test-reviewer",
        confirmed_at="test",
        decision="confirmed",
    )
    result = score(dataset, observations, judgments, approval)
    metric = result["cohorts"][0]["diagnostic_metrics"]
    assert metric["recall"]["value"] == 1 and metric["precision"]["value"] == 0.5
    assert result["cases"][0]["complete"] is False
    assert (
        result["cohorts"][0]["quality_metrics"] is None
    )  # A fixture can never become live quality.
    bad = judgments.model_copy(
        update={
            "findings": [
                judgments.findings[0],
                judgments.findings[1].model_copy(
                    update={
                        "decision": "match",
                        "duplicate_of": None,
                        "issue_id": dataset.cases[0].issues[0].issue_id,
                    }
                ),
                judgments.findings[2],
            ]
        }
    )
    with pytest.raises(AgentError, match="INVALID_EVALUATION_MATCH"):
        score(dataset, observations, bad)


def test_history_has_one_primary_miss_and_separate_low_output_experiment():
    dataset, _, fingerprint = load_dataset(DATASET)
    observations = read_model(
        ROOT / "tests/fixtures/evaluation/historical-observations.json", Observations
    )
    result = score(dataset, observations, template(fingerprint, observations))
    assert len(result["cohorts"]) == 1 and result["cohorts"][0]["started"] == 1
    assert result["cohorts"][0]["diagnostic_metrics"]["e2e_miss_rate"]["numerator"] == 1
    assert len(result["excluded_experiments"]) == 1
    assert result["excluded_experiments"][0]["source"]["finish_reason"] == "stop"
    assert result["cases"][0]["model_identity"]["actual_model_version"] is None
    assert result["cases"][0]["pricing"]["pricing_reviews"]
    with pytest.raises(AgentError, match="DUPLICATE_PRIMARY_OBSERVATION"):
        duplicate = observations.model_copy(
            update={
                "observations": [
                    observations.observations[0],
                    observations.observations[0].model_copy(update={"observation_id": "another"}),
                ]
            }
        )
        score(dataset, duplicate, template(fingerprint, duplicate))


def test_read_only_rescoring_and_judgment_fingerprints(tmp_path, monkeypatch):
    path, _ = subset(tmp_path, {"empty-guard-removed"})
    batch = tmp_path / "batch"
    run_fixture(path, batch)
    files = list(batch.rglob("*.sqlite"))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    def forbidden(*args, **kwargs):
        pytest.fail("scoring must not execute/resume a task")

    monkeypatch.setattr(app, "execute", forbidden)
    observations = batch_observations(batch)
    first = evaluate(path, observations, tmp_path / "scores")
    assert evaluate(path, observations, tmp_path / "scores") == first
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    judgments = json.loads(Path(first["judgments"]).read_text())
    judgments["dataset_digest"] = "0" * 64
    target = tmp_path / "stale.json"
    save(target, judgments)
    with pytest.raises(AgentError, match="EVALUATION_JUDGMENT_MISMATCH"):
        evaluate(path, observations, tmp_path / "scores", target)


def test_retrieved_context_is_not_consumed_when_followup_is_unknown(tmp_path):
    path, value = subset(tmp_path, {"caller-contract-context"})
    fixture = json.loads((path.parent / value["cases"][0]["fixture"]).read_text())
    fixture["responses"]["0:1:REVIEW:1"] = {"error": "timeout"}
    set_fixture(path, value, 0, fixture)
    batch = tmp_path / "batch"
    run_fixture(path, batch)
    observation = batch_observations(batch).observations[0]
    assert "h0001:new:1" in observation.retrieved_evidence
    assert observation.consumed_evidence == []
    assert observation.tools[0]["consumed_by_requests"] == []
    assert len(observation.source["post_tool_attempt_ids"]) == 1
    assert observation.source["post_tool_held_tokens"] == 600
    assert observation.source["post_tool_held_cost_nusd"] == 800000


def test_fixture_provenance_cannot_be_relabelled_as_live(tmp_path):
    dataset, _, _ = load_dataset(DATASET)
    observation = minimal_observation(dataset.cases[0]).model_dump()
    observation["execution_mode"] = "historical_live"
    observation["model_identity"] = {"requested_model": "fixture"}
    path = tmp_path / "fake-live.json"
    save(path, {"schema_version": 1, "observations": [observation]})
    with pytest.raises(AgentError, match="INVALID_EVALUATION_DATA"):
        read_model(path, Observations)


def test_mixed_model_identities_do_not_get_combined_quality_grade():
    dataset, _, fingerprint = load_dataset(DATASET)
    first = minimal_observation(dataset.cases[0], mode="historical_live")
    second = minimal_observation(dataset.cases[1], mode="historical_live").model_copy(
        update={
            "observation_id": "second",
            "model_identity": {"actual_model_version": "different-version"},
        }
    )
    observations = Observations(observations=[first, second])
    approval = AnnotationApproval(
        dataset_digest=fingerprint,
        reviewer="test-reviewer",
        confirmed_at="test",
        decision="confirmed",
    )
    result = score(dataset, observations, template(fingerprint, observations), approval)
    assert len(result["cohorts"][0]["baseline_identity_digests"]) == 2
    assert result["cohorts"][0]["quality_metrics"] is None


def test_cli_offline_entry_and_no_online_option(tmp_path):
    validated = cli("eval", "validate", "--dataset", DATASET, "--output-dir", tmp_path / "review")
    assert validated.returncode == 0, validated.stderr
    assert "待用户确认" in Path(json.loads(validated.stdout)["annotations"]).read_text()
    rejected = cli(
        "eval",
        "run-fixture",
        "--dataset",
        DATASET,
        "--batch-dir",
        tmp_path / "batch",
        "--provider",
        "deepseek",
    )
    assert rejected.returncode == 1 and "INVALID_ARGUMENTS" in rejected.stderr
    scored = cli(
        "eval",
        "score",
        "--dataset",
        DATASET,
        "--observations",
        ROOT / "tests/fixtures/evaluation/historical-observations.json",
        "--output-dir",
        tmp_path / "score",
    )
    assert scored.returncode == 0, scored.stderr
    assert "PENDING_USER_CONFIRMATION" in scored.stdout
    batch = tmp_path / "locked"
    batch.mkdir()
    with FileLock(str(batch / "batch.lock"), timeout=0):
        with pytest.raises(AgentError, match="EVALUATION_BATCH_LOCKED"):
            run_fixture(DATASET, batch)
