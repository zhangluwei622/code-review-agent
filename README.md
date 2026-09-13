# Code Review Agent

一个可运行、可恢复、可追溯的代码审阅 Agent。输入 **Python git diff** 或公开 **GitHub PR / GitLab MR URL**，通过单 Agent 审阅与工具循环生成 Markdown 报告，并在本地 Web 工作台观察模型、工具、格式修复、预算和恢复状态。

技术栈：**Python 3.12 + LangGraph + SQLite**。当前审阅指导为冻结的 **S2**，响应采用严格 JSON 重复键拒绝协议。面向面试 Take-Home 与本地实验，尚不能替代人工审阅。

## 功能

- **本地工作台**：输入、执行过程、报告三栏同屏；窄屏使用页内标签。在线任务与离线演示分别保存，长内容在面板内滚动。
- **在线审阅**：页面设置 DeepSeek API key，提交自己的 diff 或公开 PR/MR；也支持 CLI。默认模型为 `deepseek-flash`，页面不提供任意接口或模型切换。
- **单 Agent 工具循环**：`list_changed_files`、`read_hunk`、`search_diff` 读取安全快照，补取首轮省略的 diff 内上下文；工具名额耗尽后仍可总结。
- **故障恢复**：checkpoint、持久化调用结果、独立 REPAIR operation，以及 UNKNOWN 后的明确人工重试。
- **可观测报告**：评论关联请求、原安全回复、证据、预算和调用关系；下载 Markdown、trace JSON、独立只读 HTML。
- **离线评估**：按场景组划分开发集／留出集，支持 fixture 流程验证、人工标注与判分；不使用 LLM-as-judge。

## 快速启动

要求本机有 Python 3.12 和 `uv`。以下命令会安装依赖；**启动和离线演示不需要 API key，不调用真实模型**。

```bash
git clone https://github.com/zhangluwei622/code-review-agent.git
cd code-review-agent
uv sync --locked --python 3.12
uv run review-agent serve --port 8765 --state-dir .review-agent/workbench
```

打开 **http://127.0.0.1:8765/**。服务只绑定本机 `127.0.0.1`，一次执行一个任务。

1. 在左侧 **演示任务 → 运行离线演示** 选择场景。
2. 点击 **开始离线演示**，观察模型审阅、工具、REPAIR 和状态记录。
3. 点击调用查看请求、结果、证据及预算；在报告区下载可观测 HTML。

演示包含“发现问题”“工具与格式修复”“格式修复”“未知结果与人工重试”。它们使用预置输入和回复，仅说明流程，不评价任意代码质量。

没有 `uv` 时可用现有 Python 安装，依赖版本以 `uv.lock` 为复现基准：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
review-agent serve --port 8765 --state-dir .review-agent/workbench
```

## 在线任务与 API 设置

在工作台右上角 **API 设置** 输入自己的 DeepSeek key，保存后从左侧 **在线任务 → 新建在线审阅** 提交 diff 或公开 URL。保存 key 不会测试连接或调用模型；点击开始在线审阅后才可能产生费用。

key 仅存在本地服务内存中，不回显、不写入浏览器存储、任务记录、报告或进程环境；服务重启后需重新输入。清除会停用当前服务的在线入口，执行中禁止更换或清除。已保存不代表凭证有效性已经验证。

也可自行在终端设置 `DEEPSEEK_API_KEY`，使用 `--allow-live` 启动工作台；此时凭证由原环境变量入口读取。不要把真实 key 写进仓库、diff、命令历史或截图。

默认单任务预算为 **100,000 tokens / US$0.05**，单次输出上限 **1,024 tokens**；页面提交前可调整。默认每单元最多 4 次工具执行、1 次 REPAIR、6 次模型发送。预算包括后续模型轮次、REPAIR 和 HELD。任务提交后配置冻结，刷新或恢复不会重置预算。

## CLI 操作

以下命令从仓库根目录执行。`review` 输出任务 ID，后续命令中的 `task_…`、`attempt_…` 替换为实际 ID，并始终使用同一个 `--state-dir`。

**运行离线审阅，生成 Markdown：**

```bash
uv run review-agent review --diff examples/diffs/empty-list.diff --fixture examples/provider/success.json --max-tokens 100000 --max-cost-usd 0.05 --max-output-tokens 1024 --state-dir .review-agent/cli-demo --output .review-agent/cli-demo/report.md
```

**审阅自己的 diff（需自行设置环境凭证，会产生模型费用）：**

```bash
uv run review-agent review --diff changes.diff --provider deepseek --max-tokens 100000 --max-cost-usd 0.05 --max-output-tokens 1024 --state-dir .review-agent/live --output .review-agent/live/report.md
```

可用 `git diff -- '*.py' > changes.diff` 生成未暂存变更，或 `git diff --cached -- '*.py' > changes.diff` 生成暂存变更。Agent 不会执行这些目标代码。

**只读获取 URL，冻结来源快照（无模型调用）：**

```bash
uv run review-agent fetch --url 'https://github.com/OWNER/REPO/pull/123' --output .review-agent/source
```

URL 替换为真实公开 PR；GitLab 格式为 `https://gitlab.com/GROUP/REPO/-/merge_requests/123`。目标输出目录需尚不存在。随后可把 `review` 的 `--diff changes.diff` 替换为 `--source .review-agent/source`，或直接使用 `--url '真实 PR/MR URL'`；在线审阅仍需 `--provider deepseek`、预算与凭证。

**查询、恢复与追溯：**

```bash
uv run review-agent status --task task_… --state-dir .review-agent/cli-demo
uv run review-agent resume --task task_… --state-dir .review-agent/cli-demo
uv run review-agent trace --task task_… --state-dir .review-agent/cli-demo > trace.json
uv run review-agent view --trace trace.json --output review.html
```

`trace --finding FINDING_ID` 可追溯具体评论；`report --task task_… --state-dir ... --output report.md` 可重新导出报告。`view` 仅读取安全 trace，不访问任务库或调用模型。

UNKNOWN 表示请求可能已发出但结果不明，普通 `resume` 不重发。明确接受新增调用时才使用 `resume --task task_… --state-dir ... --retry-unknown attempt_…`；原 UNKNOWN 的 HELD 继续保留，新 attempt 再次 UNKNOWN 需要新的明确选择。重复提交同一重试选择复用既有绑定。

CLI 审阅退出码：`0` 完成、`2` 部分完成、`3` 预算或 UNKNOWN 暂停、`4` 安全阻断、`1` 其他错误。完成且零发现、abstain、截断与部分完成会分别记录。

## 六项核心要求与实现

| 要求 | 实现与验证入口 |
|---|---|
| Checkpoint 恢复 | [app.py](src/review_agent/app.py)、[storage.py](src/review_agent/storage.py)、[进程故障测试](tests/test_process_recovery.py)。业务事实先提交，checkpoint 保存游标与引用，重放复用结果。 |
| 评论关联 trace | [audit.py](src/review_agent/audit.py)、[viewer](src/review_agent/viewer/)、[trace 测试](tests/test_trace.py)。关联 finding、operation、attempt、请求、回复、证据与预算。 |
| 声明式工具扩展 | [注册器](src/review_agent/tools/registry.py)、[声明样例](src/review_agent/tools/builtin/read_hunk.json)、[工具测试](tests/test_tools.py)。新增可信实现及声明，无需修改主流程。 |
| 任务级 token／金额预算 | [budget.py](src/review_agent/budget.py)、[gateway.py](src/review_agent/gateway.py)、[账本测试](tests/test_ledger_gateway.py)。预留、结算、HELD 与超 quote 停发跨单元和恢复生效。 |
| 置信度分级 | [review.py](src/review_agent/review.py)、[report.py](src/review_agent/report.py)、[协议测试](tests/test_delivery_protocol.py)。high、medium 与 reference 分开，严格校验锚点和支持证据。 |
| Secret 防外传与禁止执行目标代码 | [safety.py](src/review_agent/safety.py)、[工具执行器](src/review_agent/tools/runner.py)、[安全测试](tests/test_ingest_safety.py)。原始输入先脱敏，工具只读取权限范围内的安全快照。 |

[架构与故障语义](docs/architecture/260914-code-review-agent-public-overview.md)说明业务账本、checkpoint、模型与工具的边界。

## 测试与离线评估

```bash
uv sync --locked --python 3.12
uv run pytest -q
uv run ruff check src tests
```

pytest 默认禁止网络连接，provider 使用 fixture／mock，不需要真实 key。故障测试会启动和终止受控的项目测试进程，不执行目标 diff 中的代码。完整测试包含进程故障，可能需要数分钟；[验证记录](docs/implementation/260914-code-review-agent-public-verification.md)区分历史验收与公开快照自测。

```bash
uv run review-agent eval validate --dataset examples/eval/phase-5/dataset-v2.json --output-dir .review-agent/eval-labels
uv run review-agent eval run-fixture --dataset examples/eval/phase-5/dataset-v2.json --batch-dir .review-agent/eval-demo --split all
uv run review-agent eval score --dataset examples/eval/phase-5/dataset-v2.json --batch-dir .review-agent/eval-demo --output-dir .review-agent/eval-score
```

数据集为 8 个开发样例、4 个留出样例，按场景组划分。fixture 只验证评估流程，不产生真实质量成绩；人工标注确认和评论判分分别保存。真实评估需重新准备并批准冻结 manifest，整批预算与 HELD 累计；本仓库不包含可直接复用的历史付费审批。

## 已知限制与未覆盖项

- **输入范围**：主要审阅 Python 文本 diff；工具只能读取安全 diff 快照，包括首轮省略的快照内上下文，不能读取完整仓库或 diff 外上下文。排除文件及原因会注明；缺失 patch 不冒充已覆盖。
- **平台验证**：指定 GitHub 公开 PR 的真实只读获取通过；GitLab 只有 mock 验证。URL → 真实模型 → 报告完整在线链路未验证。自托管／Enterprise、评论回写未实现。
- **质量**：历史 S2 开发集命中 3/3、留出集命中 2/2，分别是小型样例集的一次结果，开发集曾用于调优；不代表生产准确率，仍可能误报、漏报或高估严重性。
- **费用**：token quote 是工程估算，不能保证实际账单绝对不超预算；检测到实际超 quote 后阻止后续发送。金额未与提供方账单核对，UNKNOWN 保留预留，不做远端自动对账。
- **安全**：secret 扫描可能遗漏未知或混淆凭证。工具实现必须可信，声明式注册不等于任意代码沙箱。本地工作台不适用于直接公网部署或多用户服务。
- **恢复与观测**：SIGKILL 验证不代表断电或磁盘损坏验证；HTML 只呈现已有安全记录，缺失的 resume／checkpoint 事件不补造，不展示模型隐藏推理。
- **验证环境**：主要在 macOS Intel／Python 3.12 验证，其他平台未验证。新重复键协议、响应持久化修复和工作台仅做本地回归，未单独在线评估；模型精确实际版本也未完全确认。

## 仓库内容

| 路径 | 内容 |
|---|---|
| `src/review_agent/` | CLI、LangGraph、业务账本、provider、安全策略、工具、URL 来源、评估与本地工作台 |
| `tests/` | 自动化测试及具备来源 hash 的[安全回归 fixture](tests/fixtures/README.md) |
| `examples/diffs/`、`examples/provider/` | 可直接运行的预置 diff 与模型回复，含明确标识的假凭证安全样例 |
| `examples/eval/phase-5/` | 版本化离线数据集与配套 diff／fixture |
| `docs/` | 公开架构说明与本次发布验证记录 |
| `uv.lock` | 依赖锁定，推荐通过 `uv sync --locked` 安装 |

本地任务库、完整历史实验、聊天导出、真实审批、虚拟环境与重复交付 ZIP 不纳入公开仓库；原件仍在原工作区保留。GitHub 仓库只带运行和回归所需的安全样例，不声称包含完整 AI 聊天导出或历史实验复现材料。
