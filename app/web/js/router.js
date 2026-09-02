/* ============================================================
   FinFlux 前端 hash 路由
   路由表（与侧边导航一一对应）：
     #/live        运行与协作   Live Operations
     #/evidence    证据与决策   Evidence & Decisions
     #/changes     变更与影响   Change & Blast Radius
     #/trace       Trace 与恢复 Trace & Recovery
     #/evaluation  评测与边界 Evaluation & Limits
     #/skills      Skill 注册表 Skill Registry
     #/human-gate  Human Gate 人工审核
     #/settings    系统设置     System Settings
   仅渲染当前路由；切换路由共享同一 Run 上下文，不重启工作。
   ============================================================ */
(function () {
  "use strict";

  var ROUTES = {
    "live": { title: "Case 工作台", crumb: "CASE WORKBENCH", view: "live" },
    "collaboration": { title: "多Agent 决策台", crumb: "AGENTTEAMS DECISION CONSOLE", view: "collaboration" },
    "evidence": { title: "DataPass 与 Human", crumb: "DATAPASS & HUMAN AUTHORITY", view: "evidence" },
    "changes": { title: "演化与审计", crumb: "CONTROLLED EVOLUTION & AUDIT", view: "changes" },
    "trace": { title: "Trace 与恢复", crumb: "TRACE & RECOVERY", view: "trace" },
    "evaluation": { title: "评测与边界", crumb: "EVALUATION & LIMITS", view: "evaluation" },
    "skills": { title: "Skill 注册表", crumb: "SKILL REGISTRY", view: "skills" },
    "human-gate": { title: "Human 与报告", crumb: "HUMAN GATE & REPORT", view: "humanGate" },
    "settings": { title: "系统设置", crumb: "SYSTEM SETTINGS", view: "settings" },
  };
  var DEFAULT_ROUTE = "live";

  function parseRoute() {
    var raw = String(window.location.hash || "").replace(/^#\/?/, "").split("?")[0].split("/")[0].toLowerCase();
    return Object.prototype.hasOwnProperty.call(ROUTES, raw) ? raw : null;
  }

  function applyRoute() {
    var route = parseRoute();
    if (!route) {
      window.location.replace("#/" + DEFAULT_ROUTE);
      return;
    }
    var meta = ROUTES[route];

    document.querySelectorAll(".nav-item[data-route]").forEach(function (item) {
      item.classList.toggle("active", item.getAttribute("data-route") === route);
    });
    document.querySelectorAll("[data-stage-route]").forEach(function (item) {
      item.classList.toggle("active", item.getAttribute("data-stage-route") === route);
    });

    var title = document.getElementById("page-title");
    var crumb = document.getElementById("page-crumb");
    if (title) title.textContent = meta.title;
    if (crumb) crumb.textContent = "FINFLUX CONSOLE / " + meta.crumb;
    document.title = meta.title + " · FinFlux 控制台";

    var host = document.getElementById("view");
    if (!host) return;
    host.innerHTML = '<div class="view-loading"><i class="ri-loader-4-line ri-spin"></i> 载入视图数据…</div>';

    var render = window.FinfluxViews && window.FinfluxViews[meta.view];
    if (typeof render !== "function") {
      host.innerHTML = '<div class="view-loading">视图未注册：' + meta.view + "</div>";
      return;
    }
    Promise.resolve(render(host)).catch(function (err) {
      host.innerHTML = '<div class="view-loading">视图渲染失败：' + (err && err.message ? err.message : err) + "</div>";
    });

    var scroller = document.getElementById("main-scroll");
    if (scroller) scroller.scrollTo(0, 0);
  }

  window.FinfluxRouter = { apply: applyRoute, routes: ROUTES };

  window.addEventListener("hashchange", applyRoute);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      window.FinfluxUI.refreshTopbar();
      applyRoute();
    });
  } else {
    window.FinfluxUI.refreshTopbar();
    applyRoute();
  }
})();
