from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from review_agent.contracts import AgentError, StrictModel, digest
from review_agent.report import escape

from .artifacts import export, save_json, write_once
from .data import Fingerprint, Observations, load_dataset, read_model


class FindingJudgment(StrictModel):
    observation_id: str
    finding_id: str
    decision: Literal["pending", "match", "false_positive", "duplicate", "reference_supported"]
    issue_id: str | None = None
    duplicate_of: str | None = None
    rationale: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def shape(self):
        if (
            (self.decision == "match") != bool(self.issue_id)
            or (self.decision == "duplicate") != bool(self.duplicate_of)
            or (self.decision != "pending" and not self.rationale)
        ):
            raise ValueError("judgment needs explicit rationale and matching identity")
        return self


class ToolJudgment(StrictModel):
    observation_id: str
    tool_call_id: str
    decision: Literal["pending", "useful", "redundant", "inappropriate"] = "pending"
    rationale: str = Field(default="", max_length=4000)


class Judgments(StrictModel):
    schema_version: Literal[1] = 1
    dataset_digest: Fingerprint
    observations_digest: Fingerprint
    reviewer: str | None = None
    findings: list[FindingJudgment]
    tools: list[ToolJudgment]


class AnnotationApproval(StrictModel):
    dataset_digest: Fingerprint
    reviewer: str = Field(min_length=1, max_length=100)
    confirmed_at: str = Field(min_length=1, max_length=100)
    decision: Literal["confirmed"]


def template(dataset_digest, observations):
    return Judgments(
        dataset_digest=dataset_digest,
        observations_digest=digest(observations.model_dump()),
        findings=[
            FindingJudgment(
                observation_id=o.observation_id, finding_id=f["finding_id"], decision="pending"
            )
            for o in observations.observations
            for f in o.findings
        ],
        tools=[
            ToolJudgment(observation_id=o.observation_id, tool_call_id=t["tool_call_id"])
            for o in observations.observations
            for t in o.tools
        ],
    )


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if denominator == 0 else numerator / denominator,
    }


def score(dataset, observations, judgments, approval=None):
    fingerprint = digest(dataset.model_dump())
    if (
        judgments.dataset_digest != fingerprint
        or judgments.observations_digest != digest(observations.model_dump())
        or (approval and approval.dataset_digest != fingerprint)
    ):
        raise AgentError("EVALUATION_JUDGMENT_MISMATCH")
    if not judgments.reviewer and (
        any(j.decision != "pending" for j in judgments.findings)
        or any(j.decision != "pending" for j in judgments.tools)
    ):
        raise AgentError("EVALUATION_REVIEWER_REQUIRED")
    fj = {(j.observation_id, j.finding_id): j for j in judgments.findings}
    tj = {(j.observation_id, j.tool_call_id): j for j in judgments.tools}
    expected_fj = {
        (o.observation_id, f["finding_id"]) for o in observations.observations for f in o.findings
    }
    expected_tj = {
        (o.observation_id, t["tool_call_id"]) for o in observations.observations for t in o.tools
    }
    if (
        len(fj) != len(judgments.findings)
        or len(tj) != len(judgments.tools)
        or set(fj) != expected_fj
        or set(tj) != expected_tj
    ):
        raise AgentError("EVALUATION_JUDGMENT_IDENTITIES")
    cases = {c.case_id: c for c in dataset.cases}
    groups = {g.group_id: g.split for g in dataset.groups}
    seen_ids, primary = set(), set()
    rows, experiments = [], []
    for observation in observations.observations:
        o = observation
        case = cases.get(o.case_id)
        if (
            case is None
            or o.input_digest != case.input_digest
            or o.group_id != case.group_id
            or o.split != groups[case.group_id]
            or o.observation_id in seen_ids
            or len({f["finding_id"] for f in o.findings}) != len(o.findings)
            or len({t["tool_call_id"] for t in o.tools}) != len(o.tools)
        ):
            raise AgentError("EVALUATION_OBSERVATION_MISMATCH")
        seen_ids.add(o.observation_id)
        if o.execution_mode == "historical_experiment":
            experiments.append(o.model_dump())
            continue
        identity = (o.execution_mode, o.case_id)
        if identity in primary:
            raise AgentError("DUPLICATE_PRIMARY_OBSERVATION")
        primary.add(identity)
        gold = {i.issue_id for i in case.issues} if o.started else set()
        matched, reference_hits = set(), set()
        counts, confidence = Counter(), {}
        by_finding = {f["finding_id"]: f for f in o.findings}
        for finding in o.findings:
            j = fj[o.observation_id, finding["finding_id"]]
            level = finding["confidence"]
            if level not in ("high", "medium", "reference"):
                raise AgentError("INVALID_EVALUATION_CONFIDENCE")
            confidence.setdefault(level, Counter())[j.decision] += 1
            bucket = "reference" if level == "reference" else "formal"
            counts[bucket + "_" + j.decision] += 1
            if j.decision == "match":
                hits = reference_hits if bucket == "reference" else matched
                if j.issue_id not in gold or j.issue_id in hits:
                    raise AgentError("INVALID_EVALUATION_MATCH")
                hits.add(j.issue_id)
            if j.decision == "reference_supported" and bucket != "reference":
                raise AgentError("INVALID_EVALUATION_MATCH")
            if j.decision == "duplicate":
                target = by_finding.get(j.duplicate_of)
                if target is None or j.duplicate_of == j.finding_id:
                    raise AgentError("INVALID_EVALUATION_DUPLICATE")
                other = fj[o.observation_id, j.duplicate_of]
                if other.decision not in ("match", "false_positive", "reference_supported"):
                    raise AgentError("INVALID_EVALUATION_DUPLICATE")
        unmatched = gold - matched
        # Pending semantic judgments are not silently classified as false positives/misses.
        pending_issues = unmatched if counts["formal_pending"] else set()
        missed = unmatched - pending_issues
        tools = Counter(t["status"] for t in o.tools)
        tool_judgments = Counter(tj[o.observation_id, t["tool_call_id"]].decision for t in o.tools)
        if any(
            tj[o.observation_id, t["tool_call_id"]].decision != "pending"
            and not tj[o.observation_id, t["tool_call_id"]].rationale
            for t in o.tools
        ):
            raise AgentError("EVALUATION_RATIONALE_REQUIRED")
        required = set(case.tool_evidence)
        cited = {
            ref
            for f in o.findings
            for ref in f.get("evidence", []) + f.get("expectation_evidence", [])
        }
        rows.append(
            {
                "case_id": case.case_id,
                "group_id": case.group_id,
                "split": o.split,
                "execution_mode": o.execution_mode,
                "observation_id": o.observation_id,
                "started": o.started,
                "unit_states": o.unit_states,
                "reasons": o.reasons,
                "complete": o.started
                and bool(o.unit_states)
                and all(s == "DONE" for s in o.unit_states),
                "expected_issues": sorted(gold),
                "matched_issues": sorted(matched),
                "missed_issues": sorted(missed),
                "pending_issues": sorted(pending_issues),
                "reference_matched_issues": sorted(reference_hits),
                "finding_counts": dict(counts),
                "confidence": {k: dict(v) for k, v in confidence.items()},
                "tools": {
                    "requirement": case.tool_requirement,
                    "calls": len(o.tools),
                    "statuses": dict(tools),
                    "judgments": dict(tool_judgments),
                    "repeated_arguments": sum(t["repeated_arguments"] for t in o.tools),
                    "required_retrieved": bool(required) and required <= set(o.retrieved_evidence),
                    "required_consumed": bool(required) and required <= set(o.consumed_evidence),
                    # A zero-comment review has no citation-bearing finding. Its tool
                    # usefulness remains a separate human judgment, even when the
                    # evidence was present in a completed model request.
                    "required_cited": None if not required or not o.findings else required <= cited,
                    "post_tool_model_sends": len(o.source.get("post_tool_attempt_ids", [])),
                    "post_tool_settled_tokens": o.source.get("post_tool_settled_tokens", 0),
                    "post_tool_settled_cost_nusd": o.source.get("post_tool_settled_cost_nusd", 0),
                    "post_tool_held_tokens": o.source.get("post_tool_held_tokens", 0),
                    "post_tool_held_cost_nusd": o.source.get("post_tool_held_cost_nusd", 0),
                },
                "sends": o.sends,
                "totals": o.totals,
                "source": o.source,
                "model_identity": o.model_identity,
                "pricing": o.pricing,
            }
        )
    cohorts = []
    for mode in ("fixture", "historical_live", "baseline_live"):
        for split in ("development", "holdout"):
            subset = [r for r in rows if r["execution_mode"] == mode and r["split"] == split]
            if not subset:
                continue
            expected = sum(len(r["expected_issues"]) for r in subset)
            missed = sum(len(r["missed_issues"]) for r in subset)
            pending = sum(len(r["pending_issues"]) for r in subset)
            hits = sum(len(r["matched_issues"]) for r in subset)
            counts = Counter()
            reasons = Counter()
            confidence, tool_statuses, tool_judgments, miss_reasons = (
                {},
                Counter(),
                Counter(),
                Counter(),
            )
            identities = set()
            for row in subset:
                counts.update(row["finding_counts"])
                for level, values in row["confidence"].items():
                    confidence.setdefault(level, Counter()).update(values)
                tool_statuses.update(row["tools"]["statuses"])
                tool_judgments.update(row["tools"]["judgments"])
                for reason in row["reasons"] or row["unit_states"]:
                    miss_reasons[reason] += len(row["missed_issues"])
                identities.add(
                    digest(
                        {
                            "model": {
                                k: row["model_identity"].get(k)
                                for k in (
                                    "requested_model",
                                    "reported_models",
                                    "actual_model_version",
                                    "prompt_digest",
                                )
                            },
                            "pricing_snapshot": row["pricing"].get("pricing_snapshot"),
                            "pricing_source": row["pricing"].get("pricing_source"),
                        }
                    )
                )
                if not row["complete"]:
                    reasons.update(row["reasons"] or row["unit_states"])
            precision_denominator = (
                hits + counts["formal_false_positive"] + counts["formal_duplicate"]
            )
            diagnostics = {
                "expected_issues": expected,
                "matched_issues": hits,
                "e2e_miss_rate": None if pending else ratio(missed, expected),
                "e2e_miss_rate_bounds": {
                    "lower": ratio(missed, expected),
                    "upper": ratio(missed + pending, expected),
                },
                "confirmed_missed_issues": missed,
                "pending_issues": pending,
                "possible_missed_issues": missed + pending,
                "recall": None if pending else ratio(hits, expected),
                "precision": None
                if counts["formal_pending"]
                else ratio(hits, precision_denominator),
                "finding_counts": dict(counts),
                "confidence": {level: dict(values) for level, values in confidence.items()},
                "missed_by_reason": dict(miss_reasons),
            }
            required = [
                r for r in subset if r["started"] and r["tools"]["requirement"] == "required"
            ]
            cohorts.append(
                {
                    "execution_mode": mode,
                    "split": split,
                    "purpose": "PIPELINE_VALIDATION_ONLY"
                    if mode == "fixture"
                    else ("REAL_BASELINE" if mode == "baseline_live" else "HISTORICAL_OBSERVATION"),
                    "planned": sum(groups[c.group_id] == split for c in dataset.cases),
                    "started": sum(r["started"] for r in subset),
                    "completed": sum(r["complete"] for r in subset),
                    "not_started_cases": [
                        c.case_id
                        for c in dataset.cases
                        if groups[c.group_id] == split
                        and not any(r["case_id"] == c.case_id and r["started"] for r in subset)
                    ],
                    "incomplete_reasons": dict(reasons),
                    "diagnostic_metrics": diagnostics,
                    "baseline_identity_digests": sorted(identities),
                    "quality_metrics": diagnostics
                    if mode != "fixture"
                    and approval
                    and not counts["formal_pending"]
                    and len(identities) == 1
                    else None,
                    "tool_metrics": {
                        "calls": sum(r["tools"]["calls"] for r in subset),
                        "statuses": dict(tool_statuses),
                        "judgments": dict(tool_judgments),
                        "required_retrieval": ratio(
                            sum(r["tools"]["required_retrieved"] for r in required), len(required)
                        ),
                        "required_consumption": ratio(
                            sum(r["tools"]["required_consumed"] for r in required), len(required)
                        ),
                        "post_tool_model_sends": sum(
                            r["tools"]["post_tool_model_sends"] for r in subset
                        ),
                    },
                    "sends": sum(r["sends"] for r in subset),
                    "totals": {
                        k: sum(r["totals"][k] for r in subset)
                        for k in (
                            "settled_tokens",
                            "settled_cost_nusd",
                            "held_tokens",
                            "held_cost_nusd",
                        )
                    },
                }
            )
    return {
        "schema_version": 2,
        "dataset_digest": fingerprint,
        "observations_digest": digest(observations.model_dump()),
        "judgments_digest": digest(judgments.model_dump()),
        "annotation_status": "CONFIRMED" if approval else "PENDING_USER_CONFIRMATION",
        "annotation_approval": None if approval is None else approval.model_dump(),
        "paid_calls_authorized": False,
        "cohorts": cohorts,
        "cases": rows,
        "excluded_experiments": experiments,
    }


def markdown(result):
    lines = [
        "# 离线评估报告",
        "",
        f"标注状态：{result['annotation_status']}",
        "",
        "fixture 仅验证评估流程，不是模型质量成绩。待判定评论不自动归为正确或误报。",
        "",
    ]
    for cohort in result["cohorts"]:
        metric = cohort["diagnostic_metrics"]
        lines.extend(
            [
                f"## {cohort['execution_mode']} / {cohort['split']}",
                "",
                f"用途：{cohort['purpose']}；计划 {cohort['planned']}，"
                f"已启动 {cohort['started']}，完整完成 {cohort['completed']}。",
                f"按标注草案预览：已确定未命中 {metric['confirmed_missed_issues']}，"
                f"待判定问题 {metric['pending_issues']}。",
                f"发送 {cohort['sends']} 次；账本汇总：{escape(str(cohort['totals']))}。",
                "",
            ]
        )
    lines.extend(
        [
            "## 逐例记录",
            "",
            "| 样例 | 状态 | 漏检问题 | 待判定问题 | 未完成原因 | "
            "工具调用／所需证据已消费 | 评论引用／工具人工判定 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in result["cases"]:
        cited = row["tools"]["required_cited"]
        tool_judgments = ", ".join(
            f"{key}: {value}" for key, value in sorted(row["tools"]["judgments"].items())
        )
        values = [
            row["case_id"],
            ", ".join(row["unit_states"]),
            ", ".join(row["missed_issues"]),
            ", ".join(row["pending_issues"]),
            ", ".join(row["reasons"]),
            f"{row['tools']['calls']} / {row['tools']['required_consumed']}",
            f"{'不适用' if cited is None else cited} / {tool_judgments or '无工具判定'}",
        ]
        lines.append("| " + " | ".join(escape(str(v)).replace("\n", " ") for v in values) + " |")
    lines.extend(
        [
            "",
            f"另列历史实验 {len(result['excluded_experiments'])} 项，不重复计入主评估分母。",
            "",
            "零评论或无目标证据时，评论引用记为不适用；不会因无 finding 而判定工具失败。",
            "零评论的工具价值需人工核对取回证据、进入已完成请求的关联及零发现结论是否得到支持，"
            "在工具 judgment 中记录理由；未判定保持 pending，仅进入请求不自动算 useful。",
            "",
            "实际模型版本未获证据确认时保持为空；模型别名和价格快照中的声明不冒充实际版本。",
            "工具后续调用开销按唯一 attempt 统计，不能据此推断工具的因果收益。",
            "",
        ]
    )
    return "\n".join(lines)


def evaluate(dataset_path, observations, output_dir, judgments_path=None, approval_path=None):
    dataset, _, fingerprint = load_dataset(dataset_path)
    if not isinstance(observations, Observations):
        observations = read_model(observations, Observations)
    judgments = (
        read_model(judgments_path, Judgments)
        if judgments_path
        else template(fingerprint, observations)
    )
    approval = read_model(approval_path, AnnotationApproval) if approval_path else None
    result = score(dataset, observations, judgments, approval)
    directory = Path(output_dir) / digest(result)
    path = export(directory, result, "evaluation")
    save_json(directory / "judgments.json", judgments.model_dump())
    write_once(directory / "report.md", markdown(result))
    return {
        "evaluation": str(path),
        "report": str(directory / "report.md"),
        "judgments": str(directory / "judgments.json"),
        "annotation_status": result["annotation_status"],
    }
