"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const make = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  };
  const object = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
  const array = x => Array.isArray(x) ? x : [];
  const pretty = x => typeof x === "string" ? x : JSON.stringify(x, null, 2);
  const shown = x => x === undefined || x === null ? "未提供" : typeof x === "object" ? pretty(x) : String(x);
  const number = x => Number.isSafeInteger(x) ? x.toLocaleString("en-US") : "未提供";
  const money = x => Number.isSafeInteger(x) ? "$" + (x / 1e9).toFixed(9) : "未提供";
  const short = x => typeof x === "string" ? x.slice(0, 18) + (x.length > 18 ? "…" : "") : "未提供";
  const title = n => n.kind === "TOOL" ? shown(n.name) : n.kind === "PREPARED" ? "已准备 · 未创建 attempt" : `${n.kind} · attempt ${shown(n.attempt_no)}`;
  const statusStyle = s => /^(COMPLETED|SUCCEEDED|SETTLED|DONE|VALID)$/.test(s) ? "good" : /UNKNOWN|HELD|PENDING|RESERVED|TRUNCAT|INTERRUPT/.test(s) ? "warn" : /INVALID|FAIL|ERROR|REJECT/.test(s) ? "bad" : "";
  const badge = s => make("span", shown(s), "badge " + statusStyle(String(s)));
  const code = (parent, value) => parent.append(make("pre", shown(value), "code-block"));
  const note = (parent, value, warn = false) => parent.append(make("p", value, "note" + (warn ? " warn" : "")));
  const section = (parent, label) => { const s = make("section", null, "section"); s.append(make("h3", label, "section-title")); parent.append(s); return s; };
  const kv = (parent, entries) => {
    const dl = make("dl", null, "kv");
    for (const [k, v] of entries) { dl.append(make("dt", k), make("dd", shown(v))); }
    parent.append(dl);
  };
  const disclosure = (parent, label, value) => {
    const d = make("details", null, "disclosure"); d.append(make("summary", label));
    let loaded = false;
    d.addEventListener("toggle", () => { if (d.open && !loaded) { code(d, value); loaded = true; } });
    parent.append(d);
  };
  try {
    const bundle = JSON.parse($("trace-data").textContent);
    const {trace: trace, view: view} = bundle;
    const nodes = view.nodes, findings = view.findings, relations = view.relations;
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const findingMap = new Map(findings.map(f => [f.id, f]));
    const pricing = object(trace.pricing), totals = object(trace.task_totals), limits = object(pricing.limits);
    let selected = nodes.length ? {type: "node", id: nodes[0].id} : null;
    let activeTab = "summary";
    const scope = object(trace.scope);
    const scoped = Boolean(scope.finding_id || scope.attempt_id);
    const scopeLabel = scope.finding_id ? "单条评论导出" : scope.attempt_id ? "单个 attempt 导出" : "任务导出";
    const fullSections = ["budget_events", "pricing", "spans"].every(k => Object.hasOwn(trace, k)) &&
      (Object.hasOwn(trace, "findings") || Object.hasOwn(trace, "finding"));
    const budgetEvents = array(trace.budget_events);
    const searchIndex = new Map(nodes.map(n => [n.id, JSON.stringify(n).toLowerCase()]));
    $("identity").textContent = `${trace.task_id}  /  ${trace.trace_id}`;
    $("mode").textContent = `${trace.execution_mode === "fixture" ? "FIXTURE · 流程样例" : "历史模型调用 · 只读"}\n${scopeLabel} · ${fullSections ? "含完整审计区段" : "部分审计区段"}`;
    const metric = (label, value, hint, cls = "") => {
      const m = make("div", null, "metric " + cls);
      m.append(make("div", label, "metric-label"), make("div", value, "metric-value" + (cls.includes("money") ? " money" : "")), make("div", hint, "metric-note"));
      $("overview").append(m);
    };
    metric("已标记发送", "calls" in trace || "attempt" in trace ? number(view.marked_sends) : "未提供", `导出内 ${number(view.attempt_count)} attempts；RESERVED 不算发送`);
    metric("工具调用", "tool_calls" in trace || nodes.some(n => n.kind === "TOOL") ? number(view.tool_count) : "未提供", "重复引用按 tool_call_id 去重");
    metric("已结算 · 任务级", money(totals.settled_cost_nusd), `${number(totals.settled_tokens)} tokens · 本地账本估算`, "money");
    metric("HELD · 任务级", money(totals.held_cost_nusd), `${number(totals.held_tokens)} tokens · UNKNOWN 仍保留预留`, "money held");
    const meta = $("metadata");
    kv(meta, [["导出范围", `${scopeLabel}${fullSections ? "" : " / 部分区段"}`], ["任务状态／完整覆盖", "当前 trace 未导出；不从零评论或调用状态推断"], ["输入 SHA-256", bundle.input_sha256], ["Config digest", trace.config_digest], ["Prompt digest", pricing.prompt_digest], ["Review 版本 / 协议", `${shown(pricing.review_version)} / ${shown(pricing.reply_protocol)}`], ["Token / 金额上限", `${number(limits.max_tokens)} / ${money(limits.max_cost_nusd)}`], ["每单元发送 / REPAIR / 工具上限", `${shown(limits.max_sends_per_unit)} / ${shown(limits.max_repairs_per_unit)} / ${shown(limits.max_tools_per_unit)}`], ["输出上限", limits.max_output_tokens], ["批次预算", "当前 trace 未包含完整批次账本，不能据此重建批次余额"], ["来源覆盖", object(trace.source).coverage], ["停发标记", Object.hasOwn(trace, "send_block") ? trace.send_block : "未导出"]]);
    const requestedModels = [...new Set(nodes.map(n => object(object(n.raw.request).data).model).filter(Boolean))];
    const responseModels = [...new Set(nodes.map(n => object(n.raw.result).provider_model).filter(Boolean))];
    const validationCounts = new Map();
    for (const record of array(trace.validations)) {
      const status = object(record.data).status;
      if (status) validationCounts.set(status, (validationCounts.get(status) || 0) + 1);
    }
    if (trace.validation) validationCounts.set(object(trace.validation).status || "未提供", 1);
    kv(meta, [["请求模型（导出内）", requestedModels.length ? requestedModels : (trace.execution_mode === "fixture" ? "fixture" : null)], ["响应模型（提供方字段）", responseModels.length ? responseModels : null], ["校验状态（记录条数）", validationCounts.size ? [...validationCounts].map(([key, count]) => `${key} × ${count}`).join(" / ") : "未提供"]]);
    note(meta, "查看器只投影已导出事实，不重新审计数据库或判分。时间戳缺失不等于耗时为零；当前 trace 未导出每次 resume / checkpoint 事件。局部调用与证据统计不能代表整个任务。");
    disclosure(meta, "来源版本与文件覆盖（如已导出）", trace.source);
    disclosure(meta, "冻结价格、预算与追加费用复核", pricing);
    disclosure(meta, "原安全 trace（只读）", trace);
    const fillFilter = (id, values) => { for (const value of [...new Set(values.filter(v => v !== null && v !== undefined))]) { const option = make("option", value); option.value = String(value); $(id).append(option); } };
    fillFilter("unit-filter", nodes.map(n => n.unit)); fillFilter("kind-filter", nodes.map(n => n.kind)); fillFilter("status-filter", nodes.map(n => n.status));
    function jump(parent, id, label) {
      if (!nodeMap.has(id)) { parent.append(make("span", label + " · 本导出未包含", "muted small")); return; }
      const button = make("button", label, "jump");
      button.addEventListener("click", () => {
        for (const filter of ["search", "unit-filter", "kind-filter", "status-filter"]) $(filter).value = "";
        selected = {type:"node", id:id}; activeTab = "summary"; render();
      }); parent.append(button);
    }
    function renderTimeline() {
      const list = $("timeline"); list.replaceChildren();
      const query = $("search").value.toLowerCase();
      const visible = nodes.filter(n => (!$("unit-filter").value || n.unit === $("unit-filter").value) && (!$("kind-filter").value || n.kind === $("kind-filter").value) && (!$("status-filter").value || n.status === $("status-filter").value) && searchIndex.get(n.id).includes(query));
      $("node-count").textContent = `${visible.length} / ${nodes.length}`;
      for (const n of visible) {
        const button = make("button", null, "timeline-item"); button.setAttribute("aria-pressed", String(selected?.type === "node" && selected.id === n.id)); button.dataset.nodeId = n.id;
        const heading = make("div", null, "node-title"); heading.append(make("span", String(nodes.indexOf(n)+1).padStart(2,"0"), "node-index"), make("span", title(n))); button.append(heading);
        button.append(make("div", `${shown(n.unit)} · ${n.turn == null ? "轮次未提供" : "轮次 " + n.turn}`, "node-sub"), badge(n.status));
        if (n.kind === "TOOL" || n.kind === "REPAIR") button.append(make("span", " " + n.kind, "badge " + n.kind.toLowerCase()));
        button.append(make("div", n.timing.dispatched_at || "绝对时间未提供", "node-time"));
        button.addEventListener("click", () => { selected = {type:"node",id:n.id}; activeTab = "summary"; render(); }); list.append(button);
      }
      if (!visible.length) list.append(make("p", nodes.length ? "没有匹配的调用。可调整筛选。" : "本导出没有调用节点；请查看概览的区段说明。", "empty"));
    }
    function renderFindings() {
      $("finding-count").textContent = Object.hasOwn(trace,"findings") || Object.hasOwn(trace,"finding") ? String(findings.length) : "未导出";
      $("findings").replaceChildren();
      for (const f of findings) {
        const b = make("button", null, "finding-card"); b.dataset.findingId = f.id; b.setAttribute("aria-pressed", String(selected?.type === "finding" && selected.id === f.id));
        b.append(badge(f.data.confidence), make("div", f.data.title || "评论正文无法结构化；保留原文", "finding-title"), make("div", `${shown(f.raw.unit_id)} · severity ${shown(f.data.severity)}`, "finding-sub"));
        b.addEventListener("click", () => { selected = {type:"finding",id:f.id}; activeTab = "summary"; render(); }); $("findings").append(b);
      }
      if (!findings.length) $("findings").append(make("p", Object.hasOwn(trace,"findings") ? "本导出无接受评论。是否完成、abstain、截断或未覆盖须查看原校验记录；不代表零缺陷。" : "评论区段未导出。", "empty"));
    }
    function renderTabs(labels) {
      $("tabs").replaceChildren();
      for (const [id, label] of labels) { const b = make("button",label,"tab"); b.setAttribute("role","tab"); b.setAttribute("aria-selected",String(id===activeTab)); b.addEventListener("click",()=>{activeTab=id;renderDetail();}); $("tabs").append(b); }
    }
    function related(parent, id) {
      const found = relations.filter(r => r.from === id || r.to === id);
      if (!found.length) note(parent,"当前导出没有该节点的 REPAIR、工具或持久化重试关联。");
      for (const r of found) {
        const box = make("div",null,"relation"); box.append(badge(r.kind));
        const links = make("div",null,"relation-links");
        jump(links,r.from,r.from ? title(nodeMap.get(r.from)) : "来源节点"); links.append(make("span","→","muted")); jump(links,r.to,r.to ? title(nodeMap.get(r.to)) : "目标节点"); box.append(links);
        box.append(make("p",`绑定引用：${shown(r.ref)}`,"mono"));
        if (r.kind === "RETRY") { kv(box,[["Decision 状态", object(r.detail).status],["选择时间",object(r.detail).created_at]]); note(box,"原 UNKNOWN 的 HELD 保持原账本状态；绑定新 attempt 不代表释放旧预留。"); }
        if (r.detail) disclosure(box,"原关系记录",r.detail);
        parent.append(box);
      }
      note(parent,"恢复执行事件未导出：这里只展示已持久化的引用与授权绑定，不推断 checkpoint 游标、重放次数或重放时间。");
    }
    function budget(parent, n) {
      const attempt = object(n.raw.attempt);
      kv(parent,[["Fee 状态",attempt.fee_status],["Quote tokens / 金额",`${number(attempt.quote_tokens)} / ${money(attempt.quote_cost_nusd)}`],["实际记账 tokens / 金额",`${number(attempt.actual_tokens)} / ${money(attempt.actual_cost_nusd)}`],["本 attempt HELD",attempt.fee_status === "HELD" ? `${number(attempt.quote_tokens)} / ${money(attempt.quote_cost_nusd)}` : attempt.fee_status ? "无 HELD" : "未提供"],["任务级已结算",`${number(totals.settled_tokens)} / ${money(totals.settled_cost_nusd)}`],["任务级 HELD",`${number(totals.held_tokens)} / ${money(totals.held_cost_nusd)}`]]);
      note(parent,"RESERVE / SETTLE / RELEASE 是事件，不可直接相加当作当前占用。任务余额直接来自 task_totals；费用为本地账本口径，未与提供方账单核对。工具使用次数单独计算，后续模型轮次与 REPAIR 计入模型预算。");
      const events = budgetEvents.filter(e=>e.attempt_id===n.id);
      const wrap = make("div",null,"budget-wrap"), table=make("table",null,"budget-table");
      const head=make("tr"); for(const text of ["预算事件","Tokens","USD"])head.append(make("th",text));const thead=make("thead");thead.append(head);table.append(thead);
      const body=make("tbody"); for(const e of events){const row=make("tr");for(const text of [e.event_type,number(e.tokens),money(e.cost_nusd)])row.append(make("td",text));body.append(row);} table.append(body);wrap.append(table);parent.append(wrap);
      if(!events.length)note(parent,"该 attempt 的预算事件未包含在此导出。");
      disclosure(parent,"原 quote",n.raw.quote); disclosure(parent,"原 usage",object(n.raw.result).usage);
      disclosure(parent,"该 attempt 的追加费用复核",array(pricing.pricing_reviews).filter(r=>r.attempt_id===n.id)); disclosure(parent,"冻结价格与限制",pricing);
    }
    function nodeSummary(parent,n) {
      if(n.kind==="TOOL"){
        const request=object(n.raw.request), call=object(n.raw.call), result=object(n.raw.result), spec=object(object(request.tool_identity).spec);
        kv(parent,[["工具",n.name],["状态",n.status],["身份版本",spec.version],["实现 digest",object(request.tool_identity).implementation_digest],["权限",spec.permissions],["参数",request.arguments],["超时 / 输出上限",`${shown(spec.timeout_ms)} ms / ${shown(spec.max_output_bytes)} bytes`],["调用名额",call.slot_no],["结果完整",result.complete],["记录条数",Array.isArray(result.records)?result.records.length:null],["错误码",result.error_code],["工具耗时", "绝对开始／结束时间未导出"]]);
        jump(parent,relations.find(r=>r.kind==="TOOL_REQUEST"&&r.to===n.id)?.from,"查看触发工具的模型调用");
      }else{
        const a=object(n.raw.attempt), r=object(n.raw.result), request=object(object(n.raw.request).data);
        kv(parent,[["单元 / 轮次",`${shown(n.unit)} / ${shown(n.turn)}`],["调用 / 结果 / 费用",`${shown(n.status)} / ${shown(a.result_status)} / ${shown(a.fee_status)}`],["请求模型",request.model || (trace.execution_mode==="fixture"?"fixture":null)],["响应模型",r.provider_model],["Provider request ID",r.provider_request_id],["System fingerprint",r.system_fingerprint],["操作 ID",n.operation_id],["请求 artifact",n.request_ref],["结果 artifact",n.result_ref],["完成 / 截断",`${shown(r.completion_state)} / ${shown(r.truncation_reason)}`],["错误码",a.error_code || r.error_code],["原决策 action",object(r.decision).action],["原 reason",object(r.decision).reason],["发送标记时间",n.timing.dispatched_at],["完成记录时间",n.timing.completed_at],["记录时间差",n.timing.elapsed_ms===null?"未提供":`${n.timing.elapsed_ms} ms（不是服务端独立耗时）`]]);
        const validationIds=new Set(findings.filter(f=>f.node_id===n.id).map(f=>f.raw.validation_ref));
        const validations=array(trace.validations).filter(v=>validationIds.has(v.artifact_id));
        // All exported validation records remain inspectable even for zero findings.
        disclosure(parent,"导出内原校验记录（含零评论／拒绝候选）", validations.length?validations:(trace.validations??trace.validation));
        if(n.status==="UNKNOWN")note(parent,"调用结果不明。查看器不会重试或释放 HELD；下方关系仅显示已有人工选择。",true);
        if(n.kind==="PREPARED")note(parent,"请求已准备，但没有 attempt；不能当作模型已发送。");
      }
      const s=section(parent,"调用关联");related(s,n.id);
    }
    function renderEvidence(parent,f){
      if(!f.evidence.length)note(parent,"未包含结构化证据关联；保留原评论，不补改引用。");
      for(const e of f.evidence){
        const card=make("div",null,"evidence-card"), h=make("div",null,"evidence-title");
        const label={anchor:"评论锚点",evidence:"支持证据",expectation:"契约证据"}[e.role]||shown(e.role);
        h.append(badge(label),make("code",e.ref));card.append(h);
        if(!e.matches.length)note(card,"所属请求与已输入工具结果中未找到该行；可能是局部导出缺失。未补造或追认引用。",true);
        for(const match of e.matches){card.append(make("p",match.origin,"evidence-source"));code(card,object(match.line).text);jump(card,match.node_id,"查看证据所属调用");}
        parent.append(card);
      }
      note(parent,"锚点定位被评论的变更行；支持与契约证据可位于已提供的未修改行。展示结果沿用原校验，不作新的命中判定。");
    }
    function renderDetail(){
      const header=$("selection-header"),content=$("detail-content");header.replaceChildren();content.replaceChildren();
      if(!selected){$("tabs").replaceChildren();header.append(make("h2","选择一个调用或评论"));note(content,"当前导出可能仅含价格／预算区段。任务元数据仍可从上方展开。");return;}
      const isFinding=selected.type==="finding";
      const f=isFinding?findingMap.get(selected.id):null, n=isFinding?nodeMap.get(f.node_id):nodeMap.get(selected.id);
      header.append(make("div",isFinding?"FINDING / ORIGINAL ACCEPTED":"CALL / "+n.kind,"selection-kicker"),make("h2",isFinding?(f.data.title||"原接受评论"):title(n),"selection-title"),make("div",selected.id,"selection-id mono"));
      if(isFinding){
        renderTabs([["summary","评论正文"],["evidence","锚点与证据"],["relations","关联调用"],["raw","原记录"]]);
        if(activeTab==="summary"){
          kv(content,[["原置信度 / 严重性",`${shown(f.data.confidence)} / ${shown(f.data.severity)}`],["单元",f.raw.unit_id],["降级原因",f.data.confidence_reason],["截断来源",f.data.truncated_source]]);
          for(const [key,label] of [["trigger","触发条件"],["introduced_by","变更如何引入"],["actual_behavior","实际行为"],["expected_behavior","期望行为"],["impact","不利影响"],["suggestion","建议"]]){const s=section(content,label);s.append(make("p",shown(f.data[key]),"text-block"));}
          jump(content,f.node_id,"查看所属请求、原回复与预算");
        }else if(activeTab==="evidence")renderEvidence(content,f);
        else if(activeTab==="relations"){jump(content,f.node_id,"查看所属模型调用");if(n)related(content,n.id);disclosure(content,"评论的原校验记录",f.validation);}
        else{code(content,f.raw);disclosure(content,"原校验记录",f.validation);}
        return;
      }
      renderTabs([["summary","概览与关系"],["request","请求"],["result","回复 / 结果"],["budget","预算"],["raw","原记录"]]);
      if(activeTab==="summary")nodeSummary(content,n);
      else if(activeTab==="request"){
        if(n.kind==="TOOL"){kv(content,[["触发请求",object(n.raw.request).source_request_ref],["触发结果",object(n.raw.request).source_result_ref]]);code(content,n.raw.request);}
        else {kv(content,[["请求 artifact",n.request_ref],["请求 digest",object(n.raw.request).request_digest]]);code(content,object(n.raw.request).data);if(n.raw.tool_inputs)disclosure(content,"本轮实际输入的工具结果",n.raw.tool_inputs);}
      }else if(activeTab==="result"){
        const result=object(n.raw.result);
        if(n.kind==="TOOL"){kv(content,[["状态",result.status],["完整",result.complete],["错误码",result.error_code]]);code(content,result.records);disclosure(content,"原工具结果",n.raw.result);}
        else{kv(content,[["结果状态",result.result_status],["Finish reason",result.provider_finish_reason],["完成状态",result.completion_state]]);code(content,result.safe_body);note(content,"以上为原 safe_body 文本；无效 JSON、重复键与截断正文均不重新解释。",false);disclosure(content,"原已解析结果与 usage",n.raw.result);}
      }else if(activeTab==="budget"){
        if(n.kind==="TOOL"){note(content,"工具不单独结算模型 token；其结果进入后续模型请求后按原调用记账。工具名额、权限及上限见概览。");related(content,n.id);}
        else if(n.kind==="PREPARED")note(content,"未创建 attempt，没有该请求的预留或结算事实。");else budget(content,n);
      }else code(content,n.raw);
    }
    function render(){renderTimeline();renderFindings();renderDetail();}
    for(const id of ["search","unit-filter","kind-filter","status-filter"])$(id).addEventListener(id==="search"?"input":"change",renderTimeline);
    render();
  }catch(_){$("error").hidden=false;$("error").textContent="查看器无法呈现此导出。请使用 CLI 重新生成；原 trace 未被修改。";}
})();
