/* ============================================================
   页面 4 · Skill 注册表（#/skills）
   20 个原子 Skill 按 5 个能力包分组；单个 Run 只按需加载。
   FinFlux_MCP_Tool_and_Skill_Specification_v1.0 §25-§28。
   ============================================================ */
(function () {
  "use strict";

  var UI = function () { return window.FinfluxUI; };

  var SKILL_ICONS = {
    "verify-evidence-bundle": "ri-archive-stack-line",
    "reconcile-source-semantics": "ri-git-compare-line",
    "resolve-semantic-contract": "ri-book-marked-line",
    "compute-financial-impact": "ri-calculator-line",
    "validate-admission-package": "ri-shield-check-line",
    "assemble-run-result-context": "ri-stack-line",
    "select-token-budget-strategy": "ri-speed-up-line",
    "compose-result-document": "ri-file-text-line",
    "verify-result-artifact": "ri-file-shield-2-line",
  };

  var PACKS = [
    { id: "admission", name: "金融准入核心", note: "证据、权利、语义契约、金额复算与独立验证", skills: ["evidence-integrity", "rights-gate", "semantic-contract-resolver", "financial-impact-calculator", "independent-evidence-validator"] },
    { id: "specialists", name: "按需专业核验", note: "机密边界、研究上下文与运行韧性，只在Manager选中时加载", skills: ["classify-data-rights", "enforce-confidentiality-boundary", "retrieve-research-context", "verify-research-context", "guard-execution-budget", "audit-recovery-readiness"] },
    { id: "context", name: "上下文与成本控制", note: "同Run内容寻址胶囊、角色最小切片和缓存审计", skills: ["build-run-context-capsule", "load-role-context-slice"] },
    { id: "result", name: "结果与责任固化", note: "生成通俗结果、固定文件并校验Human签署边界", skills: ["assemble-run-result-context", "select-token-budget-strategy", "compose-result-document", "verify-result-artifact"] },
    { id: "evolution", name: "变更与受控演化", note: "版本差异、下游血缘与修复方案复核", skills: ["detect-version-change", "resolve-downstream-lineage", "validate-remediation-plan"] },
  ];

  function skillCard(s) {
    var ui = UI();
    return '<div class="card skill-card" data-skill="' + ui.esc(s.skill_id) + '">' +
      '<div class="card-head"><i class="' + (SKILL_ICONS[s.skill_id] || "ri-flashlight-line") + ' card-icon"></i>' +
        "<h4>" + ui.esc(s.skill_id) + ' <span class="mono ver">' + ui.esc(s.version) + "</span></h4>" +
        ui.badge(s.status) + "</div>" +
      '<div class="chip-row">' +
        '<span class="badge badge-green"><i class="dot"></i>' + (s.owner_role === "Result Composer Agent" ? "报告链 Skill" : "金融确定性 Skill") + '</span>' +
        '<span class="chip chip-dim mono">' + ui.esc(s.channel) + "</span>" +
        '<span class="chip chip-cyan mono">' + ui.esc(s.capability_id) + "</span>" +
      "</div>" +
      '<p class="card-note">' + ui.esc(s.purpose) + "</p>" +
      ui.kv("Owner 角色", ui.esc(s.owner_role)) +
      ui.kv("输入", ui.esc(s.input_summary)) +
      ui.kv("输出", ui.esc(s.output_summary)) +
      ui.kv("当前 Run 调用", '<b class="mono">' + ui.esc(s.runtime_invocations || 0) + " 次</b>") +
      ui.kv("发现状态", '<span class="mono">' + ui.esc(s.discovery_state) + "</span>") +
      (s.input_sha256 ? ui.kv("真实输入摘要", '<span class="mono">' + ui.esc(ui.shaShort(s.input_sha256, 16, 4)) + "</span>" + ui.copyBtn(s.input_sha256)) : "") +
      (s.output_sha256 ? ui.kv("真实输出摘要", '<span class="mono">' + ui.esc(ui.shaShort(s.output_sha256, 16, 4)) + "</span>" + ui.copyBtn(s.output_sha256)) : "") +
      ui.kv("包摘要", '<span class="mono">' + ui.esc(ui.shaShort(s.digest, 16, 4)) + "</span>" + ui.copyBtn(s.digest)) +
      '<p class="card-note"><i class="ri-information-line"></i> ' + ui.esc(s.truthful_note) + "</p>" +
    "</div>";
  }

  function render(host) {
    var ui = UI();
    return window.FinfluxAPI.getSkills().then(function (res) {
      var invoked = res.skills.filter(function (s) { return Number(s.runtime_invocations || 0) > 0; }).length;
      var byId = {};
      res.skills.forEach(function (s) { byId[s.skill_id] = s; });
      var packs = PACKS.map(function (pack, index) {
        var items = pack.skills.map(function (id) { return byId[id]; }).filter(Boolean);
        var used = items.filter(function (s) { return Number(s.runtime_invocations || 0) > 0; }).length;
        return '<details class="skill-pack" ' + (index === 0 ? 'open' : '') + '><summary><div><b>' + ui.esc(pack.name) + '</b><span>' + ui.esc(pack.note) + '</span></div><em>' + used + '/' + items.length + ' 本Run调用</em></summary><div class="skill-grid">' + items.map(skillCard).join("") + '</div></details>';
      }).join('');
      host.innerHTML =
        ui.sectionHead("SKILL REGISTRY", "Skill 注册表",
          '<span class="badge badge-purple"><i class="dot"></i>运行时发现 · 版本固定 · 摘要可验</span>') +
        '<div class="banner banner-blue slim-banner"><i class="ri-information-line"></i>' +
          "<span>20个是工程内部的原子能力，不是单Run执行20次。Manager按Case路由，Worker只加载必要Skill；每次调用记录精确版本与digest，模型不代替确定性Skill产生金融数值。</span></div>" +
        '<div class="skill-registry-summary"><span><b>' + res.skills.length + '</b>注册Skill</span><span><b>5</b>能力包</span><span><b>' + invoked + '</b>所选Run已调用</span></div>' + packs;

      host.querySelectorAll(".skill-card").forEach(function (card) {
        card.addEventListener("click", function (e) {
          if (e.target.closest(".copy-btn")) return;
          var s = res.skills.find(function (x) { return x.skill_id === card.getAttribute("data-skill"); });
          if (s) ui.toast(s.skill_id + "@" + s.version + " · " + s.purpose, "info");
        });
      });
    });
  }

  window.FinfluxViews = window.FinfluxViews || {};
  window.FinfluxViews.skills = render;
})();
