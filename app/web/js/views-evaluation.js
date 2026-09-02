/* FinFlux source-bound evaluation: values come from the verified 50/50/50 API. */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };

  function shortHash(value) {
    var text = String(value || "");
    return text ? text.slice(0, 12) + "…" + text.slice(-8) : "—";
  }

  function decisionCard(label, value, tone, explanation) {
    return '<div class="eval-stat eval-decision-' + tone + '"><small>' + label + '</small><b>' + value + '</b><span>' + explanation + '</span></div>';
  }

  function pct(value) { return (Number(value || 0) * 100).toFixed(1) + "%"; }

  function metricFormula(contract, numerator, denominator, result) {
    var ui = UI();
    return '<div class="metric-formula"><div><b>' + ui.esc(contract.label) + '</b><span>' + ui.esc(contract.formula) + '</span></div>' +
      '<code>' + ui.fmtNum(numerator) + ' / ' + ui.fmtNum(denominator) + ' = ' + ui.esc(result) + '</code></div>';
  }

  function systemCard(system, contracts) {
    var ui = UI(), inputs = system.metric_inputs || {}, latency = system.latency_ms || {};
    var risky = Number(system.false_release_count || 0) > 0;
    return '<div class="card eval-system ' + (risky ? 'eval-system-risk' : 'eval-system-good') + '">' +
      '<div class="card-head"><i class="ri-function-line card-icon"></i><h4>' + ui.esc(system.system) + '</h4>' + ui.badge(risky ? "WARN" : "SUCCESS", risky ? "存在误放" : "契约一致") + '</div>' +
      '<div class="eval-meter"><span style="width:' + ui.esc(Number(system.route_accuracy || 0) * 100) + '%"></span></div>' +
      '<div class="eval-stat-grid metric-grid">' +
        decisionCard("评测Case", ui.fmtNum(system.case_count), "pass", "同一批带契约标签的配置场景") +
        decisionCard("路由准确率", pct(system.route_accuracy), "pass", ui.fmtNum(inputs.route_correct_count) + " / " + ui.fmtNum(system.case_count) + "条路由一致") +
        decisionCard("误放", ui.fmtNum(system.false_release_count) + " · " + pct(system.false_release_rate), system.false_release_count ? "block" : "pass", "分母：" + ui.fmtNum(inputs.expected_nonpass_count) + "条预期非PASS") +
        decisionCard("误阻", ui.fmtNum(system.false_block_count) + " · " + pct(system.false_block_rate), system.false_block_count ? "wait" : "pass", "分母：" + ui.fmtNum(inputs.expected_pass_count) + "条预期PASS") +
        decisionCard("本地策略P95", ui.esc(latency.p95) + " ms", "wait", "仅Python策略wall time") +
      '</div>' +
      '<details class="metric-contracts"><summary>展开计算口径、输入分母与适用边界</summary>' +
        metricFormula(contracts.route_accuracy, inputs.route_correct_count, system.case_count, pct(system.route_accuracy)) +
        metricFormula(contracts.false_release_rate, inputs.false_release_count, inputs.expected_nonpass_count, pct(system.false_release_rate)) +
        metricFormula(contracts.false_block_rate, inputs.false_block_count, inputs.expected_pass_count, pct(system.false_block_rate)) +
        metricFormula(contracts.policy_latency_p95_ms, latency.p95, system.case_count, ui.esc(latency.p95) + " ms") +
        '<p class="caveat"><b>时延边界：</b>' + ui.esc(system.latency_scope) + '</p></details>' +
    '</div>';
  }

  function sourceRow(source) {
    var ui = UI();
    return '<div class="not-run-row source-bound-row"><div>' +
      '<b>' + ui.esc(source.provider) + '</b>' +
      '<p>' + ui.esc(source.adapter) + '</p>' +
      '<p class="mono">' + ui.esc(shortHash(source.artifact_sha256)) + '</p>' +
      '</div>' + ui.badge("WARN", source.rights_state) + '</div>';
  }

  function render(host) {
    var ui = UI();
    return Promise.all([
      window.FinfluxAPI.getEvaluation(),
      window.FinfluxAPI.getEvaluationManifest(),
      window.FinfluxAPI.getEvaluationMetrics()
    ]).then(function (payloads) {
      var report = payloads[0] || {};
      var manifest = payloads[1] || {};
      var controlled = payloads[2] || {};
      var corpus = report.corpus || {};
      var pipeline = report.executed_pipeline || {};
      var decisions = pipeline.decision_counts || {};
      var assets = corpus.counts_by_asset || {};
      var sources = manifest.sources || [];
      var notExecuted = report.not_executed || [];
      var limitations = report.limitations || [];
      var metricContracts = controlled.metric_contracts || {};
      var metricSystems = controlled.executed_systems || [];
      var metricSkill = controlled.skill_invocation || {};

      host.innerHTML =
        ui.sectionHead("SOURCE-BOUND EVALUATION", "真实50 / 50 / 50 数据准入预检") +
        '<div class="truth-banner"><i class="ri-shield-check-line"></i><b>本页由后端验签后返回</b><span>150条是不同来源行，不是150个参数变体；未执行的模型评测和机构指标不补数。</span></div>' +
        '<div class="eval-hero">' +
          '<div class="card"><p class="eyebrow">IMMUTABLE SOURCE CORPUS</p><h3>' + ui.fmtNum(corpus.case_count) + ' 条真实来源记录</h3><p class="card-note">CFFEX原始字节快照 + AKShare适配器输出快照。每条记录绑定来源文件哈希、行哈希和权属状态。</p>' +
            '<div class="eval-stat-grid"><div class="eval-stat"><small>期货</small><b>' + ui.fmtNum(assets.futures) + '</b><span>CFFEX两交易日</span></div><div class="eval-stat"><small>股票</small><b>' + ui.fmtNum(assets.equity) + '</b><span>公司行动记录</span></div><div class="eval-stat"><small>基金</small><b>' + ui.fmtNum(assets.fund) + '</b><span>开放式基金净值</span></div></div>' +
          '</div>' +
          '<div class="card"><div class="card-head"><i class="ri-fingerprint-line card-icon"></i><h4>不可变绑定</h4>' + ui.badge("SUCCESS", "HASH VERIFIED") + '</div>' +
            ui.kv("记录口径", '<b class="mono">' + ui.esc(corpus.record_basis) + '</b>') +
            ui.kv("模型生成记录", '<b class="mono text-green">' + ui.fmtNum(corpus.model_generated_records) + '</b>') +
            ui.kv("原始值改写", corpus.raw_market_data_mutated ? ui.badge("DANGER", "是") : ui.badge("SUCCESS", "否")) +
            ui.kv("Manifest", '<span class="mono">' + ui.esc(shortHash(corpus.manifest_sha256)) + '</span>') +
            ui.kv("记录Merkle", '<span class="mono">' + ui.esc(shortHash(corpus.records_merkle_sha256)) + '</span>') +
            ui.kv("评测报告", '<span class="mono">' + ui.esc(shortHash(report.report_sha256)) + '</span>') +
          '</div>' +
        '</div>' +
        '<div class="card eval-source-precheck"><div class="card-head"><i class="ri-filter-3-line card-icon"></i><h4>确定性批量预检</h4>' + ui.badge("LIVE", pipeline.status) + '</div>' +
          '<p class="card-note">检查字段完整性和“声明用途—候选字段”契约。该阶段不调用模型；PASS只是进入Human候选，不等于最终批准。</p>' +
          '<div class="eval-stat-grid">' +
            decisionCard("PASS", ui.fmtNum(decisions.PASS), "pass", "证据字段与声明用途一致") +
            decisionCard("WAIT", ui.fmtNum(decisions.WAIT), "wait", "需补日期、用途或来源字段") +
            decisionCard("BLOCK", ui.fmtNum(decisions.BLOCK), "block", "已确认的确定性语义冲突") +
          '</div>' +
          ui.kv("本批处理记录", '<b class="mono">' + ui.fmtNum(pipeline.processed_count) + '</b>') +
          ui.kv("模型调用 / Token", '<b class="mono text-green">' + ui.fmtNum(pipeline.model_calls) + ' / ' + ui.fmtNum(pipeline.provider_tokens) + '</b>') +
          ui.kv("本地批处理耗时", '<span class="mono">' + ui.esc(pipeline.duration_ms) + ' ms</span>') +
          ui.kv("结果Merkle", '<span class="mono">' + ui.esc(shortHash(pipeline.result_merkle_sha256)) + '</span>') +
        '</div>' +
        '<div class="truth-banner metric-boundary"><i class="ri-scales-3-line"></i><b>二类评测必须分开阅读</b><span>上方150条衡量真实来源接入覆盖；下方187条衡量带契约标签的路由配置。' + ui.esc(controlled.label_boundary || '') + '</span></div>' +
        '<div class="card metric-skill-receipt"><div class="card-head"><i class="ri-flashlight-line card-icon"></i><h4>calculate-evaluation-metrics Skill</h4>' + ui.badge("LIVE", metricSkill.status || "NOT_RUN") + '</div>' +
          ui.kv("Skill版本", '<b class="mono">' + ui.esc(metricSkill.skill_id) + '@' + ui.esc(metricSkill.version) + '</b>') +
          ui.kv("输入哈希", '<span class="mono">' + ui.esc(shortHash(metricSkill.input_sha256)) + '</span>') +
          ui.kv("输出哈希", '<span class="mono">' + ui.esc(shortHash(metricSkill.output_sha256)) + '</span>') +
          ui.kv("模型调用 / Token", '<b class="mono text-green">' + ui.fmtNum(metricSkill.model_calls) + ' / ' + ui.fmtNum(metricSkill.provider_tokens) + '</b>') +
          '<p class="card-note">页面数值由后端Skill现场读取已验哈希Case并重新执行两套路由策略，不从前端常量读取。</p></div>' +
        '<div class="eval-compare">' + metricSystems.map(function (system) { return systemCard(system, metricContracts); }).join('') + '</div>' +
        '<div class="eval-caveats">' +
          '<div class="card"><div class="card-head"><i class="ri-database-2-line card-icon"></i><h4>来源快照与权属边界</h4></div>' +
            sources.map(sourceRow).join("") +
            '<p class="caveat">' + ui.esc(manifest.rights_boundary) + '</p></div>' +
          '<div class="card"><div class="card-head"><i class="ri-forbid-2-line card-icon"></i><h4>未执行，绝不补数</h4></div>' +
            notExecuted.map(function (item) { return '<div class="not-run-row"><div><b>' + ui.esc(item.system) + '</b><p>' + ui.esc(item.reason) + '</p></div>' + ui.badge("DIM", item.status) + '</div>'; }).join("") +
          '</div>' +
        '</div>' +
        '<div class="card"><div class="card-head"><i class="ri-information-line card-icon"></i><h4>结论适用边界</h4></div><ul class="eval-list">' +
          limitations.map(function (item) { return '<li><i class="ri-arrow-right-s-line"></i><span>' + ui.esc(item) + '</span></li>'; }).join("") +
        '</ul></div>';
    }).catch(function (error) {
      host.innerHTML = ui.sectionHead("SOURCE-BOUND EVALUATION", "真实数据评测不可用") +
        '<div class="card"><div class="card-head"><i class="ri-error-warning-line card-icon"></i><h4>后端拒绝展示</h4>' + ui.badge("DANGER", "FAIL-CLOSED") + '</div><p>' + ui.esc(error.message || String(error)) + '</p><p class="caveat">请先运行 build_real_50x3.py，并修复Manifest或报告哈希后再刷新。</p></div>';
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.evaluation = render;
})();
