from __future__ import annotations

import copy
import unittest

from prompt_budget_readiness import (
    PromptBudgetReadinessError,
    canonical_sha256,
    slim_tool_profile,
    validate_zero_model_readiness,
)
from task_identity import build_role_task_ids


RUN_ID = "RUN-LIVE-20260831154246-14a21e"
CASE_ID = "FUTURES-IF2608-20260814-SETTLEMENT"
WORKERS = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "independent-validator",
)


def _fixture() -> dict:
    identity = build_role_task_ids(CASE_ID, RUN_ID, WORKERS)
    rooms = []
    for role, room_id in (
        ("manager", "!manager14a21e:matrix.local"),
        ("finchange-case-lead", "!leader14a21e:matrix.local"),
    ):
        body = {
            "role": role,
            "room_id": room_id,
            "created_for_run_id": RUN_ID,
            "freshly_created": True,
            "prior_session_exists": False,
            "history_limit": 0,
        }
        rooms.append({**body, "receipt_sha256": canonical_sha256(body)})
    runtime = []
    actor_task_ids = {
        "manager": f"{identity['task_scope']}-manager-dispatch",
        "finchange-case-lead": f"{identity['task_scope']}-case-lead-aggregate",
        **identity["task_ids"],
    }
    for role in ("manager", "finchange-case-lead", *WORKERS):
        profile = slim_tool_profile(role)
        runtime.append(
            {
                "role": role,
                "history_limit": 0,
                "max_iters": (
                    1 if role == "manager" else 3
                    if role == "finchange-case-lead" else 2
                ),
                "max_input_length": 12000,
                "memory_prompt_enabled": False,
                "memory_summary_enabled": False,
                "force_memory_search": False,
                "context_manager_backend": "light",
                "memory_manager_backend": "none",
                "context_strategy": "native",
                "memory_tools_disabled": True,
                "prompt_visible_internal_tools": [],
                "system_prompt_files": (
                    ["FINFLUX_MANAGER_PROTOCOL.md"] if role == "manager" else
                    ["FINFLUX_CASE_LEAD_PROTOCOL.md"]
                    if role == "finchange-case-lead" else
                    ["FINFLUX_BOUNDED_WORKER_PROTOCOL.md"]
                ),
                "manager_protocol_prompt_sha256": (
                    "c" * 64 if role == "manager" else None
                ),
                "case_lead_protocol_prompt_sha256": (
                    "d" * 64 if role == "finchange-case-lead" else None
                ),
                "worker_protocol_prompt_sha256": (
                    "e" * 64
                    if role not in {"manager", "finchange-case-lead"}
                    else None
                ),
                "llm_retry_enabled": False,
                "enabled_tools": profile["allowed_tools"],
                "tool_profile_sha256": profile["profile_sha256"],
                "effective_config_sha256": "a" * 64,
                "model_gateway_headers_bound": True,
                "model_gateway_provider_id": "agentteams-gateway",
                "model_gateway_run_id": RUN_ID,
                "model_gateway_actor": role,
                "model_gateway_task_id": actor_task_ids[role],
                "model_gateway_identity_sha256": "b" * 64,
                "model_gateway_custom_header_names": [
                    "X-FinFlux-Actor",
                    "X-FinFlux-Identity",
                    "X-FinFlux-Run-ID",
                    "X-FinFlux-Task-ID",
                ],
            }
        )
    task_body = {
        "task_ids": identity["task_ids"],
        "preexisting_task_ids": [],
    }
    zero_body = {
        "provider_usage_captured_before": True,
        "provider_usage_captured_after": True,
        "provider_usage_date_before": "2026-08-31",
        "provider_usage_date_after": "2026-08-31",
        "provider_requests_before": 67,
        "provider_requests_after": 67,
        "provider_requests_delta": 0,
        "provider_tokens_before": 5_945_172,
        "provider_tokens_after": 5_945_172,
        "provider_tokens_delta": 0,
        "model_triggering_events_before": 0,
        "model_triggering_events_after": 0,
        "model_triggering_events_delta": 0,
    }
    return {
        "run_id": RUN_ID,
        "case_id": CASE_ID,
        "task_identity": identity,
        "rooms": rooms,
        "runtime_readbacks": runtime,
        "task_namespace_receipt": {
            **task_body,
            "receipt_sha256": canonical_sha256(task_body),
        },
        "zero_model_receipt": {
            **zero_body,
            "receipt_sha256": canonical_sha256(zero_body),
        },
    }


class PromptBudgetReadinessTests(unittest.TestCase):
    def test_accepts_nonzero_lifetime_usage_with_zero_preflight_delta(self) -> None:
        result = validate_zero_model_readiness(**_fixture())
        self.assertEqual(result["status"], "READY")
        self.assertEqual(
            result["zero_model_receipt"]["provider_tokens_before"],
            5_945_172,
        )

    def test_requires_global_manager_readback(self) -> None:
        data = _fixture()
        data["runtime_readbacks"] = [
            row for row in data["runtime_readbacks"] if row["role"] != "manager"
        ]
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_rejects_reused_room_or_session(self) -> None:
        data = _fixture()
        data["rooms"][0]["prior_session_exists"] = True
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_rejects_unbounded_context_or_extra_tool(self) -> None:
        data = _fixture()
        data["runtime_readbacks"][0]["max_input_length"] = 150000
        data["runtime_readbacks"][0]["enabled_tools"].append("web_search")
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_rejects_scroll_or_remelight_even_when_legacy_flags_are_false(self) -> None:
        data = _fixture()
        data["runtime_readbacks"][0]["context_strategy"] = "scroll"
        data["runtime_readbacks"][0]["memory_manager_backend"] = "remelight"
        data["runtime_readbacks"][0]["memory_tools_disabled"] = False
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_manager_must_be_single_turn_and_tool_free(self) -> None:
        data = _fixture()
        manager = next(
            row for row in data["runtime_readbacks"] if row["role"] == "manager"
        )
        manager["max_iters"] = 2
        manager["enabled_tools"] = ["message"]
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_rejects_real_provider_delta(self) -> None:
        data = _fixture()
        zero = copy.deepcopy(data["zero_model_receipt"])
        zero["provider_requests_after"] = 68
        zero["provider_requests_delta"] = 1
        unsigned = {key: value for key, value in zero.items() if key != "receipt_sha256"}
        zero["receipt_sha256"] = canonical_sha256(unsigned)
        data["zero_model_receipt"] = zero
        with self.assertRaises(PromptBudgetReadinessError):
            validate_zero_model_readiness(**data)

    def test_same_second_task_namespace_is_still_unique(self) -> None:
        data = _fixture()
        other = build_role_task_ids(
            CASE_ID,
            "RUN-LIVE-20260831154246-14a21f",
            WORKERS,
        )
        self.assertNotEqual(
            data["task_identity"]["task_scope"], other["task_scope"]
        )


if __name__ == "__main__":
    unittest.main()
