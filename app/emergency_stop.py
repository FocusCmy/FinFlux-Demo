from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "FINFLUX_EMERGENCY_STOP_V1.0"
TERMINAL_STATES = {"STOPPED_BY_GATE", "BUDGET_EXCEEDED"}
OPEN_STATES = {
    "SUBMITTED",
    "RUNNING",
    "AWAITING_HUMAN",
    "AGENTTEAMS_SUBMITTED",
    "ACTIVE",
}


def progress_watchdog_decision(
    run: dict[str, Any],
    *,
    worker_artifact_count: int,
    skill_invocation_count: int,
    stalled_seconds: float,
    no_artifact_progress_timeout_seconds: float,
) -> dict[str, Any] | None:
    """Return a zero-model stop decision when an open Run cannot converge.

    Provider call counts are compared with the role-specific ``max_iters``
    read back before dispatch.  The Manager is excluded: its one allowed call
    is expected to precede every Worker artifact.  This function only inspects
    already-persisted facts and never calls Matrix, a provider, or a tool.
    """

    state = str(run.get("state") or "")
    if state not in OPEN_STATES:
        return None
    # The model workflow has converged once the Human Gate is open. Waiting
    # for an authenticated decision is not an artifact-progress stall.
    if state == "AWAITING_HUMAN":
        return None
    artifacts = max(0, int(worker_artifact_count or 0))
    skills = max(0, int(skill_invocation_count or 0))
    stalled = max(0.0, float(stalled_seconds or 0.0))
    timeout = float(no_artifact_progress_timeout_seconds or 0.0)
    if timeout <= 0:
        raise ValueError("no-artifact progress timeout must be positive")

    role_budgets: dict[str, int] = {}
    readiness = run.get("prompt_budget_readiness") or {}
    for row in readiness.get("runtime_readbacks") or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip()
        max_iters = int(row.get("max_iters", 0) or 0)
        if role and max_iters > 0:
            role_budgets[role] = max_iters

    exhausted: list[dict[str, Any]] = []
    provider = run.get("provider_usage") or {}
    for row in provider.get("by_agent") or []:
        if not isinstance(row, dict):
            continue
        agent_id = str(row.get("agent_id") or "").strip()
        provider_role = str(row.get("role") or "").strip()
        if agent_id in {"global-manager", "manager"} or provider_role == "manager":
            continue
        allowed_calls = role_budgets.get(agent_id) or role_budgets.get(provider_role)
        observed_calls = int(row.get("call_count", 0) or 0)
        if allowed_calls and observed_calls >= allowed_calls:
            exhausted.append(
                {
                    "agent_id": agent_id or provider_role,
                    "observed_model_calls": observed_calls,
                    "role_model_call_budget": allowed_calls,
                }
            )

    reason_codes: list[str] = []
    if artifacts == 0 and skills == 0 and exhausted:
        reason_codes.extend(
            "ROLE_MODEL_BUDGET_EXHAUSTED_WITHOUT_ARTIFACT:"
            f"{item['agent_id']}:{item['observed_model_calls']}/"
            f"{item['role_model_call_budget']}"
            for item in exhausted
        )
    if stalled >= timeout:
        reason_codes.append(
            "NO_ARTIFACT_PROGRESS_TIMEOUT:"
            f"{int(stalled)}>={int(timeout)}"
        )
    if not reason_codes:
        return None
    return {
        "protocol": "FINFLUX_PROGRESS_WATCHDOG_DECISION_V1.0",
        "terminal_state": "STOPPED_BY_GATE",
        "reason_codes": sorted(set(reason_codes)),
        "reason": ";".join(sorted(set(reason_codes))),
        "diagnostics": {
            "run_state": state,
            "worker_artifact_count": artifacts,
            "skill_invocation_count": skills,
            "stalled_seconds": stalled,
            "no_artifact_progress_timeout_seconds": timeout,
            "exhausted_roles": exhausted,
            "model_called_by_watchdog": False,
        },
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def validate_emergency_stop_record(record: dict[str, Any]) -> None:
    if record.get("protocol") != PROTOCOL:
        raise ValueError("invalid emergency-stop protocol")
    if record.get("terminal_state") not in TERMINAL_STATES:
        raise ValueError("invalid emergency-stop terminal state")
    record_type = str(record.get("record_type", "EMERGENCY_STOP"))
    if record_type not in {"EMERGENCY_STOP", "PROVIDER_USAGE_RECONCILIATION"}:
        raise ValueError("invalid emergency-stop record type")
    if not str(record.get("run_id", "")).startswith("RUN-"):
        raise ValueError("invalid emergency-stop run_id")
    if not str(record.get("actor", "")).strip():
        raise ValueError("emergency-stop actor is required")
    if not str(record.get("reason", "")).strip():
        raise ValueError("emergency-stop reason is required")
    if not _valid_sha256(record.get("token_guard_snapshot_sha256")):
        raise ValueError("invalid Token Guard snapshot digest")
    if not isinstance(record.get("container_action_evidence_refs"), list):
        raise ValueError("container action evidence refs must be a list")
    if not record["container_action_evidence_refs"]:
        raise ValueError("at least one container action evidence ref is required")
    for item in record["container_action_evidence_refs"]:
        if not isinstance(item, dict) or not str(item.get("evidence_type", "")).strip():
            raise ValueError("invalid container action evidence ref")
        if not _valid_sha256(item.get("evidence_sha256")):
            raise ValueError("container action evidence ref must be hash-bound")
        expected_ref = canonical_sha256(
            {key: value for key, value in item.items() if key != "evidence_sha256"}
        )
        if item.get("evidence_sha256") != expected_ref:
            raise ValueError("container action evidence ref digest mismatch")
    if record_type == "PROVIDER_USAGE_RECONCILIATION":
        if not _valid_sha256(record.get("supersedes_record_sha256")):
            raise ValueError("usage reconciliation must supersede a prior record")
        usage = record.get("provider_usage_reconciliation")
        if not isinstance(usage, dict):
            raise ValueError("provider usage reconciliation is required")
        if not _valid_sha256(record.get("provider_usage_reconciliation_sha256")):
            raise ValueError("provider usage reconciliation must be hash-bound")
        if record["provider_usage_reconciliation_sha256"] != canonical_sha256(usage):
            raise ValueError("provider usage reconciliation digest mismatch")
        baseline = int(usage.get("baseline_total_tokens", -1))
        ending = int(usage.get("end_total_tokens", -1))
        delta = int(usage.get("run_delta_tokens", -1))
        if min(baseline, ending, delta) < 0 or ending - baseline != delta:
            raise ValueError("provider usage reconciliation arithmetic mismatch")
    expected = canonical_sha256(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    if record.get("record_sha256") != expected:
        raise ValueError("emergency-stop record digest mismatch")


def running_guard_decision(
    run_id: str, run_state: str, guard: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a fail-closed decision for an already running Run.

    `ACTIVE_RUN_EXISTS:<this run>` is the expected admission view while a Run
    is open.  It must not stop the Run by itself.  Unreadable provider usage,
    an exhausted hard cap, or a contradictory durable active-Run set must.
    """
    if str(run_state) not in OPEN_STATES:
        return None

    reasons: list[str] = []
    terminal_state = "STOPPED_BY_GATE"
    captured = bool(guard.get("provider_usage_captured"))
    if not captured:
        reasons.append("PROVIDER_USAGE_UNAVAILABLE")

    daily = guard.get("daily") or {}
    hard_cap = daily.get("hard_cap")
    total = daily.get("total_tokens")
    if captured and hard_cap is None:
        reasons.append("DAILY_PROVIDER_TOKEN_CAP_NOT_CONFIGURED")
    elif captured and total is not None and int(total) >= int(hard_cap):
        terminal_state = "BUDGET_EXCEEDED"
        reasons.append(f"DAILY_PROVIDER_TOKEN_HARD_CAP_EXCEEDED:{total}>={hard_cap}")

    active_ids = [str(item) for item in (guard.get("active_run_ids") or [])]
    active_count = int(guard.get("active_run_count", len(active_ids)) or 0)
    if active_count != 1 or active_ids != [run_id]:
        reasons.append(
            "ACTIVE_RUN_STATE_INCONSISTENT:"
            f"expected={run_id},observed={','.join(active_ids) or 'NONE'}"
        )

    per_run_cap = guard.get("per_run_hard_cap")
    active_tokens = guard.get("active_run_provider_tokens")
    if (
        active_tokens is not None
        and per_run_cap is not None
        and int(active_tokens) >= int(per_run_cap)
    ):
        terminal_state = "BUDGET_EXCEEDED"
        reasons.append(
            f"ACTIVE_RUN_TOKEN_CAP_EXCEEDED:{active_tokens}>={per_run_cap}"
        )

    if not reasons:
        return None
    return {
        "terminal_state": terminal_state,
        "reason_codes": sorted(set(reasons)),
        "reason": ";".join(sorted(set(reasons))),
    }


class EmergencyStopLedger:
    """Content-addressed append-only control records, one hash chain per Run."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _run_root(self, run_id: str) -> Path:
        if not re.fullmatch(r"RUN-[0-9A-Za-z-]+", run_id):
            raise ValueError("invalid emergency-stop run_id")
        return self.root / run_id

    def records(self, run_id: str, *, verify: bool = True) -> list[dict[str, Any]]:
        run_root = self._run_root(run_id)
        if not run_root.is_dir():
            return []
        records = [
            json.loads(path.read_text(encoding="utf-8-sig"))
            for path in sorted(run_root.glob("*.json"))
        ]
        if verify:
            previous = None
            for index, record in enumerate(records, start=1):
                validate_emergency_stop_record(record)
                if int(record.get("sequence", 0) or 0) != index:
                    raise ValueError("emergency-stop sequence is not contiguous")
                if record.get("previous_record_sha256") != previous:
                    raise ValueError("emergency-stop hash chain is broken")
                previous = record["record_sha256"]
        return records

    def append(
        self,
        *,
        run_id: str,
        case_id: str,
        terminal_state: str,
        actor: str,
        reason: str,
        token_guard_snapshot: dict[str, Any],
        container_action_evidence_refs: list[dict[str, Any]],
        case_envelope_sha256: str | None = None,
        reason_codes: list[str] | None = None,
        actor_source: str = "CONTROL_PLANE_REQUEST",
        record_type: str = "EMERGENCY_STOP",
        usage_truth_status: str = "PROVISIONAL_AT_STOP",
        supersedes_record_sha256: str | None = None,
        provider_usage_reconciliation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if terminal_state not in TERMINAL_STATES:
            raise ValueError("unsupported emergency-stop terminal state")
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor or not reason:
            raise ValueError("emergency-stop actor and reason are required")
        refs = [dict(item) for item in container_action_evidence_refs]
        for item in refs:
            material = {key: value for key, value in item.items() if key != "evidence_sha256"}
            item.setdefault("evidence_sha256", canonical_sha256(material))
        guard = dict(token_guard_snapshot or {})
        guard_sha = canonical_sha256(guard)
        idempotency_key = canonical_sha256(
            {
                "run_id": run_id,
                "terminal_state": terminal_state,
                "actor": actor,
                "reason": reason,
                "token_guard_snapshot_sha256": guard_sha,
                "container_action_evidence_refs": refs,
                "record_type": record_type,
                "usage_truth_status": usage_truth_status,
                "supersedes_record_sha256": supersedes_record_sha256,
                "provider_usage_reconciliation": provider_usage_reconciliation,
            }
        )
        with self._lock:
            existing = self.records(run_id)
            if record_type == "PROVIDER_USAGE_RECONCILIATION":
                if not existing:
                    raise ValueError("usage reconciliation requires a prior stop record")
                expected_superseded = existing[-1]["record_sha256"]
                if supersedes_record_sha256 != expected_superseded:
                    raise ValueError(
                        "usage reconciliation must supersede the latest control record"
                    )
            for record in existing:
                if record.get("idempotency_key") == idempotency_key:
                    return record
            previous = existing[-1]["record_sha256"] if existing else None
            sequence = len(existing) + 1
            record = {
                "protocol": PROTOCOL,
                "record_type": record_type,
                "record_id": f"ESTOP-{uuid.uuid4().hex.upper()}",
                "sequence": sequence,
                "run_id": run_id,
                "case_id": str(case_id or ""),
                "case_envelope_sha256": case_envelope_sha256,
                "terminal_state": terminal_state,
                "actor": actor,
                "actor_source": actor_source,
                "reason": reason,
                "reason_codes": sorted(set(reason_codes or [])),
                "stopped_at_utc": utc_now(),
                "token_guard_snapshot": guard,
                "token_guard_snapshot_sha256": guard_sha,
                "container_action_evidence_refs": refs,
                "previous_record_sha256": previous,
                "idempotency_key": idempotency_key,
                "usage_truth_status": usage_truth_status,
                "usage_reconciliation_allowed": True,
                "supersedes_record_sha256": supersedes_record_sha256,
                "provider_usage_reconciliation": provider_usage_reconciliation,
                "provider_usage_reconciliation_sha256": (
                    canonical_sha256(provider_usage_reconciliation)
                    if isinstance(provider_usage_reconciliation, dict)
                    else None
                ),
                "human_decision_created": False,
                "datapass_created": False,
            }
            record["record_sha256"] = canonical_sha256(record)
            validate_emergency_stop_record(record)
            run_root = self._run_root(run_id)
            run_root.mkdir(parents=True, exist_ok=True)
            path = run_root / f"{sequence:06d}-{record['record_id']}.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                finally:
                    raise
            return record
