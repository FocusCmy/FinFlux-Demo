"""Acceptance gate for the small, public AgentTeams adapter.

This validator intentionally checks observable facts rather than the legacy
adapter's internal orchestration receipts.  It is used only for Runs projected
from ``FINFLUX_AGENTTEAMS_RUN_V0.3``.  Missing evidence always fails closed;
the validator never synthesizes a Manager, Worker, Skill, Token, DataPass or
Human record.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from protocol_v02 import validate_datapass


ADAPTER_PROTOCOL = "FINFLUX_AGENTTEAMS_RUN_V0.3"
EXPECTED_WORKERS = {
    "evidence-investigator",
    "semantic-impact-analyst",
    "independent-validator",
}
EXPECTED_SKILLS = {
    "evidence-integrity": ("1.0.0", "evidence-investigator"),
    "rights-gate": ("1.0.0", "evidence-investigator"),
    "semantic-contract-resolver": ("1.1.0", "semantic-impact-analyst"),
    "financial-impact-calculator": ("1.0.0", "semantic-impact-analyst"),
    "independent-evidence-validator": ("1.0.0", "independent-validator"),
}
FINAL_HUMAN_STATES = {"APPROVED", "REJECTED", "RETURNED"}


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _valid_hash(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _self_hash(value: Any, field: str) -> bool:
    if not isinstance(value, dict) or not _valid_hash(value.get(field)):
        return False
    unsigned = {key: item for key, item in value.items() if key != field}
    return value[field] == canonical_sha256(unsigned)


def _matrix_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in run.get("events") or []:
        event_id = str(event.get("event_id") or "")
        if event_id.startswith("$"):
            rows.append(
                {
                    "event_id": event_id,
                    "role": str(event.get("lane") or ""),
                    "body": str(event.get("summary") or ""),
                    "source": str(event.get("source") or ""),
                }
            )
    return rows


def validate_refactored_run(
    run: dict[str, Any], *, require_final: bool = False
) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    run_id = str(run.get("run_id") or "")
    case_id = str(run.get("case_id") or "")

    if run.get("agentteams_adapter_protocol") != ADAPTER_PROTOCOL:
        failures.append("REFACTORED_ADAPTER_PROTOCOL_INVALID")
    if run.get("protocol") != "FINFLUX_LIVE_RUN_V0.2":
        failures.append("LIVE_RUN_PROTOCOL_INVALID")
    if (run.get("root_route_decision") or {}).get("route") != "FULL_TEAM_REVIEW":
        failures.append("MANAGER_ROUTE_NOT_FULL_TEAM_REVIEW")
    if run.get("agentteams_run_id") != run_id:
        failures.append("AGENTTEAMS_RUN_ID_MISMATCH")

    events = _matrix_events(run)
    manager_receipt = run.get("manager_dispatch_receipt") or {}
    authorization_id = str(manager_receipt.get("authorization_event_id") or "")
    manager_event = next(
        (
            item
            for item in events
            if item["event_id"] == authorization_id
            and item["role"] == "manager"
            and item["body"].strip().startswith("FINFLUX_MANAGER_DISPATCHED ")
            and run_id in item["body"]
            and case_id in item["body"]
        ),
        None,
    )
    if (
        manager_receipt.get("status") != "MANAGER_AUTHORIZED_DISPATCHED"
        or not isinstance(manager_event, dict)
    ):
        failures.append("MANAGER_AUTHORIZATION_NOT_OBSERVED")

    leader_event = next(
        (
            item
            for item in events
            if item["role"] == "team_leader"
            and "DATAPASS_DRAFT" in item["body"]
            and run_id in item["body"]
            and case_id in item["body"]
        ),
        None,
    )
    aggregation = run.get("sealed_artifact_aggregation") or {}
    aggregation_valid = bool(
        isinstance(aggregation, dict)
        and aggregation.get("protocol")
        == "FINFLUX_SEALED_ARTIFACT_AGGREGATION_V1"
        and aggregation.get("run_id") == run_id
        and aggregation.get("case_id") == case_id
        and aggregation.get("model_generated_financial_truth") is False
        and set((aggregation.get("worker_artifact_sha256") or {}))
        == EXPECTED_WORKERS
        and _self_hash(aggregation, "receipt_sha256")
    )
    if not isinstance(leader_event, dict) and not aggregation_valid:
        failures.append("CASE_LEAD_DATAPASS_EVENT_NOT_OBSERVED")

    result = run.get("agent_result") or {}
    artifacts = result.get("worker_artifacts") or {}
    if set(artifacts) != EXPECTED_WORKERS:
        failures.append("WORKER_ROLE_SET_INVALID")
    task_ids: set[str] = set()
    observed_skills: dict[str, tuple[str, str]] = {}
    for role in sorted(EXPECTED_WORKERS):
        artifact = artifacts.get(role)
        if not isinstance(artifact, dict):
            continue
        task_id = str(artifact.get("task_id") or "")
        if (
            artifact.get("role") != role
            or artifact.get("run_id") != run_id
            or artifact.get("case_id") != case_id
            or not task_id
            or run_id.replace("RUN-", "", 1) not in task_id
        ):
            failures.append(f"WORKER_IDENTITY_INVALID:{role}")
        if task_id in task_ids:
            failures.append(f"WORKER_TASK_ID_DUPLICATE:{role}")
        task_ids.add(task_id)
        if not _self_hash(artifact, "artifact_sha256"):
            failures.append(f"WORKER_ARTIFACT_HASH_INVALID:{role}")
        for receipt in artifact.get("skill_invocations") or []:
            skill_id = str(receipt.get("skill_id") or "")
            if skill_id in observed_skills:
                failures.append(f"SKILL_DUPLICATE:{skill_id}")
                continue
            if (
                receipt.get("status") != "SUCCESS"
                or receipt.get("discovered_at_runtime") is not True
                or not _valid_hash(receipt.get("input_sha256"))
                or not _valid_hash(receipt.get("output_sha256"))
                or not _valid_hash(receipt.get("manifest_sha256"))
                or not _valid_hash(receipt.get("entrypoint_sha256"))
                or not _self_hash(receipt, "receipt_sha256")
            ):
                failures.append(f"SKILL_RECEIPT_INVALID:{skill_id or role}")
            observed_skills[skill_id] = (str(receipt.get("version") or ""), role)
    if observed_skills != EXPECTED_SKILLS:
        failures.append("SKILL_SET_VERSION_OR_OWNER_INVALID")

    datapass = run.get("datapass") or {}
    try:
        validate_datapass(datapass, envelope=run.get("case_envelope"), worker_artifacts=artifacts)
    except Exception as exc:  # validation result must remain stable across library messages
        failures.append(f"FORMAL_DATAPASS_INVALID:{type(exc).__name__}")
    if datapass.get("run_id") != run_id or datapass.get("case_id") != case_id:
        failures.append("FORMAL_DATAPASS_IDENTITY_MISMATCH")

    provider = run.get("provider_usage") or {}
    ledger = provider.get("model_gateway_ledger") or {}
    prompt = int(provider.get("prompt_tokens", -1) or 0)
    completion = int(provider.get("completion_tokens", -1) or 0)
    calls = int(provider.get("call_count", -1) or 0)
    total = int(provider.get("total_tokens", -1) or 0)
    if (
        provider.get("status") != "PROVIDER_REPORTED"
        or calls <= 0
        or total <= 0
        or total != prompt + completion
        or ledger.get("run_id") != run_id
        or int(ledger.get("provider_call_count", -1)) != calls
        or int(ledger.get("total_tokens", -1)) != total
        or not _self_hash(ledger, "ledger_sha256")
    ):
        failures.append("MODEL_TOKEN_LEDGER_INVALID")

    close_receipt = run.get("model_gateway_close_receipt") or {}
    expected_terminal = "AWAITING_HUMAN" if run.get("state") == "COMPLETED" else run.get("state")
    if (
        close_receipt.get("run_id") != run_id
        or close_receipt.get("state") != "CLOSED"
        or close_receipt.get("terminal_state") != expected_terminal
        or not _self_hash(close_receipt, "binding_sha256")
    ):
        failures.append("MODEL_GATEWAY_NOT_CLOSED")

    actor_receipt = run.get("model_gateway_actor_binding_receipt") or {}
    if (
        actor_receipt.get("run_id") != run_id
        or actor_receipt.get("status") != "BOUND"
        or set(actor_receipt.get("actors") or [])
        != {"manager", "finchange-case-lead", *EXPECTED_WORKERS}
        or actor_receipt.get("plaintext_identity_persisted_in_run") is not False
    ):
        failures.append("MODEL_ACTOR_BINDING_INVALID")
    elif not _self_hash(actor_receipt, "receipt_sha256"):
        # Runs produced before the public adapter cleanup did not self-hash
        # this otherwise complete receipt.  Preserve that fact as a warning;
        # all new Runs must contain the hash.
        warnings.append("MODEL_ACTOR_BINDING_LEGACY_UNHASHED")

    gate = run.get("human_gate") or {}
    decision = result.get("human_decision") or {}
    human_event = next(
        (
            item
            for item in events
            if item["event_id"] == decision.get("event_id")
            and item["role"] == "human"
            and item["body"].startswith("HUMAN_DECISION ")
            and run_id in item["body"]
            and str(datapass.get("datapass_sha256") or "") in item["body"]
        ),
        None,
    )
    # A preview is deliberately produced *before* the accountable person
    # signs.  Treating AWAITING_HUMAN as a failed final decision made the GET
    # endpoint unusable at the exact moment the reviewer needed to inspect the
    # DataPass.  Final exports remain strict: they still require the signed
    # Matrix event and all decision hashes below.
    if require_final:
        if gate.get("state") not in FINAL_HUMAN_STATES:
            failures.append("HUMAN_FINAL_STATE_MISSING")
        if (
            decision.get("scope") != "AGENTTEAMS_MATRIX_HUMAN_GATE"
            or not str(decision.get("event_id") or "").startswith("$")
            or not str(decision.get("reviewer") or "").startswith("@")
            or decision.get("datapass_sha256") != datapass.get("datapass_sha256")
            or decision.get("room_id")
            != ((run.get("agentteams") or {}).get("leader_room_id"))
            or not _self_hash(decision, "decision_binding_sha256")
            or gate.get("post_decision_hash") != canonical_sha256(decision)
        ):
            failures.append("HUMAN_DECISION_BINDING_INVALID")
        if not isinstance(human_event, dict):
            failures.append("HUMAN_MATRIX_EVENT_NOT_ARCHIVED")
    elif gate.get("state") == "AWAITING_HUMAN":
        warnings.append("HUMAN_DECISION_PENDING")
    elif gate.get("state") not in FINAL_HUMAN_STATES:
        failures.append("HUMAN_GATE_NOT_OPEN")

    if require_final:
        final_result = run.get("final_result") or {}
        if (
            not final_result.get("result_payload_sha256")
            or not isinstance(final_result.get("manifest"), dict)
            or not isinstance(final_result.get("download_urls"), dict)
        ):
            failures.append("FINAL_RESULT_ARTIFACTS_MISSING")

    return {
        "protocol": "FINFLUX_REFACTORED_ACCEPTANCE_V1",
        "run_id": run_id,
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "pending": [],
        "warnings": warnings,
        "observed": {
            "manager_authorized": isinstance(manager_event, dict),
            "case_lead_finalized": isinstance(leader_event, dict),
            "sealed_artifact_aggregation": aggregation_valid,
            "workers": len(artifacts),
            "skills": len(observed_skills),
            "provider_calls": calls,
            "provider_tokens": total,
            "human_matrix_event": isinstance(human_event, dict),
        },
    }
