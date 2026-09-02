from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _flatten(prefix: str, value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(child, value[key]))
        return result
    if isinstance(value, list):
        return {prefix: value}
    return {prefix: value}


def _bounded_submission_view(submission: dict[str, Any]) -> dict[str, Any]:
    """Keep only immutable evidence facts relevant to change admission."""
    return {
        "profile": submission.get("profile"),
        "file": {
            "name": (submission.get("file") or {}).get("name"),
            "sha256": (submission.get("file") or {}).get("sha256"),
            "size_bytes": (submission.get("file") or {}).get("size_bytes"),
        },
        "metadata": submission.get("metadata") or {},
        "parsed": submission.get("parsed") or {},
        "rights_gate": submission.get("rights_gate") or {},
        "evidence_root_hash": submission.get("evidence_root_hash"),
        "raw_evidence_mutated": bool(submission.get("raw_evidence_mutated")),
    }


def detect_version_change(
    baseline_submission: dict[str, Any], candidate_submission: dict[str, Any]
) -> dict[str, Any]:
    """Create an observed-only ChangeSet; never assign financial meaning."""
    baseline = _bounded_submission_view(baseline_submission)
    candidate = _bounded_submission_view(candidate_submission)
    if baseline.get("profile") != candidate.get("profile"):
        raise ValueError("baseline and candidate profiles must match")
    if baseline.get("raw_evidence_mutated") or candidate.get("raw_evidence_mutated"):
        raise ValueError("mutated raw evidence is not admissible")

    left = _flatten("", baseline)
    right = _flatten("", candidate)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        before = left.get(path)
        after = right.get(path)
        if before == after:
            continue
        if path.startswith("parsed."):
            category = "OBSERVED_DATA_CHANGE"
        elif path.startswith("metadata."):
            category = "INGRESS_CONFIGURATION_CHANGE"
        elif path.startswith("file.") or path == "evidence_root_hash":
            category = "EVIDENCE_VERSION_CHANGE"
        elif path.startswith("rights_gate."):
            category = "RIGHTS_CHANGE"
        else:
            category = "OTHER_OBSERVED_CHANGE"
        changes.append(
            {
                "path": path,
                "before": before,
                "after": after,
                "category": category,
            }
        )

    payload = {
        "protocol": "FINFLUX_CHANGE_SET_V1.0",
        "profile": baseline.get("profile"),
        "baseline_submission_id": baseline_submission.get("submission_id"),
        "candidate_submission_id": candidate_submission.get("submission_id"),
        "baseline_evidence_sha256": (baseline.get("file") or {}).get("sha256"),
        "candidate_evidence_sha256": (candidate.get("file") or {}).get("sha256"),
        "raw_file_changed": (baseline.get("file") or {}).get("sha256")
        != (candidate.get("file") or {}).get("sha256"),
        "changed_paths": [item["path"] for item in changes],
        "changes": changes,
        "change_count": len(changes),
        "observed_only": True,
        "financial_decision": "NOT_PERFORMED",
    }
    payload["change_set_sha256"] = canonical_sha256(payload)
    payload["change_id"] = f"CHG-{payload['change_set_sha256'][:16].upper()}"
    return payload


def resolve_downstream_lineage(
    change_set: dict[str, Any], downstream_tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Resolve blast radius from explicit task dependencies only."""
    if not downstream_tasks:
        raise ValueError("at least one downstream task manifest is required")
    changed = set(change_set.get("changed_paths") or [])
    nodes: list[dict[str, Any]] = []
    affected_count = 0
    unknown_count = 0
    for raw in downstream_tasks:
        task_id = str(raw.get("task_id", "")).strip()
        if not task_id:
            raise ValueError("each downstream task requires task_id")
        dependencies = [str(item).strip() for item in raw.get("dependencies", []) if str(item).strip()]
        matched = sorted(
            path
            for path in changed
            if any(
                dep == "*" or path == dep or path.startswith(dep + ".")
                for dep in dependencies
            )
        )
        if not dependencies:
            state = "UNKNOWN_IMPACT"
            unknown_count += 1
        elif matched:
            state = "AFFECTED"
            affected_count += 1
        else:
            state = "NOT_AFFECTED_BY_DECLARED_DEPENDENCIES"
        nodes.append(
            {
                "task_id": task_id,
                "label": str(raw.get("label", task_id)),
                "owner": str(raw.get("owner", "UNASSIGNED")),
                "purpose": str(raw.get("purpose", "UNDECLARED")),
                "criticality": str(raw.get("criticality", "UNKNOWN")).upper(),
                "dependencies": dependencies,
                "matched_changes": matched,
                "impact_state": state,
            }
        )

    result = {
        "protocol": "FINFLUX_IMPACT_GRAPH_V1.0",
        "change_id": change_set.get("change_id"),
        "change_set_sha256": change_set.get("change_set_sha256"),
        "nodes": nodes,
        "summary": {
            "total_tasks": len(nodes),
            "affected_tasks": affected_count,
            "unknown_impact_tasks": unknown_count,
            "safe_to_claim_no_impact": unknown_count == 0 and affected_count == 0,
        },
        "truth_boundary": "Impact is derived only from declared task dependencies; missing lineage is UNKNOWN_IMPACT.",
    }
    result["impact_graph_sha256"] = canonical_sha256(result)
    return result


def validate_remediation_plan(
    baseline_submission: dict[str, Any],
    remediation_submission: dict[str, Any],
    impact_graph: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Validate a proposed configuration repair without approving production."""
    expected_mapping = str(plan.get("expected_candidate_mapping", "")).strip()
    rollback_submission_id = str(plan.get("rollback_submission_id", "")).strip()
    actions = {
        str(item.get("task_id")): str(item.get("action", "")).upper()
        for item in plan.get("task_actions", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    failures: list[str] = []
    if not expected_mapping:
        failures.append("EXPECTED_MAPPING_MISSING")
    elif str((remediation_submission.get("metadata") or {}).get("candidate_mapping")) != expected_mapping:
        failures.append("EXPECTED_MAPPING_NOT_APPLIED")
    if rollback_submission_id != str(baseline_submission.get("submission_id")):
        failures.append("ROLLBACK_REFERENCE_INVALID")
    unresolved_tasks: list[str] = []
    for node in impact_graph.get("nodes", []):
        if node.get("impact_state") in {"AFFECTED", "UNKNOWN_IMPACT"}:
            task_id = str(node.get("task_id"))
            if actions.get(task_id) not in {"RECOMPUTE", "ISOLATE", "ACCEPT_WITH_EVIDENCE"}:
                unresolved_tasks.append(task_id)
    if unresolved_tasks:
        failures.append("DOWNSTREAM_ACTION_MISSING")

    result = {
        "protocol": "FINFLUX_REMEDIATION_VALIDATION_V1.0",
        "baseline_submission_id": baseline_submission.get("submission_id"),
        "remediation_submission_id": remediation_submission.get("submission_id"),
        "expected_candidate_mapping": expected_mapping,
        "rollback_submission_id": rollback_submission_id or None,
        "unresolved_tasks": unresolved_tasks,
        "status": "VALIDATED_FOR_REVIEW" if not failures else "NEEDS_REVISION",
        "failures": failures,
        "production_approved": False,
        "human_gate_required": True,
        "truth_boundary": "This validates plan completeness; only an authorized Human may approve admission.",
    }
    result["validation_sha256"] = canonical_sha256(result)
    return result

