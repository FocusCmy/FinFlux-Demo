from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from context_capsule import build_run_context_capsule
from manager_routing import execute_manager_route_skill
from protocol_v02 import validate_case_envelope
from research_data.investigator import build_run_research_bundle
from task_identity import build_role_task_ids

from .config import AgentTeamsConfigurationError, CONTEXT_ROOT, CORE_WORKERS


TRANSPORT_PROTOCOL = "FINFLUX_LIVE_CASE_ENVELOPE_V0.3"
HANDLE_PROTOCOL = "FINFLUX_MATRIX_CASE_HANDLE_V0.3"


def sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _worker_payload(
    submission: dict[str, Any], run: dict[str, Any], research: dict[str, Any]
) -> dict[str, Any]:
    metadata = submission.get("metadata") or {}
    parsed = submission.get("parsed") or {}
    precheck = run.get("precheck") or {}
    profile = str(submission.get("profile") or "")
    mapping = str(metadata.get("candidate_mapping") or "")
    agent_semantic_discovery = mapping.lower() == "auto_agent"
    required = str(precheck.get("required_field") or "")
    if profile == "futures_settlement" and not agent_semantic_discovery:
        candidate = parsed.get("close") if mapping == "close" else parsed.get("settle")
        required_value, scale = parsed.get("settle"), metadata.get("contract_multiplier")
    elif profile == "equity_corporate_action" and not agent_semantic_discovery:
        candidate, required_value, scale = mapping, required, 1
    elif profile == "fund_nav_admission" and not agent_semantic_discovery:
        candidate, required_value, scale = parsed.get(mapping), parsed.get(required), 10000
    elif profile in {"futures_settlement", "equity_corporate_action", "fund_nav_admission"}:
        candidate, required_value = None, None
        scale = metadata.get("contract_multiplier") if profile == "futures_settlement" else (10000 if profile == "fund_nav_admission" else 1)
    else:
        raise AgentTeamsConfigurationError("仅支持期货、股票和基金Live Profile")
    available_fields = list(parsed.get("columns") or [])
    if not available_fields:
        available_fields = [
            key for key, value in parsed.items()
            if isinstance(value, (str, int, float)) and value not in (None, "")
        ]
    field_values = {
        key: value for key, value in parsed.items()
        if key in available_fields and isinstance(value, (str, int, float))
    }
    semantic_candidates = list(dict.fromkeys(
        [str(item) for item in available_fields]
        + [
            str(value) for key, value in parsed.items()
            if key in {"candidate_mapping", "candidate_nav_field", "declared_adjustment", "observed_adjustment"}
            and value not in (None, "")
        ]
    ))
    payload = {
        "s": submission.get("submission_id"),
        "f": profile,
        "a": metadata.get("asset_class"),
        "h": (submission.get("file") or {}).get("sha256"),
        "r": submission.get("evidence_root_hash"),
        "g": (submission.get("rights_gate") or {}).get("status"),
        "i": parsed.get("instrument"),
        "d": parsed.get("trade_date") or parsed.get("event_date") or parsed.get("nav_date"),
        "c": candidate,
        "t": required_value,
        "m": mapping,
        "x": scale,
        "ps": precheck.get("sha256"),
        "pi": precheck.get("impact_cny_per_contract"),
        "pr": precheck.get("machine_recommendation"),
        "rm": research.get("manifest_sha256"),
        "rb": research.get("bundle_sha256"),
        "rc": research.get("selected_count"),
        "co": {
            "status": "RESOLVED",
            "contract": precheck.get("contract"),
            "declared_purpose": metadata.get("declared_purpose"),
            "required_field": required,
            "candidate_mapping": mapping,
        },
        "im": {
            **{key: value for key, value in precheck.items() if key not in {"sha256", "contract"}},
            "recommended_decision": precheck.get("machine_recommendation"),
            "financial_misstatement_cny_per_contract": precheck.get("impact_cny_per_contract"),
            "generated_by_model": False,
        },
        "va": {
            "evidence_status": "VERIFIED",
            "contract_status": "RESOLVED",
            "calculation_status": "MATCH",
            "recommendation_status": "MATCH",
            "independent_recommendation": precheck.get("machine_recommendation"),
        },
        # q/sf/sc/sv are evidence-derived context, not a pre-resolved answer.
        # Two model Workers must independently propose the semantic mapping.
        "q": str((submission.get("case_input") or {}).get("task_instruction") or ""),
        "dp": metadata.get("declared_purpose"),
        "sf": available_fields[:40],
        "sc": semantic_candidates[:50],
        "sv": field_values,
    }
    if agent_semantic_discovery:
        payload["co"] = None
        payload["im"] = None
        payload["va"] = None
    required_keys = ("s", "f", "a", "h", "r", "i", "d", "m", "x", "ps", "rm", "rb")
    if agent_semantic_discovery:
        required_keys += ("q", "sf")
    missing = [key for key in required_keys if payload.get(key) in (None, "")]
    if missing:
        raise AgentTeamsConfigurationError("Worker上下文缺少字段: " + ",".join(missing))
    payload["ph"] = sha256(payload)
    return payload


def build_transport(
    submission: dict[str, Any], run: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    formal = run.get("case_envelope") or {}
    try:
        validate_case_envelope(formal)
    except Exception as exc:
        raise AgentTeamsConfigurationError(f"正式CaseEnvelope无效: {exc}") from exc
    if run.get("submission_id") != submission.get("submission_id"):
        raise AgentTeamsConfigurationError("Run与Submission不一致")
    root_route = run.get("root_route_decision") or {}
    decision, receipt = execute_manager_route_skill(dict(root_route.get("input_facts") or {}))
    if decision.get("decision_sha256") != root_route.get("decision_sha256"):
        raise AgentTeamsConfigurationError("Manager路由Skill重放不一致")
    if decision.get("route") not in {"FULL_TEAM_REVIEW", "BLAST_RADIUS_REVIEW"}:
        raise AgentTeamsConfigurationError("当前Case无需AgentTeams完整核验")
    workers = tuple(
        role for role in CORE_WORKERS
        if role in {
            "Evidence Investigator": "evidence-investigator",
            "Semantic Impact Analyst": "semantic-impact-analyst",
            "Independent Validator": "independent-validator",
        }.values()
    )
    research = build_run_research_bundle(
        str((submission.get("metadata") or {}).get("asset_class") or "unknown"),
        str(run["case_id"]),
        str(run["run_id"]),
    )
    if research.get("status") != "VERIFIED_METADATA":
        raise AgentTeamsConfigurationError("Research Data Layer没有可核验句柄")
    payload = _worker_payload(submission, run, research)
    route_handle = {
        "decision_id": decision["decision_id"],
        "reason_codes": decision["reason_codes"],
        "input_facts_sha256": decision["input_facts_sha256"],
        "decision_sha256": decision["decision_sha256"],
    }
    _, capsule = build_run_context_capsule(
        case_id=str(run["case_id"]),
        run_id=str(run["run_id"]),
        payload=payload,
        selected_workers=workers,
        execution_policy_id=str(policy["policy_id"]),
        root_route_decision_handle=route_handle,
        local_root=CONTEXT_ROOT,
    )
    dispatch_key = str((formal.get("execution") or {}).get("dispatch_idempotency_key") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", dispatch_key):
        raise AgentTeamsConfigurationError("正式CaseEnvelope缺少派发幂等键")
    return {
        "protocol": TRANSPORT_PROTOCOL,
        "case_id": run["case_id"],
        "run_id": run["run_id"],
        "submission_id": submission["submission_id"],
        "asset_class": formal["asset_class"],
        "profile_id": (formal.get("profile") or {}).get("profile_id"),
        "purpose_id": (formal.get("declared_purpose") or {}).get("purpose_id"),
        "formal_case_envelope_handle": {
            "protocol": formal["protocol"],
            "schema_version": formal["schema_version"],
            "envelope_sha256": formal["envelope_sha256"],
        },
        "evidence_handles": [{
            "evidence_id": submission.get("evidence_bundle_id"),
            "sha256_source": (submission.get("file") or {}).get("sha256"),
            "evidence_root_hash": submission.get("evidence_root_hash"),
            "rights_status": (submission.get("rights_gate") or {}).get("status"),
        }],
        "research_evidence_handle": {
            "selected_count": research["selected_count"],
            "manifest_sha256": research["manifest_sha256"],
            "bundle_sha256": research["bundle_sha256"],
        },
        "root_route_decision_handle": route_handle,
        "manager_skill_receipt": receipt,
        "execution_policy_id": policy["policy_id"],
        "dispatch_idempotency_key": dispatch_key,
        "required_workers": list(workers),
        "required_skill_registry_handle": {
            "count": len(decision.get("required_skill_versions") or {}),
            "sha256": sha256(decision.get("required_skill_versions") or {}),
        },
        "context_capsule_handle": capsule,
        "semantic_discovery_brief": {
            "mode": "AGENT_PROPOSES_SKILL_VERIFIES_HUMAN_DECIDES",
            "task_instruction": payload["q"],
            "declared_purpose": (submission.get("metadata") or {}).get("declared_purpose"),
            "profile_hint": submission.get("profile"),
            "available_fields": payload["sf"],
            "semantic_candidates": payload["sc"],
            "source_values_hidden_from_matrix": True,
        },
        "precheck_attestation": {
            "sha256": (run.get("precheck") or {}).get("sha256"),
            "recommendation": (run.get("precheck") or {}).get("machine_recommendation"),
            "semantic_resolution_mode": (
                "AGENT_PROPOSES"
                if str(((submission.get("metadata") or {}).get("candidate_mapping") or "")).lower() == "auto_agent"
                else "DECLARED_MAPPING_VERIFICATION"
            ),
        },
    }


def build_handle(envelope: dict[str, Any]) -> dict[str, Any]:
    workers = tuple(envelope["required_workers"])
    slices = (envelope["context_capsule_handle"] or {}).get("role_slice_handles") or {}
    task_identity = build_role_task_ids(envelope["case_id"], envelope["run_id"], workers)
    body = {
        "protocol": HANDLE_PROTOCOL,
        "run_id": envelope["run_id"],
        "case_id": envelope["case_id"],
        "route": "FULL_TEAM_REVIEW",
        "formal_envelope_sha256": envelope["formal_case_envelope_handle"]["envelope_sha256"],
        "dispatch_idempotency_key": envelope["dispatch_idempotency_key"],
        "selected_workers": list(workers),
        "role_slice_sha256": {role: slices[role]["slice_sha256"] for role in workers},
        "task_identity": task_identity,
        "full_envelope_sha256": sha256(envelope),
    }
    return {**body, "handle_sha256": sha256(body)}
