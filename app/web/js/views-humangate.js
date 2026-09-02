/* Human Gate: plain-language outcome, remediation child Run and fixed reports. */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };
  var DECISION_LABEL = {
    APPROVE_PASS: ["批准使用", "green"],
    CONFIRM_BLOCK: ["隔离数据", "red"],
    REQUEST_EVIDENCE: ["退回补证", "amber"],
  };

  function historyTable(history) {
    var ui = UI();
    var rows = history.map(function (h) {
      var meta = DECISION_LABEL[h.decision] || [h.decision, "blue"];
      return "<tr>" +
        '<td class="mono">' + ui.esc(h.run_id) + "</td>" +
        "<td>" + ui.esc(h.case) + "</td>" +
        '<td><span class="mini-badge mini-' + meta[1] + '">' + meta[0] + "</span></td>" +
        '<td class="mono">' + ui.esc(h.actor) + "</td>" +
        '<td class="mono">' + ui.esc(h.decided_at) + "</td>" +
        "<td>" + ui.esc(h.reason) + "</td>" +
      "</tr>";
    }).join("");
    return '<div class="card">' +
      '<div class="card-head"><i class="ri-history-line card-icon"></i><h4>人工处理记录</h4></div>' +
      (rows ? '<div class="table-wrap"><table class="data-table"><thead><tr><th>Run ID</th><th>Case</th><th>决定</th><th>责任人</th><th>时间</th><th>理由</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="card-note">尚无人工处理记录。</p>') +
    "</div>";
  }

  function plainFinding(res) {
    var ui = UI(), view = res.presentation || {}, result = view.result || {}, impact = view.impact || {}, available = Boolean(res.datapass && res.datapass.available);
    if (!available) {
      return '<div class="decision-hero decision-neutral"><div class="decision-icon"><i class="ri-information-line"></i></div><div><p class="eyebrow">AGENT DECISION NOT AVAILABLE</p><h2>' + ui.esc(result.title || "尚未形成多Agent DataPass") + '</h2><p>' + ui.esc(result.summary || "当前仅有确定性预检，Human Gate尚未开启。") + '</p></div></div>';
    }
    var rec = String(res.datapass.machine_recommendation || "PENDING");
    var blocked = rec === "BLOCK", waiting = rec === "NEEDS_EVIDENCE";
    return '<div class="decision-hero ' + (blocked ? "decision-stop" : (waiting ? "decision-neutral" : "decision-go")) + '">' +
      '<div class="decision-icon"><i class="' + (blocked ? "ri-error-warning-line" : (waiting ? "ri-file-search-line" : "ri-checkbox-circle-line")) + '"></i></div>' +
      '<div><p class="eyebrow">专业核验已经完成</p><h2>' + ui.esc(result.title) + '</h2><p>' + ui.esc(result.summary) + '</p>' +
      (impact.available && impact.quantified !== false ? '<div class="decision-numbers"><span><b>' + ui.esc((impact.candidate || {}).value == null ? "—" : (impact.candidate || {}).value) + '</b>候选：' + ui.esc((impact.candidate || {}).label || (impact.candidate || {}).field) + '</span><span><b>' + ui.esc((impact.required || {}).value == null ? "—" : (impact.required || {}).value) + '</b>契约要求：' + ui.esc((impact.required || {}).label || (impact.required || {}).field) + '</span><span><b>' + ui.fmtNum(impact.value, 2) + ' ' + ui.esc(impact.value_unit) + '</b>' + ui.esc(impact.value_label) + '</span></div>' : (rec === "NEEDS_EVIDENCE" ? '<div class="result-explanation"><i class="ri-information-line"></i>当前证据只能确认事件条款，尚缺同标的、同日期且复权口径明确的行情序列；系统没有把“未知影响”显示成0。</div>' : '')) +
      '</div></div>';
  }

  function actionCard(res) {
    var ui = UI(), gate = res.gate, runtime = res.runtime || {}, rec = String(res.datapass.machine_recommendation || "PENDING");
    if (gate.state !== "AWAITING_HUMAN") return "";
    if (!runtime.human_credentials_ready) {
      return '<div class="card sign-card credential-blocked">' +
        '<div class="card-head"><i class="ri-key-2-line card-icon"></i><h4>Human签署通道尚未就绪</h4><span class="mini-badge mini-red">FAIL CLOSED</span></div>' +
        '<p class="card-note">DataPass草案已经完成，但系统没有读取到外置Matrix Human凭据，因此不会显示可提交按钮，也不会把模型建议伪装成人工授权。</p>' +
        '<div class="result-explanation"><i class="ri-terminal-box-line"></i>使用统一启动脚本并通过 <span class="mono">-RuntimeEnvFile</span> 指向现有AgentTeams外置.env；凭据不会复制进提交包。</div>' +
        '<p class="lock-note"><i class="ri-lock-2-line"></i> 当前Run保持 AWAITING_HUMAN，配置就绪后刷新本页即可签署，无需重跑Agent。</p>' +
      '</div>';
    }
    var blocked = rec === "BLOCK", waiting = rec === "NEEDS_EVIDENCE", view = res.presentation || {}, contract = view.semantic_contract || {}, dp = view.datapass || {}, remediation = view.remediation_plan || null;
    var identity = runtime.human_identity || "已配置Matrix Human";
    return '<div class="card sign-card">' +
      '<div class="card-head"><i class="ri-user-settings-line card-icon"></i><h4>负责人现在需要做什么</h4></div>' +
      '<p class="card-note">负责人对使用范围和处置动作签署；决定将写入Matrix Room，并绑定责任人、理由和Run ID。</p>' +
      '<div class="modal-form"><label>签署身份（来自外置Runtime配置）<input type="text" id="hg-actor" value="' + ui.esc(identity) + '" class="mono" readonly></label><label>处理理由<textarea id="hg-reason" rows="2" placeholder="一句话说明隔离、补证或批准原因"></textarea></label></div>' +
      (blocked && remediation ? '<div class="remediation-review"><div class="card-head"><i class="ri-git-branch-line card-icon"></i><h4>待Human批准的受控修订</h4><span class="mini-badge mini-amber">不会直接PASS</span></div><div class="remediation-confirm"><span><small>当前候选</small><b class="mono">' + ui.esc(remediation.from_field || "未确认") + '</b></span><i class="ri-arrow-right-line"></i><span><small>经Agent提出、Skill验证</small><b class="mono">' + ui.esc(remediation.target_field) + '</b></span></div><p class="card-note">批准后保留父Run和原始SHA256，创建子Run并立即尝试派发真实AgentTeams；子Run仍需多Agent重新核验和再次Human签署。</p></div>' : '') +
      '<div class="business-actions">' +
        (blocked ? '<button type="button" class="business-action action-fix" data-gate="ADOPT_REMEDIATION" ' + (remediation ? '' : 'disabled') + '><i class="ri-tools-line"></i><span><b>批准修订并启动新的多Agent核验</b><small>' + ui.esc(remediation ? ((remediation.from_field || "未确认") + " → " + remediation.target_field + "；原始证据不变") : "当前Run没有经Skill验证的修订候选，请先补证") + '</small></span></button>' : (waiting ? '' : '<button type="button" class="business-action action-pass" data-gate="APPROVE_PASS"><i class="ri-checkbox-circle-line"></i><span><b>批准用于声明用途</b><small>' + ui.esc(contract.downstream_purpose) + ' · 生成正式DataPass和固定结果报告</small></span></button>')) +
        '<button type="button" class="business-action action-stop" data-gate="REJECT"><i class="ri-inbox-unarchive-line"></i><span><b>确认拦截并签署</b><small>接受多Agent的BLOCK建议；禁止进入下游并生成最终处置报告</small></span></button>' +
        '<button type="button" class="business-action" data-gate="RETURN_FOR_EVIDENCE"><i class="ri-file-search-line"></i><span><b>补充材料后再判断</b><small>记录缺少的来源、授权或字段说明</small></span></button>' +
      '</div>' +
      '<p class="lock-note"><i class="ri-lock-2-line"></i> ' + (blocked ? 'BLOCK不能直接批准；必须修订、隔离或补证。' : (waiting ? '当前缺少必要证据，只能退回补证或隔离，不能直接批准。' : 'PASS仍需Human对用途范围进行最终授权。')) + '</p>' +
    '</div>';
  }

  function previewCard(res) {
    var ui = UI(), preview = res.report_preview, plan = ((res.presentation || {}).worker_plan || {}), dp = (res.presentation || {}).datapass || {};
    if (!preview || !preview.download_urls) return "";
    var composer = preview.composer || {}, strategy = composer.strategy || {}, urls = preview.download_urls || {};
    return '<div class="card auto-report-card">' +
      '<div class="card-head"><i class="ri-file-settings-line card-icon"></i><h4>Result Composer 已自动生成待签署报告</h4><span class="mini-badge mini-green">0 MODEL TOKEN</span></div>' +
      '<p class="card-note">报告依据同一Run的DataPass、' + ui.esc(plan.completed_count || 0) + '/' + ui.esc(plan.required_count || 0) + ' Worker、实际Skill回执和真实Token账本生成；它不是Human授权，签署后会固化最终版。</p>' +
      '<div class="plain-result-grid"><div><small>生成策略</small><p class="mono">' + ui.esc(strategy.strategy || "DETERMINISTIC_TEMPLATE_ONLY") + '</p></div><div><small>上下文压缩</small><p><b>' + ui.esc(strategy.context_reduction_percent == null ? "—" : strategy.context_reduction_percent + "%") + '</b> · ' + ui.esc(strategy.compact_context_chars || 0) + ' chars</p></div></div>' +
      '<div class="report-actions">' +
        '<a class="btn btn-primary" href="' + ui.esc(urls.pdf) + '"><i class="ri-file-pdf-2-line"></i> 自动报告PDF</a>' +
        '<a class="btn btn-outline" href="' + ui.esc(urls.markdown) + '"><i class="ri-markdown-line"></i> Markdown</a>' +
        '<a class="btn btn-outline" href="' + ui.esc(urls.json) + '"><i class="ri-braces-line"></i> JSON</a>' +
      '</div>' +
      '<p class="lock-note"><i class="ri-fingerprint-line"></i> Result Composer调用4个确定性报告Skill；不会把Matrix全文再次送入模型。</p>' +
    '</div>';
  }

  function finalCard(res) {
    var ui = UI(), gate = res.gate, finalResult = res.final_result;
    if (["APPROVED", "REJECTED", "RETURNED"].indexOf(gate.state) < 0) return "";
    var outcome = finalResult && finalResult.outcome ? finalResult.outcome : {
      headline: gate.state === "APPROVED" ? "可以使用：已获准进入指定下游系统" : gate.state === "REJECTED" ? "暂不能使用：该批数据已被隔离" : "暂不能判断：需要补充证据",
      plain_reason: gate.reason || "人工处理已完成。",
      next_action: gate.state === "APPROVED" ? "按本次授权范围使用。" : "按处置要求修正或补证后创建新Run。",
    };
    var finding = finalResult && finalResult.plain_language_finding ? finalResult.plain_language_finding : {};
    var urls = finalResult && finalResult.download_urls ? finalResult.download_urls : {};
    var auditUrl = res.run && res.run.run_id ? "/api/v1/runs/" + encodeURIComponent(res.run.run_id) + "/audit-bundle.zip" : "";
    var child = res.lineage && res.lineage.child_run_id;
    return '<div class="card final-outcome-card ' + (gate.state === "APPROVED" ? "final-approved" : "final-stopped") + '">' +
      '<div class="final-ribbon">最终业务结果</div><h2>' + ui.esc(outcome.headline) + '</h2>' +
      '<div class="plain-result-grid"><div><small>为什么</small><p>' + ui.esc(outcome.plain_reason) + '</p></div><div><small>现在怎么做</small><p>' + ui.esc(outcome.next_action) + '</p></div></div>' +
      (finding.explanation ? '<div class="result-explanation"><i class="ri-lightbulb-flash-line"></i>' + ui.esc(finding.explanation) + '</div>' : '') +
      (child ? '<div class="child-run-callout"><i class="ri-git-branch-line"></i><div><b>修订子Run已经创建</b><p class="mono">' + ui.esc(child) + '</p><small>当前证据版本保持可追溯；子Run按修订方案重新执行AgentTeams协作。</small></div><a class="btn btn-primary" href="#/live">查看子Run</a></div>' : '') +
      '<div class="report-actions">' +
        (urls.pdf ? '<a class="btn btn-primary" href="' + ui.esc(urls.pdf) + '"><i class="ri-file-pdf-2-line"></i> 下载固定PDF</a>' : '') +
        (urls.markdown ? '<a class="btn btn-outline" href="' + ui.esc(urls.markdown) + '"><i class="ri-markdown-line"></i> 下载签名Markdown</a>' : '') +
        (urls.json ? '<a class="btn btn-outline" href="' + ui.esc(urls.json) + '"><i class="ri-braces-line"></i> 下载结构化结果</a>' : '') +
        (auditUrl ? '<a class="btn btn-outline" href="' + ui.esc(auditUrl) + '"><i class="ri-file-zip-line"></i> 下载不可变审计ZIP</a>' : '') +
      '</div>' +
      (finalResult ? '<p class="lock-note"><i class="ri-fingerprint-line"></i> 结果哈希 <span class="mono">' + ui.esc(finalResult.result_payload_sha256) + '</span></p>' : '<p class="card-note">结果文件正在生成，请刷新页面。</p>') +
    '</div>';
  }

  function render(host) {
    var ui = UI();
    return window.FinfluxAPI.getHumanGate().then(function (res) {
      var gate = res.gate, pending = gate.state === "AWAITING_HUMAN";
      var queueRows = res.queue.length ? res.queue.map(function (q) {
        return '<div class="queue-item"><div><b class="mono">' + ui.esc(q.run_id) + '</b><p class="mono mini">' + ui.esc(q.case_id) + '</p></div>' + ui.badge(q.state, "待负责人处理") + '<span class="mono mini">' + ui.esc(q.opened_at) + '</span></div>';
      }).join("") : '<p class="card-note">当前没有等待处理的Run。</p>';

      host.innerHTML = ui.sectionHead("HUMAN DECISION", "人工处理与最终结果", ui.badge(gate.state, gate.label)) + plainFinding(res) +
        '<div class="hg-grid"><div><div class="card"><div class="card-head"><i class="ri-inbox-line card-icon"></i><h4>待处理事项</h4><span class="mini-badge ' + (pending ? "mini-amber" : "mini-green") + '">' + (pending ? "1项" : "0项") + '</span></div>' + queueRows + '</div>' +
        '<div class="card"><div class="card-head"><i class="ri-shield-keyhole-line card-icon"></i><h4>责任边界</h4></div>' + ui.kv("Agent建议", ui.badge(res.datapass.available ? res.datapass.machine_recommendation : "NOT_AVAILABLE", res.datapass.available ? res.datapass.machine_recommendation : "尚未形成")) + ui.kv("当前状态", ui.esc(gate.label)) + ui.kv("要求角色", ui.esc(gate.required_role)) + '<p class="card-note">模型只能建议；金融数值来自确定性Skill；只有Human能形成最终授权。</p></div></div>' +
        '<div>' + previewCard(res) + actionCard(res) + finalCard(res) + '</div></div>' + historyTable(res.history);

      host.querySelectorAll("[data-gate]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var actorEl = host.querySelector("#hg-actor"), reasonEl = host.querySelector("#hg-reason");
          window.FinfluxGateAction(btn.getAttribute("data-gate"), function (response) {
            if (response && response.child_run) window.location.hash = "#/live";
            else render(host);
          }, { actor: actorEl && actorEl.value.trim(), actor_locked: true, reason: reasonEl && reasonEl.value.trim(), remediation_plan: ((res.presentation || {}).remediation_plan || null) });
        });
      });
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.humanGate = render;
})();
