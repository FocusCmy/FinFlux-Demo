/* FinFlux real backend adapter. Mock replay is opt-in only with ?replay=1. */
(function () {
  "use strict";

  var replay = new URLSearchParams(window.location.search).get("replay") === "1";
  var cache = { workspace: null, workspacePromise: null, profiles: null, profilesPromise: null };

  function request(path, options) {
    return fetch(path, options || {}).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.detail || body.error || ("HTTP " + res.status));
        return body;
      });
    });
  }

  function memoryStatusSafe() {
    return request("/api/v1/memory/status").then(function (value) {
      return Object.assign({ available: true }, value || {});
    }).catch(function (error) {
      return {
        available: false,
        protocol: "FINFLUX_MEMORY_STATUS_UNAVAILABLE",
        error: (error && error.message) || "Memory status unavailable",
        structured: {},
        context: {
          backend: "UNKNOWN",
          configured: false,
          local: { objects: 0, bindings: 0, outbox: 0 },
          cache: { run_role_query_entries: 0 },
          last_remote: { status: "NOT_OBSERVED" },
          prompt_injected: false,
        },
        selected_run: null,
        truth_boundary: "Memory状态接口不可用；设置页保持可用，未据此推断缓存命中、Prompt注入或Token变化。",
      };
    });
  }

  function workspace(refresh) {
    if (!refresh && cache.workspace) return Promise.resolve(cache.workspace);
    if (!refresh && cache.workspacePromise) return cache.workspacePromise;
    if (refresh) cache.workspace = null;
    var pending = request("/api/v1/workspace").then(function (value) {
      cache.workspace = value;
      return value;
    }).finally(function () {
      if (cache.workspacePromise === pending) cache.workspacePromise = null;
    });
    cache.workspacePromise = pending;
    return pending;
  }

  function baseBudget() {
    return {
      tokens: { observed: 0, reported: 0, prompt: 0, completion: 0, call_count: 0, budget: null, percent: null, status: "NO_MODEL_CALLS", source: "SERVER_DETERMINISTIC_NO_MODEL" },
      message_proxy: { observed: 0, limit: 10000, percent: 0, source: "MATRIX_MESSAGE_CHARACTER_PROXY" },
      events: { used: 0, budget: 30 },
      wallclock: { used_s: 0, budget_s: 600 },
      note: "尚未启动 Run；无模型 Token 消耗。",
    };
  }

  function runIds(run) {
    return run ? { run_id: run.run_id, trace_id: run.trace_id, case_id: run.case_id } :
      { run_id: "NO-LIVE-RUN", trace_id: "NOT-CREATED", case_id: "NOT-CREATED" };
  }

  function snapshot(ws) {
    return (ws && ws.runtime_snapshot) || {};
  }

  function gate(ws) {
    var presentation = (ws && ws.presentation) || snapshot(ws).presentation || {};
    return presentation.gate || { state: "NOT_OPENED", label: "尚未进入负责人处理" };
  }

  function humanWorkspace() {
    return workspace(true).then(function (ws) {
      var supervisor = ws.run_supervisor || {};
      var pendingRunId = String(supervisor.run_state || "") === "AWAITING_HUMAN" ? supervisor.run_id : null;
      var selectedRunId = ws.run && ws.run.run_id;
      if (!pendingRunId || pendingRunId === selectedRunId) return ws;
      return request("/api/v1/workspace/select-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: pendingRunId })
      }).then(function () {
        cache.workspace = null;
        return workspace(true);
      });
    });
  }

  function evidenceView(ws) {
    var presentation = ws.presentation || snapshot(ws).presentation || { empty: true, ids: runIds(ws.run) };
    var value = Object.assign({}, presentation);
    value.decision_stages = ws.decision_stages || {};
    value.judge_run = ws.judge_run || null;
    return value;
  }

  window.FinfluxAPI = {
    isReplay: replay,
    getRunSummary: function () {
      return workspace().then(function (ws) {
        var run = ws.run;
        return { ids: runIds(run), status: run ? run.state : "NO_LIVE_RUN", mode: "LIVE", platform: "AgentTeams v1.2.2", model: "按服务端配置", budget: run ? run.budget : baseBudget() };
      });
    },
    getWorkspace: function (refresh) { return workspace(Boolean(refresh)); },
    getProfiles: function (refresh) {
      if (!refresh && cache.profiles) return Promise.resolve(cache.profiles);
      if (!refresh && cache.profilesPromise) return cache.profilesPromise;
      var pending = request("/api/v1/profiles").then(function (value) {
        cache.profiles = value;
        return value;
      }).finally(function () {
        if (cache.profilesPromise === pending) cache.profilesPromise = null;
      });
      cache.profilesPromise = pending;
      return pending;
    },
    getProfile: function (profileId) {
      return request("/api/v1/profiles/" + encodeURIComponent(profileId));
    },
    getRunPresentation: function (runId) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/presentation");
    },
    getRunStatus: function (runId) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/status");
    },
    getRunEvents: function (runId, after) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/events-lite?after=" + encodeURIComponent(after || 0));
    },
    getRuns: function () { return request("/api/v1/runs"); },
    selectRun: function (runId) {
      return request("/api/v1/workspace/select-run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId }) }).then(function (res) { cache.workspace = null; return res; });
    },
    setJudgeRun: function (runId, actor, reason) {
      return request("/api/v1/judge-run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId, actor: actor, reason: reason || "" }) }).then(function (res) { cache.workspace = null; return res; });
    },
    getObservability: function () {
      return workspace().then(function (ws) {
        if (!ws.run) throw new Error("尚无真实 Run 可供观测");
        if (ws.observability) return ws.observability;
        return request("/api/v1/runs/" + encodeURIComponent(ws.run.run_id) + "/observability");
      });
    },
    createEvidenceBundle: function (formData) {
      return request("/api/v1/evidence-bundles", { method: "POST", body: formData }).then(function (res) { cache.workspace = null; return res; });
    },
    inspectFile: function (formData) {
      return request("/api/v1/intake/inspect-file", { method: "POST", body: formData });
    },
    commitInspection: function (payload) {
      return request("/api/v1/intake/commit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (res) { cache.workspace = null; return res; });
    },
    getTokenGuard: function () { return request("/api/v1/token-guard"); },
    getIntakeCapabilities: function () { return request("/api/v1/intake/capabilities"); },
    searchResearchCatalog: function (query, providerId, assetClass) {
      var params = new URLSearchParams({ q: query || "", provider_id: providerId || "", asset_class: assetClass || "", limit: "20" });
      return request("/api/v1/intake/research-catalog?" + params.toString());
    },
    createUrlEvidence: function (payload) {
      return request("/api/v1/intake/public-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (res) { cache.workspace = null; return res; });
    },
    createResearchEvidence: function (payload) {
      return request("/api/v1/intake/research-items", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (res) { cache.workspace = null; return res; });
    },
    getControlPlane: function () { return request("/api/v1/control-plane/status"); },
    reconcileControlPlane: function () {
      return request("/api/v1/control-plane/reconcile", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).then(function (res) { cache.workspace = null; return res; });
    },
    getSubmissions: function () { return request("/api/v1/submissions"); },
    createChangeBundle: function (payload) {
      return request("/api/v1/change-bundles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) { cache.workspace = null; return res; });
    },
    startChangeRun: function (changeBundleId) {
      return request("/api/v1/change-bundles/" + encodeURIComponent(changeBundleId) + "/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).then(function (res) { cache.workspace = null; return res; });
    },
    startRun: function (submissionId, taskInstruction) {
      return request("/api/v1/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ submission_id: submissionId, task_instruction: taskInstruction || "", mode: "LIVE" }) }).then(function (res) { cache.workspace = null; return res; });
    },
    dispatchRun: function (runId) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/dispatch", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).then(function (res) { cache.workspace = null; return res; });
    },
    releaseRunOccupancy: function (runId, reason) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/release-occupancy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor: "demo.operator",
          reason: reason || "现场操作：占用Run长时间无Worker产物，终止为WAIT并释放单Run门禁"
        })
      }).then(function (res) { cache.workspace = null; return res; });
    },
    repairRun: function (runId, reason) {
      return request("/api/v1/runs/" + encodeURIComponent(runId) + "/repair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requested_by: "demo.operator", reason: reason || "请求AgentTeams在同一Run内恢复可恢复故障" })
      }).then(function (res) { cache.workspace = null; return res; });
    },
    getEvidenceView: function () { return workspace().then(evidenceView); },
    getRouteDecision: function () {
      return workspace().then(function (ws) {
        var run = ws.run;
        var persisted = run && run.root_route_decision;
        var decision = persisted || {
          route: "NOT_CREATED",
          reason_codes: ["NO_LIVE_RUN"],
          worker_plan: { count: 0, workers: [], parallel: false },
          required_skill_versions: {},
          policy: { policy_id: "FINFLUX_MANAGER_ROUTE_POLICY", version: "0.2.0", generated_by_model: false },
          input_facts: {},
        };
        var selectedWorkers = ((snapshot(ws).worker_plan || {}).workers || []).slice();
        decision.declared_purpose = decision.input_facts.declared_downstream_use || "NOT_DECLARED";
        decision.evidence_profile = decision.input_facts.evidence_profile || "NOT_UPLOADED";
        decision.route_state = decision.route;
        decision.budget_strategy = { max_events: 30, max_message_proxy_tokens: 10000, provider_token_hard_cap: null, description: "DETERMINISTIC_FIRST; AGENTTEAMS_ON_DEMAND; HUMAN_FINAL_AUTHORITY" };
        return {
          route_decision: decision,
          workers: selectedWorkers,
        };
      });
    },
    getTraceEvents: function () {
      return workspace().then(function (ws) {
        var source = ws.observability ? (ws.observability.events || []) : (ws.run ? (ws.run.events || []) : []);
        var runDay = ws.run && ws.run.created_at ? String(ws.run.created_at).slice(0, 10) : "1970-01-01";
        source = source.map(function (item, index) {
          var copy = Object.assign({}, item);
          var raw = String(copy.time || "");
          var parsed = raw.indexOf("T") >= 0 ? Date.parse(raw) : Date.parse(runDay + "T" + raw + "Z");
          copy.__sort_time = isNaN(parsed) ? index : parsed;
          copy.__sort_index = index;
          return copy;
        }).sort(function (a, b) { return a.__sort_time - b.__sort_time || a.__sort_index - b.__sort_index; });
        var laneMeta = {
          gateway: ["Live Intake Gateway", "ri-shield-check-line"],
          manager: ["Global Manager", "ri-route-line"],
          team_leader: ["FinFlux Case Lead", "ri-team-line"],
          worker: ["Specialist Workers", "ri-cpu-line"],
          human: ["Matrix Human Gate", "ri-user-follow-line"],
          matrix: ["Matrix Runtime", "ri-chat-3-line"],
        };
        var laneIds = [];
        var events = source.map(function (item, index) {
          var copy = Object.assign({}, item);
          delete copy.__sort_time;
          delete copy.__sort_index;
          copy.lane = laneMeta[copy.lane] ? copy.lane : "matrix";
          copy.round = index + 1;
          if (laneIds.indexOf(copy.lane) < 0) laneIds.push(copy.lane);
          return copy;
        });
        var lanes = laneIds.map(function (id) { return { lane_id: id, name: laneMeta[id][0], icon: laneMeta[id][1] }; });
        var rounds = events.map(function (item, index) { return { round: index + 1, label: index < 5 ? "准入预检 " + (index + 1) : "协作事件 " + (index - 4), time: item.time }; });
        return { lanes: lanes, rounds: rounds, events: events };
      });
    },
    getRecoveryInfo: function () {
      return workspace().then(function (ws) {
        var run = ws.run;
        var obs = ws.observability || {}, recovery = obs.recovery || {};
        var artifacts = run && run.agent_result ? run.agent_result.worker_artifacts || {} : {};
        var taskIds = Object.keys(artifacts).map(function (key) { return artifacts[key].task_id; }).filter(Boolean);
        var recovered = recovery.status === "RECOVERED_FROM_DURABLE_MATRIX";
        var idem = run && run.agentteams ? run.agentteams.manager_idempotency || {} : {};
        return { recovery: {
          checkpoint: {
            checkpoint_id: recovered ? (recovery.event_id || "DURABLE_MATRIX_RECOVERY") : (run && run.agentteams_run_id ? "DURABLE_MATRIX_RUN_STATE" : "NOT_CREATED"),
            created_at: run ? run.created_at : null,
            recovered_at: recovered ? (run.human_gate && run.human_gate.opened_at) : null,
            recovered_tasks: taskIds,
            consistency: run && run.datapass ? "Leader Matrix event + " + run.datapass.worker_artifact_count + "/" + (((run.root_route_decision || {}).worker_plan || {}).count || 3) + " Worker artifacts + DataPass hash" : "NO_DATAPASS",
            result: recovered ? "RECOVERED_WITHOUT_MODEL_REPLAY" : (run && run.datapass ? "PERSISTED_NOT_REPLAYED" : "NOT_APPLICABLE"),
            badge: recovered ? "已恢复" : (run && run.datapass ? "已持久化" : "未执行")
          },
          idempotency_lock: {
            key: idem.dispatch_idempotency_key || (run ? run.submission_id : "NO_RUN"),
            scope: "case_id + run_id + dispatch_idempotency_key",
            first_seen_at: run ? run.created_at : null,
            duplicate_calls_blocked: (idem.duplicate_event_ids || []).length,
            note: "Manager 派发与终态事件按同一 Run 去重；原始对象按 SHA256 去重。",
            badge: idem.status || "ACTIVE"
          },
          exports: [
            { kind: "audit", name: "统一 Run 审计包 JSON", file: run ? run.run_id + ".json" : "not-created.json", size: "动态" },
            {
              kind: "audit-zip",
              name: "不可变审计证据 ZIP",
              file: run ? run.run_id + "-audit.zip" : "not-created.zip",
              size: "动态",
              href: run ? "/api/v1/runs/" + encodeURIComponent(run.run_id) + "/audit-bundle.zip" : null
            }
          ]
        }, datapass: evidenceView(ws).datapass || {}, gate: gate(ws), provider_usage: obs.provider_usage || (run ? run.provider_usage : null) || {} };
      });
    },
    getSkills: function () { return request("/api/v1/skills"); },
    getMemoryStatus: memoryStatusSafe,
    getEvaluation: function () { return request("/api/v1/evaluation-report"); },
    getEvaluationManifest: function () { return request("/api/v1/evaluation-manifest"); },
    getEvaluationMetrics: function () { return request("/api/v1/evaluation-metrics"); },
    getControlledBenchmark: function () { return request("/api/v1/controlled-benchmark"); },
    getFaultEvidence: function () { return request("/api/v1/fault-evidence"); },
    getHumanGate: function () {
      return humanWorkspace().then(function (ws) {
        var ev = evidenceView(ws), g = gate(ws), run = ws.run;
        var queue = g.state === "AWAITING_HUMAN" ? [{ run_id: run.run_id, case_id: run.case_id, state: g.state, opened_at: g.gate_opened_at }] : [];
        var history = g.decision ? [{ run_id: run.run_id, case: run.case_id, decision: g.decision, actor: g.human_actor_id, decided_at: g.decided_at, reason: g.reason }] : [];
        return {
          ids: runIds(run), gate: g, datapass: ev.datapass || {}, queue: queue, history: history,
          precheck: run ? run.precheck || {} : {},
          report_preview: run ? run.report_preview || null : null,
          final_result: run ? run.final_result || null : null,
          lineage: run ? run.lineage || {} : {},
          runtime: ws.runtime || {},
          token_guard: ws.token_guard || {}, presentation: ev,
          run: run,
        };
      });
    },
    submitHumanDecision: function (decision, actor, reason, remediationPlan) {
      return humanWorkspace().then(function (ws) {
        if (!ws.run || ws.run.human_gate.state !== "AWAITING_HUMAN") throw new Error("Human Gate 尚未由真实 AgentTeams Run 开启，拒绝伪造签署");
        var backendDecision = { REJECT: "CONFIRM_BLOCK", RETURN_FOR_EVIDENCE: "REQUEST_EVIDENCE" }[decision] || decision;
        return request("/api/v1/runs/" + encodeURIComponent(ws.run.run_id) + "/human-decisions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: backendDecision, human_actor_id: actor, reason: reason || "", remediation_plan: remediationPlan || null }) });
      }).then(function (res) { cache.workspace = null; return res; });
    },
    getSettings: function () {
      return Promise.all([workspace(), memoryStatusSafe()]).then(function (values) {
        var ws = values[0], memory = values[1];
        var limits = (ws.runtime.bounded_execution && ws.runtime.bounded_execution.limits) || {};
        if (!memory.selected_run && ws.run) {
          memory.selected_run = {
            run_id: ws.run.run_id,
            operational_memory_plan: ws.run.operational_memory_plan || null,
            context_capsule_handle: ws.run.context_capsule_handle || null,
            source: "WORKSPACE_FALLBACK",
          };
        }
        return {
          runtime: { platform: "AgentTeams v1.2.2", model: "按服务端环境配置", console: "FinFlux /api/v1", matrix_homeserver: ws.runtime.resources && ws.runtime.resources.team_room_id ? ws.runtime.resources.team_room_id : "未连接", element_url: ws.runtime.element_url, evidence_storage: "runtime/live_intake/objects/<sha256>", mode: ws.runtime.connected ? "CONNECTED" : "OFFLINE_FAIL_CLOSED", truthful_note: ws.runtime.truthful_note },
          budget_policy: { policy_id: (ws.runtime.bounded_execution && ws.runtime.bounded_execution.policy_id) || "FINFLUX-BOUNDED-EXECUTION-V0.1", version: "0.1.0", provider_token_hard_cap: null, max_message_proxy_tokens: limits.max_observed_message_token_estimate || 10000, max_model_rounds: 6, max_events: limits.max_matrix_events || 30, max_retries: 1, deadline_s: limits.max_wall_time_seconds || 600, warning_ratio: 0.75, requires_human_raise: true },
          about: { project: "FinFlux", scenario: "真实金融数据语义准入", version: "live-intake-p0", tagline: ws.truthful_boundary },
          theme: { name: "FinFlux Dark", note: "现场演示优先：状态、来源、真实性边界必须可见。" },
          memory: memory,
        };
      });
    },
    exportBundle: function () {
      return workspace().then(function (ws) { if (!ws.run) throw new Error("尚无可导出的真实 Run"); return request("/api/v1/runs/" + encodeURIComponent(ws.run.run_id) + "/audit-bundle"); });
    },
  };
})();
