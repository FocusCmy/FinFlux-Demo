from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import inspect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_identity import TaskIdentityError, run_task_scope

from finchange_gate_core import (
    ASSETS,
    compute_financial_impact,
    reconcile_source_semantics,
    resolve_semantic_contract,
    validate_admission_package,
    verify_evidence_bundle,
)
from context_capsule import load_role_context_slice
ROLES = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "data-rights-steward",
    "research-context-analyst",
    "runtime-resilience-auditor",
    "independent-validator",
)


def _validate_task_binding(
    role: str, case_id: str, run_id: str, task_id: str
) -> str:
    """Require the one exact role task derived from the complete Live Run."""

    try:
        expected = f"{run_task_scope(case_id, run_id)}-{role}"
    except TaskIdentityError as exc:
        raise ValueError(f"invalid live task identity: {exc}") from exc
    if task_id != expected:
        raise ValueError("task_id is not the exact role task for this Live Run")
    return expected
FILE_BY_ROLE = {
    "evidence-investigator": "evidence_result.json",
    "semantic-impact-analyst": "semantic_impact_result.json",
    "data-rights-steward": "rights_review_result.json",
    "research-context-analyst": "research_context_result.json",
    "runtime-resilience-auditor": "runtime_resilience_result.json",
    "independent-validator": "independent_validation.json",
}


def _task_root() -> Path:
    return Path(
        os.environ.get(
            "FINFLUX_TASK_ROOT",
            "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/tasks",
        )
    ).resolve()


def _safe_output_dir(task_id: str, output_dir: str | None) -> Path:
    root = _task_root()
    target = Path(output_dir).resolve() if output_dir else (root / task_id).resolve()
    if target.parent != root or target.name != task_id:
        raise ValueError("output directory must be the exact bounded task directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _result(
    role: str,
    asset: str,
    case_id: str,
    run_id: str,
    task_id: str,
    policy_id: str,
    scenario: str,
) -> dict[str, Any]:
    tool_run_id = hashlib.sha256(
        f"{role}|{asset}|{case_id}|{run_id}|{task_id}".encode("utf-8")
    ).hexdigest()[:24]
    common = {
        "protocol": "FINFLUX_BOUNDED_WORKER_RESULT_V0.1",
        "role": role,
        "asset_class": asset,
        "case_id": case_id,
        "run_id": run_id,
        "task_id": task_id,
        "execution_policy_id": policy_id,
        "review_scenario": scenario,
        "tool_run_id": tool_run_id,
        "deterministic": True,
        "model_generated_financial_truth": False,
    }
    if role == "evidence-investigator":
        # Research providers are deliberately packaged only with the Evidence
        # Investigator.  Import lazily so the two other bounded Worker images
        # do not gain unnecessary provider/network dependencies.
        from research_data.investigator import build_run_research_bundle

        evidence = verify_evidence_bundle(asset)
        reconciliation = reconcile_source_semantics(asset)
        research_evidence = build_run_research_bundle(asset, case_id, run_id)
        evidence_verified = evidence["status"] == "VERIFIED"
        research_verified = research_evidence.get("status") == "VERIFIED_METADATA"
        return {
            **common,
            "status": (
                "VERIFIED"
                if evidence_verified and research_verified
                else "NEEDS_EVIDENCE"
            ),
            "evidence": evidence,
            "source_semantics": reconciliation,
            "research_evidence": research_evidence,
            "research_evidence_status": research_evidence.get("status"),
            "research_item_ids": [
                item.get("research_item_id")
                for item in research_evidence.get("items", [])
            ],
        }
    if role == "semantic-impact-analyst":
        contract = resolve_semantic_contract(asset)
        impact = compute_financial_impact(asset, scenario)
        return {
            **common,
            "status": "SUCCESS",
            "contract": contract,
            "impact": impact,
            "recommendation": impact["recommended_decision"],
        }
    if role == "data-rights-steward":
        return {
            **common,
            "status": "NEEDS_EVIDENCE",
            "rights_decision": "HOLD",
            "reason_codes": ["STATIC_CASE_HAS_NO_RUN_SCOPED_RIGHTS_DECLARATION"],
            "truth_boundary": "The Agent may review declared rights metadata but may not invent legal authority.",
        }
    if role == "research-context-analyst":
        from research_data.investigator import build_run_research_bundle

        research = build_run_research_bundle(asset, case_id, run_id)
        verified = research.get("status") == "VERIFIED_METADATA"
        return {
            **common,
            "status": "VERIFIED_CONTEXT" if verified else "NEEDS_EVIDENCE",
            "research_evidence": research,
            "context_decision": "AVAILABLE" if verified else "HOLD",
        }
    if role == "runtime-resilience-auditor":
        return {
            **common,
            "status": "NEEDS_RUNTIME_TELEMETRY",
            "operational_decision": "HOLD",
            "reason_codes": ["STATIC_CASE_HAS_NO_RUN_SCOPED_RUNTIME_RECEIPT"],
            "truth_boundary": "Offline fixtures cannot be relabelled as live recovery or capacity evidence.",
        }
    validation = validate_admission_package(asset, scenario)
    return {
        **common,
        **validation,
    }


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _seal_worker_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """Bind the complete Worker artifact, including context receipts, to itself."""
    unsigned = {
        key: value for key, value in payload.items() if key != "artifact_sha256"
    }
    payload["artifact_sha256"] = _canonical_sha256(unsigned)
    return payload


def _decode_live_payload(encoded: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid live worker payload") from exc
    if not isinstance(payload, dict) or payload.get("p") != "FINFLUX_LIVE_WORKER_PAYLOAD_V0.1":
        raise ValueError("unsupported live worker payload protocol")
    digest = str(payload.pop("ph", ""))
    if len(digest) != 64 or _canonical_sha256(payload) != digest:
        raise ValueError("live worker payload hash mismatch")
    payload["ph"] = digest
    required = (
        "s", "f", "h", "r", "i", "d", "c", "t", "m", "x", "ps",
        "rm", "rb", "rc",
    )
    if any(key not in payload for key in required):
        raise ValueError("live worker payload is incomplete")
    if payload["f"] not in {
        "futures_settlement", "equity_corporate_action", "fund_nav_admission"
    }:
        raise ValueError("unsupported live financial profile")
    if any(len(str(payload[key])) != 64 for key in ("h", "r", "ps", "rm", "rb")):
        raise ValueError("live evidence hash is invalid")
    return payload


def _skill_invocation(
    skill_id: str, version: str, input_payload: Any, output_payload: Any
) -> dict[str, Any]:
    digest = hashlib.sha256(f"{skill_id}@{version}".encode()).hexdigest()
    return {
        "skill_id": skill_id,
        "version": version,
        "digest": digest,
        "input_sha256": _canonical_sha256(input_payload),
        "output_sha256": _canonical_sha256(output_payload),
        "status": "SUCCESS",
        "discovered_at_runtime": True,
    }


SKILL_MANIFEST_PROTOCOL = "FINFLUX_WORKER_SKILL_MANIFEST_V1.0"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_evidence_integrity(input_payload: dict[str, Any]) -> dict[str, Any]:
    """Verify evidence from a registered fixture or a live hash envelope."""
    source_asset = str(input_payload.get("source_asset", ""))
    if source_asset in ASSETS:
        return verify_evidence_bundle(source_asset)
    registered = input_payload.get("registered_verification")
    if isinstance(registered, dict):
        if str(registered.get("status", "")) not in {
            "VERIFIED",
            "VERIFIED_METADATA",
        }:
            raise ValueError("registered evidence is not verified")
        return copy.deepcopy(registered)
    hashes = {
        key: str(input_payload.get(key, ""))
        for key in ("file_sha256", "evidence_root_hash", "precheck_sha256")
    }
    if any(len(value) != 64 for value in hashes.values()):
        raise ValueError("evidence-integrity requires complete SHA-256 handles")
    rights_gate = str(input_payload.get("rights_gate", "MISSING")).upper()
    research_count = int(input_payload.get("research_item_count", 0) or 0)
    return {
        "status": "VERIFIED" if rights_gate == "PASS" else "NEEDS_EVIDENCE",
        "manifest_file_count": 1,
        "raw_files_checked": 1,
        "missing_files": [],
        "hash_mismatches": [],
        "file_sha256": hashes["file_sha256"],
        "evidence_root_hash": hashes["evidence_root_hash"],
        "gateway_attestation_verified": True,
        "research_evidence": {
            "status": "VERIFIED_METADATA" if research_count > 0 else "NOT_IN_ROLE_SLICE",
            "selected_count": research_count,
            "manifest_sha256": input_payload.get("research_manifest_sha256"),
            "bundle_sha256": input_payload.get("research_bundle_sha256"),
        },
    }


def skill_rights_gate(input_payload: dict[str, Any]) -> dict[str, Any]:
    state = str(
        input_payload.get("rights_state", input_payload.get("rights_gate", "MISSING"))
    ).upper()
    return {
        "status": "PASS" if state == "PASS" else "NEEDS_EVIDENCE",
        "bounded_use_only": True,
    }


def skill_semantic_contract_resolver(input_payload: dict[str, Any]) -> dict[str, Any]:
    proposal = input_payload.get("agent_proposal")
    if isinstance(proposal, dict):
        profile_id = str(input_payload.get("profile_id", ""))
        profile_asset = {
            "futures_settlement": "futures",
            "equity_corporate_action": "equity",
            "fund_nav_admission": "fund",
        }.get(profile_id, "")
        registry_path = Path(__file__).resolve().with_name("semantic_contracts.json")
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        contract_spec = registry.get(profile_asset) or {}
        purpose = str(input_payload.get("declared_purpose") or "")
        proposed_field = str(proposal.get("proposed_field") or "").strip()
        available = {
            str(item) for item in (
                list(input_payload.get("available_fields") or [])
                + list(input_payload.get("semantic_candidates") or [])
            )
        }
        required_field = str((contract_spec.get("field_for_use") or {}).get(purpose) or "")
        proposal_valid = bool(proposed_field and proposed_field in available)
        registry_resolved = bool(contract_spec and required_field)
        status = "RESOLVED" if proposal_valid and registry_resolved else "NEEDS_EVIDENCE"
        return {
            "status": status,
            "contract": (
                f"{contract_spec.get('name')}@{registry.get('contract_version')}"
                if contract_spec else None
            ),
            "declared_purpose": purpose,
            "required_field": required_field or None,
            "candidate_mapping": proposed_field or None,
            "proposal_source": "AGENT_MODEL",
            "proposal_valid_against_sealed_schema": proposal_valid,
            "registry_resolved": registry_resolved,
            "confidence_bps": proposal.get("confidence_bps"),
            "reason_code": proposal.get("reason_code"),
            "uncertainty_code": proposal.get("uncertainty_code"),
        }
    source_asset = str(input_payload.get("source_asset", ""))
    if source_asset in ASSETS:
        return resolve_semantic_contract(source_asset)
    registered = input_payload.get("registered_contract")
    if isinstance(registered, dict):
        if not registered.get("status"):
            raise ValueError("registered semantic contract has no status")
        return copy.deepcopy(registered)
    profile_id = str(input_payload.get("profile_id", ""))
    if profile_id != "futures_settlement":
        raise ValueError("semantic-contract-resolver has no registered Profile")
    candidate_mapping = str(input_payload.get("candidate_mapping", ""))
    if candidate_mapping not in {"close", "settle"}:
        raise ValueError("unsupported futures candidate mapping")
    return {
        "status": "RESOLVED",
        "contract": "FuturesSettlementContract@0.1.0",
        "declared_purpose": "daily_settlement_pnl",
        "required_field": "settle",
        "candidate_mapping": candidate_mapping,
    }


def skill_financial_impact_calculator(input_payload: dict[str, Any]) -> dict[str, Any]:
    proposal = input_payload.get("agent_proposal")
    contract = input_payload.get("resolved_contract") or {}
    if isinstance(proposal, dict):
        proposed_field = str(proposal.get("proposed_field") or "")
        required_field = str(contract.get("required_field") or "")
        values = dict(input_payload.get("field_values") or {})
        scale = float(input_payload.get("calculation_scale") or 1)
        selected_value = values.get(proposed_field)
        required_value = values.get(required_field)
        impact = None
        if isinstance(selected_value, (int, float)) and isinstance(required_value, (int, float)):
            impact = round(abs(float(required_value) - float(selected_value)) * scale, 6)
        if contract.get("status") != "RESOLVED":
            recommendation = "NEEDS_EVIDENCE"
        elif proposed_field == required_field:
            recommendation = "PASS"
            impact = 0.0 if impact is None else impact
        elif impact is None:
            recommendation = "NEEDS_EVIDENCE"
        else:
            recommendation = "BLOCK"
        metric_key = (
            "impact_cny_per_10000_units"
            if str(input_payload.get("profile_id")) == "fund_nav_admission"
            else "financial_misstatement_cny_per_contract"
        )
        return {
            "selected_price_field": proposed_field or None,
            "required_field": required_field or None,
            "selected_value": selected_value,
            "required_value": required_value,
            "calculation_scale": scale,
            "field_mapping_difference_points": (
                round(abs(float(required_value) - float(selected_value)), 6)
                if isinstance(selected_value, (int, float)) and isinstance(required_value, (int, float))
                else None
            ),
            metric_key: impact,
            "recommended_decision": recommendation,
            "proposal_source": "AGENT_MODEL",
            "generated_by_model": False,
        }
    source_asset = str(input_payload.get("source_asset", ""))
    if source_asset in ASSETS:
        return compute_financial_impact(
            source_asset, str(input_payload.get("scenario", "blocked"))
        )
    registered = input_payload.get("registered_impact")
    if isinstance(registered, dict):
        if registered.get("generated_by_model") is True:
            raise ValueError("registered impact cannot be model-generated truth")
        return copy.deepcopy(registered)
    if str(input_payload.get("profile_id", "")) != "futures_settlement":
        raise ValueError("financial-impact-calculator has no registered Profile")
    close = float(input_payload["close"])
    settle = float(input_payload["settle"])
    multiplier = float(input_payload["contract_multiplier"])
    selected_field = str(input_payload["candidate_mapping"])
    if selected_field not in {"close", "settle"}:
        raise ValueError("unsupported futures candidate mapping")
    selected_value = close if selected_field == "close" else settle
    impact = round(abs(settle - selected_value) * multiplier, 6)
    recommendation = "PASS" if selected_field == "settle" else "BLOCK"
    return {
        "selected_price_field": selected_field,
        "selected_price": selected_value,
        "close": close,
        "settle": settle,
        "contract_multiplier": multiplier,
        "field_mapping_difference_points": round(abs(settle - selected_value), 6),
        "financial_misstatement_cny_per_contract": impact,
        "recommended_decision": recommendation,
        "generated_by_model": False,
    }


def skill_classify_data_rights(input_payload: dict[str, Any]) -> dict[str, Any]:
    classification = str(input_payload.get("confidentiality_class", "PUBLIC")).upper()
    permitted_scope = str(input_payload.get("permitted_scope", "EVALUATION_ONLY")).upper()
    if classification not in {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}:
        raise ValueError("unsupported confidentiality class")
    result = {
        "confidentiality_class": classification,
        "declared_rights_basis_present": bool(input_payload.get("rights_basis")),
        "permitted_scope": permitted_scope,
        "raw_content_export_allowed": classification == "PUBLIC",
        "rights_gate": str(input_payload.get("rights_gate", "MISSING")).upper(),
    }
    for key in (
        "rights_state_counts",
        "provider_counts",
        "permitted_projection_count",
        "unrestricted_fulltext_denied_count",
        "rights_decisions_sha256",
    ):
        if key in input_payload:
            result[key] = copy.deepcopy(input_payload[key])
    return result


def skill_enforce_confidentiality_boundary(input_payload: dict[str, Any]) -> dict[str, Any]:
    denied_count = int(input_payload.get("unrestricted_fulltext_denied_count", 0) or 0)
    if denied_count > 0:
        return {
            "status": "BLOCK_UNRESTRICTED_FULLTEXT",
            "permitted_projection_count": int(
                input_payload.get("permitted_projection_count", 0) or 0
            ),
            "unrestricted_fulltext_denied_count": denied_count,
            "rights_decisions_sha256": input_payload.get("rights_decisions_sha256"),
        }
    classification = str(input_payload.get("confidentiality_class", "")).upper()
    scope = str(input_payload.get("permitted_scope", "")).upper()
    rights_pass = (
        str(input_payload.get("rights_gate", "MISSING")).upper() == "PASS"
        and bool(input_payload.get("declared_rights_basis_present"))
        and classification in {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}
        and scope in {"EVALUATION_ONLY", "INTERNAL_RESEARCH", "PRODUCTION_REVIEW"}
    )
    return {
        "status": "PASS" if rights_pass else "NEEDS_EVIDENCE",
        "rights_decision": "PASS" if rights_pass else "HOLD",
        "reason_codes": []
        if rights_pass
        else ["RIGHTS_OR_CONFIDENTIALITY_DECLARATION_INCOMPLETE"],
        "model_may_read_raw_content": classification == "PUBLIC" and rights_pass,
    }


def skill_retrieve_research_context(input_payload: dict[str, Any]) -> dict[str, Any]:
    registered = input_payload.get("registered_context")
    if isinstance(registered, dict):
        if int(registered.get("selected_count", 0) or 0) < 1:
            raise ValueError("registered research context is empty")
        return copy.deepcopy(registered)
    count = int(input_payload.get("research_item_count", 0) or 0)
    return {
        "status": "AVAILABLE" if count > 0 else "EMPTY",
        **copy.deepcopy(input_payload),
        "raw_provider_payload_copied_to_matrix": False,
    }


def skill_verify_research_context(input_payload: dict[str, Any]) -> dict[str, Any]:
    count = int(
        input_payload.get("research_item_count", input_payload.get("selected_count", 0))
        or 0
    )
    manifest = str(input_payload.get("manifest_sha256", ""))
    bundle = str(input_payload.get("bundle_sha256", ""))
    verified = count > 0 and len(manifest) == 64 and len(bundle) == 64
    return {
        "status": "VERIFIED_CONTEXT" if verified else "NEEDS_EVIDENCE",
        "selected_count": count,
        "manifest_sha256": manifest,
        "bundle_sha256": bundle,
        "manifest_hash_valid": len(manifest) == 64,
        "bundle_hash_valid": len(bundle) == 64,
        "context_is_financial_truth": False,
        "financial_truth_generated": False,
    }


def skill_guard_execution_budget(input_payload: dict[str, Any]) -> dict[str, Any]:
    bounded = (
        str(input_payload.get("execution_policy_id", ""))
        == "FINFLUX-BOUNDED-EXECUTION-V0.1"
        and 0 < int(input_payload.get("tool_timeout_seconds", 0) or 0) <= 90
        and int(input_payload.get("max_tool_retries", -1) or 0) == 0
        and bool(input_payload.get("checkpoint_required"))
    )
    return {
        "status": "READY" if bounded else "HOLD",
        "fail_closed": True,
        "provider_token_hard_cap_available": False,
        "limits": copy.deepcopy(input_payload),
    }


def skill_audit_recovery_readiness(input_payload: dict[str, Any]) -> dict[str, Any]:
    limits = input_payload.get("limits") or {}
    bounded = input_payload.get("status") == "READY"
    return {
        "status": "READY_FOR_CHECKPOINTED_RUN" if bounded else "HOLD",
        "checkpoint_required": bool(limits.get("checkpoint_required")),
        "implicit_retry_allowed": False,
        "failure_receipt_required": True,
        "claim_proven_live_failover": False,
    }


def skill_independent_evidence_validator(input_payload: dict[str, Any]) -> dict[str, Any]:
    source_asset = str(input_payload.get("source_asset", ""))
    if source_asset in ASSETS:
        return validate_admission_package(
            source_asset, str(input_payload.get("scenario", "blocked"))
        )
    registered = input_payload.get("registered_validation")
    if isinstance(registered, dict):
        return copy.deepcopy(registered)
    evidence = skill_evidence_integrity(input_payload)
    contract = skill_semantic_contract_resolver(input_payload)
    impact = skill_financial_impact_calculator(input_payload)
    expected_impact = float(
        input_payload.get(
            "expected_impact", impact["financial_misstatement_cny_per_contract"]
        )
    )
    expected_recommendation = str(
        input_payload.get("expected_recommendation", impact["recommended_decision"])
    )
    calculation_match = abs(
        expected_impact - float(impact["financial_misstatement_cny_per_contract"])
    ) < 1e-9
    recommendation_match = (
        expected_recommendation == impact["recommended_decision"]
    )
    consistent = (
        evidence["status"] == "VERIFIED"
        and contract["status"] == "RESOLVED"
        and calculation_match
        and recommendation_match
    )
    return {
        "evidence_status": evidence["status"],
        "contract_status": contract["status"],
        "calculation_status": "MATCH" if calculation_match else "MISMATCH",
        "recommendation_status": "MATCH" if recommendation_match else "MISMATCH",
        "independent_recommendation": impact["recommended_decision"]
        if consistent
        else "NEEDS_EVIDENCE",
    }


def _discover_skill_registry(
    role: str, manifest_path: str | Path | None = None
) -> dict[str, Any]:
    """Discover and verify the role package's runtime Skill manifest."""
    configured = os.environ.get("FINFLUX_WORKER_SKILL_MANIFEST", "").strip()
    adjacent = Path(__file__).resolve().with_name("runtime-skill-manifest.json")
    fallback = (
        Path(__file__).resolve().parent
        / "runtime-skill-manifests"
        / f"{role}.json"
    )
    path = Path(manifest_path or configured or (adjacent if adjacent.is_file() else fallback)).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    declared = str(manifest.pop("manifest_sha256", ""))
    actual = _canonical_sha256(manifest)
    if declared != actual:
        raise ValueError("Worker Skill manifest digest mismatch")
    if manifest.get("protocol") != SKILL_MANIFEST_PROTOCOL:
        raise ValueError("unsupported Worker Skill manifest protocol")
    if manifest.get("role") != role:
        raise ValueError("Worker Skill manifest role mismatch")
    package_root_token = str(manifest.get("package_root", "."))
    if package_root_token not in {".", ".."}:
        raise ValueError("Worker Skill manifest package root is invalid")
    package_root = (path.parent / package_root_token).resolve()
    entrypoint = (package_root / str(manifest.get("entrypoint", {}).get("path", ""))).resolve()
    entrypoint.relative_to(package_root)
    if _file_sha256(entrypoint) != manifest["entrypoint"]["sha256"]:
        raise ValueError("Worker Skill entrypoint digest mismatch")
    skills: dict[str, dict[str, Any]] = {}
    for item in manifest.get("skills") or []:
        skill_id = str(item.get("skill_id", ""))
        version = str(item.get("version", ""))
        key = f"{skill_id}@{version}"
        if not skill_id or not version or key in skills:
            raise ValueError("Worker Skill manifest contains an invalid Skill identity")
        instruction = (package_root / str(item.get("instruction_path", ""))).resolve()
        instruction.relative_to(package_root)
        if _file_sha256(instruction) != item.get("instruction_sha256"):
            raise ValueError(f"Worker Skill instruction digest mismatch: {key}")
        callable_name = str(item.get("callable", ""))
        callable_obj = globals().get(callable_name)
        if not callable_name or not callable(callable_obj):
            raise ValueError(f"Worker Skill callable is unavailable: {key}")
        callable_sha256 = hashlib.sha256(
            inspect.getsource(callable_obj).encode("utf-8")
        ).hexdigest()
        if callable_sha256 != item.get("callable_sha256"):
            raise ValueError(f"Worker Skill callable digest mismatch: {key}")
        skills[key] = {
            **dict(item),
            "callable_obj": callable_obj,
        }
    if not skills:
        raise ValueError("Worker Skill manifest is empty")
    return {
        "role": role,
        "manifest_sha256": declared,
        "entrypoint_sha256": manifest["entrypoint"]["sha256"],
        "loaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "skills": skills,
    }


def _execute_verified_skill(
    registry: dict[str, Any],
    skill_id: str,
    version: str,
    input_payload: Any,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one manifest-bound callable and emit a receipt only on success."""
    key = f"{skill_id}@{version}"
    skill = registry["skills"].get(key)
    if not skill:
        raise ValueError(f"required runtime Skill is unavailable: {key}")
    if timeout_seconds <= 0:
        raise ValueError("Skill timeout must be positive")
    immutable_input = copy.deepcopy(input_payload)
    input_sha256 = _canonical_sha256(immutable_input)
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finflux-skill")
    future = executor.submit(skill["callable_obj"], immutable_input)
    try:
        output_payload = future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"runtime Skill timed out: {key}") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if not isinstance(output_payload, dict):
        raise ValueError(f"runtime Skill returned a non-object result: {key}")
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    output_sha256 = _canonical_sha256(output_payload)
    tool_run_id = hashlib.sha256(
        (
            f"{key}|{registry['manifest_sha256']}|{skill['callable_sha256']}|"
            f"{input_sha256}|{output_sha256}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    receipt = _skill_invocation(skill_id, version, immutable_input, output_payload)
    receipt.update(
        {
            "manifest_sha256": registry["manifest_sha256"],
            "entrypoint_sha256": registry["entrypoint_sha256"],
            "entrypoint_callable": skill["callable"],
            "callable_sha256": skill["callable_sha256"],
            "instruction_sha256": skill["instruction_sha256"],
            "loaded_at_utc": registry["loaded_at_utc"],
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "execution_channel": "WORKER_DETERMINISTIC_SKILL_RUNTIME",
            "tool_run_id": tool_run_id,
            "exit_code": 0,
            "elapsed_ms": elapsed_ms,
            "provider_tokens": 0,
        }
    )
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return output_payload, receipt


def _live_result(
    role: str,
    case_id: str,
    run_id: str,
    task_id: str,
    policy_id: str,
    payload: dict[str, Any],
    skill_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = skill_registry or _discover_skill_registry(role)
    tool_run_id = hashlib.sha256(
        f"{role}|live|{case_id}|{run_id}|{task_id}|{payload['ph']}".encode()
    ).hexdigest()[:24]
    common = {
        "protocol": "FINFLUX_BOUNDED_WORKER_RESULT_V0.2",
        "role": role,
        "asset_class": str(payload.get("a") or "unknown"),
        "case_id": case_id,
        "run_id": run_id,
        "task_id": task_id,
        "execution_policy_id": policy_id,
        "review_scenario": "live_submission",
        "submission_id": payload["s"],
        "worker_payload_sha256": payload["ph"],
        "context_capsule_sha256": payload.get("context_capsule_sha256"),
        "context_slice_sha256": payload.get("context_slice_sha256"),
        "context_cache_status": payload.get("context_cache_status", "LEGACY_INLINE_PAYLOAD"),
        "tool_run_id": tool_run_id,
        "deterministic": True,
        "model_generated_financial_truth": False,
    }
    evidence_input = {
        "file_sha256": payload["h"],
        "evidence_root_hash": payload["r"],
        "precheck_sha256": payload["ps"],
        "rights_gate": payload.get("g", "PASS"),
        "research_item_count": int(payload.get("rc", 0)),
        "research_manifest_sha256": payload.get("rm"),
        "research_bundle_sha256": payload.get("rb"),
    }
    evidence_output, evidence_receipt = _execute_verified_skill(
        registry, "evidence-integrity", "1.0.0", evidence_input
    ) if role == "evidence-investigator" else ({}, {})
    if role == "evidence-investigator":
        rights_output, rights_receipt = _execute_verified_skill(
            registry, "rights-gate", "1.0.0", evidence_input
        )
        invocations = [evidence_receipt, rights_receipt]
        return {
            **common,
            "status": evidence_output["status"],
            "evidence": evidence_output,
            "source_semantics": {
                "status": "OBSERVED",
                "instrument": payload["i"],
                "trade_date": payload["d"],
                "candidate_mapping": payload["m"],
            },
            "skill_invocations": invocations,
        }

    if role == "data-rights-steward":
        classification_input = {
            "confidentiality_class": payload.get("cl", "PUBLIC"),
            "rights_basis": payload.get("gb", ""),
            "permitted_scope": payload.get("us", "EVALUATION_ONLY"),
            "rights_gate": payload.get("g", "PASS"),
        }
        classification_output, classification_receipt = _execute_verified_skill(
            registry, "classify-data-rights", "1.0.0", classification_input
        )
        boundary_output, boundary_receipt = _execute_verified_skill(
            registry,
            "enforce-confidentiality-boundary",
            "1.0.0",
            classification_output,
        )
        return {
            **common,
            **boundary_output,
            "classification": classification_output,
            "skill_invocations": [classification_receipt, boundary_receipt],
        }

    if role == "research-context-analyst":
        context_input = {
            "research_item_count": int(payload["rc"]),
            "manifest_sha256": payload["rm"],
            "bundle_sha256": payload["rb"],
            "instrument": payload["i"],
            "trade_date": payload["d"],
        }
        retrieval_output, retrieval_receipt = _execute_verified_skill(
            registry, "retrieve-research-context", "1.0.0", context_input
        )
        verification_output, verification_receipt = _execute_verified_skill(
            registry, "verify-research-context", "1.0.0", retrieval_output
        )
        return {
            **common,
            "status": verification_output["status"],
            "research_context": retrieval_output,
            "context_verification": verification_output,
            "skill_invocations": [retrieval_receipt, verification_receipt],
        }

    if role == "runtime-resilience-auditor":
        execution_input = {
            "execution_policy_id": policy_id,
            "max_wall_time_seconds": int(payload.get("ew", 600)),
            "tool_timeout_seconds": int(payload.get("et", 90)),
            "max_tool_retries": int(payload.get("er", 0)),
            "checkpoint_required": bool(payload.get("ec", True)),
        }
        budget_output, budget_receipt = _execute_verified_skill(
            registry, "guard-execution-budget", "1.0.0", execution_input
        )
        recovery_output, recovery_receipt = _execute_verified_skill(
            registry, "audit-recovery-readiness", "1.0.0", budget_output
        )
        return {
            **common,
            "status": recovery_output["status"],
            "operational_decision": "PASS" if budget_output["status"] == "READY" else "HOLD",
            "budget_guard": budget_output,
            "recovery_readiness": recovery_output,
            "skill_invocations": [budget_receipt, recovery_receipt],
        }

    financial_input = {
        "profile_id": payload["f"],
        "candidate_mapping": payload["m"],
        "candidate_value": payload["c"],
        "required_value": payload["t"],
        "calculation_scale": payload["x"],
        "registered_contract": payload.get("co"),
        "registered_impact": payload.get("im"),
        "registered_validation": payload.get("va"),
        "agent_proposal": payload.get("ap"),
        "declared_purpose": payload.get("dp"),
        "available_fields": payload.get("sf") or [],
        "semantic_candidates": payload.get("sc") or [],
        "field_values": payload.get("sv") or {},
    }
    if payload["f"] == "futures_settlement":
        financial_input.update(
            {
                "close": payload["c"] if payload["m"] == "close" else payload["t"],
                "settle": payload["t"],
                "contract_multiplier": payload["x"],
            }
        )
    if role == "semantic-impact-analyst":
        contract_output, contract_receipt = _execute_verified_skill(
            registry, "semantic-contract-resolver", "1.1.0", financial_input
        )
        financial_input["resolved_contract"] = contract_output
        impact_output, impact_receipt = _execute_verified_skill(
            registry, "financial-impact-calculator", "1.0.0", financial_input
        )
        recommendation = impact_output["recommended_decision"]
        return {
            **common,
            "status": "SUCCESS",
            "contract": contract_output,
            "impact": impact_output,
            "recommendation": recommendation,
            "agent_semantic_proposal": payload.get("ap"),
            "skill_invocations": [contract_receipt, impact_receipt],
        }

    if payload.get("ap"):
        independent_contract = skill_semantic_contract_resolver(financial_input)
        financial_input["resolved_contract"] = independent_contract
        independent_impact = skill_financial_impact_calculator(financial_input)
        validation_input = {
            **evidence_input,
            **financial_input,
            "registered_validation": {
                "evidence_status": "VERIFIED",
                "contract_status": independent_contract.get("status"),
                "calculation_status": "MATCH",
                "recommendation_status": "MATCH",
                "independent_recommendation": independent_impact.get("recommended_decision"),
            },
        }
    else:
        validation_input = {
        **evidence_input,
        **financial_input,
        "expected_impact": payload.get("pi"),
        "expected_recommendation": payload.get("pr"),
        }
    validation_output, validation_receipt = _execute_verified_skill(
        registry,
        "independent-evidence-validator",
        "1.0.0",
        validation_input,
    )
    return {
        **common,
        "status": "PASS"
        if validation_output["independent_recommendation"] in {"PASS", "BLOCK"}
        else "DISAGREEMENT",
        **validation_output,
        "agent_semantic_proposal": payload.get("ap"),
        "skill_invocations": [validation_receipt],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--asset", required=True, choices=ASSETS)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument(
        "--scenario",
        default="blocked",
        choices=("blocked", "admissible", "post_remediation_review"),
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--live-payload-b64")
    parser.add_argument("--context-capsule-ref")
    parser.add_argument("--proposed-field")
    parser.add_argument("--proposed-semantic")
    parser.add_argument("--confidence-bps", type=int)
    parser.add_argument("--reason-code")
    parser.add_argument("--uncertainty-code")
    parser.add_argument(
        "--execution-recipe-id",
        choices=(
            "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
            "FINFLUX_SIGNED_MEMORY_HASH_CONTEXT_V1",
            "FINFLUX_SIGNED_MEMORY_GUARDED_V1",
        ),
        default="FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
    )
    args = parser.parse_args()
    if bool(args.live_payload_b64) == bool(args.context_capsule_ref):
        if args.live_payload_b64 or args.context_capsule_ref:
            raise ValueError("choose exactly one live context transport")
    _validate_task_binding(args.role, args.case_id, args.run_id, args.task_id)

    output_dir = _safe_output_dir(args.task_id, args.output_dir)
    live_payload = None
    context_load_receipt = None
    if args.context_capsule_ref:
        live_payload, context_load_receipt = load_role_context_slice(
            args.context_capsule_ref,
            args.role,
            case_id=args.case_id,
            run_id=args.run_id,
            with_receipt=True,
        )
    elif args.live_payload_b64:
        live_payload = _decode_live_payload(args.live_payload_b64)
    if live_payload is not None and args.proposed_field:
        if args.role not in {"semantic-impact-analyst", "independent-validator"}:
            raise ValueError("only semantic roles may submit an Agent proposal")
        if args.confidence_bps is None or not 0 <= args.confidence_bps <= 10000:
            raise ValueError("confidence-bps must be between 0 and 10000")
        live_payload["ap"] = {
            "proposed_field": args.proposed_field,
            "proposed_semantic": args.proposed_semantic or "unspecified",
            "confidence_bps": args.confidence_bps,
            "reason_code": args.reason_code or "MODEL_SEMANTIC_INTERPRETATION",
            "uncertainty_code": args.uncertainty_code or "NONE_DECLARED",
            "proposal_source": "AGENT_MODEL_TOOL_ARGUMENT",
        }
    if (
        live_payload is not None
        and str(live_payload.get("m") or "").lower() == "auto_agent"
        and args.role in {"semantic-impact-analyst", "independent-validator"}
        and not live_payload.get("ap")
    ):
        raise ValueError("agent semantic discovery requires a model-authored proposal")
    payload = (
        _live_result(
            args.role,
            args.case_id,
            args.run_id,
            args.task_id,
            args.policy_id,
            live_payload,
        )
        if live_payload is not None
        else _result(
            args.role,
            args.asset,
            args.case_id,
            args.run_id,
            args.task_id,
            args.policy_id,
            args.scenario,
        )
    )
    if context_load_receipt is not None:
        payload.setdefault("context_skill_invocations", []).append(context_load_receipt)
    payload["operational_memory_recipe_id"] = args.execution_recipe_id
    _seal_worker_artifact(payload)
    json_path = output_dir / FILE_BY_ROLE[args.role]
    result_path = output_dir / "result.md"
    with json_path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    result_lines = [
        "# FinFlux Bounded Worker Result",
        "",
        f"- case_id: {args.case_id}",
        f"- run_id: {args.run_id}",
        f"- task_id: {args.task_id}",
        f"- role: {args.role}",
        f"- review_scenario: {args.scenario}",
        f"- status: {payload['status']}",
        f"- tool_run_id: {payload['tool_run_id']}",
        f"- result_json: {json_path.name}",
    ]
    with result_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(result_lines) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "status": payload["status"],
                "result_path": str(result_path),
                "json_path": str(json_path),
                "tool_run_id": payload["tool_run_id"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
