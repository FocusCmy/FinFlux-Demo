from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from manager_routing import (
    build_change_root_route_decision,
    build_live_root_route_decision,
)
from result_composer_agent import REPORT_SKILLS, ResultComposerAgent
from decision_reports import verify_result_artifacts
from run_lifecycle import bootstrap_lifecycle, record_transition
from emergency_stop import validate_emergency_stop_record
from structured_memory import StructuredMemoryStore
from context_memory import (
    HumanApproval,
    MemoryBudget,
    MemoryReference,
    build_context_memory_adapter,
    content_sha256 as context_content_sha256,
    memory_setting as context_memory_setting,
)
from change_control import (
    canonical_sha256 as change_control_sha256,
    detect_version_change,
    resolve_downstream_lineage,
    validate_remediation_plan,
)
from profile_registry import (
    build_contract_projection,
    build_profile_projection,
    impact_display as profile_impact_display,
)

try:
    from protocol_v02 import (
        CASE_ENVELOPE_PROTOCOL as FORMAL_CASE_ENVELOPE_PROTOCOL,
        DATAPASS_PROTOCOL as FORMAL_DATAPASS_PROTOCOL,
        ProtocolValidationError as FormalProtocolValidationError,
        build_case_envelope as build_formal_case_envelope,
        build_datapass_draft as build_formal_datapass_draft,
        resolve_profile as resolve_formal_profile,
        validate_case_envelope as validate_formal_case_envelope,
        validate_datapass as validate_formal_datapass,
    )
except ImportError:  # pragma: no cover - legacy standalone deployments
    FORMAL_CASE_ENVELOPE_PROTOCOL = "FINFLUX_CASE_ENVELOPE_V0.2"
    FORMAL_DATAPASS_PROTOCOL = "FINFLUX_DATAPASS_V0.2"
    FormalProtocolValidationError = ValueError
    build_formal_case_envelope = None
    build_formal_datapass_draft = None
    resolve_formal_profile = None
    validate_formal_case_envelope = None
    validate_formal_datapass = None


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CASE_INSTRUCTION_CHARS = 2000
ALLOWED_SUFFIXES = {
    ".csv", ".json", ".txt", ".md", ".xml", ".html", ".htm",
    ".xlsx", ".pdf", ".zip",
}
FUTURES_PROFILE_ID = "futures_settlement"
EQUITY_PROFILE_ID = "equity_corporate_action"
FUND_PROFILE_ID = "fund_nav_admission"
LIVE_PROFILE_IDS = (FUTURES_PROFILE_ID, EQUITY_PROFILE_ID, FUND_PROFILE_ID)
# Backward-compatible name used by the existing futures acceptance suite.
PROFILE_ID = FUTURES_PROFILE_ID
UNIVERSAL_PROFILE_ID = "universal_financial_evidence"
CONTRACT_VERSION = "FuturesSettlementContract@0.2.0"
LIVE_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    FUTURES_PROFILE_ID: {
        "asset_class": "futures",
        "default_purpose": "daily_settlement_pnl",
        "purpose_statement": "核验已登记期货价格字段能否用于交易所逐日结算盈亏口径",
        "accountable_role": "期货估值负责人",
        "evidence_type": "futures_settlement_bundle",
        "contract": "FuturesSettlementContract@0.2.0",
        "required_field": "settle",
        "date_field": "trade_date",
    },
    EQUITY_PROFILE_ID: {
        "asset_class": "equity",
        "default_purpose": "return_analysis",
        "purpose_statement": "核验公司行动事件前后的股票行情序列能否用于可比收益率分析",
        "accountable_role": "量化研究负责人",
        "evidence_type": "equity_corporate_action_bundle",
        "contract": "EquityCorporateActionContract@0.2.0",
        "required_field": "qfq",
        "date_field": "event_date",
    },
    FUND_PROFILE_ID: {
        "asset_class": "fund",
        "default_purpose": "holding_valuation",
        "purpose_statement": "核验公开基金净值字段能否用于声明的持仓估值或累计收益分析",
        "accountable_role": "基金估值负责人",
        "evidence_type": "fund_nav_bundle",
        "contract": "FundNetAssetValueContract@0.2.0",
        "required_field": "unit_nav",
        "date_field": "nav_date",
    },
}
SKILLS = [
    ("evidence-integrity", "1.0.0", "文件哈希、清单和来源完整性校验"),
    ("rights-gate", "1.0.0", "采集来源与使用边界检查"),
    ("semantic-contract-resolver", "1.1.0", "下游用途到金融字段语义的确定性绑定"),
    ("financial-impact-calculator", "1.0.0", "错配金额的确定性复算"),
    ("independent-evidence-validator", "1.0.0", "独立复核输入、契约与计算结果"),
    ("classify-data-rights", "1.0.0", "将权属声明、密级与允许用途归一为可审计分类"),
    ("enforce-confidentiality-boundary", "1.0.0", "按密级限制原文进入模型和跨角色共享"),
    ("retrieve-research-context", "1.0.0", "按Run级哈希句柄读取已固化研究背景"),
    ("verify-research-context", "1.0.0", "核验研究证据包并保持背景与金融真值边界"),
    ("guard-execution-budget", "1.0.0", "核验墙钟、工具超时、重试与Token真值边界"),
    ("audit-recovery-readiness", "1.0.0", "核验检查点和失败回执，不夸大未实证的故障迁移"),
    ("build-run-context-capsule", "1.0.0", "把同一Run的结构化上下文固化为内容寻址对象，避免跨Agent重复传输"),
    ("load-role-context-slice", "1.0.0", "按Case、Run与角色校验哈希后仅加载最小必要上下文切片"),
]
CHANGE_CONTROL_SKILLS = [
    (
        "detect-version-change",
        "1.0.0",
        "比较两个不可变 EvidenceBundle，生成只陈述已观察变化的 ChangeSet",
    ),
    (
        "resolve-downstream-lineage",
        "1.0.0",
        "按显式依赖解析变更影响范围；缺失血缘必须标记 UNKNOWN_IMPACT",
    ),
    (
        "validate-remediation-plan",
        "1.0.0",
        "校验修复映射、下游动作和回滚引用是否完整，不代替 Human 批准",
    ),
]

INTAKE_INSPECTION_SKILLS = [
    ("detect-financial-file-structure", "1.0.0"),
    ("match-provider-evidence-fingerprint", "1.0.0"),
    ("select-financial-profile", "1.0.0"),
]

# These records are produced by the AgentTeams adapter and are part of the
# strict V0.2 acceptance surface.  The Live repository is the API-facing
# projection, so omitting one here would make a real, valid adapter Run
# impossible to verify from the public Run endpoint.  Keep this an explicit
# allowlist: arbitrary adapter internals must not leak into the signed Run.
AGENTTEAMS_ACCEPTANCE_FIELDS = (
    "formal_case_envelope_handle",
    "matrix_case_envelope_handle",
    "prompt_budget_readiness",
    "session_hygiene",
    "manager_dispatch_mode",
    "manager_dispatch_receipt",
    "leader_relay",
    "task_convergence_receipt",
    "provider_usage_baseline",
    "provider_usage_baseline_guard",
    "model_gateway_binding",
    "model_gateway_close_receipt",
    "model_gateway_actor_binding_receipt",
    "model_gateway_identity_cleanup",
    "model_execution_seal_status",
    "worker_tool_execution_receipts",
    "sealed_artifact_aggregation",
)

# Deterministic public contract metadata.  This registry is deliberately small:
# an unknown contract is not guessed and remains a required confirmation.
FUTURES_MULTIPLIERS = {
    "IF": 300.0,
    "IH": 300.0,
    "IC": 200.0,
    "IM": 200.0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def require_v02_export_ready(
    run: dict[str, Any], *, require_final_artifacts: bool
) -> dict[str, Any]:
    """Fail closed before a V0.2 preview/final/audit artifact is composed.

    Historical runs stay readable, but a native V0.2 Run may only produce a
    polished artifact after the independent acceptance validator can recompute
    the same-Run Manager, Worker, Skill, Token and Human evidence.  This keeps
    the web download endpoints from becoming a shortcut around the acceptance
    CLI.
    """

    if run.get("protocol") != "FINFLUX_LIVE_RUN_V0.2":
        return {"status": "LEGACY_NOT_APPLICABLE", "failures": []}
    if run.get("agentteams_adapter_protocol") == "FINFLUX_AGENTTEAMS_RUN_V0.3":
        try:
            from refactored_acceptance import validate_refactored_run
        except ModuleNotFoundError:  # pragma: no cover - package import mode
            from .refactored_acceptance import validate_refactored_run
        validation = validate_refactored_run(
            run, require_final=require_final_artifacts
        )
    else:
        try:
            from v02_live_acceptance import validate_run
        except ModuleNotFoundError:  # pragma: no cover - package import mode
            from .v02_live_acceptance import validate_run
        validation = validate_run(run, require_final=require_final_artifacts)
    if validation.get("status") != "PASS":
        failures = ",".join(str(item) for item in validation.get("failures") or [])
        pending = ",".join(str(item) for item in validation.get("pending") or [])
        raise ValueError(
            "V0.2严格验收未通过，拒绝生成或导出结果"
            f"；failures={failures or 'NONE'}；pending={pending or 'NONE'}"
        )
    return validation


def _evidence_media_type(filename: str) -> str:
    return {
        ".csv": "text/csv",
        ".json": "application/json",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".xml": "application/xml",
        ".html": "text/html",
        ".htm": "text/html",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf": "application/pdf",
        ".zip": "application/zip",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


def build_live_formal_case_envelope(
    submission: dict[str, Any],
    *,
    run_id: str,
    case_id: str,
    expected_route: str,
    execution_policy_id: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Create the native V0.2 financial contract for a fresh Live Run.

    The evidence handle binds the immutable source bytes, not a parsed or
    model-produced projection.  Code-only admission uses the frozen protocol's
    ``CODE_ONLY_PASS`` route spelling; AgentTeams routes retain their exact
    Manager decision.
    """
    if build_formal_case_envelope is None or validate_formal_case_envelope is None:
        raise RuntimeError("FINFLUX_CASE_ENVELOPE_V0.2 validator is unavailable")
    profile_id = str(submission.get("profile") or "")
    spec = LIVE_PROFILE_SPECS.get(profile_id)
    if not spec:
        raise ValueError("fresh Live Run requires a registered futures, equity or fund Profile")
    file_record = submission.get("file") or {}
    source_sha256 = str(file_record.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("immutable source SHA256 is missing")
    metadata = submission.get("metadata") or {}
    purpose_id = str(metadata.get("declared_purpose") or "")
    if resolve_formal_profile is None:
        raise RuntimeError("FINFLUX Profile registry resolver is unavailable")
    formal_profile = resolve_formal_profile(profile_id)
    purposes = {
        str(item.get("purpose_id")): item
        for item in formal_profile.get("declared_purposes") or []
    }
    purpose = purposes.get(purpose_id)
    if not purpose:
        raise ValueError(f"purpose {purpose_id!r} is not registered for {profile_id}")
    route = (
        "CODE_ONLY_PASS"
        if expected_route == "CODE_ONLY_PRECHECK"
        else "HUMAN_REVIEW_WITHOUT_IMPACT"
        if expected_route == "NEEDS_EVIDENCE"
        else expected_route
    )
    envelope = build_formal_case_envelope(
        profile_id=profile_id,
        case_id=case_id,
        source_case_id=case_id,
        run_id=run_id,
        purpose_id=purpose_id,
        purpose_statement=str(spec["purpose_statement"]),
        accountable_role=str(
            purpose.get("default_accountable_role") or spec["accountable_role"]
        ),
        evidence_handles=[
            {
                "evidence_id": str(submission.get("evidence_bundle_id") or ""),
                "evidence_type": str(spec["evidence_type"]),
                "content_sha256": source_sha256,
                "source_locator": str(file_record.get("immutable_object") or ""),
                "media_type": _evidence_media_type(str(file_record.get("name") or "")),
                "rights_state": (
                    "PUBLIC"
                    if str(metadata.get("confidentiality_class") or "PUBLIC").upper()
                    == "PUBLIC"
                    else "AUTHORIZED"
                ),
                "version_id": str(submission.get("submission_id") or ""),
            }
        ],
        trigger="LIVE_EVIDENCE_BUNDLE_SUBMISSION",
        expected_route=route,
        execution_policy_id=execution_policy_id,
        created_at_utc=created_at_utc,
        legacy_reference=None,
    )
    validate_formal_case_envelope(envelope)
    return envelope


def datapass_presentation_projection(datapass: dict[str, Any]) -> dict[str, Any]:
    """Normalize formal and historical DataPass fields for the frontend only."""
    if not isinstance(datapass, dict) or not datapass:
        return {}
    if datapass.get("protocol") != FORMAL_DATAPASS_PROTOCOL:
        return datapass
    workers = datapass.get("workers") or {}
    skills = datapass.get("skills") or {}
    impact = datapass.get("impact_assessment") or {}
    metrics = {
        str(item.get("metric_id")): item
        for item in (impact.get("metrics") or [])
        if isinstance(item, dict)
    }
    primary_impact = metrics.get("financial_misstatement_cny_per_contract") or next(
        iter(metrics.values()), {}
    )
    semantic = datapass.get("semantic_assessment") or {}
    return {
        **datapass,
        "worker_artifact_count": int(workers.get("reported_completed_count") or 0),
        "required_worker_count": int(workers.get("reported_required_count") or 0),
        "worker_ids": list(workers.get("completed_worker_ids") or []),
        "skill_invocations": list(skills.get("invocations") or []),
        "required_skill_invocation_count": len(skills.get("required_skill_ids") or []),
        "observed_skill_invocation_count": len(skills.get("invocations") or []),
        "skill_attestation_status": skills.get("attestation_status"),
        "draft_sha256": datapass.get("datapass_sha256"),
        "semantic_contract": {
            "name": semantic.get("contract_id"),
            "version": semantic.get("version"),
            "validation_state": semantic.get("status"),
        },
        "formal_impact": {
            "value": primary_impact.get("value"),
            "unit": primary_impact.get("unit"),
            "label": primary_impact.get("label"),
        },
    }


WORKER_CATALOG: dict[str, dict[str, str]] = {
    "evidence-investigator": {
        "display_name": "Evidence Investigator",
        "duty": "证据完整性与来源复核",
        "color": "green",
    },
    "semantic-impact-analyst": {
        "display_name": "Semantic Impact Analyst",
        "duty": "金融语义与金额影响复核",
        "color": "purple",
    },
    "downstream-impact-analyst": {
        "display_name": "Downstream Impact Analyst",
        "duty": "下游依赖与变化影响范围复核",
        "color": "blue",
    },
    "data-rights-steward": {
        "display_name": "Data Rights Steward",
        "duty": "数据权属、密级与使用边界复核",
        "color": "red",
    },
    "research-context-analyst": {
        "display_name": "Research Context Analyst",
        "duty": "研报、公告与宏观背景证据复核",
        "color": "green",
    },
    "runtime-resilience-auditor": {
        "display_name": "Runtime Resilience Auditor",
        "duty": "预算、超时、检查点与失败关闭复核",
        "color": "blue",
    },
    "independent-validator": {
        "display_name": "Independent Validator",
        "duty": "独立验证与分歧检查",
        "color": "amber",
    },
    "result-composer": {
        "display_name": "Result Composer",
        "duty": "通俗结果与责任证据固化",
        "color": "cyan",
    },
}


_WORKER_ALIASES = {
    "research-evidence-investigator": "evidence-investigator",
    **{
        metadata["display_name"].strip().lower(): agent_id
        for agent_id, metadata in WORKER_CATALOG.items()
    },
}


def canonical_agent_id(value: Any) -> str:
    """Resolve persisted display names and Runtime slugs to one stable Agent ID."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower().replace("_", "-")
    if lowered in WORKER_CATALOG:
        return lowered
    if raw.strip().lower() in _WORKER_ALIASES:
        return _WORKER_ALIASES[raw.strip().lower()]
    slug = re.sub(r"[^0-9a-z]+", "-", lowered).strip("-")
    return _WORKER_ALIASES.get(slug, slug)


def project_worker_plan(run: dict[str, Any] | None) -> dict[str, Any]:
    """Project the authoritative per-Run Worker plan and observed completion.

    The Worker list is never inferred from a route name.  Legacy display names,
    Runtime slugs and artifact keys are normalized before they are compared.
    """
    if not run:
        return {
            "required_count": 0,
            "worker_ids": [],
            "workers": [],
            "completed_count": 0,
            "complete": False,
            "plan_consistent": True,
        }
    raw_plan = ((run.get("root_route_decision") or {}).get("worker_plan") or {})
    raw_workers = raw_plan.get("workers") or []
    worker_ids: list[str] = []
    for value in raw_workers:
        agent_id = canonical_agent_id(value)
        if agent_id and agent_id not in worker_ids:
            worker_ids.append(agent_id)
    declared_count = int(raw_plan.get("count") or 0)
    required_count = declared_count if declared_count > 0 else len(worker_ids)

    agent_result = run.get("agent_result") or {}
    artifacts_by_id = {
        canonical_agent_id(key): value
        for key, value in (agent_result.get("worker_artifacts") or {}).items()
        if canonical_agent_id(key) and isinstance(value, dict)
    }
    results_by_id = {
        canonical_agent_id(key): value
        for key, value in (agent_result.get("worker_results") or {}).items()
        if canonical_agent_id(key) and isinstance(value, dict)
    }
    # PASS/BLOCK/VERIFIED are domain findings, not transport completion.
    # Completion must be proven per selected Worker; the aggregate
    # ``workers_completed`` counter is retained only as historical telemetry.
    declared_completed = int(agent_result.get("workers_completed") or 0)
    completed_states = {
        "SEALED",
        "SUCCEEDED",
        "COMPLETED",
    }
    workers: list[dict[str, Any]] = []
    for agent_id in worker_ids:
        metadata = WORKER_CATALOG.get(
            agent_id,
            {
                "display_name": agent_id,
                "duty": "按本Run任务计划执行专业复核",
                "color": "blue",
            },
        )
        artifact = artifacts_by_id.get(agent_id) or {}
        result = results_by_id.get(agent_id) or {}
        status = str(
            result.get("status")
            or artifact.get("status")
            or ("PENDING" if run.get("agentteams_run_id") else "NOT_STARTED")
        )
        source = artifact or result
        task_id = str(source.get("task_id") or "")
        source_run_id = str(source.get("run_id") or "")
        source_role = canonical_agent_id(source.get("role"))
        run_bound = bool(task_id) and source_run_id == str(run.get("run_id") or "")
        role_bound = not source.get("role") or source_role == agent_id
        binding_status = (
            "VERIFIED"
            if run_bound and source_role == agent_id
            else "LEGACY_KEY_BOUND"
            if run_bound and not source.get("role")
            else "INVALID"
        )
        artifact_sha256 = canonical_sha256(artifact) if artifact else ""
        explicit_seal = str(artifact.get("seal_hash") or result.get("seal_hash") or "")
        explicit_seal_valid = bool(re.fullmatch(r"[0-9a-fA-F]{64}", explicit_seal))
        completion_evidence = (
            "ARTIFACT_SHA256"
            if artifact_sha256
            else "EXPLICIT_SEAL"
            if explicit_seal_valid
            else "EXPLICIT_STATE"
            if status.upper() in completed_states
            else "NONE"
        )
        completed = bool(
            source
            and run_bound
            and role_bound
            and (
                artifact_sha256
                or explicit_seal_valid
                or status.upper() in completed_states
            )
        )
        workers.append(
            {
                "agent_id": agent_id,
                "name": metadata["display_name"],
                "duty": metadata["duty"],
                "color": metadata["color"],
                "selected_for_run": True,
                # Keep transport/business wording separate. Historical
                # worker payloads sometimes used PASS/BLOCK in ``status``;
                # that value describes the domain finding, not whether the
                # isolated task executed successfully.
                "status": status,
                "execution_status": "SEALED" if completed else status,
                "completed": completed,
                "task_id": task_id or None,
                "artifact_run_id": source_run_id or None,
                "artifact_sha256": artifact_sha256 or None,
                "binding_status": binding_status,
                "completion_evidence": completion_evidence,
                "tool_run_id": result.get("tool_run_id")
                or artifact.get("tool_run_id"),
                "conclusion": artifact.get("recommendation")
                or artifact.get("independent_recommendation")
                or artifact.get("conclusion")
                or (status if completed else "尚未形成产物"),
                "evidence_note": (
                    f"{len(artifact.get('skill_invocations') or [])} 次运行时Skill调用"
                    if artifact
                    else "尚无本Run产物"
                ),
                # Never present the shared Worker input/payload digest as an
                # independent output seal.  Prefer the per-artifact digest.
                "seal_hash": artifact_sha256 or (explicit_seal if explicit_seal_valid else ""),
                "finished_at": artifact.get("finished_at")
                or result.get("finished_at"),
            }
        )
    task_owners: dict[str, list[dict[str, Any]]] = {}
    artifact_owners: dict[str, list[dict[str, Any]]] = {}
    for item in workers:
        if item.get("task_id"):
            task_owners.setdefault(str(item["task_id"]), []).append(item)
        if item.get("artifact_sha256"):
            artifact_owners.setdefault(str(item["artifact_sha256"]), []).append(item)
    for duplicated in (task_owners, artifact_owners):
        for items in duplicated.values():
            if len(items) > 1:
                for item in items:
                    item["completed"] = False
                    item["execution_status"] = "ARTIFACT_BINDING_INVALID"
                    item["binding_status"] = "DUPLICATE_PER_RUN_EVIDENCE"
    completed_count = sum(1 for item in workers if item["completed"])
    return {
        "declared_count": declared_count,
        "required_count": required_count,
        "worker_ids": worker_ids,
        "workers": workers,
        "completed_count": completed_count,
        "reported_completed_count": declared_completed,
        "complete": required_count > 0 and completed_count == required_count,
        "plan_consistent": declared_count in {0, len(worker_ids)},
    }


def project_run_states(run: dict[str, Any] | None) -> dict[str, Any]:
    """Separate technical execution, business disposition and terminal state."""
    if not run:
        return {
            "execution_state": {
                "code": "NOT_CREATED",
                "label": "尚未创建运行",
                "active": False,
            },
            "business_disposition": {
                "code": "NOT_EVALUATED",
                "label": "尚未形成业务建议",
                "source": "NONE",
                "final": False,
            },
            "lifecycle_terminal": {
                "code": "OPEN",
                "label": "尚未开始",
                "phase": "NOT_CREATED",
                "is_terminal": False,
            },
            "consistency": {"status": "CONSISTENT", "issues": []},
        }

    plan = project_worker_plan(run)
    lifecycle = run.get("lifecycle") or {}
    phase = str(lifecycle.get("current_phase") or run.get("state") or "UNKNOWN")
    gate = run.get("human_gate") or {}
    gate_state = str(gate.get("state") or "NOT_OPENED")
    datapass = datapass_presentation_projection(run.get("datapass") or {})
    recommendation = str(datapass.get("machine_recommendation") or "")
    precheck = run.get("precheck") or {}
    precheck_recommendation = str(precheck.get("machine_recommendation") or "")
    emergency_state = str(run.get("state") or "")
    emergency_stopped = emergency_state in {
        "STOPPED_BY_GATE",
        "BUDGET_EXCEEDED",
    } and bool(run.get("emergency_stop"))

    terminal_by_gate = {
        "APPROVED": ("APPROVED", "负责人已批准"),
        "REJECTED": ("BLOCKED", "负责人已拒绝准入"),
        "RETURNED": ("RETURNED", "负责人已退回补证"),
    }
    if emergency_stopped:
        terminal_code = emergency_state
        terminal_label = (
            "Token硬上限已触发，当前运行失败关闭"
            if emergency_state == "BUDGET_EXCEEDED"
            else "控制面已紧急停止当前运行"
        )
        execution_code, execution_label, active = (
            emergency_state,
            terminal_label,
            False,
        )
    elif phase == "FAILED_CLOSED":
        terminal_code, terminal_label = "FAILED_CLOSED", "运行在准入前失败关闭"
        execution_code = str(run.get("state") or "FAILED_CLOSED")
        execution_label, active = "未形成DataPass，未进入下游", False
    elif gate_state in terminal_by_gate:
        terminal_code, terminal_label = terminal_by_gate[gate_state]
        execution_code, execution_label, active = "COMPLETED", "运行已结束", False
    elif gate_state == "AWAITING_HUMAN":
        terminal_code, terminal_label = "OPEN", "等待负责人处理"
        execution_code, execution_label, active = (
            "AWAITING_HUMAN",
            "专业核验完成，等待负责人处理",
            True,
        )
    elif datapass:
        terminal_code, terminal_label = "OPEN", "DataPass草案尚未处置"
        execution_code, execution_label, active = (
            "DATAPASS_READY",
            "DataPass草案已形成",
            True,
        )
    elif plan["complete"]:
        terminal_code, terminal_label = "OPEN", "等待汇聚专业产物"
        execution_code, execution_label, active = (
            "WORKERS_COMPLETED",
            "所需Worker均已完成",
            True,
        )
    elif plan["completed_count"]:
        terminal_code, terminal_label = "OPEN", "专业核验进行中"
        execution_code, execution_label, active = (
            "WORKERS_RUNNING",
            "专业Worker正在核验",
            True,
        )
    elif run.get("agentteams_run_id"):
        terminal_code, terminal_label = "OPEN", "已提交协作运行"
        execution_code, execution_label, active = (
            "DISPATCHED",
            "已提交AgentTeams",
            True,
        )
    elif run.get("dispatch_guard") or str(run.get("state")) == "DISPATCH_GUARDED":
        terminal_code, terminal_label = "OPEN", "模型派发尚未发生"
        execution_code, execution_label, active = (
            "DISPATCH_GUARDED",
            "运行门禁暂未放行",
            False,
        )
    else:
        terminal_code, terminal_label = "OPEN", "等待下一执行步骤"
        execution_code = str(run.get("state") or "READY")
        execution_label, active = "等待调度或确定性处理", False

    if emergency_stopped:
        disposition = (
            "NOT_ADMITTED_CONTROL_STOP",
            "本次运行未完成准入，不得进入下游",
            "CONTROL_PLANE",
            True,
        )
    elif phase == "FAILED_CLOSED":
        disposition = (
            "NOT_ADMITTED_FAILED_CLOSED",
            "运行失败关闭，数据未获准进入声明用途",
            "CONTROL_PLANE",
            True,
        )
    elif gate_state == "APPROVED":
        disposition = ("APPROVED_FOR_DECLARED_USE", "已批准用于声明用途", "HUMAN", True)
    elif gate_state == "REJECTED":
        disposition = ("NOT_ADMITTED", "未获准进入声明用途", "HUMAN", True)
    elif gate_state == "RETURNED":
        disposition = ("MORE_EVIDENCE_REQUIRED", "需补充材料后重新核验", "HUMAN", True)
    elif recommendation == "PASS":
        disposition = ("RECOMMEND_ADMIT", "建议提交负责人审批", "AGENTTEAMS", False)
    elif recommendation == "BLOCK":
        disposition = ("RECOMMEND_BLOCK", "建议保持隔离并处置", "AGENTTEAMS", False)
    elif recommendation == "NEEDS_EVIDENCE":
        disposition = ("RECOMMEND_MORE_EVIDENCE", "建议补充证据", "AGENTTEAMS", False)
    elif precheck_recommendation == "PASS":
        disposition = ("PRECHECK_CLEAR", "确定性预检未发现冲突", "PRECHECK", False)
    elif precheck_recommendation == "BLOCK":
        disposition = ("PRECHECK_REVIEW_REQUIRED", "确定性预检发现待复核事项", "PRECHECK", False)
    else:
        disposition = ("NOT_EVALUATED", "尚未形成业务建议", "NONE", False)

    issues: list[str] = []
    if not plan["plan_consistent"]:
        issues.append("WORKER_PLAN_COUNT_MISMATCH")
    if datapass and not plan["complete"]:
        issues.append("DATAPASS_WITH_INCOMPLETE_WORKER_PLAN")
    if gate_state == "AWAITING_HUMAN" and not datapass:
        issues.append("HUMAN_GATE_OPEN_WITHOUT_DATAPASS")
    if gate_state in terminal_by_gate and phase not in {
        "APPROVED",
        "BLOCKED",
        "RETURNED",
    }:
        issues.append("TERMINAL_GATE_LIFECYCLE_MISMATCH")
    if emergency_stopped and phase != "FAILED_CLOSED":
        issues.append("EMERGENCY_STOP_LIFECYCLE_MISMATCH")
    return {
        "execution_state": {
            "code": execution_code,
            "label": execution_label,
            "active": active,
            "worker_progress": f"{plan['completed_count']}/{plan['required_count']}",
        },
        "business_disposition": {
            "code": disposition[0],
            "label": disposition[1],
            "source": disposition[2],
            "final": disposition[3],
        },
        "lifecycle_terminal": {
            "code": terminal_code,
            "label": terminal_label,
            "phase": phase,
            "is_terminal": (
                emergency_stopped
                or phase == "FAILED_CLOSED"
                or gate_state in terminal_by_gate
            ),
        },
        "consistency": {
            "status": "CONSISTENT" if not issues else "INCONSISTENT",
            "issues": issues,
        },
    }


def build_run_presentation(
    run: dict[str, Any] | None,
    submission: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the business-facing Run view from persisted backend facts only."""
    states = project_run_states(run)
    if not run or not submission:
        return {
            "empty": True,
            "states": states,
            "result": {
                "title": "尚未提交核验任务",
                "summary": "提交金融资料并说明业务用途后，系统才会创建受控运行。",
                "tone": "neutral",
            },
        }

    metadata = submission.get("metadata") or {}
    parsed = submission.get("parsed") or {}
    file_record = submission.get("file") or {}
    precheck = run.get("precheck") or {}
    datapass = datapass_presentation_projection(run.get("datapass") or {})
    plan = project_worker_plan(run)
    worker_artifacts = (
        (run.get("agent_result") or {}).get("worker_artifacts") or {}
    )
    semantic_artifact = worker_artifacts.get("semantic-impact-analyst") or {}
    independent_artifact = worker_artifacts.get("independent-validator") or {}
    semantic_skill_result = semantic_artifact.get("contract") or {}
    deterministic_impact = semantic_artifact.get("impact") or {}
    semantic_proposals = []
    for role, artifact in (
        ("Semantic Impact Analyst", semantic_artifact),
        ("Independent Validator", independent_artifact),
    ):
        proposal = artifact.get("agent_semantic_proposal") or {}
        if not proposal.get("proposed_field"):
            continue
        semantic_proposals.append(
            {
                "role": role,
                "proposed_field": proposal.get("proposed_field"),
                "proposed_semantic": proposal.get("proposed_semantic"),
                "confidence_bps": proposal.get("confidence_bps"),
                "reason_code": proposal.get("reason_code"),
                "uncertainty_code": proposal.get("uncertainty_code"),
                "proposal_source": proposal.get("proposal_source"),
            }
        )
    proposed_fields = {
        str(item.get("proposed_field") or "") for item in semantic_proposals
    } - {""}
    semantic_agreement = bool(semantic_proposals) and len(proposed_fields) == 1
    agent_selected_field = (
        next(iter(proposed_fields)) if semantic_agreement else None
    )
    gate = run.get("human_gate") or {}
    profile = str(submission.get("profile") or metadata.get("profile") or "unknown")
    declared_purpose = str(metadata.get("declared_purpose") or "未声明")

    gate_labels = {
        "AWAITING_HUMAN": "等待负责人处理",
        "APPROVED": "负责人已批准",
        "REJECTED": "负责人已拒绝准入",
        "RETURNED": "已退回补充材料",
        "NOT_OPENED": "尚未进入负责人处理",
    }
    gate_view = {
        **gate,
        "type": "MATRIX_HUMAN_GATE",
        "label": gate_labels.get(str(gate.get("state") or "NOT_OPENED"), str(gate.get("state") or "尚未开启")),
        "current_stage": states["execution_state"]["label"],
        "gate_opened_at": gate.get("opened_at"),
    }

    evidence = {
        "evidence_bundle_id": submission.get("evidence_bundle_id"),
        "declared_source": metadata.get("declared_source") or "未声明来源",
        "provider": metadata.get("provider") or metadata.get("declared_source") or "未声明提供方",
        "received_at": submission.get("created_at"),
        "first_stored_at": submission.get("created_at"),
        "raw_response_sha256": file_record.get("sha256"),
        "integrity_status": "HASH_VERIFIED" if len(str(file_record.get("sha256") or "")) == 64 else "NOT_VERIFIED",
        "rights_status": "DECLARATION_RECORDED" if metadata.get("rights_basis") else "NOT_DECLARED",
        "rights_basis": metadata.get("rights_basis") or "未登记使用依据",
        "files": [
            {
                "name": file_record.get("name") or "未命名资料",
                "kind": Path(str(file_record.get("name") or "evidence")).suffix.lstrip(".").lower() or "data",
                "role": "本Run原始资料",
                "size_kb": float(file_record.get("size_bytes") or 0) / 1024,
                "sha256": file_record.get("sha256"),
                "mapping_note": "原始版本已按内容哈希登记",
            }
        ],
    }

    profile_view = build_profile_projection(profile, submission, precheck)
    contract_payload = datapass.get("semantic_contract") or run.get("semantic_contract") or {}
    if not isinstance(contract_payload, dict):
        contract_payload = {"name": str(contract_payload)}
    contract = build_contract_projection(
        profile,
        declared_purpose,
        {
            **contract_payload,
            "required_field": precheck.get("required_field"),
            "required_concept": precheck.get("required_concept"),
        },
        contract_payload.get("sha256")
        or precheck.get("sha256")
        or submission.get("evidence_root_hash"),
        "RESOLVED" if precheck else "NOT_RUN",
    )

    # Once model-authored proposals exist, the presentation must show what the
    # Agents actually proposed instead of making the domain Profile look like
    # a hard-coded answer table.  The Profile remains the verification
    # boundary; it does not choose the field on behalf of the Agents.
    if semantic_proposals:
        proposal_fields: list[dict[str, Any]] = []
        seen_fields: set[str] = set()
        for proposal in semantic_proposals:
            field = str(proposal.get("proposed_field") or "")
            if not field or field in seen_fields:
                continue
            seen_fields.add(field)
            meanings = sorted(
                {
                    str(item.get("proposed_semantic") or "待解释")
                    for item in semantic_proposals
                    if str(item.get("proposed_field") or "") == field
                }
            )
            proposal_fields.append(
                {
                    "field": field,
                    "semantics": " / ".join(meanings),
                    "mapping_note": "由模型Agent基于本Run业务目标提出；已交给确定性Skill验真",
                }
            )
        for field in precheck.get("available_fields") or []:
            field = str(field)
            if not field or field in seen_fields or field in {
                "candidate_mapping",
                "business_purpose",
            }:
                continue
            proposal_fields.append(
                {
                    "field": field,
                    "semantics": "本Run未选择",
                    "mapping_note": "保留为原始证据字段，不因未选中而改写或删除",
                }
            )
        contract["key_fields"] = proposal_fields
        contract["validation_state"] = str(
            semantic_skill_result.get("status") or contract.get("validation_state")
        )
        contract["selected_field"] = agent_selected_field
        contract["resolution_source"] = "AGENT_MODEL_PROPOSAL_VERIFIED_BY_RUNTIME_SKILL"
        contract["agent_proposals"] = semantic_proposals
        contract["agent_agreement"] = semantic_agreement

    current_recommendation = str(
        datapass.get("machine_recommendation")
        or (run.get("agent_result") or {}).get("leader_recommendation")
        or precheck.get("machine_recommendation")
        or "NOT_AVAILABLE"
    ).upper()
    preserve_declared_conflict = (
        current_recommendation == "BLOCK"
        and str(precheck.get("machine_recommendation") or "").upper() == "BLOCK"
        and bool(precheck.get("candidate_mapping"))
        and bool(precheck.get("required_field"))
        and precheck.get("candidate_mapping") != precheck.get("required_field")
    )
    required_field = str(
        semantic_skill_result.get("required_field")
        or deterministic_impact.get("required_field")
        or precheck.get("required_field")
        or ""
    )
    candidate_field = str(
        precheck.get("candidate_mapping") if preserve_declared_conflict else
        deterministic_impact.get("selected_price_field")
        or semantic_skill_result.get("candidate_mapping")
        or agent_selected_field
        or precheck.get("candidate_mapping")
        or ""
    )
    field_conflict = bool(required_field and candidate_field and required_field != candidate_field)
    if deterministic_impact or precheck:
        metric = profile_impact_display(
            profile,
            declared_purpose,
            precheck if preserve_declared_conflict else deterministic_impact,
            deterministic_impact if preserve_declared_conflict else precheck,
        )
        # The runtime Skill evaluates the Agent's proposed remediation.  When
        # the submitted mapping itself is the reason for BLOCK, its zero
        # residual must not replace the impact of the immutable original
        # submission.  Show both facts separately: the original mapping is
        # wrong (and has an impact), while the proposed repair validates to
        # zero residual.
        if preserve_declared_conflict:
            metric = {
                **metric,
                "value": precheck.get("impact_cny_per_contract"),
                "label": "原始字段映射造成的每手金额影响",
                "unit": "元/手",
                "field_unit": metric.get("field_unit") or "点",
            }
        value = metric.get("value")
        value_unit = str(metric.get("unit") or "")
        value_label = str(metric.get("label") or "确定性影响")
        field_unit = str(metric.get("field_unit") or "")
        quantified_impact = value is not None
        impact = {
            "available": quantified_impact,
            "quantified": quantified_impact,
            "title": "原始字段映射未通过契约" if preserve_declared_conflict else "Agent语义候选未通过契约" if field_conflict else "Agent语义候选已通过契约验真",
            "explanation": (
                "接入配置仍选择候选字段；Agent提出的修订建议不能静默改写原提交，"
                "因此按原配置复算影响并保持阻断。"
                if preserve_declared_conflict
                else "模型Agent提出的候选与确定性契约解析结果不同，当前数据不得准入。"
                if field_conflict
                else "两个专业Agent独立提出候选；确定性Skill验证其与声明用途一致。"
            ),
            "conflict": field_conflict,
            "candidate": {
                "field": candidate_field,
                "label": (
                    "公司行动条款证据（不能替代行情序列）"
                    if profile == EQUITY_PROFILE_ID
                    and candidate_field == "declared_adjustment"
                    else candidate_field
                ),
                "value": (
                    precheck.get(candidate_field)
                    if preserve_declared_conflict
                    else deterministic_impact.get("selected_value", precheck.get(candidate_field))
                ),
                "unit": field_unit,
                "source_file": file_record.get("name"),
            },
            "required": {
                "field": required_field,
                "label": (
                    "公司行动调整后的可比行情序列"
                    if profile == EQUITY_PROFILE_ID and required_field == "qfq"
                    else required_field
                ),
                "value": (
                    precheck.get(required_field)
                    if preserve_declared_conflict
                    else deterministic_impact.get("required_value", precheck.get(required_field))
                ),
                "unit": field_unit,
                "source_file": file_record.get("name"),
            },
            "formula": precheck.get("calculation_formula") or (
                "abs(required_value - selected_value) × calculation_scale"
                if deterministic_impact else "NOT_CAPTURED"
            ),
            "value": value,
            "value_unit": value_unit,
            "value_label": value_label,
            "conclusion": current_recommendation,
            "remediation_validation": (
                {
                    "proposed_field": deterministic_impact.get("selected_price_field"),
                    "residual_value": deterministic_impact.get(
                        "financial_misstatement_cny_per_contract"
                    ),
                    "residual_unit": "元/手",
                    "meaning": "这是修订建议的验证结果，不是原始提交的影响值。",
                }
                if preserve_declared_conflict
                else None
            ),
            "basis": [
                {"item": "语义候选来源", "value": "专业Agent独立推理", "source_file": "AgentTeams worker artifact"},
                {"item": "契约与金额验真", "value": ("接入预检保留原配置；运行时Skill验证Agent修订建议" if preserve_declared_conflict else "运行时确定性Skill"), "source_file": "Skill receipts + Run precheck" if preserve_declared_conflict else "Skill receipts"},
                {"item": "金融数值来源", "value": "已登记原始资料字段", "source_file": file_record.get("name")},
                {"item": "模型参与", "value": "参与语义理解，不生成金融数值", "source_file": "Provider token ledger"},
            ],
        }
    else:
        impact = {
            "available": False,
            "title": "尚未形成确定性影响计算",
            "explanation": "当前Profile尚未返回可复核的影响结果。",
            "conflict": False,
            "candidate": {},
            "required": {},
            "formula": "NOT_RUN",
            "value": None,
            "value_unit": "",
            "value_label": "尚未计算",
            "conclusion": "NOT_RUN",
            "basis": [],
        }

    recommendation = str(datapass.get("machine_recommendation") or "NOT_AVAILABLE")
    action_by_recommendation = {
        "PASS": "提交负责人审批",
        "BLOCK": "保持隔离并选择处置方式",
        "NEEDS_EVIDENCE": "补充证据后重新核验",
    }
    datapass_view = {
        "available": bool(datapass),
        "protocol": datapass.get("protocol"),
        "machine_recommendation": recommendation,
        "precheck_recommendation": precheck.get("machine_recommendation") or "NOT_RUN",
        "recommended_action": action_by_recommendation.get(recommendation, "等待专业核验完成"),
        "recommended_field": candidate_field or required_field or None,
        "impact": datapass.get("formal_impact")
        or {
            "value": impact.get("value"),
            "unit": impact.get("value_unit"),
            "label": impact.get("value_label"),
        },
        "evidence_consensus": (
            "服务端哈希已核验；专业结论以本Run产物为准"
            if evidence["integrity_status"] == "HASH_VERIFIED"
            else "证据完整性尚未核验"
        ),
        "worker_consensus": {
            "agreed": int(datapass.get("worker_artifact_count") or plan["completed_count"]),
            "total": plan["required_count"],
            "complete": plan["complete"],
            "skill_versions_aligned": datapass.get("skill_attestation_status") == "VERIFIED",
            "isolation_verified": bool(datapass.get("context_isolation_verified", False)),
            "state": "SEALED" if datapass and plan["complete"] else "PENDING" if run.get("agentteams_run_id") else "NOT_RUN",
        },
        "admission_advice": states["business_disposition"]["label"],
        "draft_hash": datapass.get("draft_sha256"),
        "remediation_summary": datapass.get("remediation_summary")
        or "保留当前证据版本，创建带血缘的修订Run并重新核验",
    }
    remediation_plan = None
    if recommendation == "BLOCK" and required_field:
        target_semantics = next(
            (
                item.get("proposed_semantic")
                for item in semantic_proposals
                if str(item.get("proposed_field") or "") == required_field
            ),
            None,
        )
        remediation_plan = {
            "protocol": "FINFLUX_HUMAN_REMEDIATION_PROPOSAL_V1",
            "parent_run_id": run.get("run_id"),
            "from_field": candidate_field or None,
            "target_field": required_field,
            "target_semantic": target_semantics,
            "proposal_source": (
                "AGENT_MODEL_PROPOSAL_VERIFIED_BY_RUNTIME_SKILL"
                if semantic_proposals
                else "DETERMINISTIC_CONTRACT_PRECHECK"
            ),
            "reason_code": semantic_skill_result.get("reason_code")
            or "SEMANTIC_MAPPING_CONFLICT",
            "raw_evidence_mutated": False,
            "requires_human_approval": True,
            "requires_fresh_agentteams_review": True,
        }
        remediation_plan["proposal_sha256"] = canonical_sha256(remediation_plan)

    result_title = (
        "负责人已批准：可按授权范围使用"
        if gate.get("state") == "APPROVED"
        else "负责人已拒绝：当前资料保持隔离"
        if gate.get("state") == "REJECTED"
        else "负责人已退回：补充材料后重新核验"
        if gate.get("state") == "RETURNED"
        else "专业核验建议：可提交负责人审批"
        if recommendation == "PASS"
        else "专业核验建议：先阻断并处置"
        if recommendation == "BLOCK"
        else "专业核验建议：补充证据"
        if recommendation == "NEEDS_EVIDENCE"
        else "预检已完成，尚未形成多Agent DataPass"
    )
    result = {
        "title": result_title,
        "summary": (
            "结论绑定同一Run的证据哈希、Worker产物、Skill版本和负责人决定。"
            if datapass
            else "当前仅有确定性预检；未产生的Worker结论不会被补写。"
        ),
        "tone": "positive" if gate.get("state") == "APPROVED" or recommendation == "PASS" else "caution",
        "truth_statement": "金融数值来自已登记证据与确定性计算；Agent提供核验建议，负责人保留最终授权权。",
    }
    artifact_rows = []
    for worker_id, artifact in worker_artifacts.items():
        if not isinstance(artifact, dict):
            continue
        artifact_rows.append(
            {
                "worker_id": worker_id,
                "task_id": artifact.get("task_id"),
                "artifact_type": artifact.get("artifact_type") or artifact.get("protocol") or "WORKER_ARTIFACT",
                "recommendation": artifact.get("recommendation") or artifact.get("status") or "NOT_REPORTED",
                "context_slice_sha256": artifact.get("context_slice_sha256") or artifact.get("context_sha256"),
                "artifact_sha256": artifact.get("artifact_sha256") or artifact.get("sha256"),
                "skill_invocations": artifact.get("skill_invocations") or [],
                "sealed": bool(artifact.get("sealed") or artifact.get("artifact_sha256") or artifact.get("sha256")),
            }
        )
    provider_usage = run.get("provider_usage") or {}
    presentation = {
        "protocol": "FINFLUX_PRESENTATION_V1",
        "empty": False,
        "ids": {
            "run_id": run.get("run_id"),
            "trace_id": run.get("trace_id"),
            "case_id": run.get("case_id"),
            "evidence_bundle_id": submission.get("evidence_bundle_id"),
        },
        "updated_at": run.get("updated_at") or run.get("created_at"),
        "profile": profile,
        "profile_definition": profile_view,
        "presentation_schema": profile_view.get("presentation_schema") or [],
        "states": states,
        "result": result,
        "evidence_bundle": evidence,
        "semantic_contract": contract,
        "semantic_discovery": {
            "mode": precheck.get("semantic_resolution_mode")
            or "AGENT_PROPOSES_SKILL_VERIFIES_HUMAN_DECIDES",
            "proposals": semantic_proposals,
            "agreement": semantic_agreement,
            "selected_field": agent_selected_field,
            "skill_status": semantic_skill_result.get("status") or "NOT_RUN",
            "model_calls": (run.get("provider_usage") or {}).get("call_count"),
            "model_tokens": (run.get("provider_usage") or {}).get("total_tokens"),
        },
        "impact": impact,
        "workers": plan["workers"],
        "worker_plan": plan,
        "agent_team": {
            "manager": "global-manager",
            "case_lead": "finflux-case-lead",
            "selected_workers": plan["worker_ids"],
            "required_count": plan["required_count"],
            "completed_count": plan["completed_count"],
            "parallel": bool(((run.get("root_route_decision") or {}).get("worker_plan") or {}).get("parallel")),
        },
        "artifacts": artifact_rows,
        "datapass": datapass_view,
        "remediation_plan": remediation_plan,
        "gate": gate_view,
        "human_gate": gate_view,
        "evolution": {
            "parent_run_id": ((run.get("lineage") or {}).get("parent_run_id")),
            "root_run_id": ((run.get("lineage") or {}).get("root_run_id")) or run.get("run_id"),
            "revision_reason": ((run.get("lineage") or {}).get("revision_reason")),
            "remediation_plan": remediation_plan,
            "raw_evidence_mutated": False,
        },
        "observability": {
            "provider_usage": provider_usage,
            "event_count": len(run.get("events") or []),
            "latest_event_seq": len(run.get("events") or []),
            "token_source": provider_usage.get("source") or provider_usage.get("status") or "NOT_REPORTED",
        },
    }
    presentation["presentation_sha256"] = canonical_sha256(presentation)
    return presentation


def build_runtime_snapshot(
    run: dict[str, Any] | None,
    submission: dict[str, Any] | None,
    runtime: dict[str, Any],
    token_guard: dict[str, Any],
    sync_error: str | None = None,
) -> dict[str, Any]:
    """Capture one hash-bound backend snapshot for every frontend view."""
    plan = project_worker_plan(run)
    selected = set(plan["worker_ids"])
    topology = []
    for raw_item in runtime.get("topology", []) or []:
        item = dict(raw_item)
        agent_id = canonical_agent_id(item.get("name"))
        metadata = WORKER_CATALOG.get(agent_id, {})
        item["agent_id"] = agent_id or str(item.get("name") or "")
        item["display_name"] = metadata.get("display_name") or item.get("label") or item.get("name")
        item["selected_for_run"] = item["agent_id"] in selected
        topology.append(item)
    captured_at = utc_now()
    snapshot = {
        "protocol": "FINFLUX_RUNTIME_SNAPSHOT_V1.0",
        "captured_at_utc": captured_at,
        "selected_run_id": run.get("run_id") if run else None,
        "run_revision_sha256": canonical_sha256(run) if run else None,
        "runtime": {
            **runtime,
            "topology": topology,
        },
        "worker_plan": plan,
        "states": project_run_states(run),
        "presentation": build_run_presentation(run, submission),
        "token_guard": token_guard,
        "sync_error": sync_error,
    }
    snapshot["snapshot_sha256"] = canonical_sha256(snapshot)
    return snapshot


def project_decision_stages(run: dict[str, Any] | None) -> dict[str, Any]:
    """Project the three decision layers without promoting a precheck to DataPass.

    The projection is intentionally derived from persisted facts only.  It never
    invents Worker consensus and it never turns an Agent recommendation into a
    Human authorization.
    """
    if not run:
        return {
            "precheck": {"state": "NOT_RUN", "recommendation": None},
            "agent": {"state": "NOT_RUN", "recommendation": None, "workers": "0/0"},
            "human": {"state": "NOT_OPENED", "decision": None},
            "final_authority": "HUMAN",
        }
    precheck = run.get("precheck") or {}
    precheck_recommendation = str(precheck.get("machine_recommendation") or "PENDING")
    datapass = run.get("datapass") or {}
    plan = project_worker_plan(run)
    required = int(plan["required_count"])
    completed = int(plan["completed_count"])
    if datapass and required > 0 and completed >= required:
        agent_recommendation = str(datapass.get("machine_recommendation") or "PENDING")
        agent_state = f"AGENT_RECOMMEND_{agent_recommendation}"
    elif run.get("agentteams_run_id"):
        agent_recommendation = None
        agent_state = "RUNNING"
    else:
        agent_recommendation = None
        agent_state = "NOT_RUN"
    gate = run.get("human_gate") or {}
    gate_state = str(gate.get("state") or "NOT_OPENED")
    human_state = {
        "APPROVED": "HUMAN_APPROVED",
        "REJECTED": "HUMAN_REJECTED",
        "RETURNED": "HUMAN_RETURNED",
        "AWAITING_HUMAN": "AWAITING_HUMAN",
    }.get(gate_state, "NOT_REQUIRED" if agent_state == "NOT_RUN" else "NOT_OPENED")
    return {
        "precheck": {
            "state": f"PRECHECK_{precheck_recommendation}",
            "recommendation": precheck_recommendation,
            "generated_by_model": False,
        },
        "agent": {
            "state": agent_state,
            "recommendation": agent_recommendation,
            "workers": f"{completed}/{required}",
            "datapass_present": bool(datapass),
        },
        "human": {
            "state": human_state,
            "decision": gate.get("decision"),
            "actor": gate.get("human_actor_id"),
        },
        "final_authority": "HUMAN",
    }


def _safe_name(name: str) -> str:
    candidate = Path(name).name.strip().replace("\x00", "")
    candidate = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", candidate)
    if not candidate or candidate in {".", ".."}:
        raise ValueError("上传文件名无效")
    if Path(candidate).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("仅接受 CSV、JSON、TXT、MD、XML、HTML、XLSX、PDF 或 ZIP 证据包")
    return candidate


def _json_read(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _stable_display_suffix(value: str, length: int = 6) -> str:
    """Return a stable display suffix without changing the canonical ID."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()


def submission_display_id(submission_id: str, created_at: str | None = None) -> str:
    """Human-readable deterministic alias; the original ID remains authoritative."""
    match = re.search(r"(20\d{6})", submission_id)
    date_part = match.group(1) if match else ""
    if not date_part and created_at:
        date_part = re.sub(r"\D", "", str(created_at))[:8]
    return f"F-{date_part or 'UNDATED'}-{_stable_display_suffix(submission_id, 4)}"


def run_display_id(run_id: str, asset_class: str | None = None) -> str:
    """Stable short UI alias that is never accepted as an audit key."""
    asset_code = {
        "futures": "FUT",
        "future": "FUT",
        "equity": "EQT",
        "stock": "EQT",
        "option": "OPT",
        "research": "RSH",
        "research_rights": "RSH",
    }.get(str(asset_class or "").strip().lower(), "GEN")
    return f"RUN-{asset_code}-{_stable_display_suffix(run_id)}"


class LiveIntakeRepository:
    """Small local POC repository with immutable evidence and append-only Run facts."""

    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root / "live_intake"
        self.submissions = self.root / "submissions"
        self.objects = self.root / "objects"
        self.inspections = self.root / "inspections"
        self.runs = self.root / "runs"
        self.run_creation_attempts = self.root / "run_creation_attempts"
        self.change_bundles = self.root / "change_bundles"
        self.memory = StructuredMemoryStore(self.root / "memory")
        self.context_memory = build_context_memory_adapter(
            local_root=self.root / "context_memory"
        )
        self.index_path = self.root / "index.json"
        self.lock = threading.RLock()
        # Cache JSON projections only. Canonical IDs and persisted files remain
        # the source of truth. The digest receipts the bytes parsed on a miss.
        self._json_cache: dict[str, tuple[int, int, str, Any]] = {}
        self._json_cache_stats = {"hits": 0, "misses": 0, "invalidations": 0}

    def _invalidate_json_cache(self, path: Path) -> None:
        key = str(path.resolve())
        with self.lock:
            if self._json_cache.pop(key, None) is not None:
                self._json_cache_stats["invalidations"] += 1

    def _cached_json_read(self, path: Path, default: Any = None) -> Any:
        """Parse once per mtime/size version and return a mutation-safe copy."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return copy.deepcopy(default)
        key = str(path.resolve())
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        with self.lock:
            cached = self._json_cache.get(key)
            if cached and cached[:2] == signature:
                self._json_cache_stats["hits"] += 1
                return copy.deepcopy(cached[3])
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            if key in self._json_cache:
                self._json_cache_stats["invalidations"] += 1
            self._json_cache[key] = (signature[0], signature[1], digest, value)
            self._json_cache_stats["misses"] += 1
        return copy.deepcopy(value)

    def json_cache_status(self) -> dict[str, int]:
        """Return counters only; never expose cached contents."""
        with self.lock:
            return {**self._json_cache_stats, "entries": len(self._json_cache)}

    @staticmethod
    def _operational_memory_query(
        submission: dict[str, Any],
        route_decision: dict[str, Any],
        execution_policy_id: str,
    ) -> str:
        """Build one redacted, non-financial lookup query per Run."""
        worker_plan = route_decision.get("worker_plan") or {}
        metadata = submission.get("metadata") or {}
        query = {
            "profile": submission.get("profile"),
            "declared_purpose": metadata.get("declared_purpose"),
            "route": route_decision.get("route"),
            "reason_codes": sorted(route_decision.get("reason_codes") or []),
            "execution_policy_id": execution_policy_id,
            "workers": sorted(worker_plan.get("workers") or []),
            "required_skill_versions": dict(
                route_decision.get("required_skill_versions") or {}
            ),
        }
        return json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _resolve_operational_memory_plan(
        self,
        *,
        run_id: str,
        submission: dict[str, Any],
        route_decision: dict[str, Any],
        execution_policy_id: str,
    ) -> dict[str, Any]:
        """Recall once before dispatch; never inject memory into an Agent prompt."""
        query = self._operational_memory_query(
            submission, route_decision, execution_policy_id
        )
        try:
            configured_token_budget = int(
                context_memory_setting("FINFLUX_CONTEXT_MEMORY_MAX_TOKENS", "192")
            )
        except ValueError:
            configured_token_budget = 192
        # This is a candidate-retrieval budget, not a model-token allowance.
        # Clamp it so a deployment typo can never re-introduce full-history reads.
        recall_token_budget = min(512, max(8, configured_token_budget))
        try:
            recalled = self.context_memory.lookup(
                run_id=run_id,
                role="finflux-execution-control",
                query=query,
                budget=MemoryBudget(
                    max_characters=min(2048, max(32, recall_token_budget * 4)),
                    max_token_estimate=recall_token_budget,
                    max_items=3,
                ),
            )
            _json_atomic(
                self.root
                / "context_memory"
                / "lookup_receipts"
                / f"{run_id}.json",
                recalled,
            )
            matched_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
            expected_profile = str(submission.get("profile") or "")
            expected_purpose = str(
                (submission.get("metadata") or {}).get("declared_purpose") or ""
            )
            expected_route = str(route_decision.get("route") or "")
            expected_reasons = sorted(route_decision.get("reason_codes") or [])
            expected_workers = sorted(
                ((route_decision.get("worker_plan") or {}).get("workers") or [])
            )
            expected_skills = dict(
                route_decision.get("required_skill_versions") or {}
            )
            for item in recalled.get("items") or []:
                signed_item = self.context_memory.resolve_signed_reference(
                    role="finflux-execution-control",
                    content_sha256_value=str(
                        item.get("content_sha256") or ""
                    ),
                    reference_uri=str(item.get("uri") or ""),
                )
                if signed_item is None:
                    continue
                try:
                    experience = json.loads(
                        str(signed_item.get("summary") or "")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    not isinstance(experience, dict)
                    or experience.get("protocol")
                    != "FINFLUX_SIGNED_OPERATIONAL_EXPERIENCE_V1.0"
                    or experience.get("profile") != expected_profile
                    or experience.get("declared_purpose") != expected_purpose
                    or experience.get("route") != expected_route
                    or sorted(experience.get("worker_roles") or [])
                    != expected_workers
                    or dict(experience.get("skill_versions") or {})
                    != expected_skills
                ):
                    continue
                recorded_reasons = experience.get("reason_codes")
                if recorded_reasons is not None and sorted(recorded_reasons) != expected_reasons:
                    continue
                recorded_policy = str(
                    experience.get("execution_policy_id") or ""
                )
                if recorded_policy and recorded_policy != execution_policy_id:
                    continue
                matched_items.append((signed_item, experience))
            handles = [
                {
                    "uri": item.get("uri"),
                    "content_sha256": item.get("content_sha256"),
                }
                for item, _ in matched_items
            ]
            metrics = dict(recalled.get("metrics") or {})
            recall_status = "HIT" if handles else "MISS_SAFE_DEFAULT"
            lookup_receipt_sha256 = recalled.get("receipt_sha256")
        except Exception as exc:  # noqa: BLE001 - memory is advisory and fail-open to safe default
            handles = []
            metrics = {
                "source": "SAFE_DEFAULT_AFTER_MEMORY_ERROR",
                "remote_status": type(exc).__name__,
                "cache_hit": False,
                "lookup_latency_ms": None,
                "candidate_characters": 0,
                "candidate_token_estimate": 0,
                "injected_characters": 0,
                "injected_token_estimate": 0,
            }
            recall_status = "DEGRADED_SAFE_DEFAULT"
            lookup_receipt_sha256 = None
            matched_items = []

        selected_recipe = {
            "recipe_id": "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
            "history_limit": 0,
            "manager_max_iters": 1,
            "worker_max_iters": 2,
            "tool_timeout_seconds": 90,
            "llm_retry_enabled": False,
            "context_transport": "CONTENT_ADDRESSED_ROLE_SLICE",
            "context_publish_policy": "HASH_REFERENCE_ONLY",
            "leader_finalization": "SEALED_WORKER_ARTIFACTS_ONLY",
            "remote_memory_reads_per_run": 1,
            "candidate_recall_token_budget": recall_token_budget,
        }
        if matched_items:
            prior_failed_budget = False
            for _, prior in matched_items:
                prior_usage = prior.get("provider_usage") or {}
                try:
                    prior_total_tokens = int(
                        prior_usage.get("total_tokens") or 0
                    )
                except (TypeError, ValueError):
                    prior_total_tokens = 0
                prior_failed_budget = prior_failed_budget or (
                    prior.get("terminal_state") == "BUDGET_EXCEEDED"
                    or prior.get("result_code") == "QUARANTINED"
                    or prior_total_tokens >= 120_000
                )
            selected_recipe.update(
                {
                    "recipe_id": (
                        "FINFLUX_SIGNED_MEMORY_GUARDED_V1"
                        if prior_failed_budget
                        else "FINFLUX_SIGNED_MEMORY_HASH_CONTEXT_V1"
                    ),
                    # Only an operational timeout may change. Financial route,
                    # Worker/Skill set and deterministic calculations remain fixed.
                    "tool_timeout_seconds": 60 if prior_failed_budget else 90,
                }
            )
        plan = {
            "protocol": "FINFLUX_OPERATIONAL_MEMORY_PLAN_V1.0",
            "run_id": run_id,
            "recipe_id": selected_recipe["recipe_id"],
            "recipe": selected_recipe,
            "recall_status": recall_status,
            "reference_handles": handles,
            "lookup_receipt_sha256": lookup_receipt_sha256,
            "metrics": metrics,
            "advisory_only": True,
            "prompt_injected": False,
            "financial_truth_authority": False,
            "route_or_skill_override_allowed": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
        }
        plan["plan_sha256"] = canonical_sha256(plan)
        return plan

    def _commit_signed_operational_memory(
        self,
        run: dict[str, Any],
        submission: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Write a redacted signed experience after the authoritative Run is durable."""
        gate = run.get("human_gate") or {}
        final_result = run.get("final_result") or {}
        decision = str(gate.get("decision") or "").strip()
        signature = str(gate.get("post_decision_hash") or "").strip()
        signer = str(gate.get("human_actor_id") or "").strip()
        signed_at = str(gate.get("decided_at") or "").strip()
        if (
            decision not in {"APPROVE_PASS", "CONFIRM_BLOCK", "REQUEST_EVIDENCE"}
            or not final_result
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not signer
            or not signed_at
        ):
            return None
        route = run.get("root_route_decision") or {}
        usage = run.get("provider_usage") or {}
        summary_payload = {
            "protocol": "FINFLUX_SIGNED_OPERATIONAL_EXPERIENCE_V1.0",
            "source_run_id": run.get("run_id"),
            "profile": submission.get("profile"),
            "declared_purpose": (submission.get("metadata") or {}).get(
                "declared_purpose"
            ),
            "route": route.get("route"),
            "reason_codes": sorted(route.get("reason_codes") or []),
            "worker_roles": sorted(
                ((route.get("worker_plan") or {}).get("workers") or [])
            ),
            "skill_versions": dict(route.get("required_skill_versions") or {}),
            "execution_recipe_id": (
                (run.get("operational_memory_plan") or {}).get("recipe_id")
                or "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1"
            ),
            "execution_policy_id": run.get("execution_policy_id"),
            "terminal_state": run.get("state"),
            "human_decision": decision,
            "result_code": (final_result.get("outcome") or {}).get("code"),
            "provider_usage": {
                key: usage.get(key)
                for key in ("status", "total_tokens", "call_count", "source")
            },
        }
        summary = json.dumps(
            summary_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        reference = MemoryReference(
            run_id=str(run["run_id"]),
            role="finflux-execution-control",
            summary=summary,
            uri=f"finflux://runs/{run['run_id']}/operational-memory",
            content_sha256=context_content_sha256(summary),
        )
        approval = HumanApproval(
            decision=decision,
            signer_id=signer,
            signature_sha256=signature,
            signed_at=signed_at,
        )
        receipt_path = (
            self.root / "context_memory" / "commit_receipts" / f"{run['run_id']}.json"
        )
        existing_receipt = _json_read(receipt_path, None)
        if isinstance(existing_receipt, dict):
            declared_receipt_hash = str(
                existing_receipt.get("receipt_sha256") or ""
            ).strip()
            receipt_body = {
                key: value
                for key, value in existing_receipt.items()
                if key != "receipt_sha256"
            }
            receipt_identity_valid = (
                re.fullmatch(r"[0-9a-f]{64}", declared_receipt_hash)
                and canonical_sha256(receipt_body) == declared_receipt_hash
                and existing_receipt.get("run_id") == run.get("run_id")
                and existing_receipt.get("role")
                == "finflux-execution-control"
                and existing_receipt.get("human_signature_sha256") == signature
            )
            if receipt_identity_valid:
                if (
                    existing_receipt.get("content_sha256")
                    == reference.content_sha256
                ):
                    return existing_receipt
                raise ValueError(
                    "SIGNED_OPERATIONAL_MEMORY_IMMUTABILITY_VIOLATION"
                )
            raise ValueError("SIGNED_OPERATIONAL_MEMORY_RECEIPT_INVALID")
        receipt = self.context_memory.commit_long_term(
            reference=reference,
            approval=approval,
        )
        _json_atomic(receipt_path, receipt)
        # Delivery is detached from the Human transaction. Disabled/local mode
        # returns immediately and never creates a network request.
        schedule = getattr(self.context_memory, "schedule_outbox_drain", None)
        if callable(schedule):
            schedule(limit=20)
        return receipt

    def memory_status(self, run_id: str | None = None) -> dict[str, Any]:
        structured = self.memory.status()
        context = self.context_memory.status()
        selected = None
        if run_id:
            try:
                run = self.get_run(run_id)
                selected = {
                    "run_id": run_id,
                    "operational_memory_plan": run.get("operational_memory_plan"),
                    "context_capsule_handle": run.get("context_capsule_handle"),
                }
            except (FileNotFoundError, ValueError):
                selected = {"run_id": run_id, "status": "NOT_FOUND"}
        return {
            "protocol": "FINFLUX_MEMORY_STATUS_V2.0",
            "structured": structured,
            "context": context,
            "selected_run": selected,
            "truth_boundary": (
                "新V0.2代码路径仅召回脱敏运行经验，并按Run/Role查询键至多一次；"
                "旧Run未生成operational_memory_plan时不声称使用记忆。召回内容不进入"
                "Agent提示词，也不覆盖EvidenceBundle、金融计算、路由、Skill或Human决定。"
            ),
        }

    def _index(self) -> dict[str, Any]:
        defaults = {
            "latest_submission_id": None,
            "latest_run_id": None,
            "latest_change_bundle_id": None,
            "selected_run_id": None,
            "judge_run_id": None,
            "judge_selected_at": None,
            "judge_selected_by": None,
            "judge_reason": None,
            "selection_mode": None,
        }
        stored = self._cached_json_read(self.index_path, {}) or {}
        return {**defaults, **stored}

    def _save_index(self, data: dict[str, Any]) -> None:
        _json_atomic(self.index_path, data)
        self._invalidate_json_cache(self.index_path)

    def _persist_run(self, run: dict[str, Any]) -> None:
        """Persist the authoritative Run and its compact operational memory."""
        bootstrap_lifecycle(run)
        projected = project_run_states(run)
        run["execution_state"] = projected["execution_state"]
        run["business_disposition"] = projected["business_disposition"]
        run["lifecycle_terminal"] = projected["lifecycle_terminal"]
        run["state_consistency"] = projected["consistency"]
        run["updated_at"] = utc_now()
        run_path = self.runs / f"{run['run_id']}.json"
        _json_atomic(run_path, run)
        self._invalidate_json_cache(run_path)
        submission = self.get_submission(str(run["submission_id"]))
        self.memory.update_run(run, submission)
        try:
            self._commit_signed_operational_memory(run, submission)
        except Exception as exc:  # noqa: BLE001 - signed Run remains authoritative
            diagnostic = {
                "protocol": "FINFLUX_CONTEXT_MEMORY_COMMIT_DIAGNOSTIC_V1.0",
                "run_id": run.get("run_id"),
                "status": "LOCAL_MEMORY_COMMIT_FAILED",
                "failure_class": type(exc).__name__,
                "recorded_at": utc_now(),
                "run_or_human_state_changed": False,
            }
            diagnostic["diagnostic_sha256"] = canonical_sha256(diagnostic)
            _json_atomic(
                self.root
                / "context_memory"
                / "commit_diagnostics"
                / f"{run['run_id']}.json",
                diagnostic,
            )

    @staticmethod
    def _client_run_key(value: str) -> str:
        """Validate an opaque client key without ever persisting the secret itself."""
        key = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{16,160}", key):
            raise ValueError(
                "client_idempotency_key must be 16-160 safe opaque characters"
            )
        return key

    def _run_creation_binding(
        self, submission_id: str, client_idempotency_key: str
    ) -> dict[str, Any]:
        key = self._client_run_key(client_idempotency_key)
        key_sha256 = hashlib.sha256(key.encode("utf-8")).hexdigest()
        request_sha256 = canonical_sha256(
            {
                "submission_id": str(submission_id),
                "client_idempotency_key_sha256": key_sha256,
            }
        )
        return {
            "protocol": "FINFLUX_RUN_CREATE_IDEMPOTENCY_V1.0",
            "client_idempotency_key_sha256": key_sha256,
            "request_sha256": request_sha256,
        }

    def _run_creation_paths(self, key_sha256: str) -> tuple[Path, Path]:
        root = self.run_creation_attempts / key_sha256
        return root / "attempt.json", root / "commit.json"

    def _read_run_creation_attempt(
        self, binding: dict[str, Any]
    ) -> dict[str, Any] | None:
        attempt_path, _ = self._run_creation_paths(
            binding["client_idempotency_key_sha256"]
        )
        attempt = _json_read(attempt_path)
        if attempt is None:
            return None
        if not isinstance(attempt, dict):
            raise ValueError("run creation attempt ledger is malformed")
        unsigned = {key: value for key, value in attempt.items() if key != "attempt_sha256"}
        if (
            attempt.get("protocol") != "FINFLUX_RUN_CREATE_ATTEMPT_V1.0"
            or attempt.get("client_idempotency_key_sha256")
            != binding["client_idempotency_key_sha256"]
            or attempt.get("request_sha256") != binding["request_sha256"]
            or attempt.get("attempt_sha256") != canonical_sha256(unsigned)
        ):
            raise ValueError(
                "client idempotency key is already bound to a different or invalid Run request"
            )
        return attempt

    def _ensure_run_creation_attempt(
        self, submission_id: str, binding: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self._read_run_creation_attempt(binding)
        if existing is not None:
            if existing.get("submission_id") != submission_id:
                raise ValueError(
                    "client idempotency key is already bound to another submission"
                )
            return existing
        attempt = {
            "protocol": "FINFLUX_RUN_CREATE_ATTEMPT_V1.0",
            "client_idempotency_key_sha256": binding[
                "client_idempotency_key_sha256"
            ],
            "request_sha256": binding["request_sha256"],
            "submission_id": submission_id,
            "prepared_at_utc": utc_now(),
        }
        attempt["attempt_sha256"] = canonical_sha256(attempt)
        attempt_path, _ = self._run_creation_paths(
            binding["client_idempotency_key_sha256"]
        )
        _json_atomic(attempt_path, attempt)
        return attempt

    def _runs_for_creation_binding(
        self, binding: dict[str, Any]
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for path in self.runs.glob("RUN-*.json"):
            run = _json_read(path)
            receipt = (run or {}).get("run_creation_idempotency") or {}
            if (
                receipt.get("client_idempotency_key_sha256")
                == binding["client_idempotency_key_sha256"]
                and receipt.get("request_sha256") == binding["request_sha256"]
            ):
                unsigned = {
                    key: value
                    for key, value in receipt.items()
                    if key != "receipt_sha256"
                }
                if (
                    receipt.get("protocol")
                    != "FINFLUX_RUN_CREATE_IDEMPOTENCY_V1.0"
                    or receipt.get("receipt_sha256") != canonical_sha256(unsigned)
                ):
                    raise ValueError(
                        "durable Run idempotency receipt failed integrity validation"
                    )
                matches.append(run)
        if len(matches) > 1:
            raise ValueError(
                "idempotency invariant violated: one client key resolved to multiple Runs"
            )
        return matches

    def _read_run_creation_commit(
        self, binding: dict[str, Any], attempt: dict[str, Any]
    ) -> dict[str, Any] | None:
        _, commit_path = self._run_creation_paths(
            binding["client_idempotency_key_sha256"]
        )
        commit = _json_read(commit_path)
        if commit is None:
            return None
        if not isinstance(commit, dict):
            raise ValueError("run creation commit ledger is malformed")
        unsigned = {key: value for key, value in commit.items() if key != "commit_sha256"}
        if (
            commit.get("protocol") != "FINFLUX_RUN_CREATE_COMMIT_V1.0"
            or commit.get("client_idempotency_key_sha256")
            != binding["client_idempotency_key_sha256"]
            or commit.get("request_sha256") != binding["request_sha256"]
            or commit.get("attempt_sha256") != attempt.get("attempt_sha256")
            or commit.get("commit_sha256") != canonical_sha256(unsigned)
        ):
            raise ValueError("run creation commit ledger failed integrity validation")
        return commit

    def _write_run_creation_commit(
        self,
        binding: dict[str, Any],
        attempt: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        commit = {
            "protocol": "FINFLUX_RUN_CREATE_COMMIT_V1.0",
            "client_idempotency_key_sha256": binding[
                "client_idempotency_key_sha256"
            ],
            "request_sha256": binding["request_sha256"],
            "attempt_sha256": attempt["attempt_sha256"],
            "run_id": run_id,
            "committed_at_utc": utc_now(),
        }
        commit["commit_sha256"] = canonical_sha256(commit)
        _, commit_path = self._run_creation_paths(
            binding["client_idempotency_key_sha256"]
        )
        existing = self._read_run_creation_commit(binding, attempt)
        if existing is not None:
            if existing.get("run_id") != run_id:
                raise ValueError(
                    "idempotency invariant violated: commit points to another Run"
                )
            return existing
        _json_atomic(commit_path, commit)
        return commit

    @staticmethod
    def _run_creation_response(
        run: dict[str, Any], *, replayed: bool
    ) -> dict[str, Any]:
        response = copy.deepcopy(run)
        response["run_creation_response"] = {
            "protocol": "FINFLUX_RUN_CREATE_RESPONSE_V1.0",
            "replayed": replayed,
            "live_run_created_by_this_request": not replayed,
            "model_dispatch_claim": "NOT_RECORDED_BY_CREATION_LEDGER",
        }
        return response

    def reconcile_run_creation(
        self, submission_id: str, client_idempotency_key: str
    ) -> dict[str, Any]:
        """Resolve a prepared client attempt without creating or dispatching a Run."""
        binding = self._run_creation_binding(submission_id, client_idempotency_key)
        with self.lock:
            attempt = self._read_run_creation_attempt(binding)
            if attempt is None:
                return {
                    "protocol": "FINFLUX_RUN_CREATE_RECONCILIATION_V1.0",
                    "status": "NOT_FOUND",
                    "submission_id": submission_id,
                    **binding,
                    "run_id": None,
                    "run": None,
                    "creates_run": False,
                    "dispatches_model": False,
                }
            if attempt.get("submission_id") != submission_id:
                raise ValueError(
                    "client idempotency key is already bound to another submission"
                )
            commit = self._read_run_creation_commit(binding, attempt)
            matches = self._runs_for_creation_binding(binding)
            if commit is not None:
                run = self.get_run(str(commit.get("run_id") or ""))
                if not matches or matches[0].get("run_id") != run.get("run_id"):
                    raise ValueError("run creation commit does not match durable Run evidence")
            elif matches:
                run = matches[0]
                commit = self._write_run_creation_commit(
                    binding, attempt, str(run["run_id"])
                )
            else:
                run = None
            return {
                "protocol": "FINFLUX_RUN_CREATE_RECONCILIATION_V1.0",
                "status": "COMMITTED" if run else "PREPARED",
                "submission_id": submission_id,
                **binding,
                "attempt_sha256": attempt["attempt_sha256"],
                "commit_sha256": (commit or {}).get("commit_sha256"),
                "run_id": (run or {}).get("run_id"),
                "run": self._run_creation_response(run, replayed=True) if run else None,
                "creates_run": False,
                "dispatches_model": False,
            }

    def _validate_zip(self, body: bytes) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        total_uncompressed = 0
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            if len(infos) > 50:
                raise ValueError("ZIP 文件数超过 50 个")
            for info in infos:
                pure = PurePosixPath(info.filename)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError("ZIP 包含不安全路径")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("ZIP 不允许符号链接")
                total_uncompressed += info.file_size
                if total_uncompressed > 25 * 1024 * 1024:
                    raise ValueError("ZIP 解压后总大小超过 25MB")
                if info.compress_size and info.file_size / info.compress_size > 100:
                    raise ValueError("ZIP 压缩比异常")
                if info.is_dir():
                    continue
                content = archive.read(info)
                results.append(
                    {
                        "name": info.filename,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
        return results

    @staticmethod
    def _parse_futures_csv(body: bytes, target_instrument: str = "") -> dict[str, Any]:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("gb18030")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV 缺少表头")
        aliases = {
            "instrument": ["instrument", "instrument_id", "symbol", "合约", "合约代码"],
            "trade_date": ["trade_date", "observation_date", "date", "交易日", "日期"],
            "close": ["close", "close_price", "收盘价", "今收盘"],
            "settle": ["settle", "settlement", "settlement_price", "结算价", "今结算"],
            "candidate_mapping": ["candidate_mapping", "candidate_price_field", "候选字段"],
            "business_purpose": ["business_purpose", "declared_purpose", "业务用途"],
        }
        normalized = {str(name).strip().lower(): name for name in reader.fieldnames}

        def column(kind: str) -> str | None:
            for alias in aliases[kind]:
                if alias.lower() in normalized:
                    return normalized[alias.lower()]
            return None

        cols = {kind: column(kind) for kind in aliases}
        if not cols["close"] or not cols["settle"]:
            raise ValueError("futures_settlement 至少需要 close 与 settle 两列")
        rows = list(reader)
        if not rows:
            raise ValueError("CSV 没有数据行")
        row = rows[-1]
        target = target_instrument.strip().upper()
        if target and cols["instrument"]:
            matches = [
                candidate
                for candidate in rows
                if str(candidate.get(cols["instrument"] or "", "")).strip().upper()
                == target
            ]
            if not matches:
                raise ValueError(f"CSV 中未找到目标合约 {target}")
            row = matches[-1]
        try:
            close = float(str(row[cols["close"]]).replace(",", "").strip())
            settle = float(str(row[cols["settle"]]).replace(",", "").strip())
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("close/settle 无法解析为数值") from exc
        return {
            "row_count": len(rows),
            "instrument": str(row.get(cols["instrument"] or "", "UNKNOWN") or "UNKNOWN"),
            "instrument_candidates": list(
                dict.fromkeys(
                    str(candidate.get(cols["instrument"] or "", "")).strip()
                    for candidate in rows
                    if str(candidate.get(cols["instrument"] or "", "")).strip()
                )
            )[:100],
            "trade_date": str(row.get(cols["trade_date"] or "", "UNKNOWN") or "UNKNOWN"),
            "close": close,
            "settle": settle,
            "candidate_mapping": str(
                row.get(cols["candidate_mapping"] or "", "") or ""
            ).strip().lower() or None,
            "business_purpose": str(
                row.get(cols["business_purpose"] or "", "") or ""
            ).strip() or None,
            "columns": {key: value for key, value in cols.items() if value},
        }

    @staticmethod
    def _csv_rows(body: bytes) -> tuple[list[str], list[dict[str, str]]]:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("gb18030")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV 缺少表头")
        rows = list(reader)
        if not rows:
            raise ValueError("CSV 没有数据行")
        return [str(item) for item in reader.fieldnames], rows

    @staticmethod
    def _csv_column(fieldnames: list[str], aliases: tuple[str, ...]) -> str | None:
        normalized = {str(name).strip().lower(): name for name in fieldnames}
        for alias in aliases:
            if alias.lower() in normalized:
                return normalized[alias.lower()]
        return None

    @staticmethod
    def _select_csv_row(
        rows: list[dict[str, str]], instrument_column: str | None, target: str
    ) -> dict[str, str]:
        if not target or not instrument_column:
            return rows[-1]
        normalized_target = target.strip().upper()
        matches = [
            row
            for row in rows
            if str(row.get(instrument_column, "")).strip().upper() == normalized_target
        ]
        if not matches:
            raise ValueError(f"CSV 中未找到目标标的 {target}")
        return matches[-1]

    @classmethod
    def _parse_equity_csv(
        cls, body: bytes, target_instrument: str = ""
    ) -> dict[str, Any]:
        fieldnames, rows = cls._csv_rows(body)
        aliases = {
            "instrument": ("instrument", "stock_code", "ts_code", "证券代码", "股票代码", "代码"),
            "event_date": ("event_date", "ex_date", "除权除息日", "事件日期", "生效日期"),
            "declared_adjustment": (
                "declared_adjustment", "candidate_mapping", "adjustment", "adjustment_type",
                "复权方式", "声明复权",
            ),
            "observed_adjustment": (
                "observed_adjustment", "observed_semantics", "实际复权", "实际序列表现",
            ),
            "return_difference": (
                "return_difference", "return_difference_pct_points", "收益率差异",
            ),
            "cash_dividend_per_10": ("cash_dividend_per_10", "每10股派息", "派息"),
            "stock_or_transfer_per_10": (
                "stock_or_transfer_per_10", "每10股送转", "送转股",
            ),
        }
        cols = {key: cls._csv_column(fieldnames, values) for key, values in aliases.items()}
        recognizable = bool(
            cols["instrument"]
            and cols["event_date"]
            and (
                cols["declared_adjustment"]
                or cols["cash_dividend_per_10"]
                or cols["stock_or_transfer_per_10"]
            )
        )
        if not recognizable:
            raise ValueError("未识别为股票公司行动或复权证据")
        row = cls._select_csv_row(rows, cols["instrument"], target_instrument)

        def optional_float(column_name: str | None) -> float | None:
            if not column_name:
                return None
            raw = str(row.get(column_name, "") or "").replace(",", "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        declared = str(row.get(cols["declared_adjustment"] or "", "") or "").strip().lower()
        observed = str(row.get(cols["observed_adjustment"] or "", "") or "").strip().lower()
        return {
            "row_count": len(rows),
            "instrument": str(row.get(cols["instrument"] or "", "") or "").strip(),
            "instrument_candidates": list(dict.fromkeys(
                str(item.get(cols["instrument"] or "", "") or "").strip()
                for item in rows
                if str(item.get(cols["instrument"] or "", "") or "").strip()
            ))[:100],
            "event_date": str(row.get(cols["event_date"] or "", "") or "").strip(),
            "declared_adjustment": declared or None,
            "observed_adjustment": observed or None,
            "return_difference": optional_float(cols["return_difference"]),
            "cash_dividend_per_10": optional_float(cols["cash_dividend_per_10"]),
            "stock_or_transfer_per_10": optional_float(cols["stock_or_transfer_per_10"]),
            "columns": {key: value for key, value in cols.items() if value},
        }

    @classmethod
    def _parse_fund_csv(
        cls, body: bytes, target_instrument: str = ""
    ) -> dict[str, Any]:
        fieldnames, rows = cls._csv_rows(body)
        aliases = {
            "instrument": ("instrument", "fund_code", "基金代码", "代码"),
            "nav_date": ("nav_date", "observation_date", "date", "净值日期", "日期"),
            "unit_nav": ("unit_nav", "单位净值"),
            "cumulative_nav": ("cumulative_nav", "累计净值"),
            "candidate_nav_field": (
                "candidate_nav_field", "candidate_mapping", "候选净值字段",
            ),
        }
        cols = {key: cls._csv_column(fieldnames, values) for key, values in aliases.items()}
        if not (cols["instrument"] and (cols["unit_nav"] or cols["cumulative_nav"])):
            raise ValueError("未识别为基金净值证据")
        row = cls._select_csv_row(rows, cols["instrument"], target_instrument)

        def required_or_none(column_name: str | None) -> float | None:
            if not column_name:
                return None
            raw = str(row.get(column_name, "") or "").replace(",", "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"基金净值字段 {column_name} 不是数值") from exc

        return {
            "row_count": len(rows),
            "instrument": str(row.get(cols["instrument"] or "", "") or "").strip(),
            "instrument_candidates": list(dict.fromkeys(
                str(item.get(cols["instrument"] or "", "") or "").strip()
                for item in rows
                if str(item.get(cols["instrument"] or "", "") or "").strip()
            ))[:100],
            "nav_date": str(row.get(cols["nav_date"] or "", "") or "").strip(),
            "unit_nav": required_or_none(cols["unit_nav"]),
            "cumulative_nav": required_or_none(cols["cumulative_nav"]),
            "candidate_nav_field": str(
                row.get(cols["candidate_nav_field"] or "", "") or ""
            ).strip().lower() or None,
            "columns": {key: value for key, value in cols.items() if value},
        }

    @staticmethod
    def _parse_generic_content(
        body: bytes, suffix: str, zip_manifest: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "parser_mode": "METADATA_ONLY",
            "content_kind": suffix.lstrip(".").upper() or "UNKNOWN",
            "byte_count": len(body),
        }
        if suffix == ".zip":
            result.update(
                {
                    "parser_mode": "ZIP_MANIFEST",
                    "file_count": len(zip_manifest),
                    "filenames": [item["name"] for item in zip_manifest[:20]],
                }
            )
            return result
        if suffix == ".json":
            try:
                value = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("JSON 文件无法解析") from exc
            result.update(
                {
                    "parser_mode": "JSON_STRUCTURE",
                    "json_type": type(value).__name__,
                    "top_level_keys": (
                        [str(key) for key in list(value)[:30]]
                        if isinstance(value, dict)
                        else []
                    ),
                    "item_count": (
                        len(value.get("items", []))
                        if isinstance(value, dict) and isinstance(value.get("items"), list)
                        else len(value)
                        if isinstance(value, (dict, list))
                        else 1
                    ),
                }
            )
            return result
        if suffix in {".csv", ".txt", ".md", ".xml", ".html", ".htm"}:
            try:
                text = body.decode("utf-8-sig")
                encoding = "utf-8-sig"
            except UnicodeDecodeError:
                text = body.decode("gb18030")
                encoding = "gb18030"
            result.update(
                {
                    "parser_mode": "TEXT_STRUCTURE",
                    "encoding": encoding,
                    "line_count": len(text.splitlines()),
                    "character_count": len(text),
                    "preview": text[:500],
                }
            )
            if suffix == ".csv":
                reader = csv.reader(io.StringIO(text))
                rows = list(reader)
                result["columns"] = rows[0][:50] if rows else []
                result["row_count"] = max(0, len(rows) - 1)
            return result
        return result

    def _known_evidence_metadata(self, sha256: str) -> dict[str, Any] | None:
        """Return declarations previously bound to exactly the same immutable bytes."""
        matches: list[dict[str, Any]] = []
        if not self.submissions.is_dir():
            return None
        for path in self.submissions.glob("*.json"):
            record = _json_read(path, {}) or {}
            if str((record.get("file") or {}).get("sha256", "")) == sha256:
                matches.append(record)
        if not matches:
            return None
        latest = max(matches, key=lambda item: str(item.get("created_at", "")))
        metadata = latest.get("metadata") or {}
        return {
            "submission_id": latest.get("submission_id"),
            "declared_source": metadata.get("declared_source"),
            "rights_basis": metadata.get("rights_basis"),
            "provider": metadata.get("provider"),
            "confidentiality_class": metadata.get("confidentiality_class"),
            "declared_purpose": metadata.get("declared_purpose"),
            "target_instrument": metadata.get("target_instrument")
            or metadata.get("entity_query"),
            "contract_multiplier": metadata.get("contract_multiplier"),
            "candidate_mapping": metadata.get("candidate_mapping"),
        }

    @staticmethod
    def _contract_prefix(instrument: str) -> str:
        match = re.match(r"([A-Za-z]+)", str(instrument or "").strip())
        return match.group(1).upper() if match else ""

    def inspect_file(
        self,
        filename: str,
        body: bytes,
        intake_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Quarantine and inspect bytes without creating evidence or calling a model."""
        if not body:
            raise ValueError("证据文件为空")
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError("证据文件超过10MB POC上限")
        intake_context = dict(intake_context or {})
        safe_name = _safe_name(filename)
        suffix = Path(safe_name).suffix.lower()
        sha256 = hashlib.sha256(body).hexdigest()
        zip_manifest: list[dict[str, Any]] = []
        parsed: dict[str, Any]
        profile = UNIVERSAL_PROFILE_ID
        asset_class = "unknown"
        entity = ""
        confidence = 0.55
        purpose_candidate = "evidence_review"
        multiplier: float | None = None
        mapping: str | None = None
        try:
            if suffix == ".zip":
                zip_manifest = self._validate_zip(body)
                parsed = self._parse_generic_content(body, suffix, zip_manifest)
            elif suffix == ".csv":
                parsers = (
                    (FUTURES_PROFILE_ID, self._parse_futures_csv),
                    (FUND_PROFILE_ID, self._parse_fund_csv),
                    (EQUITY_PROFILE_ID, self._parse_equity_csv),
                )
                parsed = {}
                for candidate_profile, parser in parsers:
                    try:
                        parsed = parser(
                            body,
                            str(
                                intake_context.get("entity_query")
                                or intake_context.get("target_instrument")
                                or ""
                            ),
                        )
                        profile = candidate_profile
                        break
                    except ValueError:
                        continue
                if profile == UNIVERSAL_PROFILE_ID:
                    parsed = self._parse_generic_content(body, suffix, zip_manifest)
                else:
                    spec = LIVE_PROFILE_SPECS[profile]
                    asset_class = str(spec["asset_class"])
                    entity = str(parsed.get("instrument", ""))
                    purpose_candidate = str(
                        intake_context.get("declared_purpose")
                        or parsed.get("business_purpose")
                        or spec["default_purpose"]
                    )
                    if profile == FUTURES_PROFILE_ID:
                        mapping = str(parsed.get("candidate_mapping") or "settle")
                        multiplier = FUTURES_MULTIPLIERS.get(self._contract_prefix(entity))
                    elif profile == EQUITY_PROFILE_ID:
                        mapping = str(parsed.get("declared_adjustment") or "") or None
                    else:
                        mapping = str(parsed.get("candidate_nav_field") or "unit_nav")
                    confidence = 0.98 if entity else 0.86
            else:
                parsed = self._parse_generic_content(body, suffix, zip_manifest)
        except (UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise ValueError("文件结构无法安全解析") from exc

        if profile == UNIVERSAL_PROFILE_ID:
            searchable = " ".join(
                [safe_name, str(parsed.get("preview", ""))]
                + [str(value) for value in parsed.get("columns", [])]
            ).lower()
            classifiers = [
                ("option", ("strike", "exercise", "call", "put", "行权", "期权")),
                ("fund", ("unit_nav", "nav", "净值", "基金")),
                ("equity", ("ts_code", "stock_code", "qfq", "hfq", "复权", "股票")),
                ("research", ("研报", "research", "report", "publisher")),
                ("institution", ("world bank", "imf", "bis", "ecb", "macro", "宏观")),
            ]
            for candidate, keywords in classifiers:
                if any(keyword in searchable for keyword in keywords):
                    asset_class = candidate
                    confidence = 0.76
                    purpose_candidate = (
                        "research_review" if candidate in {"research", "institution"}
                        else "evidence_review"
                    )
                    break

        known = self._known_evidence_metadata(sha256)
        if profile in LIVE_PROFILE_IDS and known:
            known_target = str(known.get("target_instrument") or "").strip()
            candidates = list(parsed.get("instrument_candidates") or [])
            if known_target and (not candidates or known_target in candidates):
                entity = known_target
                if profile == FUTURES_PROFILE_ID:
                    multiplier = float(known.get("contract_multiplier") or 0) or multiplier
                mapping = str(known.get("candidate_mapping") or mapping or "") or None
                purpose_candidate = str(
                    known.get("declared_purpose") or purpose_candidate
                )
        multiple_instruments = len(parsed.get("instrument_candidates") or []) > 1
        requested_target = str(
            intake_context.get("entity_query")
            or intake_context.get("target_instrument")
            or ""
        ).strip()
        if requested_target:
            entity = requested_target
        elif multiple_instruments and not (known or {}).get("target_instrument"):
            entity = ""
            multiplier = None
        context = dict(intake_context or {})
        task_instruction = str(context.get("task_instruction", "")).strip()
        if len(task_instruction) > MAX_CASE_INSTRUCTION_CHARS:
            raise ValueError("任务指令超过2000字符上限")
        inferred = {
            "profile": profile,
            "asset_class": asset_class,
            "entity_query": entity or None,
            "declared_purpose": purpose_candidate,
            "candidate_mapping": mapping,
            "contract_multiplier": multiplier,
            "declared_source": (known or {}).get("declared_source"),
            "rights_basis": (known or {}).get("rights_basis"),
            "provider": (known or {}).get("provider") or "UNIFIED_FILE_INTAKE",
            "confidentiality_class": (known or {}).get("confidentiality_class"),
            "task_instruction": task_instruction or None,
            "task_instruction_sha256": (
                hashlib.sha256(task_instruction.encode("utf-8")).hexdigest()
                if task_instruction
                else None
            ),
            "input_mode": str(context.get("input_mode", "FILE")).strip().upper()
            or "FILE",
        }
        for field in (
            "declared_source",
            "rights_basis",
            "provider",
            "confidentiality_class",
            "declared_purpose",
            "asset_class",
            "entity_query",
        ):
            value = context.get(field)
            if value is not None and str(value).strip():
                inferred[field] = str(value).strip()
        # A registered Profile may provide a deterministic default purpose
        # (or the source row may declare it).  Only stop the one-click pipeline
        # when the purpose is genuinely unresolved.
        required: list[str] = []
        if (
            profile == UNIVERSAL_PROFILE_ID
            or not str(inferred.get("declared_purpose") or "").strip()
            or inferred.get("declared_purpose") == "evidence_review"
        ):
            required.append("declared_purpose")
        if not inferred["declared_source"]:
            required.extend(["declared_source", "rights_basis"])
        if not inferred["confidentiality_class"]:
            required.append("confidentiality_class")
        missing_evidence_fields: list[str] = []
        if profile == FUTURES_PROFILE_ID and not multiplier:
            required.append("contract_multiplier")
        if profile in LIVE_PROFILE_IDS and not entity:
            required.append("entity_query")
        if profile == EQUITY_PROFILE_ID:
            if not parsed.get("event_date"):
                missing_evidence_fields.append("event_date")
            if not mapping:
                missing_evidence_fields.append("declared_adjustment")
        if profile == FUND_PROFILE_ID:
            if not parsed.get("nav_date"):
                missing_evidence_fields.append("nav_date")
            required_nav = (
                "cumulative_nav"
                if inferred.get("declared_purpose") == "total_return_analysis"
                else "unit_nav"
            )
            if parsed.get(required_nav) is None:
                missing_evidence_fields.append(required_nav)
        input_hash = canonical_sha256(
            {
                "file_sha256": sha256,
                "filename": safe_name,
                "task_instruction_sha256": inferred.get("task_instruction_sha256"),
                "input_mode": inferred.get("input_mode"),
            }
        )
        skill_invocations = []
        previous = input_hash
        for skill_id, version in INTAKE_INSPECTION_SKILLS:
            output = canonical_sha256(
                {"skill_id": skill_id, "version": version, "input": previous, "inferred": inferred}
            )
            skill_invocations.append(
                {"skill_id": skill_id, "version": version, "input_sha256": previous, "output_sha256": output}
            )
            previous = output
        inspection_id = (
            f"INSP-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-"
            f"{sha256[:10]}-{secrets.token_hex(2)}"
        )
        inspection_dir = self.inspections / inspection_id
        inspection_file = inspection_dir / safe_name
        record = {
            "protocol": "FINFLUX_INTAKE_INSPECTION_V1.0",
            "inspection_id": inspection_id,
            "status": (
                "WAIT_FOR_PROFILE"
                if profile == UNIVERSAL_PROFILE_ID
                else "WAIT_FOR_EVIDENCE"
                if missing_evidence_fields
                else "READY_FOR_CONFIRMATION"
                if required
                else "READY_TO_COMMIT"
            ),
            "created_at": utc_now(),
            "file": {"name": safe_name, "size_bytes": len(body), "sha256": sha256},
            "parsed": parsed,
            "zip_manifest": zip_manifest,
            "inferred": inferred,
            "confidence": confidence,
            "known_evidence_match": known,
            "required_confirmations": list(dict.fromkeys(required)),
            "missing_evidence_fields": list(dict.fromkeys(missing_evidence_fields)),
            "wait_reason_codes": (
                ["UNKNOWN_FINANCIAL_PROFILE", "DECLARE_ASSET_PURPOSE_AND_SCHEMA"]
                if profile == UNIVERSAL_PROFILE_ID
                else [f"MISSING_SOURCE_FIELD_{item.upper()}" for item in missing_evidence_fields]
            ),
            "execution_readiness_candidate": (
                "WAIT_FOR_PROFILE"
                if profile == UNIVERSAL_PROFILE_ID
                else "WAIT_FOR_EVIDENCE"
                if missing_evidence_fields
                else "AGENTTEAMS_EXECUTABLE"
                if profile in LIVE_PROFILE_IDS and (profile != FUTURES_PROFILE_ID or multiplier)
                else "WAIT_FOR_EVIDENCE"
            ),
            "skill_invocations": skill_invocations,
            "token_usage": {"total_tokens": 0, "call_count": 0, "source": "SERVER_DETERMINISTIC_NO_MODEL"},
            "committed_submission_id": None,
        }
        with self.lock:
            inspection_dir.mkdir(parents=True, exist_ok=True)
            inspection_file.write_bytes(body)
            _json_atomic(inspection_dir / "inspection.json", record)
        return record

    def get_inspection(self, inspection_id: str) -> dict[str, Any]:
        path = self.inspections / Path(inspection_id).name / "inspection.json"
        record = _json_read(path)
        if not isinstance(record, dict):
            raise KeyError(f"未找到Inspection: {inspection_id}")
        return record

    def commit_inspection(
        self, inspection_id: str, confirmations: dict[str, Any]
    ) -> dict[str, Any]:
        with self.lock:
            inspection = self.get_inspection(inspection_id)
            if inspection.get("committed_submission_id"):
                return self.get_submission(str(inspection["committed_submission_id"]))
            filename = str(inspection["file"]["name"])
            file_path = self.inspections / Path(inspection_id).name / filename
            body = file_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != inspection["file"]["sha256"]:
                raise ValueError("Inspection中的原始文件哈希已改变，拒绝固化")
            inferred = dict(inspection.get("inferred") or {})
            for field in inspection.get("required_confirmations") or []:
                value = confirmations.get(field, inferred.get(field))
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ValueError(f"尚需确认: {field}")
                inferred[field] = value
            # These values describe intended downstream use and candidate
            # adapter configuration. They may be confirmed after inspection,
            # but never alter immutable source bytes or parsed financial
            # values. One real file can therefore prove both a correct and an
            # incorrect mapping without manufacturing market data.
            for field in {
                "declared_purpose",
                "candidate_mapping",
                "entity_query",
                "contract_multiplier",
            }:
                if field not in confirmations:
                    continue
                value = confirmations.get(field)
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                inferred[field] = value
            # A task instruction may be entered after zero-token file
            # inspection. Bind it at commit time so evidence bytes stay
            # unchanged while the resulting CaseEnvelope remains hash-sealed.
            task_instruction = str(
                confirmations.get(
                    "task_instruction", inferred.get("task_instruction", "")
                )
                or ""
            ).strip()
            if len(task_instruction) > MAX_CASE_INSTRUCTION_CHARS:
                raise ValueError("任务指令超过2000字符上限")
            inferred["task_instruction"] = task_instruction or None
            inferred["task_instruction_sha256"] = (
                hashlib.sha256(task_instruction.encode("utf-8")).hexdigest()
                if task_instruction
                else None
            )
            inferred.update(
                {
                    "profile": inferred.get("profile") or UNIVERSAL_PROFILE_ID,
                    "target_instrument": inferred.get("entity_query") or "",
                    "permitted_usage_scope": "EVALUATION_ONLY",
                    "rights_review_required": str(inferred.get("confidentiality_class", "PUBLIC")).upper() != "PUBLIC",
                    "inspection_id": inspection_id,
                    "inspection_sha256": canonical_sha256(inspection),
                    "intake_skill_invocations": inspection.get("skill_invocations") or [],
                }
            )
            submission = self.create_submission(filename, body, inferred)
            inspection["status"] = "COMMITTED"
            inspection["committed_at"] = utc_now()
            inspection["committed_submission_id"] = submission["submission_id"]
            _json_atomic(
                self.inspections / Path(inspection_id).name / "inspection.json", inspection
            )
            return submission

    def create_submission(
        self, filename: str, body: bytes, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        if not body:
            raise ValueError("证据文件为空")
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError("证据文件超过 10MB POC 上限")
        safe_name = _safe_name(filename)
        requested_profile = str(metadata.get("profile", "auto")).strip() or "auto"
        if requested_profile not in {"auto", *LIVE_PROFILE_IDS, UNIVERSAL_PROFILE_ID}:
            raise ValueError(
                "未知Profile；请选择auto、futures_settlement、"
                "equity_corporate_action、fund_nav_admission或universal_financial_evidence"
            )
        declared_source = str(metadata.get("declared_source", "")).strip()
        rights_basis = str(metadata.get("rights_basis", "")).strip()
        declared_purpose = str(metadata.get("declared_purpose", "evidence_review")).strip()
        task_instruction = str(metadata.get("task_instruction", "") or "").strip()
        if len(task_instruction) > MAX_CASE_INSTRUCTION_CHARS:
            raise ValueError("任务指令超过2000字符上限")
        if not declared_source or not rights_basis:
            raise ValueError("必须声明数据来源和使用依据")

        suffix = Path(safe_name).suffix.lower()
        zip_manifest: list[dict[str, Any]] = []
        parsed: dict[str, Any] = {}
        if suffix == ".zip":
            zip_manifest = self._validate_zip(body)
            parsed = self._parse_generic_content(body, suffix, zip_manifest)
        elif suffix == ".csv":
            target = str(metadata.get("target_instrument", ""))
            profile_parsers = {
                FUTURES_PROFILE_ID: self._parse_futures_csv,
                EQUITY_PROFILE_ID: self._parse_equity_csv,
                FUND_PROFILE_ID: self._parse_fund_csv,
            }
            if requested_profile in profile_parsers:
                parsed = profile_parsers[requested_profile](body, target)
            elif requested_profile == "auto":
                detected_profile = None
                for candidate_profile in LIVE_PROFILE_IDS:
                    try:
                        parsed = profile_parsers[candidate_profile](body, target)
                        detected_profile = candidate_profile
                        break
                    except ValueError:
                        continue
                if detected_profile:
                    requested_profile = detected_profile
                else:
                    parsed = self._parse_generic_content(body, suffix, zip_manifest)
            else:
                parsed = self._parse_generic_content(body, suffix, zip_manifest)
        else:
            parsed = self._parse_generic_content(body, suffix, zip_manifest)

        profile = requested_profile
        if requested_profile == "auto":
            profile = (
                requested_profile
                if requested_profile in LIVE_PROFILE_IDS
                else UNIVERSAL_PROFILE_ID
            )
        if profile in LIVE_PROFILE_IDS:
            spec = LIVE_PROFILE_SPECS[profile]
            declared_purpose = declared_purpose or str(spec["default_purpose"])
            if declared_purpose == "evidence_review":
                declared_purpose = str(spec["default_purpose"])
            if profile == FUTURES_PROFILE_ID:
                try:
                    multiplier = float(metadata.get("contract_multiplier", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("合约乘数必须为数值") from exc
                if multiplier <= 0:
                    raise ValueError("期货结算Profile必须提供大于0的合约乘数")
                mapping = str(metadata.get("candidate_mapping", "auto_agent")).strip().lower()
                if mapping not in {"auto_agent", "close", "settle"}:
                    raise ValueError("candidate_mapping 只能由Agent发现，或显式声明 close / settle")
            elif profile == EQUITY_PROFILE_ID:
                multiplier = None
                mapping = str(
                    metadata.get("candidate_mapping")
                    or parsed.get("declared_adjustment")
                    or "auto_agent"
                ).strip().lower() or None
            else:
                multiplier = None
                mapping = str(
                    metadata.get("candidate_mapping")
                    or parsed.get("candidate_nav_field")
                    or "auto_agent"
                ).strip().lower() or None
                if mapping and mapping not in {"auto_agent", "unit_nav", "cumulative_nav"}:
                    raise ValueError("基金 candidate_mapping 只能由Agent发现，或显式声明净值字段")
            missing_evidence = []
            if not parsed.get("instrument"):
                missing_evidence.append("instrument")
            date_field = str(spec["date_field"])
            if not parsed.get(date_field):
                missing_evidence.append(date_field)
            required_field = (
                "cumulative_nav"
                if profile == FUND_PROFILE_ID
                and declared_purpose == "total_return_analysis"
                else str(spec["required_field"])
            )
            if profile == EQUITY_PROFILE_ID and not mapping:
                missing_evidence.append("declared_adjustment")
            if profile == FUND_PROFILE_ID and parsed.get(required_field) is None:
                missing_evidence.append(required_field)
            execution_readiness = (
                "WAIT_FOR_EVIDENCE" if missing_evidence else "AGENTTEAMS_EXECUTABLE"
            )
        else:
            multiplier = None
            mapping = str(metadata.get("candidate_mapping", "")).strip().lower() or None
            missing_evidence = ["registered_financial_profile", "declared_purpose", "required_source_fields"]
            execution_readiness = "WAIT_FOR_PROFILE"

        sha256 = hashlib.sha256(body).hexdigest()
        now = utc_now()
        metadata_hash = canonical_sha256(metadata)
        task_instruction_sha256 = (
            hashlib.sha256(task_instruction.encode("utf-8")).hexdigest()
            if task_instruction
            else None
        )
        case_input = {
            "protocol": "FINFLUX_CASE_INPUT_V0.2",
            "task_instruction": task_instruction or None,
            "task_instruction_sha256": task_instruction_sha256,
            "evidence_sha256": sha256,
            "input_mode": str(metadata.get("input_mode", "FILE")).strip().upper()
            or "FILE",
            "source_submission_id": str(
                metadata.get("source_submission_id", "") or ""
            ).strip()
            or None,
        }
        case_input["case_input_sha256"] = canonical_sha256(case_input)
        submission_id = (
            f"SUB-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-"
            f"{sha256[:8]}-{metadata_hash[:8]}-{secrets.token_hex(2)}"
        )
        object_dir = self.objects / sha256
        object_path = object_dir / safe_name
        with self.lock:
            object_dir.mkdir(parents=True, exist_ok=True)
            if not object_path.exists():
                object_path.write_bytes(body)
            record = {
                "protocol": "FINFLUX_EVIDENCE_BUNDLE_V0.1",
                "submission_id": submission_id,
                "evidence_bundle_id": f"EB-{sha256[:16].upper()}",
                "profile": profile,
                "execution_readiness": execution_readiness,
                "status": "VERIFIED",
                "created_at": now,
                "file": {
                    "name": safe_name,
                    "size_bytes": len(body),
                    "sha256": sha256,
                    "immutable_object": str(object_path.relative_to(self.root)).replace("\\", "/"),
                },
                "zip_manifest": zip_manifest,
                "metadata": {
                    "declared_source": declared_source,
                    "rights_basis": rights_basis,
                    "declared_purpose": declared_purpose,
                    "provider": str(metadata.get("provider", "现场上传")).strip() or "现场上传",
                    "candidate_mapping": mapping,
                    "target_instrument": str(metadata.get("target_instrument", "")).strip(),
                    "contract_multiplier": multiplier,
                    "multiplier_source": str(metadata.get("multiplier_source", "用户随包声明")).strip(),
                    "notes": str(metadata.get("notes", "")).strip(),
                    "review_mode": str(
                        metadata.get("review_mode", "STANDARD")
                    ).strip()
                    or "STANDARD",
                    "parent_run_id": str(metadata.get("parent_run_id", "")).strip()
                    or None,
                    "remediation_action": str(
                        metadata.get("remediation_action", "")
                    ).strip()
                    or None,
                    "asset_class": str(
                        metadata.get("asset_class")
                        or (LIVE_PROFILE_SPECS.get(profile) or {}).get("asset_class")
                        or "unknown"
                    ).strip() or "unknown",
                    "entity_query": str(metadata.get("entity_query", "")).strip() or None,
                    "content_type": str(metadata.get("content_type", "")).strip() or None,
                    "research_item_ids": list(metadata.get("research_item_ids", []) or []),
                    "captured_at": str(metadata.get("captured_at", "")).strip() or None,
                    "inspection_id": str(metadata.get("inspection_id", "")).strip() or None,
                    "inspection_sha256": str(metadata.get("inspection_sha256", "")).strip() or None,
                    "intake_skill_invocations": list(
                        metadata.get("intake_skill_invocations", []) or []
                    ),
                    "confidentiality_class": str(
                        metadata.get("confidentiality_class", "PUBLIC")
                    ).strip().upper()
                    or "PUBLIC",
                    "permitted_usage_scope": str(
                        metadata.get("permitted_usage_scope", "EVALUATION_ONLY")
                    ).strip().upper()
                    or "EVALUATION_ONLY",
                    "rights_review_required": bool(
                        metadata.get("rights_review_required", False)
                    ),
                    "research_context_required": bool(
                        metadata.get("research_context_required", False)
                    ),
                    "operational_risk_review_required": bool(
                        metadata.get("operational_risk_review_required", False)
                    ),
                    "missing_evidence_fields": list(dict.fromkeys(missing_evidence)),
                    "task_instruction": task_instruction or None,
                    "task_instruction_sha256": task_instruction_sha256,
                    "input_mode": case_input["input_mode"],
                    "source_submission_id": case_input["source_submission_id"],
                },
                "case_input": case_input,
                "parsed": parsed,
                "rights_gate": {
                    "status": "PASS",
                    "basis": rights_basis,
                    "note": "POC 仅记录声明与证据；不替代机构法务授权审查。",
                },
                "evidence_root_hash": canonical_sha256(
                    {"file_sha256": sha256, "metadata": metadata, "profile": profile}
                ),
                "raw_evidence_mutated": False,
                "adapter_note": (
                    "已匹配受控金融语义契约，可创建受控Run"
                    if execution_readiness == "AGENTTEAMS_EXECUTABLE"
                    else "当前结论为WAIT：原始证据已固化，但缺少已登记Profile或必要源字段；系统不会猜测金融结论"
                ),
            }
            submission_path = self.submissions / f"{submission_id}.json"
            _json_atomic(submission_path, record)
            self._invalidate_json_cache(submission_path)
            index = self._index()
            index["latest_submission_id"] = submission_id
            self._save_index(index)
        return record

    def derive_submission_with_instruction(
        self,
        source_submission_id: str,
        task_instruction: str,
    ) -> dict[str, Any]:
        """Bind a user task to existing immutable evidence without rewriting it."""
        clean_instruction = str(task_instruction or "").strip()
        if not clean_instruction:
            raise ValueError("请说明希望AgentTeams核验什么")
        if len(clean_instruction) > MAX_CASE_INSTRUCTION_CHARS:
            raise ValueError("任务指令超过2000字符上限")
        source = self.get_submission(source_submission_id)
        object_rel = str((source.get("file") or {}).get("immutable_object", ""))
        object_path = (self.root / object_rel).resolve()
        object_path.relative_to(self.root.resolve())
        body = object_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != str(source["file"]["sha256"]):
            raise ValueError("原始证据哈希已改变，拒绝创建CaseEnvelope")
        metadata = dict(source.get("metadata") or {})
        metadata.update(
            {
                "task_instruction": clean_instruction,
                "task_instruction_sha256": hashlib.sha256(
                    clean_instruction.encode("utf-8")
                ).hexdigest(),
                "input_mode": "EXISTING_EVIDENCE_PLUS_INTENT",
                "source_submission_id": source_submission_id,
                "notes": (
                    f"任务指令绑定到不可变证据 {source_submission_id}；"
                    "未改写原始金融数据。"
                ),
            }
        )
        return self.create_submission(str(source["file"]["name"]), body, metadata)

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"SUB-[0-9A-Za-z-]+", submission_id):
            raise ValueError("invalid submission_id")
        record = self._cached_json_read(self.submissions / f"{submission_id}.json")
        if not record:
            raise FileNotFoundError(submission_id)
        return record

    @staticmethod
    def _change_skill_receipt(
        skill_id: str,
        version: str,
        input_payload: Any,
        output_payload: Any,
    ) -> dict[str, Any]:
        definition = next(
            (item for item in CHANGE_CONTROL_SKILLS if item[0] == skill_id), None
        )
        if not definition or definition[1] != version:
            raise ValueError(f"unregistered change-control skill: {skill_id}@{version}")
        digest = hashlib.sha256(
            f"{definition[0]}@{definition[1]}|{definition[2]}".encode("utf-8")
        ).hexdigest()
        return {
            "skill_id": skill_id,
            "version": version,
            "digest": digest,
            "discovered_at_runtime": True,
            "executor": "SERVER_DETERMINISTIC_NO_MODEL",
            "input_sha256": canonical_sha256(input_payload),
            "output_sha256": canonical_sha256(output_payload),
            "provider_tokens": 0,
            "executed_at_utc": utc_now(),
        }

    def create_change_bundle(
        self,
        baseline_submission_id: str,
        candidate_submission_id: str,
        downstream_tasks: list[dict[str, Any]],
        remediation_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a content-addressed, observed-only change and blast-radius bundle.

        The two source submissions remain immutable.  Downstream impact is derived
        solely from caller-declared task manifests.  This endpoint never approves
        a financial value or a production release.
        """
        baseline = self.get_submission(baseline_submission_id)
        candidate = self.get_submission(candidate_submission_id)
        change_input = {
            "baseline_submission": baseline,
            "candidate_submission": candidate,
        }
        change_set = detect_version_change(baseline, candidate)
        impact_input = {
            "change_set": change_set,
            "downstream_tasks": downstream_tasks,
        }
        impact_graph = resolve_downstream_lineage(change_set, downstream_tasks)
        receipts = [
            self._change_skill_receipt(
                "detect-version-change", "1.0.0", change_input, change_set
            ),
            self._change_skill_receipt(
                "resolve-downstream-lineage", "1.0.0", impact_input, impact_graph
            ),
        ]
        remediation_validation = None
        if remediation_plan is not None:
            remediation_input = {
                "baseline_submission": baseline,
                "remediation_submission": candidate,
                "impact_graph": impact_graph,
                "plan": remediation_plan,
            }
            remediation_validation = validate_remediation_plan(
                baseline, candidate, impact_graph, remediation_plan
            )
            receipts.append(
                self._change_skill_receipt(
                    "validate-remediation-plan",
                    "1.0.0",
                    remediation_input,
                    remediation_validation,
                )
            )

        payload = {
            "protocol": "FINFLUX_CHANGE_BUNDLE_V1.0",
            "created_at_utc": utc_now(),
            "baseline_submission_id": baseline_submission_id,
            "candidate_submission_id": candidate_submission_id,
            "change_set": change_set,
            "impact_graph": impact_graph,
            "remediation_validation": remediation_validation,
            "skill_invocations": receipts,
            "state": (
                "READY_FOR_HUMAN_REVIEW"
                if remediation_validation
                and remediation_validation.get("status") == "VALIDATED_FOR_REVIEW"
                else "IMPACT_REVIEW_REQUIRED"
            ),
            "production_approved": False,
            "human_gate_required": True,
            "truth_boundary": (
                "变化来自两份不可变真实提交；影响仅来自显式任务依赖。"
                "Skill不补值、不决定金融真值、不拥有生产签署权。"
            ),
        }
        payload["bundle_sha256"] = change_control_sha256(payload)
        payload["change_bundle_id"] = f"CB-{payload['bundle_sha256'][:20].upper()}"
        with self.lock:
            _json_atomic(
                self.change_bundles / f"{payload['change_bundle_id']}.json", payload
            )
            index = self._index()
            index["latest_change_bundle_id"] = payload["change_bundle_id"]
            self._save_index(index)
        return payload

    def get_change_bundle(self, change_bundle_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"CB-[0-9A-F]{20}", change_bundle_id):
            raise ValueError("invalid change_bundle_id")
        record = _json_read(self.change_bundles / f"{change_bundle_id}.json")
        if not record:
            raise FileNotFoundError(change_bundle_id)
        return record

    def latest_change_bundle(self) -> dict[str, Any] | None:
        bundle_id = self._index().get("latest_change_bundle_id")
        return self.get_change_bundle(bundle_id) if bundle_id else None

    def create_change_run(
        self, change_bundle_id: str, runtime: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a Fresh Run whose Manager route is bound to a ChangeBundle."""
        bundle = self.get_change_bundle(change_bundle_id)
        candidate_id = str(bundle.get("candidate_submission_id", ""))
        candidate = self.get_submission(candidate_id)
        run = self.create_run(candidate_id, runtime)
        route_decision = build_change_root_route_decision(bundle, candidate, run)
        run["change_bundle_id"] = change_bundle_id
        run["baseline_submission_id"] = bundle.get("baseline_submission_id")
        run["root_route_decision"] = route_decision
        route = route_decision["route"]
        if run.get("protocol") == "FINFLUX_LIVE_RUN_V0.2":
            run["case_envelope"] = build_live_formal_case_envelope(
                candidate,
                run_id=run["run_id"],
                case_id=run["case_id"],
                expected_route=route,
                execution_policy_id=str(
                    run.get("execution_policy_id")
                    or "FINFLUX-BOUNDED-EXECUTION-V0.1"
                ),
                created_at_utc=run["created_at"],
            )
            run["case_envelope_sha256"] = run["case_envelope"][
                "envelope_sha256"
            ]
        run["state"] = (
            "READY_FOR_AGENTTEAMS"
            if runtime.get("connected") and route == "BLAST_RADIUS_REVIEW"
            else "RUNTIME_UNAVAILABLE"
            if route == "BLAST_RADIUS_REVIEW"
            else run["state"]
        )
        run["events"].append(
            self._event(
                len(run["events"]) + 1,
                run["run_id"],
                "Manager 变更影响路由已固化",
                (
                    f"{route} · ChangeBundle={change_bundle_id} · "
                    f"workers={route_decision['worker_plan']['count']} · "
                    f"decision={route_decision['decision_id']}"
                ),
                "READY" if run["state"] == "READY_FOR_AGENTTEAMS" else "BLOCKED",
            )
        )
        record_transition(
            run,
            str(run["state"]),
            actor="global-manager",
            reason=f"ChangeBundle {change_bundle_id} routed as {route}",
        )
        with self.lock:
            self._persist_run(run)
        return run

    @staticmethod
    def _event(
        number: int, run_id: str, title: str, summary: str, status: str = "DONE"
    ) -> dict[str, Any]:
        return {
            "event_id": f"EVT-{run_id[-10:]}-{number:03d}",
            "round": 1,
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "lane": "gateway",
            "title": title,
            "summary": summary,
            "status": status,
            "duration": "<1s",
            "tokens": 0,
            "token_ledger": "SERVER_DETERMINISTIC_NO_MODEL",
            "tool": "finflux.live_intake",
            "timeout_s": 10,
            "retry_policy": "none",
            "retry_actual": 0,
            "input_hash": "",
            "output_hash": "",
            "task_id": f"TASK-{run_id[-10:]}-PRECHECK",
        }

    def create_run(
        self,
        submission_id: str,
        runtime: dict[str, Any],
        client_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create one Run, or replay the durable result for the same client key.

        Legacy callers that omit a key retain the historical fresh-Run behavior.
        The acceptance path supplies a key persisted before the HTTP request.
        """
        if client_idempotency_key is None:
            return self._create_run_materialized(submission_id, runtime)
        binding = self._run_creation_binding(
            submission_id, client_idempotency_key
        )
        with self.lock:
            attempt = self._ensure_run_creation_attempt(submission_id, binding)
            commit = self._read_run_creation_commit(binding, attempt)
            matches = self._runs_for_creation_binding(binding)
            if commit is not None:
                run = self.get_run(str(commit.get("run_id") or ""))
                if not matches or matches[0].get("run_id") != run.get("run_id"):
                    raise ValueError(
                        "run creation commit does not match durable Run evidence"
                    )
                return self._run_creation_response(run, replayed=True)
            if matches:
                run = matches[0]
                self._write_run_creation_commit(
                    binding, attempt, str(run["run_id"])
                )
                return self._run_creation_response(run, replayed=True)
            receipt = {
                **binding,
                "attempt_sha256": attempt["attempt_sha256"],
            }
            receipt["receipt_sha256"] = canonical_sha256(receipt)
            run = self._create_run_materialized(
                submission_id,
                runtime,
                run_creation_idempotency=receipt,
            )
            self._write_run_creation_commit(binding, attempt, str(run["run_id"]))
            return self._run_creation_response(run, replayed=False)

    def _create_run_materialized(
        self,
        submission_id: str,
        runtime: dict[str, Any],
        *,
        run_creation_idempotency: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        submission = self.get_submission(submission_id)
        if submission.get("execution_readiness") not in {
            "AGENTTEAMS_EXECUTABLE", "WAIT_FOR_EVIDENCE"
        }:
            raise ValueError(
                "该EvidenceBundle已完成接入与固化，但尚未匹配已登记金融Profile；"
                "系统返回WAIT并列出补充项，不允许模型猜测金融结论"
            )
        parsed = submission.get("parsed") or {}
        metadata = submission["metadata"]
        profile_id = str(submission.get("profile") or "")
        spec = LIVE_PROFILE_SPECS.get(profile_id)
        if not spec:
            raise ValueError("Live Run只接受期货、股票和基金三个已登记Profile")
        purpose = str(metadata.get("declared_purpose") or spec["default_purpose"])
        selected = str(metadata.get("candidate_mapping") or "").lower() or None
        missing = list(metadata.get("missing_evidence_fields") or [])
        precheck: dict[str, Any]
        if selected == "auto_agent":
            available_fields = list(parsed.get("columns") or []) or [
                key for key, value in parsed.items()
                if isinstance(value, (str, int, float)) and value not in (None, "")
            ]
            precheck = {
                "contract": str(spec["contract"]),
                "required_field": None,
                "candidate_mapping": "AUTO_AGENT",
                "available_fields": available_fields,
                "semantic_resolution_mode": "AGENT_PROPOSES_SKILL_VERIFIES_HUMAN_DECIDES",
                "impact_status": "PENDING_AGENT_PROPOSAL",
                "machine_recommendation": "NEEDS_EVIDENCE",
                "calculation_formula": None,
                "generated_by_model": False,
            }
        elif profile_id == FUTURES_PROFILE_ID:
            if not all(key in parsed for key in ("close", "settle")):
                raise ValueError("futures_settlement Run需要可解析的close与settle字段")
            close = float(parsed["close"])
            settle = float(parsed["settle"])
            multiplier = float(metadata["contract_multiplier"])
            selected_value = close if selected == "close" else settle
            impact = abs(settle - selected_value) * multiplier
            recommendation = "PASS" if selected == "settle" else "BLOCK"
            precheck = {
                "contract": str(spec["contract"]),
                "required_field": "settle",
                "candidate_mapping": selected,
                "close": close,
                "settle": settle,
                "contract_multiplier": multiplier,
                "impact_status": "COMPUTED",
                "impact_cny_per_contract": round(impact, 6),
                "machine_recommendation": recommendation,
                "calculation_formula": "abs(settle - selected_value) * contract_multiplier",
                "generated_by_model": False,
            }
        elif profile_id == EQUITY_PROFILE_ID:
            required_field = "qfq"
            recommendation = (
                "NEEDS_EVIDENCE" if not selected or missing
                else "PASS" if selected == required_field
                else "BLOCK"
            )
            observed_difference = parsed.get("return_difference")
            precheck = {
                "contract": str(spec["contract"]),
                "required_field": required_field,
                "candidate_mapping": selected,
                "event_date": parsed.get("event_date"),
                "declared_adjustment": parsed.get("declared_adjustment"),
                "observed_adjustment": parsed.get("observed_adjustment"),
                "impact_status": "COMPUTED" if observed_difference is not None else "NOT_AVAILABLE",
                "return_difference_pct_points": observed_difference,
                "machine_recommendation": recommendation,
                "calculation_formula": (
                    "source-provided deterministic return difference"
                    if observed_difference is not None else None
                ),
                "missing_evidence_fields": missing,
                "generated_by_model": False,
            }
        else:
            required_field = (
                "cumulative_nav" if purpose == "total_return_analysis" else "unit_nav"
            )
            candidate_value = parsed.get(selected) if selected in {"unit_nav", "cumulative_nav"} else None
            required_value = parsed.get(required_field)
            if not selected or required_value is None or missing:
                recommendation = "NEEDS_EVIDENCE"
            elif selected == required_field:
                recommendation = "PASS"
            elif candidate_value is None:
                recommendation = "NEEDS_EVIDENCE"
                missing.append(str(selected))
            else:
                recommendation = "BLOCK"
            impact = (
                round(abs(float(required_value) - float(candidate_value)) * 10000, 6)
                if required_value is not None and candidate_value is not None
                else None
            )
            precheck = {
                "contract": str(spec["contract"]),
                "required_field": required_field,
                "candidate_mapping": selected,
                "unit_nav": parsed.get("unit_nav"),
                "cumulative_nav": parsed.get("cumulative_nav"),
                "impact_status": "COMPUTED" if impact is not None else "NOT_AVAILABLE",
                "impact_cny_per_10000_units": impact,
                "machine_recommendation": recommendation,
                "calculation_formula": (
                    "abs(required_nav - selected_nav) * 10000" if impact is not None else None
                ),
                "missing_evidence_fields": list(dict.fromkeys(missing)),
                "generated_by_model": False,
            }
        run_id = f"RUN-LIVE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        trace_id = f"TRACE-{secrets.token_hex(8)}"
        remediation_suffix = (
            "-REMEDIATION"
            if metadata.get("review_mode") == "POST_REMEDIATION_REVIEW"
            else ""
        )
        date_value = parsed.get(str(spec["date_field"])) or "UNKNOWN"
        case_id = (
            f"{str(spec['asset_class']).upper()}-{parsed.get('instrument', 'UNKNOWN')}-"
            f"{date_value}-{purpose.upper()}{remediation_suffix}"
        )
        precheck["sha256"] = canonical_sha256(precheck)
        connected = bool(runtime.get("connected"))
        events = [
            self._event(1, run_id, "EvidenceBundle 已受理", "读取不可变对象并校验服务端 SHA256"),
            self._event(2, run_id, "Rights Gate 已记录", "来源与使用依据随提交固化；不改写原始金融数据"),
            self._event(
                3, run_id,
                "金融语义发现已编排" if selected == "auto_agent" else "金融语义契约已执行",
                (
                    f"Profile提示={profile_id}；用途={purpose}；待两个Agent独立提出语义候选，Skill随后验真"
                    if selected == "auto_agent"
                    else f"Profile={profile_id}；用途={purpose}；契约字段={precheck.get('required_field')}；候选映射={selected or 'MISSING'}"
                ),
            ),
            self._event(
                4, run_id,
                "结构与权属预检完成" if selected == "auto_agent" else "确定性预检完成",
                (
                    "原始字节、Schema、来源和权属已固化；未预写金融语义答案；模型 Token=0"
                    if selected == "auto_agent"
                    else f"建议={recommendation}；影响状态={precheck.get('impact_status')}；模型 Token=0"
                ),
            ),
        ]
        run = {
            "protocol": "FINFLUX_LIVE_RUN_V0.2",
            "run_id": run_id,
            "trace_id": trace_id,
            "case_id": case_id,
            "submission_id": submission_id,
            "created_at": utc_now(),
            "state": "ROUTING",
            "runtime_connected": connected,
            "runtime_truthful_note": runtime.get("truthful_note", ""),
            "precheck": precheck,
            "events": events,
            "agentteams_run_id": None,
            "datapass": None,
            "human_gate": {
                "gate_id": f"HG-{run_id[-10:]}",
                "state": "NOT_OPENED",
                "required_role": "FinChange Data Owner",
                "decision": None,
            },
            "budget": {
                "tokens": {
                    "observed": 0,
                    "reported": 0,
                    "prompt": 0,
                    "completion": 0,
                    "call_count": 0,
                    "budget": None,
                    "percent": None,
                    "status": "NO_MODEL_CALLS",
                    "source": "SERVER_DETERMINISTIC_NO_MODEL",
                },
                "message_proxy": {
                    "observed": 0,
                    "limit": 10000,
                    "percent": 0,
                    "source": "MATRIX_MESSAGE_CHARACTER_PROXY",
                },
                "events": {"used": len(events), "budget": 30},
                "wallclock": {
                    "used_s": 0,
                    "budget_s": int(
                        runtime.get("bounded_execution", {})
                        .get("limits", {})
                        .get("max_wall_time_seconds", 600)
                    ),
                },
                "note": "当前仅完成确定性准入预检；未调用模型，真实 Token 消耗为 0。",
            },
        }
        if run_creation_idempotency is not None:
            run["run_creation_idempotency"] = copy.deepcopy(
                run_creation_idempotency
            )
        route_decision = build_live_root_route_decision(submission, run)
        run["root_route_decision"] = route_decision
        route = route_decision["route"]
        execution_policy_id = str(
            (runtime.get("bounded_execution") or {}).get("policy_id")
            or "FINFLUX-BOUNDED-EXECUTION-V0.1"
        )
        formal_envelope = build_live_formal_case_envelope(
            submission,
            run_id=run_id,
            case_id=case_id,
            expected_route=route,
            execution_policy_id=execution_policy_id,
            created_at_utc=run["created_at"],
        )
        run["case_envelope"] = formal_envelope
        run["case_envelope_sha256"] = formal_envelope["envelope_sha256"]
        run["execution_policy_id"] = execution_policy_id
        run["operational_memory_plan"] = self._resolve_operational_memory_plan(
            run_id=run_id,
            submission=submission,
            route_decision=route_decision,
            execution_policy_id=execution_policy_id,
        )
        if route == "FULL_TEAM_REVIEW":
            run["state"] = "READY_FOR_AGENTTEAMS" if connected else "RUNTIME_UNAVAILABLE"
            route_note = (
                "语义冲突需要三名Worker独立核验；Runtime已连接"
                if connected
                else "语义冲突需要三名Worker，但Runtime未连接；不伪造结果"
            )
        elif route == "CODE_ONLY_PRECHECK":
            run["state"] = "CODE_ONLY_PRECHECK"
            route_note = "证据、用途与字段映射一致；确定性Skill完成准入，不消耗模型Token"
        else:
            run["state"] = route
            route_note = "根路由门禁拒绝进入完整AgentTeams协作"
        run["events"].append(
            self._event(
                5,
                run_id,
                "Manager RootRouteDecision 已固化",
                f"{route} · {route_note} · decision={route_decision['decision_id']}",
                "READY" if route in {"FULL_TEAM_REVIEW", "CODE_ONLY_PRECHECK"} else "BLOCKED",
            )
        )
        bootstrap_lifecycle(run)
        with self.lock:
            self._persist_run(run)
            index = self._index()
            index["latest_run_id"] = run_id
            if not index.get("judge_run_id") or index.get("selection_mode") == "MANUAL":
                index["selected_run_id"] = run_id
            self._save_index(index)
        return run

    def create_remediation_child(
        self,
        parent_run_id: str,
        runtime: dict[str, Any],
        remediation_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a child Run over the same immutable bytes with corrected metadata.

        The original financial file is never overwritten.  The child is routed
        through a full AgentTeams review because a Human-selected remediation is
        a high-responsibility semantic change even when deterministic precheck
        now passes.
        """
        with self.lock:
            parent = self.get_run(parent_run_id)
            existing_child = (parent.get("lineage") or {}).get("child_run_id")
            if existing_child:
                return self.get_run(str(existing_child))
            if (parent.get("human_gate") or {}).get("state") != "REJECTED":
                raise ValueError("只有已确认隔离的父Run才能创建整改子Run")
            submission = self.get_submission(parent["submission_id"])
            semantic_artifact = (
                ((parent.get("agent_result") or {}).get("worker_artifacts") or {})
                .get("semantic-impact-analyst")
                or {}
            )
            verified_contract = semantic_artifact.get("contract") or {}
            verified_target = str(
                verified_contract.get("required_field")
                or (parent.get("precheck") or {}).get("required_field")
                or ""
            ).strip()
            plan = dict(remediation_plan or {})
            requested_target = str(
                plan.get("target_field") or verified_target
            ).strip()
            if not verified_target:
                raise ValueError("父Run没有经Skill验证的修订目标，必须先补证")
            if requested_target != verified_target:
                raise ValueError(
                    "Human修订目标与本Run经Skill验证的目标不一致；拒绝绕过专业核验"
                )
            available_fields = {
                str(item)
                for item in ((parent.get("precheck") or {}).get("available_fields") or [])
            }
            if not available_fields:
                columns = (submission.get("parsed") or {}).get("columns") or []
                available_fields = {str(item) for item in columns}
            if available_fields and requested_target not in available_fields:
                raise ValueError("修订目标字段不在已封存的原始Schema中")
            approved_plan = {
                "protocol": "FINFLUX_HUMAN_APPROVED_REMEDIATION_V1",
                "parent_run_id": parent_run_id,
                "from_field": str(
                    plan.get("from_field")
                    or verified_contract.get("candidate_mapping")
                    or (parent.get("precheck") or {}).get("candidate_mapping")
                    or ""
                ),
                "target_field": requested_target,
                "target_semantic": plan.get("target_semantic"),
                "proposal_source": plan.get("proposal_source")
                or (
                    "AGENT_MODEL_PROPOSAL_VERIFIED_BY_RUNTIME_SKILL"
                    if semantic_artifact.get("agent_semantic_proposal")
                    else "DETERMINISTIC_CONTRACT_PRECHECK"
                ),
                "reason_code": plan.get("reason_code")
                or verified_contract.get("reason_code")
                or "SEMANTIC_MAPPING_CONFLICT",
                "human_actor_id": (parent.get("human_gate") or {}).get(
                    "human_actor_id"
                ),
                "human_decision": (parent.get("human_gate") or {}).get("decision"),
                "raw_evidence_mutated": False,
                "requires_fresh_agentteams_review": True,
            }
            approved_plan["plan_sha256"] = canonical_sha256(approved_plan)
            object_rel = str((submission.get("file") or {}).get("immutable_object", ""))
            object_path = (self.root / object_rel).resolve()
            object_path.relative_to(self.root.resolve())
            if not object_path.is_file():
                raise FileNotFoundError("父Run原始证据对象不存在")
            metadata = dict(submission.get("metadata") or {})
            metadata.update(
                {
                    "candidate_mapping": requested_target,
                    "review_mode": "POST_REMEDIATION_REVIEW",
                    "parent_run_id": parent_run_id,
                    "remediation_action": "ADOPT_AGENT_PROPOSED_VERIFIED_SEMANTIC",
                    "remediation_plan_sha256": approved_plan["plan_sha256"],
                    "notes": (
                        f"由Human Gate对父Run {parent_run_id} 选择整改；"
                        f"采用经Skill验证的语义候选 {requested_target}；"
                        "仅修订准入元数据，不改写原始金融数据。"
                    ),
                }
            )
            revised_submission = self.create_submission(
                str((submission.get("file") or {}).get("name", "evidence.csv")),
                object_path.read_bytes(),
                metadata,
            )
            child = self.create_run(revised_submission["submission_id"], runtime)
            if (child.get("root_route_decision") or {}).get("route") != "FULL_TEAM_REVIEW":
                raise ValueError("整改子Run未进入完整AgentTeams复核，拒绝继续")
            child["lineage"] = {
                "parent_run_id": parent_run_id,
                "relation": "HUMAN_SELECTED_REMEDIATION",
                "raw_evidence_sha256_unchanged": (
                    revised_submission["file"]["sha256"]
                    == submission["file"]["sha256"]
                ),
                "metadata_revision": {
                    "from_candidate_mapping": (submission.get("metadata") or {}).get(
                        "candidate_mapping"
                    ),
                    "to_candidate_mapping": requested_target,
                },
                "human_approved_remediation": approved_plan,
            }
            child["events"].append(
                self._event(
                    len(child["events"]) + 1,
                    child["run_id"],
                    "Human整改子Run已创建",
                    (
                        f"父Run {parent_run_id}；原始文件SHA256保持不变；"
                        f"采用经Skill验证并由Human批准的语义候选 {requested_target}；"
                        "进入新的AgentTeams多角色独立复核"
                    ),
                    "READY",
                )
            )
            parent["lineage"] = {
                **(parent.get("lineage") or {}),
                "child_run_id": child["run_id"],
                "relation": "REMEDIATED_BY_CHILD_RUN",
            }
            # A final disposition may have been auto-composed immediately after
            # the Matrix Human event.  The child Run is part of the final
            # lineage, so invalidate only that derived report summary and let
            # ensure_final_result create a new content-addressed version.
            parent.pop("final_result", None)
            if isinstance(parent.get("result_composer_runs"), dict):
                parent["result_composer_runs"].pop("final", None)
            self._persist_run(parent)
            self._persist_run(child)
            return child

    def ensure_final_result(self, run_id: str) -> dict[str, Any]:
        """Generate content-addressed Markdown/PDF/JSON for a signed Human outcome."""
        with self.lock:
            run = self.get_run(run_id)
            if run.get("final_result"):
                require_v02_export_ready(run, require_final_artifacts=True)
                verify_result_artifacts(
                    self.root / "reports",
                    run_id,
                    expected_payload_sha256=str(
                        run["final_result"].get("result_payload_sha256") or ""
                    ),
                )
                return run["final_result"]
            gate_state = str((run.get("human_gate") or {}).get("state", ""))
            if gate_state not in {"APPROVED", "REJECTED", "RETURNED"}:
                raise ValueError("Human Gate尚未形成最终处置，不能生成最终结果文件")
            require_v02_export_ready(run, require_final_artifacts=False)
            submission = self.get_submission(run["submission_id"])
            composer_run = ResultComposerAgent(self.root).compose(
                run, submission, stage="final"
            )
            artifacts = composer_run["artifacts"]
            run["final_result"] = {
                "protocol": artifacts["payload"]["protocol"],
                "outcome": artifacts["payload"]["outcome"],
                "plain_language_finding": artifacts["payload"][
                    "plain_language_finding"
                ],
                "result_payload_sha256": artifacts["payload"][
                    "result_payload_sha256"
                ],
                "manifest": artifacts["manifest"],
                "download_urls": artifacts["download_urls"],
                "composer": {
                    key: composer_run[key]
                    for key in (
                        "protocol",
                        "agent_id",
                        "agent_version",
                        "execution_channel",
                        "agentteams_worker_package",
                        "strategy",
                        "skill_invocations",
                        "verification",
                        "truth_boundary",
                    )
                },
            }
            run.setdefault("result_composer_runs", {})["final"] = run[
                "final_result"
            ]["composer"]
            require_v02_export_ready(run, require_final_artifacts=True)
            self._persist_run(run)
            return run["final_result"]

    def rebuild_final_result(
        self, run_id: str, *, reason: str
    ) -> dict[str, Any]:
        """Regenerate derived reports without changing evidence or authorization.

        This maintenance path is intentionally limited to already signed Runs.
        The previous content-addressed report directory is retained under
        ``reports/_superseded`` for auditability.
        """

        with self.lock:
            run = self.get_run(run_id)
            gate_state = str((run.get("human_gate") or {}).get("state", ""))
            if gate_state not in {"APPROVED", "REJECTED", "RETURNED"}:
                raise ValueError("只有已形成Human最终处置的Run才能重建派生报告")
            if not str(reason or "").strip():
                raise ValueError("重建派生报告必须记录原因")
            require_v02_export_ready(run, require_final_artifacts=False)
            submission = self.get_submission(run["submission_id"])
            previous_hash = str(
                (run.get("final_result") or {}).get("result_payload_sha256") or ""
            )
            composer_run = ResultComposerAgent(self.root).compose(
                run,
                submission,
                stage="final",
                replace_existing=True,
            )
            artifacts = composer_run["artifacts"]
            run["final_result"] = {
                "protocol": artifacts["payload"]["protocol"],
                "outcome": artifacts["payload"]["outcome"],
                "plain_language_finding": artifacts["payload"][
                    "plain_language_finding"
                ],
                "result_payload_sha256": artifacts["payload"][
                    "result_payload_sha256"
                ],
                "manifest": artifacts["manifest"],
                "download_urls": artifacts["download_urls"],
                "composer": {
                    key: composer_run[key]
                    for key in (
                        "protocol",
                        "agent_id",
                        "agent_version",
                        "execution_channel",
                        "agentteams_worker_package",
                        "strategy",
                        "skill_invocations",
                        "verification",
                        "truth_boundary",
                    )
                },
            }
            run.setdefault("result_composer_runs", {})["final"] = run[
                "final_result"
            ]["composer"]
            rebuild = {
                "rebuilt_at": utc_now(),
                "reason": str(reason).strip(),
                "previous_result_payload_sha256": previous_hash or None,
                "new_result_payload_sha256": run["final_result"][
                    "result_payload_sha256"
                ],
                "evidence_mutated": False,
                "datapass_mutated": False,
                "human_decision_mutated": False,
            }
            rebuild["receipt_sha256"] = canonical_sha256(rebuild)
            run.setdefault("derived_report_rebuilds", []).append(rebuild)
            run.setdefault("events", []).append(
                self._event(
                    len(run.get("events") or []) + 1,
                    run_id,
                    "最终报告派生投影已重建",
                    (
                        f"原因：{reason}；旧结果 {previous_hash or 'NONE'}；"
                        f"新结果 {run['final_result']['result_payload_sha256']}；"
                        "原始证据、DataPass与Human决定均未变更"
                    ),
                    "VERIFIED",
                )
            )
            require_v02_export_ready(run, require_final_artifacts=True)
            self._persist_run(run)
            return {"final_result": run["final_result"], "rebuild_receipt": rebuild}

    def ensure_report_preview(self, run_id: str) -> dict[str, Any]:
        """Automatically generate a truthful pending report without Human signing."""
        with self.lock:
            run = self.get_run(run_id)
            if run.get("report_preview"):
                return run["report_preview"]
            gate_state = str((run.get("human_gate") or {}).get("state", ""))
            if gate_state != "AWAITING_HUMAN" or not run.get("datapass"):
                raise ValueError("只有已形成DataPass并等待Human的Run才能生成待签署报告")
            require_v02_export_ready(run, require_final_artifacts=False)
            submission = self.get_submission(run["submission_id"])
            composer_run = ResultComposerAgent(self.root).compose(
                run, submission, stage="preview"
            )
            artifacts = composer_run["artifacts"]
            composer = {
                key: composer_run[key]
                for key in (
                    "protocol",
                    "agent_id",
                    "agent_version",
                    "execution_channel",
                    "agentteams_worker_package",
                    "strategy",
                    "skill_invocations",
                    "verification",
                    "truth_boundary",
                )
            }
            run["report_preview"] = {
                "protocol": artifacts["payload"]["protocol"],
                "outcome": artifacts["payload"]["outcome"],
                "plain_language_finding": artifacts["payload"][
                    "plain_language_finding"
                ],
                "result_payload_sha256": artifacts["payload"][
                    "result_payload_sha256"
                ],
                "manifest": artifacts["manifest"],
                "download_urls": artifacts["download_urls"],
                "composer": composer,
            }
            run.setdefault("result_composer_runs", {})["preview"] = composer
            self._persist_run(run)
            return run["report_preview"]

    def result_artifact_path(self, run_id: str, kind: str) -> Path:
        if kind not in {"markdown", "pdf", "json"}:
            raise ValueError("unsupported result artifact")
        run = self.get_run(run_id)
        final_result = run.get("final_result") or self.ensure_final_result(run_id)
        verify_result_artifacts(
            self.root / "reports",
            run_id,
            expected_payload_sha256=str(
                final_result.get("result_payload_sha256") or ""
            ),
        )
        file_name = str(final_result["manifest"]["files"][kind]["name"])
        path = (self.root / "reports" / run_id / file_name).resolve()
        path.relative_to((self.root / "reports").resolve())
        if not path.is_file():
            raise FileNotFoundError(file_name)
        return path

    def preview_artifact_path(self, run_id: str, kind: str) -> Path:
        if kind not in {"markdown", "pdf", "json"}:
            raise ValueError("unsupported preview artifact")
        run = self.get_run(run_id)
        preview = run.get("report_preview") or self.ensure_report_preview(run_id)
        file_name = str(preview["manifest"]["files"][kind]["name"])
        path = (self.root / "previews" / run_id / file_name).resolve()
        path.relative_to((self.root / "previews").resolve())
        if not path.is_file():
            raise FileNotFoundError(file_name)
        return path

    def get_run(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"RUN-[0-9A-Za-z-]+", run_id):
            raise ValueError("invalid run_id")
        run_path = self.runs / f"{run_id}.json"
        run = self._cached_json_read(run_path)
        if not run:
            raise FileNotFoundError(run_id)
        # Backward-compatible derived migration for Runs created before the
        # lifecycle protocol was introduced.  It never changes financial
        # evidence or a Human decision; it only projects already persisted
        # state into a hash-bound transition history and compact memory.
        if not isinstance(run.get("lifecycle"), dict):
            bootstrap_lifecycle(run, actor="lifecycle-migration")
            _json_atomic(run_path, run)
            self._invalidate_json_cache(run_path)
            submission = self.get_submission(str(run["submission_id"]))
            self.memory.update_run(run, submission)
        # Presentation fields are derived from durable facts. Recompute them on
        # every read so a projection fix cannot leave an older failed Run shown
        # as OPEN, while the persisted financial evidence remains untouched.
        projected = project_run_states(run)
        run["execution_state"] = projected["execution_state"]
        run["business_disposition"] = projected["business_disposition"]
        run["lifecycle_terminal"] = projected["lifecycle_terminal"]
        run["state_consistency"] = projected["consistency"]
        return run

    def attach_agentteams(self, run_id: str, agent_run: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            run = self.get_run(run_id)
            if str(agent_run.get("run_id")) != run_id:
                raise ValueError("AgentTeams Run ID与Live Run不一致")
            for field in AGENTTEAMS_ACCEPTANCE_FIELDS:
                if field in agent_run:
                    run[field] = copy.deepcopy(agent_run[field])
            run["agentteams_trace"] = copy.deepcopy(agent_run.get("trace") or [])
            run["agentteams_run_id"] = run_id
            run["state"] = "AGENTTEAMS_SUBMITTED"
            request = dict(run.get("dispatch_request") or {})
            if request:
                request.update(
                    {
                        "status": "DISPATCHED",
                        "dispatched_at_utc": utc_now(),
                        "agentteams_run_id": run_id,
                    }
                )
                run["dispatch_request"] = request
            record_transition(
                run,
                run["state"],
                actor="agentteams-adapter",
                reason="CaseEnvelope accepted and dispatched to the Team Leader",
            )
            run["agentteams"] = {
                "state": str(agent_run.get("state", "SUBMITTED")),
                "case_envelope_sha256": agent_run.get("case_envelope_sha256"),
                "manager_room_id": agent_run.get("manager_room_id"),
                "leader_room_id": agent_run.get("leader_room_id"),
                "leader_relay": agent_run.get("leader_relay"),
                "submitted_at_utc": agent_run.get("submitted_at_utc"),
            }
            run["events"].append(
                self._event(
                    len(run["events"]) + 1,
                    run_id,
                    "AgentTeams Live Case 已提交",
                    "动态 CaseEnvelope 已绑定同一 Submission/Run 并发送至真实 Matrix Leader Room",
                    "SUBMITTED",
                )
            )
            self._persist_run(run)
            return run

    def request_dispatch(
        self, run_id: str, requested_by: str = "demo.operator"
    ) -> dict[str, Any]:
        """Persist the operator's one-click intent until AgentTeams accepts it."""

        with self.lock:
            run = self.get_run(run_id)
            if run.get("agentteams_run_id"):
                return run
            now = time.time()
            superseded_run_ids: list[str] = []
            # A new, explicit operator request must not wait behind historical
            # Runs that never reached AgentTeams.  Preserve every old Run and
            # its evidence, but retire its unstarted queue intent.  Active or
            # already-dispatched Runs are deliberately outside this operation.
            for path in self.runs.glob("RUN-*.json"):
                queued = self._cached_json_read(path)
                if not isinstance(queued, dict):
                    continue
                queued_run_id = str(queued.get("run_id") or "")
                if not queued_run_id or queued_run_id == run_id:
                    continue
                if queued.get("agentteams_run_id"):
                    continue
                queued_request = dict(queued.get("dispatch_request") or {})
                if queued_request.get("status") not in {"QUEUED", "RETRY_WAIT"}:
                    continue
                if str(queued.get("state") or "") not in {
                    "READY_FOR_AGENTTEAMS",
                    "DISPATCH_GUARDED",
                    "AGENTTEAMS_DISPATCH_FAILED",
                }:
                    continue
                queued_request.update(
                    {
                        "status": "SUPERSEDED",
                        "superseded_by_run_id": run_id,
                        "superseded_at_utc": utc_now(),
                        "next_attempt_epoch": None,
                    }
                )
                queued["dispatch_request"] = queued_request
                queued.setdefault("events", []).append(
                    self._event(
                        len(queued.get("events") or []) + 1,
                        queued_run_id,
                        "旧派发请求已由最新操作替代",
                        f"未启动的排队请求由{run_id}替代；Run证据仍完整保留",
                        "SUPERSEDED",
                    )
                )
                queued["updated_at"] = utc_now()
                _json_atomic(path, queued)
                self._invalidate_json_cache(path)
                superseded_run_ids.append(queued_run_id)

            request = dict(run.get("dispatch_request") or {})
            if request.get("status") not in {"QUEUED", "RETRY_WAIT"}:
                request = {
                    "protocol": "FINFLUX_BACKGROUND_DISPATCH_REQUEST_V1",
                    "status": "QUEUED",
                    "requested_by": str(requested_by or "demo.operator"),
                    "requested_at_utc": utc_now(),
                    "requested_epoch": now,
                    "attempt_count": 0,
                    "next_attempt_epoch": now,
                    "last_error": None,
                }
                run.setdefault("events", []).append(
                    self._event(
                        len(run.get("events") or []) + 1,
                        run_id,
                        "后台派发请求已登记",
                        "浏览器可以关闭；RunSupervisor将在单活动Run门禁放行后继续同一Run派发",
                        "QUEUED",
                    )
                )
            else:
                request["status"] = "QUEUED"
                request["requested_by"] = str(requested_by or "demo.operator")
                request["requested_at_utc"] = utc_now()
                request["requested_epoch"] = now
                request["next_attempt_epoch"] = min(
                    float(request.get("next_attempt_epoch") or time.time()),
                    time.time(),
                )
            if superseded_run_ids:
                request["superseded_request_count"] = len(superseded_run_ids)
                request["superseded_run_ids_sha256"] = canonical_sha256(
                    sorted(superseded_run_ids)
                )
                run.setdefault("events", []).append(
                    self._event(
                        len(run.get("events") or []) + 1,
                        run_id,
                        "最新操作已取得派发队列优先权",
                        (
                            f"已将{len(superseded_run_ids)}个未启动旧请求标记为"
                            "SUPERSEDED；未删除任何Run或证据"
                        ),
                        "QUEUED",
                    )
                )
            run["dispatch_request"] = request
            self._persist_run(run)
            return run

    def next_dispatch_request(self) -> dict[str, Any] | None:
        """Return one durable, explicitly requested Run that is due for dispatch."""

        now = time.time()
        candidates: list[tuple[float, dict[str, Any]]] = []
        for path in self.runs.glob("RUN-*.json"):
            run = self._cached_json_read(path)
            if not isinstance(run, dict) or run.get("agentteams_run_id"):
                continue
            request = run.get("dispatch_request") or {}
            if request.get("status") not in {"QUEUED", "RETRY_WAIT"}:
                continue
            if str(run.get("state") or "") not in {
                "READY_FOR_AGENTTEAMS",
                "DISPATCH_GUARDED",
                "AGENTTEAMS_DISPATCH_FAILED",
            }:
                continue
            if float(request.get("next_attempt_epoch") or 0) > now:
                continue
            candidates.append((float(request.get("requested_epoch") or 0), run))
        if not candidates:
            return None
        # Prefer the latest explicit operator intent.  request_dispatch also
        # retires older unstarted requests; reverse ordering keeps pre-upgrade
        # backlogs from delaying the current on-site demonstration.
        candidates.sort(key=lambda item: item[0], reverse=True)
        return copy.deepcopy(candidates[0][1])

    def record_dispatch_retry(
        self,
        run_id: str,
        detail: str,
        *,
        retry_after_seconds: float = 90.0,
        max_attempts: int = 3,
        max_age_seconds: float = 600.0,
    ) -> dict[str, Any]:
        """Persist a bounded background dispatch retry or a truthful WAIT outcome."""

        with self.lock:
            run = self.get_run(run_id)
            request = dict(run.get("dispatch_request") or {})
            now = time.time()
            attempts = int(request.get("attempt_count") or 0) + 1
            age = max(0.0, now - float(request.get("requested_epoch") or now))
            request.update(
                {
                    "attempt_count": attempts,
                    "last_attempt_at_utc": utc_now(),
                    "last_error": str(detail),
                }
            )
            exhausted = attempts >= int(max_attempts) or age >= float(max_age_seconds)
            if exhausted:
                request["status"] = "WAIT"
                run["state"] = "WAIT"
                run["supervisor_outcome"] = {
                    "protocol": "FINFLUX_SUPERVISOR_WAIT_V1",
                    "decision": "WAIT",
                    "reason": str(detail),
                    "reason_codes": [
                        "BACKGROUND_DISPATCH_RETRY_EXHAUSTED",
                        "HUMAN_EVIDENCE_REQUIRED",
                    ],
                    "datapass_created": False,
                    "new_run_created": False,
                    "created_at_utc": utc_now(),
                }
                request["next_attempt_epoch"] = None
                event_status = "WAIT"
                event_title = "后台派发转为WAIT"
            else:
                request["status"] = "RETRY_WAIT"
                request["next_attempt_epoch"] = now + float(retry_after_seconds)
                run["state"] = "DISPATCH_GUARDED"
                event_status = "RETRY_WAIT"
                event_title = "后台派发将在同一Run重试"
            run["dispatch_request"] = request
            run.setdefault("events", []).append(
                self._event(
                    len(run.get("events") or []) + 1,
                    run_id,
                    event_title,
                    f"attempt={attempts}/{max_attempts} · {detail}",
                    event_status,
                )
            )
            self._persist_run(run)
            return run

    def record_dispatch_guard(
        self, run_id: str, guard: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist a denied admission without pretending an AgentTeams failure."""
        with self.lock:
            run = self.get_run(run_id)
            run["dispatch_guard"] = guard
            run["state"] = "DISPATCH_GUARDED"
            record_transition(
                run,
                run["state"],
                actor="provider-token-guard",
                reason="Provider token admission denied before model dispatch",
            )
            run["events"].append(
                self._event(
                    len(run["events"]) + 1,
                    run_id,
                    "Token 安全闸门已拒绝模型派发",
                    "；".join(str(item) for item in guard.get("reasons", []))
                    or "没有可证明的剩余预算",
                    "BLOCKED",
                )
            )
            self._persist_run(run)
            return run

    def record_emergency_stop(
        self, run_id: str, control_record: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply an immutable emergency-stop fact to the Live Run projection.

        The control record is created by the append-only control ledger before
        this method is called.  This method never creates a DataPass or Human
        decision and never communicates with Matrix or a model provider.
        """
        validate_emergency_stop_record(control_record)
        if str(control_record.get("run_id")) != run_id:
            raise ValueError("emergency-stop record Run binding mismatch")
        with self.lock:
            run = self.get_run(run_id)
            latest = run.get("emergency_stop") or {}
            same_record = (
                latest.get("record_sha256") == control_record["record_sha256"]
            )
            terminal_projection_complete = (
                str(run.get("state") or "") == str(control_record["terminal_state"])
                and (run.get("lifecycle") or {}).get("current_phase")
                == "FAILED_CLOSED"
            )
            if same_record and terminal_projection_complete:
                return run
            gate_state = str((run.get("human_gate") or {}).get("state", ""))
            if gate_state in {"APPROVED", "REJECTED", "RETURNED"}:
                raise ValueError("已由Human终结的Run不能改写为紧急停止")

            refs = list(run.get("emergency_stop_records") or [])
            if not any(
                item.get("record_sha256") == control_record["record_sha256"]
                for item in refs
                if isinstance(item, dict)
            ):
                refs.append(
                    {
                        "record_id": control_record["record_id"],
                        "record_sha256": control_record["record_sha256"],
                        "terminal_state": control_record["terminal_state"],
                        "stopped_at_utc": control_record["stopped_at_utc"],
                    }
                )
            run["emergency_stop_records"] = refs
            run["emergency_stop"] = {
                "record_id": control_record["record_id"],
                "record_sha256": control_record["record_sha256"],
                "terminal_state": control_record["terminal_state"],
                "actor": control_record["actor"],
                "reason": control_record["reason"],
                "reason_codes": control_record.get("reason_codes", []),
                "stopped_at_utc": control_record["stopped_at_utc"],
                "token_guard_snapshot_sha256": control_record[
                    "token_guard_snapshot_sha256"
                ],
                "container_action_evidence_refs": control_record[
                    "container_action_evidence_refs"
                ],
                "human_decision_created": False,
                "datapass_created": False,
            }
            run["state"] = str(control_record["terminal_state"])
            bootstrap_lifecycle(run)
            if run["lifecycle"]["current_phase"] != "FAILED_CLOSED":
                record_transition(
                    run,
                    run["state"],
                    actor=str(control_record["actor"]),
                    reason=str(control_record["reason"]),
                    target_phase="FAILED_CLOSED",
                )
            event = self._event(
                len(run.get("events") or []) + 1,
                run_id,
                "控制面紧急停止已固化",
                (
                    f"{run['state']} · actor={control_record['actor']} · "
                    f"record={control_record['record_id']} · 未创建Human/DataPass"
                ),
                "STOPPED",
            )
            event["source"] = "APPEND_ONLY_EMERGENCY_CONTROL_LEDGER"
            event["input_hash"] = control_record["token_guard_snapshot_sha256"]
            event["output_hash"] = control_record["record_sha256"]
            event["tokens"] = 0
            event["token_ledger"] = "CONTROL_PLANE_NO_MODEL"
            run.setdefault("events", []).append(event)
            self._persist_run(run)
            return run

    def record_dispatch_failure(self, run_id: str, detail: str) -> dict[str, Any]:
        with self.lock:
            run = self.get_run(run_id)
            run["state"] = "AGENTTEAMS_DISPATCH_FAILED"
            record_transition(
                run,
                run["state"],
                actor="agentteams-adapter",
                reason="AgentTeams dispatch failed closed",
            )
            run["events"].append(
                self._event(
                    len(run["events"]) + 1,
                    run_id,
                    "AgentTeams 派发失败",
                    detail,
                    "BLOCKED",
                )
            )
            self._persist_run(run)
            return run

    def _skill_attestation(
        self, run: dict[str, Any], artifacts: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Join pre-route deterministic receipts and Worker receipts by Skill.

        Version-change detection must happen before Manager can route a change
        case.  Its receipt therefore belongs to the same Run evidence chain but
        is not falsely labelled as a Worker call.  When a Worker re-executes a
        Skill, the Worker receipt takes precedence over the gateway receipt.
        """
        invocations: list[dict[str, Any]] = []
        by_skill: dict[str, dict[str, Any]] = {}
        for artifact in artifacts.values():
            for receipt in artifact.get("skill_invocations", []) or []:
                skill_id = str(receipt.get("skill_id", ""))
                if skill_id:
                    item = dict(receipt)
                    invocations.append(item)
                    by_skill[skill_id] = item
        change_bundle_id = str(run.get("change_bundle_id", ""))
        if change_bundle_id:
            bundle = self.get_change_bundle(change_bundle_id)
            for receipt in bundle.get("skill_invocations", []) or []:
                skill_id = str(receipt.get("skill_id", ""))
                if skill_id and skill_id not in by_skill:
                    item = dict(receipt)
                    item["execution_stage"] = "PRE_ROUTE_CHANGE_GATEWAY"
                    by_skill[skill_id] = item
                    invocations.append(item)
        required = (run.get("root_route_decision") or {}).get(
            "required_skill_versions", {}
        ) or {}
        missing = [
            skill_id
            for skill_id, version in required.items()
            if skill_id not in by_skill
            or str(by_skill[skill_id].get("version", "")) != str(version)
        ]
        return invocations, missing

    @staticmethod
    def _formal_worker_artifact(
        worker_id: str,
        artifact: dict[str, Any],
        *,
        run_id: str,
        seen_task_ids: set[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(artifact, dict):
            raise ValueError(f"Worker {worker_id} 未形成独立产物")
        declared_sha = str(artifact.get("artifact_sha256") or "")
        unsigned = {
            key: value for key, value in artifact.items() if key != "artifact_sha256"
        }
        actual_sha = canonical_sha256(unsigned)
        if not re.fullmatch(r"[0-9a-f]{64}", declared_sha) or declared_sha != actual_sha:
            raise ValueError(f"Worker {worker_id} 产物自哈希不一致")
        if str(artifact.get("run_id") or "") != run_id:
            raise ValueError(f"Worker {worker_id} 产物不属于本Run")
        if canonical_agent_id(artifact.get("role")) != worker_id:
            raise ValueError(f"Worker {worker_id} 产物责任角色不一致")
        task_id = str(artifact.get("task_id") or "")
        if not task_id or task_id in seen_task_ids:
            raise ValueError(f"Worker {worker_id} 缺少独立Task身份")
        seen_task_ids.add(task_id)
        return artifact, {
            "worker_id": worker_id,
            "status": "SEALED",
            "artifact_sha256": declared_sha,
        }

    def _build_formal_live_datapass(
        self,
        run: dict[str, Any],
        agent_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Aggregate only independently sealed, same-Run Worker evidence."""
        if build_formal_datapass_draft is None or validate_formal_datapass is None:
            raise RuntimeError("FINFLUX_DATAPASS_V0.2 builder is unavailable")
        envelope = run.get("case_envelope") or {}
        if envelope.get("protocol") != FORMAL_CASE_ENVELOPE_PROTOCOL:
            raise ValueError("本Run未绑定原生FINFLUX_CASE_ENVELOPE_V0.2")
        validate_formal_case_envelope(envelope)
        if envelope.get("run_id") != run.get("run_id") or envelope.get("case_id") != run.get("case_id"):
            raise ValueError("正式CaseEnvelope与Live Run身份不一致")

        plan = project_worker_plan(run)
        required_worker_ids = list(plan["worker_ids"])
        raw_artifacts = agent_result.get("worker_artifacts") or {}
        normalized_artifacts = {
            canonical_agent_id(worker): artifact
            for worker, artifact in raw_artifacts.items()
            if canonical_agent_id(worker) and isinstance(artifact, dict)
        }
        if not required_worker_ids or set(normalized_artifacts) != set(required_worker_ids):
            missing = sorted(set(required_worker_ids) - set(normalized_artifacts))
            extra = sorted(set(normalized_artifacts) - set(required_worker_ids))
            raise ValueError(
                "Worker产物集合与Manager路由不一致"
                f"; missing={','.join(missing) or 'none'}; extra={','.join(extra) or 'none'}"
            )

        worker_receipts: list[dict[str, Any]] = []
        sealed_artifacts: dict[str, dict[str, Any]] = {}
        skill_by_id: dict[str, dict[str, Any]] = {}
        seen_task_ids: set[str] = set()
        required_skill_versions = dict(
            (run.get("root_route_decision") or {}).get("required_skill_versions")
            or {}
        )
        for worker_id in required_worker_ids:
            artifact, worker_receipt = self._formal_worker_artifact(
                worker_id,
                normalized_artifacts[worker_id],
                run_id=str(run["run_id"]),
                seen_task_ids=seen_task_ids,
            )
            sealed_artifacts[worker_id] = artifact
            worker_receipts.append(worker_receipt)
            for raw_receipt in artifact.get("skill_invocations") or []:
                if not isinstance(raw_receipt, dict):
                    raise ValueError(f"Worker {worker_id} Skill receipt格式错误")
                skill_id = str(raw_receipt.get("skill_id") or "")
                if not skill_id or skill_id in skill_by_id:
                    raise ValueError(f"Skill receipt缺失身份或重复: {skill_id or 'EMPTY'}")
                raw_status = str(raw_receipt.get("status") or "").upper()
                normalized_status = (
                    "CACHE_HIT" if raw_status == "CACHE_HIT" else
                    "SUCCEEDED" if raw_status in {"SUCCESS", "SUCCEEDED"} else
                    raw_status
                )
                skill_by_id[skill_id] = {
                    "skill_id": skill_id,
                    "version": str(raw_receipt.get("version") or ""),
                    "worker_id": worker_id,
                    "input_sha256": str(raw_receipt.get("input_sha256") or ""),
                    "output_sha256": str(raw_receipt.get("output_sha256") or ""),
                    "status": normalized_status,
                }
        if set(skill_by_id) != set(required_skill_versions):
            missing = sorted(set(required_skill_versions) - set(skill_by_id))
            extra = sorted(set(skill_by_id) - set(required_skill_versions))
            raise ValueError(
                "Skill执行回执与Manager路由不一致"
                f"; missing={','.join(missing) or 'none'}; extra={','.join(extra) or 'none'}"
            )
        for skill_id, version in required_skill_versions.items():
            if skill_by_id[skill_id]["version"] != str(version):
                raise ValueError(f"Skill版本不一致: {skill_id}@{skill_by_id[skill_id]['version']}")

        evidence_artifact = sealed_artifacts.get("evidence-investigator") or {}
        semantic_artifact = sealed_artifacts.get("semantic-impact-analyst") or {}
        validator_artifact = sealed_artifacts.get("independent-validator") or {}
        evidence_result = evidence_artifact.get("evidence") or {}
        contract_result = semantic_artifact.get("contract") or {}
        impact_facts = semantic_artifact.get("impact") or {}
        aggregate_impact_facts = dict(impact_facts)
        evidence_status = str(evidence_result.get("status") or "NOT_AVAILABLE")
        semantic_status = str(contract_result.get("status") or "NOT_AVAILABLE")
        independent_evidence = str(
            validator_artifact.get("evidence_status") or "NOT_AVAILABLE"
        )
        evidence_quorum_met = (
            evidence_status == "VERIFIED" and independent_evidence == "VERIFIED"
        )
        recommendation = str(
            agent_result.get("leader_recommendation") or "NEEDS_EVIDENCE"
        ).upper()
        if recommendation not in {"PASS", "BLOCK", "NEEDS_EVIDENCE"}:
            raise ValueError("Case Lead未形成可接受的机器建议")
        precheck = run.get("precheck") or {}
        configured_mapping_conflict = (
            recommendation == "BLOCK"
            and str(precheck.get("machine_recommendation") or "").upper() == "BLOCK"
            and bool(precheck.get("candidate_mapping"))
            and bool(precheck.get("required_field"))
            and precheck.get("candidate_mapping") != precheck.get("required_field")
        )
        if configured_mapping_conflict:
            aggregate_impact_facts.update(
                {
                    "configured_candidate_mapping": precheck.get("candidate_mapping"),
                    "required_field": precheck.get("required_field"),
                    "configured_candidate_value": precheck.get(
                        str(precheck.get("candidate_mapping"))
                    ),
                    "required_value": precheck.get(str(precheck.get("required_field"))),
                    "configured_financial_impact_cny_per_contract": precheck.get(
                        "impact_cny_per_contract"
                    ),
                    "agent_proposed_mapping": impact_facts.get("selected_price_field"),
                    "agent_proposal_residual_cny_per_contract": impact_facts.get(
                        "financial_misstatement_cny_per_contract"
                    ),
                }
            )
            semantic_status = "CONFLICT"
        impact_metrics: list[dict[str, Any]] = []
        financial_impact = (
            precheck.get("impact_cny_per_contract")
            if configured_mapping_conflict
            else impact_facts.get("financial_misstatement_cny_per_contract")
        )
        if financial_impact is not None:
            impact_metrics.append(
                {
                    "metric_id": "financial_misstatement_cny_per_contract",
                    "label": "按声明用途测算的每手金额影响",
                    "value": financial_impact,
                    "unit": "CNY/contract",
                    "source_kind": "DETERMINISTIC",
                }
            )
        field_difference = (
            abs(float(precheck.get("settle")) - float(precheck.get("close")))
            if configured_mapping_conflict
            and precheck.get("settle") is not None
            and precheck.get("close") is not None
            else impact_facts.get("field_mapping_difference_points")
        )
        if field_difference is not None:
            impact_metrics.append(
                {
                    "metric_id": "field_mapping_difference_points",
                    "label": "候选字段与契约字段的价格差",
                    "value": field_difference,
                    "unit": "points",
                    "source_kind": "DETERMINISTIC",
                }
            )
        if configured_mapping_conflict and impact_facts.get(
            "financial_misstatement_cny_per_contract"
        ) is not None:
            impact_metrics.append(
                {
                    "metric_id": "agent_remediation_residual_cny_per_contract",
                    "label": "Agent修订建议经Skill验证后的剩余金额影响",
                    "value": impact_facts["financial_misstatement_cny_per_contract"],
                    "unit": "CNY/contract",
                    "source_kind": "DETERMINISTIC",
                }
            )
        if impact_facts.get("impact_cny_per_10000_units") is not None:
            impact_metrics.append(
                {
                    "metric_id": "impact_cny_per_10000_units",
                    "label": "候选净值口径对每万份持仓的确定性金额影响",
                    "value": impact_facts["impact_cny_per_10000_units"],
                    "unit": "CNY/10000 units",
                    "source_kind": "DETERMINISTIC",
                }
            )
        if impact_facts.get("return_difference_pct_points") is not None:
            impact_metrics.append(
                {
                    "metric_id": "return_difference_pct_points",
                    "label": "来源证据提供的复权语义收益率差异",
                    "value": impact_facts["return_difference_pct_points"],
                    "unit": "percentage points",
                    "source_kind": "DETERMINISTIC",
                }
            )
        reason_codes = list(
            dict.fromkeys(
                list((run.get("root_route_decision") or {}).get("reason_codes") or [])
                + [f"LEADER_RECOMMENDATION_{recommendation}"]
            )
        )
        datapass = build_formal_datapass_draft(
            envelope=envelope,
            machine_recommendation=recommendation,
            reason_codes=reason_codes,
            recommendation_summary=(
                "同一Run的路由所选Worker均形成独立自哈希产物，所需Skill版本和输入输出哈希已核验；"
                f"Case Lead建议为{recommendation}，最终准入仍需负责人决定。"
            ),
            evidence_status=evidence_status,
            evidence_quorum_met=evidence_quorum_met,
            semantic_status=semantic_status,
            # A Worker artifact may exist while all numeric impact fields are
            # intentionally null.  That is missing evidence, not a zero-valued
            # calculation.  Keep the formal metric list empty and mark the
            # assessment unavailable rather than manufacturing ``0``.
            impact_status="COMPUTED" if impact_metrics else "NOT_AVAILABLE",
            impact_facts=aggregate_impact_facts,
            impact_metrics=impact_metrics,
            required_worker_ids=required_worker_ids,
            worker_receipts=worker_receipts,
            required_skill_ids=list(required_skill_versions),
            skill_invocations=[skill_by_id[item] for item in required_skill_versions],
            generated_at_utc=utc_now(),
            legacy_reference=None,
        )
        validate_formal_datapass(
            datapass,
            envelope=envelope,
            worker_artifacts=sealed_artifacts,
        )
        return datapass

    def _existing_formal_datapass(
        self,
        run: dict[str, Any],
        agent_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reuse a sealed DataPass for the same immutable Run facts.

        A DataPass is a signable evidence object, not a live view.  Rebuilding
        it on every GET changes ``generated_at_utc`` and therefore its hash,
        invalidating a Human signature even though no financial fact changed.
        """
        existing = run.get("datapass")
        if not isinstance(existing, dict) or existing.get("protocol") != FORMAL_DATAPASS_PROTOCOL:
            return None
        artifacts = (agent_result.get("worker_artifacts") or {})
        try:
            validate_formal_datapass(
                existing,
                envelope=run.get("case_envelope") or None,
                worker_artifacts=artifacts,
            )
        except Exception:
            return None
        plan = project_worker_plan(run)
        expected_workers = set(plan["worker_ids"])
        expected_skills = set(
            (run.get("root_route_decision") or {}).get(
                "required_skill_versions", {}
            )
            or {}
        )
        existing_workers = set(
            ((existing.get("workers") or {}).get("required_worker_ids") or [])
        )
        existing_skills = set(
            ((existing.get("skills") or {}).get("required_skill_ids") or [])
        )
        recommendation = str(
            agent_result.get("leader_recommendation") or "NEEDS_EVIDENCE"
        ).upper()
        precheck = run.get("precheck") or {}
        expected_configured_impact = precheck.get("impact_cny_per_contract")
        existing_metrics = {
            str(item.get("metric_id")): item.get("value")
            for item in ((existing.get("impact_assessment") or {}).get("metrics") or [])
            if isinstance(item, dict)
        }
        configured_conflict_not_preserved = (
            recommendation == "BLOCK"
            and str(precheck.get("machine_recommendation") or "").upper() == "BLOCK"
            and expected_configured_impact is not None
            and existing_metrics.get("financial_misstatement_cny_per_contract")
            != expected_configured_impact
        )
        if (
            existing.get("run_id") != run.get("run_id")
            or existing.get("case_id") != run.get("case_id")
            or existing.get("machine_recommendation") != recommendation
            or existing_workers != expected_workers
            or existing_skills != expected_skills
            or configured_conflict_not_preserved
        ):
            return None
        return copy.deepcopy(existing)

    def sync_agentteams(self, run_id: str, agent_run: dict[str, Any]) -> dict[str, Any]:
        """Project real Matrix/Worker facts into the same Live Run; never invent missing facts."""
        with self.lock:
            run = self.get_run(run_id)
            if str(agent_run.get("run_id")) != run_id:
                raise ValueError("AgentTeams同步Run ID不一致")
            if str(agent_run.get("state")) in {
                "STOPPED_BY_GATE",
                "BUDGET_EXCEEDED",
            }:
                control_record = agent_run.get("emergency_stop_record")
                if not isinstance(control_record, dict):
                    raise ValueError(
                        "AgentTeams终止状态缺少正式紧急停止控制记录；拒绝投影"
                    )
                return self.record_emergency_stop(run_id, control_record)
            if run.get("protocol") == "FINFLUX_LIVE_RUN_V0.2":
                formal_envelope = run.get("case_envelope") or {}
                try:
                    validate_formal_case_envelope(formal_envelope)
                except Exception as exc:
                    raise ValueError(f"Live Run正式CaseEnvelope验证失败: {exc}") from exc
                transport = agent_run.get("case_envelope") or {}
                observed_handle = (
                    agent_run.get("formal_case_envelope_handle")
                    or transport.get("formal_case_envelope_handle")
                    or {}
                )
                if observed_handle != {
                    "protocol": formal_envelope["protocol"],
                    "schema_version": formal_envelope["schema_version"],
                    "envelope_sha256": formal_envelope["envelope_sha256"],
                }:
                    failure = {
                        "protocol": "FINFLUX_FORMAL_AGGREGATION_FAILURE_V0.2",
                        "case_id": run.get("case_id"),
                        "run_id": run_id,
                        "case_envelope_sha256": formal_envelope.get("envelope_sha256"),
                        "failure_class": "CaseEnvelopeHandleMismatch",
                        "reason": "AgentTeams transport未绑定本Run正式CaseEnvelope哈希Handle",
                        "failed_at_utc": utc_now(),
                        "human_gate_opened": False,
                    }
                    failure["failure_sha256"] = canonical_sha256(failure)
                    run["formal_aggregation_failure"] = failure
                    run["state"] = "FAILED_CLOSED"
                    run["datapass"] = None
                    run["human_gate"].update(
                        {"state": "NOT_OPENED", "decision": None}
                    )
                    bootstrap_lifecycle(run)
                    if run["lifecycle"]["current_phase"] != "FAILED_CLOSED":
                        record_transition(
                            run,
                            "FORMAL_ENVELOPE_HANDLE_MISMATCH",
                            actor="finflux-formal-aggregator",
                            reason=failure["reason"],
                            target_phase="FAILED_CLOSED",
                        )
                    self._persist_run(run)
                    return run
            state = str(agent_run.get("state", "SUBMITTED"))
            run["agentteams_adapter_protocol"] = str(
                agent_run.get("protocol") or ""
            )
            for field in AGENTTEAMS_ACCEPTANCE_FIELDS:
                if field in agent_run:
                    run[field] = copy.deepcopy(agent_run[field])
            run["agentteams_trace"] = copy.deepcopy(agent_run.get("trace") or [])
            run["state"] = state
            run["agentteams_run_id"] = run_id
            bootstrap_lifecycle(run)
            dispatch_request = dict(run.get("dispatch_request") or {})
            if dispatch_request and dispatch_request.get("status") in {
                "QUEUED",
                "RETRY_WAIT",
            }:
                dispatch_request.update(
                    {
                        "status": "DISPATCHED",
                        "dispatched_at_utc": dispatch_request.get("dispatched_at_utc")
                        or utc_now(),
                        "agentteams_run_id": run_id,
                        "last_error": None,
                        "next_attempt_epoch": None,
                    }
                )
                run["dispatch_request"] = dispatch_request
            agent_result = agent_run.get("agent_result") or {}
            run["agent_result"] = agent_result
            run["agentteams"] = {
                "state": state,
                "case_envelope_sha256": agent_run.get("case_envelope_sha256"),
                "manager_room_id": agent_run.get("manager_room_id"),
                "leader_room_id": agent_run.get("leader_room_id"),
                "leader_relay": agent_run.get("leader_relay"),
                "manager_idempotency": agent_run.get("manager_idempotency"),
                "trace_source": agent_run.get("trace_source"),
            }
            existing_ids = {str(item.get("event_id")) for item in run.get("events", [])}
            for matrix_event in agent_run.get("trace", []) or []:
                event_id = str(matrix_event.get("event_id", ""))
                if not event_id or event_id in existing_ids:
                    continue
                actor = matrix_event.get("actor", {}) or {}
                run["events"].append(
                    {
                        "event_id": event_id,
                        "round": 2,
                        "time": str(matrix_event.get("timestamp_utc") or ""),
                        "lane": str(actor.get("role", "matrix")),
                        "title": f"{actor.get('name', 'Matrix Actor')} · Matrix",
                        "summary": str(matrix_event.get("body", "")),
                        "status": str(matrix_event.get("protocol_status", "OBSERVED")),
                        "duration": "observed",
                        "tokens": 0,
                        "token_ledger": "MATRIX_MESSAGE_OBSERVED",
                        "tool": None,
                        "timeout_s": None,
                        "retry_policy": None,
                        "retry_actual": 0,
                        "input_hash": "",
                        "output_hash": "",
                        "task_id": None,
                        "source": str(matrix_event.get("source", "MATRIX")),
                    }
                )
                existing_ids.add(event_id)
            agent_budget = agent_run.get("budget", {}) or {}
            message_proxy = int(
                agent_budget.get("observed_message_token_estimate", 0) or 0
            )
            provider_usage = agent_run.get("provider_usage", {}) or {}
            provider_reported = provider_usage.get("status") == "PROVIDER_REPORTED"
            provider_total = (
                int(provider_usage.get("total_tokens", 0) or 0)
                if provider_reported
                else None
            )
            run["provider_usage"] = provider_usage
            run["budget"]["tokens"] = {
                "observed": provider_total,
                "reported": provider_total,
                "prompt": (
                    int(provider_usage.get("prompt_tokens", 0) or 0)
                    if provider_reported
                    else None
                ),
                "completion": (
                    int(provider_usage.get("completion_tokens", 0) or 0)
                    if provider_reported
                    else None
                ),
                "call_count": (
                    int(provider_usage.get("call_count", 0) or 0)
                    if provider_reported
                    else None
                ),
                "budget": None,
                "percent": None,
                "status": provider_usage.get("status", "NOT_CAPTURED"),
                "source": provider_usage.get("source", "NOT_CAPTURED"),
                "attribution_status": provider_usage.get(
                    "attribution_status", "NOT_CAPTURED"
                ),
            }
            run["budget"]["message_proxy"] = {
                "observed": message_proxy,
                "limit": 10000,
                "percent": min(100, round(message_proxy / 10000 * 100, 1)),
                "source": "MATRIX_MESSAGE_CHARACTER_PROXY",
                "is_provider_usage": False,
            }
            run["budget"]["events"]["used"] = int(
                agent_budget.get("matrix_events", len(run["events"]))
                or len(run["events"])
            )
            run["budget"]["wallclock"]["used_s"] = int(agent_budget.get("elapsed_seconds", 0) or 0)
            run["budget"]["note"] = (
                "模型Token来自AgentTeams Runtime落盘的供应商逐调用usage；Matrix字符估算已移至"
                "message_proxy，仅用于协议消息体积门禁，禁止再显示为模型Token。"
            )
            recommendation = str(agent_result.get("leader_recommendation", "PENDING"))
            artifacts = agent_result.get("worker_artifacts", {}) or {}
            current_phase = str(run["lifecycle"]["current_phase"])
            if current_phase == "FAILED_CLOSED" and state in {
                "SUBMITTED",
                "AGENTTEAMS_SUBMITTED",
                "ACTIVE",
                "RUNNING",
            }:
                record_transition(
                    run,
                    "READY_FOR_AGENTTEAMS",
                    actor="run-supervisor",
                    reason="Previously guarded queue was admitted after the active Run released occupancy",
                    target_phase="READY_FOR_DISPATCH",
                )
                current_phase = "READY_FOR_DISPATCH"
            if current_phase == "READY_FOR_DISPATCH":
                record_transition(
                    run,
                    "AGENTTEAMS_SUBMITTED",
                    actor="agentteams-adapter",
                    reason="AgentTeams Run identity observed during synchronization",
                )
            if artifacts and run["lifecycle"]["current_phase"] in {
                "DISPATCHED",
                "FAILED_CLOSED",
            }:
                record_transition(
                    run,
                    "ACTIVE",
                    actor="agentteams-runtime",
                    reason=f"{len(artifacts)} Worker artifact(s) observed",
                )
            worker_plan = project_worker_plan(run)
            required_workers = int(worker_plan["required_count"])
            required_skills = len(
                (run.get("root_route_decision") or {}).get(
                    "required_skill_versions", {}
                )
            ) or 5
            if recommendation != "PENDING" and worker_plan["complete"]:
                formal_run = run.get("protocol") == "FINFLUX_LIVE_RUN_V0.2"
                if formal_run:
                    try:
                        formal_datapass = self._existing_formal_datapass(
                            run, agent_result
                        ) or self._build_formal_live_datapass(run, agent_result)
                    except Exception as exc:
                        failure = {
                            "protocol": "FINFLUX_FORMAL_AGGREGATION_FAILURE_V0.2",
                            "case_id": run.get("case_id"),
                            "run_id": run_id,
                            "case_envelope_sha256": (
                                (run.get("case_envelope") or {}).get("envelope_sha256")
                            ),
                            "failure_class": type(exc).__name__,
                            "reason": str(exc),
                            "failed_at_utc": utc_now(),
                            "human_gate_opened": False,
                        }
                        failure["failure_sha256"] = canonical_sha256(failure)
                        run["formal_aggregation_failure"] = failure
                        run["datapass"] = None
                        run["human_gate"].update(
                            {"state": "NOT_OPENED", "decision": None}
                        )
                        run["state"] = "FAILED_CLOSED"
                        state = "FAILED_CLOSED"
                        if run["lifecycle"]["current_phase"] != "FAILED_CLOSED":
                            record_transition(
                                run,
                                "FORMAL_PROTOCOL_FAILED",
                                actor="finflux-formal-aggregator",
                                reason=failure["reason"],
                                target_phase="FAILED_CLOSED",
                            )
                    else:
                        run.pop("formal_aggregation_failure", None)
                        run["datapass"] = formal_datapass
                else:
                    invocations, missing_skills = self._skill_attestation(run, artifacts)
                    # Historical V0.1 runs remain viewable, but their old
                    # projection is never relabelled as the native V0.2
                    # protocol or admitted by the formal Judge validator.
                    run["datapass"] = {
                        "protocol": "FINFLUX_LEGACY_DATAPASS_PROJECTION_V0.1",
                        "legacy_projection": True,
                        "source_protocol": str(run.get("protocol") or "LEGACY_UNVERSIONED"),
                        "run_id": run_id,
                        "submission_id": run["submission_id"],
                        "case_id": run["case_id"],
                        "status": "DRAFT_CREATED",
                        "machine_recommendation": recommendation,
                        "worker_artifact_count": worker_plan["completed_count"],
                        "required_worker_count": required_workers,
                        "worker_ids": worker_plan["worker_ids"],
                        "skill_invocations": invocations,
                        "required_skill_invocation_count": required_skills,
                        "observed_skill_invocation_count": len(invocations),
                        "skill_attestation_status": (
                            "LEGACY_HASH_BOUND"
                            if not missing_skills
                            else "MISSING_FROM_WORKER_ARTIFACT"
                        ),
                        "missing_skill_receipts": missing_skills,
                        "leader_datapass_event_id": agent_result.get("leader_datapass_event_id"),
                        "human_decision": agent_result.get("human_decision"),
                        "draft_sha256": canonical_sha256(
                            {"run_id": run_id, "recommendation": recommendation, "artifacts": artifacts}
                        ),
                        "signed": bool(agent_result.get("human_decision")),
                    }
                if not run.get("datapass"):
                    self._persist_run(run)
                    return run
                if run["lifecycle"]["current_phase"] == "DISPATCHED":
                    record_transition(
                        run,
                        "ACTIVE",
                        actor="agentteams-runtime",
                        reason="Worker results were completed before the next poll",
                    )
                if run["lifecycle"]["current_phase"] in {
                    "WORKERS_RUNNING",
                    "DISPATCHED",
                    "FAILED_CLOSED",
                }:
                    record_transition(
                        run,
                        "COMPLETED",
                        actor="finflux-case-lead",
                        reason="Worker seals were aggregated into DataPassDraft",
                        target_phase="DATAPASS_DRAFTED",
                    )
            if (
                run.get("protocol") == "FINFLUX_LIVE_RUN_V0.2"
                and state == "AWAITING_HUMAN"
                and not run.get("datapass")
            ):
                failure = {
                    "protocol": "FINFLUX_FORMAL_AGGREGATION_FAILURE_V0.2",
                    "case_id": run.get("case_id"),
                    "run_id": run_id,
                    "case_envelope_sha256": (
                        (run.get("case_envelope") or {}).get("envelope_sha256")
                    ),
                    "failure_class": "FormalDataPassMissing",
                    "reason": "AgentTeams报告等待Human，但正式DataPass尚未通过同Run验证",
                    "failed_at_utc": utc_now(),
                    "human_gate_opened": False,
                }
                failure["failure_sha256"] = canonical_sha256(failure)
                run["formal_aggregation_failure"] = failure
                run["human_gate"].update({"state": "NOT_OPENED", "decision": None})
                run["state"] = "FAILED_CLOSED"
                state = "FAILED_CLOSED"
                if run["lifecycle"]["current_phase"] != "FAILED_CLOSED":
                    record_transition(
                        run,
                        "FORMAL_DATAPASS_MISSING",
                        actor="finflux-formal-aggregator",
                        reason=failure["reason"],
                        target_phase="FAILED_CLOSED",
                    )
            if state == "AWAITING_HUMAN":
                run["human_gate"]["state"] = "AWAITING_HUMAN"
                run["human_gate"]["opened_at"] = utc_now()
                if run.get("datapass") and run["lifecycle"]["current_phase"] == "DATAPASS_DRAFTED":
                    record_transition(
                        run,
                        state,
                        actor="finflux-case-lead",
                        reason="DataPassDraft submitted to the Matrix Human Gate",
                    )
            elif state == "COMPLETED" and agent_result.get("human_decision"):
                decision = agent_result["human_decision"]
                decision_state = {
                    "APPROVE_PASS": "APPROVED",
                    "CONFIRM_BLOCK": "REJECTED",
                    "REQUEST_EVIDENCE": "RETURNED",
                }.get(decision.get("decision"), "REJECTED")
                run["human_gate"].update(
                    {
                        "state": decision_state,
                        "decision": decision.get("decision"),
                        "human_actor_id": decision.get("reviewer"),
                        "decided_at": decision.get("decided_at_utc"),
                        "reason": decision.get("reason", ""),
                        "post_decision_hash": canonical_sha256(decision),
                    }
                )
                if run["lifecycle"]["current_phase"] == "DATAPASS_DRAFTED":
                    record_transition(
                        run,
                        "AWAITING_HUMAN",
                        actor="finflux-case-lead",
                        reason="Persisted Human decision proves the gate was opened",
                    )
                target_phase = {
                    "APPROVED": "APPROVED",
                    "REJECTED": "BLOCKED",
                    "RETURNED": "RETURNED",
                }[decision_state]
                record_transition(
                    run,
                    state,
                    actor=str(decision.get("reviewer") or "matrix-human"),
                    reason=str(decision.get("reason") or decision.get("decision")),
                    target_phase=target_phase,
                )
            self._persist_run(run)
            if state == "AWAITING_HUMAN" and run.get("datapass"):
                self.ensure_report_preview(run_id)
                return self.get_run(run_id)
            if state == "COMPLETED" and agent_result.get("human_decision"):
                self.ensure_final_result(run_id)
                return self.get_run(run_id)
            return run

    def recover_from_persisted_matrix(self, run_id: str) -> dict[str, Any]:
        """Reproject a durable Matrix DataPass after the runtime goes offline.

        This method never invents a Leader result.  It requires a persisted
        Matrix-sourced Team Leader event plus every route-selected Worker
        artifacts before opening the Human Gate.
        """
        with self.lock:
            run = self.get_run(run_id)
            worker_plan = project_worker_plan(run)
            required_workers = int(worker_plan["required_count"])
            required_skills = len(
                (run.get("root_route_decision") or {}).get(
                    "required_skill_versions", {}
                )
            ) or 5
            if run.get("datapass"):
                datapass = run["datapass"]
                if datapass.get("protocol") == FORMAL_DATAPASS_PROTOCOL:
                    validate_formal_datapass(
                        datapass,
                        envelope=run.get("case_envelope") or None,
                        worker_artifacts=(
                            (run.get("agent_result") or {}).get("worker_artifacts")
                            or {}
                        ),
                    )
                    return run
                if "skill_attestation_status" not in datapass:
                    invocations, missing_skills = self._skill_attestation(
                        run, (run.get("agent_result") or {}).get("worker_artifacts") or {}
                    )
                    observed = len(invocations)
                    datapass["skill_invocations"] = invocations
                    datapass["required_skill_invocation_count"] = required_skills
                    datapass["observed_skill_invocation_count"] = observed
                    datapass["skill_attestation_status"] = (
                        "VERIFIED"
                        if not missing_skills
                        else "MISSING_FROM_WORKER_ARTIFACT"
                    )
                    datapass["missing_skill_receipts"] = missing_skills
                    self._persist_run(run)
                return run
            agent_result = run.get("agent_result") or {}
            artifacts = agent_result.get("worker_artifacts") or {}
            if not worker_plan["complete"]:
                return run
            leader_event = None
            recommendation = "PENDING"
            for event in reversed(run.get("events", []) or []):
                body = str(event.get("summary", ""))
                if (
                    event.get("lane") != "team_leader"
                    or not str(event.get("source", "")).startswith("MATRIX")
                    or run["run_id"] not in body
                    or run["case_id"] not in body
                    or not re.search(
                        r"(?i)\bDATAPASS(?:_DRAFT|DRAFT)\b\s*(?:\||:|CASE_ID\s*=)",
                        body,
                    )
                ):
                    continue
                upper = body.upper().replace("-", "_")
                for candidate in ("NEEDS_EVIDENCE", "BLOCK", "PASS"):
                    if re.search(rf"\b{candidate}\b", upper):
                        recommendation = candidate
                        break
                if recommendation != "PENDING":
                    leader_event = event
                    break
            if not leader_event:
                return run
            agent_result.update(
                {
                    "leader_recommendation": recommendation,
                    "leader_datapass_event_id": leader_event.get("event_id"),
                    "leader_datapass_body": leader_event.get("summary"),
                    "final_decision": recommendation,
                    "recovered_from_persisted_matrix": True,
                }
            )
            run["agent_result"] = agent_result
            if run.get("protocol") == "FINFLUX_LIVE_RUN_V0.2":
                try:
                    run["datapass"] = self._build_formal_live_datapass(
                        run, agent_result
                    )
                except Exception as exc:
                    failure = {
                        "protocol": "FINFLUX_FORMAL_AGGREGATION_FAILURE_V0.2",
                        "case_id": run.get("case_id"),
                        "run_id": run_id,
                        "case_envelope_sha256": (
                            (run.get("case_envelope") or {}).get("envelope_sha256")
                        ),
                        "failure_class": type(exc).__name__,
                        "reason": str(exc),
                        "failed_at_utc": utc_now(),
                        "human_gate_opened": False,
                    }
                    failure["failure_sha256"] = canonical_sha256(failure)
                    run["formal_aggregation_failure"] = failure
                    run["datapass"] = None
                    run["state"] = "FAILED_CLOSED"
                    run["human_gate"].update(
                        {"state": "NOT_OPENED", "decision": None}
                    )
                    bootstrap_lifecycle(run)
                    if run["lifecycle"]["current_phase"] != "FAILED_CLOSED":
                        record_transition(
                            run,
                            "FORMAL_RECOVERY_FAILED",
                            actor="durable-matrix-recovery",
                            reason=failure["reason"],
                            target_phase="FAILED_CLOSED",
                        )
                    self._persist_run(run)
                    return run
            else:
                invocations, missing_skills = self._skill_attestation(run, artifacts)
                run["datapass"] = {
                    "protocol": "FINFLUX_LEGACY_DATAPASS_PROJECTION_V0.1",
                    "legacy_projection": True,
                    "source_protocol": str(run.get("protocol") or "LEGACY_UNVERSIONED"),
                    "run_id": run_id,
                    "submission_id": run["submission_id"],
                    "case_id": run["case_id"],
                    "status": "DRAFT_CREATED",
                    "machine_recommendation": recommendation,
                    "worker_artifact_count": required_workers,
                    "required_worker_count": required_workers,
                    "worker_ids": worker_plan["worker_ids"],
                    "skill_invocations": invocations,
                    "skill_attestation_status": (
                        "LEGACY_HASH_BOUND"
                        if not missing_skills
                        else "MISSING_FROM_WORKER_ARTIFACT"
                    ),
                    "missing_skill_receipts": missing_skills,
                    "required_skill_invocation_count": required_skills,
                    "observed_skill_invocation_count": len(invocations),
                    "leader_datapass_event_id": leader_event.get("event_id"),
                    "human_decision": None,
                    "draft_sha256": canonical_sha256(
                        {"run_id": run_id, "recommendation": recommendation, "artifacts": artifacts}
                    ),
                    "signed": False,
                    "recovered_from_persisted_matrix": True,
                }
            run["state"] = "AWAITING_HUMAN"
            run["human_gate"]["state"] = "AWAITING_HUMAN"
            run["human_gate"]["opened_at"] = utc_now()
            bootstrap_lifecycle(run)
            if run["lifecycle"]["current_phase"] == "READY_FOR_DISPATCH":
                record_transition(
                    run,
                    "AGENTTEAMS_SUBMITTED",
                    actor="durable-matrix-recovery",
                    reason="Persisted Matrix Run identity recovered",
                )
            if run["lifecycle"]["current_phase"] in {"DISPATCHED", "FAILED_CLOSED"}:
                record_transition(
                    run,
                    "ACTIVE",
                    actor="durable-matrix-recovery",
                    reason=f"Recovered {required_workers}/{required_workers} Worker artifacts",
                )
            if run["lifecycle"]["current_phase"] == "WORKERS_RUNNING":
                record_transition(
                    run,
                    "COMPLETED",
                    actor="durable-matrix-recovery",
                    reason="Recovered persisted Leader DataPassDraft",
                    target_phase="DATAPASS_DRAFTED",
                )
            if run["lifecycle"]["current_phase"] == "DATAPASS_DRAFTED":
                record_transition(
                    run,
                    "AWAITING_HUMAN",
                    actor="durable-matrix-recovery",
                    reason="Recovered DataPassDraft opened the Human Gate without replay",
                )
            marker = f"EVT-{run_id[-10:]}-RECOVERY-DATAPASS"
            if not any(item.get("event_id") == marker for item in run.get("events", [])):
                event = self._event(
                    len(run["events"]) + 1,
                    run_id,
                    "持久化 Matrix DataPass 已恢复投影",
                    (
                        f"Leader事件 {leader_event.get('event_id')} 与{required_workers}/{required_workers} Worker产物完成关联；"
                        "Runtime离线期间未重跑Agent、未新增模型Token"
                    ),
                    "RECOVERED",
                )
                event["event_id"] = marker
                event["source"] = "DURABLE_MATRIX_RECOVERY"
                run["events"].append(event)
            self._persist_run(run)
            return run

    @staticmethod
    def _judge_datapass_validation_status(
        datapass: dict[str, Any],
        worker_artifacts: dict[str, dict[str, Any]] | None = None,
        envelope: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        protocol = str(datapass.get("protocol") or "LEGACY_UNVERSIONED")
        if protocol == FORMAL_DATAPASS_PROTOCOL:
            if validate_formal_datapass is None:
                return False, "FORMAL_VALIDATOR_UNAVAILABLE"
            try:
                # The formal validator can bind every receipt in DataPass to
                # exactly one self-hashed Worker artifact.  Judge admission
                # must use that stronger validation whenever the V0.2
                # protocol and its validator are available.
                validate_formal_datapass(
                    datapass,
                    envelope=envelope,
                    worker_artifacts=worker_artifacts,
                )
            except Exception:  # validator owns the detailed schema diagnostics
                return False, "FORMAL_VALIDATION_FAILED"
            return True, "FORMAL_VALIDATED"
        legacy_digest = str(
            datapass.get("draft_sha256") or datapass.get("datapass_sha256") or ""
        )
        if not re.fullmatch(r"[0-9a-fA-F]{64}", legacy_digest):
            return False, f"LEGACY_DIGEST_INVALID:{protocol}"
        return True, f"LEGACY_HASH_BOUND:{protocol}"

    @classmethod
    def _judge_eligibility(cls, run: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not run.get("agentteams_run_id"):
            reasons.append("NO_REAL_AGENTTEAMS_RUN")
        datapass = run.get("datapass") or {}
        if not datapass:
            reasons.append("NO_DATAPASS")
        else:
            datapass_valid, datapass_status = cls._judge_datapass_validation_status(
                datapass,
                ((run.get("agent_result") or {}).get("worker_artifacts") or {}),
                run.get("case_envelope") or None,
            )
            if not datapass_valid:
                reasons.append(f"DATAPASS_INVALID:{datapass_status}")
        plan = project_worker_plan(run)
        required = int(plan["required_count"])
        if required < 3:
            reasons.append("WORKER_PLAN_LT_3")
        if not plan["plan_consistent"]:
            reasons.append("WORKER_PLAN_COUNT_MISMATCH")
        invalid_workers = [
            str(item["agent_id"])
            for item in plan["workers"]
            if not item.get("completed")
            or not item.get("task_id")
            or item.get("artifact_run_id") != run.get("run_id")
            or item.get("binding_status")
            not in {"VERIFIED", "LEGACY_KEY_BOUND"}
            # A transport state alone may advance the runtime projection, but
            # a frozen Judge Run must retain one independently hashable output
            # (or explicit seal) for every selected Worker.
            or item.get("completion_evidence")
            not in {"ARTIFACT_SHA256", "EXPLICIT_SEAL"}
        ]
        if invalid_workers:
            reasons.append(
                "WORKER_ARTIFACT_BINDING_INVALID:" + ",".join(invalid_workers)
            )
        if not plan["complete"]:
            reasons.append("WORKER_ARTIFACTS_INCOMPLETE")
        if not (run.get("events") or []):
            reasons.append("NO_TRACE_EVENTS")
        return not reasons, reasons

    @classmethod
    def _judge_score(cls, run: dict[str, Any]) -> int:
        eligible, _ = cls._judge_eligibility(run)
        if not eligible:
            return -1
        gate_state = str((run.get("human_gate") or {}).get("state") or "")
        score = 100 + len(((run.get("agent_result") or {}).get("worker_artifacts") or {}))
        if gate_state == "AWAITING_HUMAN":
            score += 30
        if gate_state in {"APPROVED", "REJECTED", "RETURNED"}:
            score += 80
        if run.get("final_result"):
            score += 40
        if str(((run.get("budget") or {}).get("tokens") or {}).get("status")) == "PROVIDER_REPORTED":
            score += 10
        return score

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return a compact catalog used to choose a presentation context."""
        safe_limit = max(1, min(int(limit), 500))
        index = self._index()
        effective_selected_id = self._effective_selected_run_id(index)
        records: list[dict[str, Any]] = []
        for path in self.runs.glob("RUN-*.json"):
            run = self._cached_json_read(path)
            if not isinstance(run, dict):
                continue
            eligible, reasons = self._judge_eligibility(run)
            stages = project_decision_stages(run)
            asset_class = str(
                ((run.get("case_envelope") or {}).get("asset_class"))
                or run.get("asset_class")
                or run.get("profile")
                or ""
            )
            records.append(
                {
                    "run_id": run.get("run_id"),
                    "audit_run_id": run.get("run_id"),
                    "display_run_id": run_display_id(
                        str(run.get("run_id") or ""), asset_class
                    ),
                    "case_id": run.get("case_id"),
                    "created_at": run.get("created_at"),
                    "state": run.get("state"),
                    "route": ((run.get("root_route_decision") or {}).get("route")),
                    "agentteams_run_id": run.get("agentteams_run_id"),
                    "worker_progress": stages["agent"]["workers"],
                    "datapass_recommendation": stages["agent"]["recommendation"],
                    "human_state": stages["human"]["state"],
                    "judge_eligible": eligible,
                    "judge_ineligible_reasons": reasons,
                    "is_latest": run.get("run_id") == index.get("latest_run_id"),
                    "is_selected": run.get("run_id") == effective_selected_id,
                    "is_judge_run": run.get("run_id") == index.get("judge_run_id"),
                }
            )
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[:safe_limit]

    def _best_judge_candidate_id(self) -> str | None:
        candidates: list[tuple[int, str, str]] = []
        for path in self.runs.glob("RUN-*.json"):
            run = self._cached_json_read(path)
            if not isinstance(run, dict):
                continue
            score = self._judge_score(run)
            if score >= 0:
                candidates.append(
                    (score, str(run.get("created_at") or ""), str(run.get("run_id") or ""))
                )
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    def selected_run(self) -> dict[str, Any] | None:
        index = self._index()
        run_id = self._effective_selected_run_id(index)
        if run_id:
            try:
                return self.get_run(str(run_id))
            except FileNotFoundError:
                pass
        run_id = index.get("judge_run_id") or self._best_judge_candidate_id()
        if run_id:
            return self.get_run(str(run_id))
        return self.latest_run()

    def _effective_selected_run_id(self, index: dict[str, Any]) -> str | None:
        """Keep the frozen Judge Run as default unless an operator selects another Run."""
        judge_run_id = index.get("judge_run_id")
        if judge_run_id and index.get("selection_mode") != "MANUAL":
            return str(judge_run_id)
        selected_run_id = index.get("selected_run_id")
        return str(selected_run_id) if selected_run_id else None

    def select_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        with self.lock:
            index = self._index()
            index["selected_run_id"] = run_id
            index["selection_mode"] = "MANUAL"
            self._save_index(index)
        return run

    def judge_run(self) -> dict[str, Any] | None:
        index = self._index()
        run_id = index.get("judge_run_id") or self._best_judge_candidate_id()
        if not run_id:
            return None
        run = self.get_run(str(run_id))
        eligible, reasons = self._judge_eligibility(run)
        asset_class = str(
            ((run.get("case_envelope") or {}).get("asset_class"))
            or run.get("asset_class")
            or run.get("profile")
            or ""
        )
        return {
            "run_id": run["run_id"],
            "audit_run_id": run["run_id"],
            "display_run_id": run_display_id(str(run["run_id"]), asset_class),
            "case_id": run.get("case_id"),
            "stage": (
                "SEALED"
                if str((run.get("human_gate") or {}).get("state"))
                in {"APPROVED", "REJECTED", "RETURNED"}
                else "CANDIDATE_AWAITING_HUMAN"
            ),
            "eligible": eligible,
            "ineligible_reasons": reasons,
            "selected_at": index.get("judge_selected_at"),
            "selected_by": index.get("judge_selected_by"),
            "reason": index.get("judge_reason"),
            "persisted": bool(index.get("judge_run_id")),
            "datapass_validation_status": self._judge_datapass_validation_status(
                run.get("datapass") or {},
                ((run.get("agent_result") or {}).get("worker_artifacts") or {}),
                run.get("case_envelope") or None,
            )[1]
            if run.get("datapass")
            else "NO_DATAPASS",
        }

    def set_judge_run(self, run_id: str, actor: str, reason: str = "") -> dict[str, Any]:
        run = self.get_run(run_id)
        eligible, reasons = self._judge_eligibility(run)
        if not eligible:
            raise ValueError("该Run尚不能作为裁判验收Run：" + ", ".join(reasons))
        clean_actor = str(actor).strip()
        if not clean_actor:
            raise ValueError("actor is required")
        with self.lock:
            index = self._index()
            index.update(
                {
                    "judge_run_id": run_id,
                    "selected_run_id": run_id,
                    "judge_selected_at": utc_now(),
                    "judge_selected_by": clean_actor,
                    "judge_reason": str(reason).strip() or "复赛现场端到端验收Run",
                    "selection_mode": "JUDGE_PINNED",
                }
            )
            self._save_index(index)
        return self.judge_run() or {}

    def latest_run(self) -> dict[str, Any] | None:
        run_id = self._index().get("latest_run_id")
        return self.get_run(run_id) if run_id else None

    def latest_submission(self) -> dict[str, Any] | None:
        submission_id = self._index().get("latest_submission_id")
        return self.get_submission(submission_id) if submission_id else None

    def list_submissions(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        records = []
        for path in self.submissions.glob("SUB-*.json"):
            item = self._cached_json_read(path)
            if item:
                item["audit_submission_id"] = item.get("submission_id")
                item["display_submission_id"] = submission_display_id(
                    str(item.get("submission_id") or ""),
                    str(item.get("created_at") or ""),
                )
                records.append(item)
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records[:safe_limit]

    def workspace_catalog_snapshot(self, limit: int = 50) -> dict[str, Any]:
        """Build catalog and Judge projection against one cached file version set."""
        return {
            "run_catalog": self.list_runs(limit),
            "judge_run": self.judge_run(),
            "json_cache": self.json_cache_status(),
        }

    def list_skills(
        self,
        run: dict[str, Any] | None = None,
        change_bundle: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        owners = {
            "evidence-integrity": "Evidence Investigator",
            "rights-gate": "Evidence Investigator",
            "semantic-contract-resolver": "Semantic Impact Analyst",
            "financial-impact-calculator": "Semantic Impact Analyst",
            "independent-evidence-validator": "Independent Validator",
            "classify-data-rights": "Data Rights Steward",
            "enforce-confidentiality-boundary": "Data Rights Steward",
            "retrieve-research-context": "Research Context Analyst",
            "verify-research-context": "Research Context Analyst",
            "guard-execution-budget": "Runtime Resilience Auditor",
            "audit-recovery-readiness": "Runtime Resilience Auditor",
            "build-run-context-capsule": "Context Gateway",
            "load-role-context-slice": "Context Gateway",
        }
        invocations: list[dict[str, Any]] = []
        if run:
            datapass = datapass_presentation_projection(run.get("datapass") or {})
            worker_invocations = list(datapass.get("skill_invocations") or [])
            if not worker_invocations:
                artifacts = (run.get("agent_result") or {}).get("worker_artifacts") or {}
                for artifact in artifacts.values():
                    worker_invocations.extend(artifact.get("skill_invocations") or [])
            invocations = worker_invocations + list(
                (run.get("context_capsule_publish_receipt") or {}).get(
                    "skill_invocations"
                )
                or []
            )
        invocation_by_id: dict[str, list[dict[str, Any]]] = {}
        for invocation in invocations:
            invocation_by_id.setdefault(str(invocation.get("skill_id", "")), []).append(invocation)

        items = []
        for skill_id, version, purpose in SKILLS:
            digest = hashlib.sha256(f"{skill_id}@{version}|{purpose}".encode()).hexdigest()
            observed = invocation_by_id.get(skill_id, [])
            latest = observed[-1] if observed else {}
            version_match = bool(observed) and all(
                str(item.get("version")) == version for item in observed
            )
            status = (
                "INVOKED_VERIFIED"
                if observed and version_match
                else "INVOKED_VERSION_MISMATCH"
                if observed
                else "REGISTERED_NOT_INVOKED"
            )
            items.append(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "purpose": purpose,
                    "status": status,
                    "channel": "AgentTeams Worker runtime" if observed else "local-registry",
                    "capability_id": f"finflux.{skill_id}",
                    "owner_role": owners[skill_id],
                    "input_summary": "EvidenceHandle + CaseEnvelope",
                    "output_summary": "可哈希、可复核的确定性结果",
                    "digest": str(latest.get("digest") or digest),
                    "registry_digest": digest,
                    "discovery_state": (
                        "DISCOVERED_AT_RUNTIME"
                        if latest.get("discovered_at_runtime")
                        else "REGISTERED"
                    ),
                    "runtime_invocations": len(observed),
                    "input_sha256": latest.get("input_sha256"),
                    "output_sha256": latest.get("output_sha256"),
                    "run_id": run.get("run_id") if run and observed else None,
                    "truthful_note": (
                        f"同一 Run 已记录 {len(observed)} 次真实调用，版本与输入/输出摘要可核验。"
                        if observed and version_match
                        else "已注册；只有 AgentTeams Worker 实际加载后才计为 invoked。"
                    ),
                }
            )
        composer = None
        if run:
            composer = (
                ((run.get("final_result") or {}).get("composer"))
                or ((run.get("report_preview") or {}).get("composer"))
            )
        report_invocations = {
            str(item.get("skill_id")): item
            for item in ((composer or {}).get("skill_invocations") or [])
        }
        for skill_id, version, purpose in REPORT_SKILLS:
            latest = report_invocations.get(skill_id, {})
            digest = hashlib.sha256(
                f"{skill_id}@{version}|{purpose}".encode()
            ).hexdigest()
            invoked = bool(latest)
            items.append(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "purpose": purpose,
                    "status": "INVOKED_VERIFIED" if invoked else "REGISTERED_NOT_INVOKED",
                    "channel": (
                        "Result Composer deterministic runtime"
                        if invoked
                        else "local-registry"
                    ),
                    "capability_id": f"finflux.{skill_id}",
                    "owner_role": "Result Composer Agent",
                    "input_summary": "Run + DataPass + Human Gate + provider usage",
                    "output_summary": "最小上下文、PDF/MD/JSON与哈希Manifest",
                    "digest": digest,
                    "registry_digest": digest,
                    "discovery_state": "DISCOVERED_AT_RUNTIME" if invoked else "REGISTERED",
                    "runtime_invocations": 1 if invoked else 0,
                    "input_sha256": latest.get("input_sha256"),
                    "output_sha256": latest.get("output_sha256"),
                    "run_id": run.get("run_id") if run and invoked else None,
                    "truthful_note": (
                        "同一Run由Result Composer确定性执行；模型调用0次、Provider Token为0。"
                        if invoked
                        else "已注册；报告事件发生后才调用。"
                    ),
                }
            )
        change_invocations = {
            str(item.get("skill_id")): item
            for item in ((change_bundle or {}).get("skill_invocations") or [])
        }
        change_owners = {
            "detect-version-change": "Change Evidence Worker",
            "resolve-downstream-lineage": "Downstream Impact Analyst",
            "validate-remediation-plan": "Independent Validator",
        }
        for skill_id, version, purpose in CHANGE_CONTROL_SKILLS:
            observed = change_invocations.get(skill_id) or {}
            digest = hashlib.sha256(
                f"{skill_id}@{version}|{purpose}".encode("utf-8")
            ).hexdigest()
            invoked = bool(observed)
            items.append(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "purpose": purpose,
                    "status": "INVOKED_VERIFIED" if invoked else "REGISTERED_NOT_INVOKED",
                    "channel": (
                        "ChangeBundle deterministic runtime"
                        if invoked
                        else "local-registry"
                    ),
                    "capability_id": f"finflux.{skill_id}",
                    "owner_role": change_owners[skill_id],
                    "input_summary": "Two immutable submissions + declared lineage",
                    "output_summary": "ChangeSet / ImpactGraph / remediation validation",
                    "digest": str(observed.get("digest") or digest),
                    "registry_digest": digest,
                    "discovery_state": (
                        "DISCOVERED_AT_RUNTIME"
                        if observed.get("discovered_at_runtime")
                        else "REGISTERED"
                    ),
                    "runtime_invocations": 1 if invoked else 0,
                    "input_sha256": observed.get("input_sha256"),
                    "output_sha256": observed.get("output_sha256"),
                    "run_id": None,
                    "change_bundle_id": (
                        change_bundle.get("change_bundle_id")
                        if change_bundle and invoked
                        else None
                    ),
                    "truthful_note": (
                        "已由确定性ChangeBundle运行时执行；模型调用0次。"
                        if invoked
                        else "已注册；创建版本变更调查后才调用。"
                    ),
                }
            )
        return items
