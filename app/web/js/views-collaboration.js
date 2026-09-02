/* AgentTeams collaboration view: only persisted Run, Worker, Skill and usage facts. */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };
  function esc(value) { return UI().esc(value == null ? "—" : value); }

  function stageCards(stages) {
    var p = stages.precheck || {}, a = stages.agent || {}, h = stages.human || {};
    return '<div class="decision-stage-grid">' +
      '<div class="decision-stage"><small>① 确定性预检</small><b>' + esc(p.state || "NOT_RUN") + '</b><span>证据、契约和数值计算；不调用模型</span></div>' +
      '<div class="decision-stage"><small>② AgentTeams 建议</small><b>' + esc(a.state || "NOT_RUN") + '</b><span>' + esc(a.workers || "0/0") + ' Worker · 不拥有签署权</span></div>' +
      '<div class="decision-stage"><small>③ Human 最终授权</small><b>' + esc(h.state || "NOT_OPENED") + '</b><span>责任人、理由、时间和哈希写入同一Run</span></div>' +
    '</div>';
  }

  function runSelector(ws) {
    var items = ws.run_catalog || [], judge = ws.judge_run || {};
    var options = items.map(function (item) {
      return '<option value="' + esc(item.run_id) + '" title="审计ID：' + esc(item.audit_run_id || item.run_id) + '" ' + (item.run_id === ws.selected_run_id ? 'selected' : '') + '>' +
        esc(item.display_run_id || item.run_id) + ' · ' + esc(item.route || item.state) + ' · ' + esc(item.worker_progress) +
      '</option>';
    }).join('');
    var frozen = Boolean(judge.persisted && judge.run_id === ws.selected_run_id);
    return '<div class="judge-run-bar"><div><p class="eyebrow">JUDGE RUN CONTEXT</p><h3>' + esc(judge.display_run_id || judge.run_id || "尚无合格裁判Run") + '</h3>' + (judge.run_id ? '<p class="mono">审计ID：' + esc(judge.audit_run_id || judge.run_id) + '</p>' : '') + '<p>这是演示与验收上下文，不等于 Human 业务签署。</p></div><div class="judge-run-actions"><select id="run-context-select">' + options + '</select><button id="btn-select-run" class="btn btn-outline" type="button">切换查看</button><button id="btn-freeze-judge" class="btn btn-primary" type="button" ' + (judge.eligible && !frozen ? '' : 'disabled') + '><i class="ri-pushpin-line"></i> ' + (frozen ? '裁判Run已冻结' : '冻结为裁判Run') + '</button></div></div>';
  }

  function managerCard(run, route) {
    var decision = route.route_decision || {}, policy = decision.policy || {}, plan = decision.worker_plan || {};
    return '<section class="card collaboration-manager"><div class="card-head"><i class="ri-route-line card-icon"></i><h4>Manager 动态路由与任务分派</h4>' + UI().badge(decision.route || "NOT_CREATED") + '</div>' +
      UI().kv("Decision ID", '<span class="mono">' + esc(decision.decision_id || "NOT_CREATED") + '</span>') +
      UI().kv("策略", '<span class="mono">' + esc(policy.policy_id || "FINFLUX_MANAGER_ROUTE_POLICY") + '@' + esc(policy.version || "—") + '</span>') +
      UI().kv("路由原因", esc((decision.reason_codes || []).join(' / ') || '未生成')) +
      UI().kv("任务计划", '<b>' + esc(plan.count || 0) + ' 个专业Worker · ' + (plan.parallel ? '并行隔离' : '按需执行') + '</b>') +
      '<p class="card-note">Manager只读CaseEnvelope和结构化上下文，负责路由、预算与幂等派发；金融数值由确定性Skill计算。</p></section>';
  }

  function agentPool(runtime, route) {
    runtime = runtime || {};
    var resources = runtime.resources || {}, topology = runtime.topology || [];
    var selected = {};
    (route.workers || []).forEach(function (worker) { selected[worker.agent_id] = true; });
    var agents = topology.filter(function (item) {
      return item.role === "worker" || item.role === "post_processor";
    });
    var totalAgents = resources.total_workers == null ? agents.length : Number(resources.total_workers || 0);
    var readyAgents = resources.ready_workers == null ? agents.filter(function (item) {
      return ["RUNNING", "READY"].indexOf(String(item.phase || "").toUpperCase()) >= 0;
    }).length : Number(resources.ready_workers || 0);
    var cards = agents.map(function (item) {
      var inRun = Boolean(item.selected_for_run || selected[item.agent_id || item.name]);
      var mode = inRun ? "本Run已出场" : (item.on_demand ? "按需待命" : "本Run未路由");
      return '<div class="agent-pool-item ' + (inRun ? 'agent-pool-active' : '') + '"><span class="agent-pool-dot"></span><div><b>' + esc(item.display_name || item.label) + '</b><small class="mono">' + esc(item.agent_id || item.name) + '</small></div><em>' + esc(mode) + '</em></div>';
    }).join('');
    return '<section class="card agent-pool-card"><div class="card-head"><i class="ri-team-line card-icon"></i><h4>当前 AgentTeams 可用 Agent 池</h4>' + UI().badge(readyAgents + '/' + totalAgents + ' READY') + '</div>' +
      '<div class="agent-pool-summary"><span><b>' + esc(Object.keys(selected).length) + '</b>本Run实际出场Worker</span><span><b>' + esc(readyAgents) + '</b>当前Team可用Agent</span><span><b>' + esc(((runtime.extension_capability || {}).ready_workers || []).length) + '</b>按需扩展Agent</span></div>' +
      '<p class="card-note">下面展示真实Runtime中的Agent。未被历史Run路由的Agent只能标记为“待命”，不能伪装成本Run执行结果。</p>' +
      '<div class="agent-pool-grid">' + (cards || '<p class="card-note">Runtime尚未返回Agent拓扑。</p>') + '</div>' +
      '<div class="agent-control-line"><span><b>控制角色</b> Global Manager → FinFlux Case Lead</span><span><b>结果角色</b> Result Composer → Human Gate</span></div></section>';
  }

  function workerCards(route) {
    var workers = route.workers || [];
    if (!workers.length) return '<div class="card empty-card"><i class="ri-node-tree"></i><h3>本Run未进入完整AgentTeams</h3><p>确定性预检通过的低不确定性路径不会为展示而强行调用多Agent。</p></div>';
    return '<div class="collaboration-workers">' + workers.map(function (w, index) {
      var done = ["SEALED", "SUCCEEDED", "PASS", "BLOCK"].indexOf(String(w.status)) >= 0 || Boolean(w.seal_hash);
      return '<article class="worker-runtime-card topo-' + esc(w.color) + '"><div class="worker-order">0' + (index + 1) + '</div><div><small>' + esc(w.duty) + '</small><h3>' + esc(w.name) + '</h3><p class="mono">' + esc(w.task_id) + '</p><div class="worker-runtime-result"><span>' + UI().badge(done ? "SEALED" : w.status) + '</span><b>' + esc(w.conclusion) + '</b></div><p>' + esc(w.evidence_note) + '</p></div></article>';
    }).join('') + '</div>';
  }

  function artifactDecisionMatrix(presentation, route) {
    var artifacts = presentation.artifacts || [];
    var byWorker = {};
    artifacts.forEach(function (item) { byWorker[item.worker_id] = item; });
    var workers = route.workers || [];
    var rows = workers.map(function (worker) {
      var artifact = byWorker[worker.agent_id] || {};
      var skills = artifact.skill_invocations || [];
      return '<tr><td><b>' + esc(worker.name || worker.agent_id) + '</b><small class="mono">' + esc(worker.task_id) + '</small></td>' +
        '<td>' + esc(worker.duty || artifact.artifact_type || '专业核验') + '</td>' +
        '<td class="mono">' + esc(artifact.context_slice_sha256 ? UI().shaShort(artifact.context_slice_sha256, 10, 5) : '未记录') + '</td>' +
        '<td>' + esc(skills.length ? skills.length + ' 次调用' : '见右侧回执') + '</td>' +
        '<td>' + UI().badge(artifact.recommendation || worker.conclusion || worker.status || 'PENDING') + '</td>' +
        '<td class="mono">' + esc(artifact.artifact_sha256 ? UI().shaShort(artifact.artifact_sha256, 10, 5) : (worker.seal_hash ? UI().shaShort(worker.seal_hash, 10, 5) : '未封存')) + '</td></tr>';
    }).join('');
    return '<section class="card agent-artifact-matrix"><div class="card-head"><i class="ri-layout-grid-line card-icon"></i><h4>独立Context Slice与Worker产物矩阵</h4>' + UI().badge((presentation.agent_team || {}).completed_count + '/' + (presentation.agent_team || {}).required_count + ' SEALED') + '</div>' +
      '<p class="card-note">每个Worker只接收与职责相关的最小上下文，并独立提交可哈希产物；Case Lead只能汇聚这些产物，不能替Worker补结论。</p>' +
      '<div class="table-wrap"><table class="data-table"><thead><tr><th>Worker / Task</th><th>职责</th><th>Context Slice</th><th>Skill回执</th><th>结论</th><th>产物封印</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6">本Run尚无被Manager选择的Worker</td></tr>') + '</tbody></table></div>' +
      '<div class="case-lead-synthesis"><b>Case Lead 汇聚边界</b><span>输入：已封存Worker产物</span><span>输出：DataPassDraft</span><span>签署权：Human Only</span></div></section>';
  }

  function liveStatusStrip(status) {
    status = status || {};
    var progress = status.worker_progress || {}, supervisor = status.run_supervisor || {};
    var usage = status.provider_usage || {};
    return '<div id="run-live-status" class="run-live-status" data-state="' + esc(status.execution_state || 'UNKNOWN') + '"><span><i class="ri-pulse-line"></i> 单Run轻量状态</span><b>' + esc(status.execution_state || 'UNKNOWN') + '</b><span>' + esc(progress.completed || 0) + '/' + esc(progress.required || 0) + ' Worker</span><span>' + esc(usage.call_count || 0) + ' calls · ' + esc(UI().fmtNum(usage.total_tokens || 0)) + ' Token</span><span>Supervisor · ' + esc(supervisor.last_action || supervisor.state || 'UNKNOWN') + '</span><small>浏览器只读持久化快照，不负责推进Run</small></div>';
  }

  function pollRunStatus(host, runId) {
    window.setTimeout(function () {
      if (window.location.hash.indexOf('#/collaboration') !== 0) return;
      window.FinfluxAPI.getRunStatus(runId).then(function (latest) {
        var strip = host.querySelector('#run-live-status');
        if (strip) strip.outerHTML = liveStatusStrip(latest);
        var terminal = ['AWAITING_HUMAN', 'COMPLETED', 'FAILED_CLOSED', 'STOPPED_BY_GATE', 'BUDGET_EXCEEDED'].indexOf(String(latest.execution_state)) >= 0;
        if (!terminal) pollRunStatus(host, runId);
      }).catch(function () { pollRunStatus(host, runId); });
    }, 3000);
  }

  function skillTable(skills) {
    var runSkills = (skills || []).filter(function (s) {
      return s.channel === "AgentTeams Worker runtime" && Number(s.runtime_invocations || 0) > 0;
    });
    var rows = runSkills.map(function (s) {
      var invocation = s.latest_invocation || s.invocation || {};
      var used = Number(s.runtime_invocations || 0) > 0;
      return '<tr><td><b>' + esc(s.skill_id || s.name) + '</b></td><td class="mono">' + esc(s.version) + '</td><td>' + esc(s.owner_role || s.owner || s.worker_owner || '按Manager路由') + '</td><td>' + UI().badge(used ? "INVOKED" : "AVAILABLE", used ? "本Run已调用" : "已注册") + '</td><td class="mono">' + esc(invocation.provider_tokens == null ? (s.provider_tokens == null ? 0 : s.provider_tokens) : invocation.provider_tokens) + '</td></tr>';
    }).join('');
    return '<section class="card collaboration-skills"><div class="card-head"><i class="ri-flashlight-line card-icon"></i><h4>本Run实际调用的Skill</h4>' + UI().badge(runSkills.length + '/' + (skills || []).length + ' USED') + '</div>' +
      '<div class="skill-scope-summary"><span><b>' + esc(runSkills.length) + '</b>本Run调用</span><span><b>' + esc((skills || []).length) + '</b>注册能力</span><span><b>按需</b>不会全量加载</span></div>' +
      '<p class="card-note">注册能力不会在每条任务中全量执行；这里只统计本Run实际留下调用回执的Skill。</p>' +
      '<div class="table-wrap"><table class="data-table"><thead><tr><th>Skill</th><th>版本</th><th>责任Worker</th><th>状态</th><th>Provider Token</th></tr></thead><tbody>' + (rows || '<tr><td colspan="5">本Run没有AgentTeams Skill调用记录</td></tr>') + '</tbody></table></div>' +
      '<a class="btn btn-outline skill-registry-link" href="#/skills"><i class="ri-store-2-line"></i> 查看完整Skill注册表与调用条件</a></section>';
  }

  function runGuide(ws) {
    var guard = ws.token_guard || {}, runtime = ws.runtime || {}, resources = runtime.resources || {};
    var reasons = (guard.reasons || []).map(function (reason) {
      if (String(reason).indexOf('ACTIVE_RUN_EXISTS:') === 0) return '存在未关闭的AgentTeams Run：' + String(reason).split(':').slice(1).join(':');
      if (String(reason).indexOf('DAILY_TOKEN_RESERVE_INSUFFICIENT:') === 0) return '今日真实供应商Token预留不足：' + String(reason).split(':').slice(1).join(':');
      return reason;
    });
    var topology = runtime.topology || [];
    var expectedRoles = Number((ws.control_plane || {}).topology_expected || topology.length || 0);
    var readyRoles = Number((ws.control_plane || {}).topology_ready || topology.filter(function (item) {
      return ["RUNNING", "READY"].indexOf(String(item.phase || "").toUpperCase()) >= 0;
    }).length);
    var ready = Boolean(runtime.connected && expectedRoles > 0 && readyRoles === expectedRoles);
    var allowed = Boolean(guard.allowed);
    return '<section class="card agent-run-guide ' + (allowed ? 'run-allowed' : 'run-blocked') + '"><div class="card-head"><i class="ri-play-circle-line card-icon"></i><h4>怎样运行一条新的Agent Run</h4>' + UI().badge(allowed ? '可以派发' : '当前阻断') + '</div>' +
      '<div class="run-guide-steps"><span><b>1</b>接入真实资料并写明任务</span><span><b>2</b>0-Token固化与预检</span><span><b>3</b>Manager按需选择Agent和Skill</span><span><b>4</b>DataPass后由Human签署</span></div>' +
      '<p class="card-note">Runtime：' + esc(ready ? ('AgentTeams ' + readyRoles + '/' + expectedRoles + ' 角色就绪') : ('未完全就绪（' + readyRoles + '/' + expectedRoles + '）')) + '。' + (allowed ? '门禁允许创建一条新Run。' : '当前不能发起模型Run：' + esc(reasons.join('；') || 'Token Guard未放行。')) + '</p>' +
      '<a class="btn ' + (allowed ? 'btn-primary' : 'btn-outline') + ' btn-xl" href="#/live"><i class="ri-upload-cloud-2-line"></i> ' + (allowed ? '接入资料并启动AgentTeams' : '先准备下一条Case（暂不派发）') + '</a></section>';
  }

  function render(host, refresh) {
    return Promise.all([window.FinfluxAPI.getWorkspace(Boolean(refresh)), window.FinfluxAPI.getRouteDecision(), window.FinfluxAPI.getSkills()]).then(function (values) {
      var ws = values[0], route = values[1], skills = values[2].skills || [], run = ws.run;
      if (!run) { host.innerHTML = '<div class="card empty-card"><i class="ri-inbox-line"></i><h3>尚无Run</h3><p>请先接入真实金融资料并创建受控Run。</p><a class="btn btn-primary" href="#/live">前往数据接入</a></div>'; return; }
      return Promise.all([window.FinfluxAPI.getRunPresentation(run.run_id), window.FinfluxAPI.getRunStatus(run.run_id)]).then(function (extra) {
      var presentation = extra[0], status = extra[1];
      host.innerHTML = runSelector(ws) + liveStatusStrip(status) + agentPool(ws.runtime, route) + stageCards(ws.decision_stages || {}) + artifactDecisionMatrix(presentation, route) + '<div class="collaboration-layout"><div>' + managerCard(run, route) + workerCards(route) + '</div><div>' + skillTable(skills) + runGuide(ws) + '<div class="card collaboration-next"><div class="card-head"><i class="ri-arrow-right-circle-line card-icon"></i><h4>查看本Run结果</h4></div><p class="card-note">Worker产物齐备后，Case Lead只汇总已封存事实并生成DataPassDraft，再交Human。</p><a class="btn btn-primary btn-xl" href="#/evidence">查看 DataPass 与 Human</a><a class="btn btn-outline btn-xl" href="#/trace">展开同Run Trace / Tool I/O / Token</a></div></div></div>';
      var select = host.querySelector('#run-context-select');
      var choose = host.querySelector('#btn-select-run');
      if (choose && select) choose.addEventListener('click', function () { choose.disabled = true; window.FinfluxAPI.selectRun(select.value).then(function () { UI().toast('已切换Run上下文', 'success'); UI().refreshTopbar(); return render(host, true); }).catch(function (err) { UI().toast(err.message, 'error'); choose.disabled = false; }); });
      var freeze = host.querySelector('#btn-freeze-judge');
      if (freeze) freeze.addEventListener('click', function () { freeze.disabled = true; window.FinfluxAPI.setJudgeRun(ws.selected_run_id, 'demo.operator', '复赛现场端到端验收Run').then(function () { UI().toast('裁判Run已冻结；Human业务签署状态未改变', 'success'); return render(host, true); }).catch(function (err) { UI().toast(err.message, 'error'); freeze.disabled = false; }); });
      var terminal = ['AWAITING_HUMAN', 'COMPLETED', 'FAILED_CLOSED', 'STOPPED_BY_GATE', 'BUDGET_EXCEEDED'].indexOf(String(status.execution_state)) >= 0;
      if (!terminal) pollRunStatus(host, run.run_id);
      });
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.collaboration = render;
})();
