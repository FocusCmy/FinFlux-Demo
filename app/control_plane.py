from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class ControlPlaneSupervisor:
    """Deterministic HA guard for dispatch, checkpoints and recovery advice.

    This controller deliberately performs no financial calculation and invokes
    no model.  It makes infrastructure state explicit so the AgentTeam cannot
    silently accept work while its runtime or durable checkpoint is unhealthy.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / "control_plane"
        self.latest_path = self.root / "latest.json"
        self.lock = threading.RLock()

    def inspect(
        self,
        runtime: dict[str, Any],
        run: dict[str, Any] | None,
        admission_guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        topology = runtime.get("topology") or []
        required_topology = [item for item in topology if not item.get("on_demand")]
        extension_topology = [item for item in topology if item.get("on_demand")]
        ready = [
            item for item in required_topology
            if str(item.get("phase", "")).upper() in {"READY", "RUNNING", "ACTIVE"}
        ]
        ready_extensions = [
            item for item in extension_topology
            if str(item.get("phase", "")).upper() in {"READY", "RUNNING", "ACTIVE"}
        ]
        expected = len(required_topology)
        runtime_connected = bool(runtime.get("connected"))
        guarded_run_id = (admission_guard or {}).get("active_run_id")
        guarded_run_state = (admission_guard or {}).get("active_run_state")
        selected_run_id = (run or {}).get("run_id")
        selected_run_state = str((run or {}).get("state", "NO_RUN"))
        active_states = {
            "DISPATCHED", "WORKERS_RUNNING", "DATAPASS_DRAFTED", "AWAITING_HUMAN"
        }
        # When a Provider Token Guard is supplied it is the sole authority for
        # model-active Run identity.  The UI-selected historical Run remains a
        # checkpoint subject, but must never be relabelled as active merely
        # because its snapshot is being inspected.
        if admission_guard is not None:
            active_run_id = guarded_run_id
            active_state = str(guarded_run_state or "NO_ACTIVE_RUN")
        elif selected_run_state in active_states:
            active_run_id = selected_run_id
            active_state = selected_run_state
        else:
            active_run_id = None
            active_state = "NO_ACTIVE_RUN"
        active_run = bool(active_run_id) and active_state in active_states
        lifecycle = (run or {}).get("lifecycle") or {}
        history = lifecycle.get("history") or []
        # Run lifecycle entries are independently hash-sealed.  Older runs do
        # not carry a top-level history_sha256, so requiring that optional
        # aggregate field would incorrectly report a healthy durable Run as
        # checkpoint-less.
        aggregate_hash = len(str(lifecycle.get("history_sha256", ""))) == 64
        entry_hashes = bool(
            lifecycle.get("current_phase")
            and history
            and all(
                len(str(item.get("transition_sha256", ""))) == 64
                for item in history
            )
        )
        durable = bool(run and (aggregate_hash or entry_hashes))
        if admission_guard and admission_guard.get("allowed") is False:
            admission = "BLOCKED_BY_TOKEN_GUARD"
            guard_reasons = list(admission_guard.get("reasons") or [])
            action = "Token Guard拒绝新模型派发：" + (
                "；".join(str(item) for item in guard_reasons)
                if guard_reasons
                else "供应商用量或活动Run状态不满足准入条件"
            )
        elif not runtime_connected:
            admission = "DEGRADED_NO_DISPATCH"
            action = "恢复AgentTeams Runtime后再派发；接入与证据固化仍可继续"
        elif expected and len(ready) < expected:
            admission = "DEGRADED_ROLE_NOT_READY"
            action = "保持fail-closed，等待缺失角色恢复或由运维执行受控恢复"
        elif active_run:
            admission = "HOLD_ACTIVE_RUN"
            action = "保护当前Run；完成或处理Human Gate后再接收下一条高成本任务"
        else:
            admission = "READY"
            action = "允许接收一条新Run；仍受幂等键、预算和Human Gate约束"
        core = {
            "protocol": "FINFLUX_HA_CONTROL_PLANE_V0.1",
            "checked_at": utc_now(),
            "runtime_connected": runtime_connected,
            "topology_ready": len(ready),
            "topology_expected": expected,
            "extension_topology_ready": len(ready_extensions),
            "extension_topology_registered": len(extension_topology),
            "active_run_id": active_run_id,
            "active_run_state": active_state,
            "selected_run_id": selected_run_id,
            "selected_run_state": selected_run_state,
            "checkpoint_run_id": selected_run_id if durable else None,
            "durable_checkpoint_present": durable,
            "admission_state": admission,
            "recommended_action": action,
            "recovery_policy": {
                "model_replay": "FORBIDDEN_BY_DEFAULT",
                "source_of_truth": "DURABLE_MATRIX_AND_LOCAL_RUN",
                "dispatch": "IDEMPOTENT_SINGLE_ACTIVE_RUN",
                "financial_truth": "DETERMINISTIC_SKILLS_ONLY",
                "human_authority": "PRESERVED",
            },
            "provider_tokens": 0,
            "admission_source": (
                "PROVIDER_TOKEN_GUARD"
                if admission_guard is not None
                else "RUNTIME_AND_SELECTED_RUN_ONLY"
            ),
            "token_guard": {
                "status": (admission_guard or {}).get("status"),
                "allowed": (admission_guard or {}).get("allowed"),
                "active_run_id": guarded_run_id,
                "active_run_state": guarded_run_state,
                "active_run_provider_tokens": (admission_guard or {}).get(
                    "active_run_provider_tokens"
                ),
                "reasons": list((admission_guard or {}).get("reasons") or []),
            },
            "generated_by_model": False,
        }
        core["snapshot_sha256"] = canonical_sha256(core)
        return core

    def reconcile(
        self,
        runtime: dict[str, Any],
        run: dict[str, Any] | None,
        admission_guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.inspect(runtime, run, admission_guard)
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.latest_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.latest_path)
        return snapshot
