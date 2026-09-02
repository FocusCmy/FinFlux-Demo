from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


PROTOCOL = "FINFLUX_PROMPT_BUDGET_READINESS_V1"
# QwenPaw invokes tools from ``.qwenpaw/workspaces/default``.  Resolve the
# deployed package from that real model cwd instead of the container's image
# WORKDIR (the role root), which is not the cwd used by model tool calls.
WORKER_TOOL_GATEWAY_ENTRY = "../../agent-packages/current/tool_gateway.py"
_ROOM_ID = re.compile(r"^![^:\s]+:\S+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PromptBudgetReadinessError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def slim_tool_profile(role: str) -> dict[str, Any]:
    """Return the only prompt-visible tools allowed for a model actor.

    Domain calculations remain deterministic Skills behind ``tool_gateway``;
    they are not expanded into arbitrary model tools.  The Leader gets only
    task creation/collection and messaging, while a Worker gets one bounded
    shell entry plus task file synchronisation and completion signalling.
    """

    role = str(role or "").strip()
    if role == "manager":
        # The bounded Manager authorizes one immutable route by emitting a
        # protocol line on the Matrix channel.  It must not inspect files,
        # search memory, calculate finance, or invoke collaboration tools.
        tools = ()
    elif role == "finchange-case-lead":
        # The QwenPaw Matrix channel emits the Leader's response.  `message`
        # is not present in the deployed TeamHarness surface for this role and
        # must not be invented by the readiness receipt.
        tools = ("taskflow", "filesync")
    elif role:
        tools = ("execute_shell_command", "filesync", "artifact")
    else:
        raise PromptBudgetReadinessError("tool profile role is empty")
    body = {
        "protocol": "FINFLUX_SLIM_TOOL_PROFILE_V1",
        "role": role,
        "allowed_tools": list(tools),
        "tool_count": len(tools),
        "arbitrary_network_tool_allowed": False,
        "deterministic_financial_skill_entry": (
            f"{WORKER_TOOL_GATEWAY_ENTRY}:signed-worker"
        ),
    }
    return {**body, "profile_sha256": canonical_sha256(body)}


def validate_zero_model_readiness(
    *,
    run_id: str,
    case_id: str,
    task_identity: Mapping[str, Any],
    rooms: Iterable[Mapping[str, Any]],
    runtime_readbacks: Iterable[Mapping[str, Any]],
    task_namespace_receipt: Mapping[str, Any],
    zero_model_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless isolation and prompt limits are proven pre-dispatch.

    This function performs no I/O.  Callers must collect Matrix, filesystem,
    and QwenPaw readbacks first.  Therefore it is unit-testable and cannot
    accidentally trigger a provider request while validating the gate.
    """

    run_id = str(run_id or "")
    case_id = str(case_id or "")
    expected_task_ids = {
        str(role): str(task_id)
        for role, task_id in dict(task_identity.get("task_ids") or {}).items()
    }
    if not run_id or not case_id or not expected_task_ids:
        raise PromptBudgetReadinessError("run, case, and task identities are required")
    if task_identity.get("run_id") != run_id or task_identity.get("case_id") != case_id:
        raise PromptBudgetReadinessError("task identities are not bound to this Run")
    scope = str(task_identity.get("task_scope") or "")
    if len(set(expected_task_ids.values())) != len(expected_task_ids) or any(
        not task_id.startswith(scope + "-") for task_id in expected_task_ids.values()
    ):
        raise PromptBudgetReadinessError("task identities escape the canonical scope")

    room_rows = [dict(item) for item in rooms]
    if len(room_rows) < 2:
        raise PromptBudgetReadinessError("fresh Manager and Leader rooms are required")
    room_ids = [str(item.get("room_id") or "") for item in room_rows]
    if len(set(room_ids)) != len(room_ids) or any(not _ROOM_ID.fullmatch(item) for item in room_ids):
        raise PromptBudgetReadinessError("control rooms are missing or reused")
    for item in room_rows:
        if (
            item.get("created_for_run_id") != run_id
            or item.get("freshly_created") is not True
            or item.get("prior_session_exists") is not False
            or int(item.get("history_limit", -1)) != 0
        ):
            raise PromptBudgetReadinessError("control room/session isolation is not proven")

    runtime_rows = [dict(item) for item in runtime_readbacks]
    expected_runtime_roles = {"manager", "finchange-case-lead", *expected_task_ids}
    if {str(item.get("role") or "") for item in runtime_rows} != expected_runtime_roles:
        raise PromptBudgetReadinessError("runtime readback set does not match routed actors")
    for item in runtime_rows:
        role = str(item.get("role") or "")
        profile = slim_tool_profile(role)
        observed_tools = sorted(item.get("enabled_tools") or [])
        max_iters = int(item.get("max_iters", 0))
        manager_protocol_valid = role != "manager" or (
            max_iters == 1
            and observed_tools == []
            and item.get("system_prompt_files") == ["FINFLUX_MANAGER_PROTOCOL.md"]
            and _SHA256.fullmatch(
                str(item.get("manager_protocol_prompt_sha256") or "")
            )
            is not None
        )
        leader_protocol_valid = role != "finchange-case-lead" or (
            max_iters == 3
            and item.get("system_prompt_files")
            == ["FINFLUX_CASE_LEAD_PROTOCOL.md"]
            and _SHA256.fullmatch(
                str(item.get("case_lead_protocol_prompt_sha256") or "")
            )
            is not None
        )
        worker_protocol_valid = role in {"manager", "finchange-case-lead"} or (
            item.get("system_prompt_files")
            == ["FINFLUX_BOUNDED_WORKER_PROTOCOL.md"]
            and _SHA256.fullmatch(
                str(item.get("worker_protocol_prompt_sha256") or "")
            )
            is not None
        )
        actor_iteration_valid = (
            max_iters == 1
            if role == "manager"
            else max_iters == 3
            if role == "finchange-case-lead"
            else 1 <= max_iters <= 2
        )
        expected_gateway_headers = {
            "X-FinFlux-Actor",
            "X-FinFlux-Identity",
            "X-FinFlux-Run-ID",
            "X-FinFlux-Task-ID",
        }
        if (
            int(item.get("history_limit", -1)) != 0
            or not actor_iteration_valid
            or not 1000 <= int(item.get("max_input_length", 0)) <= 12000
            or item.get("memory_prompt_enabled") is not False
            or item.get("memory_summary_enabled") is not False
            or item.get("force_memory_search") is not False
            or item.get("context_manager_backend") != "light"
            or item.get("memory_manager_backend") != "none"
            or item.get("context_strategy") != "native"
            or item.get("memory_tools_disabled") is not True
            or item.get("prompt_visible_internal_tools") != []
            or item.get("llm_retry_enabled") is not False
            or observed_tools != sorted(profile["allowed_tools"])
            or not manager_protocol_valid
            or not leader_protocol_valid
            or not worker_protocol_valid
            or item.get("tool_profile_sha256") != profile["profile_sha256"]
            or item.get("model_gateway_headers_bound") is not True
            or item.get("model_gateway_run_id") != run_id
            or item.get("model_gateway_actor") != role
            or not str(item.get("model_gateway_task_id") or "").startswith(scope + "-")
            or not _SHA256.fullmatch(
                str(item.get("model_gateway_identity_sha256") or "")
            )
            or not expected_gateway_headers.issubset(
                set(item.get("model_gateway_custom_header_names") or [])
            )
            or not str(item.get("model_gateway_provider_id") or "").strip()
        ):
            raise PromptBudgetReadinessError(f"runtime prompt budget mismatch: {role}")
        digest = str(item.get("effective_config_sha256") or "")
        if not _SHA256.fullmatch(digest):
            raise PromptBudgetReadinessError(f"runtime readback is not hash-bound: {role}")

    observed_ids = {
        str(role): str(task_id)
        for role, task_id in dict(task_namespace_receipt.get("task_ids") or {}).items()
    }
    if observed_ids != expected_task_ids or task_namespace_receipt.get("preexisting_task_ids") != []:
        raise PromptBudgetReadinessError("canonical task namespace is not clean")
    if not _SHA256.fullmatch(str(task_namespace_receipt.get("receipt_sha256") or "")):
        raise PromptBudgetReadinessError("task namespace receipt is not hash-bound")

    # "Zero-model" is a zero delta, not a fabricated zero lifetime total.
    # Earlier Runs may already have consumed provider tokens.  Require two
    # complete, monotonic, same-day cumulative readbacks and prove that this
    # readiness operation added no request, token, or model-triggering event.
    before_calls = int(zero_model_receipt.get("provider_requests_before", -1))
    after_calls = int(zero_model_receipt.get("provider_requests_after", -1))
    before_tokens = int(zero_model_receipt.get("provider_tokens_before", -1))
    after_tokens = int(zero_model_receipt.get("provider_tokens_after", -1))
    before_events = int(zero_model_receipt.get("model_triggering_events_before", -1))
    after_events = int(zero_model_receipt.get("model_triggering_events_after", -1))
    if min(
        before_calls,
        after_calls,
        before_tokens,
        after_tokens,
        before_events,
        after_events,
    ) < 0:
        raise PromptBudgetReadinessError("zero-model readback is incomplete")
    if (
        zero_model_receipt.get("provider_usage_captured_before") is not True
        or zero_model_receipt.get("provider_usage_captured_after") is not True
        or zero_model_receipt.get("provider_usage_date_before")
        != zero_model_receipt.get("provider_usage_date_after")
        or after_calls - before_calls != 0
        or after_tokens - before_tokens != 0
        or after_events - before_events != 0
        or int(zero_model_receipt.get("provider_requests_delta", -1)) != 0
        or int(zero_model_receipt.get("provider_tokens_delta", -1)) != 0
        or int(zero_model_receipt.get("model_triggering_events_delta", -1)) != 0
    ):
        raise PromptBudgetReadinessError("readiness validation was not zero-model")
    if not _SHA256.fullmatch(str(zero_model_receipt.get("receipt_sha256") or "")):
        raise PromptBudgetReadinessError("zero-model receipt is not hash-bound")

    body = {
        "protocol": PROTOCOL,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evidence_mode": "ZERO_MODEL",
        "status": "READY",
        "run_id": run_id,
        "case_id": case_id,
        "task_identity": dict(task_identity),
        "rooms": room_rows,
        "runtime_readbacks": runtime_rows,
        "task_namespace_receipt": dict(task_namespace_receipt),
        "zero_model_receipt": dict(zero_model_receipt),
        "gate_results": {
            "fresh_control_rooms": "PASS",
            "session_absence": "PASS",
            "history_memory_retry_limits": "PASS",
            "slim_tool_profiles": "PASS",
            "canonical_task_namespace": "PASS",
            "zero_model_validation": "PASS",
        },
    }
    return {**body, "readiness_sha256": canonical_sha256(body)}
