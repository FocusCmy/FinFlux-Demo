from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

try:
    import v02_live_acceptance as acceptance
    from protocol_v02 import (
        build_case_envelope,
        build_datapass_draft,
        canonical_sha256 as protocol_sha256,
        resolve_profile,
    )
    from task_identity import build_role_task_ids
except ModuleNotFoundError:
    from app import v02_live_acceptance as acceptance
    from app.protocol_v02 import (
        build_case_envelope,
        build_datapass_draft,
        canonical_sha256 as protocol_sha256,
        resolve_profile,
    )
    from app.task_identity import build_role_task_ids


RUN_ID = "RUN-LIVE-20260831000000-a0b1c2"
NOW = "2026-08-31T00:00:00+00:00"


def h(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def bind_run_creation(run: dict, payload: dict) -> dict:
    binding = acceptance._run_create_binding(
        payload["submission_id"], payload["client_idempotency_key"]
    )
    receipt = {
        "protocol": "FINFLUX_RUN_CREATE_IDEMPOTENCY_V1.0",
        "client_idempotency_key_sha256": binding[
            "client_idempotency_key_sha256"
        ],
        "request_sha256": binding["run_create_request_sha256"],
        "attempt_sha256": "a" * 64,
    }
    receipt["receipt_sha256"] = acceptance.canonical_sha256(receipt)
    run["submission_id"] = payload["submission_id"]
    run["run_creation_idempotency"] = receipt
    return run


def formal_envelope() -> dict:
    profile = resolve_profile("futures_settlement")
    purpose = profile["declared_purposes"][0]
    evidence_sha = protocol_sha256({"source": "real-public-futures"})
    return build_case_envelope(
        profile_id="futures_settlement",
        case_id="CASE-FUTURES-V02-AUDIT-001",
        run_id=RUN_ID,
        purpose_id=purpose["purpose_id"],
        purpose_statement=purpose["target_decision"],
        evidence_handles=[
            {
                "evidence_id": "EVID-FUTURES-V02-001",
                "evidence_type": profile["evidence_requirements"][0]["evidence_type"],
                "content_sha256": evidence_sha,
                "source_locator": "immutable://audit/futures/v02/001",
                "media_type": "text/csv",
                "rights_state": "PUBLIC",
                "version_id": f"sha256:{evidence_sha[:16]}",
            }
        ],
        trigger="V02_LIVE_ACCEPTANCE",
        expected_route="FULL_TEAM_REVIEW",
        execution_policy_id="FINFLUX-BOUNDED-EXECUTION-V0.1",
        created_at_utc=NOW,
    )


def build_run(*, final: bool = False) -> dict:
    envelope = formal_envelope()
    task_identity = build_role_task_ids(
        envelope["case_id"], RUN_ID, sorted(acceptance.REQUIRED_WORKERS)
    )
    worker_skills = {
        "evidence-investigator": ["evidence-integrity", "rights-gate"],
        "semantic-impact-analyst": [
            "semantic-contract-resolver",
            "financial-impact-calculator",
        ],
        "independent-validator": ["independent-evidence-validator"],
    }
    artifacts = {}
    datapass_invocations = []
    for worker_id, skills in worker_skills.items():
        raw_receipts = []
        for skill_id in skills:
            item = {
                "skill_id": skill_id,
                "version": acceptance.REQUIRED_SKILLS[skill_id],
                "input_sha256": h({"in": skill_id}),
                "output_sha256": h({"out": skill_id}),
                "status": "SUCCESS",
                "discovered_at_runtime": True,
            }
            raw_receipts.append(item)
            datapass_invocations.append(
                {
                    "skill_id": skill_id,
                    "version": item["version"],
                    "worker_id": worker_id,
                    "input_sha256": item["input_sha256"],
                    "output_sha256": item["output_sha256"],
                    "status": "SUCCEEDED",
                }
            )
        artifact = {
            "role": worker_id,
            "run_id": RUN_ID,
            "task_id": task_identity["task_ids"][worker_id],
            "status": "SUCCESS",
            "skill_invocations": raw_receipts,
        }
        artifact["artifact_sha256"] = protocol_sha256(artifact)
        artifacts[worker_id] = artifact
    worker_receipts = [
        {
            "worker_id": worker_id,
            "status": "SEALED",
            "artifact_sha256": artifacts[worker_id]["artifact_sha256"],
        }
        for worker_id in sorted(acceptance.REQUIRED_WORKERS)
    ]
    datapass = build_datapass_draft(
        envelope=envelope,
        machine_recommendation="PASS",
        reason_codes=["CONTRACT_AND_EVIDENCE_VERIFIED"],
        recommendation_summary="证据、金融语义和确定性影响均已核验，等待负责人签署。",
        evidence_status="VERIFIED",
        evidence_quorum_met=True,
        semantic_status="RESOLVED",
        impact_status="COMPUTED",
        impact_facts={"residual": 0},
        impact_metrics=[
            {
                "metric_id": "residual",
                "label": "剩余差异",
                "value": 0,
                "unit": "",
                "source_kind": "DETERMINISTIC",
            }
        ],
        required_worker_ids=sorted(acceptance.REQUIRED_WORKERS),
        worker_receipts=worker_receipts,
        required_skill_ids=list(acceptance.REQUIRED_SKILLS),
        skill_invocations=[
            next(item for item in datapass_invocations if item["skill_id"] == skill_id)
            for skill_id in acceptance.REQUIRED_SKILLS
        ],
        generated_at_utc=NOW,
    )
    run = {
        "protocol": "FINFLUX_LIVE_RUN_V0.2",
        "run_id": RUN_ID,
        "case_id": envelope["case_id"],
        "submission_id": "SUB-NEW-V02",
        "state": "AWAITING_HUMAN",
        "case_envelope": envelope,
        "root_route_decision": {
            "route": "FULL_TEAM_REVIEW",
            "worker_plan": {"workers": sorted(acceptance.REQUIRED_WORKERS)},
            "required_skill_versions": acceptance.REQUIRED_SKILLS,
        },
        "matrix_case_envelope_handle": {"task_identity": task_identity},
        "agentteams_run_id": RUN_ID,
        "agent_result": {
            "worker_artifacts": artifacts,
            "leader_datapass_event_id": "$leader-event",
        },
        "datapass": datapass,
        "human_gate": {"state": "AWAITING_HUMAN"},
        "lifecycle": {"current_phase": "DATAPASS_DRAFTED"},
    }
    room_rows = []
    for role, room_id in (
        ("manager", "!managera0b1c2:matrix.local"),
        ("finchange-case-lead", "!leadera0b1c2:matrix.local"),
    ):
        actor_id = f"@{role}:matrix.local"
        invited_actor_ids = [actor_id]
        if role == "finchange-case-lead":
            invited_actor_ids.extend(
                f"@{worker}:matrix.local"
                for worker in sorted(acceptance.REQUIRED_WORKERS)
            )
        room = {
            "protocol": "FINFLUX_FRESH_CONTROL_ROOM_V1",
            "role": role,
            "actor_id": actor_id,
            "invited_actor_ids": invited_actor_ids,
            "room_id": room_id,
            "session_id": f"matrix:{room_id}",
            "created_for_run_id": RUN_ID,
            "freshly_created": True,
            "prior_session_exists": False,
            "history_limit": 0,
            "model_triggering_events_before_ready": 0,
        }
        if role == "finchange-case-lead":
            membership = {
                "protocol": "FINFLUX_MATRIX_JOINED_MEMBERSHIP_RECEIPT_V1",
                "status": "JOINED",
                "run_id": RUN_ID,
                "room_id": room_id,
                "expected_joined_actor_ids": invited_actor_ids,
                "observed_joined_actor_ids": invited_actor_ids,
                "missing_actor_ids": [],
                "attempts": 1,
                "membership_source": "MATRIX_JOIN_STATE",
                "checked_at_utc": NOW,
                "model_called": False,
                "provider_tokens": 0,
            }
            membership["receipt_sha256"] = acceptance.canonical_sha256(membership)
            room["membership_receipt"] = membership
        room["receipt_sha256"] = acceptance.canonical_sha256(room)
        room_rows.append(room)
    actor_task_ids = {
        "manager": f"{task_identity['task_scope']}-manager-dispatch",
        "finchange-case-lead": f"{task_identity['task_scope']}-case-lead-aggregate",
        **task_identity["task_ids"],
    }
    actor_identity_sha256 = {
        role: h("model-identity:" + role) for role in actor_task_ids
    }
    readbacks = []
    for role in ("manager", "finchange-case-lead", *sorted(acceptance.REQUIRED_WORKERS)):
        tools = [] if role == "manager" else ["filesync"]
        readbacks.append(
            {
                "role": role,
                "history_limit": 0,
                "max_iters": (
                    1
                    if role == "manager"
                    else 3
                    if role == "finchange-case-lead"
                    else 2
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
                    ["FINFLUX_MANAGER_PROTOCOL.md"]
                    if role == "manager"
                    else ["FINFLUX_CASE_LEAD_PROTOCOL.md"]
                    if role == "finchange-case-lead"
                    else ["FINFLUX_BOUNDED_WORKER_PROTOCOL.md"]
                ),
                "manager_protocol_prompt_sha256": (
                    h("finflux-manager-protocol") if role == "manager" else None
                ),
                "case_lead_protocol_prompt_sha256": (
                    h("finflux-case-lead-protocol")
                    if role == "finchange-case-lead"
                    else None
                ),
                "worker_protocol_prompt_sha256": (
                    h("finflux-bounded-worker-protocol")
                    if role not in {"manager", "finchange-case-lead"}
                    else None
                ),
                "llm_retry_enabled": False,
                "enabled_tools": tools,
                "tool_profile_sha256": h({"role": role, "tools": tools}),
                "effective_config_sha256": h({"role": role, "effective": True}),
                "model_gateway_headers_bound": True,
                "model_gateway_provider_id": "agentteams-gateway",
                "model_gateway_run_id": RUN_ID,
                "model_gateway_actor": role,
                "model_gateway_task_id": actor_task_ids[role],
                "model_gateway_identity_sha256": actor_identity_sha256[role],
                "model_gateway_custom_header_names": [
                    "X-FinFlux-Actor",
                    "X-FinFlux-Identity",
                    "X-FinFlux-Run-ID",
                    "X-FinFlux-Task-ID",
                ],
            }
        )
    namespace = {
        "protocol": "FINFLUX_TASK_NAMESPACE_ABSENCE_V1",
        "task_scope": task_identity["task_scope"],
        "task_ids": task_identity["task_ids"],
        "preexisting_task_ids": [],
    }
    namespace["receipt_sha256"] = acceptance.canonical_sha256(namespace)
    zero = {
        "protocol": "FINFLUX_ZERO_MODEL_PREFLIGHT_RECEIPT_V1",
        "provider_usage_captured_before": True,
        "provider_usage_captured_after": True,
        "provider_usage_date_before": "2026-08-31",
        "provider_usage_date_after": "2026-08-31",
        "provider_requests_before": 9,
        "provider_requests_after": 9,
        "provider_requests_delta": 0,
        "provider_tokens_before": 123,
        "provider_tokens_after": 123,
        "provider_tokens_delta": 0,
        "model_triggering_events_before": 0,
        "model_triggering_events_after": 0,
        "model_triggering_events_delta": 0,
    }
    zero["receipt_sha256"] = acceptance.canonical_sha256(zero)
    readiness = {
        "protocol": acceptance.PROMPT_BUDGET_PROTOCOL,
        "generated_at_utc": NOW,
        "evidence_mode": "ZERO_MODEL",
        "status": "READY",
        "run_id": RUN_ID,
        "case_id": envelope["case_id"],
        "task_identity": task_identity,
        "rooms": room_rows,
        "runtime_readbacks": readbacks,
        "task_namespace_receipt": namespace,
        "zero_model_receipt": zero,
        "gate_results": {"all": "PASS"},
    }
    readiness["readiness_sha256"] = acceptance.canonical_sha256(readiness)
    run["prompt_budget_readiness"] = readiness
    run["session_hygiene"] = {
        "protocol": "FINFLUX_FRESH_RUN_ISOLATION_V1",
        "status": "FRESH_ROOMS_AND_SESSIONS_VERIFIED",
        "model_called": False,
        "provider_tokens": 0,
        "legacy_clear_command_used": False,
        "manager_room": room_rows[0],
        "leader_room": room_rows[1],
    }
    manager = {
        "protocol": acceptance.MANAGER_DISPATCH_PROTOCOL,
        "case_id": envelope["case_id"],
        "run_id": RUN_ID,
        "dispatch_idempotency_key": h("dispatch-key"),
        "dispatch_block_sha256": h("dispatch-block"),
        "selected_workers": sorted(acceptance.REQUIRED_WORKERS),
        "status": "MANAGER_AUTHORIZED_DISPATCHED",
        "authorization_event_id": "$manager-authorized",
        "authorized_by": "@manager:matrix.local",
        "authorized_at_utc": "2026-08-31T00:00:01+00:00",
        "leader_room_event_id": "$leader-relay",
        "task_identity_sha256": acceptance.canonical_sha256(task_identity),
        "fallback": False,
    }
    manager["receipt_sha256"] = acceptance.canonical_sha256(manager)
    run["manager_dispatch_receipt"] = manager
    run["manager_dispatch_mode"] = "REAL_MANAGER"
    relay_body = (
        f"{room_rows[1]['actor_id']}\n"
        f"FINFLUX_LIVE_RELAY {envelope['case_id']} {RUN_ID}\n"
        "CASE_ENVELOPE_HANDLE:{\"handle_sha256\":\""
        + h("handle")
        + "\"}"
    )
    leader_relay = {
        "protocol": "FINFLUX_AUTHORIZED_LEADER_RELAY_V1",
        "status": "RELAY_SENT",
        "authorization_event_id": "$manager-authorized",
        "event_id": "$leader-relay",
        "room_id": room_rows[1]["room_id"],
        "leader_id": room_rows[1]["actor_id"],
        "transaction_id": "finflux_authorized_relay",
        "message_sha256": acceptance.canonical_sha256(relay_body),
        "prepared_at_utc": "2026-08-31T00:00:01+00:00",
        "sent_at_utc": "2026-08-31T00:00:02+00:00",
        "fallback": False,
    }
    leader_relay["receipt_sha256"] = acceptance.canonical_sha256(leader_relay)
    run["leader_relay"] = leader_relay
    run["agentteams_trace"] = [
        {
            "event_id": "$manager-authorized",
            "room_id": room_rows[0]["room_id"],
            "timestamp_utc": "2026-08-31T00:00:01+00:00",
            "actor": {
                "role": "manager",
                "sender": "@manager:matrix.local",
            },
            "body": (
                "FINFLUX_MANAGER_DISPATCHED "
                f"{envelope['case_id']} {RUN_ID} "
                f"{manager['dispatch_idempotency_key']} "
                f"{manager['dispatch_block_sha256']} "
                f"{room_rows[1]['actor_id']}"
            ),
        },
        {
            "event_id": "$leader-relay",
            "room_id": room_rows[1]["room_id"],
            "timestamp_utc": "2026-08-31T00:00:02+00:00",
            "actor": {
                "role": "system_relay",
                "sender": "@admin:matrix.local",
            },
            "body": relay_body,
        },
        *[
            {
                "event_id": f"$worker-{index}",
                "room_id": room_rows[1]["room_id"],
                "timestamp_utc": f"2026-08-31T00:00:0{index + 2}+00:00",
                "actor": {
                    "role": worker_id,
                    "sender": f"@{worker_id}:matrix.local",
                },
                "body": f"SEALED {task_identity['task_ids'][worker_id]}",
            }
            for index, worker_id in enumerate(
                sorted(acceptance.REQUIRED_WORKERS), start=1
            )
        ],
        {
            "event_id": "$leader-event",
            "room_id": room_rows[1]["room_id"],
            "timestamp_utc": "2026-08-31T00:00:06+00:00",
            "actor": {
                "role": "team_leader",
                "sender": "@finchange-case-lead:matrix.local",
            },
            "body": f"DATAPASS_DRAFT | {RUN_ID} | PASS",
        },
    ]
    convergence = {
        "protocol": acceptance.TASK_CONVERGENCE_PROTOCOL,
        "run_id": RUN_ID,
        "case_id": envelope["case_id"],
        "task_scope": task_identity["task_scope"],
        "task_identity_sha256": acceptance.canonical_sha256(task_identity),
        "status": "CONVERGED",
        "expected": task_identity["task_ids"],
        "observed": {
            role: {
                "task_id": artifacts[role]["task_id"],
                "artifact_sha256": artifacts[role]["artifact_sha256"],
            }
            for role in sorted(acceptance.REQUIRED_WORKERS)
        },
        "missing": [],
        "unexpected": [],
        "duplicate": [],
        "actual_task_directories": sorted(task_identity["task_ids"].values()),
    }
    convergence["receipt_sha256"] = acceptance.canonical_sha256(convergence)
    run["task_convergence_receipt"] = convergence
    baseline = {
        "date_utc": "2026-08-31",
        "captured_at_utc": NOW,
        "source": "QWENPAW_TOKEN_USAGE_JSON",
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "call_count": 1,
        "by_agent": [],
    }
    run["provider_usage_baseline"] = baseline
    baseline_record = {
        "protocol": "FINFLUX_PROVIDER_USAGE_BASELINE_V1",
        "run_id": RUN_ID,
        "captured_before_model_dispatch": True,
        "snapshot": baseline,
        "cumulative_totals": {
            key: baseline[key]
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "call_count",
            )
        },
    }
    baseline_record["baseline_sha256"] = acceptance.canonical_sha256(baseline_record)
    binding = {
        "protocol": acceptance.GATEWAY_BINDING_PROTOCOL,
        "run_id": RUN_ID,
        "state": "CLOSED",
        "terminal_state": "AWAITING_HUMAN",
        "provider_usage_baseline_sha256": baseline_record["baseline_sha256"],
        "actor_identity_sha256": actor_identity_sha256,
        "actor_task_ids": actor_task_ids,
    }
    binding["binding_sha256"] = acceptance.canonical_sha256(binding)
    run["model_gateway_binding"] = binding
    actor_binding = {
        "protocol": "FINFLUX_MODEL_ACTOR_BINDING_RECEIPT_V1",
        "status": "BOUND",
        "run_id": RUN_ID,
        "actors": sorted(actor_task_ids),
        "actor_task_ids": actor_task_ids,
        "actor_identity_sha256": actor_identity_sha256,
        "runtime_readiness_sha256": readiness["readiness_sha256"],
        "gateway_binding_sha256": binding["binding_sha256"],
        "plaintext_identity_persisted_in_run": False,
    }
    actor_binding["receipt_sha256"] = acceptance.canonical_sha256(actor_binding)
    run["model_gateway_actor_binding_receipt"] = actor_binding
    identity_cleanup = {
        "protocol": "FINFLUX_MODEL_ACTOR_IDENTITY_CLEANUP_V1",
        "status": "CLEARED",
        "run_id": RUN_ID,
        "gateway_binding_sha256": binding["binding_sha256"],
        "actor_task_ids": actor_task_ids,
        "roles": [
            {
                "role": role,
                "provider_id": "agentteams-gateway",
                "finflux_headers_remaining": 0,
                "remaining_custom_header_names": [],
            }
            for role in sorted(actor_task_ids)
        ],
        "finflux_headers_remaining": 0,
        "model_or_provider_called": False,
    }
    identity_cleanup["receipt_sha256"] = acceptance.canonical_sha256(
        identity_cleanup
    )
    run["model_gateway_identity_cleanup"] = identity_cleanup
    run["model_execution_seal_status"] = "SEALED"
    ledger = {
        "protocol": acceptance.GATEWAY_LEDGER_PROTOCOL,
        "run_id": RUN_ID,
        "status": "ACTIVE",
        "request_attempt_count": 1,
        "provider_call_count": 1,
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
        "in_flight_reserved_tokens": 0,
        "reservations": {},
        "records": [],
        "updated_at_utc": NOW,
    }
    ledger["ledger_sha256"] = acceptance.canonical_sha256(ledger)
    run["provider_usage"] = {
        "protocol": "FINFLUX_PROVIDER_USAGE_LEDGER_V0.2",
        "status": "PROVIDER_REPORTED",
        "attribution_status": "DAILY_DELTA_EXCLUSIVE_ACTIVE_RUN",
        "source": "QWENPAW_TOKEN_USAGE_JSON_DELTA",
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
        "call_count": 1,
        "by_agent": [],
        "by_model": [],
        "baseline_snapshot_sha256": h("baseline"),
        "current_snapshot_sha256": h("current"),
        "attribution_sha256": h("attribution"),
        "model_gateway_ledger": ledger,
    }
    if final:
        human_time = "2026-08-31T00:00:07+00:00"
        decision = {
            "reviewer": "@reviewer:matrix.local",
            "matrix_user_id": "@reviewer:matrix.local",
            "authenticated_principal": "@reviewer:matrix.local",
            "client_claim_ignored": None,
            "decision": "APPROVE_PASS",
            "reason": "evidence accepted",
            "datapass_sha256": datapass["datapass_sha256"],
            "room_id": room_rows[1]["room_id"],
            "event_id": "$human-event",
            "decided_at_utc": human_time,
            "scope": "AGENTTEAMS_MATRIX_HUMAN_GATE",
            "leader_room_receipt_sha256": room_rows[1]["receipt_sha256"],
        }
        decision["decision_binding_sha256"] = acceptance._human_binding_sha256(RUN_ID, decision)
        run["agent_result"]["human_decision"] = decision
        run["human_gate"] = {
            "state": "APPROVED",
            "decision": "APPROVE_PASS",
            "human_actor_id": "@reviewer:matrix.local",
            "decided_at": human_time,
            "reason": "evidence accepted",
            "post_decision_hash": acceptance.canonical_sha256(decision),
        }
        run["state"] = "COMPLETED"
        run["agentteams_trace"].append(
            {
                "event_id": "$human-event",
                "room_id": room_rows[1]["room_id"],
                "timestamp_utc": human_time,
                "actor": {
                    "role": "human",
                    "sender": "@reviewer:matrix.local",
                },
                "body": (
                    "HUMAN_DECISION "
                    f"{RUN_ID} @reviewer:matrix.local APPROVE_PASS "
                    f"{datapass['datapass_sha256']}"
                ),
            }
        )
        run["final_result"] = {
            "manifest": {
                "run_id": RUN_ID,
                "files": {
                    kind: {"sha256": h(kind)}
                    for kind in ("markdown", "pdf", "json")
                },
            }
        }
    return run


def report_bytes() -> dict[str, bytes]:
    run = build_run(final=True)
    datapass_sha = run["datapass"]["datapass_sha256"]
    human_hash = run["human_gate"]["post_decision_hash"]
    human_event = run["agent_result"]["human_decision"]["event_id"]
    source_sha = h("source-file")
    payload = {
        "protocol": "FINFLUX_HUMAN_READABLE_RESULT_V1.0",
        "run_id": RUN_ID,
        "case_id": run["case_id"],
        "submission_id": run["submission_id"],
        "human_decision": {"matrix_event_id": human_event},
        "tamper_evidence": {
            "source_file_sha256": source_sha,
            "datapass_draft_sha256": datapass_sha,
            "human_post_decision_hash": human_hash,
        },
    }
    payload["result_payload_sha256"] = acceptance.canonical_sha256(payload)
    markdown = (
        f"# Final\n\nRun ID: {RUN_ID}\n\n"
        f"Source: {source_sha}\nDataPass: {datapass_sha}\nHuman: {human_hash}\n"
    ).encode()
    return {
        "markdown": markdown,
        "pdf": b"%PDF-1.4\n% acceptance fixture\n",
        "json": json.dumps(payload, sort_keys=True).encode(),
    }


def audit_zip(run: dict, reports: dict[str, bytes]) -> bytes:
    run = copy.deepcopy(run)
    run["final_result"]["manifest"]["files"] = {
        kind: {"sha256": hashlib.sha256(content).hexdigest()}
        for kind, content in reports.items()
    }
    components = {
        "run": run,
        "submission": {
            "submission_id": "SUB-NEW-V02",
            "file": {"sha256": h("source-file")},
        },
        "change_bundle": None,
        "lifecycle": run["lifecycle"],
        "memory": {"run_id": RUN_ID},
        "worker_artifacts": run["agent_result"]["worker_artifacts"],
        "skill_receipts": [
            item
            for artifact in run["agent_result"]["worker_artifacts"].values()
            for item in artifact["skill_invocations"]
        ],
        "tool_receipts": [],
        "human_decision": run["human_gate"],
        "observability": {"run_id": RUN_ID},
    }
    component_sha = {
        key: acceptance.canonical_sha256(value)
        for key, value in components.items()
        if value is not None
    }
    truth = "same-Run evidence only"
    payload = {
        "protocol": acceptance.AUDIT_PROTOCOL,
        **components,
        "component_sha256": component_sha,
        "truth_boundary": truth,
    }
    bundle_sha = acceptance.canonical_sha256(payload)
    file_values = {
        "run.json": json.dumps(run, sort_keys=True).encode(),
        "submission.json": json.dumps(components["submission"], sort_keys=True).encode(),
        "lifecycle.json": json.dumps(components["lifecycle"], sort_keys=True).encode(),
        "memory/run-memory.json": json.dumps(components["memory"], sort_keys=True).encode(),
        "observability.json": json.dumps(components["observability"], sort_keys=True).encode(),
        "receipts/skill-receipts.json": json.dumps(components["skill_receipts"], sort_keys=True).encode(),
        "receipts/tool-receipts.json": b"[]",
        "human/human-decision.json": json.dumps(components["human_decision"], sort_keys=True).encode(),
        "result/result.md": reports["markdown"],
        "result/result.pdf": reports["pdf"],
        "result/result.json": reports["json"],
    }
    for worker_id, artifact in components["worker_artifacts"].items():
        file_values[f"workers/{worker_id}.json"] = json.dumps(artifact, sort_keys=True).encode()
    files = {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for name, raw in file_values.items()
    }
    manifest = {
        "protocol": acceptance.AUDIT_PROTOCOL,
        "bundle_sha256": bundle_sha,
        "component_sha256": component_sha,
        "truth_boundary": truth,
        "files": files,
        "file_count": len(files),
    }
    manifest["manifest_payload_sha256"] = acceptance.canonical_sha256(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, raw in file_values.items():
            archive.writestr(name, raw)
        archive.writestr("manifest.json", json.dumps(manifest))
    return output.getvalue()


class V02LiveAcceptanceTests(unittest.TestCase):
    def test_complete_native_v02_same_run_passes_before_human(self) -> None:
        result = acceptance.validate_run(build_run())
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertEqual(result["worker_artifact_count"], 3)
        self.assertEqual(result["skill_invocation_count"], 5)

    def test_running_empty_run_is_pending_but_claimed_complete_fails(self) -> None:
        run = build_run()
        run["state"] = "RUNNING"
        run["agent_result"]["worker_artifacts"] = {}
        run["datapass"] = None
        result = acceptance.validate_run(run)
        self.assertEqual(result["status"], "PENDING")
        run["state"] = "AWAITING_HUMAN"
        result = acceptance.validate_run(run)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("WORKER_ARTIFACTS_MISSING", result["failures"])

    def test_active_run_waits_for_relay_projection_and_leader_final(self) -> None:
        run = build_run()
        run["state"] = "RUNNING"
        run["lifecycle"]["current_phase"] = "DISPATCHED"
        run["agent_result"]["worker_artifacts"] = {}
        run["agent_result"]["leader_datapass_event_id"] = None
        run["datapass"] = None
        # The authorization receipt and immutable relay receipt already exist,
        # but their downstream Trace projection has not arrived yet.
        run["agentteams_trace"] = [run["agentteams_trace"][0]]

        result = acceptance.validate_run(run)

        self.assertEqual(result["status"], "PENDING", result["failures"])
        self.assertNotIn(
            "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING", result["failures"]
        )
        self.assertNotIn(
            "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING", result["failures"]
        )
        self.assertIn(
            "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING", result["pending"]
        )
        self.assertIn(
            "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING", result["pending"]
        )

        run["state"] = "AWAITING_HUMAN"
        result = acceptance.validate_run(run)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING", result["failures"]
        )
        self.assertIn(
            "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING", result["failures"]
        )

    def test_active_run_waits_for_authorization_trace_projection(self) -> None:
        run = build_run()
        run["state"] = "RUNNING"
        run["lifecycle"]["current_phase"] = "DISPATCHED"
        run["agent_result"]["worker_artifacts"] = {}
        run["agent_result"]["leader_datapass_event_id"] = None
        run["datapass"] = None
        run["agentteams_trace"] = []

        result = acceptance.validate_run(run)

        self.assertEqual(result["status"], "PENDING", result["failures"])
        self.assertIn("MANAGER_AUTHORIZATION_TRACE_EVENT_MISSING", result["pending"])
        self.assertNotIn("MANAGER_AUTHORIZATION_TRACE_EVENT_MISSING", result["failures"])

        run["state"] = "AWAITING_HUMAN"
        result = acceptance.validate_run(run)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("MANAGER_AUTHORIZATION_TRACE_EVENT_MISSING", result["failures"])

    def test_active_run_fails_on_observed_relay_or_leader_identity_mismatch(self) -> None:
        run = build_run()
        run["state"] = "RUNNING"
        relay_event = next(
            item
            for item in run["agentteams_trace"]
            if item["event_id"] == "$leader-relay"
        )
        relay_event["body"] += "\ntampered: true"

        result = acceptance.validate_run(run)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_INVALID", result["failures"]
        )

        run = build_run()
        run["state"] = "RUNNING"
        leader_final = next(
            item
            for item in run["agentteams_trace"]
            if item["event_id"] == "$leader-event"
        )
        leader_final["actor"] = {
            "role": "independent-validator",
            "sender": "@independent-validator:matrix.local",
        }

        result = acceptance.validate_run(run)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_INVALID", result["failures"]
        )

    def test_missing_agentteams_id_and_legacy_protocol_fail(self) -> None:
        run = build_run()
        run["agentteams_run_id"] = None
        run["protocol"] = "FINFLUX_LIVE_RUN_V0.1"
        result = acceptance.validate_run(run)
        self.assertIn("AGENTTEAMS_RUN_ID_MISSING", result["failures"])
        self.assertIn("RUN_PROTOCOL_NOT_V02", result["failures"])

    def test_worker_self_hash_duplicate_skill_and_wrong_owner_fail(self) -> None:
        run = build_run()
        run["agent_result"]["worker_artifacts"]["evidence-investigator"]["status"] = "TAMPERED"
        run["datapass"]["skills"]["invocations"].append(
            copy.deepcopy(run["datapass"]["skills"]["invocations"][0])
        )
        run["datapass"]["skills"]["invocations"][1]["worker_id"] = "independent-validator"
        result = acceptance.validate_run(run)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(item.startswith("WORKER_SELF_HASH_MISMATCH") for item in result["failures"]))
        self.assertTrue(any(item.startswith("SKILL_ID_DUPLICATE") for item in result["failures"]))
        self.assertTrue(any(item.startswith("SKILL_OWNER_MISMATCH") for item in result["failures"]))

    def test_final_rejects_fabricated_human(self) -> None:
        run = build_run(final=True)
        run["agent_result"]["human_decision"]["scope"] = "LOCAL_HTTP_BODY"
        result = acceptance.validate_run(run, require_final=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("HUMAN_SCOPE_INVALID", result["failures"])
        self.assertIn("HUMAN_POST_DECISION_HASH_MISMATCH", result["failures"])

    def test_final_validates_authenticated_matrix_human(self) -> None:
        result = acceptance.validate_run(build_run(final=True), require_final=True)
        self.assertEqual(result["status"], "PASS", result["failures"])

    def test_completed_run_rejects_rewritten_model_execution_terminal(self) -> None:
        run = build_run(final=True)
        binding = run["model_gateway_binding"]
        binding["terminal_state"] = "COMPLETED"
        binding.pop("binding_sha256")
        binding["binding_sha256"] = acceptance.canonical_sha256(binding)
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("MODEL_GATEWAY_BINDING_INVALID", result["failures"])

    def test_human_binding_includes_fresh_leader_room_receipt(self) -> None:
        run = build_run(final=True)
        decision = run["agent_result"]["human_decision"]
        decision["leader_room_receipt_sha256"] = h("different-room-receipt")
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("HUMAN_DECISION_BINDING_HASH_MISMATCH", result["failures"])

    def test_final_requires_archived_human_matrix_event_after_datapass(self) -> None:
        run = build_run(final=True)
        run["agentteams_trace"] = [
            item
            for item in run["agentteams_trace"]
            if item["event_id"] != "$human-event"
        ]
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("HUMAN_MATRIX_EVENT_NOT_ARCHIVED", result["failures"])

        run = build_run(final=True)
        human_event = run["agentteams_trace"][-1]
        human_event["timestamp_utc"] = "2026-08-31T00:00:05+00:00"
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("HUMAN_DECISION_NOT_AFTER_DATAPASS_DRAFT", result["failures"])

    def test_final_fails_without_prompt_budget_or_real_manager_authorization(self) -> None:
        run = build_run(final=True)
        run.pop("prompt_budget_readiness")
        receipt = run["manager_dispatch_receipt"]
        receipt["status"] = "APPLICATION_FAILOVER"
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = acceptance.canonical_sha256(receipt)
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("PROMPT_BUDGET_READINESS_MISSING", result["failures"])
        self.assertIn("MANAGER_AUTHORIZED_DISPATCH_RECEIPT_MISSING", result["failures"])

    def test_final_fails_on_wrong_task_and_nonconverged_receipt(self) -> None:
        run = build_run(final=True)
        worker = run["agent_result"]["worker_artifacts"]["evidence-investigator"]
        worker["task_id"] = "task-outside-canonical-scope"
        worker.pop("artifact_sha256")
        worker["artifact_sha256"] = acceptance.canonical_sha256(worker)
        receipt = run["task_convergence_receipt"]
        receipt["status"] = "PARTIAL"
        receipt["unexpected"] = ["task-outside-canonical-scope"]
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = acceptance.canonical_sha256(receipt)
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn(
            "WORKER_TASK_ID_NOT_CANONICAL:evidence-investigator", result["failures"]
        )
        self.assertIn("TASK_CONVERGENCE_NOT_COMPLETE", result["failures"])

    def test_manager_rejects_failover_history_and_posthoc_authorization(self) -> None:
        run = build_run(final=True)
        run["manager_dispatch_mode"] = "APPLICATION_FAILOVER"
        receipt = run["manager_dispatch_receipt"]
        receipt["failover_event_id"] = "$application-failover"
        receipt["authorized_at_utc"] = "2026-08-31T00:00:07+00:00"
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = acceptance.canonical_sha256(receipt)
        run["agentteams_trace"][0]["timestamp_utc"] = (
            "2026-08-31T00:00:07+00:00"
        )
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("MANAGER_DISPATCH_MODE_NOT_REAL_MANAGER", result["failures"])
        self.assertIn("MANAGER_DISPATCH_HAS_FAILOVER_HISTORY", result["failures"])
        self.assertIn(
            "MANAGER_AUTHORIZATION_AFTER_DOWNSTREAM_EVENT", result["failures"]
        )

    def test_manager_rejects_unbound_relay_event(self) -> None:
        run = build_run()
        run["leader_relay"]["event_id"] = "$different-relay"
        result = acceptance.validate_run(run)
        self.assertIn(
            "MANAGER_AUTHORIZATION_RELAY_BINDING_INVALID", result["failures"]
        )

    def test_convergence_binds_scope_identity_and_exact_task_directories(self) -> None:
        run = build_run(final=True)
        receipt = run["task_convergence_receipt"]
        receipt["task_scope"] = "task-scope-tampered"
        receipt["task_identity_sha256"] = h("wrong-task-identity")
        receipt["actual_task_directories"] = receipt[
            "actual_task_directories"
        ][:-1] + ["task-unexpected"]
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = acceptance.canonical_sha256(receipt)
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("TASK_CONVERGENCE_SCOPE_MISMATCH", result["failures"])
        self.assertIn(
            "TASK_CONVERGENCE_TASK_IDENTITY_HASH_MISMATCH", result["failures"]
        )
        self.assertIn(
            "TASK_CONVERGENCE_DIRECTORY_SET_MISMATCH", result["failures"]
        )

    def test_human_must_sign_formal_datapass_in_fresh_leader_room(self) -> None:
        run = build_run(final=True)
        decision = run["agent_result"]["human_decision"]
        decision["datapass_sha256"] = h("different-datapass")
        decision["room_id"] = "!reused-room:matrix.local"
        decision["decision_binding_sha256"] = acceptance._human_binding_sha256(
            RUN_ID, decision
        )
        run["human_gate"]["post_decision_hash"] = acceptance.canonical_sha256(
            decision
        )
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn(
            "HUMAN_FORMAL_DATAPASS_BINDING_MISMATCH", result["failures"]
        )
        self.assertIn(
            "HUMAN_FRESH_LEADER_ROOM_BINDING_MISMATCH", result["failures"]
        )

    def test_final_fails_on_gateway_provider_mismatch(self) -> None:
        run = build_run(final=True)
        run["provider_usage"]["model_gateway_ledger"]["total_tokens"] = 9
        ledger = run["provider_usage"]["model_gateway_ledger"]
        ledger.pop("ledger_sha256")
        ledger["ledger_sha256"] = acceptance.canonical_sha256(ledger)
        result = acceptance.validate_run(run, require_final=True)
        self.assertIn("MODEL_GATEWAY_LEDGER_RECONCILIATION_FAILED", result["failures"])

    def test_final_fails_without_model_gateway_identity_cleanup_receipt(self) -> None:
        run = build_run(final=True)
        run.pop("model_gateway_identity_cleanup")
        result = acceptance.validate_run(run, require_final=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "MODEL_GATEWAY_IDENTITY_CLEANUP_INVALID", result["failures"]
        )

    def test_final_fails_when_finflux_identity_header_remains(self) -> None:
        run = build_run(final=True)
        cleanup = run["model_gateway_identity_cleanup"]
        cleanup["finflux_headers_remaining"] = 1
        cleanup["roles"][0]["finflux_headers_remaining"] = 1
        cleanup["roles"][0]["remaining_custom_header_names"] = [
            "X-FinFlux-Identity"
        ]
        cleanup.pop("receipt_sha256")
        cleanup["receipt_sha256"] = acceptance.canonical_sha256(cleanup)
        result = acceptance.validate_run(run, require_final=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "MODEL_GATEWAY_IDENTITY_CLEANUP_INVALID", result["failures"]
        )

    def test_finalize_refuses_export_endpoint_before_strict_run_passes(self) -> None:
        run = build_run(final=True)
        run.pop("prompt_budget_readiness")
        calls = []

        def fake_api(_base_url, path, payload=None, **_kwargs):
            calls.append((path, payload))
            return run

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            acceptance,
            "load_session",
            return_value={"run_id": RUN_ID, "submission_id": "SUB-NEW-V02"},
        ), mock.patch.object(acceptance, "api_json", side_effect=fake_api):
            with self.assertRaisesRegex(RuntimeError, "pre-export"):
                acceptance.finalize(
                    "http://control",
                    Path(tmp) / "session.json",
                    Path(tmp) / "final",
                )
        self.assertEqual(calls, [(f"/api/v1/runs/{RUN_ID}", None)])

    def test_audit_zip_recomputes_bundle_components_and_reports(self) -> None:
        reports = report_bytes()
        result = acceptance.verify_audit_zip(
            audit_zip(build_run(final=True), reports),
            RUN_ID,
            expected_reports=reports,
        )
        self.assertEqual(result["status"], "PASS", result["failures"])

    def test_weak_audit_manifest_and_report_mismatch_fail(self) -> None:
        reports = report_bytes()
        source = zipfile.ZipFile(io.BytesIO(audit_zip(build_run(final=True), reports)))
        values = {name: source.read(name) for name in source.namelist()}
        source.close()
        manifest = json.loads(values["manifest.json"])
        manifest["protocol"] = "FINFLUX_AUDIT_BUNDLE_V0.1"
        manifest["manifest_payload_sha256"] = acceptance.canonical_sha256(
            {key: item for key, item in manifest.items() if key != "manifest_payload_sha256"}
        )
        values["manifest.json"] = json.dumps(manifest).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in values.items():
                archive.writestr(name, raw)
        result = acceptance.verify_audit_zip(
            output.getvalue(),
            RUN_ID,
            expected_reports={**reports, "markdown": b"different"},
        )
        self.assertIn("AUDIT_MANIFEST_PROTOCOL_INVALID", result["failures"])
        self.assertIn("AUDIT_REPORT_DOWNLOAD_MISMATCH:markdown", result["failures"])

    def test_launch_posts_exactly_one_run_and_never_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.csv"
            source_path.write_bytes(b"real,public,futures\n")
            source_sha = acceptance.file_sha256(source_path)
            preflight = {
                "protocol": "FINFLUX_FUTURES_V02_PREFLIGHT_V1.0",
                "status": "READY",
                "reasons": [],
                "source_submission_id": "SUB-SOURCE",
                "source_file": {"sha256": source_sha},
            }
            preflight["snapshot_sha256"] = acceptance.canonical_sha256(preflight)
            source = {
                "submission_id": "SUB-SOURCE",
                "file": {"name": "source.csv", "sha256": source_sha},
                "metadata": {"contract_multiplier": 300},
            }
            run = build_run()
            run["state"] = "SUBMITTED"
            run["agent_result"]["worker_artifacts"] = {}
            run["datapass"] = None
            calls = []

            def fake_api(base_url, path, payload=None, **kwargs):
                calls.append((path, payload))
                self.assertEqual(path, "/api/v1/runs")
                self.assertEqual(payload["submission_id"], "SUB-NEW-V02")
                self.assertRegex(
                    payload["client_idempotency_key"], r"^FVA-[0-9a-f]{48}$"
                )
                return bind_run_creation(run, payload)

            with (
                mock.patch.object(acceptance, "preflight", return_value=preflight),
                mock.patch.object(acceptance, "source_submission", return_value=source),
                mock.patch.object(acceptance, "source_object_path", return_value=source_path),
                mock.patch.object(
                    acceptance,
                    "post_evidence_bundle",
                    return_value={
                        "submission_id": "SUB-NEW-V02",
                        "execution_readiness": "AGENTTEAMS_EXECUTABLE",
                        "file": {"sha256": source_sha},
                    },
                ) as post_bundle,
                mock.patch.object(acceptance, "api_json", side_effect=fake_api),
            ):
                session_file = Path(tmp) / "session.json"
                result = acceptance.launch("http://control", session_file, "SUB-SOURCE")
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["session"]["post_runs_count"], 1)
                self.assertEqual(
                    result["session"]["client_idempotency_key_sha256"],
                    hashlib.sha256(
                        calls[0][1]["client_idempotency_key"].encode("utf-8")
                    ).hexdigest(),
                )
                self.assertFalse(result["session"]["human_decision_automated"])
                acceptance.load_session(session_file, expected_base_url="http://control")
                with self.assertRaisesRegex(RuntimeError, "duplicate launch"):
                    acceptance.launch("http://control", session_file, "SUB-SOURCE")
                post_bundle.assert_called_once()
                self.assertFalse(any("human" in path.lower() for path, _ in calls))

    def test_run_create_rejection_is_hash_sealed_with_provider_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.csv"
            source_path.write_bytes(b"real,public,futures\n")
            source_sha = acceptance.file_sha256(source_path)
            preflight = {
                "protocol": "FINFLUX_FUTURES_V02_PREFLIGHT_V1.0",
                "status": "READY",
                "reasons": [],
                "source_submission_id": "SUB-SOURCE",
                "source_file": {"sha256": source_sha},
            }
            preflight["snapshot_sha256"] = acceptance.canonical_sha256(preflight)
            source = {
                "submission_id": "SUB-SOURCE",
                "file": {"name": "source.csv", "sha256": source_sha},
                "metadata": {"contract_multiplier": 300},
            }
            token_guard = {
                "protocol": "FINFLUX_PROVIDER_TOKEN_GUARD_V1.0",
                "status": "BLOCKED",
                "allowed": False,
                "provider_usage_captured": True,
                "active_run_id": None,
                "reasons": ["PER_RUN_BUDGET_REJECTED"],
                "daily": {"total_tokens": 120000, "remaining_tokens": 180000},
            }
            calls = []

            def fake_api(base_url, path, payload=None, **kwargs):
                calls.append((path, payload))
                if path == "/api/v1/runs":
                    raise RuntimeError(
                        'HTTP 409 /api/v1/runs: {"error":"TOKEN_BUDGET_REJECTED"}'
                    )
                if path == "/api/v1/run-creation-attempts/reconcile":
                    return {
                        "protocol": "FINFLUX_RUN_CREATE_RECONCILIATION_V1.0",
                        "status": "NOT_FOUND",
                        "run_id": None,
                        "run": None,
                        "creates_run": False,
                        "dispatches_model": False,
                    }
                if path == "/api/v1/token-guard":
                    return token_guard
                self.fail(f"unexpected API path: {path}")

            with (
                mock.patch.object(acceptance, "preflight", return_value=preflight),
                mock.patch.object(acceptance, "source_submission", return_value=source),
                mock.patch.object(acceptance, "source_object_path", return_value=source_path),
                mock.patch.object(
                    acceptance,
                    "post_evidence_bundle",
                    return_value={
                        "submission_id": "SUB-NEW-V02",
                        "execution_readiness": "AGENTTEAMS_EXECUTABLE",
                        "file": {"sha256": source_sha},
                    },
                ),
                mock.patch.object(acceptance, "api_json", side_effect=fake_api),
            ):
                session_file = Path(tmp) / "session.json"
                with self.assertRaisesRegex(RuntimeError, "HTTP 409"):
                    acceptance.launch(
                        "http://control", session_file, "SUB-SOURCE"
                    )

            session = acceptance.load_session(
                session_file, expected_base_url="http://control"
            )
            self.assertEqual(session["status"], "RUN_CREATE_REJECTED")
            self.assertEqual(session["post_runs_count"], 1)
            self.assertIsNone(session["run_id"])
            self.assertIsNone(session["launch_response"])
            self.assertEqual(session["error_class"], "RuntimeError")
            self.assertIn("HTTP 409", session["http_detail"])
            self.assertTrue(session["failed_at_utc"])
            self.assertEqual(
                session["provider_usage_snapshot"]["status"], "CAPTURED"
            )
            self.assertEqual(
                session["provider_usage_snapshot"]["token_guard"], token_guard
            )
            self.assertEqual(
                [path for path, _ in calls],
                [
                    "/api/v1/runs",
                    "/api/v1/run-creation-attempts/reconcile",
                    "/api/v1/token-guard",
                ],
            )

    def test_lost_create_response_reconciles_the_same_run_without_second_post(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.csv"
            source_path.write_bytes(b"real,public,futures\n")
            source_sha = acceptance.file_sha256(source_path)
            preflight = {
                "protocol": "FINFLUX_FUTURES_V02_PREFLIGHT_V1.0",
                "status": "READY",
                "reasons": [],
                "source_submission_id": "SUB-SOURCE",
                "source_file": {"sha256": source_sha},
            }
            preflight["snapshot_sha256"] = acceptance.canonical_sha256(preflight)
            source = {
                "submission_id": "SUB-SOURCE",
                "file": {"name": "source.csv", "sha256": source_sha},
                "metadata": {"contract_multiplier": 300},
            }
            run = build_run()
            run["state"] = "SUBMITTED"
            run["agent_result"]["worker_artifacts"] = {}
            run["datapass"] = None
            calls = []
            committed = {}

            def fake_api(base_url, path, payload=None, **kwargs):
                calls.append((path, payload))
                if path == "/api/v1/runs":
                    committed["run"] = bind_run_creation(run, payload)
                    raise TimeoutError("response lost after durable commit")
                if path == "/api/v1/run-creation-attempts/reconcile":
                    self.assertEqual(
                        payload["client_idempotency_key"],
                        calls[0][1]["client_idempotency_key"],
                    )
                    return {
                        "protocol": "FINFLUX_RUN_CREATE_RECONCILIATION_V1.0",
                        "status": "COMMITTED",
                        "run_id": RUN_ID,
                        "run": committed["run"],
                        "creates_run": False,
                        "dispatches_model": False,
                    }
                self.fail(f"unexpected API path: {path}")

            with (
                mock.patch.object(acceptance, "preflight", return_value=preflight),
                mock.patch.object(acceptance, "source_submission", return_value=source),
                mock.patch.object(acceptance, "source_object_path", return_value=source_path),
                mock.patch.object(
                    acceptance,
                    "post_evidence_bundle",
                    return_value={
                        "submission_id": "SUB-NEW-V02",
                        "execution_readiness": "AGENTTEAMS_EXECUTABLE",
                        "file": {"sha256": source_sha},
                    },
                ),
                mock.patch.object(acceptance, "api_json", side_effect=fake_api),
            ):
                session_file = Path(tmp) / "session.json"
                result = acceptance.launch(
                    "http://control", session_file, "SUB-SOURCE"
                )

            self.assertEqual(
                [path for path, _ in calls],
                [
                    "/api/v1/runs",
                    "/api/v1/run-creation-attempts/reconcile",
                ],
            )
            self.assertEqual(result["run"]["run_id"], RUN_ID)
            self.assertTrue(result["session"]["response_loss_reconciled"])
            self.assertEqual(result["session"]["post_runs_count"], 1)
            acceptance.load_session(
                session_file, expected_base_url="http://control"
            )

    def test_session_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            value = {
                "protocol": "FINFLUX_FUTURES_V02_ACCEPTANCE_SESSION_V1.0",
                "post_runs_count": 1,
                "human_decision_automated": False,
            }
            acceptance.atomic_json(path, acceptance._sealed_session(value))
            tampered = json.loads(path.read_text())
            tampered["run_id"] = "RUN-TAMPERED"
            acceptance.atomic_json(path, tampered)
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                acceptance.load_session(path)

    def test_worker_membership_missing_or_forged_fails_v02_acceptance(self) -> None:
        missing_run = build_run()
        leader = missing_run["session_hygiene"]["leader_room"]
        membership = leader["membership_receipt"]
        missing_mxid = "@independent-validator:matrix.local"
        membership["status"] = "NOT_READY"
        membership["observed_joined_actor_ids"].remove(missing_mxid)
        membership["missing_actor_ids"] = [missing_mxid]
        membership.pop("receipt_sha256")
        membership["receipt_sha256"] = acceptance.canonical_sha256(membership)
        leader.pop("receipt_sha256")
        leader["receipt_sha256"] = acceptance.canonical_sha256(leader)
        readiness = missing_run["prompt_budget_readiness"]
        readiness.pop("readiness_sha256")
        readiness["readiness_sha256"] = acceptance.canonical_sha256(readiness)
        result = acceptance.validate_run(missing_run)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("PROMPT_BUDGET_WORKER_MEMBERSHIP_INVALID", result["failures"])
        self.assertIn("FRESH_SESSION_WORKER_MEMBERSHIP_INVALID", result["failures"])

        forged_run = build_run()
        forged_leader = forged_run["session_hygiene"]["leader_room"]
        forged_leader["membership_receipt"]["receipt_sha256"] = "0" * 64
        forged_leader.pop("receipt_sha256")
        forged_leader["receipt_sha256"] = acceptance.canonical_sha256(forged_leader)
        forged_readiness = forged_run["prompt_budget_readiness"]
        forged_readiness.pop("readiness_sha256")
        forged_readiness["readiness_sha256"] = acceptance.canonical_sha256(
            forged_readiness
        )
        forged_result = acceptance.validate_run(forged_run)
        self.assertEqual(forged_result["status"], "FAIL")
        self.assertIn(
            "PROMPT_BUDGET_WORKER_MEMBERSHIP_INVALID", forged_result["failures"]
        )
        self.assertIn(
            "FRESH_SESSION_WORKER_MEMBERSHIP_INVALID", forged_result["failures"]
        )

    def test_launch_metadata_forces_three_worker_control(self) -> None:
        metadata = acceptance.launch_metadata({"metadata": {"contract_multiplier": 300}})
        self.assertEqual(metadata["candidate_mapping"], "close")
        self.assertFalse(metadata["research_context_required"])
        self.assertFalse(metadata["operational_risk_review_required"])


if __name__ == "__main__":
    unittest.main()
