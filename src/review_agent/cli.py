import argparse
import json
import re
import sys
from pathlib import Path

from review_agent import app
from review_agent.audit import SECTIONS
from review_agent.config import usd_to_nusd
from review_agent.contracts import AgentError
from review_agent.report import exit_code


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo raw user inputs.
        self.print_usage(sys.stderr)
        self.exit(1, "INVALID_ARGUMENTS: use --help\n")


def parser() -> argparse.ArgumentParser:
    root = SafeParser(prog="review-agent")
    sub = root.add_subparsers(dest="command", required=True)
    from review_agent.evaluation.entry import configure

    configure(sub)
    serve = sub.add_parser("serve", help="启动本地审阅工作台；默认只允许离线演示")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--state-dir", type=Path, default=Path(".review-agent/workbench"))
    serve.add_argument("--allow-live", action="store_true", help="允许用户在页面主动提交真实调用")
    view = sub.add_parser("view", help="从已有安全 trace 导出离线只读 HTML；不访问任务数据库")
    view.add_argument("--trace", required=True, type=Path)
    view.add_argument("--output", required=True, type=Path)
    fetch = sub.add_parser("fetch", help="只读获取并冻结安全来源；不调用模型")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--output", required=True, type=Path)
    fetch.add_argument("--source-auth", action="store_true", help="显式使用对应源站 token 环境变量")
    for name in ("review", "resume", "status", "report", "trace", "review-cost"):
        command = sub.add_parser(name)
        command.add_argument("--state-dir", type=Path, default=Path(".review-agent"))
        if name == "review":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--diff", type=Path)
            source.add_argument("--url")
            source.add_argument("--source", type=Path)
            command.add_argument("--source-auth", action="store_true")
            command.add_argument("--provider", choices=["fixture", "deepseek"], default="fixture")
            command.add_argument("--fixture", type=Path)
            command.add_argument("--max-tokens", type=int, required=True)
            command.add_argument("--max-cost-usd", required=True)
            command.add_argument("--max-output-tokens", type=int, default=1024)
            command.add_argument("--max-repairs-per-unit", type=int, choices=[0, 1], default=1)
            command.add_argument("--max-tools-per-unit", type=int, choices=range(5), default=4)
        else:
            command.add_argument("--task", required=True)
        if name == "resume":
            command.add_argument(
                "--retry-unknown",
                metavar="ATTEMPT_ID",
                help="持久化对该 UNKNOWN 的一次重试选择；重复提交复用绑定",
            )
        if name in ("review", "resume", "report"):
            command.add_argument("--output", type=Path)
        if name == "trace":
            scope = command.add_mutually_exclusive_group()
            scope.add_argument("--finding", help="按 finding 查询关联调用；默认展示完整审计链")
            scope.add_argument("--attempt", help="按 attempt 查询，可审计 UNKNOWN 或零发现调用")
            command.add_argument(
                "--section",
                action="append",
                choices=SECTIONS,
                help="可重复指定；默认 all。request=安全请求，budget-events=预算事件，"
                "pricing=冻结价格、费用复核、预算限制与 prompt/policy 指纹。"
                "未指定 scope 时查询整个任务。",
            )
    return root


def main() -> int:
    args = parser().parse_args()
    task_id = None
    try:
        if args.command == "serve":
            from review_agent.workbench.server import serve

            serve(args.state_dir, port=args.port, allow_live=args.allow_live)
            return 0
        if args.command == "view":
            from review_agent.viewer import export_view

            print(json.dumps(export_view(args.trace, args.output), ensure_ascii=False, indent=2))
            return 0
        if args.command == "fetch":
            from review_agent.sources.contracts import SourceError
            from review_agent.sources.service import fetch_source, save_source, source_summary

            if args.output.exists() or args.output.is_symlink():
                raise SourceError("SOURCE_OUTPUT_EXISTS")
            source = fetch_source(args.url, authenticate=args.source_auth)
            save_source(source, args.output)
            print(json.dumps(source_summary(source), ensure_ascii=False, indent=2))
            return 0
        if args.command == "eval":
            from review_agent.evaluation.entry import dispatch

            result = dispatch(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3 if args.eval_command == "run-live" and result["stop_reason"] else 0
        if args.command == "review":
            task_id = app.create_task(
                args.diff,
                args.fixture,
                args.state_dir,
                max_tokens=args.max_tokens,
                max_cost_nusd=usd_to_nusd(args.max_cost_usd),
                max_output_tokens=args.max_output_tokens,
                provider_name=args.provider,
                max_repairs_per_unit=args.max_repairs_per_unit,
                max_tools_per_unit=args.max_tools_per_unit,
                source_url=args.url,
                source_path=args.source,
                source_auth=args.source_auth,
            )
        else:
            if not re.fullmatch(r"task_[0-9a-f]{32}", args.task):
                raise AgentError("INVALID_TASK_ID")
            task_id = args.task
        if args.command == "review-cost":
            print(
                json.dumps(
                    app.review_historical_costs(task_id, args.state_dir),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        snapshot = (
            app.execute(task_id, args.state_dir, retry_unknown=getattr(args, "retry_unknown", None))
            if args.command in ("review", "resume")
            else app.read_task(task_id, args.state_dir)
        )
        if args.command == "trace":
            value = app.trace(
                snapshot, finding_id=args.finding, attempt_id=args.attempt, sections=args.section
            )
        else:
            value = app.summary(snapshot)
        if args.command in ("review", "resume", "report"):
            output = args.output or args.state_dir / task_id / "report.md"
            app.export_report(snapshot, output)
            app.export_audit(snapshot, output.with_name(output.stem + ".audit.md"))
            value["report_written"] = True
            value["audit_report_written"] = True
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return exit_code(snapshot) if args.command in ("review", "resume") else 0
    except AgentError as error:
        failure = {"error": error.code, "task_id": task_id}
        if hasattr(error, "diagnostics"):
            failure["source_diagnostics"] = error.diagnostics
        print(json.dumps(failure), file=sys.stderr)
        return (
            4
            if error.code
            in (
                "UNSAFE_REQUEST",
                "SAFETY_SCAN_FAILED",
                "UNCLOSED_PRIVATE_KEY",
                "SECRET_IN_DIFF_METADATA",
                "UNSAFE_CONTROL_CHARACTER",
                "UNSAFE_DIFF_PATH",
            )
            else 1
        )
    except Exception:
        print(json.dumps({"error": "OPERATION_FAILED", "task_id": task_id}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
