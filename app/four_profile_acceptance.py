from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bounded_worker_task import _discover_skill_registry, _execute_verified_skill
from context_capsule import (
    build_run_context_capsule,
    canonical_sha256 as context_sha256,
    load_role_context_slice,
)
from finchange_gate_core import (
    compute_financial_impact,
    reconcile_source_semantics,
    resolve_semantic_contract,
    validate_admission_package,
    verify_evidence_bundle,
)
from manager_routing import execute_manager_route_skill
from protocol_v02 import (
    build_case_envelope,
    build_datapass_draft,
    canonical_sha256,
    resolve_profile,
)
from research_data.core import (
    DATA_ROOT as RESEARCH_DATA_ROOT,
    RightsGate,
    ResearchDataStore,
    load_provider_registry,
    sha256_json,
    validate_research_item,
)
from research_data.investigator import inspect_cached_research


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = (
    PROJECT_ROOT / "agentteams" / "config" / "acceptance_cases_v0.2.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "agentteams" / "evidence" / "four-profile-zero-model-v0.2"
)
CONFIG_PROTOCOL = "FINFLUX_FOUR_PROFILE_ACCEPTANCE_CONFIG_V0.2"
RESULT_PROTOCOL = "FINFLUX_ZERO_MODEL_PROFILE_ACCEPTANCE_V0.2"
MANIFEST_PROTOCOL = "FINFLUX_FOUR_PROFILE_ACCEPTANCE_MANIFEST_V0.2"
EXECUTION_MODE = "NO_MODEL_CALL"
SUPPORTED_ACCEPTANCE_SCENARIOS = {"blocked", "post_remediation_review"}
EXPECTED_RECOMMENDATIONS = {"PASS", "BLOCK"}
ACCEPTANCE_PROFILE_IDS = {
    "equity_corporate_action",
    "futures_settlement",
    "option_contract_identity",
    "research_material_rights",
}

ROLE_BY_LABEL = {
    "Evidence Investigator": "evidence-investigator",
    "Semantic Impact Analyst": "semantic-impact-analyst",
    "Independent Validator": "independent-validator",
    "Data Rights Steward": "data-rights-steward",
    "Research Context Analyst": "research-context-analyst",
    "Runtime Resilience Auditor": "runtime-resilience-auditor",
}

CONFIG_FIELDS = {
    "profile_id",
    "case_id",
    "purpose_id",
    "purpose_statement",
    "trigger",
    "primary_evidence_path",
    "supporting_evidence_paths",
    "candidate_mapping",
    "required_mapping",
    "scenario",
    "expected_route",
    "expected_machine_recommendation",
    "candidate_configuration_truth",
    "rights_state",
    "source_asset",
    "rights_review_required",
    "research_context_required",
}


class AcceptanceFailure(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"acceptance config is unavailable: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "protocol",
        "schema_version",
        "execution_policy_id",
        "cases",
    }:
        raise AcceptanceFailure("acceptance config has unknown or missing top-level fields")
    if payload["protocol"] != CONFIG_PROTOCOL or payload["schema_version"] != "0.2.0":
        raise AcceptanceFailure("unsupported acceptance config protocol")
    if not isinstance(payload["cases"], list) or len(payload["cases"]) != 4:
        raise AcceptanceFailure("acceptance config must contain exactly four cases")
    profile_ids: list[str] = []
    for index, item in enumerate(payload["cases"]):
        if not isinstance(item, dict) or set(item) != CONFIG_FIELDS:
            raise AcceptanceFailure(f"acceptance case {index} has unknown or missing fields")
        profile_id = str(item.get("profile_id", ""))
        resolve_profile(profile_id)
        profile_ids.append(profile_id)
        if not isinstance(item["supporting_evidence_paths"], list):
            raise AcceptanceFailure(f"acceptance case {profile_id} has invalid supporting paths")
        scenario = str(item.get("scenario", ""))
        if scenario not in SUPPORTED_ACCEPTANCE_SCENARIOS:
            raise AcceptanceFailure(
                f"acceptance case {profile_id} has unsupported scenario {scenario!r}"
            )
        expected_recommendation = str(item.get("expected_machine_recommendation", ""))
        if expected_recommendation not in EXPECTED_RECOMMENDATIONS:
            raise AcceptanceFailure(
                f"acceptance case {profile_id} has invalid expected recommendation"
            )
        if scenario == "blocked" and expected_recommendation != "BLOCK":
            raise AcceptanceFailure(
                f"acceptance case {profile_id} blocked scenario must expect BLOCK"
            )
        if scenario != "blocked" and expected_recommendation != "PASS":
            raise AcceptanceFailure(
                f"acceptance case {profile_id} control scenario must expect PASS"
            )
        if (
            item.get("candidate_configuration_truth")
            != "EXPLICIT_COUNTERFACTUAL_CONTROL"
        ):
            raise AcceptanceFailure(
                f"acceptance case {profile_id} must disclose its candidate configuration boundary"
            )
        if item.get("source_asset") == "research" and scenario != "blocked":
            raise AcceptanceFailure(
                "research rights acceptance currently supports only the blocked rights boundary"
            )
    if set(profile_ids) != ACCEPTANCE_PROFILE_IDS or len(set(profile_ids)) != 4:
        raise AcceptanceFailure("acceptance config does not cover the frozen four profiles")
    return payload


def _resolve_evidence_path(relative: str) -> Path:
    path = (PROJECT_ROOT / relative).resolve()
    path.relative_to(PROJECT_ROOT.resolve())
    if not path.is_file():
        raise AcceptanceFailure(f"required real evidence is missing: {relative}")
    if path.stat().st_size <= 0:
        raise AcceptanceFailure(f"required real evidence is empty: {relative}")
    return path


def _research_evidence() -> dict[str, Any]:
    store = ResearchDataStore(RESEARCH_DATA_ROOT)
    items = store.load_items()
    if not items:
        raise AcceptanceFailure("research cache contains no real ResearchItem")
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8-sig"))
    quality = json.loads(store.quality_path.read_text(encoding="utf-8-sig"))
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    checks = {
        "manifest_self_hash": manifest.get("manifest_sha256")
        == sha256_json(unsigned_manifest),
        "items_file_hash": manifest.get("items_sha256")
        == _file_sha256(store.items_path),
        "declared_item_count": manifest.get("item_count") == len(items),
        "quality_report_pass": quality.get("status") == "PASS",
        "all_items_schema_valid": all(not validate_research_item(item) for item in items),
    }
    raw_failures: list[str] = []
    for raw in manifest.get("raw_files") or []:
        path = (store.root / str(raw.get("path", ""))).resolve()
        try:
            path.relative_to(store.root.resolve())
        except ValueError:
            raw_failures.append(str(raw.get("path", "")))
            continue
        if not path.is_file() or _file_sha256(path) != raw.get("sha256"):
            raw_failures.append(str(raw.get("path", "")))
    checks["raw_file_hashes"] = not raw_failures
    if not all(checks.values()):
        raise AcceptanceFailure(f"research cache integrity failed: {checks}; raw={raw_failures}")

    registry = load_provider_registry()
    gate = RightsGate(registry)
    rights_decisions: list[dict[str, Any]] = []
    unrestricted_decisions: list[dict[str, Any]] = []
    for item in items:
        action = (
            "STORE_STRUCTURED_DATA"
            if item.get("content_type") == "OFFICIAL_STATISTIC"
            and item.get("storage_policy") == "FULL_CONTENT"
            else "STORE_METADATA"
        )
        rights_decisions.append(
            gate.evaluate(str(item["provider_id"]), str(item["content_type"]), action)
        )
        unrestricted_decisions.append(
            gate.evaluate(
                str(item["provider_id"]),
                str(item["content_type"]),
                "STORE_UNRESTRICTED_FULLTEXT",
            )
        )
    if any(item["decision"] != "ALLOW" for item in rights_decisions):
        raise AcceptanceFailure("research cache contains an item without an allowed storage projection")
    investigation = inspect_cached_research("equity", max_items=12)
    if investigation.get("status") != "VERIFIED_METADATA":
        raise AcceptanceFailure("research context inspection did not verify cached metadata")
    return {
        "status": "VERIFIED",
        "checks": checks,
        "item_count": len(items),
        "provider_counts": manifest.get("provider_counts", {}),
        "content_type_counts": manifest.get("content_type_counts", {}),
        "rights_state_counts": manifest.get("rights_state_counts", {}),
        "permitted_projection_count": sum(
            item["decision"] == "ALLOW" for item in rights_decisions
        ),
        "unrestricted_fulltext_denied_count": sum(
            item["decision"] == "DENY" for item in unrestricted_decisions
        ),
        "manifest_sha256": manifest["manifest_sha256"],
        "items_sha256": manifest["items_sha256"],
        "context": investigation,
        "rights_decisions_sha256": canonical_sha256(rights_decisions),
        "unrestricted_decisions_sha256": canonical_sha256(unrestricted_decisions),
    }


def _real_case_evidence(case: dict[str, Any]) -> dict[str, Any]:
    primary = _resolve_evidence_path(str(case["primary_evidence_path"]))
    supporting = [
        _resolve_evidence_path(str(relative))
        for relative in case["supporting_evidence_paths"]
    ]
    source_asset = str(case["source_asset"])
    scenario = str(case["scenario"])
    if source_asset == "research":
        verification = _research_evidence()
        reconciliation = {
            "status": "RECONCILED",
            "difference_type": "provider_rights_and_storage_policy",
            "observed_only": True,
            "unrestricted_fulltext_denied_count": verification[
                "unrestricted_fulltext_denied_count"
            ],
        }
        profile = resolve_profile(str(case["profile_id"]))
        contract = {
            "status": "RESOLVED",
            "asset_class": "research",
            "contract_version": profile["semantic_contract"]["version"],
            "contract": {
                "name": profile["semantic_contract"]["contract_id"],
                "required_mapping": case["required_mapping"],
            },
        }
        impact = {
            "asset_class": "research",
            "case_id": case["case_id"],
            "observed": True,
            "impact_is_counterfactual": False,
            "item_count": verification["item_count"],
            "unrestricted_fulltext_denied_count": verification[
                "unrestricted_fulltext_denied_count"
            ],
            "permitted_projection_count": verification[
                "permitted_projection_count"
            ],
            "recommended_decision": "BLOCK",
            "financial_impact_status": "NOT_APPLICABLE",
        }
        validation = {
            "status": "PASS",
            "checks": {
                "evidence_verified": True,
                "rights_projection_available": verification[
                    "permitted_projection_count"
                ]
                == verification["item_count"],
                "unrestricted_fulltext_is_denied": verification[
                    "unrestricted_fulltext_denied_count"
                ]
                > 0,
                "contract_resolved": True,
            },
            "independent_recommendation": "BLOCK",
        }
    else:
        verification = verify_evidence_bundle(source_asset)
        if verification.get("status") != "VERIFIED":
            raise AcceptanceFailure(
                f"{case['profile_id']} real evidence verification failed: {verification}"
            )
        reconciliation = reconcile_source_semantics(source_asset)
        contract = resolve_semantic_contract(source_asset)
        impact = compute_financial_impact(source_asset, scenario)
        validation = validate_admission_package(source_asset, scenario)
        if validation.get("status") != "PASS":
            raise AcceptanceFailure(
                f"{case['profile_id']} independent validation failed: {validation}"
            )
    return {
        "primary_path": primary,
        "primary_sha256": _file_sha256(primary),
        "supporting_files": [
            {
                "path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in supporting
        ],
        "verification": verification,
        "reconciliation": reconciliation,
        "contract": contract,
        "impact": impact,
        "validation": validation,
    }


def _display_facts(case: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    impact = evidence["impact"]
    profile_id = case["profile_id"]
    if profile_id == "equity_corporate_action":
        return {
            "instrument": impact["anchor_instrument"],
            "event_date": impact["anchor_event_date"],
            "candidate_value": case["candidate_mapping"],
            "required_value": case["required_mapping"],
            "deterministic_impact": impact[
                "anchor_return_difference_percentage_points"
            ],
            "impact_unit": "percentage_points",
        }
    if profile_id == "futures_settlement":
        pass_control = impact.get("recommended_decision") == "PASS"
        return {
            "instrument": impact["instrument"],
            "event_date": impact["trade_date"],
            "candidate_value": (
                impact.get("selected_price") if pass_control else impact["close"]
            ),
            "required_value": impact["settle"],
            "deterministic_impact": (
                impact.get("financial_misstatement_cny_per_contract", 0.0)
                if pass_control
                else impact["absolute_impact_cny_per_contract"]
            ),
            "impact_unit": "CNY_per_contract",
        }
    if profile_id == "option_contract_identity":
        return {
            "instrument": impact["underlying"],
            "event_date": impact["effective_date"],
            "candidate_value": 10000,
            "required_value": 10260,
            "deterministic_impact": impact[
                "notional_understatement_cny_per_contract"
            ],
            "impact_unit": "CNY_per_contract_counterfactual",
        }
    return {
        "instrument": "RESEARCH-CATALOG",
        "event_date": "POINT_IN_TIME_METADATA",
        "candidate_value": case["candidate_mapping"],
        "required_value": case["required_mapping"],
        "deterministic_impact": evidence["impact"][
            "unrestricted_fulltext_denied_count"
        ],
        "impact_unit": "items_denied_for_unrestricted_fulltext",
    }


def _context_payload(
    case: dict[str, Any],
    run_id: str,
    envelope: dict[str, Any],
    evidence: dict[str, Any],
    precheck_sha256: str,
) -> dict[str, Any]:
    display = _display_facts(case, evidence)
    research = (
        evidence["verification"].get("context", {})
        if case["source_asset"] == "research"
        else inspect_cached_research(str(case["source_asset"]), max_items=6)
    )
    research_manifest = str(research.get("manifest_sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", research_manifest):
        raise AcceptanceFailure(f"{case['profile_id']} research manifest hash is missing")
    research_bundle_sha = canonical_sha256(research)
    unsigned = {
        "p": "FINFLUX_PROFILE_ACCEPTANCE_PAYLOAD_V0.2",
        "s": f"SUB-{canonical_sha256({'case_id': case['case_id']})[:20].upper()}",
        "f": case["profile_id"],
        "h": evidence["primary_sha256"],
        "r": canonical_sha256(
            [
                evidence["primary_sha256"],
                *[item["sha256"] for item in evidence["supporting_files"]],
            ]
        ),
        "g": "PASS",
        "i": str(display["instrument"]),
        "d": str(display["event_date"]),
        "c": display["candidate_value"],
        "t": display["required_value"],
        "m": case["candidate_mapping"],
        "x": 1,
        "ps": precheck_sha256,
        "pi": display["deterministic_impact"],
        "pr": evidence["impact"]["recommended_decision"],
        "rm": research_manifest,
        "rb": research_bundle_sha,
        "rc": int(research.get("selected_count", 0) or 0),
        "cl": "PUBLIC",
        "gb": "registered public-source or provider-specific rights decision",
        "us": "EVALUATION_ONLY",
        "ew": 600,
        "et": 90,
        "er": 0,
        "ec": True,
        "ce": envelope["envelope_sha256"],
        "rid": run_id,
    }
    # Context Capsule only projects role-allowlisted keys; extra envelope/run
    # bindings remain in this source fingerprint but cannot leak into a role.
    return {**unsigned, "ph": context_sha256(unsigned)}


def _worker_artifacts(
    *,
    case: dict[str, Any],
    envelope: dict[str, Any],
    route: dict[str, Any],
    context_handle: dict[str, Any],
    context_root: Path,
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    roles = [ROLE_BY_LABEL[label] for label in route["worker_plan"]["workers"]]
    artifacts: dict[str, Any] = {}
    datapass_worker_receipts: list[dict[str, Any]] = []
    datapass_skill_invocations: list[dict[str, Any]] = []
    for role in roles:
        slice_sha = context_handle["role_slice_handles"][role]["slice_sha256"]
        role_payload, context_load_receipt = load_role_context_slice(
            slice_sha,
            role,
            case_id=envelope["case_id"],
            run_id=envelope["run_id"],
            root=context_root,
            with_receipt=True,
        )
        registry = _discover_skill_registry(role)
        receipts: list[dict[str, Any]] = []
        outputs: dict[str, Any] = {}
        if role == "evidence-investigator":
            evidence_output, evidence_receipt = _execute_verified_skill(
                registry,
                "evidence-integrity",
                "1.0.0",
                {
                    "source_asset": case["source_asset"],
                    "registered_verification": evidence["verification"],
                },
            )
            rights_output, rights_receipt = _execute_verified_skill(
                registry,
                "rights-gate",
                "1.0.0",
                {
                    "rights_state": case["rights_state"],
                    "source_sha256": evidence["primary_sha256"],
                },
            )
            outputs = {
                "evidence": evidence_output,
                "source_semantics": evidence["reconciliation"],
            }
            receipts = [evidence_receipt, rights_receipt]
        elif role == "semantic-impact-analyst":
            contract_output, contract_receipt = _execute_verified_skill(
                registry,
                "semantic-contract-resolver",
                "1.1.0",
                {
                    "source_asset": case["source_asset"],
                    "registered_contract": evidence["contract"],
                },
            )
            impact_output, impact_receipt = _execute_verified_skill(
                registry,
                "financial-impact-calculator",
                "1.0.0",
                {
                    "source_asset": case["source_asset"],
                    "scenario": case["scenario"],
                    "registered_impact": evidence["impact"],
                },
            )
            outputs = {
                "semantic_contract": contract_output,
                "impact": impact_output,
            }
            receipts = [contract_receipt, impact_receipt]
        elif role == "independent-validator":
            validation_output, validation_receipt = _execute_verified_skill(
                registry,
                "independent-evidence-validator",
                "1.0.0",
                {
                    "source_asset": case["source_asset"],
                    "scenario": case["scenario"],
                    "registered_validation": evidence["validation"],
                },
            )
            outputs = {"independent_validation": validation_output}
            receipts = [validation_receipt]
        elif role == "data-rights-steward":
            classification_input = {
                "confidentiality_class": "PUBLIC",
                "rights_basis": "registered provider-specific rights decision",
                "permitted_scope": "EVALUATION_ONLY",
                "rights_gate": case["rights_state"],
                "rights_state_counts": evidence["verification"]["rights_state_counts"],
                "provider_counts": evidence["verification"]["provider_counts"],
                "permitted_projection_count": evidence["verification"][
                    "permitted_projection_count"
                ],
                "unrestricted_fulltext_denied_count": evidence["verification"][
                    "unrestricted_fulltext_denied_count"
                ],
                "rights_decisions_sha256": evidence["verification"][
                    "rights_decisions_sha256"
                ],
            }
            classification, classification_receipt = _execute_verified_skill(
                registry,
                "classify-data-rights",
                "1.0.0",
                classification_input,
            )
            rights_output, rights_receipt = _execute_verified_skill(
                registry,
                "enforce-confidentiality-boundary",
                "1.0.0",
                classification,
            )
            outputs = {
                "classification": classification,
                "rights_boundary": rights_output,
            }
            receipts = [classification_receipt, rights_receipt]
        elif role == "research-context-analyst":
            context = evidence["verification"]["context"]
            retrieved_context, retrieval_receipt = _execute_verified_skill(
                registry,
                "retrieve-research-context",
                "1.0.0",
                {"registered_context": context},
            )
            verification_input = {
                **retrieved_context,
                "bundle_sha256": canonical_sha256(retrieved_context),
            }
            verification, verification_receipt = _execute_verified_skill(
                registry,
                "verify-research-context",
                "1.0.0",
                verification_input,
            )
            outputs = {
                "research_context": retrieved_context,
                "verification": verification,
            }
            receipts = [retrieval_receipt, verification_receipt]
        else:
            raise AcceptanceFailure(f"zero-model acceptance has no executor for role {role}")

        observed_skill_ids = {receipt["skill_id"] for receipt in receipts}
        expected_for_role = {
            key.split("@", 1)[0] for key in registry["skills"]
        }
        if observed_skill_ids != expected_for_role:
            raise AcceptanceFailure(
                f"{case['profile_id']} {role} Skill receipt mismatch: "
                f"expected={sorted(expected_for_role)} observed={sorted(observed_skill_ids)}"
            )
        artifact_core = {
            "protocol": "FINFLUX_ZERO_MODEL_WORKER_ARTIFACT_V0.2",
            "case_id": envelope["case_id"],
            "run_id": envelope["run_id"],
            "role": role,
            "status": "SEALED",
            "execution_mode": EXECUTION_MODE,
            "provider_tokens": 0,
            "model_call": False,
            "context_slice_sha256": slice_sha,
            "context_load_receipt": context_load_receipt,
            "outputs": outputs,
            "skill_invocations": receipts,
        }
        artifact = {
            **artifact_core,
            "artifact_sha256": canonical_sha256(artifact_core),
        }
        artifacts[role] = artifact
        datapass_worker_receipts.append(
            {
                "worker_id": role,
                "status": "SEALED",
                "artifact_sha256": artifact["artifact_sha256"],
            }
        )
        for receipt in receipts:
            datapass_skill_invocations.append(
                {
                    "skill_id": receipt["skill_id"],
                    "version": receipt["version"],
                    "worker_id": role,
                    "input_sha256": receipt["input_sha256"],
                    "output_sha256": receipt["output_sha256"],
                    "status": "SUCCEEDED",
                }
            )
    required_skills = set(route["required_skill_versions"])
    observed_skills = {item["skill_id"] for item in datapass_skill_invocations}
    if required_skills != observed_skills:
        raise AcceptanceFailure(
            f"{case['profile_id']} route/Worker Skill mismatch: "
            f"missing={sorted(required_skills - observed_skills)} "
            f"unexpected={sorted(observed_skills - required_skills)}"
        )
    return artifacts, datapass_worker_receipts, datapass_skill_invocations


def _impact_metrics(case: dict[str, Any], evidence: dict[str, Any]) -> list[dict[str, Any]]:
    display = _display_facts(case, evidence)
    source_kind = (
        "COUNTERFACTUAL"
        if evidence["impact"].get("impact_is_counterfactual")
        else "DETERMINISTIC"
    )
    return [
        {
            "metric_id": "material_impact",
            "label": "本次核验的确定性影响",
            "value": display["deterministic_impact"],
            "unit": display["impact_unit"],
            "source_kind": source_kind,
        }
    ]


def _trace_summary(
    *,
    envelope: dict[str, Any],
    manager_receipt: dict[str, Any],
    context_handle: dict[str, Any],
    artifacts: dict[str, Any],
    datapass: dict[str, Any],
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []

    def add(stage: str, actor: str, artifact_sha256: str) -> None:
        core = {
            "sequence": len(events) + 1,
            "stage": stage,
            "actor": actor,
            "status": "SUCCESS",
            "artifact_sha256": artifact_sha256,
            "provider_tokens": 0,
            "model_call": False,
        }
        events.append({**core, "event_sha256": canonical_sha256(core)})

    add("CASE_ENVELOPE_VALIDATED", "admission-gateway", envelope["envelope_sha256"])
    add("MANAGER_ROUTE_SKILL_EXECUTED", "global-manager", manager_receipt["receipt_sha256"])
    add("ROLE_CONTEXT_SLICES_BUILT", "context-gateway", context_handle["capsule_sha256"])
    for role, artifact in artifacts.items():
        add("WORKER_SKILLS_EXECUTED", role, artifact["artifact_sha256"])
    add("DATAPASS_DRAFT_CREATED", "case-lead", datapass["datapass_sha256"])
    return {
        "protocol": "FINFLUX_ZERO_MODEL_TRACE_SUMMARY_V0.2",
        "run_id": envelope["run_id"],
        "event_count": len(events),
        "events": events,
        "provider_tokens": 0,
        "model_calls": 0,
        "execution_mode": EXECUTION_MODE,
        "trace_sha256": canonical_sha256(events),
    }


def _render_case_markdown(result: dict[str, Any]) -> str:
    route = result["manager_route"]
    datapass = result["datapass_draft"]
    evidence = result["evidence_summary"]
    return f"""# {result['profile']['display_name']}：零模型结构验收

- Case ID：`{result['case_id']}`
- Run ID：`{result['run_id']}`
- 执行方式：`{EXECUTION_MODE}`（未调用模型）
- Provider Token：`0`
- 真实证据：`{evidence['primary_path']}`
- 证据SHA256：`{evidence['primary_sha256']}`
- Manager 路由：`{route['route']}`
- 按需 Worker：{', '.join(route['worker_plan']['workers'])}
- 运行时 Skill：{', '.join(route['required_skill_versions'])}
- 机器建议：`{datapass['machine_recommendation']}`
- 业务处置：`{result['business_disposition']}`（机器草案，不是最终批准）
- 候选配置：`EXPLICIT_COUNTERFACTUAL_CONTROL`（原始金融记录未改写）
- Human Gate：`{datapass['human_gate']['state']}`（本验收不替代责任人审批）

## 验收结论

结构验证 `{result['acceptance_status']}`：真实证据、Manager 路由回执、角色隔离上下文、Worker Skill
输入输出哈希、DataPassDraft 与同 Run Trace 已形成；本结果只证明确定性基础设施结构可复现，
不声称完成AgentTeams模型推理、机构整改或生产批准。
"""


def _run_case(
    case: dict[str, Any],
    *,
    output_dir: Path,
    execution_policy_id: str,
    started_at_utc: str,
) -> dict[str, Any]:
    profile = resolve_profile(case["profile_id"])
    evidence = _real_case_evidence(case)
    scenario = str(case["scenario"])
    expected_recommendation = str(case["expected_machine_recommendation"])
    observed_recommendation = str(evidence["impact"].get("recommended_decision", ""))
    if observed_recommendation != expected_recommendation:
        raise AcceptanceFailure(
            f"{case['profile_id']} deterministic impact recommendation="
            f"{observed_recommendation} expected={expected_recommendation}"
        )
    run_suffix = canonical_sha256(
        {
            "profile_id": case["profile_id"],
            "primary_sha256": evidence["primary_sha256"],
            "scenario": scenario,
            "candidate_mapping": case["candidate_mapping"],
            "started_at_utc": started_at_utc,
        }
    )[:12]
    run_id = f"RUN-ACCEPT-{profile['asset_class'].upper()}-{run_suffix.upper()}"
    required_evidence_type = profile["evidence_requirements"][0]["evidence_type"]
    evidence_handle = {
        "evidence_id": f"EVID-ACCEPT-{run_suffix.upper()}",
        "evidence_type": required_evidence_type,
        "content_sha256": evidence["primary_sha256"],
        "source_locator": str(evidence["primary_path"].relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "media_type": (
            "application/jsonl"
            if evidence["primary_path"].suffix == ".jsonl"
            else "application/json"
        ),
        "rights_state": case["rights_state"],
        "version_id": f"sha256:{evidence['primary_sha256'][:16]}",
    }
    envelope = build_case_envelope(
        profile_id=case["profile_id"],
        case_id=case["case_id"],
        run_id=run_id,
        purpose_id=case["purpose_id"],
        purpose_statement=case["purpose_statement"],
        evidence_handles=[evidence_handle],
        trigger=case["trigger"],
        expected_route=case["expected_route"],
        execution_policy_id=execution_policy_id,
        created_at_utc=started_at_utc,
    )
    precheck = {
        "profile_id": case["profile_id"],
        "evidence_status": "VERIFIED",
        "candidate_mapping": case["candidate_mapping"],
        "required_mapping": case["required_mapping"],
        "scenario": scenario,
        "recommendation": observed_recommendation,
        "impact_sha256": canonical_sha256(evidence["impact"]),
    }
    precheck_sha = canonical_sha256(precheck)
    manager_facts = {
        "case_id": case["case_id"],
        "run_id": run_id,
        "submission_id": f"SUB-{run_suffix.upper()}",
        "asset_class": profile["asset_class"],
        "evidence_profile": case["profile_id"],
        "declared_downstream_use": case["purpose_id"],
        "rights_status": "PASS",
        "evidence_status": "VERIFIED",
        "evidence_hash_valid": True,
        "precheck_recommendation": observed_recommendation,
        "semantic_conflict_code": (
            "SEMANTIC_MAPPING_ALIGNED"
            if observed_recommendation == "PASS"
            else "SEMANTIC_MAPPING_CONFLICT"
        ),
        "precheck_sha256": precheck_sha,
        "evidence_sha256": evidence["primary_sha256"],
        "evidence_root_hash": canonical_sha256(
            [
                evidence["primary_sha256"],
                *[item["sha256"] for item in evidence["supporting_files"]],
            ]
        ),
        "budget_available": True,
        "review_mode": (
            "POST_REMEDIATION_REVIEW"
            if scenario == "post_remediation_review"
            else "ZERO_MODEL_PROFILE_ACCEPTANCE"
        ),
        "confidentiality_class": "PUBLIC",
        "rights_review_required": case["rights_review_required"],
        "research_context_required": case["research_context_required"],
        "operational_risk_review_required": False,
    }
    route, manager_receipt = execute_manager_route_skill(manager_facts)
    if route["route"] != case["expected_route"]:
        raise AcceptanceFailure(
            f"{case['profile_id']} Manager route={route['route']} expected={case['expected_route']}"
        )
    if route["machine_recommendation"] != expected_recommendation:
        raise AcceptanceFailure(
            f"{case['profile_id']} Manager recommendation="
            f"{route['machine_recommendation']} expected={expected_recommendation}"
        )
    roles = [ROLE_BY_LABEL[label] for label in route["worker_plan"]["workers"]]
    payload = _context_payload(case, run_id, envelope, evidence, precheck_sha)
    context_root = output_dir / "context_cache"
    _, context_handle = build_run_context_capsule(
        case_id=case["case_id"],
        run_id=run_id,
        payload=payload,
        selected_workers=roles,
        execution_policy_id=execution_policy_id,
        root_route_decision_handle={
            "decision_id": route["decision_id"],
            "input_facts_sha256": route["input_facts_sha256"],
            "decision_sha256": route["decision_sha256"],
        },
        local_root=context_root,
    )
    if set(context_handle["role_slice_handles"]) != set(roles):
        raise AcceptanceFailure(f"{case['profile_id']} role Context Slice set is incomplete")
    artifacts, worker_receipts, skill_invocations = _worker_artifacts(
        case=case,
        envelope=envelope,
        route=route,
        context_handle=context_handle,
        context_root=context_root,
        evidence=evidence,
    )
    datapass = build_datapass_draft(
        envelope=envelope,
        machine_recommendation=route["machine_recommendation"],
        reason_codes=route["reason_codes"],
        recommendation_summary=(
            "真实证据上的候选语义映射与声明用途一致，确定性复算未发现残余差异；"
            "本结果仍是待责任人审批的DataPassDraft。"
            if expected_recommendation == "PASS"
            else "现有真实证据显示候选语义映射不满足声明用途；保持隔离并提交责任人审批。"
        ),
        evidence_status="VERIFIED",
        evidence_quorum_met=True,
        semantic_status=(
            "RESOLVED" if expected_recommendation == "PASS" else "CONFLICT"
        ),
        impact_status=(
            "NOT_APPLICABLE"
            if case["source_asset"] == "research"
            else "COUNTERFACTUAL"
            if evidence["impact"].get("impact_is_counterfactual")
            else "COMPUTED"
        ),
        impact_facts=evidence["impact"],
        impact_metrics=_impact_metrics(case, evidence),
        required_worker_ids=roles,
        worker_receipts=worker_receipts,
        required_skill_ids=list(route["required_skill_versions"]),
        skill_invocations=skill_invocations,
        generated_at_utc=_utc_now(),
    )
    trace = _trace_summary(
        envelope=envelope,
        manager_receipt=manager_receipt,
        context_handle=context_handle,
        artifacts=artifacts,
        datapass=datapass,
    )
    return {
        "protocol": RESULT_PROTOCOL,
        "acceptance_status": "PASS",
        "verification_status": "VERIFIED",
        "business_disposition": f"MACHINE_{expected_recommendation}_DRAFT",
        "scenario": scenario,
        "execution_mode": EXECUTION_MODE,
        "provider_tokens": 0,
        "model_calls": 0,
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_sha256": profile["profile_sha256"],
            "display_name": profile["display_name"],
        },
        "case_id": case["case_id"],
        "run_id": run_id,
        "evidence_summary": {
            "status": "VERIFIED",
            "primary_path": evidence_handle["source_locator"],
            "primary_sha256": evidence["primary_sha256"],
            "supporting_files": evidence["supporting_files"],
            "verification_sha256": canonical_sha256(evidence["verification"]),
        },
        "case_envelope": envelope,
        "manager_route": route,
        "manager_skill_receipt": manager_receipt,
        "context_capsule_handle": context_handle,
        "worker_artifacts": artifacts,
        "datapass_draft": datapass,
        "trace_summary": trace,
        "truth_boundary": {
            "real_existing_evidence": True,
            "financial_source_records_real": True,
            "synthetic_financial_records": 0,
            "candidate_configuration_truth": case[
                "candidate_configuration_truth"
            ],
            "candidate_configuration_is_counterfactual": True,
            "observed_institution_remediation_claimed": False,
            "provider_tokens": 0,
            "model_called": False,
            "human_approval_claimed": False,
            "production_write_performed": False,
        },
    }


def run_four_profile_acceptance(
    *,
    config_path: Path = DEFAULT_CONFIG,
    output_dir: Path = DEFAULT_OUTPUT,
    started_at_utc: str | None = None,
) -> dict[str, Any]:
    started_at = started_at_utc or _utc_now()
    try:
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceFailure("started_at_utc must be ISO-8601") from exc
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = _load_config(config_path.resolve())
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for case in config["cases"]:
        try:
            result = _run_case(
                case,
                output_dir=output_dir,
                execution_policy_id=config["execution_policy_id"],
                started_at_utc=started_at,
            )
            slug = str(case["profile_id"])
            json_path = output_dir / f"{slug}.json"
            md_path = output_dir / f"{slug}.md"
            _write_json(json_path, result)
            _write_text(md_path, _render_case_markdown(result))
            results.append(
                {
                    "profile_id": slug,
                    "case_id": result["case_id"],
                    "run_id": result["run_id"],
                    "status": result["acceptance_status"],
                    "verification_status": result["verification_status"],
                    "business_disposition": result["business_disposition"],
                    "scenario": result["scenario"],
                    "route": result["manager_route"]["route"],
                    "worker_count": len(result["worker_artifacts"]),
                    "skill_count": len(result["datapass_draft"]["skills"]["invocations"]),
                    "machine_recommendation": result["datapass_draft"]["machine_recommendation"],
                    "primary_evidence_sha256": result["evidence_summary"]["primary_sha256"],
                    "provider_tokens": 0,
                    "model_calls": 0,
                    "json": {
                        "path": json_path.name,
                        "sha256": _file_sha256(json_path),
                    },
                    "markdown": {
                        "path": md_path.name,
                        "sha256": _file_sha256(md_path),
                    },
                }
            )
        except Exception as exc:  # fail-closed manifest preserves the exact profile failure
            failures.append(
                {
                    "profile_id": str(case.get("profile_id", "UNKNOWN")),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            break
    manifest_core = {
        "protocol": MANIFEST_PROTOCOL,
        "schema_version": "0.2.0",
        "generated_at_utc": _utc_now(),
        "started_at_utc": started_at,
        "status": "PASS" if len(results) == 4 and not failures else "FAIL_CLOSED",
        "execution_mode": EXECUTION_MODE,
        "provider_tokens": 0,
        "model_calls": 0,
        "case_count": len(results),
        "cases": results,
        "failures": failures,
        "truth_boundary": (
            "This manifest proves a deterministic, zero-provider-token infrastructure acceptance only. "
            "It does not claim a model Run, Human approval, or production authorization."
        ),
    }
    manifest = {
        **manifest_core,
        "manifest_sha256": canonical_sha256(manifest_core),
    }
    _write_json(output_dir / "acceptance_manifest.json", manifest)
    if manifest["status"] != "PASS":
        raise AcceptanceFailure(
            f"four-profile acceptance failed closed: {json.dumps(failures, ensure_ascii=False)}"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the FinFlux four-profile zero-model acceptance chain."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--started-at", default=None)
    args = parser.parse_args()
    try:
        manifest = run_four_profile_acceptance(
            config_path=args.config,
            output_dir=args.output_dir,
            started_at_utc=args.started_at,
        )
    except AcceptanceFailure as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
