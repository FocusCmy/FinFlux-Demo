from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = "0.2.0"
CASE_ENVELOPE_PROTOCOL = "FINFLUX_CASE_ENVELOPE_V0.2"
DATAPASS_PROTOCOL = "FINFLUX_DATAPASS_V0.2"
PROFILE_REGISTRY_PROTOCOL = "FINFLUX_PROFILE_REGISTRY_V0.2"

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
_BUNDLED_PROFILE_REGISTRY_PATH = APP_ROOT / "profile_registry_v0.2.json"
PROFILE_REGISTRY_PATH = (
    _BUNDLED_PROFILE_REGISTRY_PATH
    if _BUNDLED_PROFILE_REGISTRY_PATH.is_file()
    else PROJECT_ROOT / "agentteams" / "config" / "profile_registry_v0.2.json"
)

PROFILE_BY_ASSET = {
    "equity": "equity_corporate_action",
    "futures": "futures_settlement",
    "fund": "fund_nav_admission",
    "option": "option_contract_identity",
    "research": "research_material_rights",
}

# DataPass V0.2 attests an exact, frozen Skill identity, not merely a display
# name. The owner is part of the contract: a receipt produced by another
# Worker is not interchangeable, even when its input/output hashes are valid.
_SKILL_CONTRACTS: dict[str, tuple[str, str]] = {
    "audit-recovery-readiness": ("1.0.0", "runtime-resilience-auditor"),
    "classify-data-rights": ("1.0.0", "data-rights-steward"),
    "enforce-confidentiality-boundary": ("1.0.0", "data-rights-steward"),
    "evidence-integrity": ("1.0.0", "evidence-investigator"),
    "financial-impact-calculator": ("1.0.0", "semantic-impact-analyst"),
    "guard-execution-budget": ("1.0.0", "runtime-resilience-auditor"),
    "independent-evidence-validator": ("1.0.0", "independent-validator"),
    "retrieve-research-context": ("1.0.0", "research-context-analyst"),
    "rights-gate": ("1.0.0", "evidence-investigator"),
    "semantic-contract-resolver": ("1.1.0", "semantic-impact-analyst"),
    "verify-research-context": ("1.0.0", "research-context-analyst"),
}

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DECISIONS = {"PASS", "BLOCK", "NEEDS_EVIDENCE"}
_ROUTES = {
    "CODE_ONLY_PASS",
    "HUMAN_REVIEW_WITHOUT_IMPACT",
    "FULL_TEAM_REVIEW",
    "POST_REMEDIATION_FULL_TEAM_REVIEW",
    "PRESERVE_DISPUTE_AND_ESCALATE",
    "BLAST_RADIUS_REVIEW",
}
_RIGHTS_STATES = {"PUBLIC", "AUTHORIZED", "RESTRICTED", "REVIEW_REQUIRED"}


class ProtocolValidationError(ValueError):
    """Fail-closed protocol error with a stable field path."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolValidationError(field, "must be an object")
    return value


def _strict_keys(
    value: dict[str, Any],
    field: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise ProtocolValidationError(field, f"missing fields: {', '.join(missing)}")
    if unknown:
        raise ProtocolValidationError(field, f"unknown fields: {', '.join(unknown)}")


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ProtocolValidationError(field, "must be a non-empty string")
    return value


def _require_string_list(value: Any, field: str, *, min_items: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        raise ProtocolValidationError(field, f"must contain at least {min_items} item(s)")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProtocolValidationError(field, "must contain only non-empty strings")
    if len(set(value)) != len(value):
        raise ProtocolValidationError(field, "must not contain duplicates")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProtocolValidationError(field, "must be a lowercase SHA256 digest")
    return value


def _require_datetime(value: Any, field: str) -> str:
    text = _require_string(value, field)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolValidationError(field, "must be an ISO-8601 timestamp") from exc
    return text


def _unsigned(value: dict[str, Any], hash_field: str) -> dict[str, Any]:
    return {key: copy.deepcopy(item) for key, item in value.items() if key != hash_field}


def _validate_self_hash(value: dict[str, Any], hash_field: str, field: str) -> None:
    declared = _require_sha256(value.get(hash_field), f"{field}.{hash_field}")
    actual = canonical_sha256(_unsigned(value, hash_field))
    if declared != actual:
        raise ProtocolValidationError(f"{field}.{hash_field}", "does not match canonical payload")


def _validate_profile(profile: Any, field: str) -> dict[str, Any]:
    profile = _require_object(profile, field)
    _strict_keys(
        profile,
        field,
        (
            "profile_id",
            "profile_version",
            "asset_class",
            "display_name",
            "semantic_contract",
            "declared_purposes",
            "evidence_requirements",
            "accepted_evidence_types",
            "required_outputs",
            "display_fields",
            "profile_sha256",
        ),
    )
    _require_string(profile["profile_id"], f"{field}.profile_id")
    _require_string(profile["profile_version"], f"{field}.profile_version")
    _require_string(profile["asset_class"], f"{field}.asset_class")
    _require_string(profile["display_name"], f"{field}.display_name")

    contract = _require_object(profile["semantic_contract"], f"{field}.semantic_contract")
    _strict_keys(contract, f"{field}.semantic_contract", ("contract_id", "version"))
    _require_string(contract["contract_id"], f"{field}.semantic_contract.contract_id")
    _require_string(contract["version"], f"{field}.semantic_contract.version")

    purposes = profile["declared_purposes"]
    if not isinstance(purposes, list) or not purposes:
        raise ProtocolValidationError(f"{field}.declared_purposes", "must be a non-empty list")
    purpose_ids: list[str] = []
    for index, purpose in enumerate(purposes):
        item_field = f"{field}.declared_purposes[{index}]"
        purpose = _require_object(purpose, item_field)
        _strict_keys(
            purpose,
            item_field,
            ("purpose_id", "label", "target_decision", "default_accountable_role"),
        )
        purpose_ids.append(_require_string(purpose["purpose_id"], f"{item_field}.purpose_id"))
        for key in ("label", "target_decision", "default_accountable_role"):
            _require_string(purpose[key], f"{item_field}.{key}")
    if len(set(purpose_ids)) != len(purpose_ids):
        raise ProtocolValidationError(f"{field}.declared_purposes", "purpose_id must be unique")

    requirements = profile["evidence_requirements"]
    if not isinstance(requirements, list) or not requirements:
        raise ProtocolValidationError(f"{field}.evidence_requirements", "must be a non-empty list")
    requirement_types: list[str] = []
    for index, requirement in enumerate(requirements):
        item_field = f"{field}.evidence_requirements[{index}]"
        requirement = _require_object(requirement, item_field)
        _strict_keys(
            requirement,
            item_field,
            ("evidence_type", "min_count", "content_hash_required", "description"),
        )
        evidence_type = _require_string(requirement["evidence_type"], f"{item_field}.evidence_type")
        requirement_types.append(evidence_type)
        if not isinstance(requirement["min_count"], int) or requirement["min_count"] < 1:
            raise ProtocolValidationError(f"{item_field}.min_count", "must be an integer >= 1")
        if requirement["content_hash_required"] is not True:
            raise ProtocolValidationError(
                f"{item_field}.content_hash_required", "P0 profiles require immutable content hashes"
            )
        _require_string(requirement["description"], f"{item_field}.description")

    accepted = _require_string_list(
        profile["accepted_evidence_types"], f"{field}.accepted_evidence_types", min_items=1
    )
    if not set(requirement_types).issubset(set(accepted)):
        raise ProtocolValidationError(
            f"{field}.accepted_evidence_types", "must include every required evidence type"
        )
    _require_string_list(profile["required_outputs"], f"{field}.required_outputs", min_items=3)

    display_fields = profile["display_fields"]
    if not isinstance(display_fields, list) or not display_fields:
        raise ProtocolValidationError(f"{field}.display_fields", "must be a non-empty list")
    display_ids: list[str] = []
    for index, display in enumerate(display_fields):
        item_field = f"{field}.display_fields[{index}]"
        display = _require_object(display, item_field)
        _strict_keys(display, item_field, ("field_id", "label", "value_type", "description"))
        display_ids.append(_require_string(display["field_id"], f"{item_field}.field_id"))
        for key in ("label", "value_type", "description"):
            _require_string(display[key], f"{item_field}.{key}")
    if len(set(display_ids)) != len(display_ids):
        raise ProtocolValidationError(f"{field}.display_fields", "field_id must be unique")
    _validate_self_hash(profile, "profile_sha256", field)
    return profile


def load_profile_registry(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or PROFILE_REGISTRY_PATH
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError("profile_registry", f"cannot load {registry_path}: {exc}") from exc
    registry = _require_object(registry, "profile_registry")
    _strict_keys(
        registry,
        "profile_registry",
        ("protocol", "schema_version", "profiles", "registry_sha256"),
    )
    if registry["protocol"] != PROFILE_REGISTRY_PROTOCOL:
        raise ProtocolValidationError("profile_registry.protocol", "unsupported registry protocol")
    if registry["schema_version"] != PROTOCOL_VERSION:
        raise ProtocolValidationError("profile_registry.schema_version", "unsupported schema version")
    profiles = registry["profiles"]
    if not isinstance(profiles, list) or len(profiles) != len(PROFILE_BY_ASSET):
        raise ProtocolValidationError(
            "profile_registry.profiles",
            f"registry must contain exactly {len(PROFILE_BY_ASSET)} governed profiles",
        )
    ids: list[str] = []
    for index, profile in enumerate(profiles):
        validated = _validate_profile(profile, f"profile_registry.profiles[{index}]")
        ids.append(validated["profile_id"])
    if len(set(ids)) != len(ids):
        raise ProtocolValidationError("profile_registry.profiles", "profile_id must be unique")
    if set(ids) != set(PROFILE_BY_ASSET.values()):
        raise ProtocolValidationError("profile_registry.profiles", "governed profile set is incomplete")
    _validate_self_hash(registry, "registry_sha256", "profile_registry")
    return registry


def resolve_profile(profile_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_profile_registry()
    for profile in registry["profiles"]:
        if profile["profile_id"] == profile_id:
            return copy.deepcopy(profile)
    raise ProtocolValidationError("profile_id", f"unknown profile {profile_id!r}; execution is denied")


def _purpose(profile: dict[str, Any], purpose_id: str) -> dict[str, Any]:
    for purpose in profile["declared_purposes"]:
        if purpose["purpose_id"] == purpose_id:
            return purpose
    raise ProtocolValidationError(
        "declared_purpose.purpose_id",
        f"purpose {purpose_id!r} is not registered for {profile['profile_id']}",
    )


def _validate_evidence_handle(value: Any, field: str) -> dict[str, Any]:
    value = _require_object(value, field)
    _strict_keys(
        value,
        field,
        (
            "evidence_id",
            "evidence_type",
            "content_sha256",
            "source_locator",
            "media_type",
            "rights_state",
            "version_id",
        ),
    )
    for key in ("evidence_id", "evidence_type", "source_locator", "media_type", "version_id"):
        _require_string(value[key], f"{field}.{key}")
    _require_sha256(value["content_sha256"], f"{field}.content_sha256")
    if value["rights_state"] not in _RIGHTS_STATES:
        raise ProtocolValidationError(f"{field}.rights_state", "unsupported rights state")
    return value


def validate_case_envelope(
    envelope: Any,
    *,
    registry: dict[str, Any] | None = None,
    verify_hash: bool = True,
) -> dict[str, Any]:
    envelope = _require_object(envelope, "case_envelope")
    _strict_keys(
        envelope,
        "case_envelope",
        (
            "protocol",
            "schema_version",
            "case_id",
            "source_case_id",
            "run_id",
            "profile",
            "asset_class",
            "declared_purpose",
            "trigger",
            "evidence_handles",
            "route_request",
            "execution",
            "required_outputs",
            "created_at_utc",
            "legacy_reference",
            "envelope_sha256",
        ),
    )
    if envelope["protocol"] != CASE_ENVELOPE_PROTOCOL:
        raise ProtocolValidationError("case_envelope.protocol", "unsupported CaseEnvelope protocol")
    if envelope["schema_version"] != PROTOCOL_VERSION:
        raise ProtocolValidationError("case_envelope.schema_version", "unsupported schema version")
    for key in ("case_id", "source_case_id", "run_id", "asset_class", "trigger"):
        _require_string(envelope[key], f"case_envelope.{key}")
    _require_datetime(envelope["created_at_utc"], "case_envelope.created_at_utc")

    profile_ref = _require_object(envelope["profile"], "case_envelope.profile")
    _strict_keys(
        profile_ref,
        "case_envelope.profile",
        ("profile_id", "profile_version", "profile_sha256"),
    )
    profile = resolve_profile(profile_ref["profile_id"], registry)
    if profile_ref != {
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "profile_sha256": profile["profile_sha256"],
    }:
        raise ProtocolValidationError("case_envelope.profile", "does not match frozen registry snapshot")
    if envelope["asset_class"] != profile["asset_class"]:
        raise ProtocolValidationError("case_envelope.asset_class", "does not match profile")

    purpose = _require_object(envelope["declared_purpose"], "case_envelope.declared_purpose")
    _strict_keys(
        purpose,
        "case_envelope.declared_purpose",
        ("purpose_id", "statement", "target_decision", "accountable_role"),
    )
    registered_purpose = _purpose(profile, purpose["purpose_id"])
    for key in ("statement", "target_decision", "accountable_role"):
        _require_string(purpose[key], f"case_envelope.declared_purpose.{key}")
    if purpose["target_decision"] != registered_purpose["target_decision"]:
        raise ProtocolValidationError(
            "case_envelope.declared_purpose.target_decision", "does not match profile purpose"
        )

    handles = envelope["evidence_handles"]
    if not isinstance(handles, list) or not handles:
        raise ProtocolValidationError("case_envelope.evidence_handles", "must be a non-empty list")
    for index, handle in enumerate(handles):
        _validate_evidence_handle(handle, f"case_envelope.evidence_handles[{index}]")
        if handle["evidence_type"] not in profile["accepted_evidence_types"]:
            raise ProtocolValidationError(
                f"case_envelope.evidence_handles[{index}].evidence_type",
                "is not accepted by the selected profile",
            )
    type_counts: dict[str, int] = {}
    for handle in handles:
        type_counts[handle["evidence_type"]] = type_counts.get(handle["evidence_type"], 0) + 1
    for requirement in profile["evidence_requirements"]:
        if type_counts.get(requirement["evidence_type"], 0) < requirement["min_count"]:
            raise ProtocolValidationError(
                "case_envelope.evidence_handles",
                f"missing required evidence type {requirement['evidence_type']}",
            )

    route = _require_object(envelope["route_request"], "case_envelope.route_request")
    _strict_keys(route, "case_envelope.route_request", ("expected_route", "human_gate_required"))
    if route["expected_route"] not in _ROUTES:
        raise ProtocolValidationError("case_envelope.route_request.expected_route", "unsupported route")
    if not isinstance(route["human_gate_required"], bool):
        raise ProtocolValidationError("case_envelope.route_request.human_gate_required", "must be boolean")

    execution = _require_object(envelope["execution"], "case_envelope.execution")
    _strict_keys(execution, "case_envelope.execution", ("policy_id", "dispatch_idempotency_key"))
    _require_string(execution["policy_id"], "case_envelope.execution.policy_id")
    _require_sha256(
        execution["dispatch_idempotency_key"],
        "case_envelope.execution.dispatch_idempotency_key",
    )
    if envelope["required_outputs"] != profile["required_outputs"]:
        raise ProtocolValidationError("case_envelope.required_outputs", "must equal frozen profile outputs")
    legacy = envelope["legacy_reference"]
    if legacy is not None:
        legacy = _require_object(legacy, "case_envelope.legacy_reference")
        _strict_keys(
            legacy,
            "case_envelope.legacy_reference",
            (
                "source_protocol",
                "source_sha256",
                "migration_status",
                "content_hash_resolution",
                "unmapped_fields",
            ),
        )
        _require_string(legacy["source_protocol"], "case_envelope.legacy_reference.source_protocol")
        _require_sha256(legacy["source_sha256"], "case_envelope.legacy_reference.source_sha256")
        if legacy["migration_status"] not in {"LOSSLESS", "BOUNDED_PROJECTION"}:
            raise ProtocolValidationError(
                "case_envelope.legacy_reference.migration_status", "unsupported migration state"
            )
        if legacy["content_hash_resolution"] not in {
            "SOURCE_DIGEST",
            "COMPUTED_FROM_LOCAL_EVIDENCE",
        }:
            raise ProtocolValidationError(
                "case_envelope.legacy_reference.content_hash_resolution",
                "unsupported content-hash resolution",
            )
        _require_string_list(
            legacy["unmapped_fields"],
            "case_envelope.legacy_reference.unmapped_fields",
        )
    if verify_hash:
        _validate_self_hash(envelope, "envelope_sha256", "case_envelope")
    return envelope


def build_case_envelope(
    *,
    profile_id: str,
    case_id: str,
    run_id: str,
    purpose_id: str,
    purpose_statement: str,
    evidence_handles: list[dict[str, Any]],
    trigger: str,
    expected_route: str,
    execution_policy_id: str,
    source_case_id: str | None = None,
    accountable_role: str | None = None,
    created_at_utc: str,
    legacy_reference: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = resolve_profile(profile_id, registry)
    purpose = _purpose(profile, purpose_id)
    idempotency_key = canonical_sha256(
        {
            "case_id": case_id,
            "run_id": run_id,
            "profile_sha256": profile["profile_sha256"],
            "purpose_id": purpose_id,
            "evidence_sha256": [item.get("content_sha256") for item in evidence_handles],
            "policy_id": execution_policy_id,
        }
    )
    envelope: dict[str, Any] = {
        "protocol": CASE_ENVELOPE_PROTOCOL,
        "schema_version": PROTOCOL_VERSION,
        "case_id": case_id,
        "source_case_id": source_case_id or case_id,
        "run_id": run_id,
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_sha256": profile["profile_sha256"],
        },
        "asset_class": profile["asset_class"],
        "declared_purpose": {
            "purpose_id": purpose_id,
            "statement": purpose_statement,
            "target_decision": purpose["target_decision"],
            "accountable_role": accountable_role or purpose["default_accountable_role"],
        },
        "trigger": trigger,
        "evidence_handles": copy.deepcopy(evidence_handles),
        "route_request": {
            "expected_route": expected_route,
            "human_gate_required": True,
        },
        "execution": {
            "policy_id": execution_policy_id,
            "dispatch_idempotency_key": idempotency_key,
        },
        "required_outputs": copy.deepcopy(profile["required_outputs"]),
        "created_at_utc": created_at_utc,
        "legacy_reference": copy.deepcopy(legacy_reference),
    }
    envelope["envelope_sha256"] = canonical_sha256(envelope)
    validate_case_envelope(envelope, registry=registry)
    return envelope


def _resolve_legacy_evidence_hash(
    item: dict[str, Any],
    *,
    source_base: Path,
) -> tuple[str, str, str]:
    sha_source = str(item.get("sha256_source") or "")
    if _SHA256.fullmatch(sha_source):
        return sha_source, str(item.get("path") or item.get("evidence_id") or "legacy-evidence"), "LOSSLESS"
    path_text = str(item.get("path") or "")
    path = Path(path_text)
    if not path.is_absolute():
        path = source_base / path
    if not path.is_file():
        raise ProtocolValidationError(
            "legacy.evidence_handles",
            f"cannot resolve immutable content hash for {path_text or item.get('evidence_id')}",
        )
    return hashlib.sha256(path.read_bytes()).hexdigest(), path_text, "HASH_RESOLVED_FROM_LOCAL_EVIDENCE"


def adapt_legacy_case_envelope(
    legacy: dict[str, Any],
    *,
    created_at_utc: str,
    source_base: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    legacy = _require_object(legacy, "legacy_case_envelope")
    source_protocol = str(legacy.get("protocol") or "FINFLUX_LEGACY_CASE_RECORD")
    asset = str(legacy.get("asset_class") or "")
    profile_id = PROFILE_BY_ASSET.get(asset)
    if not profile_id:
        raise ProtocolValidationError("legacy_case_envelope.asset_class", "cannot infer a frozen profile")
    profile = resolve_profile(profile_id, registry)
    raw_handles = list(legacy.get("evidence_handles") or legacy.get("evidence") or [])
    if not raw_handles:
        raise ProtocolValidationError("legacy_case_envelope.evidence", "at least one evidence handle is required")
    source_base = source_base or PROJECT_ROOT
    migration_states: list[str] = []
    handles: list[dict[str, Any]] = []
    required_type = profile["evidence_requirements"][0]["evidence_type"]
    for index, raw in enumerate(raw_handles):
        raw = _require_object(raw, f"legacy_case_envelope.evidence[{index}]")
        digest, locator, migration_state = _resolve_legacy_evidence_hash(raw, source_base=source_base)
        migration_states.append(migration_state)
        handles.append(
            {
                "evidence_id": str(raw.get("evidence_id") or f"LEGACY-EVIDENCE-{index + 1}"),
                "evidence_type": required_type if index == 0 else "supporting_research_bundle",
                "content_sha256": digest,
                "source_locator": locator,
                "media_type": "application/json",
                "rights_state": str(raw.get("rights_state") or "REVIEW_REQUIRED"),
                "version_id": f"sha256:{digest[:16]}",
            }
        )
    purpose_id = str(
        legacy.get("declared_downstream_use")
        or (legacy.get("declared_purpose") or {}).get("purpose_id")
        or ""
    )
    if not purpose_id:
        raise ProtocolValidationError("legacy_case_envelope.declared_purpose", "purpose is required")
    run_id = str(legacy.get("run_id") or f"RUN-MIGRATION-{canonical_sha256(legacy)[:16]}")
    case_id = str(legacy.get("case_id") or "")
    if not case_id:
        raise ProtocolValidationError("legacy_case_envelope.case_id", "case_id is required")
    source_sha = canonical_sha256(legacy)
    content_hash_resolution = (
        "COMPUTED_FROM_LOCAL_EVIDENCE"
        if "HASH_RESOLVED_FROM_LOCAL_EVIDENCE" in migration_states
        else "SOURCE_DIGEST"
    )
    mapped_legacy_fields = {
        "protocol",
        "case_id",
        "source_case_id",
        "run_id",
        "asset_class",
        "declared_downstream_use",
        "declared_purpose",
        "trigger",
        "evidence_handles",
        "evidence",
        "expected_route",
        "route_request",
        "execution_policy_id",
        "execution",
    }
    unmapped_fields = sorted(set(legacy) - mapped_legacy_fields)
    route = str(
        legacy.get("expected_route")
        or (legacy.get("route_request") or {}).get("expected_route")
        or "PRESERVE_DISPUTE_AND_ESCALATE"
    )
    return build_case_envelope(
        profile_id=profile_id,
        case_id=case_id,
        source_case_id=str(legacy.get("source_case_id") or case_id),
        run_id=run_id,
        purpose_id=purpose_id,
        purpose_statement=str(
            (legacy.get("declared_purpose") or {}).get("statement")
            or f"核验数据是否满足 {purpose_id} 的已登记语义契约"
        ),
        evidence_handles=handles,
        trigger=str(legacy.get("trigger") or "LEGACY_CASE_MIGRATION"),
        expected_route=route,
        execution_policy_id=str(
            legacy.get("execution_policy_id")
            or (legacy.get("execution") or {}).get("policy_id")
            or "FINFLUX-BOUNDED-EXECUTION-V0.1"
        ),
        accountable_role=(legacy.get("declared_purpose") or {}).get("accountable_role"),
        created_at_utc=created_at_utc,
        legacy_reference={
            "source_protocol": source_protocol,
            "source_sha256": source_sha,
            "migration_status": (
                "BOUNDED_PROJECTION" if unmapped_fields else "LOSSLESS"
            ),
            "content_hash_resolution": content_hash_resolution,
            "unmapped_fields": unmapped_fields,
        },
        registry=registry,
    )


def _validate_worker_receipt(value: Any, field: str) -> dict[str, Any]:
    value = _require_object(value, field)
    _strict_keys(value, field, ("worker_id", "status", "artifact_sha256"))
    _require_string(value["worker_id"], f"{field}.worker_id")
    if value["status"] not in {"SEALED", "FAILED", "NOT_AVAILABLE"}:
        raise ProtocolValidationError(f"{field}.status", "unsupported Worker status")
    if value["artifact_sha256"] is not None:
        _require_sha256(value["artifact_sha256"], f"{field}.artifact_sha256")
    if value["status"] == "SEALED" and value["artifact_sha256"] is None:
        raise ProtocolValidationError(f"{field}.artifact_sha256", "SEALED Worker requires a hash")
    return value


def _validate_skill_invocation(value: Any, field: str) -> dict[str, Any]:
    value = _require_object(value, field)
    _strict_keys(
        value,
        field,
        ("skill_id", "version", "worker_id", "input_sha256", "output_sha256", "status"),
    )
    for key in ("skill_id", "version", "worker_id"):
        _require_string(value[key], f"{field}.{key}")
    for key in ("input_sha256", "output_sha256"):
        _require_sha256(value[key], f"{field}.{key}")
    if value["status"] not in {"SUCCEEDED", "CACHE_HIT"}:
        raise ProtocolValidationError(
            f"{field}.status",
            "DataPass may attest only SUCCEEDED or CACHE_HIT Skill receipts",
        )
    return value


def validate_datapass(
    datapass: Any,
    *,
    envelope: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    worker_artifacts: dict[str, dict[str, Any]] | None = None,
    verify_hash: bool = True,
) -> dict[str, Any]:
    datapass = _require_object(datapass, "datapass")
    _strict_keys(
        datapass,
        "datapass",
        (
            "protocol",
            "schema_version",
            "datapass_id",
            "case_id",
            "run_id",
            "envelope_sha256",
            "profile",
            "declared_purpose",
            "evidence_assessment",
            "semantic_assessment",
            "impact_assessment",
            "machine_recommendation",
            "reason_codes",
            "recommendation_summary",
            "workers",
            "skills",
            "human_gate",
            "integrity",
            "status",
            "generated_at_utc",
            "legacy_reference",
            "datapass_sha256",
        ),
    )
    if datapass["protocol"] != DATAPASS_PROTOCOL:
        raise ProtocolValidationError("datapass.protocol", "unsupported DataPass protocol")
    if datapass["schema_version"] != PROTOCOL_VERSION:
        raise ProtocolValidationError("datapass.schema_version", "unsupported schema version")
    for key in ("datapass_id", "case_id", "run_id", "recommendation_summary"):
        _require_string(datapass[key], f"datapass.{key}")
    _require_sha256(datapass["envelope_sha256"], "datapass.envelope_sha256")
    _require_datetime(datapass["generated_at_utc"], "datapass.generated_at_utc")

    profile_ref = _require_object(datapass["profile"], "datapass.profile")
    _strict_keys(profile_ref, "datapass.profile", ("profile_id", "profile_version", "profile_sha256"))
    profile = resolve_profile(profile_ref["profile_id"], registry)
    if profile_ref != {
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "profile_sha256": profile["profile_sha256"],
    }:
        raise ProtocolValidationError("datapass.profile", "does not match frozen registry snapshot")
    purpose = _require_object(datapass["declared_purpose"], "datapass.declared_purpose")
    _strict_keys(purpose, "datapass.declared_purpose", ("purpose_id", "target_decision"))
    registered_purpose = _purpose(profile, purpose["purpose_id"])
    if purpose["target_decision"] != registered_purpose["target_decision"]:
        raise ProtocolValidationError("datapass.declared_purpose", "does not match profile")

    evidence = _require_object(datapass["evidence_assessment"], "datapass.evidence_assessment")
    _strict_keys(evidence, "datapass.evidence_assessment", ("status", "quorum_met", "evidence_sha256"))
    if evidence["status"] not in {"VERIFIED", "INVALID", "PARTIAL", "NOT_AVAILABLE"}:
        raise ProtocolValidationError("datapass.evidence_assessment.status", "unsupported status")
    if not isinstance(evidence["quorum_met"], bool):
        raise ProtocolValidationError("datapass.evidence_assessment.quorum_met", "must be boolean")
    evidence_hashes = _require_string_list(
        evidence["evidence_sha256"], "datapass.evidence_assessment.evidence_sha256", min_items=1
    )
    for index, digest in enumerate(evidence_hashes):
        _require_sha256(digest, f"datapass.evidence_assessment.evidence_sha256[{index}]")

    semantic = _require_object(datapass["semantic_assessment"], "datapass.semantic_assessment")
    _strict_keys(semantic, "datapass.semantic_assessment", ("contract_id", "version", "status"))
    if semantic["contract_id"] != profile["semantic_contract"]["contract_id"]:
        raise ProtocolValidationError("datapass.semantic_assessment.contract_id", "does not match profile")
    if semantic["version"] != profile["semantic_contract"]["version"]:
        raise ProtocolValidationError("datapass.semantic_assessment.version", "does not match profile")
    if semantic["status"] not in {"RESOLVED", "CONFLICT", "NOT_AVAILABLE"}:
        raise ProtocolValidationError("datapass.semantic_assessment.status", "unsupported status")

    impact = _require_object(datapass["impact_assessment"], "datapass.impact_assessment")
    _strict_keys(impact, "datapass.impact_assessment", ("status", "facts_sha256", "metrics"))
    if impact["status"] not in {"COMPUTED", "COUNTERFACTUAL", "NOT_APPLICABLE", "NOT_AVAILABLE"}:
        raise ProtocolValidationError("datapass.impact_assessment.status", "unsupported status")
    _require_sha256(impact["facts_sha256"], "datapass.impact_assessment.facts_sha256")
    if not isinstance(impact["metrics"], list):
        raise ProtocolValidationError("datapass.impact_assessment.metrics", "must be a list")
    for index, metric in enumerate(impact["metrics"]):
        item_field = f"datapass.impact_assessment.metrics[{index}]"
        metric = _require_object(metric, item_field)
        _strict_keys(metric, item_field, ("metric_id", "label", "value", "unit", "source_kind"))
        for key in ("metric_id", "label", "unit"):
            _require_string(metric[key], f"{item_field}.{key}", allow_empty=key == "unit")
        if isinstance(metric["value"], (dict, list)) or metric["value"] is None:
            raise ProtocolValidationError(f"{item_field}.value", "must be a scalar")
        if metric["source_kind"] not in {"OBSERVED", "DETERMINISTIC", "COUNTERFACTUAL"}:
            raise ProtocolValidationError(f"{item_field}.source_kind", "unsupported source kind")

    if datapass["machine_recommendation"] not in _DECISIONS:
        raise ProtocolValidationError("datapass.machine_recommendation", "unsupported recommendation")
    _require_string_list(datapass["reason_codes"], "datapass.reason_codes", min_items=1)

    workers = _require_object(datapass["workers"], "datapass.workers")
    _strict_keys(
        workers,
        "datapass.workers",
        (
            "required_worker_ids",
            "completed_worker_ids",
            "reported_required_count",
            "reported_completed_count",
            "attestation_status",
            "receipts",
        ),
    )
    required_workers = _require_string_list(
        workers["required_worker_ids"], "datapass.workers.required_worker_ids"
    )
    completed_workers = _require_string_list(
        workers["completed_worker_ids"], "datapass.workers.completed_worker_ids"
    )
    for key in ("reported_required_count", "reported_completed_count"):
        if not isinstance(workers[key], int) or workers[key] < 0:
            raise ProtocolValidationError(f"datapass.workers.{key}", "must be an integer >= 0")
    if workers["attestation_status"] not in {"VERIFIED", "PARTIAL", "NOT_AVAILABLE", "LEGACY_COUNT_ONLY"}:
        raise ProtocolValidationError("datapass.workers.attestation_status", "unsupported status")
    if not isinstance(workers["receipts"], list):
        raise ProtocolValidationError("datapass.workers.receipts", "must be a list")
    worker_receipts_by_id: dict[str, dict[str, Any]] = {}
    for index, receipt in enumerate(workers["receipts"]):
        receipt = _validate_worker_receipt(receipt, f"datapass.workers.receipts[{index}]")
        worker_id = receipt["worker_id"]
        if worker_id in worker_receipts_by_id:
            raise ProtocolValidationError(
                f"datapass.workers.receipts[{index}].worker_id",
                "duplicate Worker receipt",
            )
        worker_receipts_by_id[worker_id] = receipt

    legacy_count_only = workers["attestation_status"] == "LEGACY_COUNT_ONLY"
    legacy_mode = datapass["legacy_reference"] is not None
    if legacy_count_only:
        if not legacy_mode:
            raise ProtocolValidationError(
                "datapass.workers.attestation_status",
                "LEGACY_COUNT_ONLY requires an explicit legacy_reference",
            )
        if required_workers or completed_workers or worker_receipts_by_id:
            raise ProtocolValidationError(
                "datapass.workers",
                "LEGACY_COUNT_ONLY must not invent Worker identities or receipts",
            )
        if workers["reported_completed_count"] > workers["reported_required_count"]:
            raise ProtocolValidationError(
                "datapass.workers.reported_completed_count",
                "cannot exceed the legacy reported required count",
            )
    else:
        receipt_ids = set(worker_receipts_by_id)
        required_worker_set = set(required_workers)
        unexpected_workers = receipt_ids - required_worker_set
        if unexpected_workers:
            raise ProtocolValidationError(
                "datapass.workers.receipts",
                f"contains unrequested Worker receipt(s): {', '.join(sorted(unexpected_workers))}",
            )
        sealed_worker_ids = {
            worker_id
            for worker_id, receipt in worker_receipts_by_id.items()
            if receipt["status"] == "SEALED"
        }
        if set(completed_workers) != sealed_worker_ids:
            raise ProtocolValidationError(
                "datapass.workers.completed_worker_ids",
                "must equal Worker identities derived from SEALED receipts",
            )
        if workers["reported_required_count"] != len(required_workers):
            raise ProtocolValidationError(
                "datapass.workers.reported_required_count",
                "must equal the number of required_worker_ids",
            )
        if workers["reported_completed_count"] != len(sealed_worker_ids):
            raise ProtocolValidationError(
                "datapass.workers.reported_completed_count",
                "must equal the number of SEALED Worker receipts",
            )
        workers_verified = (
            required_worker_set == sealed_worker_ids
            and receipt_ids == sealed_worker_ids
        )
        if workers["attestation_status"] == "VERIFIED" and not workers_verified:
            raise ProtocolValidationError(
                "datapass.workers.attestation_status",
                "VERIFIED contradicts failed, unavailable, or missing Worker receipts",
            )
        if workers["attestation_status"] == "PARTIAL" and workers_verified:
            raise ProtocolValidationError(
                "datapass.workers.attestation_status",
                "PARTIAL contradicts a complete set of SEALED Worker receipts",
            )
        if workers["attestation_status"] == "NOT_AVAILABLE" and (
            completed_workers or worker_receipts_by_id
        ):
            raise ProtocolValidationError(
                "datapass.workers.attestation_status",
                "NOT_AVAILABLE cannot include Worker receipts",
            )

        if worker_artifacts is not None:
            if not isinstance(worker_artifacts, dict):
                raise ProtocolValidationError("worker_artifacts", "must be an object")
            artifact_ids = set(worker_artifacts)
            if artifact_ids != sealed_worker_ids:
                raise ProtocolValidationError(
                    "worker_artifacts",
                    "must contain exactly one artifact for every SEALED Worker receipt",
                )
            for worker_id in sorted(sealed_worker_ids):
                artifact = _require_object(
                    worker_artifacts[worker_id], f"worker_artifacts.{worker_id}"
                )
                declared_role = artifact.get("worker_id", artifact.get("role"))
                if declared_role is not None and declared_role != worker_id:
                    raise ProtocolValidationError(
                        f"worker_artifacts.{worker_id}",
                        "artifact responsibility does not match Worker receipt",
                    )
                _validate_self_hash(
                    artifact, "artifact_sha256", f"worker_artifacts.{worker_id}"
                )
                if (
                    worker_receipts_by_id[worker_id]["artifact_sha256"]
                    != artifact["artifact_sha256"]
                ):
                    raise ProtocolValidationError(
                        f"datapass.workers.receipts.{worker_id}.artifact_sha256",
                        "does not bind the supplied Worker artifact",
                    )

    skills = _require_object(datapass["skills"], "datapass.skills")
    _strict_keys(skills, "datapass.skills", ("required_skill_ids", "attestation_status", "invocations"))
    required_skills = _require_string_list(
        skills["required_skill_ids"], "datapass.skills.required_skill_ids"
    )
    if skills["attestation_status"] not in {"VERIFIED", "PARTIAL", "NOT_AVAILABLE"}:
        raise ProtocolValidationError("datapass.skills.attestation_status", "unsupported status")
    if not isinstance(skills["invocations"], list):
        raise ProtocolValidationError("datapass.skills.invocations", "must be a list")
    invocations_by_skill: dict[str, dict[str, Any]] = {}
    for index, invocation in enumerate(skills["invocations"]):
        invocation = _validate_skill_invocation(
            invocation, f"datapass.skills.invocations[{index}]"
        )
        skill_id = invocation["skill_id"]
        if skill_id in invocations_by_skill:
            raise ProtocolValidationError(
                f"datapass.skills.invocations[{index}].skill_id",
                "duplicate Skill receipt",
            )
        invocations_by_skill[skill_id] = invocation

    required_skill_set = set(required_skills)
    observed_skill_set = set(invocations_by_skill)
    if legacy_mode:
        if skills["attestation_status"] == "VERIFIED":
            raise ProtocolValidationError(
                "datapass.skills.attestation_status",
                "legacy Skill identities cannot be promoted to VERIFIED",
            )
        if skills["attestation_status"] == "NOT_AVAILABLE" and invocations_by_skill:
            raise ProtocolValidationError(
                "datapass.skills.attestation_status",
                "NOT_AVAILABLE cannot include Skill receipts",
            )
    else:
        unknown_required = required_skill_set - set(_SKILL_CONTRACTS)
        if unknown_required:
            raise ProtocolValidationError(
                "datapass.skills.required_skill_ids",
                f"unknown frozen Skill contract(s): {', '.join(sorted(unknown_required))}",
            )
        unexpected_skills = observed_skill_set - required_skill_set
        if unexpected_skills:
            raise ProtocolValidationError(
                "datapass.skills.invocations",
                f"contains unrequested Skill receipt(s): {', '.join(sorted(unexpected_skills))}",
            )
        required_worker_set = set(required_workers)
        for skill_id in required_skills:
            expected_version, expected_worker = _SKILL_CONTRACTS[skill_id]
            if expected_worker not in required_worker_set:
                raise ProtocolValidationError(
                    "datapass.skills.required_skill_ids",
                    f"{skill_id}@{expected_version} requires Worker {expected_worker}",
                )
            invocation = invocations_by_skill.get(skill_id)
            if invocation is None:
                continue
            if invocation["version"] != expected_version:
                raise ProtocolValidationError(
                    f"datapass.skills.invocations.{skill_id}.version",
                    f"must equal frozen version {expected_version}",
                )
            if invocation["worker_id"] != expected_worker:
                raise ProtocolValidationError(
                    f"datapass.skills.invocations.{skill_id}.worker_id",
                    f"must equal responsible Worker {expected_worker}",
                )
        skills_verified = observed_skill_set == required_skill_set
        if skills["attestation_status"] == "VERIFIED" and not skills_verified:
            raise ProtocolValidationError(
                "datapass.skills.attestation_status",
                "VERIFIED contradicts missing Skill receipts",
            )
        if skills["attestation_status"] == "PARTIAL" and skills_verified:
            raise ProtocolValidationError(
                "datapass.skills.attestation_status",
                "PARTIAL contradicts a complete set of successful Skill receipts",
            )
        if skills["attestation_status"] == "NOT_AVAILABLE" and invocations_by_skill:
            raise ProtocolValidationError(
                "datapass.skills.attestation_status",
                "NOT_AVAILABLE cannot include Skill receipts",
            )

    human = _require_object(datapass["human_gate"], "datapass.human_gate")
    _strict_keys(
        human,
        "datapass.human_gate",
        ("required", "state", "decision", "actor_id", "decided_at_utc", "signature_sha256"),
    )
    if human["required"] is not True:
        raise ProtocolValidationError("datapass.human_gate.required", "P0-A DataPass requires Human Gate")
    if human["state"] not in {"NOT_OPENED", "AWAITING_HUMAN", "APPROVED", "REJECTED", "RETURNED"}:
        raise ProtocolValidationError("datapass.human_gate.state", "unsupported state")
    for key in ("decision", "actor_id", "decided_at_utc", "signature_sha256"):
        if human[key] is not None and not isinstance(human[key], str):
            raise ProtocolValidationError(f"datapass.human_gate.{key}", "must be string or null")
    if human["state"] in {"APPROVED", "REJECTED", "RETURNED"}:
        for key in ("decision", "actor_id", "decided_at_utc", "signature_sha256"):
            if not human[key]:
                raise ProtocolValidationError(f"datapass.human_gate.{key}", "is required for a final decision")
        _require_datetime(human["decided_at_utc"], "datapass.human_gate.decided_at_utc")
        _require_sha256(human["signature_sha256"], "datapass.human_gate.signature_sha256")

    integrity = _require_object(datapass["integrity"], "datapass.integrity")
    _strict_keys(
        integrity,
        "datapass.integrity",
        ("raw_evidence_mutated", "production_write_performed", "parent_datapass_sha256"),
    )
    if not isinstance(integrity["raw_evidence_mutated"], bool):
        raise ProtocolValidationError("datapass.integrity.raw_evidence_mutated", "must be boolean")
    if not isinstance(integrity["production_write_performed"], bool):
        raise ProtocolValidationError("datapass.integrity.production_write_performed", "must be boolean")
    if integrity["parent_datapass_sha256"] is not None:
        _require_sha256(integrity["parent_datapass_sha256"], "datapass.integrity.parent_datapass_sha256")

    if datapass["status"] not in {"DRAFT_CREATED", "AWAITING_HUMAN", "FINALIZED"}:
        raise ProtocolValidationError("datapass.status", "unsupported status")
    legacy = datapass["legacy_reference"]
    if legacy is not None:
        legacy = _require_object(legacy, "datapass.legacy_reference")
        _strict_keys(
            legacy,
            "datapass.legacy_reference",
            ("source_protocol", "source_sha256", "unresolved_fields"),
        )
        _require_string(legacy["source_protocol"], "datapass.legacy_reference.source_protocol")
        _require_sha256(legacy["source_sha256"], "datapass.legacy_reference.source_sha256")
        _require_string_list(legacy["unresolved_fields"], "datapass.legacy_reference.unresolved_fields")

    if datapass["machine_recommendation"] == "PASS":
        if evidence["status"] != "VERIFIED" or not evidence["quorum_met"]:
            raise ProtocolValidationError("datapass.machine_recommendation", "PASS requires verified evidence quorum")
        if semantic["status"] != "RESOLVED":
            raise ProtocolValidationError("datapass.machine_recommendation", "PASS requires resolved semantics")
    if envelope is not None:
        validate_case_envelope(envelope, registry=registry)
        if datapass["envelope_sha256"] != envelope["envelope_sha256"]:
            raise ProtocolValidationError("datapass.envelope_sha256", "does not bind the supplied CaseEnvelope")
        for key in ("case_id", "run_id"):
            if datapass[key] != envelope[key]:
                raise ProtocolValidationError(f"datapass.{key}", "does not match CaseEnvelope")
        if datapass["profile"] != envelope["profile"]:
            raise ProtocolValidationError("datapass.profile", "does not match CaseEnvelope")
        if datapass["declared_purpose"]["purpose_id"] != envelope["declared_purpose"]["purpose_id"]:
            raise ProtocolValidationError("datapass.declared_purpose", "does not match CaseEnvelope")
    if verify_hash:
        _validate_self_hash(datapass, "datapass_sha256", "datapass")
    return datapass


def build_datapass_draft(
    *,
    envelope: dict[str, Any],
    machine_recommendation: str,
    reason_codes: list[str],
    recommendation_summary: str,
    evidence_status: str,
    evidence_quorum_met: bool,
    semantic_status: str,
    impact_status: str,
    impact_facts: dict[str, Any],
    impact_metrics: list[dict[str, Any]],
    required_worker_ids: list[str],
    worker_receipts: list[dict[str, Any]],
    required_skill_ids: list[str],
    skill_invocations: list[dict[str, Any]],
    generated_at_utc: str,
    legacy_reference: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_case_envelope(envelope, registry=registry)
    profile = resolve_profile(envelope["profile"]["profile_id"], registry)
    completed_worker_ids = [
        receipt["worker_id"] for receipt in worker_receipts if receipt.get("status") == "SEALED"
    ]
    invoked_skills = {str(item.get("skill_id")) for item in skill_invocations}
    skills_complete = set(required_skill_ids) == invoked_skills
    workers_complete = (
        set(required_worker_ids) == set(completed_worker_ids)
        and all(receipt.get("status") == "SEALED" for receipt in worker_receipts)
    )
    datapass: dict[str, Any] = {
        "protocol": DATAPASS_PROTOCOL,
        "schema_version": PROTOCOL_VERSION,
        "datapass_id": f"DP-{canonical_sha256({'run_id': envelope['run_id'], 'case_id': envelope['case_id']})[:20].upper()}",
        "case_id": envelope["case_id"],
        "run_id": envelope["run_id"],
        "envelope_sha256": envelope["envelope_sha256"],
        "profile": copy.deepcopy(envelope["profile"]),
        "declared_purpose": {
            "purpose_id": envelope["declared_purpose"]["purpose_id"],
            "target_decision": envelope["declared_purpose"]["target_decision"],
        },
        "evidence_assessment": {
            "status": evidence_status,
            "quorum_met": evidence_quorum_met,
            "evidence_sha256": [item["content_sha256"] for item in envelope["evidence_handles"]],
        },
        "semantic_assessment": {
            "contract_id": profile["semantic_contract"]["contract_id"],
            "version": profile["semantic_contract"]["version"],
            "status": semantic_status,
        },
        "impact_assessment": {
            "status": impact_status,
            "facts_sha256": canonical_sha256(impact_facts),
            "metrics": copy.deepcopy(impact_metrics),
        },
        "machine_recommendation": machine_recommendation,
        "reason_codes": copy.deepcopy(reason_codes),
        "recommendation_summary": recommendation_summary,
        "workers": {
            "required_worker_ids": copy.deepcopy(required_worker_ids),
            "completed_worker_ids": completed_worker_ids,
            "reported_required_count": len(required_worker_ids),
            "reported_completed_count": len(completed_worker_ids),
            "attestation_status": (
                "VERIFIED"
                if workers_complete and required_worker_ids
                else "NOT_AVAILABLE"
                if not required_worker_ids and not worker_receipts
                else "PARTIAL"
            ),
            "receipts": copy.deepcopy(worker_receipts),
        },
        "skills": {
            "required_skill_ids": copy.deepcopy(required_skill_ids),
            "attestation_status": (
                "VERIFIED"
                if skills_complete and required_skill_ids
                else "NOT_AVAILABLE"
                if not required_skill_ids and not skill_invocations
                else "PARTIAL"
            ),
            "invocations": copy.deepcopy(skill_invocations),
        },
        "human_gate": {
            "required": True,
            "state": "NOT_OPENED",
            "decision": None,
            "actor_id": None,
            "decided_at_utc": None,
            "signature_sha256": None,
        },
        "integrity": {
            "raw_evidence_mutated": False,
            "production_write_performed": False,
            "parent_datapass_sha256": None,
        },
        "status": "DRAFT_CREATED",
        "generated_at_utc": generated_at_utc,
        "legacy_reference": copy.deepcopy(legacy_reference),
    }
    datapass["datapass_sha256"] = canonical_sha256(datapass)
    validate_datapass(datapass, envelope=envelope, registry=registry)
    return datapass


def adapt_legacy_datapass(
    legacy: dict[str, Any],
    *,
    envelope: dict[str, Any],
    generated_at_utc: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    legacy = _require_object(legacy, "legacy_datapass")
    recommendation = str(
        legacy.get("machine_recommendation")
        or legacy.get("agent_recommendation")
        or "NEEDS_EVIDENCE"
    )
    if recommendation not in _DECISIONS:
        recommendation = "NEEDS_EVIDENCE"
    raw_impact = legacy.get("impact") if isinstance(legacy.get("impact"), dict) else {}
    metrics: list[dict[str, Any]] = []
    metric_units = {
        "anchor_return_difference_percentage_points": "percentage_points",
        "absolute_impact_cny_per_contract": "CNY_per_contract",
        "covered_security_shortfall_shares_per_contract": "shares_per_contract",
        "notional_understatement_cny_per_contract": "CNY_per_contract",
        "difference_points": "points",
    }
    for key, unit in metric_units.items():
        value = raw_impact.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics.append(
                {
                    "metric_id": key,
                    "label": key,
                    "value": value,
                    "unit": unit,
                    "source_kind": (
                        "COUNTERFACTUAL"
                        if raw_impact.get("impact_is_counterfactual")
                        else "DETERMINISTIC"
                    ),
                }
            )
    legacy_invocations = list(legacy.get("skill_invocations") or [])
    skill_invocations: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for index, invocation in enumerate(legacy_invocations):
        if not isinstance(invocation, dict):
            unresolved.append(f"skill_invocations[{index}]")
            continue
        input_sha = invocation.get("input_sha256")
        output_sha = invocation.get("output_sha256")
        if not (_SHA256.fullmatch(str(input_sha or "")) and _SHA256.fullmatch(str(output_sha or ""))):
            unresolved.append(f"skill_invocations[{index}].io_hashes")
            continue
        skill_invocations.append(
            {
                "skill_id": str(invocation.get("skill_id") or "legacy-unknown-skill"),
                "version": str(invocation.get("version") or "legacy-unversioned"),
                "worker_id": str(invocation.get("worker_id") or invocation.get("owner_role") or "legacy-unknown-worker"),
                "input_sha256": str(input_sha),
                "output_sha256": str(output_sha),
                "status": "SUCCEEDED",
            }
        )
    worker_count = int(legacy.get("worker_artifact_count", 0) or 0)
    if worker_count:
        unresolved.append("worker_identities")
    legacy_reference = {
        "source_protocol": str(
            legacy.get("protocol") or legacy.get("datapass_version") or "FINFLUX_LEGACY_DATAPASS"
        ),
        "source_sha256": canonical_sha256(legacy),
        "unresolved_fields": sorted(set(unresolved)),
    }
    datapass = build_datapass_draft(
        envelope=envelope,
        machine_recommendation=recommendation,
        reason_codes=["LEGACY_DATAPASS_MIGRATED"],
        recommendation_summary="Legacy DataPass migrated without inventing missing Worker or Skill evidence.",
        evidence_status=str(legacy.get("evidence_status") or "NOT_AVAILABLE"),
        evidence_quorum_met=bool(legacy.get("evidence_quorum", False)),
        semantic_status=(
            "RESOLVED" if (legacy.get("contract") or legacy.get("semantic_contract")) else "NOT_AVAILABLE"
        ),
        impact_status=(
            "COUNTERFACTUAL"
            if raw_impact.get("impact_is_counterfactual")
            else "COMPUTED"
            if raw_impact
            else "NOT_AVAILABLE"
        ),
        impact_facts=raw_impact,
        impact_metrics=metrics,
        required_worker_ids=[],
        worker_receipts=[],
        required_skill_ids=[],
        skill_invocations=skill_invocations,
        generated_at_utc=generated_at_utc,
        legacy_reference=legacy_reference,
        registry=registry,
    )
    legacy_required_count = int(
        legacy.get("required_worker_count", worker_count) or 0
    )
    datapass["workers"].update(
        {
            "reported_required_count": legacy_required_count,
            "reported_completed_count": worker_count,
            "attestation_status": (
                "LEGACY_COUNT_ONLY"
                if legacy_required_count or worker_count
                else "NOT_AVAILABLE"
            ),
        }
    )
    # Legacy rows are not bound to the frozen V0.2 skill_id@version/owner
    # contract, so migration must never promote them to VERIFIED.
    datapass["skills"]["attestation_status"] = (
        "PARTIAL" if skill_invocations else "NOT_AVAILABLE"
    )
    datapass["datapass_sha256"] = canonical_sha256(_unsigned(datapass, "datapass_sha256"))
    validate_datapass(datapass, envelope=envelope, registry=registry)
    return datapass
