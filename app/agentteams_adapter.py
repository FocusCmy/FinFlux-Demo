"""Stable public facade for FinFlux's AgentTeams integration.

Open-source consumers should import only this module. Implementation details
live in ``agentteams_runtime`` and are intentionally not re-exported.
"""

from __future__ import annotations

from typing import Any

from agentteams_runtime import (
    AgentTeamsConfigurationError,
    AgentTeamsService,
    AgentTeamsUnavailable,
)


_SERVICE = AgentTeamsService()


def runtime_status() -> dict[str, Any]:
    return _SERVICE.runtime_status()


def provider_token_guard_snapshot(force: bool = False) -> dict[str, Any]:
    return _SERVICE.provider_guard(force)


def get_active_run() -> dict[str, Any] | None:
    return _SERVICE.active_run()


def get_persisted_run(run_id: str) -> dict[str, Any]:
    return _SERVICE.peek(run_id)


def submit_live_case(
    submission: dict[str, Any], live_run: dict[str, Any]
) -> dict[str, Any]:
    return _SERVICE.submit(submission, live_run)


def get_run(run_id: str) -> dict[str, Any]:
    return _SERVICE.get(run_id)


def request_same_run_repair(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    return _SERVICE.repair(run_id, requested_by, reason)


def supervisor_wake_manager(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    return _SERVICE.supervisor_wake_manager(run_id, requested_by, reason)


def supervisor_dispatch_missing_workers(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    return _SERVICE.supervisor_dispatch_missing_workers(
        run_id, requested_by, reason
    )


def supervisor_stop_wait(
    run_id: str,
    *,
    requested_by: str,
    reason: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    return _SERVICE.supervisor_wait(
        run_id, requested_by, reason, reason_codes
    )


def rearm_same_run_manager(run_id: str) -> dict[str, Any]:
    """Replay a failed first Manager authorization without creating a new Run."""

    return _SERVICE.rearm_manager(run_id)


def submit_human_decision(
    run_id: str, decision: str, reviewer: str, reason: str = ""
) -> dict[str, Any]:
    return _SERVICE.human_decision(run_id, decision, reviewer, reason)


def terminal_control_status(run_id: str) -> dict[str, Any]:
    return _SERVICE.terminal_status(run_id)


def record_emergency_stop(run_id: str, **fields: Any) -> dict[str, Any]:
    return _SERVICE.emergency_stop(run_id, **fields)


def recover_terminal_control_gap(run_id: str, **fields: Any) -> dict[str, Any]:
    return _SERVICE.recover_terminal(run_id, **fields)


def reset_agent_sessions() -> dict[str, Any]:
    return _SERVICE.reset_sessions()


__all__ = [
    "AgentTeamsConfigurationError",
    "AgentTeamsUnavailable",
    "get_active_run",
    "get_persisted_run",
    "get_run",
    "provider_token_guard_snapshot",
    "record_emergency_stop",
    "rearm_same_run_manager",
    "recover_terminal_control_gap",
    "request_same_run_repair",
    "reset_agent_sessions",
    "runtime_status",
    "submit_human_decision",
    "submit_live_case",
    "supervisor_dispatch_missing_workers",
    "supervisor_stop_wait",
    "supervisor_wake_manager",
    "terminal_control_status",
]
