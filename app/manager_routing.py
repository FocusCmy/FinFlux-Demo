from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "FINFLUX_ROOT_ROUTE_DECISION_V0.5"
POLICY_ID = "FINFLUX_MANAGER_ROUTE_POLICY"
POLICY_VERSION = "0.5.0"
MANAGER_ROUTE_SKILL_ID = "manager-route-case"
MANAGER_ROUTE_SKILL_VERSION = "1.0.0"
MANAGER_SKILL_MANIFEST_PROTOCOL = "FINFLUX_MANAGER_SKILL_MANIFEST_V1.0"
DEFAULT_MANAGER_SKILL_MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "agentteams"
    / "config"
    / "manager-route-skill-manifest.json"
)

SKILL_VERSIONS = {
    "evidence-integrity": "1.0.0",
    "rights-gate": "1.0.0",
    "semantic-contract-resolver": "1.1.0",
    "financial-impact-calculator": "1.0.0",
    "independent-evidence-validator": "1.0.0",
    "detect-version-change": "1.0.0",
    "resolve-downstream-lineage": "1.0.0",
    "validate-remediation-plan": "1.0.0",
    "classify-data-rights": "1.0.0",
    "enforce-confidentiality-boundary": "1.0.0",
    "retrieve-research-context": "1.0.0",
    "verify-research-context": "1.0.0",
    "guard-execution-budget": "1.0.0",
    "audit-recovery-readiness": "1.0.0",
}

CORE_FULL_REVIEW_SKILLS = (
    "evidence-integrity",
    "rights-gate",
    "semantic-contract-resolver",
    "financial-impact-calculator",
    "independent-evidence-validator",
)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manager_route_skill(
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load and verify the versioned deterministic Manager routing Skill."""
    configured = os.environ.get("FINFLUX_MANAGER_SKILL_MANIFEST", "").strip()
    path = Path(manifest_path or configured or DEFAULT_MANAGER_SKILL_MANIFEST).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    declared_manifest_sha = str(manifest.pop("manifest_sha256", ""))
    actual_manifest_sha = canonical_sha256(manifest)
    if declared_manifest_sha != actual_manifest_sha:
        raise ValueError("Manager Skill manifest digest mismatch")
    if manifest.get("protocol") != MANAGER_SKILL_MANIFEST_PROTOCOL:
        raise ValueError("unsupported Manager Skill manifest protocol")
    if (
        manifest.get("skill_id") != MANAGER_ROUTE_SKILL_ID
        or manifest.get("version") != MANAGER_ROUTE_SKILL_VERSION
    ):
        raise ValueError("Manager Skill id/version mismatch")
    root = path.parent.parent.parent.resolve()
    entrypoint = (root / str(manifest.get("entrypoint", {}).get("path", ""))).resolve()
    instruction = (root / str(manifest.get("instruction", {}).get("path", ""))).resolve()
    entrypoint.relative_to(root)
    instruction.relative_to(root)
    if _file_sha256(entrypoint) != manifest["entrypoint"]["sha256"]:
        raise ValueError("Manager Skill entrypoint digest mismatch")
    if _file_sha256(instruction) != manifest["instruction"]["sha256"]:
        raise ValueError("Manager Skill instruction digest mismatch")
    return {
        **manifest,
        "manifest_sha256": declared_manifest_sha,
        "manifest_path": str(path),
        "loaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def decide_root_route(facts: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, auditable Manager root-route decision.

    The policy selects who must investigate; it never computes or invents a
    financial value.  Financial truth remains in versioned deterministic
    Skills and immutable evidence handles.
    """

    normalized = {
        "case_id": str(facts.get("case_id", "")),
        "run_id": str(facts.get("run_id", "")),
        "submission_id": str(facts.get("submission_id", "")),
        "asset_class": str(facts.get("asset_class", "")),
        "evidence_profile": str(facts.get("evidence_profile", "")),
        "declared_downstream_use": str(facts.get("declared_downstream_use", "")),
        "rights_status": str(facts.get("rights_status", "MISSING")).upper(),
        "evidence_status": str(facts.get("evidence_status", "MISSING")).upper(),
        "evidence_hash_valid": bool(facts.get("evidence_hash_valid", False)),
        "precheck_recommendation": str(
            facts.get("precheck_recommendation", "PENDING")
        ).upper(),
        # Manager receives only the gateway's categorical semantic outcome.
        # Candidate/required field values, prices and calculated amounts stay
        # behind deterministic financial Skills and are never normalized or
        # hashed by the Manager control plane.
        "semantic_conflict_code": str(
            facts.get("semantic_conflict_code")
            or (
                "SEMANTIC_MAPPING_ALIGNED"
                if str(facts.get("precheck_recommendation", "")).upper() == "PASS"
                else "SEMANTIC_MAPPING_CONFLICT"
                if str(facts.get("precheck_recommendation", "")).upper() == "BLOCK"
                else "SEMANTIC_CONTRACT_INCOMPLETE"
            )
        ).upper(),
        "precheck_sha256": str(facts.get("precheck_sha256", "")),
        "evidence_sha256": str(facts.get("evidence_sha256", "")),
        "evidence_root_hash": str(facts.get("evidence_root_hash", "")),
        "budget_available": bool(facts.get("budget_available", True)),
        "review_mode": str(facts.get("review_mode", "STANDARD")),
        "change_bundle_id": str(facts.get("change_bundle_id", "")),
        "change_count": int(facts.get("change_count", 0) or 0),
        "affected_task_count": int(facts.get("affected_task_count", 0) or 0),
        "unknown_impact_task_count": int(
            facts.get("unknown_impact_task_count", 0) or 0
        ),
        "financial_semantic_change": bool(
            facts.get("financial_semantic_change", False)
        ),
        "confidentiality_class": str(
            facts.get("confidentiality_class", "PUBLIC")
        ).upper(),
        "rights_review_required": bool(facts.get("rights_review_required", False)),
        "research_context_required": bool(
            facts.get("research_context_required", False)
        ),
        "operational_risk_review_required": bool(
            facts.get("operational_risk_review_required", False)
        ),
        "semantic_discovery_requested": bool(
            facts.get("semantic_discovery_requested", False)
        ),
    }
    input_sha256 = canonical_sha256(normalized)

    route = "FULL_TEAM_REVIEW"
    recommendation = "BLOCK"
    reason_codes: list[str] = []
    workers = [
        "Evidence Investigator",
        "Semantic Impact Analyst",
        "Independent Validator",
    ]
    skills = list(CORE_FULL_REVIEW_SKILLS)

    if normalized["rights_status"] != "PASS":
        route = "REJECT_AT_INTAKE"
        recommendation = "REJECT"
        reason_codes = ["RIGHTS_GATE_NOT_PASS"]
        workers = []
        skills = ["rights-gate"]
    elif (
        normalized["evidence_status"] != "VERIFIED"
        or not normalized["evidence_hash_valid"]
    ):
        route = "NEEDS_EVIDENCE"
        recommendation = "NEEDS_EVIDENCE"
        reason_codes = ["EVIDENCE_INTEGRITY_INCOMPLETE"]
        workers = ["Evidence Investigator"]
        skills = ["evidence-integrity", "rights-gate"]
    elif (
        normalized["semantic_discovery_requested"]
        and normalized["declared_downstream_use"]
    ):
        route = "FULL_TEAM_REVIEW"
        recommendation = "NEEDS_EVIDENCE"
        reason_codes = [
            "AGENT_SEMANTIC_DISCOVERY_REQUESTED",
            "INDEPENDENT_SEMANTIC_REVIEW_REQUIRED",
            "HUMAN_FINAL_AUTHORITY",
        ]
    elif (
        not normalized["declared_downstream_use"]
        or normalized["semantic_conflict_code"] == "SEMANTIC_CONTRACT_INCOMPLETE"
    ):
        route = "NEEDS_EVIDENCE"
        recommendation = "NEEDS_EVIDENCE"
        reason_codes = ["SEMANTIC_CONTRACT_INCOMPLETE"]
        workers = ["Evidence Investigator", "Semantic Impact Analyst"]
        skills = [
            "evidence-integrity",
            "rights-gate",
            "semantic-contract-resolver",
        ]
    elif not normalized["budget_available"]:
        route = "HOLD_FOR_BUDGET"
        recommendation = "HOLD"
        reason_codes = ["EXECUTION_BUDGET_UNAVAILABLE"]
        workers = []
        skills = []
    elif normalized["change_bundle_id"] and (
        normalized["affected_task_count"] > 0
        or normalized["unknown_impact_task_count"] > 0
    ):
        route = "BLAST_RADIUS_REVIEW"
        recommendation = "HOLD"
        reason_codes = ["VERSION_CHANGE_REQUIRES_IMPACT_REVIEW"]
        workers = ["Evidence Investigator", "Downstream Impact Analyst"]
        skills = [
            "evidence-integrity",
            "rights-gate",
            "detect-version-change",
            "resolve-downstream-lineage",
        ]
        if normalized["financial_semantic_change"]:
            workers.append("Semantic Impact Analyst")
            skills.extend(
                ["semantic-contract-resolver", "financial-impact-calculator"]
            )
            reason_codes.append("FINANCIAL_SEMANTIC_CHANGE")
        workers.append("Independent Validator")
        # validate-remediation-plan is intentionally absent from the first
        # blast-radius review.  It is only a required receipt after a concrete
        # remediation plan exists; claiming it here would make a truthful first
        # review impossible to attest.
        skills.append("independent-evidence-validator")
        if normalized["unknown_impact_task_count"]:
            reason_codes.append("UNKNOWN_LINEAGE_REQUIRES_HUMAN")
    elif (
        normalized["review_mode"] == "POST_REMEDIATION_REVIEW"
        and normalized["semantic_conflict_code"] == "SEMANTIC_MAPPING_ALIGNED"
        and normalized["precheck_recommendation"] == "PASS"
    ):
        route = "FULL_TEAM_REVIEW"
        recommendation = "PASS"
        reason_codes = [
            "HUMAN_REMEDIATION_REVIEW",
            "SEMANTIC_MAPPING_ALIGNED",
            "CROSS_ROLE_REVIEW_REQUIRED",
        ]
    elif (
        normalized["semantic_conflict_code"] == "SEMANTIC_MAPPING_ALIGNED"
        and normalized["precheck_recommendation"] == "PASS"
    ):
        route = "CODE_ONLY_PRECHECK"
        recommendation = "PASS"
        reason_codes = ["SEMANTIC_MAPPING_ALIGNED", "NO_MODEL_REQUIRED"]
        workers = []
        skills = [
            "evidence-integrity",
            "rights-gate",
            "semantic-contract-resolver",
            "financial-impact-calculator",
            "independent-evidence-validator",
        ]
    else:
        reason_codes = ["SEMANTIC_MAPPING_CONFLICT", "CROSS_ROLE_REVIEW_REQUIRED"]

    # V0.5 keeps the admission path intentionally small.  Rights,
    # confidentiality, research and runtime-risk facts remain immutable input
    # handles, but they no longer create mandatory model roles.  The previous
    # policy promoted every optional concern into the critical path and made a
    # valid three-role financial review wait forever for unrelated specialists.
    # A later, explicitly requested follow-up Run may route those concerns; the
    # admission Run is always Evidence -> Semantic Impact -> Independent Review.

    workers = list(dict.fromkeys(workers))
    skills = list(dict.fromkeys(skills))

    result = {
        "protocol": PROTOCOL,
        "decision_id": f"RRD-{input_sha256[:16].upper()}",
        "case_id": normalized["case_id"],
        "run_id": normalized["run_id"],
        "submission_id": normalized["submission_id"],
        "policy": {
            "policy_id": POLICY_ID,
            "version": POLICY_VERSION,
            "generated_by_model": False,
            "financial_truth_computed_by_manager": False,
        },
        "input_facts": normalized,
        "input_facts_sha256": input_sha256,
        "route": route,
        "machine_recommendation": recommendation,
        "reason_codes": reason_codes,
        "worker_plan": {
            "count": len(workers),
            "workers": workers,
            "parallel": len(workers) > 1,
        },
        "required_skill_versions": {
            name: SKILL_VERSIONS[name] for name in skills
        },
        "manager_boundary": (
            "Manager routes immutable evidence and context; it does not read raw "
            "financial truth or perform financial calculations."
        ),
    }
    result["decision_sha256"] = canonical_sha256(result)
    return result


def build_live_root_route_decision(
    submission: dict[str, Any], live_run: dict[str, Any]
) -> dict[str, Any]:
    parsed = submission.get("parsed") or {}
    metadata = submission.get("metadata") or {}
    precheck = live_run.get("precheck") or {}
    file_info = submission.get("file") or {}
    rights = submission.get("rights_gate") or {}
    file_sha = str(file_info.get("sha256", ""))
    root_hash = str(submission.get("evidence_root_hash", ""))
    precheck_sha = str(precheck.get("sha256", ""))
    return decide_root_route(
        {
            "case_id": live_run.get("case_id"),
            "run_id": live_run.get("run_id"),
            "submission_id": submission.get("submission_id"),
            "asset_class": metadata.get("asset_class") or "unknown",
            "evidence_profile": submission.get("profile"),
            "declared_downstream_use": metadata.get("declared_purpose"),
            "rights_status": rights.get("status"),
            "evidence_status": submission.get("status"),
            "evidence_hash_valid": all(
                len(value) == 64 for value in (file_sha, root_hash, precheck_sha)
            ),
            "precheck_recommendation": precheck.get("machine_recommendation"),
            "semantic_conflict_code": (
                "SEMANTIC_MAPPING_ALIGNED"
                if precheck.get("machine_recommendation") == "PASS"
                else "SEMANTIC_MAPPING_CONFLICT"
                if precheck.get("machine_recommendation") == "BLOCK"
                else "SEMANTIC_CONTRACT_INCOMPLETE"
            ),
            "precheck_sha256": precheck_sha,
            "evidence_sha256": file_sha,
            "evidence_root_hash": root_hash,
            "budget_available": True,
            "instrument": parsed.get("instrument"),
            "review_mode": metadata.get("review_mode", "STANDARD"),
            "confidentiality_class": metadata.get(
                "confidentiality_class", "PUBLIC"
            ),
            "rights_review_required": metadata.get(
                "rights_review_required", False
            ),
            "research_context_required": metadata.get(
                "research_context_required", False
            ),
            "operational_risk_review_required": metadata.get(
                "operational_risk_review_required", False
            ),
            "semantic_discovery_requested": str(
                metadata.get("candidate_mapping") or ""
            ).lower() == "auto_agent",
        }
    )


def execute_manager_route_skill(
    facts: dict[str, Any], manifest_path: Path | str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute deterministic routing and emit a post-success invocation receipt.

    The receipt is deliberately created only after the RouteDecision hash has
    been computed. The input is the normalized routing-facts envelope; no raw
    financial evidence or financial-value calculation is available here.
    """
    loaded = load_manager_route_skill(manifest_path)
    decision = decide_root_route(facts)
    receipt = {
        "protocol": "FINFLUX_MANAGER_SKILL_INVOCATION_RECEIPT_V1.0",
        "skill_id": loaded["skill_id"],
        "version": loaded["version"],
        "manifest_sha256": loaded["manifest_sha256"],
        "entrypoint_sha256": loaded["entrypoint"]["sha256"],
        "instruction_sha256": loaded["instruction"]["sha256"],
        "loaded_at_utc": loaded["loaded_at_utc"],
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_facts_sha256": decision["input_facts_sha256"],
        "route_decision_sha256": decision["decision_sha256"],
        "decision_id": decision["decision_id"],
        "status": "SUCCESS",
        "discovered_at_runtime": True,
        "execution_channel": "MANAGER_CONTROL_PLANE_DETERMINISTIC_SKILL",
        "provider_tokens": 0,
        "raw_financial_values_read": False,
        "financial_truth_computed_by_manager": False,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return decision, receipt


def build_change_root_route_decision(
    change_bundle: dict[str, Any],
    candidate_submission: dict[str, Any],
    live_run: dict[str, Any],
) -> dict[str, Any]:
    """Route a version-change Case without letting Manager infer finance facts."""
    change_set = change_bundle.get("change_set") or {}
    impact = change_bundle.get("impact_graph") or {}
    summary = impact.get("summary") or {}
    changed_paths = set(change_set.get("changed_paths") or [])
    metadata = candidate_submission.get("metadata") or {}
    rights = candidate_submission.get("rights_gate") or {}
    file_info = candidate_submission.get("file") or {}
    precheck = live_run.get("precheck") or {}
    return decide_root_route(
        {
            "case_id": live_run.get("case_id"),
            "run_id": live_run.get("run_id"),
            "submission_id": candidate_submission.get("submission_id"),
            "asset_class": "futures",
            "evidence_profile": candidate_submission.get("profile"),
            "declared_downstream_use": metadata.get("declared_purpose"),
            "rights_status": rights.get("status"),
            "evidence_status": candidate_submission.get("status"),
            "evidence_hash_valid": all(
                len(str(value)) == 64
                for value in (
                    file_info.get("sha256", ""),
                    candidate_submission.get("evidence_root_hash", ""),
                    precheck.get("sha256", ""),
                )
            ),
            "precheck_recommendation": precheck.get("machine_recommendation"),
            "semantic_conflict_code": (
                "SEMANTIC_MAPPING_ALIGNED"
                if precheck.get("machine_recommendation") == "PASS"
                else "SEMANTIC_MAPPING_CONFLICT"
                if precheck.get("machine_recommendation") == "BLOCK"
                else "SEMANTIC_CONTRACT_INCOMPLETE"
            ),
            "precheck_sha256": precheck.get("sha256"),
            "evidence_sha256": file_info.get("sha256"),
            "evidence_root_hash": candidate_submission.get("evidence_root_hash"),
            "budget_available": True,
            "review_mode": "VERSION_CHANGE_REVIEW",
            "change_bundle_id": change_bundle.get("change_bundle_id"),
            "change_count": change_set.get("change_count", 0),
            "affected_task_count": summary.get("affected_tasks", 0),
            "unknown_impact_task_count": summary.get("unknown_impact_tasks", 0),
            "financial_semantic_change": bool(
                {"metadata.candidate_mapping", "metadata.declared_purpose"}
                & changed_paths
            ),
        }
    )
