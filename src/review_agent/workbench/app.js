"use strict";
const $ = (id) => document.getElementById(id);
const state = { token: "", config: null, selected: null, detail: null, active: null, busy: false,
  inputType: "diff", selectedNode: null, detailTab: "request", signature: "", pending: null, settingsBusy: false };
const names = { RUNNING: "运行中", PREPARING: "准备输入", FINISHED: "运行已结束", INTERRUPTED: "等待恢复",
  FAILED: "执行停止", COMPLETED: "已完成", PARTIAL: "部分完成", PAUSED_UNKNOWN: "等待人工选择",
  PAUSED_BUDGET: "预算暂停", BLOCKED_SECURITY: "安全阻断", RESERVED: "已预留", DISPATCHED: "已发送",
  UNKNOWN: "结果未知", CANCELLED: "已取消", HELD: "保留预留", SETTLED: "已记账", PENDING: "待处理",
  DONE: "已审阅", ABSTAIN: "弃权", ABSTAINED: "弃权", TRUNCATED: "截断", PARTIAL_TRUNCATED: "回复截断",
  PARTIAL_INVALID_RESULT: "结果校验失败", PARTIAL_LIMIT: "达到限额", REGISTERED: "已登记",
  VALID: "有效结果", FORMAT_INVALID: "格式错误", UNUSABLE: "不可用", SAFETY_REJECTED: "安全拒绝",
  SUCCEEDED: "执行成功", REJECTED: "已拒绝", TIMED_OUT: "执行超时",
  REVIEW: "模型审阅", REPAIR: "格式修复", TOOL: "工具调用", UNIT: "审阅单元", TASK: "任务",
  PREPARED: "冻结请求", RUNNER: "工作台", RESUME_REQUESTED: "请求恢复", GAP: "观察缺口" };
const errorNames = { LIVE_DISABLED: "请打开 API 设置，保存 DeepSeek API key 后提交在线任务。",
  WORKBENCH_BUSY: "已有任务正在运行，请等待该任务结束。", INVALID_INPUT: "输入或预算格式不正确，请检查后再提交。",
  DEMO_INPUT_CHANGED: "离线演示仅运行所选示例。请重新载入示例；审阅自己的输入需要启用真实模式。",
  PROVIDER_CREDENTIAL_MISSING: "尚未配置可用凭证，请打开 API 设置重新输入 key。",
  INPUT_NOT_COMMITTED: "输入快照尚未提交，无法恢复。请重新提供输入，明确创建新任务。",
  INVALID_DIFF: "输入不是有效的 unified diff，请粘贴 git diff 输出。",
  SESSION_REQUIRED: "本地服务会话已变化，正在重新连接；不会自动重发任务。",
  INPUT_TOO_LARGE: "输入超过大小上限。Diff 最大为 1 MiB。", JOB_NOT_FOUND: "本地服务中未找到该任务。" };
const label = (s) => names[s] || s || "未记录";
const number = (n) => Number(n || 0).toLocaleString("en-US");
const usd = (n) => "$" + (Number(n || 0) / 1e9).toFixed(6);
const short = (s) => (s || "").slice(-10);
const tone = (s) => ["COMPLETED", "DONE", "VALID", "SETTLED", "SUCCEEDED"].includes(s) ? "success" :
  ["RUNNING", "PREPARING", "DISPATCHED", "RESERVED", "REGISTERED"].includes(s) ? "running" :
  ["UNKNOWN", "PAUSED_UNKNOWN", "PAUSED_BUDGET", "PARTIAL", "INTERRUPTED", "HELD"].includes(s) ? "warning" :
  ["FAILED", "BLOCKED_SECURITY", "SAFETY_REJECTED"].includes(s) ? "failed" : "neutral";
function el(tag, text, cls) { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; }
function badge(status) { return el("span", label(status), "badge " + tone(status)); }
function button(text, handler, cls = "secondary") { const n = el("button", text, cls); n.type = "button"; n.addEventListener("click", handler); return n; }
function showError(error) { $("error").textContent = errorNames[error.message] || "操作未完成：" + error.message; $("error").hidden = false; }
function clearError() { $("error").hidden = true; }
function remember(id) { try { if (id) localStorage.setItem("review-workbench-selection", id); else localStorage.removeItem("review-workbench-selection"); } catch (_) {} }
function remembered() { try { return localStorage.getItem("review-workbench-selection"); } catch (_) { return null; } }
async function api(path, body, download = false) {
  const options = { headers: { "X-Workbench-Token": state.token }, cache: "no-store" };
  if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  if (!response.ok) { let message = "HTTP_" + response.status; try { message = (await response.json()).error || message; } catch (_) {} throw new Error(message); }
  return download ? response.blob() : response.json();
}
function connected(ok) {
  $("connection").className = "connection" + (ok ? "" : " offline");
  $("connection").replaceChildren(el("i"), document.createTextNode(ok ? "本地服务已连接" : "连接中断 · 自动重新读取"));
}
function setMode() {
  const fixture = $("mode").value === "fixture";
  $("demo-settings").hidden = !fixture; $("diff").readOnly = fixture;
  $("diff-file").disabled = fixture; $("upload-label").classList.toggle("disabled", fixture);
  $("url-tab").disabled = fixture;
  modeHelp();
  $("submit-help").textContent = fixture ? "离线演示不会调用真实模型，也不评价输入代码质量。" : "点击开始即提交本次真实审阅；可能产生模型费用。";
  if (fixture) { setInput("diff"); loadDemo(); }
  refreshButtons();
}
function modeHelp() {
  const fixture = $("mode").value === "fixture";
  $("configure-inline").hidden = fixture || Boolean(state.config?.live_enabled) || Boolean(state.selected);
  if (state.detail) return;
  $("mode-help").textContent = fixture ? "使用预置输入和回复，仅演示流程，不计质量成绩。" :
    state.config?.live_enabled ? "API 入口已启用。提交后按下方预算发起真实调用。" : "先设置 API key，再提交自己的 diff 或公开 PR / MR。";
}
function showPane(pane) {
  document.body.dataset.pane = pane;
  document.querySelectorAll(".pane-tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.pane === pane)));
}
function closeHistory() {
  document.body.classList.remove("history-open"); $("history-toggle").setAttribute("aria-expanded", "false");
}
function applySettings(settings) {
  state.config = { ...state.config, ...settings };
  $("api-status").textContent = settings.credential_source === "memory" ? "已设置" : settings.credential_source === "environment" ? "环境变量" : "未设置";
  $("api-status").className = settings.live_enabled ? "configured" : "";
  $("settings-status").textContent = settings.credential_source === "memory" ? "已保存到本地服务内存。凭证有效性尚未在线验证。" :
    settings.credential_source === "environment" ? "已允许使用启动终端的环境变量，凭证有效性尚未验证。" : "在线入口未启用。演示任务可直接运行。";
  modeHelp(); refreshButtons();
}
async function openSettings() {
  $("api-key").value = ""; $("settings-error").hidden = true; $("settings-dialog").showModal();
  try { applySettings(await api("/api/settings")); }
  catch (_) { $("settings-error").textContent = "无法读取设置，请检查本地连接。"; $("settings-error").hidden = false; }
}
function loadDemo() {
  const demo = state.config?.demos.find(d => d.id === $("demo").value);
  if (demo) { $("diff").value = demo.diff; countLines(); }
}
function setInput(type) {
  state.inputType = type;
  $("diff-tab").setAttribute("aria-selected", String(type === "diff"));
  $("url-tab").setAttribute("aria-selected", String(type === "url"));
  $("diff-input").hidden = type !== "diff"; $("url-input").hidden = type !== "url";
}
function countLines() { $("input-lines").textContent = $("diff").value.split("\n").length + " 行"; }
function refreshButtons() {
  const running = Boolean(state.active || state.busy), needsKey = $("mode").value === "deepseek" && !state.config?.live_enabled;
  $("submit").disabled = !state.config || running || state.settingsBusy || (!state.detail && needsKey);
  $("submit").firstElementChild.textContent = running ? "任务执行中…" : state.detail ? "新建另一项审阅" : $("mode").value === "fixture" ? "开始离线演示" : "开始在线审阅";
  $("resume").disabled = running || state.settingsBusy || needsKey; $("retry").disabled = $("resume").disabled;
  $("new-task").disabled = state.busy; $("new-live").disabled = state.busy;
  $("save-key").disabled = running || state.settingsBusy;
  $("clear-key").disabled = running || state.settingsBusy || !state.config?.live_enabled;
}
function selectJob(id) {
  state.selected = id; state.signature = ""; state.selectedNode = null; state.detail = null;
  remember(id); $("inspector").hidden = true; clearError();
}
function newTask(mode = $("mode").value) {
  selectJob(null); state.pending = null; renderRun(null); renderReport(null); renderHistory(state.history || []);
  lockInput(false); $("mode").value = mode;
  $("diff").value = ""; $("url").value = ""; $("diff-file").value = "";
  setInput("diff"); setMode(); countLines(); showPane("input"); closeHistory();
  $("breadcrumb").textContent = mode === "fixture" ? "新建演示任务" : "新建在线任务";
}
function lockInput(locked) {
  for (const id of ["mode", "demo", "max-tokens", "max-cost", "max-output", "url"]) $(id).disabled = locked;
  $("diff").readOnly = locked || $("mode").value === "fixture";
  $("diff-file").disabled = locked || $("mode").value === "fixture";
  $("upload-label").classList.toggle("disabled", $("diff-file").disabled);
  $("input-title").textContent = locked ? "当前任务 · 冻结输入" : "审阅输入";
}
function renderInput(data) {
  if (!data?.summary) return;
  lockInput(true);
  $("mode").value = data.job.mode; $("demo").value = data.job.demo;
  $("demo-settings").hidden = data.job.mode !== "fixture"; $("configure-inline").hidden = true;
  $("diff").value = data.safe_diff || ""; countLines();
  $("url").value = data.summary.source?.url || "";
  $("url-tab").disabled = !data.summary.source;
  if (!data.summary.source) setInput("diff");
  $("mode-help").textContent = "当前任务已冻结，下面展示的是安全输入快照。修改输入或预算需要新建任务。";
  $("submit-help").textContent = data.job.mode === "fixture" ? "演示结果来自预置回复，不评价真实代码质量。" : "任务预算与 HELD 保留，页面刷新不会自动恢复或重发。";
  $("max-tokens").value = data.budget.max_tokens;
  $("max-cost").value = String(data.budget.max_cost_nusd / 1e9);
  $("max-output").value = data.budget.max_output_tokens;
  $("budget-caption").textContent = number(data.budget.max_tokens) + " tokens / $" + $("max-cost").value;
}
function renderHistory(jobs) {
  state.history = jobs;
  for (const [group, mode] of [["live", "deepseek"], ["demo", "fixture"]]) {
    const list = jobs.filter(j => j.mode === mode), nav = $(group + "-history"), scroll = nav.scrollTop;
    $(group + "-count").textContent = list.length;
    const rows = list.map(job => {
      const title = mode === "fixture" ? state.config.demos.find(d => d.id === job.demo)?.title : job.input_type === "url" ? "PR / MR 审阅" : "代码 Diff 审阅";
      const b = button("", () => { selectJob(job.job_id); showPane("run"); closeHistory(); }, "history-item" + (job.job_id === state.selected ? " active" : ""));
      b.append(el("strong", title), el("small", short(job.task_id || job.job_id) + " · " + label(job.runner))); return b;
    });
    nav.replaceChildren(...(rows.length ? rows : [el("p", mode === "fixture" ? "预置回复 · 无真实调用" : "自己的代码 · 真实模型审阅", "sidebar-empty")]));
    nav.scrollTop = scroll;
  }
}
function metric(title, value, suffix, note) {
  const card = el("div", null, "metric"), line = el("div");
  line.append(el("strong", value), el("em", suffix));
  card.append(el("small", title), line, el("p", note)); return card;
}
function renderSteps(data) {
  const s = data?.summary, units = s?.units || [], hasValidation = units.some(u => !["PENDING", "RUNNING"].includes(u.status));
  const terminal = s && ["COMPLETED", "PARTIAL", "BLOCKED_SECURITY"].includes(s.status);
  const stage = terminal ? 4 : data?.active && s ? 2 : hasValidation ? 3 : s ? 2 : data ? 0 : -1;
  $("steps").replaceChildren(...["输入准备", "安全快照", "审阅循环", "证据校验", "报告汇总"].map((title, i) => {
    const done = (i < 2 && Boolean(s)) || (terminal && i < 4) || (terminal && s.status === "COMPLETED" && i === 4);
    const box = el("div", null, "step" + (done ? " done" : i === stage ? " current" : ""));
    box.append(el("b", done ? "✓" : String(i + 1)), el("span", title)); return box;
  }));
}
function renderRun(data) {
  const summary = data?.summary, totals = summary?.totals || {}, view = data?.view;
  const status = data?.job.runner === "FAILED" || data?.job.runner === "INTERRUPTED" ? data.job.runner : summary?.status || data?.job.runner;
  $("run-badge").textContent = status ? label(status) : "等待输入"; $("run-badge").className = "badge " + tone(status);
  renderSteps(data);
  renderInput(data);
  const units = summary?.units || [], done = units.filter(u => u.status === "DONE").length;
  $("metrics").replaceChildren(
    metric("已审阅单元", String(done), " / " + units.length, data ? `${data.files?.length || 0} 个变更文件` : "等待输入快照"),
    metric("模型发送", String(view?.marked_sends || 0), " 次", `${view?.tool_count || 0} 次工具调用`),
    metric("已记账 Tokens", number(totals.settled_tokens), "", "HELD " + number(totals.held_tokens)),
    metric(data?.job.mode === "fixture" ? "模拟费用 / USD" : "费用估算 / USD", usd(totals.settled_cost_nusd), "", "HELD " + usd(totals.held_cost_nusd))
  );
  $("run-message").textContent = !data ? "提交后，这里会自动展示实际执行状态。" :
    data.job.error_code ? errorNames[data.job.error_code] || "执行停止：" + data.job.error_code :
    data.job.runner === "INTERRUPTED" ? "服务曾中断。已提交记录保留，任务不会自动恢复或重发。" :
    !summary ? "正在校验输入、准备来源并冻结安全快照。原始输入不会写入工作台历史。" :
    `${data.job.mode === "fixture" ? "离线演示 · 预置回复" : "真实审阅"} · ${short(summary.task_id)} · ${label(summary.status)}${data.redactions ? " · 已脱敏 " + data.redactions + " 处" : ""}`;
  $("live-text").textContent = data?.active ? "实时更新" : data ? "已保存记录" : "等待运行";
  $("live-text").parentElement.classList.toggle("active", Boolean(data?.active));
  $("calls-count").textContent = view?.nodes.length || 0;
  if (view?.nodes.length) {
    const scroll = $("calls").scrollTop;
    $("calls").replaceChildren(...view.nodes.map(node => {
      const b = button("", () => inspect(node.id), "call" + (node.id === state.selectedNode ? " selected" : ""));
      const top = el("div", null, "call-top"), tool = node.kind === "TOOL", repair = node.kind === "REPAIR";
      top.append(el("span", tool ? "⌘" : repair ? "↻" : "✦", "call-icon" + (tool ? " tool" : repair ? " repair" : "")),
        el("span", tool ? node.name || "工具调用" : `${label(node.kind)}${node.turn != null ? " · 第 " + (node.turn + 1) + " 轮" : ""}`, "call-name"), badge(node.status));
      const resultStatus = node.raw?.result?.result_status;
      b.append(top, el("p", `${node.unit || "—"} · ${short(node.id)}${node.attempt_no ? " · attempt " + node.attempt_no : ""}${resultStatus ? " · " + label(resultStatus) : ""}`, "call-meta"));
      const relations = view.relations.filter(r => r.to === node.id);
      if (relations.length) b.append(el("p", relations.map(r => `${r.kind} ← ${short(r.from || r.ref)}`).join(" · "), "call-relationship"));
      return b;
    }));
    $("calls").scrollTop = scroll;
  } else {
    const empty = el("div", null, "empty-state");
    empty.append(el("div", "↗", "empty-icon"), el("h3", data ? "任务准备中" : "每一步，都有迹可循"),
      el("p", data ? "模型调用和工具执行将在提交记录后显示。" : "提交代码变更后，模型、工具和格式修复会自动出现在这里。"));
    if (summary && !data.active) empty.lastChild.textContent = "当前任务没有已记录的模型或工具调用。";
    $("calls").replaceChildren(empty);
  }
  const eventScroll = $("events").scrollTop;
  $("events").replaceChildren(el("p", "只列出观察到的已提交状态；时间是工作台观察时间，不是模型内部步骤耗时。", "event-note"), ...(data?.events || []).map(event => {
    const row = el("div", null, "event");
    row.append(el("strong", event.label || `${label(event.kind)} · ${label(event.status)}`),
      el("small", `${new Date(event.observed_at).toLocaleTimeString()}${event.id ? " · " + short(event.id) : ""}${event.detail ? " · " + label(event.detail) : ""}`)); return row;
  }));
  $("events").scrollTop = eventScroll;
  const recoverable = data && !data.active && summary && ["RUNNING", "PAUSED_UNKNOWN", "PAUSED_BUDGET"].includes(summary.status);
  $("recovery").hidden = !recoverable;
  const unknown = latestUnknown(data);
  $("retry").hidden = !unknown;
  $("recovery-text").textContent = unknown ? "调用结果未知。普通恢复不会重发；明确重试会新增调用，原 HELD 保留。" :
    summary?.status === "PAUSED_BUDGET" ? "任务已因预算暂停。冻结限额不会在恢复时重置，当前页面不能加额绕过限制。" : "可从已有 checkpoint 继续；已提交调用和预算将被复用。";
  document.querySelectorAll("[data-download]").forEach(b => { b.disabled = !summary; });
  if (state.selectedNode) renderInspector();
  refreshButtons();
}
function latestUnknown(data) {
  const decisions = data?.summary?.retry_decisions || [];
  return (data?.summary?.attempts || []).findLast(a => a.call_status === "UNKNOWN" && !decisions.some(d => d.source_attempt_id === a.attempt_id && d.status === "BOUND"));
}
function inspect(id) {
  state.selectedNode = id; state.detailTab = "request"; renderInspector();
  document.querySelectorAll(".call").forEach(b => b.classList.remove("selected"));
  showPane("run"); $("close-inspector").focus({ preventScroll: true });
}
function renderInspector() {
  const view = state.detail?.view, node = view?.nodes.find(n => n.id === state.selectedNode);
  if (!node) { $("inspector").hidden = true; return; }
  $("inspector").hidden = false; $("inspector-title").textContent = (node.name || label(node.kind)) + " · " + short(node.id);
  const tabs = { request: "请求 / 参数", result: "原安全回复 / 结果", evidence: "证据", budget: "预算", relations: "关联关系" };
  $("inspector-tabs").replaceChildren(...Object.entries(tabs).map(([key, title]) => button(title, () => {
    state.detailTab = key; renderInspector();
  }, key === state.detailTab ? "selected" : "")));
  const pre = el("pre", null, "json-text"); let value;
  if (state.detailTab === "request") value = node.raw?.request || "尚未保存请求";
  else if (state.detailTab === "result") value = node.raw?.result || "尚未收到已保存结果";
  else if (state.detailTab === "budget") value = { task_budget: state.detail.budget, quote: node.raw?.quote, attempt: node.raw?.attempt, timing: node.timing, task_totals: state.detail.summary.totals };
  else if (state.detailTab === "relations") value = view.relations.filter(r => r.from === node.id || r.to === node.id);
  else value = view.findings.filter(f => f.node_id === node.id).map(f => ({ finding: f.id, evidence: f.evidence, validation: f.validation }));
  pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  $("inspector-body").replaceChildren(el("p", state.detailTab === "evidence" ? "评论锚点、支持证据与契约证据分开记录；未关联评论时此列表为空，可在请求中查看实际输入。" : "内容来自已保存的安全记录。工具文本和模型正文仅作为文本展示。", "detail-label"), pre);
}
function renderReport(data) {
  const s = data?.summary, panel = $("report-content");
  $("report-status").textContent = s ? label(s.status) : "尚未生成"; $("report-status").className = "badge " + tone(s?.status);
  if (!s) { panel.className = "report-empty"; panel.textContent = "完成审阅后，这里展示代码位置、问题和修改建议。"; return; }
  panel.className = "";
  const findings = data.view.findings, formal = findings.filter(f => f.data.confidence !== "reference");
  const levels = { high: "高", medium: "中", low: "低", reference: "仅供参考" };
  const card = f => {
    const box = el("article", null, "finding"), title = el("div", null, "finding-title"), d = f.data;
    title.append(el("span", "严重性 " + levels[d.severity], "badge " + (d.severity === "high" ? "failed" : "warning")), el("h3", d.title), el("span", "置信度 " + levels[d.confidence], "badge neutral"));
    const dl = el("dl");
    for (const [key, caption] of [["trigger", "触发条件"], ["actual_behavior", "问题"], ["impact", "影响"], ["suggestion", "修改建议"]]) {
      if (d[key]) dl.append(el("dt", caption), el("dd", d[key]));
    }
    if (d.truncated_source) dl.append(el("dt", "说明"), el("dd", "该条来自截断回复，审阅尚未完整结束。"));
    const location = el("p", `${data.hunk_paths[d.hunk_id]} · ${d.side === "new" ? "变更后" : "变更前"}第 ${d.line} 行`, "finding-location");
    box.append(location, title, dl); return box;
  };
  const heading = el("div", null, "report-summary");
  heading.append(el("strong", "建议修改的问题"), el("p", data.review_scope_note));
  if (data.job.mode === "fixture") heading.append(el("p", "离线演示结果，不代表真实模型审阅质量。"));
  const cards = formal.map(card);
  if (!formal.length) heading.append(el("p", `暂无正式修改建议 · ${label(s.status)}；零评论不代表代码没有缺陷。`));
  const references = findings.filter(f => f.data.confidence === "reference");
  if (references.length) {
    const referenceHeading = el("div", null, "report-summary");
    referenceHeading.append(el("strong", "仅供参考（尚未确认为缺陷）"));
    cards.push(referenceHeading, ...references.map(card));
  }
  const coverage = el("div", null, "coverage");
  const excludedReasons = { BINARY: "二进制文件", NO_PYTHON_TEXT_CHANGE: "没有 Python 文本变更" };
  for (const f of data.excluded || []) { const box = el("div", null, "unit"); box.append(el("strong", f.path), document.createTextNode("未审阅"), el("p", excludedReasons[f.reason])); coverage.append(box); }
  panel.replaceChildren(heading, ...cards, coverage);
}
async function poll() {
  try {
    if (!state.token) {
      const config = await api("/api/bootstrap"); state.token = config.token;
      if (!state.config) {
        state.config = config;
        $("demo").replaceChildren(...config.demos.map(d => { const o = el("option", d.title); o.value = d.id; return o; }));
        $("demo").value = "tools"; setMode();
        state.selected = remembered();
      }
      applySettings(config);
    }
    if (!state.settingsBusy) applySettings(await api("/api/settings"));
    const list = await api("/api/jobs"); state.active = list.active_job;
    renderHistory(list.jobs);
    const selected = state.selected;
    if (selected && !state.busy) {
      const detail = await api("/api/jobs/" + selected);
      if (state.selected === selected) {
        const signature = JSON.stringify(detail);
        state.detail = detail;
        if (signature !== state.signature) { state.signature = signature; renderRun(detail); renderReport(detail); }
        $("breadcrumb").textContent = (detail.job.mode === "fixture" ? "演示任务 / " : "在线任务 / ") + short(detail.job.task_id || selected);
      }
    }
    connected(true); refreshButtons();
  } catch (error) {
    connected(false);
    if (error.message === "SESSION_REQUIRED") { state.token = ""; }
    else if (error.message === "JOB_NOT_FOUND") { newTask(); }
  } finally { setTimeout(poll, 750); }
}
$("review-form").addEventListener("submit", async event => {
  event.preventDefault(); if (state.active || state.busy) return;
  if (state.detail) { newTask(); return; }
  clearError(); state.busy = true;
  const id = state.pending || crypto.randomUUID(); state.pending = id; selectJob(id); refreshButtons();
  try {
    const job = await api("/api/jobs", { submission_id: id, mode: $("mode").value, input_type: state.inputType,
      content: state.inputType === "diff" ? $("diff").value : $("url").value,
      demo: $("demo").value, max_tokens: Number($("max-tokens").value), max_cost_usd: $("max-cost").value,
      max_output_tokens: Number($("max-output").value) });
    state.pending = null; state.active = job.job_id; showPane("run");
  } catch (error) { showError(error); if (!(error instanceof TypeError)) state.pending = null; }
  finally { state.busy = false; refreshButtons(); }
});
async function resume(retryUnknown = null) {
  if (!state.selected || state.active || state.busy) return;
  state.busy = true; refreshButtons(); clearError();
  try {
    await api(`/api/jobs/${state.selected}/resume`, { action_id: crypto.randomUUID(), retry_unknown: retryUnknown });
    state.active = state.selected;
  } catch (error) { showError(error); }
  finally { state.busy = false; refreshButtons(); }
}
$("resume").addEventListener("click", () => resume());
$("retry").addEventListener("click", () => $("retry-dialog").showModal());
$("retry-cancel").addEventListener("click", () => $("retry-dialog").close());
$("retry-confirm").addEventListener("click", () => {
  const attempt = latestUnknown(state.detail); $("retry-dialog").close(); if (attempt) resume(attempt.attempt_id);
});
$("new-task").addEventListener("click", () => newTask("fixture"));
$("new-live").addEventListener("click", () => newTask("deepseek"));
$("history-toggle").addEventListener("click", () => {
  const open = document.body.classList.toggle("history-open"); $("history-toggle").setAttribute("aria-expanded", String(open));
});
document.querySelectorAll(".pane-tabs button").forEach(b => b.addEventListener("click", () => showPane(b.dataset.pane)));
$("api-settings").addEventListener("click", openSettings); $("configure-inline").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("settings-dialog").addEventListener("close", () => { $("api-key").value = ""; });
async function configure(body) {
  state.settingsBusy = true; refreshButtons(); $("settings-error").hidden = true;
  try { applySettings(await api("/api/settings", body)); }
  catch (_) { $("settings-error").textContent = "设置未保存，请检查 key 格式及连接，并等待当前任务结束。不会自动重发。"; $("settings-error").hidden = false; }
  finally { state.settingsBusy = false; refreshButtons(); }
}
$("settings-form").addEventListener("submit", async event => {
  event.preventDefault(); if (state.settingsBusy || state.active || state.busy) return;
  const body = { action: "save", api_key: $("api-key").value }; $("api-key").value = "";
  await configure(body);
});
$("clear-key").addEventListener("click", () => { $("api-key").value = ""; configure({ action: "clear" }); });
$("mode").addEventListener("change", () => newTask($("mode").value)); $("demo").addEventListener("change", loadDemo);
$("diff-tab").addEventListener("click", () => setInput("diff")); $("url-tab").addEventListener("click", () => setInput("url"));
$("diff").addEventListener("input", countLines);
$("diff-file").addEventListener("change", async () => {
  const file = $("diff-file").files[0]; if (!file) return;
  if (file.size > 1048576) { showError(new Error("INPUT_TOO_LARGE")); return; }
  try { $("diff").value = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); countLines(); }
  catch (_) { showError(new Error("INVALID_INPUT")); }
});
for (const id of ["max-tokens", "max-cost"]) $(id).addEventListener("input", () => {
  $("budget-caption").textContent = number($("max-tokens").value) + " tokens / $" + $("max-cost").value;
});
for (const type of ["calls", "events"]) $(type + "-tab").addEventListener("click", () => {
  for (const other of ["calls", "events"]) { $(other + "-tab").setAttribute("aria-selected", String(other === type)); $(other).hidden = other !== type; }
});
$("close-inspector").addEventListener("click", () => { state.selectedNode = null; $("inspector").hidden = true; });
document.querySelectorAll("[data-download]").forEach(b => b.addEventListener("click", () => {
  if (!state.selected) return;
  const a = el("a"); a.href = `/api/jobs/${state.selected}/${b.dataset.download}`;
  a.download = short(state.selected) + "-" + b.dataset.download;
  document.body.append(a); a.click(); a.remove();
}));
renderRun(null); poll();
