/* ============================================================
   FinFlux 前端共享 UI 工具：格式化、徽章、Toast、确认框、
   复制到剪贴板、Blob 下载。所有视图共用。
   ============================================================ */
(function () {
  "use strict";

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  function fmtNum(value, digits) {
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return n.toLocaleString("zh-CN", {
      minimumFractionDigits: digits == null ? 0 : digits,
      maximumFractionDigits: digits == null ? 2 : digits,
    });
  }

  /* 哈希/长 ID 截断显示：保留前缀与后缀 */
  function shaShort(text, head, tail) {
    var s = String(text || "");
    var h = head == null ? 18 : head;
    var t = tail == null ? 6 : tail;
    if (s.length <= h + t + 1) return s;
    return s.slice(0, h) + "…" + s.slice(-t);
  }

  /* 状态 -> 颜色语义 */
  var STATUS_TONE = {
    PASS: "green", SUCCESS: "green", VERIFIED: "green", COMPLETED: "green",
    APPROVED: "green", RETRY_SUCCESS: "green", ACTIVE: "green",
    RUNNING: "blue", EXECUTING: "blue", ROUTING: "blue",
    AWAITING_HUMAN: "amber", WAITING: "amber", RETRY: "amber", RETURNED: "amber",
    TIMEOUT: "amber", BLOCK: "red", FAILED: "red", REJECTED: "red", REJECT: "red",
  };

  function toneOf(status) {
    return STATUS_TONE[String(status || "").toUpperCase()] || "blue";
  }

  /* 发光圆点 + 胶囊徽章 */
  function badge(status, label) {
    var tone = toneOf(status);
    var pulse = String(status).toUpperCase() === "AWAITING_HUMAN" ? " pulse" : "";
    return '<span class="badge badge-' + tone + pulse + '"><i class="dot"></i>' +
      esc(label || status) + "</span>";
  }

  function dot(status) {
    return '<i class="dot dot-' + toneOf(status) + '"></i>';
  }

  /* 复制按钮（data-copy 属性，全局委托处理） */
  function copyBtn(text, title) {
    return '<button class="copy-btn" type="button" data-copy="' + esc(text) + '" title="' +
      esc(title || "复制") + '"><i class="ri-file-copy-line"></i></button>';
  }

  /* 键值行 */
  function kv(key, valueHtml, mono) {
    return '<div class="kv"><span class="kv-k">' + esc(key) + '</span>' +
      '<span class="kv-v' + (mono ? " mono" : "") + '">' + valueHtml + "</span></div>";
  }

  /* 区块标题（eyebrow + 中文标题 + 右侧徽章位） */
  function sectionHead(eyebrow, title, rightHtml) {
    return '<div class="sec-head"><div><p class="eyebrow">' + esc(eyebrow) + "</p>" +
      "<h3>" + esc(title) + "</h3></div>" + (rightHtml ? '<div class="sec-right">' + rightHtml + "</div>" : "") + "</div>";
  }

  /* ---------------- Toast ---------------- */
  function toast(message, type) {
    var host = document.getElementById("toast-host");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast toast-" + (type || "info");
    el.innerHTML = '<i class="' +
      (type === "success" ? "ri-checkbox-circle-line" : type === "warn" ? "ri-error-warning-line" : "ri-information-line") +
      '"></i><span>' + esc(message) + "</span>";
    host.appendChild(el);
    setTimeout(function () { el.classList.add("show"); }, 16);
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { el.remove(); }, 320);
    }, 3200);
  }

  /* ---------------- 确认对话框（Promise<boolean>） ---------------- */
  function confirmDialog(opts) {
    return new Promise(function (resolve) {
      var host = document.getElementById("modal-host");
      if (!host) { resolve(window.confirm(opts.title)); return; }
      var wrap = document.createElement("div");
      wrap.className = "modal-mask";
      wrap.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
        '<div class="modal-head"><i class="' + esc(opts.icon || "ri-shield-keyhole-line") + '"></i><h4>' + esc(opts.title) + "</h4></div>" +
        '<div class="modal-body">' + (opts.bodyHtml || "<p>" + esc(opts.body || "") + "</p>") + "</div>" +
        '<div class="modal-foot">' +
        '<button type="button" class="btn btn-ghost" data-act="cancel">取消</button>' +
        '<button type="button" class="btn ' + esc(opts.confirmClass || "btn-primary") + '" data-act="ok">' + esc(opts.confirmText || "确认") + "</button>" +
        "</div></div>";
      host.appendChild(wrap);
      requestAnimationFrame(function () { wrap.classList.add("show"); });
      wrap.addEventListener("click", function (e) {
        var act = e.target.closest("[data-act]");
        var isMask = e.target === wrap;
        if (!act && !isMask) return;
        var confirmed = act && act.getAttribute("data-act") === "ok";
        wrap.classList.remove("show");
        setTimeout(function () { wrap.remove(); }, 200);
        resolve(!!confirmed);
      });
    });
  }

  /* ---------------- JSON Blob 下载 ---------------- */
  function downloadJson(filename, payload) {
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  /* ---------------- 顶栏刷新 ---------------- */
  function refreshTopbar() {
    window.FinfluxAPI.getRunSummary().then(function (s) {
      var runId = document.getElementById("topbar-run-id");
      if (runId) runId.textContent = s.ids.run_id;
      var status = document.getElementById("topbar-status");
      if (status) {
        status.className = "status-badge status-" + toneOf(s.status) +
          (s.status === "AWAITING_HUMAN" ? " pulse" : "");
        status.innerHTML = '<i class="dot"></i>' + esc(s.status);
      }
      var tok = document.getElementById("topbar-token-text");
      var tokenLedger = s.budget.tokens || {};
      var providerReported = tokenLedger.status === "PROVIDER_REPORTED";
      if (tok) tok.textContent = providerReported ? fmtNum(tokenLedger.reported) : (tokenLedger.status === "NO_MODEL_CALLS" ? "0" : "待归集");
      var bar = document.getElementById("topbar-token-bar");
      if (bar) bar.style.width = "0%";
      var pct = document.getElementById("topbar-token-pct");
      if (pct) pct.textContent = providerReported ? fmtNum(tokenLedger.call_count || 0) + " calls" : tokenLedger.status;
      var ev = document.getElementById("topbar-events");
      if (ev) ev.textContent = s.budget.events.used + " / " + s.budget.events.budget;
      var wc = document.getElementById("topbar-wallclock");
      if (wc) wc.textContent = s.budget.wallclock.used_s + " / " + s.budget.wallclock.budget_s + "s";
    });
  }

  /* ---------------- 全局事件委托：复制 ---------------- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    function done() { toast("已复制到剪贴板", "success"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (err) { toast("复制失败", "warn"); }
      ta.remove();
    }
  });

  window.FinfluxUI = {
    esc: esc, fmtNum: fmtNum, shaShort: shaShort,
    badge: badge, dot: dot, toneOf: toneOf,
    copyBtn: copyBtn, kv: kv, sectionHead: sectionHead,
    toast: toast, confirmDialog: confirmDialog,
    downloadJson: downloadJson, refreshTopbar: refreshTopbar,
  };
})();
