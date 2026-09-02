from __future__ import annotations

"""Read-only UI adapter for the governed V0.2 financial Profile Registry.

Profile definitions, versions and hashes live in
``agentteams/config/profile_registry_v0.2.json`` and are validated by
``protocol_v02``. This module derives safe presentation metadata only; runtime
Skill receipts and DataPass remain the authority for every concrete Run.
"""

import copy
import hashlib
import json
from typing import Any

from protocol_v02 import load_profile_registry


PROFILE_PROTOCOL = "FINFLUX_PROFILE_DEFINITION_V1"
REGISTRY_PROTOCOL = "FINFLUX_PROFILE_REGISTRY_V1"
SAFE_COMPONENTS = {"fact-grid", "semantic-contract", "metric-card", "evidence-list"}
LIVE_EXECUTABLE_IDS = {
    "equity_corporate_action",
    "futures_settlement",
    "fund_nav_admission",
}

# Non-executable display hints. The current Run's Skill output always overrides
# these labels and candidate fields.
PURPOSE_DISPLAY_HINTS: dict[str, dict[str, Any]] = {
    "daily_settlement_pnl": {
        "required_concept": "official_daily_settlement_price",
        "required_field": "settle",
        "impact_keys": ("financial_misstatement_cny_per_contract", "impact_cny_per_contract"),
        "impact_label": "按声明用途测算的每手金额影响",
        "unit": "元/手",
        "field_unit": "点",
    },
    "margin_input": {
        "impact_keys": ("financial_misstatement_cny_per_contract", "notional_impact"),
        "impact_label": "保证金输入口径的确定性影响",
        "unit": "元",
    },
    "return_analysis": {
        "required_concept": "corporate_action_adjusted_series",
        "required_field": "qfq",
        "impact_keys": ("return_difference_pct_points",),
        "impact_label": "不同复权口径的收益率差异",
        "unit": "个百分点",
        "field_unit": "复权口径",
    },
    "factor_research": {
        "required_concept": "comparable_price_series",
        "required_field": "qfq",
        "impact_keys": ("return_difference_pct_points",),
        "impact_label": "口径变化造成的因子输入差异",
        "unit": "个百分点",
        "field_unit": "复权口径",
    },
    "backtest_input": {
        "required_concept": "comparable_price_series",
        "required_field": "qfq",
        "impact_keys": ("return_difference_pct_points",),
        "impact_label": "回测输入口径的确定性差异",
        "unit": "个百分点",
        "field_unit": "复权口径",
    },
    "holding_valuation": {
        "required_concept": "unit_net_asset_value",
        "required_field": "unit_nav",
        "impact_keys": ("impact_cny_per_10000_units",),
        "impact_label": "净值概念错用的万份金额影响",
        "unit": "元/万份",
        "field_unit": "元/份",
    },
    "total_return_analysis": {
        "required_concept": "cumulative_net_asset_value",
        "required_field": "cumulative_nav",
        "impact_keys": ("impact_cny_per_10000_units",),
        "impact_label": "净值概念错用的万份金额影响",
        "unit": "元/万份",
        "field_unit": "元/份",
    },
    "subscription_redemption_review": {
        "required_concept": "subscription_redemption_state",
        "impact_label": "申赎状态与净值日期适用性",
    },
    "covered_position_and_exposure": {
        "required_concept": "adjusted_option_contract_identity",
        "impact_keys": ("covered_shortfall", "notional_impact"),
        "impact_label": "调整合约身份造成的敞口影响",
    },
    "greeks": {
        "required_concept": "adjusted_option_contract_identity",
        "impact_keys": ("notional_impact",),
        "impact_label": "合约版本参数的确定性影响",
        "unit": "元",
    },
    "research_context": {
        "required_concept": "permitted_point_in_time_research_context",
        "required_field": "rights_state",
        "impact_label": "权属与时点适用性",
    },
    "investment_research_support": {
        "required_concept": "permitted_research_use",
        "required_field": "rights_state",
        "impact_label": "研究资料许可边界",
    },
    "evidence_corroboration": {
        "required_concept": "traceable_research_evidence",
        "required_field": "content_sha256",
        "impact_label": "来源、时点与权属完整性",
    },
}

CORE_WORKERS = (
    "Evidence Investigator",
    "Semantic Impact Analyst",
    "Independent Validator",
)
CORE_SKILLS = (
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


def _source_path(field_id: str) -> str:
    if field_id in {"declared_purpose", "provider", "publisher", "rights_state"}:
        return f"metadata.{field_id}"
    return f"parsed.{field_id}"


def _component_schema(profile: dict[str, Any]) -> list[dict[str, str]]:
    components = [
        {"key": "profile-summary", "component": "fact-grid"},
        {"key": "semantic-contract", "component": "semantic-contract"},
    ]
    value_types = {
        str(item.get("value_type") or "")
        for item in profile.get("display_fields") or []
    }
    if value_types & {"currency", "percentage_points", "shares_per_contract"}:
        components.append({"key": "financial-impact", "component": "metric-card"})
    else:
        components.append({"key": "evidence-list", "component": "evidence-list"})
    return components


def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    purposes: dict[str, dict[str, Any]] = {}
    for purpose in profile.get("declared_purposes") or []:
        purpose_id = str(purpose.get("purpose_id") or "")
        hint = PURPOSE_DISPLAY_HINTS.get(purpose_id) or {}
        purposes[purpose_id] = {
            "label": purpose.get("label") or purpose_id,
            "target_decision": purpose.get("target_decision"),
            "default_accountable_role": purpose.get("default_accountable_role"),
            "required_concept": hint.get("required_concept"),
            "required_field": hint.get("required_field"),
            "constraints": [],
            "impact_metric": {
                "value_paths": list(hint.get("impact_keys") or ()),
                "label": hint.get("impact_label") or "本Run尚未返回可复核影响指标",
                "unit": hint.get("unit") or "",
                "field_unit": hint.get("field_unit") or "",
            },
        }
    display_fields = profile.get("display_fields") or []
    summary_fields = [
        {
            "key": item.get("field_id"),
            "label": item.get("label"),
            "path": _source_path(str(item.get("field_id") or "")),
            "unit": "",
            "value_type": item.get("value_type"),
        }
        for item in display_fields[:4]
    ]
    contract = profile.get("semantic_contract") or {}
    default_purpose = next(iter(purposes), "evidence_review")
    return {
        "protocol": PROFILE_PROTOCOL,
        "profile_id": profile.get("profile_id"),
        "version": profile.get("profile_version"),
        "asset_family": profile.get("asset_class"),
        "display_name": profile.get("display_name"),
        "live_executable": profile.get("profile_id") in LIVE_EXECUTABLE_IDS,
        "default_purpose": default_purpose,
        "purpose_bindings": purposes,
        "summary_fields": summary_fields,
        "semantic_fields": copy.deepcopy(display_fields),
        "worker_policy": {
            "mode": "MANAGER_DYNAMIC_ROUTING",
            "core_workers": list(CORE_WORKERS),
        },
        "skill_policy": {
            "mode": "RUNTIME_DISCOVERY_WITH_RECEIPTS",
            "core_skills": list(CORE_SKILLS),
        },
        "presentation_schema": _component_schema(profile),
        "contract": {
            "name": contract.get("contract_id"),
            "version": contract.get("version"),
            "canonical_skill": "semantic-contract-resolver@1.1.0",
        },
        "evidence_requirements": copy.deepcopy(profile.get("evidence_requirements") or []),
        "accepted_evidence_types": copy.deepcopy(profile.get("accepted_evidence_types") or []),
        "required_outputs": copy.deepcopy(profile.get("required_outputs") or []),
        "profile_sha256": profile.get("profile_sha256"),
        "source_protocol": "FINFLUX_PROFILE_REGISTRY_V0.2",
    }


def _profiles() -> dict[str, dict[str, Any]]:
    formal = load_profile_registry()
    return {
        str(item["profile_id"]): _normalize_profile(item)
        for item in formal.get("profiles") or []
    }


def get_profile(profile_id: str) -> dict[str, Any] | None:
    profile = _profiles().get(str(profile_id or ""))
    return copy.deepcopy(profile) if profile else None


def list_profiles() -> dict[str, Any]:
    formal = load_profile_registry()
    profiles = [_normalize_profile(item) for item in formal.get("profiles") or []]
    unsigned = {
        "protocol": REGISTRY_PROTOCOL,
        "source_protocol": formal.get("protocol"),
        "source_registry_sha256": formal.get("registry_sha256"),
        "profiles": profiles,
        "count": len(profiles),
    }
    return {**unsigned, "registry_sha256": canonical_sha256(unsigned)}


def _unknown_profile() -> dict[str, Any]:
    return {
        "protocol": PROFILE_PROTOCOL,
        "profile_id": "unregistered_financial_evidence",
        "version": "NOT_REGISTERED",
        "display_name": "未登记金融资料",
        "asset_family": "unknown",
        "live_executable": False,
        "default_purpose": "evidence_review",
        "purpose_bindings": {},
        "summary_fields": [],
        "semantic_fields": [],
        "worker_policy": {"mode": "NO_EXECUTION"},
        "skill_policy": {"mode": "NO_EXECUTION"},
        "presentation_schema": [
            {"key": "evidence-list", "component": "evidence-list"}
        ],
        "profile_sha256": None,
    }


def purpose_binding(profile: dict[str, Any], purpose: str | None) -> dict[str, Any]:
    bindings = profile.get("purpose_bindings") or {}
    selected = str(purpose or profile.get("default_purpose") or "")
    return copy.deepcopy(
        bindings.get(selected)
        or bindings.get(profile.get("default_purpose"))
        or {}
    )


def _path_value(root: dict[str, Any], path: str) -> Any:
    value: Any = root
    for part in str(path).split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def build_profile_projection(
    profile_id: str,
    submission: dict[str, Any],
    precheck: dict[str, Any],
) -> dict[str, Any]:
    profile = get_profile(profile_id) or _unknown_profile()
    source = {
        "parsed": submission.get("parsed") or {},
        "metadata": submission.get("metadata") or {},
        "file": submission.get("file") or {},
        "precheck": precheck,
    }
    summary = []
    for field in profile.get("summary_fields") or []:
        value = _path_value(source, str(field.get("path") or ""))
        summary.append(
            {
                "key": field.get("key"),
                "label": field.get("label"),
                "value": value,
                "unit": field.get("unit") or "",
                "state": "PRESENT" if value not in (None, "") else "NOT_CAPTURED",
                "source_path": field.get("path"),
            }
        )
    metadata = submission.get("metadata") or {}
    purpose = str(
        metadata.get("declared_purpose") or profile.get("default_purpose") or ""
    )
    binding = purpose_binding(profile, purpose)
    return {
        "protocol": "FINFLUX_PROFILE_PRESENTATION_V1",
        "profile_id": profile.get("profile_id"),
        "profile_version": profile.get("version"),
        "profile_sha256": profile.get("profile_sha256"),
        "source_protocol": profile.get("source_protocol"),
        "display_name": profile.get("display_name"),
        "asset_family": profile.get("asset_family"),
        "live_executable": bool(profile.get("live_executable", False)),
        "declared_purpose": purpose,
        "purpose_label": binding.get("label") or purpose or "未声明",
        "purpose_options": [
            {"value": key, "label": value.get("label") or key}
            for key, value in (profile.get("purpose_bindings") or {}).items()
        ],
        "required_concept": binding.get("required_concept"),
        "required_field": binding.get("required_field"),
        "summary": summary,
        "semantic_fields": copy.deepcopy(profile.get("semantic_fields") or []),
        "worker_policy": copy.deepcopy(profile.get("worker_policy") or {}),
        "skill_policy": copy.deepcopy(profile.get("skill_policy") or {}),
        "presentation_schema": copy.deepcopy(profile.get("presentation_schema") or []),
    }


def build_contract_projection(
    profile_id: str,
    purpose: str,
    contract_payload: dict[str, Any],
    fallback_sha256: str | None,
    validation_state: str,
) -> dict[str, Any]:
    profile = get_profile(profile_id) or _unknown_profile()
    binding = purpose_binding(profile, purpose)
    contract = profile.get("contract") or {}
    return {
        "name": str(
            contract_payload.get("name")
            or contract.get("name")
            or f"{profile_id}Contract"
        ),
        "version": str(
            contract_payload.get("version")
            or contract.get("version")
            or "NOT_RESOLVED"
        ),
        "sha256": contract_payload.get("sha256") or fallback_sha256,
        "downstream_purpose": purpose,
        "purpose_label": binding.get("label") or purpose,
        "applicable_domain": profile_id,
        "canonical_skill": str(
            contract.get("canonical_skill")
            or "semantic-contract-resolver@1.1.0"
        ),
        "validation_state": validation_state,
        "required_concept": contract_payload.get("required_concept")
        or binding.get("required_concept"),
        "required_field": contract_payload.get("required_field")
        or binding.get("required_field"),
        "key_fields": copy.deepcopy(profile.get("semantic_fields") or []),
        "constraints": copy.deepcopy(binding.get("constraints") or []),
        "profile_sha256": profile.get("profile_sha256"),
    }


def impact_display(
    profile_id: str,
    purpose: str,
    *result_sources: dict[str, Any],
) -> dict[str, Any]:
    profile = get_profile(profile_id) or _unknown_profile()
    binding = purpose_binding(profile, purpose)
    metric = copy.deepcopy(
        binding.get("impact_metric")
        or {
            "value_paths": [],
            "label": "本Run尚未返回可复核影响指标",
            "unit": "",
            "field_unit": "",
        }
    )
    value = None
    for key in metric.get("value_paths") or []:
        for source in result_sources:
            if isinstance(source, dict) and source.get(key) is not None:
                value = source.get(key)
                break
        if value is not None:
            break
    metric["value"] = value
    return metric
