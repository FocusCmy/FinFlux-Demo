from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CAPSULE_PROTOCOL = "FINFLUX_RUN_CONTEXT_CAPSULE_V1.1"
SLICE_PROTOCOL = "FINFLUX_ROLE_CONTEXT_SLICE_V1.1"
BUILD_SKILL = ("build-run-context-capsule", "1.0.0")
LOAD_SKILL = ("load-role-context-slice", "1.0.0")
DEFAULT_SHARED_ROOT = Path(
    "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/context-capsules"
)
EXECUTION_RECIPE_IDS = {
    "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
    "FINFLUX_SIGNED_MEMORY_HASH_CONTEXT_V1",
    "FINFLUX_SIGNED_MEMORY_GUARDED_V1",
}

ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "evidence-investigator": (
        "s", "f", "a", "h", "r", "g", "i", "d", "m", "ps", "rm", "rb", "rc",
    ),
    "semantic-impact-analyst": (
        "s", "f", "a", "h", "r", "g", "i", "d", "c", "t", "m", "x", "ps",
        "co", "im", "q", "dp", "sf", "sc", "sv",
    ),
    "data-rights-steward": (
        "s", "f", "a", "h", "r", "g", "ps", "cl", "gb", "us",
    ),
    "research-context-analyst": (
        "s", "f", "a", "h", "r", "g", "i", "d", "ps", "rm", "rb", "rc",
    ),
    "runtime-resilience-auditor": (
        "s", "f", "a", "h", "r", "g", "ps", "ew", "et", "er", "ec",
    ),
    "independent-validator": (
        "s", "f", "a", "h", "r", "g", "i", "d", "c", "t", "m", "x", "ps", "pi", "pr",
        "co", "im", "va", "q", "dp", "sf", "sc", "sv",
    ),
}


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _skill_invocation(
    skill: tuple[str, str], input_payload: Any, output_payload: Any
) -> dict[str, Any]:
    skill_id, version = skill
    return {
        "skill_id": skill_id,
        "version": version,
        "digest": hashlib.sha256(f"{skill_id}@{version}".encode()).hexdigest(),
        "input_sha256": canonical_sha256(input_payload),
        "output_sha256": canonical_sha256(output_payload),
        "status": "SUCCESS",
        "discovered_at_runtime": True,
        "execution_channel": "ADMISSION_GATEWAY_DETERMINISTIC",
        "provider_tokens": 0,
    }


def _local_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("FINFLUX_CONTEXT_CAPSULE_LOCAL_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent / "runtime" / "context_cache").resolve()


def _write_content_addressed(root: Path, digest: str, payload: dict[str, Any]) -> str:
    target = (root / f"{digest}.json").resolve()
    target.relative_to(root)
    if target.is_file():
        existing = json.loads(target.read_text(encoding="utf-8-sig"))
        if existing != payload:
            raise ValueError("content-addressed context object collision")
        return "HIT"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return "MISS_STORED"


def build_run_context_capsule(
    *,
    case_id: str,
    run_id: str,
    payload: dict[str, Any],
    selected_workers: list[str] | tuple[str, ...],
    execution_policy_id: str,
    root_route_decision_handle: dict[str, Any],
    execution_recipe_id: str = "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
    local_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build an index plus one immutable, physically isolated file per role."""
    if not case_id or not run_id or len(str(payload.get("ph", ""))) != 64:
        raise ValueError("context capsule identity or payload hash is incomplete")
    unsigned_payload = {key: value for key, value in payload.items() if key != "ph"}
    if canonical_sha256(unsigned_payload) != payload["ph"]:
        raise ValueError("context capsule source payload hash mismatch")
    if execution_recipe_id not in EXECUTION_RECIPE_IDS:
        raise ValueError("context capsule execution recipe is not allowlisted")
    workers = list(dict.fromkeys(str(item) for item in selected_workers))
    unsupported = sorted(set(workers) - set(ROLE_KEYS))
    if unsupported:
        raise ValueError(f"context capsule has unsupported roles: {unsupported}")

    fingerprint_input = {
        "source_payload_sha256": payload["ph"],
        "execution_policy_id": execution_policy_id,
        "execution_recipe_id": execution_recipe_id,
        "selected_workers": workers,
        "route_decision_sha256": root_route_decision_handle.get("decision_sha256"),
    }
    context_fingerprint = canonical_sha256(fingerprint_input)
    root = _local_root(local_root)
    root.mkdir(parents=True, exist_ok=True)

    slice_handles: dict[str, dict[str, Any]] = {}
    slice_cache: dict[str, str] = {}
    for role in workers:
        role_payload = {key: payload[key] for key in ROLE_KEYS[role] if key in payload}
        role_payload.update({"p": "FINFLUX_LIVE_WORKER_PAYLOAD_V0.1", "ph": payload["ph"]})
        unsigned_slice = {
            "protocol": SLICE_PROTOCOL,
            "role": role,
            "case_id": case_id,
            "run_id": run_id,
            "context_fingerprint": context_fingerprint,
            "source_payload_sha256": payload["ph"],
            "execution_recipe_id": execution_recipe_id,
            "allowed_keys": sorted(role_payload),
            "payload": role_payload,
        }
        slice_sha256 = canonical_sha256(unsigned_slice)
        role_slice = {**unsigned_slice, "slice_sha256": slice_sha256}
        slice_cache[role] = _write_content_addressed(root, slice_sha256, role_slice)
        slice_handles[role] = {
            "protocol": SLICE_PROTOCOL,
            "role": role,
            "slice_sha256": slice_sha256,
            "shared_path": str((DEFAULT_SHARED_ROOT / f"{slice_sha256}.json").as_posix()),
        }

    build_output = {
        "context_fingerprint": context_fingerprint,
        "role_slice_count": len(slice_handles),
        "role_slice_sha256": {
            role: item["slice_sha256"] for role, item in slice_handles.items()
        },
    }
    build_receipt = _skill_invocation(BUILD_SKILL, fingerprint_input, build_output)
    index_body = {
        "protocol": CAPSULE_PROTOCOL,
        "identity": {
            "case_id": case_id,
            "run_id": run_id,
            "submission_id": payload["s"],
        },
        "context_fingerprint": context_fingerprint,
        "source_payload_sha256": payload["ph"],
        "execution_policy_id": execution_policy_id,
        "execution_recipe_id": execution_recipe_id,
        "root_route_decision_handle": root_route_decision_handle,
        "selected_workers": workers,
        "role_slice_handles": slice_handles,
        "context_skill_invocations": [build_receipt],
        "truth_boundary": {
            "role_payloads_embedded_in_index": False,
            "raw_evidence_in_capsule": False,
            "model_generated_financial_truth": False,
        },
    }
    capsule_sha256 = canonical_sha256(index_body)
    capsule = {**index_body, "capsule_sha256": capsule_sha256}
    index_cache = _write_content_addressed(root, capsule_sha256, capsule)
    handle = {
        "protocol": CAPSULE_PROTOCOL,
        "capsule_sha256": capsule_sha256,
        "context_fingerprint": context_fingerprint,
        "execution_recipe_id": execution_recipe_id,
        "shared_path": str((DEFAULT_SHARED_ROOT / f"{capsule_sha256}.json").as_posix()),
        "role_slice_count": len(slice_handles),
        "role_slice_handles": slice_handles,
        "cache_status": (
            "CAPSULE_HIT"
            if index_cache == "HIT" and all(v == "HIT" for v in slice_cache.values())
            else "MISS_STORED"
        ),
        "within_run_shared_context": True,
        "strict_role_isolation": True,
        "skill_invocation_count": 1,
        "skill_invocations_sha256": canonical_sha256([build_receipt]),
    }
    return capsule, handle


def load_role_context_slice(
    slice_ref: str,
    role: str,
    *,
    case_id: str,
    run_id: str,
    root: Path | None = None,
    with_receipt: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    """Load one role-bound slice; an index hash or another role is rejected."""
    if role not in ROLE_KEYS:
        raise ValueError("role is not allowlisted for context loading")
    if not re.fullmatch(r"[0-9a-f]{64}", slice_ref):
        raise ValueError("context slice reference must be a lowercase SHA-256")
    context_root = Path(
        root or os.environ.get("FINFLUX_CONTEXT_CAPSULE_ROOT", "") or DEFAULT_SHARED_ROOT
    ).resolve()
    target = (context_root / f"{slice_ref}.json").resolve()
    target.relative_to(context_root)
    context_object = json.loads(target.read_text(encoding="utf-8-sig"))
    if context_object.get("protocol") != SLICE_PROTOCOL:
        raise ValueError("context reference is not a role slice")
    declared_hash = str(context_object.pop("slice_sha256", ""))
    if declared_hash != slice_ref or canonical_sha256(context_object) != slice_ref:
        raise ValueError("role context slice hash mismatch")
    if context_object.get("role") != role:
        raise ValueError("role context slice access denied")
    if context_object.get("case_id") != case_id or context_object.get("run_id") != run_id:
        raise ValueError("role context slice is not bound to this case/run")
    execution_recipe_id = str(context_object.get("execution_recipe_id") or "")
    if execution_recipe_id not in EXECUTION_RECIPE_IDS:
        raise ValueError("role context slice execution recipe is invalid")
    payload = dict(context_object.get("payload") or {})
    allowed = set(context_object.get("allowed_keys") or [])
    if set(payload) != allowed or not set(payload).issubset(set(ROLE_KEYS[role]) | {"p", "ph"}):
        raise ValueError("role context slice violates the allowlist")
    payload["context_slice_sha256"] = slice_ref
    payload["context_fingerprint"] = context_object["context_fingerprint"]
    payload["context_cache_status"] = "HIT_ROLE_ISOLATED_SLICE"
    payload["context_execution_recipe_id"] = execution_recipe_id
    receipt = _skill_invocation(
        LOAD_SKILL,
        {"slice_sha256": slice_ref, "role": role, "case_id": case_id, "run_id": run_id},
        {
            "payload_sha256": canonical_sha256(payload),
            "allowed_keys": sorted(allowed),
            "execution_recipe_id": execution_recipe_id,
        },
    )
    receipt["strict_role_isolation"] = True
    return (payload, receipt) if with_receipt else payload


def resolve_role_context_slice(
    slice_ref: str,
    role: str,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Resolve a compact signed Worker invocation from a role-slice hash.

    Matrix and AgentTeams task notifications have finite text budgets.  A
    Worker therefore receives only its role plus the content-addressed slice
    reference; case/run identity and the minimal financial context are read
    from the hash-verified slice itself.  No model-supplied identity is trusted.
    """

    if role not in ROLE_KEYS:
        raise ValueError("role is not allowlisted for context loading")
    if not re.fullmatch(r"[0-9a-f]{64}", slice_ref):
        raise ValueError("context slice reference must be a lowercase SHA-256")
    context_root = Path(
        root or os.environ.get("FINFLUX_CONTEXT_CAPSULE_ROOT", "") or DEFAULT_SHARED_ROOT
    ).resolve()
    target = (context_root / f"{slice_ref}.json").resolve()
    target.relative_to(context_root)
    context_object = json.loads(target.read_text(encoding="utf-8-sig"))
    case_id = str(context_object.get("case_id") or "")
    run_id = str(context_object.get("run_id") or "")
    payload = load_role_context_slice(
        slice_ref,
        role,
        case_id=case_id,
        run_id=run_id,
        root=context_root,
    )
    return {
        "role": role,
        "case_id": case_id,
        "run_id": run_id,
        "payload": payload,
    }
