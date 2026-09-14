import html
import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path

from review_agent.contracts import AgentError
from review_agent.review import evidence_index, finding_data
from review_agent.safety import Safety


def usd(nusd: int) -> str:
    return f"{Decimal(nusd) / Decimal(10**9):.9f}"


def code(value) -> str:
    return chr(96) + str(value) + chr(96)


def escape(text) -> str:
    """Untrusted content cannot create Markdown links, HTML, images or autolinks."""
    value = html.escape(str(text), quote=True).replace("\r", "").replace("\n", " / ")
    value = re.sub(r"([\\*_\[\]{}()!#|>~])", r"\\\1", value)
    return value.replace(chr(96), "\\" + chr(96)).replace("://", "&#58;//")


def exit_code(snapshot: dict) -> int:
    return {
        "COMPLETED": 0,
        "PARTIAL": 2,
        "PAUSED_BUDGET": 3,
        "PAUSED_UNKNOWN": 3,
        "STOPPED_BUDGET_BOUND": 3,
        "BLOCKED_SECURITY": 4,
        "RUNNING": 1,
    }.get(snapshot["task"]["status"], 1)


def context_coverage(data, unit):
    """Count actual line identities supplied to completed model calls, not tool reads."""
    from review_agent.tool_loop import payload, supplied_evidence

    identities = {}
    all_lines = set()
    for hunk in data["snapshot"]["hunks"]:
        if hunk["hunk_id"] not in unit["hunk_ids"]:
            continue
        for index, line in enumerate(hunk["lines"]):
            identity = (hunk["hunk_id"], index)
            all_lines.add(identity)
            for side in ("old", "new"):
                if line[f"{side}_lineno"]:
                    identities[f"{hunk['hunk_id']}:{side}:{line[f'{side}_lineno']}"] = identity
    seen = set()
    for operation in data["operations"]:
        if operation["unit_id"] != unit["unit_id"] or not operation["result_ref"]:
            continue
        request = payload(
            data["artifacts"][operation["request_ref"]], data["config"]["execution_mode"]
        )
        for ref in supplied_evidence(data["snapshot"], request):
            if ref in identities:
                seen.add(identities[ref])
    return len(seen), len(all_lines)


def review_scope_note(data: dict) -> str:
    """Brief coverage caveat for the code report, with operational facts kept in audit."""
    units = data["units"]
    if not units:
        return "未进行模型审阅：没有可审阅的 Python 文本变更。"
    done = sum(unit["status"] == "DONE" for unit in units)
    if data["task"]["status"] != "COMPLETED" or done != len(units):
        return f"审阅尚未完整结束，已完成 {done}/{len(units)} 个单元；以下仅为当前结果。"
    if data["config"].get("schema_version", 1) >= 5:
        if any(seen < total for seen, total in (context_coverage(data, u) for u in units)):
            return "已完成本轮审阅，部分上下文未覆盖；结论仅基于已提供的代码。"
    return "已完成本轮审阅，范围限于所提供的代码变更。"


def render_review(data: dict) -> str:
    """Concise code findings; no execution, cost, tool, or trace details."""
    files = {f["file_id"]: f["path"] for f in data["snapshot"]["files"]}
    evidence = evidence_index(data["snapshot"])
    findings = [finding_data(row) for row in data["findings"]]
    levels = {"high": "高", "medium": "中", "low": "低", "reference": "仅供参考"}
    out = ["# 代码审阅报告", ""]
    if data["config"]["execution_mode"] == "fixture":
        out.extend(["> 离线演示：使用预置回复，不代表真实模型审阅质量。", ""])
    out.extend([escape(review_scope_note(data)), "", "## 建议修改的问题", ""])
    formal = [f for f in findings if f["confidence"] != "reference"]
    reference = [f for f in findings if f["confidence"] == "reference"]
    if not formal:
        out.extend(["本次未报告正式代码问题；这不代表代码没有缺陷。", ""])
    for title, items in ((None, formal), ("仅供参考（尚未确认为缺陷）", reference)):
        if title and items:
            out.extend([f"## {title}", ""])
        for finding in items:
            anchor = f"{finding['hunk_id']}:{finding['side']}:{finding['line']}"
            path = files[evidence[anchor]["file_id"]]
            side = "变更后" if finding["side"] == "new" else "变更前"
            out.extend([
                f"### {escape(path)} · {side}第 {finding['line']} 行",
                "",
                f"**{escape(finding['title'])}**",
                "",
                f"严重性：{levels[finding['severity']]}；置信度：{levels[finding['confidence']]}。",
                "",
            ])
            for label, field in (
                ("触发条件", "trigger"), ("问题", "actual_behavior"),
                ("影响", "impact"), ("修改建议", "suggestion"),
            ):
                out.append(f"- {label}：{escape(finding[field])}")
            if finding["truncated_source"]:
                out.append("- 说明：该条来自截断回复，审阅尚未完整结束。")
            out.append("")
    if data["snapshot"]["excluded"]:
        out.extend(["## 未审阅的文件", ""])
        reasons = {"BINARY": "二进制文件", "NO_PYTHON_TEXT_CHANGE": "没有 Python 文本变更"}
        for item in data["snapshot"]["excluded"]:
            out.append(f"- {escape(item['path'])}：{reasons[item['reason']]}")
        out.append("")
    body = "\n".join(out)
    Safety().require_safe(body)
    return body


def render_audit(data: dict) -> str:
    return render(data).replace("# Code Review 报告", "# 执行与审计明细", 1)


def render(data: dict) -> str:
    """Original comprehensive export, retained for historical evaluation compatibility."""
    task, config, totals = data["task"], data["config"], data["totals"]
    units, attempts = data["units"], data["attempts"]
    done = sum(u["status"] == "DONE" for u in units)
    files = {f["file_id"]: f["path"] for f in data["snapshot"]["files"]}
    evidence = evidence_index(data["snapshot"])
    coverage = (
        "完整完成"
        if units and done == len(units) and not data["snapshot"]["excluded"]
        else "部分完成"
    )
    contexts = (
        {u["unit_id"]: context_coverage(data, u) for u in units}
        if config.get("schema_version", 1) >= 5
        else {}
    )
    if coverage == "完整完成" and any(seen < total for seen, total in contexts.values()):
        coverage = "流程已完成；上下文覆盖不完整"
    if data.get("source", {}).get("coverage") == "EMPTY":
        coverage = "来源无变更；未进行模型审阅"
    elif data.get("source", {}).get("coverage") == "EXCLUDED_ONLY":
        coverage = "仅有范围排除文件；未进行模型审阅"
    mode_note = (
        "> fixture 模式：模拟回复与消耗，未调用真实模型；不代表模型审阅质量。"
        if config["execution_mode"] == "fixture"
        else (
            "> DeepSeek 真实模型小样例：用量来自 API；"
            "金额按冻结费率在本地估算，未与提供方账单核对。"
        )
    )
    out = [
        "# Code Review 报告",
        "",
        mode_note,
        "",
        "## 任务与覆盖",
        "",
        f"- 任务：{code(task['task_id'])}",
        f"- 执行状态：{code(task['status'])}；审阅覆盖：**{coverage}**",
        f"- 创建时间：{escape(task['created_at'])}",
        f"- 快照：{code(task['snapshot_id'])}",
        f"- 配置：{code(task['config_digest'])}",
        f"- Trace：{code(task['trace_id'])}",
        f"- Provider：{code(config['execution_mode'])}；"
        f"模型：{code(config.get('model', 'fixture'))}",
        f"- 文件：{len(files)}；排除：{len(data['snapshot']['excluded'])}",
        f"- 完整完成单元：{done}/{len(units)}；仅审阅提供的 diff 上下文。",
        f"- 输入脱敏替换：{data['snapshot']['redactions']}；未执行目标仓库代码。",
        "",
        "| 单元 | 文件 | hunks | 状态 | 原因 | 发送名额 |",
        "|---|---|---|---|---|---|",
    ]
    if "source" in data:
        source = data["source"]
        position = out.index("| 单元 | 文件 | hunks | 状态 | 原因 | 发送名额 |")
        out[position:position] = [
            f"- 来源：{escape(source['url'])}；仓库 ID：{source['repository_id']}",
            f"- 来源快照：{code(config['source_digest'])}；获取覆盖：{source['coverage']}",
            f"- 比较：{code(source['comparison'])}；base：{code(source['base_sha'])}；"
            f"head：{code(source['head_sha'])}",
            f"- 目标提交：{code(source['target_sha'])}；diff version：{source['version_id']}",
            f"- 获取时间：{escape(source['fetched_at'])}；HTTP 请求：{len(source['http_events'])}",
            *[
                f"- 来源排除：{escape(f['new_path'])} — {f['reason']}"
                for f in source["files"]
                if f["disposition"] == "EXCLUDED"
            ],
            "",
        ]
    for unit in units:
        out.append(
            f"| {unit['unit_id']} | {escape(unit['path'])} | "
            f"{', '.join(unit['hunk_ids'])} | {unit['status']} | "
            f"{escape(unit['stop_reason'] or '—')} | {unit['sends']} |"
        )
    if not units:
        out.append("| — | — | — | PENDING | 没有可审阅 Python 文本单元 | 0 |")
    for excluded in data["snapshot"]["excluded"]:
        out.extend(["", f"排除：{escape(excluded['path'])} — {excluded['reason']}"])
    out.extend(["", "## 评论", ""])
    findings = [finding_data(row) for row in data["findings"]]
    for category, items in (
        ("正式评论", [f for f in findings if f["confidence"] != "reference"]),
        ("仅供参考", [f for f in findings if f["confidence"] == "reference"]),
    ):
        out.extend([f"### {category}", ""])
        if not items:
            out.extend(["无。零评论仅描述本次输出，不等于排除未审阅范围的问题。", ""])
        for finding in items:
            anchor = f"{finding['hunk_id']}:{finding['side']}:{finding['line']}"
            path = files[evidence[anchor]["file_id"]]
            out.extend(
                [
                    f"#### {escape(finding['title'])}",
                    "",
                    f"- Finding：{code(finding['finding_id'])}",
                    f"- 位置：{escape(path)}，{finding['side']}:{finding['line']}",
                    f"- 严重性：{finding['severity']}；置信度：{finding['confidence']}",
                ]
            )
            for label, field in (
                ("触发条件", "trigger"),
                ("变更因果", "introduced_by"),
                ("实际行为", "actual_behavior"),
                ("预期行为", "expected_behavior"),
                ("影响", "impact"),
                ("建议", "suggestion"),
            ):
                out.append(f"- {label}：{escape(finding[field])}")
            if finding["confidence_reason"]:
                out.append(f"- 降级原因：{finding['confidence_reason']}")
            if finding["truncated_source"]:
                out.append("- 来源限制：来自截断结果，本单元仅部分完成。")
            out.append(f"- Trace：[查看调用与校验](#{finding['finding_id']})")
            for role, refs in (
                ("证据", finding["evidence"]),
                ("预期依据", finding["expectation_evidence"]),
            ):
                for ref in refs:
                    out.append(f"- {role} {escape(ref)}：{escape(evidence[ref]['text'].strip())}")
            out.append("")
    available_tokens = config["max_tokens"] - totals["settled_tokens"] - totals["held_tokens"]
    available_cost = (
        config["max_cost_nusd"] - totals["settled_cost_nusd"] - totals["held_cost_nusd"]
    )
    accounting_status = "OPEN" if any(a["fee_status"] == "HELD" for a in attempts) else "CLOSED"
    settled_label = (
        "账本已结算（峰时本地估算上界）"
        if config["execution_mode"] == "deepseek" and config.get("schema_version", 1) >= 3
        else "已结算"
    )
    out.extend(
        [
            "## 消耗与全部未结算预留",
            "",
            f"- 任务上限：{config['max_tokens']} tokens / {usd(config['max_cost_nusd'])} USD",
            f"- {settled_label}：{totals['settled_tokens']} tokens / "
            f"{usd(totals['settled_cost_nusd'])} USD",
            f"- 未结算占用：{totals['held_tokens']} tokens / {usd(totals['held_cost_nusd'])} USD",
            f"- 可用余额：{available_tokens} tokens / {usd(available_cost)} USD",
            f"- 账务状态：{accounting_status}",
            "",
        ]
    )
    if task["send_block_reason"]:
        out.extend(
            [
                f"**任务发送阻断：{task['send_block_reason']}**",
                f"来源 attempt：{code(task['send_block_attempt_id'])}。"
                "余额充足也不得发送；后续单元和 resume 均受约束。",
                "",
            ]
        )
    out.extend(
        [
            "| Attempt | Operation | 调用状态／未结算原因 | Tokens 预留 | USD 预留 |",
            "|---|---|---|---|---|",
        ]
    )
    held = [a for a in attempts if a["fee_status"] == "HELD"]
    reasons = {
        "RESERVED": "已预留，未发送",
        "DISPATCHED": "可能已发送，待恢复判定",
        "UNKNOWN": "执行结果未知",
        "COMPLETED": "回复已保存，usage 不完整",
    }
    for attempt in held:
        out.append(
            f"| {attempt['attempt_id']} | {attempt['operation_id']} | "
            f"{attempt['call_status']} / {reasons[attempt['call_status']]} | "
            f"{attempt['quote_tokens']} | {usd(attempt['quote_cost_nusd'])} |"
        )
    if not held:
        out.append("| — | — | 无未结算预留 | 0 | 0.000000000 |")
    out.extend(
        [
            "",
            "## 调用账本",
            "",
            "| Attempt | 调用 | 结果／完整性 | 费用 | Quote tokens / USD | "
            "用量 tokens / 账本 USD |",
            "|---|---|---|---|---|---|",
        ]
    )
    for attempt in attempts:
        result = data["artifacts"].get(attempt["result_ref"], {})
        actual = (
            "未知"
            if attempt["actual_tokens"] is None
            else f"{attempt['actual_tokens']} / {usd(attempt['actual_cost_nusd'])}"
        )
        out.append(
            f"| {attempt['attempt_id']} | {attempt['call_status']} | "
            f"{attempt['result_status'] or '—'} / {result.get('completion_state', '—')} | "
            f"{attempt['fee_status']} | {attempt['quote_tokens']} / "
            f"{usd(attempt['quote_cost_nusd'])} | {actual} |"
        )
        if result.get("provider_request_id") or result.get("provider_finish_reason"):
            out.append(
                f"- {code(attempt['attempt_id'])} provider request："
                f"{code(result.get('provider_request_id') or '—')}；"
                f"model：{code(result.get('provider_model') or '—')}；"
                f"finish：{code(result.get('provider_finish_reason') or '—')}"
            )
    if config["execution_mode"] == "deepseek":
        out.extend(["", "## 费用口径与复核", ""])
        reviews = data.get("pricing_reviews", [])
        if not reviews:
            out.append("无追加费用复核记录；账本金额仅代表任务创建时冻结费率的本地计算。")
        for row in reviews:
            review = row["data"]
            pricing = review["pricing"]
            original = review["original_cost_nusd"]
            out.extend(
                [
                    f"### {code(row['review_id'])}",
                    "",
                    f"- Attempt：{code(row['attempt_id'])}；类型：{code(row['review_kind'])}",
                    f"- 价格版本：{code(pricing['version'])}；"
                    f"生效：{code(pricing['effective_at'])}",
                    f"- 来源：{escape(pricing['source_url'])}；"
                    f"查阅日期：{code(pricing['source_checked_on'])}",
                    f"- 原账本金额：{'不适用' if original is None else usd(original) + ' USD'}",
                    f"- 修正估算区间：{usd(review['revised_estimate_min_nusd'])}–"
                    f"{usd(review['revised_estimate_max_nusd'])} USD",
                    f"- 时间假设：{code(review['assumed_at_kind'])} = "
                    f"{code(review['assumed_at'])}；推定 {code(review['assumed_tier'])}，"
                    f"对应 {usd(review['assumed_cost_nusd'])} USD",
                    "- 提供方账单核对：否；峰谷归属时间点未经提供方文档确认。",
                ]
            )
    if data.get("retry_decisions"):
        out.extend(
            [
                "",
                "## 人工重试选择",
                "",
                "| Decision | 来源 UNKNOWN | 状态 | 绑定 attempt |",
                "|---|---|---|---|",
            ]
        )
        for decision in data["retry_decisions"]:
            out.append(
                f"| {decision['decision_id']} | {decision['source_attempt_id']} | "
                f"{decision['status']} | {decision['bound_attempt_id'] or '—'} |"
            )
        out.extend(["", "一次选择只授权一个后继；原 UNKNOWN 预留继续占用预算。"])
    if config.get("schema_version", 1) >= 5:
        out.extend(
            [
                "",
                "## 工具调用与上下文",
                "",
                f"每单元最多 {config['max_tools_per_unit']} 个工具名额；"
                "耗尽后仍可在模型发送和预算限制内总结。",
                "read_hunk 返回安全 diff 中首轮省略的上下文；diff 外的文件内容不可用。",
                "",
                "| Tool call | 工具 | 状态／原因 | 名额 | 来源模型请求 | 工具结果 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for call in data.get("tool_calls", []):
            request = data["artifacts"][call["request_ref"]]
            result = data["artifacts"].get(call["result_ref"], {})
            out.append(
                f"| {call['tool_call_id']} | {escape(request['name'])} | "
                f"{call['status']} / {result.get('error_code') or '—'} | {call['slot_no']} | "
                f"{request['source_request_ref']} | {call['result_ref'] or '—'} |"
            )
        out.extend(["", "| 单元 | 已带入完成调用的行／diff 总行数 |", "|---|---|"])
        for unit_id, (seen, total) in contexts.items():
            out.append(f"| {unit_id} | {seen}/{total} |")
        out.extend(
            [
                "",
                "按快照行去重；工具产生但尚未带入模型请求的结果不算已提供上下文。"
                "完成与零发现只描述本次审阅结论，未提供上下文仍明确保留。",
            ]
        )
    repairs = [c for c in data.get("operation_contexts", []) if c["kind"] == "REPAIR"]
    if repairs:
        out.extend(
            ["", "## 格式修复", "", "| REPAIR operation | 原 operation | 原结果 |", "|---|---|---|"]
        )
        for context in repairs:
            out.append(
                f"| {context['operation_id']} | {context['source_operation_id']} | "
                f"{context['source_result_ref']} |"
            )
        out.extend(["", "修复是独立收费操作，仍须通过结构和证据校验；原回复与费用保留。"])
    out.extend(["", "## Trace 与校验", ""])
    for finding in findings:
        attempt = next(a for a in attempts if a["result_ref"] == finding["result_ref"])
        out.extend(
            [
                f"### {finding['finding_id']}",
                "",
                f"- validation：{code(finding['validation_ref'])}",
                f"- result：{code(finding['result_ref'])}",
                f"- operation：{code(attempt['operation_id'])}",
                f"- attempt：{code(attempt['attempt_id'])}；span：{code(attempt['span_id'])}",
                "",
            ]
        )
    for unit in units:
        validation = data["artifacts"].get(unit["validation_ref"])
        if validation and validation["rejected"]:
            out.append(
                f"- {unit['unit_id']} 拒绝候选：{escape(json.dumps(validation['rejected']))}"
            )
    for span in data["spans"]:
        out.append(f"- {code(span['span_id'])}：{span['kind']} / {span['status']}")
    out.extend(
        [
            "",
            "## 范围与限制",
            "",
            "- 仅 Python 文本 diff；未读取完整仓库，未执行测试、安装脚本或源码。",
            "- 置信度校验验证结构、定位和证据引用；不证明语义判断正确。",
            "- 固定 secret 规则不覆盖所有混淆或未知凭证。",
            (
                "- UNKNOWN 保留预留；仅持久化的人工选择授权一个后继。"
                "完整格式错误最多一个 REPAIR；截断不自动续写。"
                if config.get("schema_version", 1) >= 4 and config.get("max_repairs_per_unit")
                else "- UNKNOWN 保留预留；不自动重试或续写，本任务未启用格式修复。"
            ),
            (
                "- 工具仅运行 Agent 的可信只读实现；URL 限 github.com / gitlab.com。"
                "GitHub 真实只读获取已验证，GitLab 仅 mock 验收；"
                "URL → 真实模型 → 报告的完整在线链路尚未验证。"
                if config.get("schema_version") in (7, 8)
                else "- 工具仅运行 Agent 的可信只读实现；"
                "未接入 GitHub/GitLab URL，未进行完整质量评估。"
                if config.get("schema_version", 1) >= 5
                else "- 当前无工具执行或 GitHub/GitLab URL 接入；本次真实模型小样例不构成质量评估。"
                if config["execution_mode"] == "deepseek"
                else "- 当前无真实模型、工具执行或 GitHub/GitLab URL 接入。"
            ),
            "",
        ]
    )
    body = "\n".join(out)
    Safety().require_safe(body)
    return body


def write_report(path: Path, body: str):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".review-report-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise AgentError("REPORT_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
