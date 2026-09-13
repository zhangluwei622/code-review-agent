import json
import shutil
from pathlib import Path

import pytest
from conftest import ROOT
from test_evaluation import DATASET, save, set_fixture

from review_agent.evaluation.batch import batch_observations, run_fixture
from review_agent.evaluation.data import Observations, describe, load_dataset, read_model
from review_agent.evaluation.entry import annotation_report
from review_agent.evaluation.scoring import markdown, score, template

REVISED = DATASET.parent / "dataset-v2.json"
ORIGINAL_DIGEST = "c6d4d9edca5e9c80ca45525a48f20c0c36312fc17cb3e92efa4d288f71175f99"


def test_revised_groups_and_annotation_export_preserve_original(tmp_path):
    original, _, original_digest = load_dataset(DATASET)
    revised, _, revised_digest = load_dataset(REVISED)
    assert original_digest == ORIGINAL_DIGEST and revised_digest != original_digest
    assert describe(REVISED)["splits"] == {"development": 8, "holdout": 4}
    assert {g.group_id for g in revised.groups if g.split == "holdout"} == {
        "aggregation_state",
        "return_contract",
    }
    assert {c.case_id for c in revised.cases} - {c.case_id for c in original.cases} == {
        "aggregation-overwritten",
        "aggregation-equivalent",
    }
    for before, after in zip(original.cases, revised.cases, strict=True):
        if before.case_id in (
            "caller-contract-context",
            "performance-context-missing",
            "null-guard-removed",
            "null-guard-equivalent",
        ):
            continue
        assert before == after
    old_path = Path(annotation_report(DATASET, tmp_path))
    old_content = old_path.read_text()
    new_path = Path(annotation_report(REVISED, tmp_path))
    assert new_path != old_path and old_path.read_text() == old_content
    assert (
        old_path.read_text()
        == (ROOT / "tests/fixtures/evaluation/original-annotations.md").read_text()
    )
    assert "aggregation_state" in new_path.read_text()
    assert revised.annotation_status == "PENDING_USER_CONFIRMATION"


def test_zero_comment_tool_value_is_judged_separately_from_citations():
    dataset, _, fingerprint = load_dataset(REVISED)
    archived = read_model(
        ROOT / "tests/fixtures/evaluation/fixture-observations.json", Observations
    )
    observed = next(o for o in archived.observations if o.case_id == "caller-contract-context")
    assert not observed.findings and observed.unit_states == ["DONE"]
    observations = Observations(observations=[observed])
    judgments = template(fingerprint, observations)
    pending = score(dataset, observations, judgments)
    tools = pending["cases"][0]["tools"]
    assert tools["required_retrieved"] and tools["required_consumed"]
    assert tools["required_cited"] is None
    assert tools["judgments"] == {"pending": 2}  # No automatic usefulness credit.
    assert judgments.findings == [] and len(judgments.tools) == 2
    decisions = []
    for judgment, call in zip(judgments.tools, observed.tools, strict=True):
        assert call["consumed_by_requests"]
        if call["name"] == "read_hunk":
            decision = "useful"
            rationale = (
                "工具结果的 h0001:new:1 给出非空契约，已进入已完成请求 "
                + call["consumed_by_requests"][-1]
                + "；该契约排除合法调用的空输入，支持零发现，不要求生成评论。"
            )
        else:
            decision = "redundant"
            rationale = "同一契约已由 read_hunk 取回，重复搜索未提供额外信息。"
        decisions.append(judgment.model_copy(update={"decision": decision, "rationale": rationale}))
    judgments = judgments.model_copy(update={"reviewer": "fixture-test-only", "tools": decisions})
    result = score(dataset, observations, judgments)
    assert result["cases"][0]["tools"]["judgments"] == {"useful": 1, "redundant": 1}
    assert result["cases"][0]["tools"]["required_cited"] is None
    assert result["schema_version"] == 2
    assert result["cohorts"][0]["quality_metrics"] is None
    assert "不适用 / redundant: 1, useful: 1" in markdown(result)


@pytest.mark.parametrize("outcome", ["zero_findings", "abstain", "reference"])
def test_performance_case_accepts_evidence_based_outcome_variants(tmp_path, monkeypatch, outcome):
    from review_agent import app

    def forbidden(*args, **kwargs):
        pytest.fail("annotation revision must not create an online provider or read credentials")

    monkeypatch.setattr(app, "_credential", forbidden)
    monkeypatch.setattr(app, "DeepSeekProvider", forbidden)
    root = tmp_path / "data"
    root.mkdir()
    for folder in ("diffs", "fixtures"):
        shutil.copytree(DATASET.parent / folder, root / folder)
    value = json.loads(REVISED.read_text())
    value["cases"] = [c for c in value["cases"] if c["case_id"] == "performance-context-missing"]
    value["groups"] = [g for g in value["groups"] if g["group_id"] == "context_evidence"]
    path = root / "dataset.json"
    save(path, value)
    body = {"action": "submit_review", "findings": []}
    if outcome == "abstain":
        body.update(action="abstain", reason="Diff 未提供输入规模、实测结果或性能目标。")
    elif outcome == "reference":
        existing = json.loads((ROOT / "examples/eval/phase-5/fixtures/success.json").read_text())
        finding = json.loads(existing["responses"]["0:0:REVIEW:1"]["body"])["findings"][0]
        finding.update(
            title="建议核对成对比较的输入规模",
            side="new",
            line=3,
            trigger="输入规模增长时。",
            actual_behavior="新增双层循环，对每对元素执行比较，比较次数随规模二次增长。",
            expected_behavior="需要规模和性能目标才能判断可接受的执行成本。",
            introduced_by="本次新增遍历同一输入的两层循环。",
            expectation_evidence=[],
            evidence=["h0001:new:3", "h0001:new:4", "h0001:new:5"],
            impact="当前没有足够信息确认实际性能影响。",
            suggestion="补充调用规模及测量数据，再判断是否需要改进算法。",
            severity="low",
            confidence="reference",
        )
        body["findings"] = [finding]
    set_fixture(
        path,
        value,
        0,
        {
            "default": {
                "body": json.dumps(body, ensure_ascii=False),
                "usage": {"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
            }
        },
    )
    batch = tmp_path / "batch"
    run_fixture(path, batch)
    dataset, _, fingerprint = load_dataset(path)
    observations = batch_observations(batch)
    judgments = template(fingerprint, observations)
    if outcome == "reference":
        assert len(judgments.findings) == 1
        judgment = judgments.findings[0].model_copy(
            update={
                "decision": "reference_supported",
                "rationale": "循环证据支持复杂度观察，明确保留实际性能判断。",
            }
        )
        judgments = judgments.model_copy(
            update={"reviewer": "fixture-test-only", "findings": [judgment]}
        )
    result = score(dataset, observations, judgments)
    row = result["cases"][0]
    assert row["unit_states"] == ["ABSTAINED" if outcome == "abstain" else "DONE"]
    assert row["expected_issues"] == row["missed_issues"] == row["pending_issues"] == []
    assert row["tools"]["calls"] == 0
    assert row["finding_counts"].get("formal_false_positive", 0) == 0
    if outcome == "reference":
        assert row["finding_counts"] == {"reference_reference_supported": 1}
    assert result["cohorts"][0]["quality_metrics"] is None
