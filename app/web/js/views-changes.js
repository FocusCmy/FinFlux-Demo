/* Page: two immutable submissions -> ChangeSet -> explicit blast radius. */
(function () {
  "use strict";
  function esc(value) { return window.FinfluxUI.esc(value == null ? "" : value); }
  function short(value) { return window.FinfluxUI.shaShort(value || "", 18, 7); }

  function downstreamTemplate(submissions, registry) {
    var first = submissions[0] || {};
    var definition = ((registry && registry.profiles) || []).find(function (item) { return item.profile_id === first.profile; });
    if (!definition) return [];
    return Object.keys(definition.purpose_bindings || {}).map(function (purposeId) {
      var binding = definition.purpose_bindings[purposeId] || {};
      return {
        task_id: definition.profile_id + "-" + purposeId,
        label: binding.label || purposeId,
        owner: "PROFILE_ACCOUNTABLE_OWNER",
        purpose: purposeId,
        criticality: "TO_BE_CONFIRMED",
        dependencies: ["metadata.declared_purpose"].concat(binding.required_field ? ["parsed." + binding.required_field] : []),
      };
    });
  }

  function option(item) {
    var meta = item.metadata || {}, file = item.file || {};
    return '<option value="' + esc(item.submission_id) + '" title="审计ID：' + esc(item.audit_submission_id || item.submission_id) + '">' +
      esc(item.display_submission_id || item.submission_id) + " · " + esc(meta.candidate_mapping || "?") + " · " + esc(file.name || "?") +
      '</option>';
  }

  function createPanel(submissions, registry) {
    var options = submissions.map(option).join("");
    var suggestedTasks = downstreamTemplate(submissions, registry);
    var warning = submissions.length < 2
      ? '<div class="truth-banner truth-warn"><i class="ri-alert-line"></i><b>还缺一个可比较版本</b><span>请在Case工作台接入同一业务对象的另一个真实发布版本；系统只比较已经固化的证据与声明。</span></div>'
      : "";
    return warning + '<section class="card change-create">' +
      '<div class="card-head"><i class="ri-git-compare-line card-icon"></i><h4>创建 ChangeBundle</h4><span class="mini-badge mini-blue">真实版本对比</span></div>' +
      '<p class="card-note">比较两份已固化证据版本，识别来源、业务用途、语义声明或原始发布内容的真实变化，并沿显式依赖寻找影响范围。</p>' +
      '<form id="change-form">' +
        '<div class="change-select-grid">' +
          '<label class="form-label">基线版本 V1<select id="change-baseline" required>' + options + '</select></label>' +
          '<div class="change-arrow"><i class="ri-arrow-right-line"></i><span>Observed Diff</span></div>' +
          '<label class="form-label">候选版本 V2<select id="change-candidate" required>' + options + '</select></label>' +
        '</div>' +
        '<label class="form-label">下游依赖清单（由当前Profile生成；生产环境可由血缘平台提供）' +
          '<textarea id="change-tasks" class="mono change-json" rows="10">' + esc(JSON.stringify(suggestedTasks, null, 2)) + '</textarea>' +
        '</label>' +
        '<button class="btn btn-primary btn-xl" type="submit" ' + (submissions.length < 2 ? "disabled" : "") + '><i class="ri-radar-line"></i> 生成变化证据与影响范围</button>' +
      '</form>' +
    '</section>';
  }

  function changesTable(bundle) {
    var changes = (bundle.change_set && bundle.change_set.changes) || [];
    if (!changes.length) return '<div class="empty-inline">两份提交在受控字段中没有观察到差异。</div>';
    return '<div class="change-table">' + changes.map(function (item) {
      return '<div class="change-row">' +
        '<span class="mini-badge mini-blue">' + esc(item.category) + '</span>' +
        '<b class="mono">' + esc(item.path) + '</b>' +
        '<span class="mono change-before">' + esc(JSON.stringify(item.before)) + '</span>' +
        '<i class="ri-arrow-right-line"></i>' +
        '<span class="mono change-after">' + esc(JSON.stringify(item.after)) + '</span>' +
      '</div>';
    }).join("") + '</div>';
  }

  function impactCards(bundle) {
    var nodes = (bundle.impact_graph && bundle.impact_graph.nodes) || [];
    return '<div class="impact-grid">' + nodes.map(function (node) {
      var tone = node.impact_state === "AFFECTED" ? "red" : (node.impact_state === "UNKNOWN_IMPACT" ? "amber" : "green");
      return '<article class="card impact-card impact-' + tone + '">' +
        '<div class="card-head"><i class="ri-node-tree card-icon"></i><h4>' + esc(node.label) + '</h4><span class="badge badge-' + tone + '">' + esc(node.impact_state) + '</span></div>' +
        window.FinfluxUI.kv("Task / Owner", '<span class="mono">' + esc(node.task_id) + '</span> / ' + esc(node.owner)) +
        window.FinfluxUI.kv("用途 / 关键级", esc(node.purpose) + ' / <b>' + esc(node.criticality) + '</b>') +
        window.FinfluxUI.kv("声明依赖", '<span class="mono">' + esc((node.dependencies || []).join(", ") || "未登记") + '</span>') +
        window.FinfluxUI.kv("命中变化", '<span class="mono">' + esc((node.matched_changes || []).join(", ") || "—") + '</span>') +
      '</article>';
    }).join("") + '</div>';
  }

  function resultPanel(bundle, ws) {
    if (!bundle) {
      var run = ws.run || {}, view = ws.presentation || {}, evolution = view.evolution || {}, ids = view.ids || {};
      return '<section class="card evolution-run-card"><div class="card-head"><i class="ri-git-branch-line card-icon"></i><h4>当前Run受控演化血缘</h4>' + window.FinfluxUI.badge(run.state || 'NO_RUN') + '</div>' +
        window.FinfluxUI.kv("当前Run", '<span class="mono">' + esc(ids.run_id || '尚未创建') + '</span>') +
        window.FinfluxUI.kv("父Run", '<span class="mono">' + esc(evolution.parent_run_id || 'ROOT') + '</span>') +
        window.FinfluxUI.kv("原始证据", evolution.raw_evidence_mutated === false ? '<b class="text-green">保持不变</b>' : '未确认') +
        window.FinfluxUI.kv("Human状态", esc(((view.human_gate || {}).label) || '尚未进入')) +
        '<p class="card-note">PASS直接进入负责人授权；WAIT补充证据；BLOCK只能由Human批准修订计划后创建带血缘子Run，原Run永不改写。</p>' +
        (run.run_id ? '<div class="gate-actions"><a class="btn btn-outline" href="/api/v1/runs/' + encodeURIComponent(run.run_id) + '/audit-bundle.zip"><i class="ri-download-2-line"></i> 下载审计ZIP</a><a class="btn btn-outline" href="#/trace"><i class="ri-fingerprint-line"></i> 查看Trace与Token</a></div>' : '') +
      '</section>';
    }
    var summary = (bundle.impact_graph || {}).summary || {};
    var receipts = bundle.skill_invocations || [];
    return '<section class="change-result">' +
      '<div class="section-head"><div><p class="eyebrow">CHANGE EVIDENCE</p><h2>变化与影响调查结果</h2></div><span class="badge badge-amber"><i class="dot"></i>' + esc(bundle.state) + '</span></div>' +
      '<div class="card change-summary">' +
        '<div class="stat-grid4"><div class="stat"><b>' + esc(bundle.change_set.change_count) + '</b><span>已观察变化</span></div><div class="stat"><b class="text-red">' + esc(summary.affected_tasks || 0) + '</b><span>受影响任务</span></div><div class="stat"><b class="text-amber">' + esc(summary.unknown_impact_tasks || 0) + '</b><span>未知影响</span></div><div class="stat"><b>0</b><span>模型 Token</span></div></div>' +
        window.FinfluxUI.kv("ChangeBundle ID", '<span class="mono">' + esc(bundle.change_bundle_id) + '</span>' + window.FinfluxUI.copyBtn(bundle.change_bundle_id)) +
        window.FinfluxUI.kv("原始文件变化", bundle.change_set.raw_file_changed ? '<b class="text-red">是</b>' : '<b class="text-green">否；本例变化来自版本/接入配置</b>') +
        window.FinfluxUI.kv("Bundle SHA256", '<span class="mono">' + esc(short(bundle.bundle_sha256)) + '</span>' + window.FinfluxUI.copyBtn(bundle.bundle_sha256)) +
        '<p class="card-note"><i class="ri-shield-check-line"></i> ' + esc(bundle.truth_boundary) + '</p>' +
      '</div>' +
      '<section class="card"><div class="card-head"><i class="ri-file-diff-line card-icon"></i><h4>观察到的差异</h4><span class="mini-badge mini-blue">不做金融真值推断</span></div>' + changesTable(bundle) + '</section>' +
      '<div class="section-head compact"><div><p class="eyebrow">DECLARED LINEAGE</p><h2>下游影响范围</h2></div></div>' + impactCards(bundle) +
      '<section class="card"><div class="card-head"><i class="ri-flashlight-line card-icon"></i><h4>本次实际执行的 Skill</h4><span class="mini-badge mini-green">' + receipts.length + ' receipts</span></div>' +
        receipts.map(function (item) { return '<div class="file-row"><span class="badge badge-green">VERIFIED</span><span class="file-name"><b>' + esc(item.skill_id) + '</b> <span class="mono">@' + esc(item.version) + '</span></span><span class="mono">' + esc(short(item.output_sha256)) + '</span><span>0 Token</span></div>'; }).join("") +
        '<p class="lock-note"><i class="ri-user-follow-line"></i> 当前结果只说明“哪里变了、哪些任务可能受影响”；生产准入仍必须经过 AgentTeams 专业核验与 Human 签署。</p>' +
        '<button id="btn-change-run" class="btn btn-primary btn-xl" type="button"><i class="ri-node-tree"></i> 创建变更调查 Run</button>' +
      '</section>' +
    '</section>';
  }

  function bind(host, submissions) {
    var baseline = host.querySelector("#change-baseline"), candidate = host.querySelector("#change-candidate");
    if (baseline && candidate && submissions.length > 1) {
      baseline.selectedIndex = 1;
      candidate.selectedIndex = 0;
    }
    var form = host.querySelector("#change-form");
    if (!form) return;
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var tasks;
      try { tasks = JSON.parse(host.querySelector("#change-tasks").value); }
      catch (error) { return window.FinfluxUI.toast("下游任务 Manifest 不是合法 JSON", "error"); }
      if (baseline.value === candidate.value) return window.FinfluxUI.toast("V1 与 V2 必须选择不同 Submission", "error");
      var button = form.querySelector("button[type=submit]");
      button.disabled = true; button.innerHTML = '<i class="ri-loader-4-line ri-spin"></i> 计算 ChangeSet 与影响图…';
      window.FinfluxAPI.createChangeBundle({
        baseline_submission_id: baseline.value,
        candidate_submission_id: candidate.value,
        downstream_tasks: tasks,
      }).then(function (bundle) {
        window.FinfluxUI.toast("ChangeBundle 已生成 · " + bundle.change_bundle_id, "success");
        return render(host, true);
      }).catch(function (error) {
        window.FinfluxUI.toast(error.message, "error");
        button.disabled = false; button.innerHTML = '<i class="ri-radar-line"></i> 生成变化证据与影响范围';
      });
    });
    var changeRun = host.querySelector("#btn-change-run");
    if (changeRun) changeRun.addEventListener("click", function () {
      window.FinfluxAPI.getWorkspace(true).then(function (ws) {
        if (!ws.change_bundle) throw new Error("ChangeBundle不存在，请先生成变化证据");
        changeRun.disabled = true;
        changeRun.innerHTML = '<i class="ri-loader-4-line ri-spin"></i> Manager 正在生成动态路由…';
        return window.FinfluxAPI.startChangeRun(ws.change_bundle.change_bundle_id);
      }).then(function (run) {
        window.FinfluxUI.toast("变更调查 Run 已创建 · " + run.run_id, "success");
        window.FinfluxUI.refreshTopbar();
        window.location.hash = "#/live";
      }).catch(function (error) {
        window.FinfluxUI.toast(error.message, "error");
        changeRun.disabled = false;
        changeRun.innerHTML = '<i class="ri-node-tree"></i> 创建变更调查 Run';
      });
    });
  }

  function render(host, refresh) {
    return Promise.all([
      window.FinfluxAPI.getSubmissions(),
      window.FinfluxAPI.getWorkspace(refresh),
      window.FinfluxAPI.getProfiles(),
    ]).then(function (parts) {
      var submissions = parts[0].submissions || [], ws = parts[1], registry = parts[2];
      host.innerHTML = '<div class="truth-banner"><i class="ri-shield-check-line"></i><b>变化不等于错误</b><span>FinFlux 先固化两个真实版本，再按显式血缘找影响；金融真值由专业核验与责任人决定。</span></div>' +
        '<div class="change-layout">' + createPanel(submissions, registry) + resultPanel(ws.change_bundle, ws) + '</div>';
      bind(host, submissions);
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.changes = render;
})();
