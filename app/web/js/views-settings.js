/* ============================================================
   页面 6 · 系统设置（#/settings）
   运行时 / 预算 / 两层 Memory / selected Run 计划 / 真值边界。
   Memory 状态是只读、可降级信息，不得阻断设置页。
   ============================================================ */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };

  function obj(value) {
    return value && typeof value === "object" ? value : {};
  }

  function count(ui, value) {
    return '<span class="mono">' + ui.fmtNum(Number(value) || 0) + "</span>";
  }

  function stateText(ui, value, fallback) {
    return '<span class="mono">' + ui.esc(value == null || value === "" ? fallback : value) + "</span>";
  }

  function render(host) {
    var ui = UI();
    return window.FinfluxAPI.getSettings().then(function (s) {
      var r = obj(s.runtime);
      var bp = obj(s.budget_policy);
      var memory = obj(s.memory);
      var structured = Object.keys(obj(memory.structured)).length ? obj(memory.structured) : memory;
      var context = obj(memory.context);
      var local = obj(context.local);
      var memoryCache = obj(context.cache);
      var lastRemote = obj(context.last_remote);
      var selected = obj(memory.selected_run);
      var plan = obj(selected.operational_memory_plan);
      var planMetrics = obj(plan.metrics);
      var handles = Array.isArray(plan.reference_handles) ? plan.reference_handles : [];
      var contextStatus = String(context.status || "");
      var memoryAvailable = memory.available !== false && contextStatus.indexOf("DEGRADED") !== 0;
      var contextBackend = context.backend || "NOT_EXPOSED";
      var contextConfigured = context.configured === true;
      var remoteEnabled = context.remote_enabled === true;
      var remoteReadyLabel = String(context.remote_ready || "NOT_PROBED").toUpperCase();
      var remoteReady = remoteEnabled && ["READY", "HEALTHY", "ACTIVE"].indexOf(remoteReadyLabel) >= 0;
      var planHasTokenReceipt = Object.prototype.hasOwnProperty.call(plan, "finflux_agent_llm_tokens");
      var promptInjected = context.prompt_injected === true || plan.prompt_injected === true;
      var lastRemoteLabel = lastRemote.status || "NEVER";
      var selectedRunId = selected.run_id || "NO_SELECTED_RUN";
      var truth = memory.truth_boundary ||
        "Memory状态未提供真值边界；不得据此推断Prompt注入、模型缓存命中或Token下降。";
      var memoryBanner = memoryAvailable ? "" :
        '<div class="truth-banner truth-warn"><i class="ri-error-warning-line"></i>' +
          '<div><b>Memory 状态降级</b><span>' + ui.esc(memory.error || "状态接口暂不可用") +
          "。设置页与主运行链保持可用，以下空值不补造。</span></div></div>";

      host.innerHTML =
        ui.sectionHead("SYSTEM SETTINGS", "系统设置", memoryAvailable ? ui.badge("ACTIVE", "MEMORY STATUS") : ui.badge("FAILED", "MEMORY DEGRADED")) +
        memoryBanner +
        '<div class="settings-grid">' +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-server-line card-icon"></i><h4>运行时信息</h4>' + ui.badge(r.mode || "UNKNOWN") + "</div>" +
            ui.kv("平台版本", stateText(ui, r.platform, "NOT_REPORTED")) +
            ui.kv("模型", stateText(ui, r.model, "NOT_REPORTED")) +
            ui.kv("控制台", ui.esc(r.console || "NOT_REPORTED")) +
            ui.kv("Matrix Homeserver", ui.esc(r.matrix_homeserver || "未连接")) +
            ui.kv("Element", r.element_url ? '<a class="btn btn-outline btn-sm" href="' + ui.esc(r.element_url) + '" target="_blank" rel="noopener">打开真实 Matrix UI <i class="ri-external-link-line"></i></a>' : "未连接") +
            ui.kv("证据存储", ui.esc(r.evidence_storage || "NOT_REPORTED")) +
            ui.kv("运行模式", ui.badge(r.mode || "UNKNOWN")) +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-gas-station-line card-icon"></i><h4>预算策略（版本固定）</h4>' +
              '<span class="chip chip-cyan mono">' + ui.esc(bp.policy_id || "NOT_REPORTED") + " " + ui.esc(bp.version || "") + "</span></div>" +
            ui.kv("供应商 Token 硬上限", '<span class="mono text-amber">未配置（不得伪称10,000）</span>') +
            ui.kv("Matrix消息代理上限", '<span class="mono">' + ui.fmtNum(bp.max_message_proxy_tokens || 0) + "（非模型Token）</span>") +
            ui.kv("模型轮数上限", count(ui, bp.max_model_rounds)) +
            ui.kv("事件上限", count(ui, bp.max_events)) +
            ui.kv("重试上限", count(ui, bp.max_retries) + " 次") +
            ui.kv("墙钟截止", count(ui, bp.deadline_s) + "s") +
            ui.kv("告警阈值", '<span class="mono">' + Math.round((Number(bp.warning_ratio) || 0) * 100) + "%</span>") +
            ui.kv("提升预算", bp.requires_human_raise ? "仅 Human 可提升（Agent 不可修改）" : "运行时不可修改") +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-database-2-line card-icon"></i><h4>本地结构化 Memory</h4>' + ui.badge("VERIFIED", "HASH-BOUND") + "</div>" +
            ui.kv("Run 记忆", count(ui, structured.run_memories)) +
            ui.kv("Skill 内容寻址索引", count(ui, structured.skill_cache_entries)) +
            ui.kv("失败恢复点", count(ui, structured.failure_memories)) +
            ui.kv("复用条件", ui.esc(structured.cache_policy || "Skill digest + input SHA256")) +
            ui.kv("原始金融数据", structured.raw_financial_bytes_stored === true ? ui.badge("FAILED", "服务端声明已保存") : ui.badge("PASS", "不保存")) +
            '<p class="caveat">内容寻址索引命中不等于模型供应商 KV Cache 命中，也不证明模型 Token 已降低。</p>' +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-links-line card-icon"></i><h4>Context Memory / OpenViking</h4>' +
              ui.badge(remoteReady ? "ACTIVE" : contextConfigured ? "VERIFIED" : "WAITING", remoteReady ? "OPENVIKING READY" : remoteEnabled ? "CONFIGURED · NOT PROBED" : "LOCAL HASH") + "</div>" +
            ui.kv("后端", stateText(ui, contextBackend, "NOT_EXPOSED")) +
            ui.kv("本地后端", contextConfigured ? ui.badge("PASS", "AVAILABLE") : ui.badge("WAITING", "NOT AVAILABLE")) +
            ui.kv("OpenViking远端", remoteReady ? ui.badge("ACTIVE", remoteReadyLabel) : remoteEnabled ? ui.badge("WAITING", "CONFIGURED · " + remoteReadyLabel) : ui.badge("WAITING", "DISABLED / LOCAL FALLBACK")) +
            ui.kv("本地对象", count(ui, local.objects)) +
            ui.kv("签署绑定", count(ui, local.bindings)) +
            ui.kv("待投递 Outbox", count(ui, local.outbox)) +
            ui.kv("Run/Role 查询缓存", count(ui, memoryCache.run_role_query_entries)) +
            ui.kv("后台投递", context.outbox_drain_running === true ? ui.badge("RUNNING", "RUNNING") : ui.badge("WAITING", "IDLE")) +
            ui.kv("最近远端状态", stateText(ui, lastRemoteLabel, "NEVER")) +
            ui.kv("最近远端延迟", lastRemote.latency_ms == null ? "未观察" : '<span class="mono">' + ui.fmtNum(lastRemote.latency_ms) + "ms</span>") +
            ui.kv("Prompt 注入", promptInjected ? ui.badge("WAITING", "服务端声明已注入，需Trace佐证") : ui.badge("PASS", "未注入")) +
            '<p class="caveat">OpenViking 不可用时仅降级到本地内容寻址索引；不得阻断金融准入主流程。</p>' +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-route-line card-icon"></i><h4>Selected Run · Memory 计划</h4>' +
              ui.badge(plan.recall_status || "NOT_CREATED") + "</div>" +
            ui.kv("Run ID", stateText(ui, selectedRunId, "NO_SELECTED_RUN")) +
            ui.kv("执行配方", stateText(ui, plan.recipe_id, "NO_PLAN")) +
            ui.kv("召回状态", stateText(ui, plan.recall_status, "NOT_REPORTED")) +
            ui.kv("引用句柄", count(ui, handles.length)) +
            ui.kv("召回来源", stateText(ui, planMetrics.source, "NOT_OBSERVED")) +
            ui.kv("查询缓存", planMetrics.cache_hit === true ? ui.badge("ACTIVE", "RUN/ROLE QUERY HIT") : ui.badge("WAITING", "MISS / NOT OBSERVED")) +
            ui.kv("候选摘要字符", count(ui, planMetrics.candidate_characters)) +
            ui.kv("候选摘要 Token 估算", count(ui, planMetrics.candidate_token_estimate) + "（未等同模型Token）") +
            ui.kv("FinFlux Agent LLM", planHasTokenReceipt ? '<span class="mono">' + ui.fmtNum(plan.finflux_agent_llm_tokens) + " tokens（Memory控制逻辑）</span>" : ui.badge("WAITING", "NO PLAN / NOT REPORTED")) +
            ui.kv("OpenViking内部Provider", stateText(ui, plan.openviking_provider_usage || context.openviking_provider_usage, "NOT_CAPTURED")) +
            '<p class="caveat">当前计划为 advisory-only；候选摘要规模不代表已注入 Prompt，也不代表本 Run 总 Token 已减少。</p>' +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-shield-check-line card-icon"></i><h4>Memory 真值边界</h4>' + ui.badge("VERIFIED", "FAIL-SAFE") + "</div>" +
            '<p class="card-note">' + ui.esc(truth) + "</p>" +
            ui.kv("Prompt 注入声明", promptInjected ? "TRUE（仍需同 Run Trace 佐证）" : "FALSE") +
            ui.kv("金融真值权限", plan.financial_truth_authority === true ? "服务端声明 TRUE" : "FALSE") +
            ui.kv("路由/Skill覆盖", plan.route_or_skill_override_allowed === true ? "服务端声明允许" : "不允许") +
            '<p class="caveat">只展示服务端已经返回的事实；接口失败、字段缺失或未选 Run 时保持 NOT_REPORTED，不以浏览器默认值补造运行结论。</p>' +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-information-line card-icon"></i><h4>关于</h4></div>' +
            ui.kv("项目", ui.esc(s.about.project)) +
            ui.kv("场景", ui.esc(s.about.scenario)) +
            ui.kv("版本", stateText(ui, s.about.version, "NOT_REPORTED")) +
            '<p class="card-note">' + ui.esc(s.about.tagline) + "</p>" +
            '<p class="caveat">真实 /api/v1 后端 fail-closed；缺失 Agent、Memory、Token 或 Human 事实均不由前端补齐。</p>' +
          "</div>" +

          '<div class="card">' +
            '<div class="card-head"><i class="ri-palette-line card-icon"></i><h4>主题</h4></div>' +
            ui.kv("当前主题", ui.esc(s.theme.name)) +
            '<p class="card-note">' + ui.esc(s.theme.note) + "</p>" +
            '<div class="theme-swatches">' +
              '<span style="background:#22d3ee"></span><span style="background:#3b82f6"></span>' +
              '<span style="background:#34d399"></span><span style="background:#fbbf24"></span>' +
              '<span style="background:#f87171"></span><span style="background:#a78bfa"></span>' +
            "</div>" +
          "</div>" +

        "</div>";
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.settings = render;
})();
