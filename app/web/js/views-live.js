/* Unified financial intake -> immutable evidence -> bounded AgentTeams. */
(function () {
  "use strict";

  var livePollTimer = null;

  function liveStateSignature(ws, guard) {
    var run = (ws && ws.run) || {}, request = run.dispatch_request || {}, supervisor = (ws && ws.run_supervisor) || {}, runtimeSupervisor = (ws && ws.runtime_supervisor) || {};
    var result = run.agent_result || {}, usage = run.provider_usage || {};
    return JSON.stringify({
      run_id: run.run_id || "",
      state: run.state || "",
      agentteams_run_id: run.agentteams_run_id || "",
      dispatch_status: request.status || "",
      workers_completed: result.workers_completed || 0,
      workers_required: result.workers_required || 0,
      provider_calls: usage.call_count || 0,
      provider_tokens: usage.total_tokens || 0,
      active_run_ids: ((guard && guard.active_run_ids) || []).slice().sort(),
      supervisor_run_id: supervisor.run_id || "",
      supervisor_run_state: supervisor.run_state || "",
      supervisor_action: supervisor.last_action || "",
      supervisor_recoveries: supervisor.recovery_count || 0,
      runtime_state: runtimeSupervisor.state || "",
      runtime_gate_open: Boolean(runtimeSupervisor.gate_open),
      runtime_action: runtimeSupervisor.last_action || "",
      runtime_check_signature: (runtimeSupervisor.checks || []).map(function (item) { return item.check_id + ":" + item.status; }).join("|")
    });
  }

  function scheduleLivePoll(host, signature) {
    window.clearTimeout(livePollTimer);
    livePollTimer = window.setTimeout(function pollLiveRun() {
      if (String(window.location.hash || "").indexOf("#/live") !== 0 || !host.isConnected) return;
      var activeElement = document.activeElement;
      var editing = activeElement && host.contains(activeElement) && /^(INPUT|TEXTAREA|SELECT)$/.test(activeElement.tagName);
      var selectedFile = host.querySelector("#live-file");
      if (editing || (selectedFile && selectedFile.files && selectedFile.files.length)) {
        scheduleLivePoll(host, signature);
        return;
      }
      Promise.all([window.FinfluxAPI.getWorkspace(true), window.FinfluxAPI.getTokenGuard()]).then(function (values) {
        var nextSignature = liveStateSignature(values[0], values[1]);
        if (nextSignature !== signature) return render(host, true);
        scheduleLivePoll(host, signature);
      }).catch(function () {
        scheduleLivePoll(host, signature);
      });
    }, 5000);
  }

  function esc(value) { return window.FinfluxUI.esc(value == null ? "" : value); }
  function short(value) { return window.FinfluxUI.shaShort(value || "", 16, 6); }
  function profileById(registry, profileId) {
    return ((registry && registry.profiles) || []).find(function (item) { return item.profile_id === profileId; }) || null;
  }
  function readPath(source, path) {
    return String(path || "").split(".").reduce(function (value, part) {
      return value && value[part] !== undefined ? value[part] : undefined;
    }, source);
  }

  function runtimeGateReady(ws) {
    return Boolean(ws && ws.runtime_supervisor && ws.runtime_supervisor.gate_open);
  }

  function intakeWorkbench(capabilities, runtimeSupervisor) {
    var count = capabilities ? capabilities.catalog_real_item_count : 0;
    var runtimeReady = Boolean(runtimeSupervisor && runtimeSupervisor.gate_open);
    var runtimeLabel = runtimeReady ? "一键启动真实 AgentTeams 核验" : "Runtime冷启动验收未通过，暂不可创建Run";
    return '<section class="intake-workbench">' +
      '<div class="col-title"><span class="col-num">1</span><div><p class="eyebrow">UNIFIED FINANCIAL INTAKE</p><h2>提交核验任务与金融资料</h2></div></div>' +
      '<div class="card intake-card">' +
        '<div class="intake-tabs" role="tablist" aria-label="金融数据接入方式">' +
          '<button type="button" class="intake-tab active" data-intake-tab="file" role="tab" aria-selected="true"><i class="ri-file-upload-line"></i> 文件 + 任务</button>' +
          '<button type="button" class="intake-tab" data-intake-tab="url" role="tab" aria-selected="false"><i class="ri-global-line"></i> 公开URL + 任务</button>' +
          '<button type="button" class="intake-tab" data-intake-tab="catalog" role="tab" aria-selected="false"><i class="ri-search-eye-line"></i> 真实资料库 + 任务 <span>' + esc(count) + '</span></button>' +
        '</div>' +

        '<form id="intake-file-form" class="live-form intake-pane active" data-intake-pane="file">' +
          '<label class="form-label intent-field"><span>业务核验目标</span><textarea id="case-instruction" class="intake-textarea" rows="4" maxlength="2000" placeholder="例如：核验这份金融资料能否用于声明的下游业务；识别语义冲突，复算确定性影响，并给出可审计的DataPass建议。" required></textarea><small>业务目标与证据分别登记，并共同绑定到同一次受控运行。</small></label>' +
          '<label id="smart-drop-zone" class="drop-zone smart-drop" for="live-file"><i class="ri-sparkling-2-line"></i><b>补充金融资料（可选）</b><span>拖入资料，或复用右侧已登记的EvidenceBundle</span><small>支持表格、文本、网页、PDF及压缩证据包 · 最大10MB</small><input id="live-file" name="file" type="file" accept=".csv,.json,.txt,.md,.xml,.html,.htm,.xlsx,.pdf,.zip"></label>' +
          '<div class="case-compose-actions"><button class="btn btn-primary btn-xl" type="submit" ' + (runtimeReady ? '' : 'disabled') + '><i class="ri-node-tree"></i> ' + esc(runtimeLabel) + '</button><span>' + (runtimeReady ? '点击后依次完成上传、固化、创建Run和多Agent派发；只在来源、用途或权属无法确认时暂停为WAIT。' : '先在下方高可用协作控制面查看失败项，或执行“一键修复运行环境”。证据仍可检查，但不会创建Run。') + '</span></div>' +
          '<div id="file-inspection-result" class="inspection-result" aria-live="polite"><p class="card-note"><i class="ri-magic-line"></i> 金融资料、业务目标和来源声明将分别登记，并组合为同一CaseEnvelope。</p></div>' +
        '</form>' +

        '<form id="intake-url-form" class="live-form intake-pane" data-intake-pane="url">' +
          '<label class="form-label intent-field"><span>你希望 AgentTeams 核验什么？</span><textarea id="url-instruction" class="intake-textarea" rows="3" maxlength="2000" placeholder="例如：采集该公开资料，核验字段定义、适用时点与版本变化是否影响声明的下游用途。" required></textarea></label>' +
          '<label class="form-label">公开数据或文件 URL<input id="url-value" type="url" placeholder="https://..." required></label>' +
          '<div class="inline-warning"><i class="ri-shield-keyhole-line"></i><span>仅允许公网HTTP/HTTPS；禁止内网地址、重定向到内网和超过10MB的响应。采集失败不会静默使用缓存冒充最新数据。</span></div>' +
          '<button class="btn btn-primary btn-xl" type="submit"><i class="ri-radar-line"></i> 采集、识别并生成Case</button>' +
          '<div id="url-inspection-result" class="inspection-result" aria-live="polite"><p class="card-note"><i class="ri-magic-line"></i> URL内容也执行与上传文件相同的0 Token识别和人工确认。</p></div>' +
        '</form>' +

        '<form id="intake-catalog-form" class="live-form intake-pane" data-intake-pane="catalog">' +
          '<label class="form-label intent-field"><span>你希望 AgentTeams 基于真实资料回答什么？</span><textarea id="catalog-instruction" class="intake-textarea" rows="3" maxlength="2000" placeholder="例如：核验600519相关研报与官方宏观数据的来源、时点和适用边界，不允许编造缺失结论。" required></textarea></label>' +
          '<div class="catalog-search-row"><label class="form-label">股票、机构或主题<input id="catalog-query" type="search" placeholder="例如 600519、金融稳定"></label><label class="form-label">Provider<select id="catalog-provider"><option value="">全部真实Provider</option><option value="eastmoney_report">EastMoney研报元数据</option><option value="worldbank">World Bank官方数据</option></select></label><button id="btn-catalog-search" class="btn btn-outline" type="button"><i class="ri-search-line"></i> 检索</button></div>' +
          '<div id="catalog-results" class="catalog-results"><p class="card-note">输入标的或主题，从本地40条真实Provider缓存中检索；结果保留来源、权利状态和SHA256。</p></div>' +
          '<button id="btn-catalog-pack" class="btn btn-primary btn-xl" type="submit" disabled><i class="ri-folder-shield-2-line"></i> 组装、识别并生成Case</button>' +
          '<div id="catalog-inspection-result" class="inspection-result" aria-live="polite"><p class="card-note"><i class="ri-fingerprint-line"></i> 选中资料会组成带Provider、Rights与逐条SHA256的不可变EvidenceBundle。</p></div>' +
        '</form>' +
      '</div>' +
      '<p class="lock-note intake-boundary"><i class="ri-lock-2-line"></i> 指令负责说明任务，真实资料负责提供证据；Manager只规划与路由，金融数值必须来自确定性Skill，Token Guard通过后才允许派发AgentTeams。</p>' +
    '</section>';
  }

  function confirmationField(field, inferred, registry) {
    var labels = { declared_purpose: "业务用途", declared_source: "原始来源", rights_basis: "使用依据", confidentiality_class: "数据密级", contract_multiplier: "合约乘数", entity_query: "本次核验标的" };
    if (field === "declared_purpose") {
      var definition = profileById(registry, inferred.profile);
      var bindings = (definition && definition.purpose_bindings) || {};
      var options = Object.keys(bindings).map(function (value) {
        return '<option value="' + esc(value) + '" ' + (value === inferred.declared_purpose ? 'selected' : '') + '>' + esc(bindings[value].label || value) + '</option>';
      }).join('');
      if (!options) options = '<option value="' + esc(inferred.declared_purpose || "evidence_review") + '">' + esc(inferred.declared_purpose || "待补充用途") + '</option>';
      return '<label class="form-label">' + labels[field] + '<select data-confirm="declared_purpose">' + options + '</select></label>';
    }
    if (field === "confidentiality_class") return '<label class="form-label">' + labels[field] + '<select data-confirm="confidentiality_class"><option value="PUBLIC">公开</option><option value="INTERNAL">机构内部</option><option value="CONFIDENTIAL">机密（只传哈希/元数据）</option></select></label>';
    var type = field === "contract_multiplier" ? "number" : "text";
    var value = inferred[field] == null ? "" : inferred[field];
    return '<label class="form-label">' + esc(labels[field] || field) + '<input data-confirm="' + esc(field) + '" type="' + type + '" value="' + esc(value) + '" required></label>';
  }

  function candidateMappingField(inferred, registry) {
    var definition = profileById(registry, inferred.profile || "");
    if (!definition || !definition.live_executable) return "";
    return '<div class="ingress-config-panel">' +
      '<input type="hidden" data-confirm="candidate_mapping" value="AUTO_AGENT">' +
      '<div class="ingress-config-head"><span class="step-chip">AGENT-FIRST</span><div><b>由AgentTeams发现金融语义</b><small>你只声明业务目标；系统不要求你预选字段，也不把Profile默认值当答案</small></div></div>' +
      '<div id="mapping-route-preview" class="mapping-route-preview route-agent">' +
        '<i class="ri-node-tree"></i><div><b>两个专业Agent独立提出候选，确定性Skill复算，Human最终批准</b><span>Manager → Case Lead → Evidence / Semantic / Independent Review → DataPass → Human</span></div>' +
      '</div></div>';
  }

  function renderInspection(box, inspection, registry) {
    var f = inspection.file || {}, inferred = inspection.inferred || {}, required = inspection.required_confirmations || [];
    var parsed = inspection.parsed || {}, executable = inspection.execution_readiness_candidate === "AGENTTEAMS_EXECUTABLE", missing = inspection.missing_evidence_fields || [], waits = inspection.wait_reason_codes || [];
    var definition = profileById(registry, inferred.profile);
    var mappingField = candidateMappingField(inferred, registry);
    var confirmationCount = required.length;
    box.innerHTML = '<div class="inspection-card">' +
      '<div class="inspection-head"><div><p class="eyebrow">ZERO-TOKEN INSPECTION</p><h4>' + esc(f.name) + '</h4></div><span class="badge badge-green">0 Model Token</span></div>' +
      '<div class="inspection-facts"><span><small>识别资产</small><b>' + esc(inferred.asset_class || "unknown") + '</b></span><span><small>金融Profile</small><b>' + esc((definition && definition.display_name) || inferred.profile) + '</b><code>' + esc(inferred.profile) + '</code></span><span><small>Profile版本</small><b class="mono">' + esc((definition && definition.version) || "未注册") + '</b></span><span><small>执行就绪</small><b class="' + (executable ? 'text-green' : 'text-amber') + '">' + esc(inspection.execution_readiness_candidate) + '</b></span></div>' +
      '<div class="inspection-evidence"><span>SHA256</span><code>' + esc(short(f.sha256)) + '</code><span>标的</span><b>' + esc(inferred.entity_query || "未从文件确认") + '</b><span>证据行/列</span><b>' + esc(parsed.row_count == null ? (parsed.line_count || "结构级") : parsed.row_count) + ' / ' + esc((parsed.columns || []).length || "—") + '</b></div>' +
      (inspection.known_evidence_match ? '<p class="inspection-match"><i class="ri-fingerprint-line"></i> 已按原始字节哈希命中历史来源与权属声明，不是按文件名猜测。</p>' : '<p class="inspection-match warning"><i class="ri-user-follow-line"></i> 新哈希未命中已知来源，只请你补齐无法从文件中推断的责任信息。</p>') +
      (missing.length || waits.length ? '<div class="inline-warning"><i class="ri-pause-circle-line"></i><span><b>当前为WAIT，不生成金融结论。</b> 需补充：' + esc(missing.concat(waits).join('、')) + '</span></div>' : '') +
      '<div class="minimal-confirm"><h5>' + (confirmationCount ? '确认 ' + esc(confirmationCount) + ' 项用途与责任信息' : '结构识别完成，可启动语义发现') + '</h5><div class="live-form-grid">' + mappingField + required.map(function (field) { return confirmationField(field, inferred, registry); }).join('') + '</div><button id="btn-commit-inspection" class="btn btn-primary btn-xl" type="button"><i class="ri-node-tree"></i> ' + (mappingField ? '启动真实 AgentTeams 语义核验' : (required.length ? '确认补充项并继续一键核验' : '执行一键核验')) + '</button></div>' +
      '<details class="advanced-fields"><summary>展开审计细节：3个确定性Skill版本与输入输出哈希</summary><div class="inspection-skills">' + (inspection.skill_invocations || []).map(function (skill) { return '<div><b>' + esc(skill.skill_id) + '@' + esc(skill.version) + '</b><code>' + esc(short(skill.input_sha256)) + ' → ' + esc(short(skill.output_sha256)) + '</code></div>'; }).join('') + '</div></details>' +
    '</div>';
  }

  function parsedSummary(submission, registry) {
    var definition = profileById(registry, submission.profile);
    var fields = (definition && definition.summary_fields) || [];
    if (!fields.length) return '<div class="generic-summary"><span><b>WAIT</b>尚未匹配展示Profile</span><span><b>' + esc((submission.file || {}).name || "证据") + '</b>原始资料</span></div>';
    return '<div class="stat-grid4">' + fields.map(function (field) {
      var value = readPath(submission, field.path);
      return '<div class="stat"><b class="mono">' + esc(value == null || value === "" ? "未提供" : value) + '</b><span>' + esc(field.label) + (field.unit ? ' · ' + esc(field.unit) : '') + '</span></div>';
    }).join('') + '</div>';
  }

  function submissionCard(ws, registry) {
    // When the operator selects an older/guarding Run, show the evidence that
    // is actually bound to that Run.  Falling back to the latest Submission
    // used to hide the selected Run and made the following page look empty.
    var s = ws.run_submission || ws.latest_submission || ws.submission;
    if (!s) return '<section class="intake-output"><div class="col-title"><span class="col-num">2</span><div><p class="eyebrow">NO EVIDENCE</p><h2>等待真实资料</h2></div></div><div class="card empty-card"><i class="ri-inbox-archive-line"></i><h3>选择左侧任一入口</h3><p>系统接受原始表格、文本、网页或Provider记录，并按内容哈希登记其原始版本。</p></div></section>';
    var f = s.file, ready = s.execution_readiness === "AGENTTEAMS_EXECUTABLE", waiting = /^WAIT_/.test(s.execution_readiness || ""), caseInput = s.case_input || {}, hasIntent = Boolean(caseInput.task_instruction);
    var run = ws.run && ws.run.submission_id === s.submission_id ? ws.run : null;
    return '<section class="intake-output">' +
      '<div class="col-title"><span class="col-num">2</span><div><p class="eyebrow">IMMUTABLE EVIDENCE</p><h2>接入结果与下一步</h2></div></div>' +
      '<div class="card"><div class="card-head"><i class="ri-folder-shield-2-line card-icon"></i><h4>' + esc(s.evidence_bundle_id) + '</h4><span class="badge badge-green"><i class="dot"></i>' + esc(s.status) + '</span></div>' +
        window.FinfluxUI.kv("原始文件", esc(f.name) + ' · ' + window.FinfluxUI.fmtNum(f.size_bytes) + ' bytes') +
        window.FinfluxUI.kv("服务端 SHA256", '<span class="mono">' + esc(short(f.sha256)) + '</span>' + window.FinfluxUI.copyBtn(f.sha256)) +
        window.FinfluxUI.kv("识别Profile", '<b class="mono">' + esc(s.profile) + '</b>') +
        window.FinfluxUI.kv("执行就绪", '<span class="badge ' + (ready ? 'badge-green' : 'badge-amber') + '">' + esc(s.execution_readiness) + '</span>') +
        (caseInput.task_instruction ? '<div class="case-intent-summary"><small>CASE INTENT · ' + esc(short(caseInput.task_instruction_sha256)) + '</small><p>' + esc(caseInput.task_instruction) + '</p><span>证据与指令已组合封印：<b class="mono">' + esc(short(caseInput.case_input_sha256)) + '</b></span></div>' : '<div class="case-intent-summary empty"><small>CASE INTENT</small><p>尚未绑定任务指令；可在左侧输入后复用本证据。</p></div>') + parsedSummary(s, registry) + '</div>' +
      '<div class="card next-step-card ' + (ready && hasIntent ? 'ready' : 'waiting') + '"><div class="card-head"><i class="' + (ready && hasIntent ? 'ri-play-circle-line' : 'ri-pause-circle-line') + ' card-icon"></i><h4>' + (ready ? (hasIntent ? '已具备受控运行条件' : '证据已就绪，等待任务指令') : waiting ? 'WAIT：需要补充证据或用途' : '等待已登记领域Profile') + '</h4></div><p class="card-note">' + esc(s.adapter_note) + '</p>' + (((s.metadata || {}).missing_evidence_fields || []).length ? '<p class="inline-warning">补充项：' + esc((s.metadata || {}).missing_evidence_fields.join('、')) + '</p>' : '') + '</div>' +
      (run ? runCard(run, ws.runtime, ws.token_guard) : '') + '</section>';
  }

  function runCard(run, runtime, guard) {
    var p = run.precheck || {}, tokenLedger = (run.budget || {}).tokens || {};
    var providerReported = tokenLedger.status === "PROVIDER_REPORTED";
    var tokenNote = run.agentteams_run_id ? (providerReported ? "供应商真实usage：" + window.FinfluxUI.fmtNum(tokenLedger.reported) + " Token。" : "已进入AgentTeams；供应商usage尚未归集，不用字符估算冒充Token。") : "仅执行确定性Skill，模型调用0次、Token=0。";
    var activeIds = (guard && guard.active_run_ids) || [];
    var activeBlocker = activeIds.filter(function (id) { return id && id !== run.run_id; })[0] || null;
    var activeRuns = (guard && guard.active_runs) || [];
    var blocker = activeRuns.filter(function (item) { return item && item.run_id === activeBlocker; })[0] || {};
    var blockerState = String(blocker.state || "状态读取中");
    var blockerHuman = blockerState === "AWAITING_HUMAN" || blocker.human_state === "AWAITING_HUMAN";
    var blockerRunning = ["RUNNING", "AGENTTEAMS_RUNNING", "MANAGER_AUTHORIZED", "WORKERS_RUNNING"].indexOf(blockerState) >= 0;
    var blockedByActiveRun = Boolean(activeBlocker && ((guard && guard.reasons) || []).some(function (reason) { return String(reason).indexOf('ACTIVE_RUN_LIMIT') === 0; }));
    var canDispatch = runtime && runtime.connected && guard && guard.allowed;
    var dispatchLabel = !runtime || !runtime.connected ? 'Runtime离线，禁止派发' : (!guard || !guard.allowed ? (blockedByActiveRun ? '已有Run待处理' : '运行门禁未放行') : '提交真实AgentTeams');
    var terminal = ["COMPLETED", "AWAITING_HUMAN", "STOPPED_BY_GATE", "BUDGET_EXCEEDED", "MODEL_CONTROL_CLEANUP_FAILED", "CANCELLED_BY_SESSION_RESET"].indexOf(String(run.state)) >= 0;
    var repairs = (run.self_healing_attempts || []).concat(run.supervisor_recovery_attempts || []);
    var stopReason = ((run.emergency_stop || {}).reason || (run.supervisor_outcome || {}).reason || ((run.dispatch_guard || {}).reasons || []).join(" · ") || "");
    var state = String(run.state || "UNKNOWN");
    var dispatchRequestState = String(((run.dispatch_request || {}).status) || "");
    var waitingForSupervisor = !run.agentteams_run_id && ["QUEUED", "RETRY_WAIT"].indexOf(dispatchRequestState) >= 0;
    var stateGuide = state === "AWAITING_HUMAN"
      ? '<div class="run-next-action next-human"><i class="ri-user-follow-line"></i><div><b>DataPass草案已形成，下一步在 Human Gate</b><span>只有此状态才允许Human签署；进入后可批准、隔离或退回补证。</span></div><a class="btn btn-primary" href="#/human-gate">进入 Human Gate</a></div>'
      : (state === "RUNNING" || (run.agentteams_run_id && !terminal)
        ? '<div class="run-next-action next-running"><i class="ri-node-tree"></i><div><b>AgentTeams正在后台运行</b><span>浏览器可以关闭；到“多Agent决策台”查看Manager、Case Lead和Worker产物。</span></div><a class="btn btn-primary" href="#/collaboration">查看协作进度</a></div>'
        : (["STOPPED_BY_GATE", "BUDGET_EXCEEDED", "MODEL_CONTROL_CLEANUP_FAILED", "CANCELLED_BY_SESSION_RESET"].indexOf(state) >= 0
          ? '<div class="run-next-action next-stopped"><i class="ri-error-warning-line"></i><div><b>本Run已停止，尚无DataPass可签署</b><span>' + esc(stopReason || "请在Trace中查看停止原因；Human Gate不会为缺失的Agent结论生成审批项。") + '</span></div><a class="btn btn-outline" href="#/trace">查看原因与恢复记录</a></div>'
          : (state === "DISPATCH_GUARDED"
            ? '<div class="run-next-action next-queued"><i class="ri-time-line"></i><div><b>本Run已排队，尚未调用模型</b><span>后台会在占用Run结束后派发；本页保留当前状态，不会自动跳到无内容页面。</span></div></div>'
            : '')));
    var dispatch = run.agentteams_run_id
      ? stateGuide + '<p class="card-note"><i class="ri-checkbox-circle-line"></i> 已绑定真实AgentTeams Run；恢复只作用于同一Run，不更换证据哈希。</p>' +
        (!terminal ? '<button id="btn-repair-agentteams" class="btn btn-outline btn-xl" type="button"><i class="ri-restart-line"></i> 请求 AgentTeams 同Run自愈</button>' : '')
      : (blockedByActiveRun
        ? '<div class="run-next-action next-queued"><i class="ri-list-check-2"></i><div><b>当前请求已进入后台队列</b><span>占用Run：' + esc(activeBlocker) + ' · ' + esc(blockerState) + (blockerRunning ? ' · Worker ' + esc(blocker.workers_completed || 0) + '/' + esc(blocker.workers_required || 0) : '') + '。' + (blockerHuman ? '该Run确实在等待Human签署。' : (blockerRunning ? '该Run仍在多Agent执行或恢复中，并非等待Human。' : '点击后会先核验真实状态，再进入正确页面。')) + '</span></div></div>' +
          (blockerHuman
            ? '<button id="btn-resolve-blocking-run" data-run-id="' + esc(activeBlocker) + '" class="btn btn-primary btn-xl" type="button"><i class="ri-user-follow-line"></i> 前往 Human Gate 处理</button>'
            : '<div class="blocking-run-actions"><button id="btn-inspect-blocking-run" data-run-id="' + esc(activeBlocker) + '" class="btn btn-outline" type="button"><i class="ri-pulse-line"></i> 展开占用Run状态</button>' +
              (blockerRunning ? '<button id="btn-open-release-occupancy" data-run-id="' + esc(activeBlocker) + '" data-queued-run-id="' + esc(run.run_id) + '" class="btn btn-danger" type="button"><i class="ri-stop-circle-line"></i> 人工释放占用</button>' : '') + '</div>' +
              '<div id="blocking-run-detail" class="blocking-run-detail" hidden></div>' +
              (blockerRunning ? '<div id="release-occupancy-panel" class="release-occupancy-panel" hidden><div><b>终止占用Run并继续当前队列</b><span>该操作把占用Run正式终止为WAIT并关闭其模型网关账本；不会生成PASS、DataPass或Human签署。</span></div><label class="form-label">审计原因<textarea id="release-occupancy-reason" rows="2">现场操作：占用Run长时间无Worker产物，终止为WAIT并释放单Run门禁</textarea></label><div class="release-occupancy-actions"><button id="btn-cancel-release-occupancy" class="btn btn-ghost" type="button">取消</button><button id="btn-confirm-release-occupancy" class="btn btn-danger" type="button"><i class="ri-stop-circle-line"></i> 确认终止并释放</button></div></div>' : ''))
        : (waitingForSupervisor
          ? '<div class="run-next-action next-queued"><i class="ri-loader-4-line ri-spin"></i><div><b>RunSupervisor 正在接管本Run</b><span>占用已释放；无需再次点击或创建新Run，后台将在下一轮同步中完成真实AgentTeams派发。</span></div></div>'
          : '<button id="btn-dispatch-agentteams" class="btn btn-outline btn-xl" type="button" ' + (canDispatch ? '' : 'disabled') + '><i class="ri-node-tree"></i> ' + dispatchLabel + '</button>'));
    var repairNote = repairs.length ? '<div class="same-run-repair-note"><b>同Run恢复 ' + esc(repairs.length) + '/3</b><span>' + esc(repairs[repairs.length - 1].status) + (repairs[repairs.length - 1].classification ? ' · ' + esc(repairs[repairs.length - 1].classification) : '') + '</span></div>' : '';
    return '<div class="card run-result"><div class="card-head"><i class="ri-pulse-line card-icon"></i><h4>当前受控Run</h4>' + window.FinfluxUI.badge(run.state) + '</div>' + window.FinfluxUI.kv("Run ID", '<span class="mono">' + esc(run.run_id) + '</span>' + window.FinfluxUI.copyBtn(run.run_id)) + window.FinfluxUI.kv("预检建议", window.FinfluxUI.badge(p.machine_recommendation || "PENDING")) + '<p class="card-note"><i class="ri-information-line"></i> ' + esc(tokenNote) + '</p>' + repairNote + (!run.agentteams_run_id ? stateGuide : '') + dispatch + '<p class="card-note"><i class="ri-shield-keyhole-line"></i> Worker中断、工具超时、传输截断可同Run恢复；预算硬超限和代码包错误必须封存，由Human重新授权，Agent不得绕过。</p></div>';
  }

  function runtimeColumn(ws, guard) {
    var r = ws.runtime || {}, cp = ws.control_plane || {}, supervisor = ws.run_supervisor || {}, runtimeSupervisor = ws.runtime_supervisor || {}, connected = Boolean(r.connected), topology = {}, mc = r.model_connection || {}, active = ws.run || {}, usage = active.provider_usage || {}, ledger = usage.model_gateway_ledger || {};
    (r.topology || []).forEach(function (item) { topology[item.name] = item; });
    var workers = (r.topology || []).filter(function (item) { return item.role === "worker" || item.role === "post_processor"; });
    guard = guard || { status: "UNKNOWN", daily: {}, reasons: ["尚未获取后台usage"] };
    var daily = guard.daily || {}, remaining = daily.remaining_tokens;
    var runtimeChecks = runtimeSupervisor.checks || [], runtimeErrors = runtimeSupervisor.errors || [], remediation = runtimeSupervisor.remediation_actions || [];
    var runtimeReady = Boolean(runtimeSupervisor.gate_open), runtimeBusy = ["STARTING", "CHECKING", "REPAIRING"].indexOf(String(runtimeSupervisor.state || "")) >= 0;
    var checkLabels = { docker_ports: "Docker与端口", worker_quorum: "AgentTeams 8/8", ai_proxy_route: "AI Proxy → 8090", worker_packages: "Worker包摘要", model_canary: "真实模型Canary" };
    var checkRows = runtimeChecks.map(function (item) {
      var detail = item.detail || {}, proof = item.check_id === "model_canary" && item.status === "PASS" ? (window.FinfluxUI.fmtNum(detail.provider_call_count || 0) + "次 · " + window.FinfluxUI.fmtNum(detail.total_tokens || 0) + " Token") : item.summary;
      return '<div class="runtime-check ' + String(item.status || "WAIT").toLowerCase() + '"><i class="' + (item.status === "PASS" ? 'ri-checkbox-circle-line' : (item.status === "FAIL" ? 'ri-close-circle-line' : 'ri-time-line')) + '"></i><span><b>' + esc(checkLabels[item.check_id] || item.check_id) + '</b><small>' + esc(proof || "等待检查") + '</small></span><em>' + esc(item.status || "WAIT") + '</em></div>';
    }).join('');
    return '<section class="runtime-column"><div class="col-title"><span class="col-num">3</span><div><p class="eyebrow">RESILIENT AGENT CONTROL</p><h2>高可用协作控制面</h2></div></div>' +
      '<div class="card runtime-admission-card ' + (runtimeReady ? 'ready' : 'waiting') + '"><div class="card-head"><i class="ri-shield-check-line card-icon"></i><h4>RuntimeSupervisor 冷启动准入</h4>' + window.FinfluxUI.badge(runtimeSupervisor.state || "STARTING") + '</div><div class="runtime-check-list">' + (checkRows || '<div class="runtime-check wait"><i class="ri-loader-4-line ri-spin"></i><span><b>正在建立验收快照</b><small>一键启动在五项检查完成前保持关闭</small></span><em>WAIT</em></div>') + '</div>' +
        (runtimeErrors.length ? '<div class="runtime-ops-wait"><b>运维 WAIT</b><p>' + esc(runtimeErrors.join(' · ')) + '</p>' + (remediation.length ? '<ul>' + remediation.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('') + '</ul>' : '') + '<small>日志：' + esc(runtimeSupervisor.log_path || "app/runtime/runtime_supervisor/events.jsonl") + '</small></div>' : '') +
        '<p class="card-note"><i class="ri-information-line"></i> ' + esc(runtimeReady ? '冷启动验收完成：新Run准入已开放；后台仍每5秒监测，Worker异常只重建同角色容器。' : (runtimeSupervisor.last_action || '正在执行冷启动验收；不会创建业务Run。')) + '</p>' +
        '<button id="btn-repair-runtime" class="btn ' + (runtimeReady ? 'btn-outline' : 'btn-primary') + ' btn-xl" type="button" ' + (runtimeBusy ? 'disabled' : '') + '><i class="ri-tools-line"></i> ' + (runtimeBusy ? '运行环境检查/修复中…' : '一键修复运行环境') + '</button></div>' +
      '<div class="card model-connection-card"><div class="card-head"><i class="ri-plug-2-line card-icon"></i><h4>现场拟接入配置</h4>' + window.FinfluxUI.badge(connected && mc.api_key_configured ? "READY" : "CONFIG_REQUIRED") + '</div>' +
        window.FinfluxUI.kv("Agent编排", '<b class="mono">AgentTeams ' + esc(r.platform_version || "v1.2.2") + '</b> · Manager → Case Lead → 按需Worker') +
        window.FinfluxUI.kv("模型提供方", '<b class="mono">' + esc(mc.provider || "NOT_CONFIGURED") + '</b> · ' + esc(mc.default_model || "NOT_CONFIGURED")) +
        window.FinfluxUI.kv("上游端点", '<span class="mono">' + esc(mc.provider_endpoint || "NOT_CONFIGURED") + '</span>') +
        window.FinfluxUI.kv("受控网关", '<span class="mono">' + esc(mc.gateway_endpoint || "NOT_REPORTED") + '</span> · 每次调用绑定Run/Agent/Task') +
        window.FinfluxUI.kv("API Key", mc.api_key_configured ? '<span class="badge badge-green">已配置 · 不回显</span>' : '<span class="badge badge-red">未配置</span>') +
        window.FinfluxUI.kv("Human签署", r.human_credentials_ready ? '<span class="badge badge-green">' + esc(r.human_identity || "READY") + '</span>' : '<span class="badge badge-amber">待配置</span>') +
        (active.run_id ? '<div class="live-model-proof"><b>当前Run真实模型证明</b><span>' + esc(String(ledger.status || usage.status || "PENDING")) + ' · ' + window.FinfluxUI.fmtNum(usage.call_count || ledger.provider_call_count || 0) + '次调用 · ' + window.FinfluxUI.fmtNum(usage.total_tokens || ledger.total_tokens || 0) + ' Token</span>' + (ledger.fuse_reason ? '<em>' + esc(ledger.fuse_reason) + '</em>' : '') + '</div>' : '') +
        '<p class="card-note"><i class="ri-lock-line"></i> 前端只展示非敏感连接状态；模型密钥仍由本地.env注入，不写入浏览器或审计包。</p></div>' +
      '<div class="card token-guard-card ' + (guard.allowed ? 'ready' : 'blocked') + '"><div class="card-head"><i class="ri-gauge-line card-icon"></i><h4>模型用量与并发边界</h4>' + window.FinfluxUI.badge(guard.status) + '</div><div class="ha-stats"><span><b>' + esc(active.run_id ? window.FinfluxUI.fmtNum(usage.total_tokens || ledger.total_tokens || 0) : '—') + '</b>当前Run真实Token</span><span><b>' + esc(active.run_id ? window.FinfluxUI.fmtNum(usage.call_count || ledger.provider_call_count || 0) : '—') + '</b>供应商调用</span><span><b>' + esc(guard.active_run_count == null ? '—' : guard.active_run_count) + '</b>活动Run</span></div><p class="card-note">' + esc((guard.reasons || []).length ? guard.reasons.join(' · ') : '当前可创建一个新Run；执行时仍允许模型按需推理。') + '</p><small>边界用于阻止失控循环，不替代语义Agent推理；真实Token来自模型网关usage。</small></div>' +
      '<div class="card ha-card"><div class="card-head"><i class="ri-heart-pulse-line card-icon"></i><h4>RunSupervisor 后台推进</h4>' + window.FinfluxUI.badge(supervisor.state || "STOPPED") + '</div><div class="ha-stats"><span><b>' + esc(cp.topology_ready || 0) + '/' + esc(cp.topology_expected || 0) + '</b>角色就绪</span><span><b>' + esc(supervisor.recovery_count || 0) + '/3</b>同Run恢复</span><span><b>' + esc(supervisor.interval_seconds || 5) + 's</b>同步间隔</span></div><p class="card-note">' + esc(supervisor.last_action || cp.recommended_action || "等待后台状态") + (supervisor.wait_reason ? ' · ' + esc(supervisor.wait_reason) : '') + '</p><small>后台独立同步Matrix、Worker产物和模型网关账本；关闭或刷新浏览器不会中断Run。</small><button id="btn-reconcile" class="btn btn-outline" type="button"><i class="ri-refresh-line"></i> 仅执行控制面健康检查</button></div>' +
      '<div class="card"><div class="card-head"><i class="ri-node-tree card-icon"></i><h4>AgentTeams v1.2.2</h4>' + window.FinfluxUI.badge(connected ? "CONNECTED" : "OFFLINE") + '</div><div class="runtime-role-list"><div><span>Global Manager</span><b>' + esc((topology.default || {}).phase || "MISSING") + '</b></div><div><span>FinFlux Case Lead</span><b>' + esc((topology["finchange-case-lead"] || {}).phase || "MISSING") + '</b></div>' + workers.map(function (item) { return '<div><span>' + esc(item.display_name || item.label || item.name) + '</span><b>' + esc(item.phase || "MISSING") + '</b></div>'; }).join('') + '</div></div>' +
      '<div class="three-step-guide"><h4>现场演示只做三步</h4><ol><li><b>说任务</b><span>一句话说明要核验什么</span></li><li><b>给证据</b><span>文件、URL、资料库或已有EvidenceBundle</span></li><li><b>看结果</b><span>系统自动准入；门禁通过才派发AgentTeams</span></li></ol></div></section>';
  }

  function busy(button, label) { button.disabled = true; button.dataset.old = button.innerHTML; button.innerHTML = '<i class="ri-loader-4-line ri-spin"></i> ' + esc(label); }
  function restore(button) { button.disabled = false; if (button.dataset.old) button.innerHTML = button.dataset.old; }

  function finishRunStart(host, run) {
    return window.FinfluxAPI.selectRun(run.run_id).then(function () {
      var dispatched = Boolean(run.agentteams_run_id);
      window.FinfluxUI.refreshTopbar();
      if (dispatched) {
        window.FinfluxUI.toast("真实 AgentTeams 已在后台启动；本页不会自动跳转，请按当前Run卡片查看处理位置 · " + run.run_id, "success");
        return render(host, true).then(function () { return run; });
      }
      window.FinfluxUI.toast("Run已固化；如被门禁排队，本页会显示占用Run的真实状态和正确处理入口 · " + run.run_id, "warning");
      return render(host, true).then(function () { return run; });
    });
  }

  function bind(host, ws, registry) {
    host.querySelectorAll("[data-intake-tab]").forEach(function (tab) { tab.addEventListener("click", function () { host.querySelectorAll("[data-intake-tab]").forEach(function (item) { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", item === tab ? "true" : "false"); }); host.querySelectorAll("[data-intake-pane]").forEach(function (pane) { pane.classList.toggle("active", pane.getAttribute("data-intake-pane") === tab.getAttribute("data-intake-tab")); }); }); });
    var fileForm = host.querySelector("#intake-file-form"), fileInput = host.querySelector("#live-file"), dropZone = host.querySelector("#smart-drop-zone"), inspection = null, continueInspection = null;
    function bindInspectionCommit(box, result, instructionSelector, autoContinue) {
      inspection = result; renderInspection(box, result, registry);
      var commit = box.querySelector("#btn-commit-inspection");
      if (!runtimeGateReady(ws)) { commit.disabled = true; commit.title = "Runtime冷启动验收未通过，禁止创建Run"; }
      function continuePipeline() {
        if (!runtimeGateReady(ws)) { window.FinfluxUI.toast("Runtime冷启动验收未通过；请先执行一键修复运行环境", "error"); return; }
        if (commit.dataset.running === "true") return;
        var confirmations = {};
        box.querySelectorAll("[data-confirm]").forEach(function (input) { confirmations[input.getAttribute("data-confirm")] = input.type === "number" ? Number(input.value) : input.value.trim(); });
        var instruction = host.querySelector(instructionSelector);
        confirmations.task_instruction = instruction ? instruction.value.trim() : "";
        if (!confirmations.task_instruction) { window.FinfluxUI.toast("请先说明希望AgentTeams完成什么", "error"); return; }
        commit.dataset.running = "true";
        busy(commit, "校验哈希并固化Case中…");
        window.FinfluxAPI.commitInspection({ inspection_id: result.inspection_id, confirmations: confirmations }).then(function (res) {
          window.FinfluxUI.toast("证据与任务已组合封印 · " + res.evidence_bundle_id, "success");
          var committedProfile = profileById(registry, res.profile);
          if (!committedProfile || !committedProfile.live_executable) {
            window.FinfluxUI.toast("WAIT：未识别已登记金融Profile；已列出补充项，未调用模型", "warning");
            return render(host, true);
          }
          return window.FinfluxAPI.startRun(res.submission_id).then(function (run) {
            return finishRunStart(host, run);
          });
        }).catch(function (err) { commit.dataset.running = "false"; window.FinfluxUI.toast("证据不会丢失；后续链路未启动：" + err.message, "error"); return render(host, true); });
      }
      continueInspection = continuePipeline;
      commit.addEventListener("click", continuePipeline);
      // File selection is preview-only. Only an explicit primary-button click
      // may continue through commit, Run creation and real AgentTeams dispatch.
      // URL/catalog forms already use their submit button as that explicit act.
      if (autoContinue && !(result.required_confirmations || []).length) continuePipeline();
    }
    function inspectSelectedFile(file, autoContinue) {
      if (!file) return;
      var box = host.querySelector("#file-inspection-result");
      box.innerHTML = '<div class="inspection-loading"><i class="ri-loader-4-line ri-spin"></i><b>正在用0 Token识别文件…</b><span>只解析结构、哈希与确定性契约，不派发AgentTeams</span></div>';
      var fd = new FormData(); fd.append("file", file); fd.append("metadata", JSON.stringify({ task_instruction: host.querySelector("#case-instruction").value.trim(), input_mode: "FILE_PLUS_INTENT" }));
      window.FinfluxAPI.inspectFile(fd).then(function (result) {
        bindInspectionCommit(box, result, "#case-instruction", Boolean(autoContinue));
      }).catch(function (err) { box.innerHTML = '<p class="inline-warning"><i class="ri-error-warning-line"></i>' + esc(err.message) + '</p>'; window.FinfluxUI.toast(err.message, "error"); });
    }
    if (fileForm) fileForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!runtimeGateReady(ws)) { window.FinfluxUI.toast("Runtime冷启动验收未通过；当前不会创建Run", "error"); return; }
      var instruction = host.querySelector("#case-instruction").value.trim();
      if (!instruction) { window.FinfluxUI.toast("请先说明希望AgentTeams完成什么", "error"); return; }
      if (fileInput.files[0]) {
        var selectedFile = fileInput.files[0];
        var inspectedFile = inspection && inspection.file;
        if (continueInspection && inspectedFile && inspectedFile.name === selectedFile.name && Number(inspectedFile.size_bytes) === Number(selectedFile.size)) {
          continueInspection();
        } else {
          inspectSelectedFile(selectedFile, true);
        }
        return;
      }
      var current = ws.latest_submission || ws.submission;
      if (!current) { window.FinfluxUI.toast("请附加文件，或先从公开URL/真实资料库固化证据", "error"); return; }
      var btn = fileForm.querySelector("button[type=submit]"); busy(btn, "封印Case并检查Token Guard…");
      window.FinfluxAPI.startRun(current.submission_id, instruction).then(function (run) { return finishRunStart(host, run); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); restore(btn); });
    });
    if (fileInput) fileInput.addEventListener("change", function () { continueInspection = null; inspectSelectedFile(fileInput.files[0], false); });
    if (dropZone) {
      ["dragenter", "dragover"].forEach(function (name) { dropZone.addEventListener(name, function (event) { event.preventDefault(); dropZone.classList.add("drag-active"); }); });
      ["dragleave", "drop"].forEach(function (name) { dropZone.addEventListener(name, function (event) { event.preventDefault(); dropZone.classList.remove("drag-active"); }); });
      dropZone.addEventListener("drop", function (event) { var file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]; if (file) { continueInspection = null; inspectSelectedFile(file, false); } });
    }
    var urlForm = host.querySelector("#intake-url-form"); if (urlForm) urlForm.addEventListener("submit", function (event) { event.preventDefault(); var btn = urlForm.querySelector("button[type=submit]"); var instruction = host.querySelector("#url-instruction").value.trim(); if (!instruction) { window.FinfluxUI.toast("请先说明希望AgentTeams核验什么", "error"); return; } busy(btn, "受控采集与识别中…"); var meta = { profile: "auto", rights_basis: "公开URL；提交人声明按来源许可用于本次核验", task_instruction: instruction, input_mode: "PUBLIC_URL_PLUS_INTENT", permitted_usage_scope: "EVALUATION_ONLY", research_context_required: false, operational_risk_review_required: false }; window.FinfluxAPI.createUrlEvidence({ url: host.querySelector("#url-value").value, metadata: meta }).then(function (res) { window.FinfluxUI.toast("公开资料已采集，等待确认 · " + res.inspection_id, "success"); bindInspectionCommit(host.querySelector("#url-inspection-result"), res, "#url-instruction", true); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); }).finally(function () { restore(btn); }); });
    var search = host.querySelector("#btn-catalog-search"), selected = []; if (search) search.addEventListener("click", function () { busy(search, "检索中…"); window.FinfluxAPI.searchResearchCatalog(host.querySelector("#catalog-query").value, host.querySelector("#catalog-provider").value, "").then(function (res) { selected = []; var box = host.querySelector("#catalog-results"); box.innerHTML = res.items.length ? res.items.map(function (item) { return '<label class="catalog-item"><input type="checkbox" value="' + esc(item.research_item_id) + '"><span><b>' + esc(item.title) + '</b><small>' + esc(item.provider_id) + ' · ' + esc(item.publisher) + ' · ' + esc(item.published_at) + '</small></span><em>' + esc(item.rights_state) + '</em></label>'; }).join('') : '<p class="card-note">未找到匹配记录。请尝试证券代码、机构或主题。</p>'; box.querySelectorAll("input[type=checkbox]").forEach(function (input) { input.addEventListener("change", function () { selected = Array.from(box.querySelectorAll("input:checked")).map(function (node) { return node.value; }); host.querySelector("#btn-catalog-pack").disabled = !selected.length; }); }); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); }).finally(function () { restore(search); }); });
    var catalogForm = host.querySelector("#intake-catalog-form"); if (catalogForm) catalogForm.addEventListener("submit", function (event) { event.preventDefault(); if (!selected.length) return; var btn = host.querySelector("#btn-catalog-pack"); busy(btn, "组装与识别证据包中…"); window.FinfluxAPI.createResearchEvidence({ research_item_ids: selected, metadata: { declared_purpose: "research_review", asset_class: "research", entity_query: host.querySelector("#catalog-query").value, task_instruction: host.querySelector("#catalog-instruction").value.trim(), input_mode: "RESEARCH_CATALOG_PLUS_INTENT" } }).then(function (res) { window.FinfluxUI.toast("真实资料包已生成，等待确认 · " + res.inspection_id, "success"); bindInspectionCommit(host.querySelector("#catalog-inspection-result"), res, "#catalog-instruction", true); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); }).finally(function () { restore(btn); }); });
    var start = host.querySelector("#btn-fresh-run"), currentSubmission = ws.latest_submission || ws.submission; if (start && currentSubmission) start.addEventListener("click", function () { if (!runtimeGateReady(ws)) { window.FinfluxUI.toast("Runtime尚未准入，未创建Run", "error"); return; } busy(start, "创建Case并检查Token Guard…"); window.FinfluxAPI.startRun(currentSubmission.submission_id).then(function (run) { return window.FinfluxAPI.selectRun(run.run_id).then(function () { window.FinfluxUI.toast("Run已创建 · " + run.run_id, "success"); window.FinfluxUI.refreshTopbar(); return render(host, true); }); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); restore(start); }); });
    var dispatch = host.querySelector("#btn-dispatch-agentteams"); if (dispatch && ws.run) dispatch.addEventListener("click", function () { if (!runtimeGateReady(ws)) { window.FinfluxUI.toast("Runtime尚未准入，禁止派发", "error"); return; } busy(dispatch, "Matrix派发中…"); window.FinfluxAPI.dispatchRun(ws.run.run_id).then(function () { window.FinfluxUI.toast("已提交真实AgentTeams", "success"); return render(host, true); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); return render(host, true); }); });
    var blocking = host.querySelector("#btn-resolve-blocking-run"); if (blocking) blocking.addEventListener("click", function () {
      var runId = blocking.getAttribute("data-run-id"); busy(blocking, "核验占用Run真实状态…");
      window.FinfluxAPI.getRunStatus(runId).then(function (status) {
        return window.FinfluxAPI.selectRun(runId).then(function () {
          var executionState = String(status.execution_state || "UNKNOWN");
          var humanState = String(status.human_state || "NOT_OPENED");
          window.FinfluxUI.refreshTopbar();
          if (humanState === "AWAITING_HUMAN") {
            window.FinfluxUI.toast("该Run已有DataPass，进入Human Gate进行签署", "success");
            window.location.hash = "#/human-gate";
            return;
          }
          if (status.agentteams_bound && ["RUNNING", "AGENTTEAMS_RUNNING", "MANAGER_AUTHORIZED", "WORKERS_RUNNING"].indexOf(executionState) >= 0) {
            window.FinfluxUI.toast("该Run仍由AgentTeams执行；进入多Agent决策台查看进度，不进入Human Gate", "warning");
            window.location.hash = "#/collaboration";
            return;
          }
          window.FinfluxUI.toast("该Run状态为 " + executionState + "，尚无DataPass，不能签署；已切换到该Run并显示处理原因", "warning");
          window.location.hash = "#/live";
          return render(host, true);
        });
      }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); restore(blocking); });
    });
    var inspectBlocking = host.querySelector("#btn-inspect-blocking-run"); if (inspectBlocking) inspectBlocking.addEventListener("click", function () {
      var runId = inspectBlocking.getAttribute("data-run-id"), detail = host.querySelector("#blocking-run-detail");
      busy(inspectBlocking, "读取占用Run快照…");
      window.FinfluxAPI.getRunStatus(runId).then(function (status) {
        var progress = status.worker_progress || {}, supervisor = status.run_supervisor || {}, usage = status.provider_usage || {};
        detail.hidden = false;
        detail.innerHTML = '<b>占用Run控制面快照</b><div class="blocking-run-detail-grid"><span>Run状态<strong>' + esc(status.execution_state || "UNKNOWN") + '</strong></span><span>Worker产物<strong>' + esc(progress.completed || 0) + '/' + esc(progress.required || 0) + '</strong></span><span>恢复次数<strong>' + esc(supervisor.run_id === runId ? (supervisor.recovery_count || 0) : "—") + '/3</strong></span><span>真实Token<strong>' + esc((usage.total_tokens == null ? "未归集" : window.FinfluxUI.fmtNum(usage.total_tokens))) + '</strong></span></div><p>Supervisor：' + esc(supervisor.run_id === runId ? (supervisor.last_action || "观察中") : "当前快照未由Supervisor占用") + '。此处只展示状态，不会切换当前排队Run。</p>';
        restore(inspectBlocking);
      }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); restore(inspectBlocking); });
    });
    var openRelease = host.querySelector("#btn-open-release-occupancy"), releasePanel = host.querySelector("#release-occupancy-panel");
    if (openRelease && releasePanel) openRelease.addEventListener("click", function () { releasePanel.hidden = false; openRelease.disabled = true; host.querySelector("#release-occupancy-reason").focus(); });
    var cancelRelease = host.querySelector("#btn-cancel-release-occupancy"); if (cancelRelease && releasePanel && openRelease) cancelRelease.addEventListener("click", function () { releasePanel.hidden = true; openRelease.disabled = false; });
    var confirmRelease = host.querySelector("#btn-confirm-release-occupancy"); if (confirmRelease && openRelease) confirmRelease.addEventListener("click", function () {
      var runId = openRelease.getAttribute("data-run-id"), queuedRunId = openRelease.getAttribute("data-queued-run-id"), reason = host.querySelector("#release-occupancy-reason").value.trim();
      if (reason.length < 8) { window.FinfluxUI.toast("请填写至少8个字符的释放原因，写入审计链", "error"); return; }
      busy(confirmRelease, "正在终止占用并关闭网关…");
      window.FinfluxAPI.releaseRunOccupancy(runId, reason).then(function (receipt) {
        var nextRunId = receipt.next_queued_run_id || queuedRunId;
        window.FinfluxUI.toast(receipt.status === "ALREADY_FREE" ? "占用已释放；Supervisor将接管排队Run" : "占用Run已终止为WAIT；当前Run将在Supervisor下一轮启动", "success");
        return window.FinfluxAPI.selectRun(nextRunId).then(function () {
          window.FinfluxUI.refreshTopbar();
          return render(host, true).then(function () {
            window.setTimeout(function () { render(host, true); }, Math.max(1500, Number(receipt.supervisor_dispatch_eta_seconds || 5) * 1000 + 500));
          });
        });
      }).catch(function (err) { window.FinfluxUI.toast("释放未执行：" + err.message, "error"); restore(confirmRelease); });
    });
    var repair = host.querySelector("#btn-repair-agentteams"); if (repair && ws.run) repair.addEventListener("click", function () { busy(repair, "AgentTeams诊断同Run失败…"); window.FinfluxAPI.repairRun(ws.run.run_id).then(function (receipt) { var ok = ["CASE_LEAD_RECOVERY_REVIEW", "MISSING_WORKERS_REAWAKENED", "REQUESTED"].indexOf(receipt.status) >= 0; var message = receipt.status === "MISSING_WORKERS_REAWAKENED" ? "Case Lead授权已复用；仅缺失Worker被重新唤醒" : (ok ? "Case Lead已收到同Run恢复任务" : "恢复未越过门禁：" + receipt.status); window.FinfluxUI.toast(message, ok ? "success" : "warning"); return render(host, true); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); return render(host, true); }); });
    var reconcile = host.querySelector("#btn-reconcile"); if (reconcile) reconcile.addEventListener("click", function () { busy(reconcile, "检查中…"); window.FinfluxAPI.reconcileControlPlane().then(function (res) { window.FinfluxUI.toast("控制面快照已固化 · " + short(res.snapshot_sha256), "success"); return render(host, true); }).catch(function (err) { window.FinfluxUI.toast(err.message, "error"); restore(reconcile); }); });
    var repairRuntime = host.querySelector("#btn-repair-runtime"); if (repairRuntime) repairRuntime.addEventListener("click", function () { busy(repairRuntime, "正在修复端口、容器、路由与包摘要…"); window.FinfluxAPI.repairRuntime("现场操作：修复Runtime冷启动验收失败项").then(function () { window.FinfluxUI.toast("RuntimeSupervisor已接管修复；完成真实模型canary后才会开放Run", "success"); return render(host, true); }).catch(function (err) { window.FinfluxUI.toast("修复请求失败：" + err.message, "error"); restore(repairRuntime); }); });
  }

  function render(host, refresh) { return Promise.all([window.FinfluxAPI.getWorkspace(Boolean(refresh)), window.FinfluxAPI.getIntakeCapabilities(), window.FinfluxAPI.getTokenGuard(), window.FinfluxAPI.getProfiles()]).then(function (values) { var ws = values[0], capabilities = values[1], guard = values[2], registry = values[3]; ws.token_guard = guard; host.innerHTML = '<div class="truth-banner"><i class="ri-shield-check-line"></i><b>工业接入边界</b><span>' + esc(capabilities.truth_boundary) + ' · ' + esc(registry.count) + '个版本化Profile驱动当前界面</span></div><div class="unified-intake-layout">' + intakeWorkbench(capabilities, ws.runtime_supervisor) + submissionCard(ws, registry) + runtimeColumn(ws, guard) + '</div>'; bind(host, ws, registry); scheduleLivePoll(host, liveStateSignature(ws, guard)); }); }
  window.FinfluxViews = window.FinfluxViews || {}; window.FinfluxViews.live = render;
})();
