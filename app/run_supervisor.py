"""Background progression and bounded same-Run recovery for FinFlux.

The HTTP/UI layer is deliberately absent from this module.  A Run advances
because this supervisor observes the durable AgentTeams state, not because a
browser happens to refresh a page.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable


TERMINAL_STATES = {
    "AWAITING_HUMAN",
    "COMPLETED",
    "STOPPED_BY_GATE",
    "BUDGET_EXCEEDED",
    "FAILED_CLOSED",
    "CANCELLED_BY_SESSION_RESET",
    "MODEL_CONTROL_CLEANUP_FAILED",
}


def _positive_number(name: str, default: float, *, integer: bool = False) -> float | int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    value = max(1.0, value)
    return int(value) if integer else value


def _progress_signature(run: dict[str, Any]) -> str:
    result = run.get("agent_result") or {}
    artifacts = result.get("worker_artifacts") or {}
    usage = run.get("provider_usage") or {}
    ledger = usage.get("model_gateway_ledger") or {}
    payload = {
        "state": run.get("state"),
        "manager": (run.get("manager_dispatch_receipt") or {}).get("status"),
        "leader_relay": (run.get("leader_relay") or {}).get("status"),
        "leader_finalization": (run.get("leader_finalization") or {}).get("status"),
        "recommendation": result.get("leader_recommendation"),
        "artifacts": {
            role: item.get("artifact_sha256")
            for role, item in sorted(artifacts.items())
            if isinstance(item, dict)
        },
        "trace_events": len(run.get("trace") or []),
        "provider_calls": usage.get("call_count", ledger.get("provider_call_count")),
        "provider_tokens": usage.get("total_tokens", ledger.get("total_tokens")),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_age_seconds(run: dict[str, Any], wall_clock: Callable[[], float]) -> float:
    submitted_ms = run.get("submitted_at_ms")
    if submitted_ms is not None:
        try:
            return max(0.0, wall_clock() - float(submitted_ms) / 1000.0)
        except (TypeError, ValueError):
            pass
    submitted = str(run.get("submitted_at_utc") or "")
    if submitted:
        try:
            return max(0.0, wall_clock() - datetime.fromisoformat(submitted).timestamp())
        except ValueError:
            pass
    return 0.0


class RunSupervisor:
    """Advance one active AgentTeams Run and recover it within fixed bounds."""

    def __init__(
        self,
        *,
        repository: Any,
        get_active_run: Callable[[], dict[str, Any] | None],
        get_run: Callable[[str], dict[str, Any]],
        wake_manager: Callable[..., dict[str, Any]],
        dispatch_missing_workers: Callable[..., dict[str, Any]],
        stop_wait: Callable[..., dict[str, Any]],
        get_queued_run: Callable[[], dict[str, Any] | None] | None = None,
        dispatch_queued_run: Callable[[str], dict[str, Any]] | None = None,
        interval_seconds: float | None = None,
        manager_timeout_seconds: float | None = None,
        worker_timeout_seconds: float | None = None,
        max_runtime_seconds: float | None = None,
        max_recoveries: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.get_active_run = get_active_run
        self.get_run = get_run
        self.wake_manager = wake_manager
        self.dispatch_missing_workers = dispatch_missing_workers
        self.stop_wait = stop_wait
        self.get_queued_run = get_queued_run
        self.dispatch_queued_run = dispatch_queued_run
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else _positive_number("FINFLUX_SUPERVISOR_INTERVAL_SECONDS", 5)
        )
        self.manager_timeout_seconds = float(
            manager_timeout_seconds
            if manager_timeout_seconds is not None
            else _positive_number("FINFLUX_MANAGER_TIMEOUT_SECONDS", 60)
        )
        self.worker_timeout_seconds = float(
            worker_timeout_seconds
            if worker_timeout_seconds is not None
            else _positive_number("FINFLUX_WORKER_TIMEOUT_SECONDS", 90)
        )
        self.max_runtime_seconds = float(
            max_runtime_seconds
            if max_runtime_seconds is not None
            else _positive_number("FINFLUX_RUN_TIMEOUT_SECONDS", 600)
        )
        self.max_recoveries = int(
            max_recoveries
            if max_recoveries is not None
            else _positive_number("FINFLUX_MAX_SAME_RUN_RECOVERIES", 3, integer=True)
        )
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._observed: dict[str, dict[str, Any]] = {}
        self._status: dict[str, Any] = {
            "protocol": "FINFLUX_RUN_SUPERVISOR_V1",
            "state": "STOPPED",
            "run_id": None,
            "last_action": "NOT_STARTED",
            "last_error": None,
            "interval_seconds": self.interval_seconds,
            "manager_timeout_seconds": self.manager_timeout_seconds,
            "worker_timeout_seconds": self.worker_timeout_seconds,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_recoveries": self.max_recoveries,
            "browser_drives_run": False,
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._status.update({"state": "RUNNING", "last_action": "STARTED"})
            self._thread = threading.Thread(
                target=self._loop,
                name="finflux-run-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._status.update({"state": "STOPPED", "last_action": "STOPPED"})

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._status, ensure_ascii=False))

    def _set_status(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)
            self._status["last_tick_epoch_ms"] = int(self.wall_clock() * 1000)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - supervisor must survive a bad tick
                self._set_status(
                    state="DEGRADED",
                    last_action="TICK_FAILED",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            self._stop.wait(self.interval_seconds)

    def step(self) -> dict[str, Any]:
        active = self.get_active_run()
        if not active:
            queued = self.get_queued_run() if self.get_queued_run else None
            if queued and self.dispatch_queued_run:
                run_id = str(queued.get("run_id") or "")
                if not run_id:
                    raise ValueError("queued Live Run missing run_id")
                receipt = self.dispatch_queued_run(run_id)
                self._set_status(
                    state="RUNNING",
                    run_id=run_id,
                    run_state=str(receipt.get("run_state") or "QUEUED"),
                    recovery_count=int(receipt.get("attempt_count") or 0),
                    last_action=str(
                        receipt.get("status") or "BACKGROUND_DISPATCH_ATTEMPTED"
                    ),
                    wait_reason=receipt.get("reason"),
                    last_error=None,
                )
                return self.status()
            self._set_status(
                state="RUNNING",
                run_id=None,
                run_state="IDLE",
                last_action="NO_ACTIVE_RUN",
                last_error=None,
            )
            return self.status()

        run_id = str(active.get("run_id") or "")
        if not run_id:
            raise ValueError("active AgentTeams Run missing run_id")

        # A completed model workflow remains active while waiting for the
        # authenticated Human signature.  A temporary Runtime restart must
        # never reinterpret that durable state as an execution timeout.
        if str(active.get("state") or "") == "AWAITING_HUMAN":
            live_run = self.repository.sync_agentteams(run_id, active)
            if live_run.get("datapass") and not live_run.get("report_preview"):
                self.repository.ensure_report_preview(run_id)
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state="AWAITING_HUMAN",
                recovery_count=len(active.get("supervisor_recovery_attempts") or []),
                last_action="WAITING_FOR_HUMAN",
                last_error=None,
            )
            return self.status()

        # get_run performs the authoritative Matrix, Worker artifact and model
        # gateway ledger synchronization.  This call now belongs exclusively to
        # the background control plane rather than to an HTTP GET handler.
        agent_run = self.get_run(run_id)
        live_run = self.repository.sync_agentteams(run_id, agent_run)
        state = str(agent_run.get("state") or live_run.get("state") or "SUBMITTED")
        signature = _progress_signature(agent_run)
        now = self.monotonic()
        observed = self._observed.setdefault(
            run_id,
            {"signature": signature, "last_progress": now},
        )
        if observed["signature"] != signature:
            observed.update({"signature": signature, "last_progress": now})
        stale_seconds = max(0.0, now - float(observed["last_progress"]))
        recovery_count = len(agent_run.get("supervisor_recovery_attempts") or [])
        run_age = _run_age_seconds(agent_run, self.wall_clock)

        if state == "AWAITING_HUMAN":
            if live_run.get("datapass") and not live_run.get("report_preview"):
                self.repository.ensure_report_preview(run_id)
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state=state,
                recovery_count=recovery_count,
                last_action="WAITING_FOR_HUMAN",
                last_error=None,
            )
            return self.status()

        if state in TERMINAL_STATES:
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state=state,
                recovery_count=recovery_count,
                last_action="TERMINAL_RUN_OBSERVED",
                last_error=None,
            )
            return self.status()

        if run_age >= self.max_runtime_seconds or recovery_count >= self.max_recoveries:
            reason_code = (
                "SUPERVISOR_RUN_TIMEOUT"
                if run_age >= self.max_runtime_seconds
                else "SUPERVISOR_RECOVERY_LIMIT_REACHED"
            )
            reason = (
                f"Run未在{int(self.max_runtime_seconds)}秒内形成完整Worker产物与DataPass"
                if reason_code == "SUPERVISOR_RUN_TIMEOUT"
                else f"同Run恢复已达到{self.max_recoveries}次，仍有执行环节未完成"
            )
            stopped = self.stop_wait(
                run_id,
                requested_by="finflux-run-supervisor",
                reason=reason,
                reason_codes=[reason_code, "HUMAN_EVIDENCE_REQUIRED"],
            )
            self.repository.sync_agentteams(run_id, stopped)
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state="WAIT",
                recovery_count=recovery_count,
                last_action="FAILED_CLOSED_TO_WAIT",
                wait_reason=reason,
                last_error=None,
            )
            return self.status()

        manager_status = str(
            (agent_run.get("manager_dispatch_receipt") or {}).get("status") or ""
        )
        manager_authorized = manager_status == "MANAGER_AUTHORIZED_DISPATCHED" or str(
            (agent_run.get("leader_relay") or {}).get("status") or ""
        ) == "SENT"
        manager_wakeups = len(agent_run.get("manager_supervisor_wakeups") or [])
        if (
            not manager_authorized
            and manager_wakeups == 0
            and stale_seconds >= self.manager_timeout_seconds
        ):
            receipt = self.wake_manager(
                run_id,
                requested_by="finflux-run-supervisor",
                reason="Manager authorization timeout; one same-Run wakeup",
            )
            observed["last_progress"] = now
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state=state,
                recovery_count=recovery_count + 1,
                last_action=str(receipt.get("status") or "MANAGER_REAWAKENED"),
                last_error=None,
            )
            return self.status()

        result = agent_run.get("agent_result") or {}
        completed = int(result.get("workers_completed") or 0)
        required = int(result.get("workers_required") or 3)
        if (
            manager_authorized
            and completed < required
            and stale_seconds >= self.worker_timeout_seconds
        ):
            receipt = self.dispatch_missing_workers(
                run_id,
                requested_by="finflux-run-supervisor",
                reason=(
                    "Case Lead dispatch timeout"
                    if completed == 0
                    else f"Worker timeout; {completed}/{required} artifacts sealed"
                ),
            )
            observed["last_progress"] = now
            self._set_status(
                state="RUNNING",
                run_id=run_id,
                run_state=state,
                recovery_count=recovery_count + 1,
                last_action=str(receipt.get("status") or "MISSING_WORKERS_REAWAKENED"),
                missing_workers=receipt.get("missing_workers") or [],
                last_error=None,
            )
            return self.status()

        self._set_status(
            state="RUNNING",
            run_id=run_id,
            run_state=state,
            recovery_count=recovery_count,
            worker_progress={"completed": completed, "required": required},
            stale_seconds=round(stale_seconds, 1),
            run_age_seconds=round(run_age, 1),
            last_action="SYNCHRONIZED",
            last_error=None,
        )
        return self.status()
