/* ============================================================
   页面 2 · 证据与决策（#/evidence）
   五栏：不可变证据 / 金融语义契约 / 确定性影响复算 /
   三方 Worker 复核 / DataPass 与 Human Gate + 底部通栏
   ============================================================ */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };

  function fileIcon(kind) {
    return kind === "pdf" ? "ri-file-pdf-2-line" : kind === "csv" ? "ri-file-list-3-line" : "ri-braces-line";
  }

  /* ---------------- 栏 1：不可变证据 ---------------- */
  function colEvidence(ev) {
    var ui = UI();
    var eb = ev.evidence_bundle;
    var sha = ui.shaShort, sc = ui.esc;

    var officialDocs = '<div class="file-row"><i class="ri-shield-keyhole-line"></i>' +
      '<span class="file-name" title="' + sc(eb.rights_basis) + '">来源与使用依据</span>' +
      '<span class="mini-badge mini-blue">' + sc(eb.rights_status) + '</span></div>';

    var fileRows = eb.files.map(function (f) {
      return '<div class="file-row">' +
        '<i class="' + fileIcon(f.kind) + '"></i>' +
        '<span class="file-name" title="' + sc(f.role) + "｜SHA256 " + sc(f.sha256) + '">' + sc(f.name) + "</span>" +
        '<span class="file-size mono">' + ui.fmtNum(f.size_kb, 1) + " KB</span>" +
        '<span class="mini-badge mini-green">已验证</span></div>';
    }).join("");

    return '<section class="col-panel">' +
      UI().sectionHead("IMMUTABLE EVIDENCE", "不可变证据", ui.badge(eb.integrity_status, "已登记 " + eb.files.length + " 项")) +
      '<div class="card">' +
        ui.kv("证据包 ID", '<span class="mono">' + sc(eb.evidence_bundle_id) + "</span>" + ui.copyBtn(eb.evidence_bundle_id)) +
        ui.kv("上传来源", sc(eb.declared_source)) +
        ui.kv("提供方", sc(eb.provider) + ' <span class="mini-badge mini-green">已验证</span>') +
        ui.kv("接收时间", '<span class="mono">' + sc(eb.received_at) + "</span>") +
        ui.kv("原始响应 SHA256", '<span class="mono">' + sc(sha(eb.raw_response_sha256, 14, 4)) + "</span>" + ui.copyBtn(eb.raw_response_sha256)) +
        ui.kv("使用依据", ui.badge(eb.rights_status)) +
        ui.kv("完整性状态", sc(eb.integrity_status)) +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-file-pdf-2-line card-icon"></i><h4>来源与权利声明</h4></div>' + officialDocs +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-archive-stack-line card-icon"></i><h4>证据文件清单</h4></div>' +
        '<div class="file-list file-list-tall">' + fileRows + "</div>" +
      "</div>" +
      '<div class="anchor-strip">' +
        '<p><i class="ri-database-2-line"></i> 证据首次入库 <span class="mono">' + sc(eb.first_stored_at) + "</span></p>" +
        '<p><i class="ri-link"></i> 版本校验 <span class="mini-badge mini-blue">SHA256</span> <span class="mono">' + sc(sha(eb.raw_response_sha256, 14, 4)) + "</span></p>" +
      "</div>" +
    "</section>";
  }

  /* ---------------- 栏 2：金融语义契约 ---------------- */
  function colContract(ev) {
    var ui = UI();
    var c = ev.semantic_contract;
    var discovery = ev.semantic_discovery || {};
    var sc = ui.esc, sha = ui.shaShort;

    var proposalRows = (discovery.proposals || []).map(function (p) {
      var confidence = p.confidence_bps == null ? "—" : (Number(p.confidence_bps) / 100).toFixed(0) + "%";
      return '<div class="agent-proposal">' +
        '<div class="agent-proposal-head"><span>' + sc(p.role) + '</span><b class="mono">' + sc(p.proposed_field) + '</b><em>' + sc(confidence) + '</em></div>' +
        '<p>' + sc(p.proposed_semantic || "待解释") + '</p>' +
        '<small>' + sc(p.reason_code || "NO_REASON_CODE") + ' · 不确定性：' + sc(p.uncertainty_code || "NOT_DECLARED") + '</small>' +
      '</div>';
    }).join("");

    var fields = c.key_fields.map(function (f) {
      return '<div class="field-sem">' +
        '<div class="field-sem-head"><code>' + sc(f.field) + "</code><span>" + sc(f.semantics) + "</span></div>" +
        '<p>' + sc(f.mapping_note) + "</p>" +
      "</div>";
    }).join("");

    var constraints = c.constraints.map(function (rule) {
      return '<div class="rule-row"><i class="ri-checkbox-circle-fill text-green"></i><span>' + sc(rule) + "</span></div>";
    }).join("");

    return '<section class="col-panel">' +
      ui.sectionHead("SEMANTIC CONTRACT", "金融语义契约", '<span class="chip chip-cyan mono">' + sc(c.version) + "</span>") +
      '<div class="card semantic-discovery-card">' +
        '<div class="card-head"><i class="ri-brain-line card-icon"></i><h4>Agent 自主语义发现</h4>' + ui.badge(discovery.agreement ? "AGREED" : "REVIEW_REQUIRED") + '</div>' +
        (proposalRows || '<p class="card-note">尚未获得模型Agent语义候选；系统不会用Profile默认值冒充结论。</p>') +
        '<div class="semantic-proof"><span><b>' + sc(discovery.model_calls == null ? "—" : discovery.model_calls) + '</b>真实模型调用</span><span><b>' + sc(discovery.model_tokens == null ? "—" : ui.fmtNum(discovery.model_tokens)) + '</b>真实Token</span><span><b>' + sc(discovery.skill_status || "NOT_RUN") + '</b>Skill验真</span></div>' +
        '<p class="card-note"><i class="ri-shield-check-line"></i> Profile只限定可验证边界；字段选择来自本Run模型Agent提议，不是前端下拉框或固定if/else。</p>' +
      '</div>' +
      '<div class="card">' +
        ui.kv("声明用途", "<b>" + sc(c.downstream_purpose) + "</b>") +
        ui.kv("适用域", sc(c.applicable_domain)) +
        ui.kv("当前候选", '<b class="mono">' + sc(c.selected_field || "尚未形成一致候选") + '</b>') +
        ui.kv("形成方式", sc(c.resolution_source || "PROFILE_BOUNDARY_ONLY")) +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-key-2-line card-icon"></i><h4>关键字段语义</h4></div>' + fields +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-scales-3-line card-icon"></i><h4>语义约束</h4></div>' + constraints +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-book-marked-line card-icon"></i><h4>契约引用</h4></div>' +
        ui.kv("名称", sc(c.name) + " " + sc(c.version)) +
        ui.kv("规范实现", '<span class="mono">' + sc(c.canonical_skill) + "</span>") +
        ui.kv("SHA256", '<span class="mono">' + sc(sha(c.sha256, 14, 4)) + "</span>" + ui.copyBtn(c.sha256)) +
        ui.kv("契约状态", ui.badge(c.validation_state)) +
      "</div>" +
    "</section>";
  }

  /* ---------------- 栏 3：确定性影响复算 ---------------- */
  function colImpact(ev) {
    var ui = UI();
    var im = ev.impact;
    var sc = ui.esc;

    var basis = (im.basis || []).map(function (b) {
      return '<tr><td>' + sc(b.item) + '</td><td class="mono">' + sc(b.value) + '</td><td class="mono src">' + sc(b.source_file) + "</td></tr>";
    }).join("");
    var candidate = im.candidate || {}, required = im.required || {};
    var conflict = Boolean(im.conflict);
    var impactState = !im.available ? "尚未形成可复算指标" : (conflict ? "计算已完成 · 发现冲突" : "计算已完成 · 契约一致");
    var impactTone = !im.available ? "badge-amber" : (conflict ? "badge-red" : "badge-green");
    var advice = !im.available ? "WAIT" : (conflict ? "BLOCK" : "PASS");
    return '<section class="col-panel">' +
      ui.sectionHead("DETERMINISTIC IMPACT", "确定性影响复算", '<span class="badge ' + impactTone + '"><i class="dot"></i>' + impactState + '（非模型输出）</span>') +

      '<div class="conflict-banner ' + (conflict ? '' : 'conflict-ok') + '">' +
        '<i class="' + (conflict ? 'ri-error-warning-fill' : 'ri-checkbox-circle-fill') + '"></i>' +
        "<div><b>" + sc(im.title) + "</b><p>" + sc(im.explanation) + "</p></div>" +
      "</div>" +

      (im.available ? '<div class="ab-compare">' +
        '<div class="ab-card ab-a"><span class="ab-tag">A</span>' +
          '<code>' + sc(candidate.field) + '</code> = <b class="mono">' + ui.fmtNum(candidate.value, 4) + "</b> " + sc(candidate.unit) +
          '<p class="mono src">来源 ' + sc(candidate.source_file) + "</p></div>" +
        '<div class="ab-vs"><i class="ri-arrow-left-right-line"></i></div>' +
        '<div class="ab-card ab-b"><span class="ab-tag">B</span>' +
          '<code>' + sc(required.field) + '</code> = <b class="mono">' + ui.fmtNum(required.value, 4) + "</b> " + sc(required.unit) +
          '<p class="mono src">来源 ' + sc(required.source_file) + "</p></div>" +
      "</div>" : '') +

      '<div class="formula-card"><span class="mono">' + sc(im.formula) + "</span></div>" +

      '<div class="card impact-card">' +
        '<div class="card-head"><i class="ri-calculator-line card-icon"></i><h4>确定性影响</h4></div>' +
        '<p class="impact-formula">' + sc(im.value_label) + "</p>" +
        '<p class="impact-number ' + (conflict ? 'text-red' : 'text-green') + ' mono">' + (im.value == null ? '尚未计算' : ui.fmtNum(im.value, 2) + ' ' + sc(im.value_unit)) + "</p>" +
        '<table class="mini-table"><thead><tr><th>计算依据</th><th>取值</th><th>来源</th></tr></thead><tbody>' + basis + "</tbody></table>" +
      "</div>" +

      '<div class="conclusion-strip ' + (conflict ? 'conclusion-block' : 'conclusion-pass') + '"><i class="' + (conflict ? 'ri-error-warning-fill' : (im.available ? 'ri-checkbox-circle-fill' : 'ri-pause-circle-line')) + '"></i><span><b>协作建议：' + advice + '</b> · 模型负责理解，Skill负责验真，最终仍由Human Gate授权</span></div>' +
    "</section>";
  }

  /* ---------------- 栏 4：按 Manager 路由选择的 Worker 复核 ---------------- */
  function colWorkers(ev) {
    var ui = UI();
    var sc = ui.esc, sha = ui.shaShort;

    var numerals = ["①", "②", "③", "④", "⑤"];
    var complete = ev.workers.filter(function (w) { return Boolean(w.completed); }).length;
    var cards = ev.workers.map(function (w, i) {
      return '<div class="card worker-card topo-' + w.color + '" data-worker="' + i + '">' +
        '<div class="card-head"><i class="ri-cpu-line card-icon"></i>' +
          "<h4>Worker " + (numerals[i] || String(i + 1)) + " " + sc(w.name) + ' <span class="mono ver">' + sc(w.version) + "</span></h4>" +
          ui.badge(w.execution_status || (w.completed ? "SEALED" : w.status)) + "</div>" +
        ui.kv("任务 ID", '<span class="mono">' + sc(w.task_id) + "</span>" + ui.copyBtn(w.task_id)) +
        ui.kv("结论", sc(w.conclusion)) +
        ui.kv("依据", sc(w.evidence_note)) +
        ui.kv("Sealed", '<span class="mono">' + sc(sha(w.seal_hash, 14, 4)) + "</span>" + ui.copyBtn(w.seal_hash)) +
        ui.kv("完成时间", '<span class="mono">' + sc(w.finished_at) + "</span>") +
      "</div>";
    }).join("");

    return '<section class="col-panel">' +
      ui.sectionHead("INDEPENDENT REVIEW", "路由选择的专业 Worker 复核", ui.badge(complete === ev.workers.length && ev.workers.length ? "SEALED" : "PENDING", complete + "/" + ev.workers.length)) +
      cards +
      '<div class="card consensus-card">' +
        '<div class="card-head"><i class="ri-group-line card-icon"></i><h4>一致性结果</h4></div>' +
        ui.kv("当前汇总", complete === ev.workers.length ? "路由所需产物已全部封存" : "仍在等待路由所需Worker产物") +
        ui.kv("证据完整性", '<b class="' + (complete === ev.workers.length ? "text-green" : "text-amber") + '">' + complete + "/" + ev.workers.length + "</b>") +
        ui.kv("生产授权", '<b>仍由Human Gate决定</b>') +
      "</div>" +
    "</section>";
  }

  /* ---------------- 栏 5：DataPass 与 Human Gate ---------------- */
  function colDataPass(ev) {
    var ui = UI();
    var dp = ev.datapass;
    var gate = ev.gate;
    var sc = ui.esc, sha = ui.shaShort;
    var available = Boolean(dp.available);
    var pending = available && gate.state === "AWAITING_HUMAN";

    var decidedHtml = ["APPROVED", "REJECTED", "RETURNED"].indexOf(gate.state) < 0 ? "" :
      '<div class="card">' +
        '<div class="card-head"><i class="ri-quill-pen-line card-icon"></i><h4>人工决定</h4>' + ui.badge(gate.decision === "APPROVE_PASS" ? "APPROVED" : gate.decision === "REJECT" ? "REJECTED" : "RETURNED", gate.label) + "</div>" +
        ui.kv("决定", sc(gate.decision)) +
        ui.kv("责任人", '<span class="mono">' + sc(gate.human_actor_id) + "</span>") +
        ui.kv("决定时间", '<span class="mono">' + sc(gate.decided_at) + "</span>") +
        ui.kv("理由", sc(gate.reason)) +
        ui.kv("签署后哈希", '<span class="mono">' + sc(sha(gate.post_decision_hash, 14, 4)) + "</span>" + ui.copyBtn(gate.post_decision_hash)) +
      "</div>";

    return '<section class="col-panel">' +
      ui.sectionHead("DATAPASS & HUMAN GATE", "DataPass 与 Human Gate") +

      '<div class="card datapass-card">' +
        '<p class="datapass-status ' + (available ? (dp.machine_recommendation === "PASS" ? "text-green" : "text-amber") : "text-muted") + '">' + sc(dp.machine_recommendation) + "</p>" +
        (!available ? '<p class="card-note"><i class="ri-information-line"></i> 当前仅有确定性预检，尚无真实Worker共识，因此不展示伪DataPass。</p>' : '') +
        ui.kv("建议动作", sc(dp.recommended_action)) +
        ui.kv("确定性影响", '<b class="mono ' + (dp.machine_recommendation === "BLOCK" ? 'text-red' : 'text-green') + '">' + (dp.impact && dp.impact.value != null ? ui.fmtNum(dp.impact.value, 2) + ' ' + sc(dp.impact.unit) : '尚未计算') + "</b>") +
        ui.kv("建议状态", sc(dp.admission_advice)) +
        ui.kv("Draft 哈希", available ? '<span class="mono">' + sc(sha(dp.draft_hash, 14, 4)) + "</span>" + ui.copyBtn(dp.draft_hash) : '<span class="text-muted">NOT_CREATED</span>') +
      "</div>" +

      '<div class="card">' +
        '<div class="card-head"><i class="ri-archive-stack-line card-icon"></i><h4>证据共识</h4></div>' +
        '<p class="card-note">' + sc(dp.evidence_consensus) + "</p>" +
      "</div>" +

      '<div class="card">' +
        '<div class="card-head"><i class="ri-group-line card-icon"></i><h4>Worker 共识</h4></div>' +
        ui.kv("独立结论一致", '<b class="' + (dp.worker_consensus.state === "SEALED" ? "text-green" : "text-muted") + '">' + dp.worker_consensus.agreed + "/" + dp.worker_consensus.total + (dp.worker_consensus.state === "SEALED" ? "" : " · 尚未形成共识") + "</b>") +
        ui.kv("Skill 版本一致", dp.worker_consensus.skill_versions_aligned ? '<b class="text-green">是</b>' : "否") +
        ui.kv("互不共享结论", dp.worker_consensus.isolation_verified ? '<b class="text-green">已验证</b>' : "未验证") +
      "</div>" +

      '<div class="card gate-card">' +
        '<div class="card-head"><i class="ri-user-follow-line card-icon"></i><h4>生产授权状态</h4>' + ui.badge(gate.state, gate.label) + "</div>" +
        ui.kv("授权类型", sc(gate.type)) +
        ui.kv("当前环节", sc(gate.current_stage)) +
        ui.kv("门控打开", '<span class="mono">' + sc(gate.gate_opened_at) + "</span>") +
      "</div>" +

      decidedHtml +

      '<div class="gate-actions" id="evidence-gate-actions">' +
        (pending ?
          (dp.machine_recommendation === "PASS" ?
          '<button type="button" class="btn btn-approve btn-xl" data-gate="APPROVE_PASS"><i class="ri-quill-pen-line"></i> 批准并签署 DataPass</button>' :
          '<button type="button" class="btn btn-primary btn-xl" data-gate="ADOPT_REMEDIATION"><i class="ri-tools-line"></i> 采用修正并重新核验</button>') +
          '<button type="button" class="btn btn-danger" data-gate="REJECT"><i class="ri-stop-circle-line"></i> 确认拦截并签署</button>' +
          '<button type="button" class="btn btn-outline" data-gate="RETURN_FOR_EVIDENCE"><i class="ri-arrow-go-back-line"></i> 退回补证</button>'
          : '<a class="btn btn-outline" href="#/changes">查看签署血缘与审计输出 <i class="ri-arrow-right-line"></i></a>') +
      "</div>" +
    "</section>";
  }

  function resultHero(ev) {
    var ui = UI(), result = ev.result || {}, states = ev.states || {};
    var disposition = states.business_disposition || {}, execution = states.execution_state || {}, terminal = states.lifecycle_terminal || {};
    return '<section class="datapass-result-hero ' + (result.tone === "positive" ? 'result-pass' : 'result-caution') + '"><div><p class="eyebrow">PLAIN-LANGUAGE RESULT</p><h2>' + ui.esc(result.title) + '</h2><p>' + ui.esc(result.summary) + '</p></div><div class="result-stage-pills"><span>执行 <b>' + ui.esc(execution.label || '尚未开始') + '</b></span><span>业务 <b>' + ui.esc(disposition.label || '尚未判断') + '</b></span><span>终态 <b>' + ui.esc(terminal.label || '未结束') + '</b></span></div></section>';
  }

  function profileStrip(ev) {
    var ui = UI(), profile = ev.profile_definition || {};
    return '<div class="profile-strip"><span><small>金融Profile</small><b>' + ui.esc(profile.display_name || ev.profile) + '</b></span><span><small>版本</small><b class="mono">' + ui.esc(profile.profile_version || '未登记') + '</b></span><span><small>业务用途</small><b>' + ui.esc(profile.purpose_label || '未声明') + '</b></span><span><small>Profile SHA256</small><b class="mono">' + ui.esc(ui.shaShort(profile.profile_sha256, 10, 5)) + '</b></span></div>';
  }

  /* ---------------- 底部通栏 ---------------- */
  function bottomStrip(ev) {
    var ui = UI();
    var sc = ui.esc;
    return '<div class="bottom-strip">' +
      '<div class="banner banner-blue">' +
        '<i class="ri-information-line"></i>' +
        "<div><b>责任说明</b><p>" + sc((ev.result || {}).truth_statement) + "</p></div>" +
      "</div>" +
      '<div class="formula-strip">' +
        '<div class="formula-item"><i class="ri-archive-stack-line"></i><span>不可变证据</span></div>' +
        '<span class="formula-op">+</span>' +
        '<div class="formula-item"><i class="ri-book-marked-line"></i><span>金融语义契约</span></div>' +
        '<span class="formula-op">+</span>' +
        '<div class="formula-item"><i class="ri-calculator-line"></i><span>确定性复算</span></div>' +
        '<span class="formula-op">=</span>' +
        '<div class="formula-item formula-result"><i class="ri-shield-check-line"></i><span>可审计的DataPass建议</span></div>' +
      "</div>" +
      '<div class="trace-overview">' +
        '<span>Run ID <b class="mono">' + sc(ev.ids.run_id) + "</b>" + ui.copyBtn(ev.ids.run_id) + "</span>" +
        '<span>证据包 <b class="mono">' + sc(ev.ids.evidence_bundle_id) + "</b></span>" +
        '<span>契约 <b class="mono">' + sc(ev.semantic_contract.name) + " " + sc(ev.semantic_contract.version) + "</b></span>" +
        '<span>更新时间 <b class="mono">' + sc(ev.updated_at) + "</b></span>" +
      "</div>" +
    "</div>";
  }

  /* ---------------- Human Gate 决定（与 human-gate 页共用真实后端状态） ---------------- */
  window.FinfluxGateAction = function (decision, onDone, prefill) {
    var ui = UI();
    var pre = prefill || {};
    var remediationPlan = pre.remediation_plan || null;
    var meta = {
      APPROVE_PASS: { title: "批准并签署 DataPass", icon: "ri-quill-pen-line", cls: "btn-approve", body: "批准后服务端将用已配置的 Human Matrix 身份写入真实 Room，并把签署事件、责任人和哈希关联到同一 Run。此操作不可撤销。" },
      ADOPT_REMEDIATION: { title: "采用修订方案并重新核验", icon: "ri-tools-line", cls: "btn-primary", body: "系统将保留当前证据版本，创建带血缘的修订Run，并按新任务计划重新完成专业核验。" },
      REJECT: { title: "阻止准入", icon: "ri-stop-circle-line", cls: "btn-danger", body: "阻止后本 Run 以 REJECTED 终结，原始 Run 保持不可变；如需重试将创建带血缘的 child Run。" },
      RETURN_FOR_EVIDENCE: { title: "退回补证", icon: "ri-arrow-go-back-line", cls: "btn-primary", body: "退回后当前 Run 保持 AWAITING_HUMAN 审计记录，系统提示补充缺失证据后重新协调。" },
    }[decision];

    ui.confirmDialog({
      title: meta.title, icon: meta.icon, confirmClass: meta.cls, confirmText: meta.title,
      bodyHtml:
        "<p>" + meta.body + "</p>" +
        (decision === "ADOPT_REMEDIATION" && remediationPlan ?
          '<div class="remediation-confirm"><span><small>当前候选</small><b class="mono">' + ui.esc(remediationPlan.from_field || "未确认") + '</b></span><i class="ri-arrow-right-line"></i><span><small>经Skill验证的修订候选</small><b class="mono">' + ui.esc(remediationPlan.target_field) + '</b></span></div><p class="lock-note"><i class="ri-fingerprint-line"></i> 原始证据不变；Human批准的是语义元数据修订和一次新的AgentTeams复核，不是直接改成PASS。</p>' : "") +
        '<div class="modal-form">' +
        '<label>责任人 ID<input type="text" id="gate-actor" value="' + ui.esc(pre.actor || "finops.reviewer01") + '" class="mono" ' + (pre.actor_locked ? "readonly" : "") + '></label>' +
        '<label>决定理由<textarea id="gate-reason" rows="2" placeholder="请填写结构化决定理由">' + ui.esc(pre.reason || "") + "</textarea></label>" +
        "</div>",
    }).then(function (confirmed) {
      if (!confirmed) return;
      var actorEl = document.getElementById("gate-actor");
      var reasonEl = document.getElementById("gate-reason");
      var actor = actorEl && actorEl.value.trim() ? actorEl.value.trim() : "finops.reviewer01";
      var reason = reasonEl ? reasonEl.value.trim() : "";
      window.FinfluxAPI.submitHumanDecision(decision, actor, reason, remediationPlan).then(function (res) {
        var dispatch = res.child_dispatch || {};
        var suffix = dispatch.status === "AGENTTEAMS_SUBMITTED" ? " · 修订子Run已真实派发" : (res.child_run ? " · 子Run已创建，派发门禁待处理" : "");
        ui.toast(meta.title + " 已提交 · " + res.matrix_notice + suffix, dispatch.status === "AGENTTEAMS_DISPATCH_FAILED" ? "warn" : "success");
        ui.refreshTopbar();
        if (typeof onDone === "function") onDone(res);
      }).catch(function (err) { ui.toast(err.message, "error"); });
    });
  };

  function render(host) {
    return window.FinfluxAPI.getEvidenceView().then(function (ev) {
      if (ev.empty) {
        host.innerHTML = '<div class="card empty-card"><i class="ri-inbox-archive-line"></i><h3>尚无真实 EvidenceBundle</h3><p>请先提交金融资料与核验目标；未产生的核验结果不会被补写。</p><a class="btn btn-primary" href="#/live">前往现场接入</a></div>';
        return;
      }
      host.innerHTML = resultHero(ev) + profileStrip(ev) +
        '<details class="evidence-technical" open><summary>展开同一Run的证据、契约、计算、Worker与DataPass技术细节</summary><div class="evidence-grid">' +
        colEvidence(ev) + colContract(ev) + colImpact(ev) + colWorkers(ev) + colDataPass(ev) +
        "</div></details>" +
        bottomStrip(ev);

      host.querySelectorAll("[data-gate]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          window.FinfluxGateAction(btn.getAttribute("data-gate"), function () { render(host); }, { remediation_plan: ev.remediation_plan || null });
        });
      });

      host.querySelectorAll(".worker-card").forEach(function (card) {
        card.addEventListener("click", function (e) {
          if (e.target.closest(".copy-btn")) return;
          var w = ev.workers[Number(card.getAttribute("data-worker"))];
          window.FinfluxUI.toast(w.name + " · 结论：" + w.conclusion, "info");
        });
      });
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.evidence = render;
})();
