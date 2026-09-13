from pathlib import Path

from review_agent.config import usd_to_nusd
from review_agent.report import escape

from .artifacts import save_json, write_once
from .batch import read_batch, run_fixture
from .data import describe, load_dataset
from .scoring import evaluate


def configure(subparsers):
    root = subparsers.add_parser("eval", help="离线评估与需清单审批的真实基线")
    commands = root.add_subparsers(dest="eval_command", required=True)
    for name in ("validate", "run-fixture", "score"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, required=True)
        if name == "run-fixture":
            command.add_argument("--batch-dir", type=Path, required=True)
            command.add_argument("--split", choices=("development", "holdout", "all"))
            command.add_argument("--case-max-tokens", type=int)
            command.add_argument("--case-max-cost-usd")
        else:
            command.add_argument("--output-dir", type=Path, required=name == "score")
        if name == "score":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--observations", type=Path)
            source.add_argument("--batch-dir", type=Path)
            command.add_argument("--judgments", type=Path)
            command.add_argument("--annotation-approval", type=Path)
    prepare = commands.add_parser("prepare-live", help="离线生成待审批清单，不读取凭证或调用模型")
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--annotation-approval", type=Path, required=True)
    prepare.add_argument("--plan-dir", type=Path, required=True)
    prepare.add_argument("--batch-dir", type=Path, required=True)
    live = commands.add_parser("run-live", help="仅执行已明确批准的冻结清单")
    live.add_argument("--manifest", type=Path, required=True)
    live.add_argument("--approval", type=Path)
    live.add_argument("--stage", choices=("development", "holdout"), default="development")


def annotation_report(path, output):
    dataset, bundles, fingerprint = load_dataset(path)
    directory = Path(output) / fingerprint
    lines = [
        "# 第五阶段样例标注：待用户确认",
        "",
        f"Dataset digest：`{fingerprint}`",
        "",
        "以下均为建议标注，尚未由用户确认。本文件不授权真实模型调用。",
        "同一场景组的所有变体固定在同一集合；留出集不用于调整判定规则。",
        "确认时请指出需修改的 case_id、分类、预期问题、禁止误报项或工具要求。",
        "",
    ]
    for group in dataset.groups:
        lines.extend([f"## {group.group_id} / {group.split}", "", escape(group.rationale), ""])
        for case in dataset.cases:
            if case.group_id != group.group_id:
                continue
            lines.extend(
                [
                    f"### {case.case_id}：{escape(case.title)}",
                    "",
                    f"建议分类：{case.classification}；状态：待确认。",
                    "",
                    escape(case.rationale),
                    "",
                    f"工具要求：{case.tool_requirement}。{escape(case.tool_rationale)}",
                    f"工具目标证据：{', '.join(case.tool_evidence) or '无'}。",
                    "",
                ]
            )
            for issue in case.issues:
                lines.extend(
                    [
                        f"预期问题 `{issue.issue_id}`：",
                        "",
                        f"- 触发：{escape(issue.trigger)}",
                        f"- 实际／预期：{escape(issue.actual)} / {escape(issue.expected)}",
                        f"- 变更因果：{escape(issue.causality)}",
                        f"- 证据：{', '.join(issue.evidence)}；置信度：{issue.confidence}。",
                        "",
                    ]
                )
            for item in case.must_not_report:
                lines.append(f"- 禁止误报：{escape(item)}")
            text = bundles[case.case_id]["safe_diff"]
            fence = "`" * (
                max((len(word) for word in text.split() if set(word) == {"`"}), default=2) + 1
            )
            lines.extend(["", fence + "diff", text.rstrip(), fence, ""])
    write_once(directory / "annotations.md", "\n".join(lines))
    save_json(
        directory / "confirmation.pending.json",
        {
            "dataset_digest": fingerprint,
            "decision": "pending",
            "reviewer": None,
            "confirmed_at": None,
            "paid_calls_authorized": False,
        },
    )
    return str(directory / "annotations.md")


def dispatch(args):
    if args.eval_command in ("prepare-live", "run-live"):
        from .live import prepare_live, run_live

        if args.eval_command == "prepare-live":
            return prepare_live(
                args.dataset, args.annotation_approval, args.plan_dir, args.batch_dir
            )
        return run_live(args.manifest, args.approval, stage=args.stage)
    if args.eval_command == "validate":
        result = describe(args.dataset)
        if args.output_dir:
            result["annotations"] = annotation_report(args.dataset, args.output_dir)
        return result
    if args.eval_command == "run-fixture":
        return run_fixture(
            args.dataset,
            args.batch_dir,
            split=args.split,
            max_tokens=args.case_max_tokens,
            max_cost_nusd=None
            if args.case_max_cost_usd is None
            else usd_to_nusd(args.case_max_cost_usd),
        )
    observations = read_batch(args.batch_dir) if args.batch_dir else args.observations
    return evaluate(
        args.dataset, observations, args.output_dir, args.judgments, args.annotation_approval
    )
