from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

try:
    import v02_live_acceptance as acceptance
except ModuleNotFoundError:  # package import in ``python -m unittest`` mode
    from . import v02_live_acceptance as acceptance

try:
    from emergency_stop import progress_watchdog_decision
except ModuleNotFoundError:  # package import in ``python -m unittest`` mode
    from .emergency_stop import progress_watchdog_decision


FAILURE_STATES = {
    "BUDGET_EXCEEDED",
    "STOPPED_BY_GATE",
    "MODEL_CONTROL_CLEANUP_FAILED",
    "DISPATCH_FAILED",
    "FAILED_CLOSED",
    "CANCELLED_BY_SESSION_RESET",
}

ACTIVE_RUN_STATES = {
    "READY_FOR_AGENTTEAMS",
    "AGENTTEAMS_SUBMITTED",
    "SUBMITTED",
    "ACTIVE",
    "RUNNING",
}

# These are eventually-consistent Matrix/Trace projection gaps, not integrity
# violations.  The validator now reports them as PENDING for active Runs; this
# compatibility guard also prevents an older/stale status snapshot from
# terminating the observer before a later projection arrives.  Any observed
# hash, order, room, or actor mismatch remains a hard failure.
ASYNC_PROJECTION_GAP_FAILURES = {
    "MANAGER_AUTHORIZATION_TRACE_EVENT_MISSING",
    "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING",
    "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING",
}


def _count_progress(run: dict[str, Any], validation: dict[str, Any]) -> tuple[int, int]:
    artifacts = validation.get("worker_artifact_count")
    if artifacts is None:
        artifacts = len(
            ((run.get("agent_result") or {}).get("worker_artifacts") or {})
        )
    skills = validation.get("skill_invocation_count")
    if skills is None:
        skills = sum(
            len((artifact or {}).get("skill_invocations") or [])
            for artifact in (
                ((run.get("agent_result") or {}).get("worker_artifacts") or {})
            ).values()
            if isinstance(artifact, dict)
        )
    return max(0, int(artifacts or 0)), max(0, int(skills or 0))


def _emergency_stop_for_watchdog(
    base_url: str,
    run_id: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Persist a watchdog stop through the existing control API, with no model call."""

    response = acceptance.api_json(
        base_url,
        f"/api/v1/runs/{run_id}/emergency-stop",
        {
            "terminal_state": decision["terminal_state"],
            "actor": "system:v02-progress-watchdog",
            "reason": decision["reason"],
            "reason_codes": decision["reason_codes"],
        },
        timeout=60,
    )
    stopped_state = str((response.get("run") or {}).get("state") or "")
    if stopped_state not in {"STOPPED_BY_GATE", "BUDGET_EXCEEDED"}:
        raise RuntimeError(
            "progress watchdog emergency-stop did not return a terminal Run"
        )
    return response


def _requires_fail_closed(state: str, validation: dict[str, Any]) -> bool:
    if state in FAILURE_STATES:
        return True
    if validation.get("status") != "FAIL":
        return False
    failures = {
        str(item)
        for item in (validation.get("failures") or [])
        if str(item).strip()
    }
    return not (
        state in ACTIVE_RUN_STATES
        and bool(failures)
        and failures <= ASYNC_PROJECTION_GAP_FAILURES
    )


def _emit(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    phase: str,
    status: str,
    **detail: Any,
) -> dict[str, Any]:
    event = {
        "protocol": "FINFLUX_V02_ACCEPTANCE_PROGRESS_V1.0",
        "captured_at_utc": acceptance.utc_now(),
        "phase": phase,
        "status": status,
        **detail,
    }
    if callback is not None:
        callback(event)
    return event


def run_acceptance(
    *,
    base_url: str,
    session_file: Path,
    output_dir: Path,
    source_submission_id: str = acceptance.DEFAULT_SOURCE_SUBMISSION_ID,
    execute_model: bool = False,
    poll_interval_seconds: float = 5.0,
    run_timeout_seconds: int = 900,
    wait_for_human_seconds: int = 3600,
    no_artifact_progress_timeout_seconds: float = 180.0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute one V0.2 acceptance attempt and no more than one model Run.

    The coordinator deliberately cannot submit a Human decision.  It can wait
    for an authenticated operator to decide in the UI and then finalize the
    exact same Run into MD/PDF/JSON/audit ZIP.  Every phase is persisted so a
    timeout or provider failure remains inspectable rather than becoming an
    automatic retry.
    """

    if not execute_model:
        raise RuntimeError("one-click acceptance requires explicit execute_model=True")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if (
        run_timeout_seconds <= 0
        or wait_for_human_seconds < 0
        or no_artifact_progress_timeout_seconds <= 0
    ):
        raise ValueError("acceptance timeouts are invalid")

    output_dir.mkdir(parents=True, exist_ok=True)
    progress: list[dict[str, Any]] = []

    def record(*, phase: str, status: str, **detail: Any) -> dict[str, Any]:
        event = _emit(
            progress_callback,
            phase=phase,
            status=status,
            **detail,
        )
        progress.append(event)
        acceptance.atomic_json(output_dir / "progress.json", {"events": progress})
        return event

    check = acceptance.preflight(base_url, source_submission_id)
    acceptance.atomic_json(output_dir / "preflight.json", check)
    if check.get("status") != "READY":
        record(phase="PREFLIGHT", status="BLOCKED", reasons=check.get("reasons") or [])
        return {
            "status": "BLOCKED",
            "phase": "PREFLIGHT",
            "preflight": check,
            "model_runs_created": 0,
            "human_decision_automated": False,
        }
    record(phase="PREFLIGHT", status="PASS")

    launched = acceptance.launch(
        base_url,
        session_file,
        source_submission_id,
    )
    acceptance.atomic_json(output_dir / "launch.json", launched)
    run_id = str((launched.get("run") or {}).get("run_id") or "")
    if not run_id:
        raise RuntimeError("launch completed without a run_id")
    record(phase="MODEL_RUN", status="LAUNCHED", run_id=run_id)

    run_started_at = monotonic()
    run_deadline = run_started_at + run_timeout_seconds
    last_artifact_progress_at = run_started_at
    max_artifacts_seen = 0
    max_skills_seen = 0
    human_deadline: float | None = None
    status_index = 0
    while True:
        snapshot = acceptance.status(base_url, session_file)
        status_index += 1
        acceptance.atomic_json(
            output_dir / f"status-{status_index:04d}.json",
            snapshot,
        )
        run = snapshot.get("run") or {}
        state = str(run.get("state") or "UNKNOWN")
        validation = snapshot.get("validation") or {}
        artifacts, skills = _count_progress(run, validation)
        now = monotonic()
        if artifacts > max_artifacts_seen or skills > max_skills_seen:
            last_artifact_progress_at = now
            max_artifacts_seen = max(max_artifacts_seen, artifacts)
            max_skills_seen = max(max_skills_seen, skills)
        record(
            phase="RUN_POLL",
            status=state,
            run_id=run_id,
            validation_status=validation.get("status"),
            worker_artifact_count=validation.get("worker_artifact_count"),
            skill_invocation_count=validation.get("skill_invocation_count"),
            provider_tokens=validation.get("provider_tokens"),
        )

        if _requires_fail_closed(state, validation):
            result = {
                "status": "FAIL",
                "phase": "MODEL_RUN",
                "run_id": run_id,
                "run_state": state,
                "validation": validation,
                "model_runs_created": 1,
                "human_decision_automated": False,
            }
            acceptance.atomic_json(output_dir / "acceptance-failure.json", result)
            return result

        watchdog = progress_watchdog_decision(
            run,
            worker_artifact_count=artifacts,
            skill_invocation_count=skills,
            stalled_seconds=now - last_artifact_progress_at,
            no_artifact_progress_timeout_seconds=(
                no_artifact_progress_timeout_seconds
            ),
        )
        if watchdog:
            try:
                stop_response = _emergency_stop_for_watchdog(
                    base_url, run_id, watchdog
                )
            except Exception as exc:  # noqa: BLE001 - preserve a bounded diagnosis
                result = {
                    "status": "ERROR",
                    "phase": "PROGRESS_WATCHDOG_STOP_FAILED",
                    "run_id": run_id,
                    "run_state": state,
                    "diagnosis": watchdog,
                    "stop_failure_class": type(exc).__name__,
                    "model_runs_created": 1,
                    "human_decision_automated": False,
                    "watchdog_model_calls": 0,
                }
                acceptance.atomic_json(
                    output_dir / "watchdog-stop-failure.json", result
                )
                return result
            result = {
                "status": "FAIL",
                "phase": "PROGRESS_WATCHDOG",
                "run_id": run_id,
                "run_state": (stop_response.get("run") or {}).get("state"),
                "diagnosis": watchdog,
                "emergency_stop_record_sha256": (
                    stop_response.get("emergency_stop_record") or {}
                ).get("record_sha256"),
                "model_runs_created": 1,
                "human_decision_automated": False,
                "watchdog_model_calls": 0,
            }
            acceptance.atomic_json(output_dir / "watchdog-stop.json", result)
            record(
                phase="PROGRESS_WATCHDOG",
                status="STOPPED_BY_GATE",
                run_id=run_id,
                reason_codes=watchdog["reason_codes"],
            )
            return result

        if state == "AWAITING_HUMAN":
            if human_deadline is None:
                human_deadline = monotonic() + wait_for_human_seconds
                record(
                    phase="HUMAN_GATE",
                    status="AWAITING_AUTHENTICATED_HUMAN",
                    run_id=run_id,
                    ui=f"{base_url.rstrip('/')}/#/human-gate",
                )
            if wait_for_human_seconds == 0:
                return {
                    "status": "AWAITING_HUMAN",
                    "phase": "HUMAN_GATE",
                    "run_id": run_id,
                    "model_runs_created": 1,
                    "human_decision_automated": False,
                }
            if now >= human_deadline:
                result = {
                    "status": "TIMEOUT",
                    "phase": "HUMAN_GATE",
                    "run_id": run_id,
                    "model_runs_created": 1,
                    "human_decision_automated": False,
                }
                acceptance.atomic_json(output_dir / "human-timeout.json", result)
                return result
        elif state == "COMPLETED":
            record(phase="HUMAN_GATE", status="AUTHENTICATED_DECISION_OBSERVED", run_id=run_id)
            receipt = acceptance.finalize(
                base_url,
                session_file,
                output_dir / "final",
            )
            record(
                phase="EXPORT",
                status="PASS",
                run_id=run_id,
                acceptance_receipt_sha256=receipt.get("receipt_sha256"),
            )
            result = {
                "status": "PASS",
                "phase": "EXPORT",
                "run_id": run_id,
                "acceptance_receipt": receipt,
                "model_runs_created": 1,
                "human_decision_automated": False,
            }
            acceptance.atomic_json(output_dir / "one-click-result.json", result)
            return result
        elif now >= run_deadline:
            result = {
                "status": "TIMEOUT",
                "phase": "MODEL_RUN",
                "run_id": run_id,
                "run_state": state,
                "validation": validation,
                "model_runs_created": 1,
                "human_decision_automated": False,
            }
            acceptance.atomic_json(output_dir / "run-timeout.json", result)
            return result

        sleep(poll_interval_seconds)


def resume_acceptance(
    *,
    base_url: str,
    session_file: Path,
    output_dir: Path,
    poll_interval_seconds: float = 5.0,
    run_timeout_seconds: int = 900,
    wait_for_human_seconds: int = 3600,
    no_artifact_progress_timeout_seconds: float = 180.0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Resume observation/finalization of one previously reserved session.

    This path never calls preflight, evidence creation, launch, or Human POST;
    it cannot create a second Run after a terminal/CLI interruption.
    """

    if (
        poll_interval_seconds <= 0
        or run_timeout_seconds <= 0
        or wait_for_human_seconds < 0
        or no_artifact_progress_timeout_seconds <= 0
    ):
        raise ValueError("acceptance resume timeouts are invalid")
    session = acceptance.load_session(session_file, expected_base_url=base_url)
    run_id = str(session.get("run_id") or "")
    if not run_id:
        if session.get("status") != "RUN_CREATE_OUTCOME_UNKNOWN":
            raise RuntimeError(
                "reserved session has no run_id and is not safely reconcilable"
            )
        recovered = acceptance.reconcile_session_run_creation(
            base_url, session_file
        )
        session = recovered["session"]
        run_id = str(session.get("run_id") or "")
        if not run_id:
            raise RuntimeError(
                "Run creation remains uncommitted; refusing a second POST"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    progress: list[dict[str, Any]] = []

    def record(*, phase: str, status: str, **detail: Any) -> None:
        event = _emit(
            progress_callback,
            phase=phase,
            status=status,
            **detail,
        )
        progress.append(event)
        acceptance.atomic_json(output_dir / "resume-progress.json", {"events": progress})

    record(phase="RESUME", status="EXISTING_RUN_ONLY", run_id=run_id)
    run_started_at = monotonic()
    run_deadline = run_started_at + run_timeout_seconds
    last_artifact_progress_at = run_started_at
    max_artifacts_seen = 0
    max_skills_seen = 0
    human_deadline: float | None = None
    status_index = 0
    while True:
        snapshot = acceptance.status(base_url, session_file)
        status_index += 1
        acceptance.atomic_json(
            output_dir / f"resume-status-{status_index:04d}.json", snapshot
        )
        run = snapshot.get("run") or {}
        state = str(run.get("state") or "UNKNOWN")
        validation = snapshot.get("validation") or {}
        artifacts, skills = _count_progress(run, validation)
        now = monotonic()
        if artifacts > max_artifacts_seen or skills > max_skills_seen:
            last_artifact_progress_at = now
            max_artifacts_seen = max(max_artifacts_seen, artifacts)
            max_skills_seen = max(max_skills_seen, skills)
        record(
            phase="RESUME_POLL",
            status=state,
            run_id=run_id,
            validation_status=validation.get("status"),
        )
        if _requires_fail_closed(state, validation):
            return {
                "status": "FAIL",
                "phase": "RESUME",
                "run_id": run_id,
                "run_state": state,
                "validation": validation,
                "model_runs_created": 0,
                "human_decision_automated": False,
            }
        watchdog = progress_watchdog_decision(
            run,
            worker_artifact_count=artifacts,
            skill_invocation_count=skills,
            stalled_seconds=now - last_artifact_progress_at,
            no_artifact_progress_timeout_seconds=(
                no_artifact_progress_timeout_seconds
            ),
        )
        if watchdog:
            try:
                stop_response = _emergency_stop_for_watchdog(
                    base_url, run_id, watchdog
                )
            except Exception as exc:  # noqa: BLE001 - preserve a bounded diagnosis
                result = {
                    "status": "ERROR",
                    "phase": "PROGRESS_WATCHDOG_STOP_FAILED",
                    "run_id": run_id,
                    "run_state": state,
                    "diagnosis": watchdog,
                    "stop_failure_class": type(exc).__name__,
                    "model_runs_created": 0,
                    "human_decision_automated": False,
                    "watchdog_model_calls": 0,
                    "resumed_existing_session": True,
                }
                acceptance.atomic_json(
                    output_dir / "resume-watchdog-stop-failure.json", result
                )
                return result
            result = {
                "status": "FAIL",
                "phase": "PROGRESS_WATCHDOG",
                "run_id": run_id,
                "run_state": (stop_response.get("run") or {}).get("state"),
                "diagnosis": watchdog,
                "emergency_stop_record_sha256": (
                    stop_response.get("emergency_stop_record") or {}
                ).get("record_sha256"),
                "model_runs_created": 0,
                "human_decision_automated": False,
                "watchdog_model_calls": 0,
                "resumed_existing_session": True,
            }
            acceptance.atomic_json(output_dir / "resume-watchdog-stop.json", result)
            record(
                phase="PROGRESS_WATCHDOG",
                status="STOPPED_BY_GATE",
                run_id=run_id,
                reason_codes=watchdog["reason_codes"],
            )
            return result
        if state == "COMPLETED":
            receipt = acceptance.finalize(
                base_url, session_file, output_dir / "final"
            )
            return {
                "status": "PASS",
                "phase": "EXPORT",
                "run_id": run_id,
                "acceptance_receipt": receipt,
                "model_runs_created": 0,
                "human_decision_automated": False,
                "resumed_existing_session": True,
            }
        if state == "AWAITING_HUMAN":
            if human_deadline is None:
                human_deadline = monotonic() + wait_for_human_seconds
                record(
                    phase="HUMAN_GATE",
                    status="AWAITING_AUTHENTICATED_HUMAN",
                    run_id=run_id,
                    ui=f"{base_url.rstrip('/')}/#/human-gate",
                )
            if wait_for_human_seconds == 0:
                return {
                    "status": "AWAITING_HUMAN",
                    "phase": "HUMAN_GATE",
                    "run_id": run_id,
                    "model_runs_created": 0,
                    "human_decision_automated": False,
                    "resumed_existing_session": True,
                }
            if now >= human_deadline:
                return {
                    "status": "TIMEOUT",
                    "phase": "HUMAN_GATE",
                    "run_id": run_id,
                    "model_runs_created": 0,
                    "human_decision_automated": False,
                    "resumed_existing_session": True,
                }
        elif now >= run_deadline:
            return {
                "status": "TIMEOUT",
                "phase": "RESUME",
                "run_id": run_id,
                "run_state": state,
                "validation": validation,
                "model_runs_created": 0,
                "human_decision_automated": False,
                "resumed_existing_session": True,
            }
        sleep(poll_interval_seconds)
