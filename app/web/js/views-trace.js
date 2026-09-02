/* ============================================================
   页面 3 · Trace 与恢复（#/trace）
   RouteDecision 摘要条 + 统一 Trace 泳道图（点击联动详情面板）
   + 恢复/幂等/导出/DataPass 状态四卡
   ============================================================ */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };

  /* ---------------- RouteDecision 摘要条 ---------------- */
  function routeStrip(rd, workers) {
    var ui = UI();
    var workerChips = workers.map(function (w) {
      return '<span class="avatar-chip topo-' + w.color + '" title="' + ui.esc(w.name) + '">' +
        '<i class="ri-cpu-line"></i>' + ui.esc(w.name.split(" ").pop()) + "</span>";
    }).join("");

    return '<div class="route-strip">' +
      '<div class="route-cell"><p class="eyebrow">声明目的</p><b>' + ui.esc(rd.declared_purpose) + "</b></div>" +
      '<div class="route-cell"><p class="eyebrow">证据画像</p><b class="mono">' + ui.esc(rd.evidence_profile) + "</b></div>" +
      '<div class="route-cell"><p class="eyebrow">已选 Workers ' + workers.length + '/' + ((rd.worker_plan && rd.worker_plan.count) || workers.length) + '</p><div class="chip-row">' + (workerChips || '<span class="mono mini">确定性准入，无需模型Worker</span>') + "</div></div>" +
      '<div class="route-cell"><p class="eyebrow">有界执行</p><b class="mono">事件 ≤ ' + rd.budget_strategy.max_events + " · Matrix代理 ≤ " + UI().fmtNum(rd.budget_strategy.max_message_proxy_tokens) + "</b><p class=\"mini\">供应商Token硬上限：未配置</p></div>" +
      '<div class="route-cell"><p class="eyebrow">当前路由状态</p><b>' + ui.esc(rd.route_state) + "</b>" +
        '<p class="mono mini">' + ui.esc(rd.reason_codes.join(" / ")) + "</p></div>" +
    "</div>";
  }

  /* ---------------- 泳道图 ---------------- */
  function statusClass(status) {
    var s = String(status).toUpperCase();
    if (["SUCCESS", "DONE", "VERIFIED", "PASS", "RECOVERED", "SUBMITTED", "OBSERVED"].indexOf(s) >= 0) return "ev-success";
    if (s === "TIMEOUT") return "ev-timeout";
    if (s === "RETRY_SUCCESS") return "ev-retry";
    if (s === "AWAITING_HUMAN") return "ev-await";
    if (["FAILED", "BLOCKED", "BUDGET_EXCEEDED"].indexOf(s) >= 0) return "ev-fail";
    return "ev-running";
  }

  function swimlane(trace, selectedId) {
    var ui = UI();
    var byKey = {};
    trace.events.forEach(function (e) { byKey[e.lane + ":" + e.round] = e; });

    var head = '<div class="lane-corner"></div>' + trace.rounds.map(function (r) {
      return '<div class="lane-head"><b>' + ui.esc(r.label) + '</b><span class="mono">' + ui.esc(r.time) + "</span></div>";
    }).join("");

    var rows = trace.lanes.map(function (lane) {
      var cells = trace.rounds.map(function (r) {
        var e = byKey[lane.lane_id + ":" + r.round];
        if (!e) return '<div class="lane-cell"></div>';
        return '<div class="lane-cell">' +
          '<div class="ev-card ' + statusClass(e.status) + (e.event_id === selectedId ? " selected" : "") +
            '" data-event="' + ui.esc(e.event_id) + '" tabindex="0" role="button">' +
            '<div class="ev-title">' + (e.warning ? '<i class="ri-error-warning-fill text-amber"></i>' : "") + ui.esc(e.title) + "</div>" +
            '<div class="ev-meta mono"><span>' + ui.esc(e.time) + '</span><span>' + ui.esc(e.task_id) + "</span></div>" +
            '<div class="ev-meta mono"><span>' + ui.esc(e.tool) + '</span><span>模型Token见Run账本 · ' + ui.esc(e.duration) + "</span></div>" +
          "</div>" +
        "</div>";
      }).join("");
      return '<div class="lane-label"><i class="' + ui.esc(lane.icon) + '"></i><span>' + ui.esc(lane.name) + "</span></div>" + cells;
    }).join("");

    return '<div class="swimlane-wrap">' +
      '<div class="lane-legend">' +
        '<span><i class="dot dot-green"></i>成功</span>' +
        '<span><i class="dot dot-blue"></i>进行中</span>' +
        '<span><i class="dot dot-retry"></i>重试恢复</span>' +
        '<span><i class="dot dot-amber"></i>等待人审</span>' +
        '<span><i class="dot dot-red"></i>失败</span>' +
      "</div>" +
      '<div class="swimlane" style="grid-template-columns: 180px repeat(' + trace.rounds.length + ', minmax(210px, 1fr));">' +
      head + rows +
      "</div></div>";
  }

  function usageLedger(usage) {
    var ui = UI();
    usage = usage || {};
    var reported = usage.status === "PROVIDER_REPORTED";
    var agents = Array.isArray(usage.by_agent) ? usage.by_agent : [];
    var daily = usage.daily_snapshot || {};
    return '<div class="card usage-ledger">' +
      '<div class="card-head"><i class="ri-coins-line card-icon"></i><h4>真实模型 Token 账本</h4>' +
        ui.badge(reported ? "SUCCESS" : "PENDING", reported ? "PROVIDER REPORTED" : "等待归集") + '</div>' +
      (reported ?
        '<div class="stat-grid4"><div class="stat"><b class="mono">' + ui.fmtNum(usage.total_tokens) + '</b><span>本 Run 总 Token</span></div>' +
        '<div class="stat"><b class="mono">' + ui.fmtNum(usage.prompt_tokens) + '</b><span>输入 Token</span></div>' +
        '<div class="stat"><b class="mono">' + ui.fmtNum(usage.completion_tokens) + '</b><span>输出 Token</span></div>' +
        '<div class="stat"><b class="mono">' + ui.fmtNum(usage.call_count) + '</b><span>模型调用</span></div></div>' +
        '<div class="file-list">' + agents.map(function (a) { return '<div class="file-row"><span class="file-name">' + ui.esc(a.agent_id) + '</span><span class="mono">' + ui.fmtNum(a.prompt_tokens) + ' in + ' + ui.fmtNum(a.completion_tokens) + ' out</span><span class="mini-badge mini-blue">' + ui.fmtNum(a.total_tokens) + ' / ' + ui.fmtNum(a.call_count) + ' calls</span></div>'; }).join('') + '</div>' +
        '<p class="card-note"><i class="ri-shield-check-line"></i> ' + ui.esc(usage.source_truth) + '</p>' +
        '<p class="card-note mono">归集口径：' + ui.esc(usage.attribution_status) + ' · 当日容器账本快照 ' + ui.fmtNum(daily.total_tokens || 0) + ' tokens / ' + ui.fmtNum(daily.call_count || 0) + ' calls · 成本未折算（价格版本未固定）</p>'
        : '<p class="card-note">供应商 usage 尚未完成归集；系统不会用 Matrix 字符估算补成模型 Token。</p>') +
      '</div>';
  }

  /* ---------------- 事件详情面板 ---------------- */
  function detailPanel(e) {
    var ui = UI();
    if (!e) return '<aside class="detail-panel"><p class="card-note">请选择左侧任一事件卡查看详情。</p></aside>';

    var ledger = e.token_ledger && typeof e.token_ledger === "object"
      ? '<div class="card slim">' +
        '<div class="card-head"><i class="ri-coin-line card-icon"></i><h4>Token Ledger</h4></div>' +
        ui.kv("提供方使用", '<span class="mono text-amber">' + ui.esc(e.token_ledger.provider_usage) + "</span>") +
        ui.kv("Matrix消息代理", '<span class="mono">' + ui.fmtNum(e.token_ledger.observed_estimate) + "（非模型Token）</span>") +
        ui.kv("成本折算", '<span class="mono">' + (e.token_ledger.cost_cny == null ? "NOT_EXPOSED" : "CNY " + ui.fmtNum(e.token_ledger.cost_cny, 2)) + "</span>") +
        ui.kv("账本来源", '<span class="mono">' + ui.esc(e.token_ledger.source || "NOT_CAPTURED") + "</span>") +
        "</div>"
      : "";

    var retry = e.timeout_s
      ? '<div class="card slim">' +
        '<div class="card-head"><i class="ri-restart-line card-icon"></i><h4>超时 / 重试策略</h4></div>' +
        ui.kv("超时时间", '<span class="mono">' + ui.fmtNum(e.timeout_s, 1) + "s</span>") +
        ui.kv("重试策略", ui.esc(e.retry_policy)) +
        ui.kv("实际重试", '<b class="text-green">' + ui.esc(e.retry_actual) + "</b>") +
        "</div>"
      : "";

    return '<aside class="detail-panel" id="trace-detail">' +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-fingerprint-line card-icon"></i>' +
          "<h4>" + ui.esc(e.title) + (e.warning ? ' <i class="ri-error-warning-fill text-amber"></i>' : "") + "</h4>" +
          UI().badge(e.status) + "</div>" +
        ui.kv("时间", '<span class="mono">' + ui.esc(e.time) + "</span>") +
        ui.kv("轮次", "Round " + e.round) +
        ui.kv("任务 ID", '<span class="mono">' + ui.esc(e.task_id) + "</span>" + ui.copyBtn(e.task_id)) +
        ui.kv("工具", '<span class="mono">' + ui.esc(e.tool) + "</span>") +
        ui.kv("事件 ID", '<span class="mono">' + ui.esc(e.event_id) + "</span>" + ui.copyBtn(e.event_id)) +
      "</div>" +
      '<div class="card slim">' +
        '<div class="card-head"><i class="ri-input-method-line card-icon"></i><h4>输入 / 输出摘要</h4></div>' +
        ui.kv("输入哈希", e.input_hash ? '<span class="mono">' + ui.esc(UI().shaShort(e.input_hash, 16, 4)) + "</span>" + ui.copyBtn(e.input_hash) : '<span class="mono">NOT_CAPTURED</span>') +
        ui.kv("输出 / 消息哈希", '<span class="mono">' + ui.esc(UI().shaShort(e.output_hash || e.message_sha256 || "NOT_CAPTURED", 16, 4)) + "</span>" + ui.copyBtn(e.output_hash || e.message_sha256 || "NOT_CAPTURED")) +
        ui.kv("捕获状态", '<span class="mono">' + ui.esc((e.input_capture || "NOT_CAPTURED") + " / " + (e.output_capture || "NOT_CAPTURED")) + "</span>") +
        '<p class="card-note">' + ui.esc(e.summary) + "</p>" +
      "</div>" +
      retry + ledger +
      '<div class="detail-actions">' +
        '<button type="button" class="btn btn-outline btn-sm" data-act="input"><i class="ri-eye-line"></i> 查看输入</button>' +
        '<button type="button" class="btn btn-outline btn-sm" data-act="output"><i class="ri-eye-off-line"></i> 查看输出</button>' +
        '<a class="btn btn-outline btn-sm" href="#/human-gate"><i class="ri-arrow-go-back-line"></i> Human Gate / 退回补证</a>' +
      "</div>" +
    "</aside>";
  }

  /* ---------------- 底部四卡 ---------------- */
  function bottomCards(info) {
    var ui = UI();
    var cp = info.recovery.checkpoint;
    var lock = info.recovery.idempotency_lock;
    var dp = info.datapass;
    var gate = info.gate;
    var impact = dp && dp.impact && typeof dp.impact === "object" ? dp.impact : {};
    var impactValue = impact.value;
    var hasImpact = impactValue !== null && impactValue !== undefined && impactValue !== "" && Number.isFinite(Number(impactValue));
    var impactText = hasImpact
      ? '<b class="mono">' + ui.fmtNum(Number(impactValue), 2) + " " + ui.esc(impact.unit || "") + "</b>"
      : '<span class="mono">未形成确定性金额</span>';
    var mappingText = dp && dp.recommended_field
      ? '采用 <code>' + ui.esc(dp.recommended_field) + '</code> 字段处理声明用途'
      : ui.esc((dp && dp.recommended_action) || "尚未形成处置建议");

    var exportCards = info.recovery.exports.map(function (x) {
      if (x.href) {
        return '<a class="export-card" href="' + ui.esc(x.href) + '" download="' + ui.esc(x.file) + '">' +
          '<i class="ri-file-zip-line"></i><div><b>' + ui.esc(x.name) + '</b><span class="mono">' + ui.esc(x.file) + " · " + ui.esc(x.size) + "</span></div>" +
          '<i class="ri-download-2-line"></i></a>';
      }
      return '<button type="button" class="export-card" data-export="' + ui.esc(x.kind) + '" data-file="' + ui.esc(x.file) + '">' +
        '<i class="ri-file-zip-line"></i><div><b>' + ui.esc(x.name) + '</b><span class="mono">' + ui.esc(x.file) + " · " + ui.esc(x.size) + "</span></div>" +
        '<i class="ri-download-2-line"></i></button>';
    }).join("");

    return '<div class="quad-grid">' +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-save-3-line card-icon"></i><h4>恢复与检查点</h4>' + ui.badge("SUCCESS", cp.badge) + "</div>" +
        ui.kv("Checkpoint ID", '<span class="mono">' + ui.esc(cp.checkpoint_id) + "</span>" + ui.copyBtn(cp.checkpoint_id)) +
        ui.kv("时间", '<span class="mono">' + ui.esc(cp.created_at) + " → " + ui.esc(cp.recovered_at) + "</span>") +
        ui.kv("恢复结果", '<b class="text-green">' + ui.esc(cp.result) + "</b>") +
        ui.kv("恢复任务集", '<span class="mono">' + ui.esc(cp.recovered_tasks.join(", ")) + "</span>") +
        ui.kv("数据一致性", ui.esc(cp.consistency)) +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-key-2-line card-icon"></i><h4>幂等锁 Idempotency Lock</h4>' + ui.badge("SUCCESS", lock.badge) + "</div>" +
        ui.kv("Key", '<span class="mono">' + ui.esc(UI().shaShort(lock.key, 22, 4)) + "</span>" + ui.copyBtn(lock.key)) +
        ui.kv("作用域", ui.esc(lock.scope)) +
        ui.kv("首见时间", '<span class="mono">' + ui.esc(lock.first_seen_at) + "</span>") +
        ui.kv("重复调用", '<b class="text-green">' + lock.duplicate_calls_blocked + " 次已拦截</b>") +
        '<p class="card-note"><i class="ri-shield-check-line"></i> ' + ui.esc(lock.note) + "</p>" +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-download-cloud-2-line card-icon"></i><h4>可观测性导出</h4></div>' +
        '<div class="export-list">' + exportCards + "</div>" +
      "</div>" +
      '<div class="card">' +
        '<div class="card-head"><i class="ri-git-merge-line card-icon"></i><h4>DataPass / Human 状态</h4></div>' +
        ui.kv("机器建议", ui.badge(dp.machine_recommendation)) +
        ui.kv("处置建议", mappingText) +
        ui.kv("确定性影响", impactText) +
        ui.kv("人审门控", ui.badge(gate.state, gate.label)) +
        ui.kv("门控状态", ui.esc(gate.current_stage)) +
        '<a class="btn btn-outline btn-sm" href="#/evidence">查看证据与决策详情 <i class="ri-arrow-right-line"></i></a>' +
      "</div>" +
    "</div>";
  }

  function render(host) {
    var ui = UI();
    return Promise.all([
      window.FinfluxAPI.getRouteDecision(),
      window.FinfluxAPI.getTraceEvents(),
      window.FinfluxAPI.getRecoveryInfo(),
    ]).then(function (res) {
      var rd = res[0], trace = res[1], info = res[2];
      if (!trace.events.length) {
        host.innerHTML = routeStrip(rd.route_decision, rd.workers) + '<div class="card empty-card"><i class="ri-fingerprint-line"></i><h3>尚无真实 TraceEvent</h3><p>上传证据并创建 Fresh Run 后，服务端事件会按时间顺序出现在这里。</p><a class="btn btn-primary" href="#/live">创建 Fresh Run</a></div>';
        return;
      }
      /* 默认选中：超时事件，否则第一个事件 */
      var selected = trace.events.find(function (e) { return e.status === "TIMEOUT"; }) || trace.events[0];

      host.innerHTML =
        routeStrip(rd.route_decision, rd.workers) +
        usageLedger(info.provider_usage) +
        '<div class="trace-layout">' +
          '<div class="trace-main">' +
            UI().sectionHead("UNIFIED TRACE", "统一 Trace（时间顺序）") +
            swimlane(trace, selected.event_id) +
          "</div>" +
          '<div class="trace-side" id="trace-side">' + detailPanel(selected) + "</div>" +
        "</div>" +
        bottomCards(info);

      /* 事件点击 -> 详情联动 */
      host.querySelectorAll(".ev-card").forEach(function (card) {
        function pick() {
          var id = card.getAttribute("data-event");
          var e = trace.events.find(function (x) { return x.event_id === id; });
          host.querySelectorAll(".ev-card.selected").forEach(function (c) { c.classList.remove("selected"); });
          card.classList.add("selected");
          var side = host.querySelector("#trace-side");
          if (side) { side.innerHTML = detailPanel(e); bindDetailActions(side, e); }
        }
        card.addEventListener("click", pick);
        card.addEventListener("keydown", function (ev2) { if (ev2.key === "Enter" || ev2.key === " ") { ev2.preventDefault(); pick(); } });
      });

      function bindDetailActions(scope, e) {
        scope.querySelectorAll("[data-act]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var act = btn.getAttribute("data-act");
            if (act === "input") ui.toast(e.input_hash ? "已捕获输入摘要 · " + e.input_hash.slice(0, 24) + "…" : "该事件未捕获独立输入快照；未用模拟值补齐", e.input_hash ? "info" : "warn");
            else if (act === "output") {
              var hash = e.output_hash || e.message_sha256;
              ui.toast(hash ? "真实输出/Matrix消息摘要 · " + String(hash).slice(0, 24) + "…" : "该事件未捕获输出快照", hash ? "info" : "warn");
            }
          });
        });
      }
      bindDetailActions(host.querySelector("#trace-side"), selected);

      /* 导出下载：真实 Blob */
      host.querySelectorAll("[data-export]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var kind = btn.getAttribute("data-export");
          var file = btn.getAttribute("data-file");
          window.FinfluxAPI.exportBundle(kind).then(function (payload) {
            ui.downloadJson(file, payload);
            ui.toast("已从后端导出同一 Run 审计包 · " + file, "success");
          });
        });
      });
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.trace = render;
})();
