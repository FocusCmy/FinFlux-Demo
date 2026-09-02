from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEMO_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DEMO_ROOT.parent
# In the repository the evidence lives under 赛题分析方案. AgentTeams worker
# packages carry the same immutable evidence under workspace/data so the
# deterministic tools do not depend on a host-only absolute path.
PACKAGED_VALIDATION_ROOT = DEMO_ROOT / "data"
VALIDATION_ROOT = (
    PACKAGED_VALIDATION_ROOT
    if (PACKAGED_VALIDATION_ROOT / "evidence_cross_asset").exists()
    else PROJECT_ROOT / "赛题分析方案" / "阶段0_72小时验证"
)
EVIDENCE_ROOT = VALIDATION_ROOT / "evidence_cross_asset"
READINESS_PATH = EVIDENCE_ROOT / "results" / "cross_asset_readiness.json"
MANIFEST_PATH = EVIDENCE_ROOT / "manifest.json"
STOCK_BATCH_PATH = (
    VALIDATION_ROOT / "evidence_2026_batch" / "batch_verified_2026_final.json"
)
CONTRACTS_PATH = DEMO_ROOT / "semantic_contracts.json"

ASSETS = ("equity", "futures", "option")
SCENARIOS = ("blocked", "admissible", "post_remediation_review")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_asset(asset: str) -> None:
    if asset not in ASSETS:
        raise ValueError(f"Unsupported asset {asset!r}; choose one of {ASSETS}")


def _require_scenario(scenario: str) -> None:
    if scenario not in SCENARIOS:
        raise ValueError(
            f"Unsupported scenario {scenario!r}; choose one of {SCENARIOS}"
        )


def resolve_semantic_contract(asset: str) -> dict[str, Any]:
    _require_asset(asset)
    contracts = _load_json(CONTRACTS_PATH)
    contract = contracts[asset]
    return {
        "asset_class": asset,
        "contract_version": contracts["contract_version"],
        "contract": contract,
        "status": "RESOLVED",
    }


def compute_financial_impact(
    asset: str, scenario: str = "blocked"
) -> dict[str, Any]:
    _require_asset(asset)
    _require_scenario(scenario)
    readiness = _load_json(READINESS_PATH)

    if asset == "equity":
        batch = _load_json(STOCK_BATCH_PATH)
        anchor = batch["cases"][0]
        blocked = {
            "asset_class": asset,
            "case_id": readiness["assets"][asset]["case_id"],
            "scenario": scenario,
            "observed": True,
            "events_analysed": batch["summary"]["events_analysed"],
            "unique_instruments": batch["summary"]["unique_instruments_analysed"],
            "semantic_mismatch_events": batch["summary"][
                "type_1_semantic_mismatch_verified_count"
            ],
            "event_row_loss_events": batch["summary"][
                "type_2_corporate_action_row_drop_verified_count"
            ],
            "anchor_instrument": anchor["instrument"],
            "anchor_event_date": anchor["event_date"],
            "anchor_return_difference_percentage_points": round(
                anchor["returns"]["difference_percentage_points"], 6
            ),
            "recommended_decision": "BLOCK",
        }
        if scenario == "blocked":
            return blocked
        return {
            **blocked,
            "case_id": (
                f"{blocked['case_id']}-POST-REMEDIATION-REVIEW"
                if scenario == "post_remediation_review"
                else f"{blocked['case_id']}-ADMISSIBLE-CONTROL"
            ),
            "impact_is_control_mapping": True,
            "control_mapping": {
                "declared_adjustment_type": "qfq",
                "observed_series_behavior": "qfq",
                "corporate_action_event_row_policy": "preserve_raw_event_row",
            },
            "remaining_semantic_mismatches_after_mapping": 0,
            "remaining_event_row_losses_after_preserve_policy": 0,
            "recommended_decision": "PASS",
        }

    decision = readiness["assets"][asset]["decision"]
    if asset == "futures":
        blocked = {
            "asset_class": asset,
            "case_id": decision["case_id"],
            "scenario": scenario,
            "observed": True,
            "instrument": decision["instrument"],
            "trade_date": decision["trade_date"],
            "close": decision["close"],
            "settle": decision["settle"],
            "difference_points": decision[
                "difference_points_close_minus_settle"
            ],
            "absolute_impact_cny_per_contract": decision[
                "absolute_impact_cny_per_contract"
            ],
            "recommended_decision": decision["decision"],
        }
        if scenario == "blocked":
            return blocked
        return {
            **blocked,
            "case_id": (
                f"{blocked['case_id']}-POST-REMEDIATION-REVIEW"
                if scenario == "post_remediation_review"
                else f"{blocked['case_id']}-ADMISSIBLE-CONTROL"
            ),
            "impact_is_control_mapping": True,
            "declared_downstream_use": "daily_settlement_pnl",
            "selected_price_field": "settle",
            "selected_price": decision["settle"],
            "contract_required_price_field": "settle",
            "field_mapping_difference_points": 0.0,
            "financial_misstatement_cny_per_contract": 0.0,
            "recommended_decision": "PASS",
        }

    blocked = {
        "asset_class": asset,
        "case_id": decision["case_id"],
        "scenario": scenario,
        "observed_contract_terms": True,
        "impact_is_counterfactual": True,
        "underlying": decision["underlying"],
        "effective_date": decision["effective_date"],
        "covered_security_shortfall_shares_per_contract": decision[
            "covered_security_shortfall_shares_per_contract"
        ],
        "notional_understatement_cny_per_contract": decision[
            "notional_understatement_cny_per_contract"
        ],
        "recommended_decision": decision["decision"],
    }
    if scenario == "blocked":
        return blocked
    return {
        **blocked,
        "case_id": (
            f"{blocked['case_id']}-POST-REMEDIATION-REVIEW"
            if scenario == "post_remediation_review"
            else f"{blocked['case_id']}-ADMISSIBLE-CONTROL"
        ),
        "impact_is_control_mapping": True,
        "selected_adjustment_version": "A",
        "selected_contract_unit": decision["adjusted_contract_unit"],
        "selected_exercise_price": decision["example_adjusted_strike"],
        "covered_security_shortfall_shares_per_contract": 0,
        "notional_understatement_cny_per_contract": 0.0,
        "recommended_decision": "PASS",
    }


def _verify_cross_asset_manifest() -> dict[str, Any]:
    manifest = _load_json(MANIFEST_PATH)
    mismatches = []
    missing = []
    for item in manifest["files"]:
        # The evidence manifest was generated on Windows and deliberately
        # preserves its original relative names.  Worker packages run on
        # Linux, so treating a backslash as a literal filename made every
        # otherwise valid evidence file appear missing.  Resolve the manifest
        # path as platform-neutral components without changing the signed
        # manifest or its hashes.
        relative_parts = str(item["file"]).replace("\\", "/").split("/")
        path = EVIDENCE_ROOT.joinpath(*relative_parts)
        if not path.exists():
            missing.append(item["file"])
        elif _sha256(path) != item["sha256"]:
            mismatches.append(item["file"])
    return {
        "manifest_file_count": len(manifest["files"]),
        "missing_files": missing,
        "hash_mismatches": mismatches,
        "valid": not missing and not mismatches,
    }


def _verify_stock_batch() -> dict[str, Any]:
    readiness = _load_json(READINESS_PATH)
    expected_summary_hash = readiness["assets"]["equity"]["summary_sha256"]
    summary_hash_matches = _sha256(STOCK_BATCH_PATH) == expected_summary_hash
    batch = _load_json(STOCK_BATCH_PATH)
    raw_root = STOCK_BATCH_PATH.parent / "raw"
    missing = []
    mismatches = []
    checked: set[str] = set()
    for case in batch["cases"]:
        for file_key, hash_key in (
            ("tencent_raw_file", "tencent_raw_sha256"),
            ("sina_unadjusted_file", "sina_unadjusted_sha256"),
        ):
            filename = case[file_key]
            check_key = f"{filename}:{case[hash_key]}"
            if check_key in checked:
                continue
            checked.add(check_key)
            path = raw_root / filename
            if not path.exists():
                missing.append(filename)
            elif _sha256(path) != case[hash_key]:
                mismatches.append(filename)
    return {
        "summary_hash_matches": summary_hash_matches,
        "raw_files_checked": len(checked),
        "missing_files": missing,
        "hash_mismatches": mismatches,
        "valid": summary_hash_matches and not missing and not mismatches,
    }


def verify_evidence_bundle(asset: str) -> dict[str, Any]:
    _require_asset(asset)
    if asset == "equity":
        result = _verify_stock_batch()
    else:
        result = _verify_cross_asset_manifest()
    return {
        "asset_class": asset,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        **result,
        "status": "VERIFIED" if result["valid"] else "INVALID",
    }


def reconcile_source_semantics(asset: str) -> dict[str, Any]:
    """Expose observed cross-source/field differences without choosing a use."""
    _require_asset(asset)
    readiness = _load_json(READINESS_PATH)
    if asset == "equity":
        batch = _load_json(STOCK_BATCH_PATH)
        summary = batch["summary"]
        detail = {
            "sources": ["FinShare 2.1.0 Tencent adapter", "AKShare 1.18.84 Sina unadjusted"],
            "events_analysed": summary["events_analysed"],
            "semantic_mismatch_events": summary[
                "type_1_semantic_mismatch_verified_count"
            ],
            "event_row_loss_events": summary[
                "type_2_corporate_action_row_drop_verified_count"
            ],
            "difference_type": "adjustment_label_and_event_row_policy",
        }
    elif asset == "futures":
        decision = readiness["assets"][asset]["decision"]
        detail = {
            "source": "CFFEX via AKShare 1.18.84",
            "instrument": decision["instrument"],
            "trade_date": decision["trade_date"],
            "close": decision["close"],
            "settle": decision["settle"],
            "difference_points": decision["difference_points_close_minus_settle"],
            "difference_type": "field_semantics_for_declared_use",
        }
    else:
        decision = readiness["assets"][asset]["decision"]
        detail = {
            "sources": ["SSE adjustment announcement", "AKShare current contracts"],
            "underlying": decision["underlying"],
            "effective_date": decision["effective_date"],
            "original_contract_unit": decision["original_contract_unit"],
            "adjusted_contract_unit": decision["adjusted_contract_unit"],
            "adjustment_marker": decision["required_contract_mapping"][
                "trading_code_marker"
            ],
            "difference_type": "contract_identity_version",
        }
    return {
        "asset_class": asset,
        "status": "RECONCILED",
        "observed_only": True,
        "detail": detail,
    }


def validate_admission_package(
    asset: str, scenario: str = "blocked"
) -> dict[str, Any]:
    """Independently validate the deterministic inputs to a DataPass draft."""
    _require_scenario(scenario)
    evidence = verify_evidence_bundle(asset)
    reconciliation = reconcile_source_semantics(asset)
    contract = resolve_semantic_contract(asset)
    impact = compute_financial_impact(asset, scenario)
    checks = {
        "evidence_verified": evidence["status"] == "VERIFIED",
        "source_semantics_reconciled": reconciliation["status"] == "RECONCILED",
        "contract_resolved": contract["status"] == "RESOLVED",
        "recommendation_present": impact.get("recommended_decision")
        in {"PASS", "BLOCK"},
        "remediation_zero_residual": (
            scenario != "post_remediation_review"
            or (
                impact.get("field_mapping_difference_points", 0) == 0
                and impact.get("financial_misstatement_cny_per_contract", 0) == 0
            )
        ),
    }
    valid = all(checks.values())
    return {
        "asset_class": asset,
        "scenario": scenario,
        "case_id": impact["case_id"],
        "status": "PASS" if valid else "DISAGREEMENT",
        "checks": checks,
        "independent_recommendation": (
            impact["recommended_decision"] if valid else "NEEDS_EVIDENCE"
        ),
        "evidence_status": evidence["status"],
        "contract_status": contract["status"],
        "reconciliation_status": reconciliation["status"],
        "key_impact": impact,
    }


def generate_datapass(
    asset: str, scenario: str = "blocked"
) -> dict[str, Any]:
    _require_scenario(scenario)
    contract = resolve_semantic_contract(asset)
    impact = compute_financial_impact(asset, scenario)
    evidence = verify_evidence_bundle(asset)
    evidence_quorum = all(
        [
            contract["status"] == "RESOLVED",
            evidence["status"] == "VERIFIED",
            "recommended_decision" in impact,
        ]
    )
    recommendation = impact["recommended_decision"] if evidence_quorum else "BLOCK"
    admission_route = (
        "POST_REMEDIATION_FULL_TEAM_REVIEW"
        if scenario == "post_remediation_review"
        else "CODE_ONLY_PASS"
        if recommendation == "PASS"
        else "FULL_TEAM_REVIEW"
    )
    code_only = admission_route == "CODE_ONLY_PASS"
    return {
        "datapass_version": "0.1.0-draft",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "asset_class": asset,
        "scenario": scenario,
        "case_id": impact["case_id"],
        "contract": {
            "name": contract["contract"]["name"],
            "version": contract["contract_version"],
        },
        "evidence_quorum": evidence_quorum,
        "evidence_status": evidence["status"],
        "impact": impact,
        "agent_recommendation": recommendation,
        "verification_status": recommendation,
        "admission_route": admission_route,
        "datapass_status": "DRAFT_CREATED",
        "release_status": "NOT_REQUESTED" if code_only else "AGENT_REVIEW_REQUIRED",
        "human_decision": "NOT_REQUIRED_FOR_PRECHECK" if code_only else "NOT_YET_AVAILABLE",
        "signed": False,
        "final_status": "PRECHECK_PASS" if code_only else "AGENT_REVIEW_REQUIRED",
        "integrity_boundary": {
            "raw_evidence_mutated": False,
            "production_write_performed": False,
            "remediation_output": "PROPOSED_MAPPING_OR_RULE_ONLY",
        },
        "owner": "demo-data-owner",
        "limitations": [
            "This is a deterministic verification draft, not a mutation of raw evidence.",
            "It is not a production authorization or an observed institution loss record.",
        ],
    }
