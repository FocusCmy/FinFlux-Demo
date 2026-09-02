from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


PROTOCOL = "FINFLUX_RUN_LIFECYCLE_V1.0"

TERMINAL_PHASES = {"APPROVED", "BLOCKED", "RETURNED", "RELEASED"}

ALLOWED_TRANSITIONS = {
    "UPLOADED": {"PRECHECKED"},
    "PRECHECKED": {"ROUTED", "FAILED_CLOSED"},
    "ROUTED": {
        "READY_FOR_DISPATCH",
        "CODE_ONLY_PASS",
        "BLOCKED",
        "RETURNED",
        "FAILED_CLOSED",
    },
    "READY_FOR_DISPATCH": {"DISPATCHED", "FAILED_CLOSED"},
    "DISPATCHED": {
        "WORKERS_RUNNING",
        "DATAPASS_DRAFTED",
        "AWAITING_HUMAN",
        "FAILED_CLOSED",
    },
    "WORKERS_RUNNING": {
        "DATAPASS_DRAFTED",
        "AWAITING_HUMAN",
        "FAILED_CLOSED",
    },
    "DATAPASS_DRAFTED": {"AWAITING_HUMAN", "FAILED_CLOSED"},
    "AWAITING_HUMAN": {"APPROVED", "BLOCKED", "RETURNED", "FAILED_CLOSED"},
    "FAILED_CLOSED": {
        "READY_FOR_DISPATCH",
        "WORKERS_RUNNING",
        "DATAPASS_DRAFTED",
        "AWAITING_HUMAN",
    },
    # A later ChangeBundle may prove that an apparently safe single-record
    # mapping has a wider downstream blast radius.  That new evidence is
    # allowed to reopen the Run for a bounded Team review; no other terminal
    # phase can be reopened automatically.
    "CODE_ONLY_PASS": {"READY_FOR_DISPATCH", "RELEASED"},
    "APPROVED": {"RELEASED"},
    "BLOCKED": set(),
    "RETURNED": set(),
    "RELEASED": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def phase_for_state(raw_state: str, human_gate: dict[str, Any] | None = None) -> str:
    state = str(raw_state or "").upper()
    gate_state = str((human_gate or {}).get("state", "")).upper()
    if gate_state == "APPROVED":
        return "APPROVED"
    if gate_state == "REJECTED":
        return "BLOCKED"
    if gate_state == "RETURNED":
        return "RETURNED"
    mapping = {
        "ROUTING": "ROUTED",
        "READY_FOR_AGENTTEAMS": "READY_FOR_DISPATCH",
        "CODE_ONLY_PRECHECK": "CODE_ONLY_PASS",
        "REJECT_AT_INTAKE": "BLOCKED",
        "NEEDS_EVIDENCE": "RETURNED",
        "HOLD_FOR_BUDGET": "FAILED_CLOSED",
        "RUNTIME_UNAVAILABLE": "FAILED_CLOSED",
        "AGENTTEAMS_DISPATCH_FAILED": "FAILED_CLOSED",
        "AGENTTEAMS_SUBMITTED": "DISPATCHED",
        "SUBMITTED": "DISPATCHED",
        "ACTIVE": "WORKERS_RUNNING",
        "RUNNING": "WORKERS_RUNNING",
        "AWAITING_HUMAN": "AWAITING_HUMAN",
        "BUDGET_EXCEEDED": "FAILED_CLOSED",
        "STOPPED_BY_GATE": "FAILED_CLOSED",
        "CANCELLED_BY_SESSION_RESET": "FAILED_CLOSED",
        "FAILED": "FAILED_CLOSED",
        "COMPLETED": "DATAPASS_DRAFTED",
    }
    return mapping.get(state, "FAILED_CLOSED")


def _entry(
    run_id: str,
    from_phase: str | None,
    to_phase: str,
    raw_state: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    value = {
        "sequence": 0,
        "at_utc": utc_now(),
        "from_phase": from_phase,
        "to_phase": to_phase,
        "raw_state": raw_state,
        "actor": actor,
        "reason": reason,
    }
    value["transition_sha256"] = canonical_sha256(
        {"run_id": run_id, **value}
    )
    return value


def bootstrap_lifecycle(run: dict[str, Any], *, actor: str = "live-intake") -> None:
    if isinstance(run.get("lifecycle"), dict):
        return
    raw_state = str(run.get("state", "ROUTING"))
    target = phase_for_state(raw_state, run.get("human_gate"))
    phases = ["UPLOADED", "PRECHECKED", "ROUTED"]
    if target != "ROUTED":
        phases.append(target)
    history: list[dict[str, Any]] = []
    previous: str | None = None
    for phase in phases:
        item = _entry(
            str(run.get("run_id")),
            previous,
            phase,
            raw_state,
            actor,
            "initial evidence intake and deterministic route projection",
        )
        item["sequence"] = len(history) + 1
        item["transition_sha256"] = canonical_sha256(
            {"run_id": run.get("run_id"), **item}
        )
        history.append(item)
        previous = phase
    run["lifecycle"] = {
        "protocol": PROTOCOL,
        "current_phase": phases[-1],
        "terminal": phases[-1] in TERMINAL_PHASES,
        "history": history,
    }


def record_transition(
    run: dict[str, Any],
    raw_state: str,
    *,
    actor: str,
    reason: str,
    target_phase: str | None = None,
) -> dict[str, Any]:
    bootstrap_lifecycle(run)
    lifecycle = run["lifecycle"]
    current = str(lifecycle["current_phase"])
    target = target_phase or phase_for_state(raw_state, run.get("human_gate"))
    if target == current:
        lifecycle["last_raw_state"] = raw_state
        lifecycle["last_observed_at_utc"] = utc_now()
        return lifecycle
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(f"invalid Run lifecycle transition: {current} -> {target}")
    item = _entry(str(run.get("run_id")), current, target, raw_state, actor, reason)
    item["sequence"] = len(lifecycle["history"]) + 1
    item["transition_sha256"] = canonical_sha256(
        {"run_id": run.get("run_id"), **item}
    )
    lifecycle["history"].append(item)
    lifecycle["current_phase"] = target
    lifecycle["terminal"] = target in TERMINAL_PHASES
    lifecycle["last_raw_state"] = raw_state
    lifecycle["last_observed_at_utc"] = item["at_utc"]
    return lifecycle
