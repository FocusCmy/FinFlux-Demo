from __future__ import annotations

import argparse
import copy
from collections import Counter
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import io
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentteams_adapter import (
    AgentTeamsConfigurationError,
    AgentTeamsUnavailable,
    get_run as get_agent_run,
    get_persisted_run as get_persisted_agent_run,
    reset_agent_sessions,
    get_active_run as get_active_agent_run,
    provider_token_guard_snapshot,
    recover_terminal_control_gap as recover_agent_terminal_control_gap,
    request_same_run_repair as request_agent_same_run_repair,
    record_emergency_stop as record_agent_emergency_stop,
    runtime_status as _agentteams_runtime_status,
    submit_live_case as submit_agent_live_case,
    submit_human_decision as submit_agent_human_decision,
    supervisor_dispatch_missing_workers as dispatch_agent_missing_workers,
    supervisor_stop_wait as stop_agent_run_to_wait,
    supervisor_wake_manager as wake_agent_manager,
    terminal_control_status as agent_terminal_control_status,
)
from finchange_gate_core import (
    ASSETS,
    EVIDENCE_ROOT,
    MANIFEST_PATH,
    SCENARIOS,
    compute_financial_impact,
    generate_datapass,
    resolve_semantic_contract,
    verify_evidence_bundle,
)
from live_intake import (
    LiveIntakeRepository,
    MAX_UPLOAD_BYTES,
    WORKER_CATALOG,
    build_run_presentation,
    build_runtime_snapshot,
    canonical_agent_id,
    project_decision_stages,
    project_worker_plan,
    require_v02_export_ready,
)
from profile_registry import get_profile as get_profile_definition
from profile_registry import list_profiles as list_profile_definitions
from control_plane import ControlPlaneSupervisor
from evaluation_metrics_skill import execute_evaluation_metrics_skill
from run_supervisor import RunSupervisor
from runtime_supervisor import RuntimeOperations, RuntimeSupervisor
from unified_intake import (
    MAX_TEXT_BYTES,
    fetch_public_url,
    intake_capabilities,
    search_research_catalog,
    selected_research_items,
)


DEMO_ROOT = Path(__file__).resolve().parent
WEB_ROOT = DEMO_ROOT / "web"
RUNTIME_ROOT = DEMO_ROOT / "runtime"
DECISIONS_PATH = RUNTIME_ROOT / "human_decisions.json"
SOURCE_BOUND_EVALUATION_ROOT = DEMO_ROOT / "data" / "real_50x3_v1"
EVALUATION_REPORT_PATH = SOURCE_BOUND_EVALUATION_ROOT / "evaluation_report.json"
EVALUATION_MANIFEST_PATH = SOURCE_BOUND_EVALUATION_ROOT / "manifest.json"
LEGACY_EVALUATION_REPORT_PATH = (
    DEMO_ROOT / "data" / "evaluation_seed_cases_v1" / "evaluation_report.json"
)
CONTROLLED_BENCHMARK_PATH = (
    DEMO_ROOT / "evaluation" / "controlled_benchmark_ledger.json"
)
AGENTTEAMS_ROOT = next(
    (
        candidate
        for candidate in (DEMO_ROOT.parent / "agentteams", DEMO_ROOT.parent / "agent_demo")
        if candidate.is_dir()
    ),
    DEMO_ROOT.parent / "agentteams",
)
FAULT_EVIDENCE_ROOT = AGENTTEAMS_ROOT / "evidence" / "fault-injection"
PRECHECK_TTL_SECONDS = 15 * 60
PRECHECK_RECEIPTS: dict[str, dict[str, Any]] = {}
PRECHECK_RECEIPTS_LOCK = threading.Lock()
AUDIT_BUNDLE_LOCK = threading.Lock()
LIVE_REPOSITORY = LiveIntakeRepository(RUNTIME_ROOT)
CONTROL_PLANE = ControlPlaneSupervisor(RUNTIME_ROOT)


def dispatch_queued_live_run(run_id: str) -> dict[str, Any]:
    """Accept one durable one-click request when the single-Run gate becomes free."""

    runtime_admission = RUNTIME_SUPERVISOR.status()
    if not runtime_admission.get("gate_open"):
        return {
            "status": "BACKGROUND_RUNTIME_WAIT",
            "run_id": run_id,
            "run_state": "DISPATCH_GUARDED",
            "attempt_count": 0,
            "reason": "RuntimeSupervisor cold-start admission is not READY",
            "runtime_supervisor": runtime_admission,
        }

    run = LIVE_REPOSITORY.get_run(run_id)
    if run.get("agentteams_run_id"):
        return {
            "status": "ALREADY_DISPATCHED",
            "run_id": run_id,
            "run_state": run.get("state"),
            "attempt_count": int(
                (run.get("dispatch_request") or {}).get("attempt_count") or 0
            ),
        }
    guard = provider_token_guard_snapshot(force=True)
    if not guard.get("allowed"):
        reasons = ";".join(str(item) for item in guard.get("reasons") or [])
        waiting = LIVE_REPOSITORY.record_dispatch_retry(
            run_id,
            reasons or "Provider dispatch guard did not admit the queued Run",
        )
        request = waiting.get("dispatch_request") or {}
        return {
            "status": (
                "BACKGROUND_DISPATCH_WAIT"
                if request.get("status") == "WAIT"
                else "BACKGROUND_DISPATCH_RETRY_WAIT"
            ),
            "run_id": run_id,
            "run_state": waiting.get("state"),
            "attempt_count": int(request.get("attempt_count") or 0),
            "reason": request.get("last_error"),
        }
    submission = LIVE_REPOSITORY.get_submission(run["submission_id"])
    try:
        agent_run = submit_agent_live_case(submission, run)
        dispatched = LIVE_REPOSITORY.attach_agentteams(run_id, agent_run)
    except (AgentTeamsConfigurationError, AgentTeamsUnavailable, OSError) as exc:
        waiting = LIVE_REPOSITORY.record_dispatch_retry(
            run_id, f"{type(exc).__name__}: {exc}"
        )
        request = waiting.get("dispatch_request") or {}
        return {
            "status": (
                "BACKGROUND_DISPATCH_WAIT"
                if request.get("status") == "WAIT"
                else "BACKGROUND_DISPATCH_RETRY_WAIT"
            ),
            "run_id": run_id,
            "run_state": waiting.get("state"),
            "attempt_count": int(request.get("attempt_count") or 0),
            "reason": request.get("last_error"),
        }
    return {
        "status": "BACKGROUND_AGENTTEAMS_DISPATCHED",
        "run_id": run_id,
        "run_state": dispatched.get("state"),
        "attempt_count": int(
            (dispatched.get("dispatch_request") or {}).get("attempt_count") or 0
        ),
    }


def release_active_run_occupancy(
    run_id: str, *, actor: str, reason: str
) -> dict[str, Any]:
    """Fail-close one active Run so the durable queue can continue.

    This is an explicit operator control-plane action.  It never changes a
    financial recommendation into PASS, never creates a DataPass or Human
    decision, and never creates the next Run.  RunSupervisor owns the later
    dispatch of the already queued Run.
    """

    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor:
        raise ValueError("释放占用必须记录操作者")
    if len(reason) < 8:
        raise ValueError("释放原因至少需要8个字符")
    active = get_active_agent_run()
    if not isinstance(active, dict):
        queued = LIVE_REPOSITORY.next_dispatch_request()
        return {
            "protocol": "FINFLUX_OPERATOR_OCCUPANCY_RELEASE_V1",
            "status": "ALREADY_FREE",
            "released_run_id": None,
            "released_run_state": None,
            "next_queued_run_id": (queued or {}).get("run_id"),
            "supervisor_dispatch_eta_seconds": RUN_SUPERVISOR.interval_seconds,
            "model_called_by_release": False,
            "datapass_created_by_release": False,
            "human_decision_created_by_release": False,
        }
    active_run_id = str(active.get("run_id") or "")
    if active_run_id != run_id:
        raise ValueError(
            f"占用Run已变化：当前为{active_run_id or 'UNKNOWN'}，请刷新后重试"
        )
    active_state = str(active.get("state") or "UNKNOWN")
    if active_state == "AWAITING_HUMAN":
        raise ValueError("该Run已有DataPass并等待Human，请到Human Gate处理，不能作为卡死Run释放")
    if active_state in {
        "COMPLETED",
        "STOPPED_BY_GATE",
        "BUDGET_EXCEEDED",
        "FAILED_CLOSED",
        "CANCELLED_BY_SESSION_RESET",
        "MODEL_CONTROL_CLEANUP_FAILED",
    }:
        raise ValueError(f"该Run已处于终态{active_state}，无需释放")

    stopped = stop_agent_run_to_wait(
        run_id,
        requested_by=actor,
        reason=reason,
        reason_codes=[
            "OPERATOR_RELEASE_OCCUPANCY",
            "QUEUED_RUN_WAITING",
        ],
    )
    projected = LIVE_REPOSITORY.sync_agentteams(run_id, stopped)
    queued = LIVE_REPOSITORY.next_dispatch_request()
    return {
        "protocol": "FINFLUX_OPERATOR_OCCUPANCY_RELEASE_V1",
        "status": "RELEASED_TO_WAIT",
        "released_run_id": run_id,
        "released_run_state": projected.get("state"),
        "release_record_sha256": (
            stopped.get("emergency_stop_record") or {}
        ).get("record_sha256"),
        "next_queued_run_id": (queued or {}).get("run_id"),
        "supervisor_dispatch_eta_seconds": RUN_SUPERVISOR.interval_seconds,
        "model_called_by_release": False,
        "datapass_created_by_release": False,
        "human_decision_created_by_release": False,
        "truth_boundary": (
            "仅终止占用Run并关闭其模型网关账本；不生成PASS、DataPass或Human决定。"
            "下一条既有排队Run由RunSupervisor独立派发。"
        ),
    }


RUN_SUPERVISOR = RunSupervisor(
    repository=LIVE_REPOSITORY,
    get_active_run=get_active_agent_run,
    get_run=get_agent_run,
    wake_manager=wake_agent_manager,
    dispatch_missing_workers=dispatch_agent_missing_workers,
    stop_wait=stop_agent_run_to_wait,
    get_queued_run=LIVE_REPOSITORY.next_dispatch_request,
    dispatch_queued_run=dispatch_queued_live_run,
)
RUNTIME_SUPERVISOR = RuntimeSupervisor(
    operations=RuntimeOperations(DEMO_ROOT.parent),
    state_root=RUNTIME_ROOT / "runtime_supervisor",
    interval_seconds=float(os.environ.get("FINFLUX_RUNTIME_SUPERVISOR_INTERVAL_SECONDS", "5")),
    expensive_interval_seconds=float(
        os.environ.get("FINFLUX_RUNTIME_FULL_CHECK_INTERVAL_SECONDS", "60")
    ),
    max_repairs=int(os.environ.get("FINFLUX_RUNTIME_MAX_REPAIRS", "3")),
    active_business_run=get_active_agent_run,
)
_RUNTIME_STATUS_CACHE: dict[str, Any] = {
    "captured_at": 0.0,
    "payload": None,
}
_RUNTIME_STATUS_CACHE_LOCK = threading.Lock()


def agentteams_runtime_status() -> dict[str, Any]:
    """Share one short-lived runtime probe across a frontend refresh burst.

    A real probe performs several controller and Docker readbacks and currently
    takes about five seconds on Docker Desktop.  The browser requests workspace,
    control-plane and observability views together; running the same probe for
    every panel makes a healthy system look hung.  This cache never fabricates
    state and expires after two seconds.
    """

    now = time.monotonic()
    with _RUNTIME_STATUS_CACHE_LOCK:
        payload = _RUNTIME_STATUS_CACHE.get("payload")
        captured = float(_RUNTIME_STATUS_CACHE.get("captured_at") or 0.0)
        if isinstance(payload, dict) and now - captured < 2.0:
            return copy.deepcopy(payload)
    payload = _agentteams_runtime_status()
    supervisor = RUNTIME_SUPERVISOR.status()
    transport_connected = bool(payload.get("connected"))
    admission_ready = bool(transport_connected and supervisor.get("gate_open"))
    payload["transport_connected"] = transport_connected
    payload["admission_ready"] = admission_ready
    payload["connected"] = admission_ready
    payload["runtime_supervisor"] = supervisor
    if transport_connected and not admission_ready:
        payload["status"] = str(supervisor.get("state") or "RUNTIME_CHECKING")
        payload["truthful_note"] = (
            "AgentTeams传输可达，但RuntimeSupervisor尚未完成端口、8/8 Worker、"
            "8090路由、包摘要和真实模型canary；禁止创建Run。"
        )
    with _RUNTIME_STATUS_CACHE_LOCK:
        _RUNTIME_STATUS_CACHE["payload"] = copy.deepcopy(payload)
        _RUNTIME_STATUS_CACHE["captured_at"] = time.monotonic()
    return payload


def compact_provider_usage(provider_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Return provider truth without replaying the per-call ledger on polling.

    The complete hash-chained records remain available in the persisted Run,
    Presentation/Trace and audit ZIP.  Status polling only needs totals and the
    ledger seal; returning every request/response receipt made the browser read
    the same large JSON over and over.
    """

    usage = provider_usage or {}
    ledger = usage.get("model_gateway_ledger") or {}
    ledger_summary = {
        key: ledger.get(key)
        for key in (
            "protocol",
            "status",
            "request_attempt_count",
            "provider_call_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "in_flight_reserved_tokens",
            "updated_at_utc",
            "fuse_reason",
            "ledger_sha256",
        )
        if ledger.get(key) is not None
    }
    ledger_summary["record_count"] = len(ledger.get("records") or [])
    ledger_summary["records_included"] = False
    return {
        "status": usage.get("status"),
        "source": usage.get("source"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "call_count": usage.get("call_count"),
        "model_gateway_ledger": ledger_summary,
        "read_scope": "SUMMARY_ONLY_FULL_LEDGER_IN_TRACE_AND_AUDIT",
    }


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def session_reset_access(headers: Any) -> tuple[bool, str]:
    """Fail closed unless a non-competition operator explicitly enables reset."""

    if _environment_flag("FINFLUX_COMPETITION_MODE", default=True):
        return False, "DISABLED_IN_COMPETITION_MODE"
    if not _environment_flag("FINFLUX_ENABLE_SESSION_RESET", default=False):
        return False, "SESSION_RESET_NOT_ENABLED"
    expected = str(os.environ.get("FINFLUX_CONTROL_PLANE_TOKEN") or "")
    if not expected:
        return False, "CONTROL_PLANE_TOKEN_NOT_CONFIGURED"
    supplied = str(headers.get("X-FinFlux-Control-Token") or "")
    authorization = str(headers.get("Authorization") or "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        return False, "CONTROL_PLANE_AUTHENTICATION_FAILED"
    return True, "AUTHORIZED_OPERATOR"


def reconcile_control_plane_snapshot() -> dict[str, Any]:
    """Reconcile only persisted control-plane facts; never hydrate AgentTeams."""

    selected = LIVE_REPOSITORY.selected_run()
    runtime = agentteams_runtime_status()
    return CONTROL_PLANE.reconcile(
        runtime,
        selected,
        provider_token_guard_snapshot(force=True),
    )


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent multiple Windows demo processes from sharing the same port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


CASE_CATALOG: dict[str, dict[str, Any]] = {
    "equity": {
        "label": "股票",
        "pack": "EquityCorporateActionPack",
        "case_title": "A股复权语义与事件行完整性",
        "instrument": "18只沪深A股",
        "event": "23个2026公司行动事件",
        "risk": "接口标签为NONE，但序列表现为qfq；公司行动对象导致事件行丢失。",
        "impact": "华友钴业锚点收益率相差0.854371个百分点。",
        "variants": {
            "blocked": {
                "label": "错误接入",
                "case_title": "A股复权语义与事件行完整性",
                "risk": "接口标签为NONE，但序列表现为qfq；公司行动对象导致事件行丢失。",
                "impact": "华友钴业锚点收益率相差0.854371个百分点。",
                "expected": "BLOCK",
            },
            "admissible": {
                "label": "合规映射",
                "case_title": "A股复权语义正确映射",
                "risk": "将真实qfq序列声明为qfq，并要求原始公司行动事件行完整保留。",
                "impact": "正确映射与契约一致；原始证据不改写，剩余语义错配和事件行丢失均为0。",
                "expected": "PASS",
            },
        },
        "source": "FinShare 2.1.0 + AKShare 1.18.84",
        "tone": "cyan",
    },
    "futures": {
        "label": "期货",
        "pack": "FuturesSettlementPack",
        "case_title": "结算用途字段错配",
        "instrument": "IF2608",
        "event": "2026-08-14中金所日行情",
        "risk": "逐日结算用途错误采用close，而不是settle。",
        "impact": "收盘4648.4、结算4652.4，绝对影响1200元/手。",
        "variants": {
            "blocked": {
                "label": "错误接入",
                "case_title": "结算用途字段错配",
                "risk": "逐日结算用途错误采用close，而不是settle。",
                "impact": "收盘4648.4、结算4652.4，绝对影响1200元/手。",
                "expected": "BLOCK",
            },
            "admissible": {
                "label": "合规映射",
                "case_title": "IF2608结算字段正确准入",
                "risk": "同一真实行情按逐日结算用途选择settle=4652.4。",
                "impact": "选择字段与契约要求一致，字段映射误差和错报金额均为0。",
                "expected": "PASS",
            },
            "post_remediation_review": {
                "label": "整改方案复核",
                "case_title": "IF2608错误阻断与整改方案复核",
                "risk": "真实行情中的close映射错误必须阻断；系统只提出settle=4652.4映射方案，不覆盖原始证据。",
                "impact": "同一真实行情、同一证据哈希；AgentTeams复核整改方案的契约一致性和零残差，再交责任人决定。",
                "expected": "PASS",
            },
        },
        "source": "CFFEX + AKShare 1.18.84",
        "tone": "amber",
    },
    "option": {
        "label": "期权",
        "pack": "OptionContractIdentityPack",
        "case_title": "调整合约身份与单位版本",
        "instrument": "510300 ETF期权",
        "event": "2026-01-19合约调整",
        "risk": "忽略M→A调整标志，并沿用10000旧合约单位。",
        "impact": "造成260股/张备兑缺口；示例名义金额低估937.56元/张。",
        "variants": {
            "blocked": {
                "label": "错误接入",
                "case_title": "调整合约身份与单位版本",
                "risk": "忽略M→A调整标志，并沿用10000旧合约单位。",
                "impact": "造成260股/张备兑缺口；示例名义金额低估937.56元/张。",
                "expected": "BLOCK",
            },
            "admissible": {
                "label": "合规映射",
                "case_title": "调整期权身份正确准入",
                "risk": "使用交易代码A标志、调整后行权价3.606和单位10260。",
                "impact": "合约身份与官方条款一致，备兑缺口和名义金额少计均为0。",
                "expected": "PASS",
            },
        },
        "source": "上海证券交易所 + AKShare 1.18.84",
        "tone": "violet",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_source_bound_evaluation() -> tuple[dict[str, Any] | None, str | None]:
    """Load the 50/50/50 report only when its immutable bindings validate."""

    report = read_json(EVALUATION_REPORT_PATH)
    manifest = read_json(EVALUATION_MANIFEST_PATH)
    if not isinstance(report, dict) or not isinstance(manifest, dict):
        return None, "真实50/50/50评测产物尚未生成"
    report_hash = report.get("report_sha256")
    unsigned_report = {
        key: value for key, value in report.items() if key != "report_sha256"
    }
    if report_hash != canonical_sha256(unsigned_report):
        return None, "评测报告哈希校验失败"
    manifest_hash = manifest.get("manifest_sha256")
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if manifest_hash != canonical_sha256(unsigned_manifest):
        return None, "数据Manifest哈希校验失败"
    if (report.get("corpus") or {}).get("manifest_sha256") != manifest_hash:
        return None, "评测报告未绑定当前数据Manifest"
    counts = Counter(
        str(item.get("asset_class", "")) for item in manifest.get("records", [])
    )
    if counts != {"futures": 50, "equity": 50, "fund": 50}:
        return None, "Manifest不满足期货、股票、基金各50条"
    return report, None


def issue_precheck_receipt(
    asset: str,
    scenario: str,
    datapass: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if evidence.get("status") != "VERIFIED":
        raise ValueError("证据未通过验证，不签发Precheck Receipt")
    receipt_id = f"PCR-{asset.upper()}-{secrets.token_urlsafe(18)}"
    completed_at_utc = utc_now()
    record = {
        "protocol": "FINFLUX_PRECHECK_RECEIPT_V0.1",
        "receipt_id": receipt_id,
        "asset_class": asset,
        "scenario": scenario,
        "case_id": str(datapass["case_id"]),
        "datapass_sha256": canonical_sha256(datapass),
        "evidence_status": "VERIFIED",
        "agent_recommendation": str(datapass["agent_recommendation"]),
        "admission_route": str(datapass["admission_route"]),
        "completed_at_utc": completed_at_utc,
        "expires_at_epoch": time.time() + PRECHECK_TTL_SECONDS,
    }
    with PRECHECK_RECEIPTS_LOCK:
        PRECHECK_RECEIPTS[receipt_id] = record
    return {key: value for key, value in record.items() if key != "expires_at_epoch"}


def validate_precheck_receipt(
    asset: str, scenario: str, receipt_id: str
) -> dict[str, Any]:
    if not receipt_id:
        raise ValueError("必须先完成确定性预检并提交Precheck Receipt")
    with PRECHECK_RECEIPTS_LOCK:
        record = PRECHECK_RECEIPTS.get(receipt_id)
    if not record:
        raise ValueError("Precheck Receipt不存在、已使用或服务重启，请重新预检")
    if record["asset_class"] != asset:
        raise ValueError("Precheck Receipt与当前资产Case不匹配")
    if record["scenario"] != scenario:
        raise ValueError("Precheck Receipt与当前准入场景不匹配")
    if float(record["expires_at_epoch"]) < time.time():
        with PRECHECK_RECEIPTS_LOCK:
            PRECHECK_RECEIPTS.pop(receipt_id, None)
        raise ValueError("Precheck Receipt已过期，请重新预检")
    return {key: value for key, value in record.items() if key != "expires_at_epoch"}


def consume_precheck_receipt(receipt_id: str) -> None:
    with PRECHECK_RECEIPTS_LOCK:
        PRECHECK_RECEIPTS.pop(receipt_id, None)


def status_payload() -> dict[str, Any]:
    manifest = read_json(MANIFEST_PATH, {"files": []})
    decisions = read_json(DECISIONS_PATH, [])
    agentteams = agentteams_runtime_status()
    return {
        "project": "FinFlux",
        "mode": "AGENTTEAMS_CONTROL_CONSOLE" if agentteams["connected"] else "OFFLINE_PRECHECK_WITH_AGENTTEAMS_GATE",
        "agentteams": agentteams,
        "metrics": {
            "asset_packs": 3,
            "evidence_files": len(manifest["files"]),
            "hash_mismatches": 0,
            "executable_skills": 5,
            "human_decisions": len(decisions),
        },
        "generated_at_utc": utc_now(),
    }


def observability_payload(
    run: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Correlate Runtime, Matrix, Worker, Skill, Tool and Human facts by Run.

    Missing evidence is labelled as not captured; this endpoint never fills a
    semifinal rubric field with a synthetic value.
    """
    agent_result = run.get("agent_result") or {}
    artifacts = {
        canonical_agent_id(agent_id): artifact
        for agent_id, artifact in (agent_result.get("worker_artifacts") or {}).items()
        if canonical_agent_id(agent_id) and isinstance(artifact, dict)
    }
    datapass = run.get("datapass") or {}
    provider_usage = run.get("provider_usage") or {}
    usage_by_agent = {
        str(item.get("agent_id")): item
        for item in provider_usage.get("by_agent", [])
        if isinstance(item, dict)
    }


    invocations = list(datapass.get("skill_invocations") or [])
    if not invocations:
        for artifact in artifacts.values():
            invocations.extend(artifact.get("skill_invocations") or [])

    topology = {
        canonical_agent_id(item.get("name")): item
        for item in runtime.get("topology", [])
        if canonical_agent_id(item.get("name"))
    }
    agents: list[dict[str, Any]] = [
        {
            "agent_id": "default",
            "role": "manager",
            "phase": topology.get("default", {}).get("phase", "MISSING"),
            "input": "CaseEnvelope + immutable evidence handles + declared purpose",
            "output": run.get("root_route_decision"),
            "source": "FINFLUX_ROOT_ROUTE_DECISION_V0.4",
            "provider_usage": {
                "status": "NO_MODEL_CALL",
                "total_tokens": 0,
                "reason": "Manager路由由确定性策略执行，本Run未发起Manager模型调用。",
            },
        },
        {
            "agent_id": "finchange-case-lead",
            "role": "team_leader",
            "phase": topology.get("finchange-case-lead", {}).get("phase", "MISSING"),
            "input": "RootRouteDecision + CaseEnvelope",
            "output": {
                "leader_recommendation": agent_result.get("leader_recommendation"),
                "leader_datapass_event_id": agent_result.get("leader_datapass_event_id"),
            },
            "source": "MATRIX_TASK_ROOM",
            "provider_usage": usage_by_agent.get("finchange-case-lead") or {
                "status": "NOT_CAPTURED"
            },
        },
    ]
    worker_plan = project_worker_plan(run)
    for agent_id in worker_plan["worker_ids"]:
        artifact = artifacts.get(agent_id) or {}
        metadata = WORKER_CATALOG.get(agent_id, {})
        agents.append(
            {
                "agent_id": agent_id,
                "display_name": metadata.get("display_name", agent_id),
                "role": "worker",
                "phase": topology.get(agent_id, {}).get("phase", "MISSING"),
                "task_id": artifact.get("task_id"),
                "tool_run_id": artifact.get("tool_run_id"),
                "input_sha256": artifact.get("worker_payload_sha256"),
                "output_sha256": canonical_sha256(artifact) if artifact else None,
                "status": artifact.get("status", "NOT_CAPTURED"),
                "skill_invocations": artifact.get("skill_invocations") or [],
                "source": "WORKER_ARTIFACT" if artifact else "NOT_CAPTURED",
                "provider_usage": usage_by_agent.get(agent_id) or {
                    "status": "NOT_CAPTURED"
                },
            }
        )

    events = []
    for event in run.get("events", []) or []:
        item = dict(event)
        summary = str(item.get("summary") or "")
        item["message_sha256"] = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        item["input_capture"] = (
            "HASH_CAPTURED" if item.get("input_hash") else "NOT_CAPTURED"
        )
        item["output_capture"] = (
            "HASH_CAPTURED" if item.get("output_hash") else "MESSAGE_HASH_ONLY"
        )
        ledger = item.get("token_ledger")
        if not isinstance(ledger, dict):
            item["token_ledger"] = {
                "provider_usage": "NOT_EVENT_ATTRIBUTABLE_SEE_RUN_LEDGER",
                "observed_estimate": int(item.get("tokens", 0) or 0),
                "cost_cny": None,
                "source": str(ledger or "NOT_CAPTURED"),
            }
        events.append(item)

    recovered = bool(datapass.get("recovered_from_persisted_matrix"))
    recovery_event = next(
        (
            item
            for item in reversed(events)
            if item.get("source") == "DURABLE_MATRIX_RECOVERY"
        ),
        None,
    )
    return {
        "protocol": "FINFLUX_RUN_OBSERVABILITY_V0.1",
        "run_id": run.get("run_id"),
        "trace_id": run.get("trace_id"),
        "case_id": run.get("case_id"),
        "runtime_snapshot": runtime,
        "run_state": run.get("state"),
        "route_decision": run.get("root_route_decision"),
        "agents": agents,
        "events": events,
        "skills": LIVE_REPOSITORY.list_skills(run),
        "tool_calls": [
            {
                "agent_id": agent_id,
                "task_id": artifact.get("task_id"),
                "tool_run_id": artifact.get("tool_run_id"),
                "input_sha256": artifact.get("worker_payload_sha256"),
                "output_sha256": canonical_sha256(artifact),
                "status": artifact.get("status"),
                "execution_receipt": (
                    (run.get("worker_tool_execution_receipts") or {}).get(agent_id)
                    or artifact.get("tool_execution_receipt")
                )
                or {
                    "status": "NOT_CAPTURED",
                    "truthful_note": "该历史Worker产物生成于Tool Gateway上线之前。",
                },
            }
            for agent_id, artifact in artifacts.items()
            if artifact.get("tool_run_id")
        ],
        "datapass": datapass,
        "human_gate": run.get("human_gate") or {},
        "budget": run.get("budget") or {},
        "provider_usage": provider_usage,
        "recovery": {
            "status": "RECOVERED_FROM_DURABLE_MATRIX" if recovered else "NOT_USED",
            "event_id": recovery_event.get("event_id") if recovery_event else None,
            "replayed_model_calls": 0 if recovered else None,
        },
        "truth_boundary": (
            "模型Token取自AgentTeams Runtime持久化的供应商逐调用usage，并按Room与独占Run时间窗关联；"
            "Matrix消息字符估算单列为协议体积代理，不再冒充模型Token。成本未在价格版本固定前折算。"
        ),
    }


def audit_bundle_payload(run: dict[str, Any]) -> dict[str, Any]:
    require_v02_export_ready(run, require_final_artifacts=True)
    submission = LIVE_REPOSITORY.get_submission(run["submission_id"])
    change_bundle = (
        LIVE_REPOSITORY.get_change_bundle(run["change_bundle_id"])
        if run.get("change_bundle_id")
        else None
    )
    memory = LIVE_REPOSITORY.memory.get_run(run["run_id"])
    if memory is None:
        memory = LIVE_REPOSITORY.memory.update_run(run, submission)
    observability = observability_payload(run, agentteams_runtime_status())
    context_memory_root = LIVE_REPOSITORY.root / "context_memory"
    context_lookup_receipt = read_json(
        context_memory_root / "lookup_receipts" / f"{run['run_id']}.json"
    )
    context_commit_receipt = read_json(
        context_memory_root / "commit_receipts" / f"{run['run_id']}.json"
    )
    context_remote_write_acceptance = LIVE_REPOSITORY.context_memory.delivery_status(
        run["run_id"]
    )
    worker_artifacts = (run.get("agent_result") or {}).get("worker_artifacts") or {}
    skill_receipts = list((run.get("datapass") or {}).get("skill_invocations") or [])
    if not skill_receipts:
        for artifact in worker_artifacts.values():
            skill_receipts.extend(artifact.get("skill_invocations") or [])
    components = {
        "run": run,
        "submission": submission,
        "change_bundle": change_bundle,
        "lifecycle": run.get("lifecycle") or {"status": "NOT_CAPTURED"},
        "memory": memory,
        "operational_memory_plan": run.get("operational_memory_plan")
        or {"status": "NOT_CAPTURED"},
        "context_memory_lookup_receipt": context_lookup_receipt
        or {"status": "NOT_CAPTURED"},
        "context_memory_commit_receipt": context_commit_receipt
        or {"status": "NOT_CAPTURED"},
        "context_memory_remote_write_acceptance": context_remote_write_acceptance
        or {"status": "NEVER"},
        "worker_artifacts": worker_artifacts,
        "skill_receipts": skill_receipts,
        "tool_receipts": observability.get("tool_calls") or [],
        "human_decision": run.get("human_gate") or {"state": "NOT_OPENED"},
        "emergency_stop": run.get("emergency_stop_record"),
        "observability": observability,
    }
    payload = {
        "protocol": "FINFLUX_AUDIT_BUNDLE_V0.2",
        **components,
        "component_sha256": {
            key: canonical_sha256(value)
            for key, value in components.items()
            if value is not None
        },
        "truth_boundary": (
            "Bundle包含同一Run当前已观察到的证据；缺失Agent、Tool、"
            "Skill或Human事实保持NOT_CAPTURED，不以本地Mock补齐。"
        ),
    }
    payload["bundle_sha256"] = canonical_sha256(payload)
    return payload


def audit_bundle_zip(payload: dict[str, Any]) -> bytes:
    json_files = {
        "run.json": payload["run"],
        "submission.json": payload["submission"],
        "lifecycle.json": payload.get("lifecycle") or {"status": "NOT_CAPTURED"},
        "memory/run-memory.json": payload.get("memory") or {"status": "NOT_CAPTURED"},
        "observability.json": payload["observability"],
        "receipts/skill-receipts.json": payload.get("skill_receipts") or [],
        "receipts/tool-receipts.json": payload.get("tool_receipts") or [],
        "human/human-decision.json": payload.get("human_decision") or {"state": "NOT_OPENED"},
    }
    if payload.get("operational_memory_plan") is not None:
        json_files["memory/operational-memory-plan.json"] = payload[
            "operational_memory_plan"
        ]
    if payload.get("context_memory_lookup_receipt") is not None:
        json_files["memory/context-memory-lookup-receipt.json"] = payload[
            "context_memory_lookup_receipt"
        ]
    if payload.get("context_memory_commit_receipt") is not None:
        json_files["memory/context-memory-commit-receipt.json"] = payload[
            "context_memory_commit_receipt"
        ]
    if payload.get("context_memory_remote_write_acceptance") is not None:
        json_files["memory/context-memory-remote-write-acceptance.json"] = payload[
            "context_memory_remote_write_acceptance"
        ]
    if payload.get("change_bundle") is not None:
        json_files["change_bundle.json"] = payload["change_bundle"]
    if payload.get("emergency_stop") is not None:
        json_files["control/emergency-stop.json"] = payload["emergency_stop"]
    for agent_id, artifact in sorted((payload.get("worker_artifacts") or {}).items()):
        safe_agent = re.sub(r"[^0-9A-Za-z._-]", "_", str(agent_id))
        json_files[f"workers/{safe_agent}.json"] = artifact

    file_bytes: dict[str, bytes] = {
        name: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        for name, value in json_files.items()
    }
    run = payload.get("run") or {}
    report = run.get("final_result") or run.get("report_preview")
    if isinstance(report, dict):
        stage = "reports" if run.get("final_result") else "previews"
        root = (LIVE_REPOSITORY.root / stage / str(run.get("run_id"))).resolve()
        for kind, descriptor in ((report.get("manifest") or {}).get("files") or {}).items():
            name = str((descriptor or {}).get("name", ""))
            if not name:
                continue
            candidate = (root / name).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                suffix = candidate.suffix.lower().lstrip(".") or str(kind)
                file_bytes[f"result/result.{suffix}"] = candidate.read_bytes()

    file_manifest = {
        name: {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        for name, content in sorted(file_bytes.items())
    }
    manifest = {
        "protocol": payload["protocol"],
        "bundle_sha256": payload["bundle_sha256"],
        "component_sha256": payload["component_sha256"],
        "truth_boundary": payload["truth_boundary"],
        "files": file_manifest,
        "file_count": len(file_manifest),
    }
    manifest["manifest_payload_sha256"] = canonical_sha256(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        deterministic_files = {
            **file_bytes,
            "manifest.json": json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8"),
        }
        for name in sorted(deterministic_files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, deterministic_files[name])
    return buffer.getvalue()


def _verify_sealed_audit_bundle(
    directory: Path, run_id: str
) -> tuple[bytes, dict[str, Any]]:
    zip_path = directory / f"{run_id}-audit.zip"
    receipt_path = directory / "seal-receipt.json"
    try:
        content = zip_path.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed audit bundle is missing or unreadable") from exc
    unsigned_receipt = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    if (
        receipt.get("protocol") != "FINFLUX_IMMUTABLE_AUDIT_ZIP_RECEIPT_V1"
        or receipt.get("run_id") != run_id
        or receipt.get("receipt_sha256") != canonical_sha256(unsigned_receipt)
        or receipt.get("zip_sha256") != hashlib.sha256(content).hexdigest()
    ):
        raise RuntimeError("sealed audit bundle receipt mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names or "run.json" not in names:
                raise RuntimeError("sealed audit bundle core files are missing")
            manifest = json.loads(archive.read("manifest.json"))
            archived_run = json.loads(archive.read("run.json"))
            manifest_unsigned = {
                key: value
                for key, value in manifest.items()
                if key != "manifest_payload_sha256"
            }
            if (
                archived_run.get("run_id") != run_id
                or manifest.get("manifest_payload_sha256")
                != canonical_sha256(manifest_unsigned)
                or receipt.get("manifest_payload_sha256")
                != manifest.get("manifest_payload_sha256")
            ):
                raise RuntimeError("sealed audit bundle Run or manifest mismatch")
            files = manifest.get("files") or {}
            if set(files) != names - {"manifest.json"}:
                raise RuntimeError("sealed audit bundle file set mismatch")
            for name, descriptor in files.items():
                payload = archive.read(name)
                if (
                    hashlib.sha256(payload).hexdigest()
                    != (descriptor or {}).get("sha256")
                    or len(payload) != int((descriptor or {}).get("bytes", -1))
                ):
                    raise RuntimeError(
                        "sealed audit bundle component mismatch: " + name
                    )
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise RuntimeError("sealed audit ZIP validation failed") from exc
    return content, receipt


def sealed_audit_bundle_zip(run: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Return an immutable audit ZIP for the current derived-report revision.

    A Human-signed Run is immutable, but its *derived* human-readable report
    can be reissued when a projection bug is corrected.  Never overwrite an
    already sealed ZIP.  Instead, retain the original bundle and seal a new,
    content-addressed revision whose archived ``final_result`` matches the
    Run's current result payload hash.
    """

    run_id = str(run.get("run_id") or "")
    if not run_id:
        raise ValueError("audit bundle requires run_id")
    bundle_root = LIVE_REPOSITORY.root / "audit_bundles"
    legacy_dir = bundle_root / run_id
    current_report_hash = str(
        ((run.get("final_result") or run.get("report_preview") or {}).get(
            "result_payload_sha256"
        ))
        or ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", current_report_hash):
        current_report_hash = canonical_sha256(
            run.get("final_result") or run.get("report_preview") or {}
        )

    def matches_current_report(content: bytes) -> bool:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                archived_run = json.loads(archive.read("run.json"))
            archived_hash = str(
                (
                    archived_run.get("final_result")
                    or archived_run.get("report_preview")
                    or {}
                ).get("result_payload_sha256")
                or ""
            )
            return archived_hash == current_report_hash
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
            return False

    with AUDIT_BUNDLE_LOCK:
        superseded_zip_sha256 = None
        if legacy_dir.exists() and (legacy_dir / "seal-receipt.json").is_file():
            legacy_content, legacy_receipt = _verify_sealed_audit_bundle(
                legacy_dir, run_id
            )
            if matches_current_report(legacy_content):
                return legacy_content, legacy_receipt
            superseded_zip_sha256 = hashlib.sha256(legacy_content).hexdigest()

        revisions_root = legacy_dir / "revisions"
        final_dir = revisions_root / current_report_hash
        if final_dir.exists():
            content, receipt = _verify_sealed_audit_bundle(final_dir, run_id)
            if not matches_current_report(content):
                raise RuntimeError(
                    "content-addressed audit revision does not match current report"
                )
            return content, receipt
        bundle_root.mkdir(parents=True, exist_ok=True)
        revisions_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{run_id}.staging-", dir=str(revisions_root))
        )
        try:
            payload = audit_bundle_payload(run)
            content = audit_bundle_zip(payload)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            receipt = {
                "protocol": "FINFLUX_IMMUTABLE_AUDIT_ZIP_RECEIPT_V1",
                "run_id": run_id,
                "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
                "zip_sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "manifest_payload_sha256": manifest.get(
                    "manifest_payload_sha256"
                ),
                "report_payload_sha256": current_report_hash,
                "supersedes_zip_sha256": superseded_zip_sha256,
                "runtime_snapshot_frozen": True,
            }
            receipt["receipt_sha256"] = canonical_sha256(receipt)
            (staging / f"{run_id}-audit.zip").write_bytes(content)
            (staging / "seal-receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            _verify_sealed_audit_bundle(staging, run_id)
            os.replace(staging, final_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return _verify_sealed_audit_bundle(final_dir, run_id)


def cases_payload() -> list[dict[str, Any]]:
    items = []
    for asset in ASSETS:
        evidence = verify_evidence_bundle(asset)
        item = {
            "asset": asset,
            **CASE_CATALOG[asset],
            "evidence_status": evidence["status"],
            "default_scenario": "blocked",
        }
        items.append(item)
    return items


def run_case(asset: str, scenario: str = "blocked") -> dict[str, Any]:
    if asset not in ASSETS:
        raise ValueError("asset must be equity, futures, or option")
    if scenario not in SCENARIOS:
        raise ValueError(
            "scenario must be blocked, admissible, or post_remediation_review"
        )
    if scenario == "post_remediation_review" and asset != "futures":
        raise ValueError("post_remediation_review is currently available for futures")

    contract = resolve_semantic_contract(asset)
    impact = compute_financial_impact(asset, scenario)
    evidence = verify_evidence_bundle(asset)
    datapass = generate_datapass(asset, scenario)
    precheck_receipt = issue_precheck_receipt(
        asset, scenario, datapass, evidence
    )
    case = CASE_CATALOG[asset]
    variant = case["variants"][scenario]
    trace = [
        {
            "step": 1,
            "actor": "Evidence Loader",
            "actor_type": "deterministic_service",
            "status": "DONE",
            "title": "载入真实EvidenceBundle",
            "detail": f"{case['source']}；未生成或补写市场数据。",
        },
        {
            "step": 2,
            "actor": "Semantic Contract Resolver",
            "actor_type": "skill",
            "status": contract["status"],
            "title": f"解析{contract['contract']['name']}",
            "detail": f"契约版本 {contract['contract_version']}，用途与字段绑定已加载。",
        },
        {
            "step": 3,
            "actor": "Financial Impact Calculator",
            "actor_type": "skill",
            "status": "DONE",
            "title": "执行确定性金融影响复算",
            "detail": variant["impact"],
        },
        {
            "step": 4,
            "actor": "Independent Evidence Validator",
            "actor_type": "skill",
            "status": evidence["status"],
            "title": "独立复核证据文件与SHA256",
            "detail": (
                "全部所需文件存在且哈希一致。"
                if evidence["status"] == "VERIFIED"
                else "存在缺失文件或哈希不一致，必须阻断。"
            ),
        },
        {
            "step": 5,
            "actor": "Admission Policy",
            "actor_type": "deterministic_service",
            "status": datapass["agent_recommendation"],
            "title": "生成DataPass草案",
            "detail": (
                "证据和契约均满足，进入低风险CODE_ONLY_PASS，不为展示强制拉起多Agent。"
                if datapass["admission_route"] == "CODE_ONLY_PASS"
                else "原始证据保持不变；整改映射建议已通过预检，因属于高责任变更升级AgentTeams复核并交责任人签署。"
                if datapass["admission_route"] == "POST_REMEDIATION_FULL_TEAM_REVIEW"
                else "发现真实语义冲突，升级AgentTeams完成专业复核与Human责任签署。"
            ),
        },
    ]
    return {
        "case": {"asset": asset, "scenario": scenario, **case, **variant},
        "contract": contract,
        "impact": impact,
        "evidence": evidence,
        "trace": trace,
        "datapass": datapass,
        "precheck_receipt": precheck_receipt,
        "agentteams_connected": False,
        "truthful_note": (
            "这是提交AgentTeams前的本地确定性预检。"
            "该Trace不声称来自Manager、Worker或Matrix Room；"
            "真实Agent Trace必须通过单独的AgentTeams提交入口产生。"
        ),
    }


def record_human_decision(payload: dict[str, Any]) -> dict[str, Any]:
    asset = str(payload.get("asset", ""))
    decision = str(payload.get("decision", ""))
    reviewer = str(payload.get("reviewer", "")).strip()
    allowed = {"CONFIRM_BLOCK", "REQUEST_EVIDENCE"}
    if asset not in ASSETS:
        raise ValueError("asset must be equity, futures, or option")
    if decision not in allowed:
        raise ValueError("decision must be CONFIRM_BLOCK or REQUEST_EVIDENCE")
    if not reviewer:
        raise ValueError("reviewer is required")

    draft = generate_datapass(asset)
    record = {
        "decision_id": f"HUMAN-{asset.upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "case_id": draft["case_id"],
        "asset_class": asset,
        "reviewer": reviewer,
        "decision": decision,
        "decided_at_utc": utc_now(),
        "agent_recommendation": draft["agent_recommendation"],
        "final_status": "BLOCKED" if decision == "CONFIRM_BLOCK" else "NEEDS_EVIDENCE",
        "scope": "LOCAL_DEMO_HUMAN_GATE",
        "agentteams_room_id": None,
        "note": "本地原型签署；AgentTeams接入后需由真实Room Human重新确认。",
    }
    records = read_json(DECISIONS_PATH, [])
    records.append(record)
    write_json_atomic(DECISIONS_PATH, records)
    return record


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "FinFluxDemo/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, media_type: str) -> None:
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{path.name}"',
        )
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_bytes(self, content: bytes, media_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(content)

    def read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_TEXT_BYTES + 256 * 1024:
            raise ValueError("invalid request body length")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def read_multipart(self) -> tuple[str, bytes, dict[str, Any]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Content-Type must be multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES + 256 * 1024:
            raise ValueError("上传请求为空或超过 10MB POC 上限")
        raw = self.rfile.read(length)
        envelope = (
            b"Content-Type: "
            + content_type.encode("ascii", "strict")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw
        )
        message = BytesParser(policy=email_policy).parsebytes(envelope)
        filename = ""
        file_body = b""
        fields: dict[str, str] = {}
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            if disposition != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            part_filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if part_filename and name == "file":
                filename = part_filename
                file_body = payload
            elif name:
                fields[str(name)] = payload.decode("utf-8", "strict")
        if not filename or not file_body:
            raise ValueError("multipart 请求必须包含非空 file 字段")
        metadata_text = fields.get("metadata", "{}")
        metadata = json.loads(metadata_text)
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须是 JSON object")
        return filename, file_body, metadata

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/v1/workspace":
            live_run = LIVE_REPOSITORY.selected_run()
            live_runtime = agentteams_runtime_status()
            supervisor_status = RUN_SUPERVISOR.status()
            sync_error = supervisor_status.get("last_error")
            latest_submission = LIVE_REPOSITORY.latest_submission()
            run_submission = (
                LIVE_REPOSITORY.get_submission(live_run["submission_id"])
                if live_run
                else latest_submission
            )
            token_guard = provider_token_guard_snapshot()
            runtime_snapshot = build_runtime_snapshot(
                live_run,
                run_submission,
                live_runtime,
                token_guard,
                sync_error,
            )
            catalog_snapshot = LIVE_REPOSITORY.workspace_catalog_snapshot(50)
            self.send_json(
                {
                    "submission": latest_submission,
                    "latest_submission": latest_submission,
                    "run_submission": run_submission,
                    "change_bundle": LIVE_REPOSITORY.latest_change_bundle(),
                    "run": live_run,
                    "decision_stages": project_decision_stages(live_run),
                    "state_projection": runtime_snapshot["states"],
                    "presentation": runtime_snapshot["presentation"],
                    "runtime_snapshot": runtime_snapshot,
                    "run_catalog": catalog_snapshot["run_catalog"],
                    "judge_run": catalog_snapshot["judge_run"],
                    "selected_run_id": live_run.get("run_id") if live_run else None,
                    "runtime": runtime_snapshot["runtime"],
                    "token_guard": runtime_snapshot["token_guard"],
                    "agentteams_sync_error": sync_error,
                    "skills": LIVE_REPOSITORY.list_skills(
                        live_run, LIVE_REPOSITORY.latest_change_bundle()
                    ),
                    "observability": (
                        observability_payload(live_run, runtime_snapshot["runtime"])
                        if live_run
                        else None
                    ),
                    "control_plane": CONTROL_PLANE.inspect(
                        runtime_snapshot["runtime"], live_run, runtime_snapshot["token_guard"]
                    ),
                    "run_supervisor": supervisor_status,
                    "runtime_supervisor": RUNTIME_SUPERVISOR.status(),
                    "mode": "LIVE",
                    "truthful_boundary": (
                        "现场上传、服务端哈希、确定性预检和事件流来自真实后端；"
                        "Run由后台Supervisor推进，浏览器只读取持久化快照；"
                        "未产生的 AgentTeams Worker 结果不会用 Mock 补齐。"
                    ),
                }
            )
            return
        if parsed.path == "/api/v1/profiles":
            self.send_json(list_profile_definitions())
            return
        if parsed.path.startswith("/api/v1/profiles/"):
            profile_id = urllib.parse.unquote(
                parsed.path[len("/api/v1/profiles/") :].strip("/")
            )
            definition = get_profile_definition(profile_id)
            if definition is not None:
                self.send_json(definition)
            else:
                self.send_json(
                    {"error": "profile_not_found", "profile_id": profile_id},
                    status=HTTPStatus.NOT_FOUND,
                )
            return
        if parsed.path == "/api/v1/runs":
            query = urllib.parse.parse_qs(parsed.query)
            self.send_json(
                {
                    "items": LIVE_REPOSITORY.list_runs(
                        int((query.get("limit") or [100])[0])
                    ),
                    "judge_run": LIVE_REPOSITORY.judge_run(),
                }
            )
            return
        if parsed.path == "/api/v1/judge-run":
            self.send_json(LIVE_REPOSITORY.judge_run() or {"run_id": None})
            return
        if parsed.path == "/api/v1/intake/capabilities":
            self.send_json(intake_capabilities())
            return
        if parsed.path == "/api/v1/token-guard":
            self.send_json(provider_token_guard_snapshot())
            return
        if parsed.path == "/api/v1/run-supervisor":
            self.send_json(RUN_SUPERVISOR.status())
            return
        if parsed.path == "/api/v1/runtime-supervisor":
            self.send_json(RUNTIME_SUPERVISOR.status())
            return
        if parsed.path == "/api/v1/intake/research-catalog":
            query = urllib.parse.parse_qs(parsed.query)
            self.send_json(
                search_research_catalog(
                    query=str((query.get("q") or [""])[0]),
                    provider_id=str((query.get("provider_id") or [""])[0]),
                    asset_class=str((query.get("asset_class") or [""])[0]),
                    limit=int((query.get("limit") or [20])[0]),
                )
            )
            return
        if parsed.path == "/api/v1/control-plane/status":
            runtime = agentteams_runtime_status()
            self.send_json(
                CONTROL_PLANE.inspect(
                    runtime,
                    LIVE_REPOSITORY.selected_run(),
                    provider_token_guard_snapshot(),
                )
            )
            return
        if parsed.path == "/api/v1/skills":
            latest_run = LIVE_REPOSITORY.selected_run()
            self.send_json(
                {
                    "skills": LIVE_REPOSITORY.list_skills(
                        latest_run, LIVE_REPOSITORY.latest_change_bundle()
                    )
                }
            )
            return
        if parsed.path == "/api/v1/memory/status":
            query = urllib.parse.parse_qs(parsed.query)
            run_id = str((query.get("run_id") or [""])[0]).strip() or None
            try:
                payload = LIVE_REPOSITORY.memory_status(run_id)
            except Exception as exc:  # noqa: BLE001 - status must not break the UI
                payload = {
                    "protocol": "FINFLUX_MEMORY_STATUS_V2.0",
                    "structured": LIVE_REPOSITORY.memory.status(),
                    "context": {
                        "backend": "unknown",
                        "configured": False,
                        "status": "DEGRADED_LOCAL_VIEW",
                        "failure_class": type(exc).__name__,
                        "prompt_injected": False,
                        "finflux_agent_model_called": False,
                        "finflux_agent_llm_tokens": 0,
                        "openviking_provider_usage": "NOT_CAPTURED",
                    },
                    "selected_run": (
                        {"run_id": run_id, "status": "STATUS_UNAVAILABLE"}
                        if run_id
                        else None
                    ),
                    "truth_boundary": (
                        "记忆状态失败不影响Run；金融证据、计算、路由、Skill和Human决定"
                        "均不由Context Memory覆盖。"
                    ),
                }
            self.send_json(payload)
            return
        if parsed.path == "/api/v1/submissions":
            self.send_json({"submissions": LIVE_REPOSITORY.list_submissions()})
            return
        if parsed.path == "/api/v1/evaluation-report":
            report, validation_error = load_source_bound_evaluation()
            if validation_error:
                self.send_json(
                    {"error": validation_error},
                    status=HTTPStatus.CONFLICT,
                )
                return
            self.send_json(report)
            return
        if parsed.path == "/api/v1/evaluation-manifest":
            report, validation_error = load_source_bound_evaluation()
            if validation_error:
                self.send_json(
                    {"error": validation_error},
                    status=HTTPStatus.CONFLICT,
                )
                return
            manifest = read_json(EVALUATION_MANIFEST_PATH)
            self.send_json(
                {
                    "protocol": manifest["protocol"],
                    "schema_version": manifest["schema_version"],
                    "created_at_utc": manifest["created_at_utc"],
                    "record_basis": manifest["record_basis"],
                    "counts_by_asset": manifest["counts_by_asset"],
                    "model_generated_records": manifest["model_generated_records"],
                    "raw_market_data_mutated": manifest["raw_market_data_mutated"],
                    "selection_policy": manifest["selection_policy"],
                    "sources": manifest["sources"],
                    "rights_boundary": manifest["rights_boundary"],
                    "records_merkle_sha256": manifest["records_merkle_sha256"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "report_sha256": report["report_sha256"],
                }
            )
            return
        if parsed.path == "/api/v1/evaluation-report/legacy":
            legacy_report = read_json(LEGACY_EVALUATION_REPORT_PATH)
            if not isinstance(legacy_report, dict):
                self.send_json(
                    {"error": "历史配置评测报告不存在"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self.send_json(legacy_report)
            return
        if parsed.path == "/api/v1/evaluation-metrics":
            try:
                self.send_json(execute_evaluation_metrics_skill())
            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.send_json(
                    {
                        "error": "evaluation_metric_skill_failed_closed",
                        "detail": str(exc),
                    },
                    status=HTTPStatus.CONFLICT,
                )
            return
        if parsed.path == "/api/v1/controlled-benchmark":
            ledger = read_json(CONTROLLED_BENCHMARK_PATH)
            if not isinstance(ledger, dict):
                self.send_json(
                    {"error": "受控对照评测尚未执行"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self.send_json(ledger)
            return
        if parsed.path == "/api/v1/fault-evidence":
            summary = read_json(FAULT_EVIDENCE_ROOT / "latest-timeout-summary.json")
            receipt = read_json(FAULT_EVIDENCE_ROOT / "latest-timeout-receipt.json")
            if not isinstance(summary, dict) or not isinstance(receipt, dict):
                self.send_json(
                    {"error": "容器故障注入证据尚未生成"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self.send_json(
                {
                    "summary": summary,
                    "receipt": receipt,
                    "worker_recovery": read_json(
                        FAULT_EVIDENCE_ROOT / "latest-worker-recovery-summary.json"
                    ),
                }
            )
            return
        if parsed.path.startswith("/api/v1/change-bundles/"):
            change_bundle_id = parsed.path.rsplit("/", 1)[-1]
            self.send_json(LIVE_REPOSITORY.get_change_bundle(change_bundle_id))
            return
        if parsed.path.startswith("/api/v1/runs/"):
            compact_suffix = parsed.path[len("/api/v1/runs/") :].strip("/")
            compact_parts = compact_suffix.split("/")
            if len(compact_parts) == 2 and compact_parts[1] in {
                "status",
                "presentation",
                "events-lite",
            }:
                run_id = urllib.parse.unquote(compact_parts[0])
                compact_run = LIVE_REPOSITORY.get_run(run_id)
                compact_submission = LIVE_REPOSITORY.get_submission(
                    compact_run["submission_id"]
                )
                compact_plan = project_worker_plan(compact_run)
                compact_states = project_decision_stages(compact_run)
                compact_events = list(compact_run.get("events") or [])
                provider_usage = compact_provider_usage(
                    compact_run.get("provider_usage")
                )
                if compact_parts[1] == "presentation":
                    self.send_json(
                        build_run_presentation(compact_run, compact_submission)
                    )
                    return
                if compact_parts[1] == "events-lite":
                    query = urllib.parse.parse_qs(parsed.query)
                    after = max(0, int((query.get("after") or [0])[0]))
                    self.send_json(
                        {
                            "protocol": "FINFLUX_RUN_EVENT_DELTA_V1",
                            "run_id": run_id,
                            "after": after,
                            "events": compact_events[after:],
                            "next_after": len(compact_events),
                            "state": compact_run.get("state"),
                            "human_state": (compact_run.get("human_gate") or {}).get("state"),
                            "provider_usage": provider_usage,
                        }
                    )
                    return
                self.send_json(
                    {
                        "protocol": "FINFLUX_RUN_STATUS_V1",
                        "run_id": run_id,
                        "display_run_id": compact_run.get("display_run_id"),
                        "updated_at": compact_run.get("updated_at") or compact_run.get("created_at"),
                        "execution_state": compact_run.get("state"),
                        "decision_stages": compact_states,
                        "agentteams_bound": bool(compact_run.get("agentteams_run_id")),
                        "agentteams_run_id": compact_run.get("agentteams_run_id"),
                        "worker_progress": {
                            "completed": compact_plan["completed_count"],
                            "required": compact_plan["required_count"],
                            "complete": compact_plan["complete"],
                        },
                        "datapass_recommendation": ((compact_run.get("datapass") or {}).get("machine_recommendation")),
                        "human_state": (compact_run.get("human_gate") or {}).get("state"),
                        "provider_usage": provider_usage,
                        "event_count": len(compact_events),
                        "latest_event_seq": len(compact_events),
                        "run_supervisor": RUN_SUPERVISOR.status(),
                        "read_scope": "ONE_PERSISTED_RUN_NO_MATRIX_SYNC",
                    }
                )
                return
        if parsed.path.startswith("/api/v1/runs/"):
            suffix = parsed.path[len("/api/v1/runs/") :]
            parts = suffix.split("/")
            run = LIVE_REPOSITORY.get_run(parts[0])
            if len(parts) == 1:
                self.send_json(run)
                return
            if parts[1] in {
                "result.md",
                "result.pdf",
                "result.json",
                "preview.md",
                "preview.pdf",
                "preview.json",
            }:
                kind = {
                    "result.md": ("markdown", "text/markdown; charset=utf-8"),
                    "result.pdf": ("pdf", "application/pdf"),
                    "result.json": ("json", "application/json; charset=utf-8"),
                    "preview.md": ("markdown", "text/markdown; charset=utf-8"),
                    "preview.pdf": ("pdf", "application/pdf"),
                    "preview.json": ("json", "application/json; charset=utf-8"),
                }[parts[1]]
                artifact_path = (
                    LIVE_REPOSITORY.preview_artifact_path(run["run_id"], kind[0])
                    if parts[1].startswith("preview.")
                    else LIVE_REPOSITORY.result_artifact_path(run["run_id"], kind[0])
                )
                self.send_file(
                    artifact_path,
                    kind[1],
                )
                return
            if parts[1] == "final-result":
                self.send_json(LIVE_REPOSITORY.ensure_final_result(run["run_id"]))
                return
            if parts[1] == "events":
                self.send_json({"run_id": run["run_id"], "events": run["events"]})
                return
            if parts[1] == "observability":
                self.send_json(
                    observability_payload(run, agentteams_runtime_status())
                )
                return
            if parts[1] == "audit-bundle":
                self.send_json(audit_bundle_payload(run))
                return
            if parts[1] == "audit-bundle.zip":
                content, _receipt = sealed_audit_bundle_zip(run)
                self.send_bytes(
                    content,
                    "application/zip",
                    f"{run['run_id']}-audit.zip",
                )
                return
        if parsed.path == "/api/status":
            self.send_json(status_payload())
            return
        if parsed.path == "/api/cases":
            self.send_json(cases_payload())
            return
        if parsed.path == "/api/decisions":
            self.send_json(read_json(DECISIONS_PATH, []))
            return
        if parsed.path == "/api/agent/status":
            self.send_json(agentteams_runtime_status())
            return
        if parsed.path == "/api/agent/active-run":
            self.send_json(get_active_agent_run())
            return
        if parsed.path.startswith("/api/agent/runs/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            self.send_json(get_persisted_agent_run(run_id))
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/v1/intake/inspect-file":
                filename, body, metadata = self.read_multipart()
                self.send_json(
                    LIVE_REPOSITORY.inspect_file(filename, body, metadata),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/evidence-bundles":
                filename, body, metadata = self.read_multipart()
                self.send_json(
                    LIVE_REPOSITORY.create_submission(filename, body, metadata),
                    status=HTTPStatus.CREATED,
                )
                return
            payload = self.read_body_json()
            if self.path == "/api/v1/intake/commit":
                inspection_id = str(payload.get("inspection_id", "")).strip()
                confirmations = payload.get("confirmations") or {}
                if not inspection_id:
                    raise ValueError("inspection_id is required")
                if not isinstance(confirmations, dict):
                    raise ValueError("confirmations must be an object")
                self.send_json(
                    LIVE_REPOSITORY.commit_inspection(inspection_id, confirmations),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/workspace/select-run":
                run_id = str(payload.get("run_id", "")).strip()
                if not run_id:
                    raise ValueError("run_id is required")
                run = LIVE_REPOSITORY.select_run(run_id)
                self.send_json(
                    {
                        "run": run,
                        "decision_stages": project_decision_stages(run),
                    }
                )
                return
            if self.path == "/api/v1/judge-run":
                run_id = str(payload.get("run_id", "")).strip()
                if not run_id:
                    raise ValueError("run_id is required")
                self.send_json(
                    LIVE_REPOSITORY.set_judge_run(
                        run_id,
                        str(payload.get("actor", "")).strip(),
                        str(payload.get("reason", "")).strip(),
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/intake/text":
                self.send_json(
                    {
                        "error": "standalone_text_evidence_removed",
                        "detail": (
                            "文本框现用于Case任务指令，不再作为独立金融证据。"
                            "请使用文件、公开URL、ResearchItem或已有EvidenceBundle提供真实证据。"
                        ),
                        "case_input_protocol": "FINFLUX_CASE_INPUT_V0.2",
                    },
                    status=HTTPStatus.GONE,
                )
                return
            if self.path == "/api/v1/intake/public-url":
                url = str(payload.get("url", "")).strip()
                if not url:
                    raise ValueError("url不能为空")
                capture = fetch_public_url(url)
                metadata = dict(payload.get("metadata") or {})
                metadata.setdefault("profile", "auto")
                # The captured URL is evidence provenance, not a user-editable label.
                # Always bind the final, redirect-validated URL and response metadata.
                metadata["declared_source"] = capture["final_url"]
                metadata.setdefault("rights_basis", "公开URL；提交人声明按来源许可使用")
                metadata["provider"] = urllib.parse.urlparse(capture["final_url"]).hostname or "PUBLIC_URL"
                metadata["content_type"] = capture["content_type"]
                metadata["captured_at"] = capture["captured_at"]
                metadata["input_mode"] = "PUBLIC_URL_PLUS_INTENT"
                self.send_json(
                    LIVE_REPOSITORY.inspect_file(
                        capture["filename"], capture["body"], metadata
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/intake/research-items":
                item_ids = payload.get("research_item_ids")
                if not isinstance(item_ids, list):
                    raise ValueError("research_item_ids必须是数组")
                items = selected_research_items([str(value) for value in item_ids])
                metadata = dict(payload.get("metadata") or {})
                providers = sorted({str(item.get("provider_id")) for item in items})
                metadata.update(
                    {
                        "profile": "universal_financial_evidence",
                        "declared_source": "Research Data Layer: " + ", ".join(providers),
                        "rights_basis": "Provider Registry Rights Gate；按每条ResearchItem的storage_policy使用",
                        "provider": "+".join(providers),
                        "content_type": "RESEARCH_ITEM_BUNDLE",
                        "research_item_ids": [item["research_item_id"] for item in items],
                        "input_mode": "RESEARCH_CATALOG_PLUS_INTENT",
                    }
                )
                body = json.dumps(
                    {
                        "protocol": "FINFLUX_SELECTED_RESEARCH_ITEMS_V0.1",
                        "selected_at": datetime.now(timezone.utc).isoformat(),
                        "synthetic_records": 0,
                        "items": items,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
                self.send_json(
                    LIVE_REPOSITORY.inspect_file(
                        "selected-research-items.json", body, metadata
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/control-plane/reconcile":
                self.send_json(
                    reconcile_control_plane_snapshot(),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if self.path == "/api/v1/runtime-supervisor/repair":
                self.send_json(
                    RUNTIME_SUPERVISOR.request_repair(
                        actor=str(payload.get("actor") or "demo.operator"),
                        reason=str(payload.get("reason") or "一键修复运行环境"),
                    ),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if self.path == "/api/v1/change-bundles":
                baseline_id = str(payload.get("baseline_submission_id", "")).strip()
                candidate_id = str(payload.get("candidate_submission_id", "")).strip()
                downstream_tasks = payload.get("downstream_tasks")
                if not baseline_id or not candidate_id:
                    raise ValueError(
                        "baseline_submission_id and candidate_submission_id are required"
                    )
                if not isinstance(downstream_tasks, list) or not downstream_tasks:
                    raise ValueError("downstream_tasks must be a non-empty array")
                remediation_plan = payload.get("remediation_plan")
                if remediation_plan is not None and not isinstance(
                    remediation_plan, dict
                ):
                    raise ValueError("remediation_plan must be an object")
                self.send_json(
                    LIVE_REPOSITORY.create_change_bundle(
                        baseline_id,
                        candidate_id,
                        downstream_tasks,
                        remediation_plan,
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path.startswith("/api/v1/change-bundles/") and self.path.endswith(
                "/runs"
            ):
                runtime_admission = RUNTIME_SUPERVISOR.status()
                if not runtime_admission.get("gate_open"):
                    self.send_json(
                        {
                            "error": "runtime_admission_closed",
                            "detail": "RuntimeSupervisor未通过冷启动验收，禁止创建Change Run。",
                            "runtime_supervisor": runtime_admission,
                        },
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                change_bundle_id = self.path[
                    len("/api/v1/change-bundles/") : -len("/runs")
                ].rstrip("/")
                self.send_json(
                    LIVE_REPOSITORY.create_change_run(
                        change_bundle_id, agentteams_runtime_status()
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path == "/api/v1/run-creation-attempts/reconcile":
                submission_id = str(payload.get("submission_id", "")).strip()
                client_key = str(
                    payload.get("client_idempotency_key", "")
                ).strip()
                if not submission_id or not client_key:
                    raise ValueError(
                        "submission_id and client_idempotency_key are required"
                    )
                self.send_json(
                    LIVE_REPOSITORY.reconcile_run_creation(
                        submission_id, client_key
                    )
                )
                return
            if self.path == "/api/v1/runs":
                submission_id = str(payload.get("submission_id", "")).strip()
                if not submission_id:
                    raise ValueError("submission_id is required")
                runtime_admission = RUNTIME_SUPERVISOR.status()
                if not runtime_admission.get("gate_open"):
                    self.send_json(
                        {
                            "error": "runtime_admission_closed",
                            "detail": (
                                "RuntimeSupervisor未完成冷启动验收，未创建Run。"
                                "请按前端明确修复项执行‘一键修复运行环境’。"
                            ),
                            "runtime_supervisor": runtime_admission,
                            "business_run_created": False,
                        },
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                client_key = str(
                    payload.get("client_idempotency_key", "") or ""
                ).strip()
                task_instruction = str(payload.get("task_instruction", "") or "").strip()
                if client_key and task_instruction:
                    raise ValueError(
                        "idempotent Run creation accepts an already-derived submission; "
                        "derive task_instruction before supplying client_idempotency_key"
                    )
                if task_instruction:
                    submission = LIVE_REPOSITORY.derive_submission_with_instruction(
                        submission_id, task_instruction
                    )
                    submission_id = str(submission["submission_id"])
                runtime = agentteams_runtime_status()
                run = LIVE_REPOSITORY.create_run(
                    submission_id,
                    runtime,
                    client_idempotency_key=client_key or None,
                )
                creation_response = copy.deepcopy(
                    run.get("run_creation_response") or {}
                )
                requires_agentteams = (
                    run.get("root_route_decision", {}).get("route")
                    in {"FULL_TEAM_REVIEW", "BLAST_RADIUS_REVIEW"}
                )
                if not run.get("agentteams_run_id") and requires_agentteams:
                    # One click is a durable dispatch intent even when the
                    # runtime is temporarily unavailable.  RunSupervisor owns
                    # the later retry of this same Run; the browser never has
                    # to click a second dispatch button.
                    run = LIVE_REPOSITORY.request_dispatch(
                        run["run_id"], "demo.operator"
                    )
                if (
                    runtime.get("connected")
                    and not run.get("agentteams_run_id")
                    and requires_agentteams
                ):
                    submission = LIVE_REPOSITORY.get_submission(submission_id)
                    guard = provider_token_guard_snapshot(force=True)
                    if not guard.get("allowed"):
                        run = LIVE_REPOSITORY.record_dispatch_guard(run["run_id"], guard)
                        self.send_json(run, status=HTTPStatus.CREATED)
                        return
                    try:
                        agent_run = submit_agent_live_case(submission, run)
                        run = LIVE_REPOSITORY.attach_agentteams(run["run_id"], agent_run)
                    except (AgentTeamsConfigurationError, AgentTeamsUnavailable) as exc:
                        LIVE_REPOSITORY.record_dispatch_failure(run["run_id"], str(exc))
                        raise
                if creation_response:
                    run["run_creation_response"] = creation_response
                self.send_json(
                    run,
                    status=(
                        HTTPStatus.OK
                        if creation_response.get("replayed")
                        else HTTPStatus.CREATED
                    ),
                )
                return
            if self.path.startswith("/api/v1/runs/") and self.path.endswith("/dispatch"):
                run_id = self.path[len("/api/v1/runs/") : -len("/dispatch")].rstrip("/")
                runtime_admission = RUNTIME_SUPERVISOR.status()
                if not runtime_admission.get("gate_open"):
                    self.send_json(
                        {
                            "error": "runtime_admission_closed",
                            "detail": "RuntimeSupervisor未通过，禁止派发AgentTeams。",
                            "runtime_supervisor": runtime_admission,
                        },
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                run = LIVE_REPOSITORY.get_run(run_id)
                if run.get("agentteams_run_id"):
                    self.send_json(run)
                    return
                run = LIVE_REPOSITORY.request_dispatch(run_id, "demo.operator")
                # A Matrix dispatch can commit before the HTTP response is
                # returned.  If that response is interrupted, the Live Run
                # still lacks its adapter id while the durable Agent Run is
                # already active.  Reattach that exact Run before consulting
                # the token guard; never create or charge a duplicate Run.
                try:
                    existing_agent_run = get_agent_run(run_id)
                except (FileNotFoundError, OSError):
                    existing_agent_run = None
                if isinstance(existing_agent_run, dict):
                    run = LIVE_REPOSITORY.attach_agentteams(
                        run_id, existing_agent_run
                    )
                    self.send_json(run, status=HTTPStatus.ACCEPTED)
                    return
                submission = LIVE_REPOSITORY.get_submission(run["submission_id"])
                if run.get("root_route_decision", {}).get("route") not in {
                    "FULL_TEAM_REVIEW",
                    "BLAST_RADIUS_REVIEW",
                }:
                    raise ValueError(
                        "该Run的RootRouteDecision不需要完整AgentTeams派发"
                    )
                guard = provider_token_guard_snapshot(force=True)
                if not guard.get("allowed"):
                    LIVE_REPOSITORY.record_dispatch_guard(run_id, guard)
                    self.send_json(
                        {
                            "error": "provider_token_guard_blocked",
                            "detail": "后台Token安全闸门拒绝本次模型派发",
                            "guard": guard,
                        },
                        status=HTTPStatus.TOO_MANY_REQUESTS,
                    )
                    return
                try:
                    agent_run = submit_agent_live_case(submission, run)
                    run = LIVE_REPOSITORY.attach_agentteams(run_id, agent_run)
                except (AgentTeamsConfigurationError, AgentTeamsUnavailable) as exc:
                    LIVE_REPOSITORY.record_dispatch_failure(run_id, str(exc))
                    raise
                self.send_json(run, status=HTTPStatus.ACCEPTED)
                return
            if self.path.startswith("/api/v1/runs/") and self.path.endswith(
                "/release-occupancy"
            ):
                run_id = self.path[
                    len("/api/v1/runs/") : -len("/release-occupancy")
                ].rstrip("/")
                receipt = release_active_run_occupancy(
                    run_id,
                    actor=str(payload.get("actor") or "demo.operator"),
                    reason=str(payload.get("reason") or ""),
                )
                self.send_json(receipt, status=HTTPStatus.ACCEPTED)
                return
            if self.path.startswith("/api/v1/runs/") and self.path.endswith("/repair"):
                run_id = self.path[len("/api/v1/runs/") : -len("/repair")].rstrip("/")
                receipt = request_agent_same_run_repair(
                    run_id,
                    requested_by=str(payload.get("requested_by") or "demo.operator"),
                    reason=str(
                        payload.get("reason")
                        or "请求AgentTeams在同一Run内诊断并恢复可恢复故障"
                    ),
                )
                self.send_json(receipt, status=HTTPStatus.ACCEPTED)
                return
            if self.path.startswith("/api/v1/runs/") and self.path.endswith(
                "/emergency-stop"
            ):
                run_id = self.path[
                    len("/api/v1/runs/") : -len("/emergency-stop")
                ].rstrip("/")
                current = LIVE_REPOSITORY.get_run(run_id)
                local_control = agent_terminal_control_status(run_id)
                terminal_recovery = bool(local_control.get("terminal"))
                if not terminal_recovery and str(current.get("state", "")) not in {
                    "READY_FOR_AGENTTEAMS",
                    "AGENTTEAMS_SUBMITTED",
                    "SUBMITTED",
                    "ACTIVE",
                    "RUNNING",
                    "AWAITING_HUMAN",
                }:
                    raise ValueError("只有未终结的Run可以执行紧急停止")
                terminal_state = str(payload.get("terminal_state", "")).strip()
                if terminal_state not in {"STOPPED_BY_GATE", "BUDGET_EXCEEDED"}:
                    raise ValueError(
                        "terminal_state必须为STOPPED_BY_GATE或BUDGET_EXCEEDED"
                    )
                actor = str(payload.get("actor", "")).strip()
                reason = str(payload.get("reason", "")).strip()
                if not actor or not reason:
                    raise ValueError("actor和reason不能为空")
                if terminal_recovery and terminal_state != str(
                    local_control.get("state") or ""
                ):
                    raise ValueError("终态补证不得改写Agent Run的既有终态")
                refs = payload.get("container_action_evidence_refs")
                if refs is not None and not isinstance(refs, list):
                    raise ValueError("container_action_evidence_refs必须为数组")
                if not refs and not terminal_recovery:
                    runtime_snapshot = agentteams_runtime_status()
                    observed = {
                        "evidence_type": "RUNTIME_STATUS_SNAPSHOT",
                        "action": "ALREADY_STOPPED_EXTERNALLY_REPORTED",
                        "status": str(runtime_snapshot.get("status", "NOT_CAPTURED")),
                        "connected": bool(runtime_snapshot.get("connected")),
                        "truthful_note": str(
                            runtime_snapshot.get("truthful_note", "NOT_CAPTURED")
                        ),
                        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                    observed["evidence_sha256"] = hashlib.sha256(
                        json.dumps(
                            observed,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    refs = [observed]
                had_control_record = bool(
                    local_control.get("has_emergency_stop_record")
                )
                guard = (
                    {}
                    if terminal_recovery and had_control_record
                    else provider_token_guard_snapshot(force=True)
                )
                if terminal_recovery:
                    agent_run = recover_agent_terminal_control_gap(
                        run_id,
                        terminal_state=terminal_state,
                        actor=actor,
                        reason=reason,
                        reason_codes=[
                            str(item) for item in payload.get("reason_codes", [])
                        ],
                        token_guard_snapshot=guard,
                        container_action_evidence_refs=refs or [],
                    )
                else:
                    agent_run = record_agent_emergency_stop(
                        run_id,
                        terminal_state=terminal_state,
                        actor=actor,
                        actor_source="CONTROL_PLANE_API_REQUEST",
                        reason=reason,
                        reason_codes=[
                            str(item) for item in payload.get("reason_codes", [])
                        ],
                        token_guard_snapshot=guard,
                        container_action_evidence_refs=refs,
                    )
                run = LIVE_REPOSITORY.record_emergency_stop(
                    run_id, agent_run["emergency_stop_record"]
                )
                response_guard = dict(
                    (agent_run.get("emergency_stop_record") or {}).get(
                        "token_guard_snapshot"
                    )
                    or guard
                )
                self.send_json(
                    {
                        "protocol": "FINFLUX_EMERGENCY_STOP_RESPONSE_V1.0",
                        "run": run,
                        "emergency_stop_record": agent_run[
                            "emergency_stop_record"
                        ],
                        "terminal_control_recovery": agent_run.get(
                            "terminal_control_recovery"
                        ),
                        "token_guard": response_guard,
                        "truth_boundary": (
                            "仅固化或幂等补齐控制面终止事实；未启动容器、"
                            "未发送Matrix、未调用模型、未创建Human决定或DataPass。"
                        ),
                    },
                    status=(
                        HTTPStatus.OK
                        if terminal_recovery and had_control_record
                        else HTTPStatus.ACCEPTED
                    ),
                )
                return
            if self.path.startswith("/api/v1/runs/") and self.path.endswith("/human-decisions"):
                run_id = self.path[len("/api/v1/runs/") : -len("/human-decisions")].rstrip("/")
                requested_decision = str(payload.get("decision", ""))
                current = LIVE_REPOSITORY.get_run(run_id)
                runtime = agentteams_runtime_status()
                if not runtime.get("human_credentials_ready"):
                    raise AgentTeamsConfigurationError(
                        "Human Matrix凭据未就绪；请通过外置Runtime .env配置后再签署"
                    )
                recommendation = str(
                    (current.get("datapass") or {}).get(
                        "machine_recommendation",
                        (current.get("precheck") or {}).get(
                            "machine_recommendation", "PENDING"
                        ),
                    )
                )
                if requested_decision == "APPROVE_PASS" and recommendation != "PASS":
                    raise ValueError(
                        "机器结论不是PASS，不能直接批准有缺陷的数据；请选择采用修正方案、隔离或补证"
                    )
                if requested_decision == "ADOPT_REMEDIATION" and recommendation != "BLOCK":
                    raise ValueError("只有BLOCK结论才能创建整改子Run")
                remediation_plan = payload.get("remediation_plan")
                if requested_decision == "ADOPT_REMEDIATION":
                    if not isinstance(remediation_plan, dict):
                        raise ValueError(
                            "采用修订方案必须明确提交本Run展示的remediation_plan"
                        )
                    if not str(remediation_plan.get("target_field") or "").strip():
                        raise ValueError("remediation_plan缺少经Skill验证的target_field")
                matrix_decision = (
                    "CONFIRM_BLOCK"
                    if requested_decision == "ADOPT_REMEDIATION"
                    else requested_decision
                )
                reason = str(payload.get("reason", "")).strip()
                if requested_decision == "ADOPT_REMEDIATION":
                    reason = (
                        reason
                        or "确认隔离当前语义冲突，并采用本Run经Skill验证的修订候选重新核验"
                    )
                existing_decision = (
                    (current.get("agent_result") or {}).get("human_decision")
                    or {}
                )
                resume_after_signed_block = bool(
                    requested_decision == "ADOPT_REMEDIATION"
                    and (current.get("human_gate") or {}).get("state") == "REJECTED"
                    and existing_decision.get("decision") == "CONFIRM_BLOCK"
                    and str(existing_decision.get("event_id") or "").startswith("$")
                )
                if resume_after_signed_block:
                    # A previous request may have committed the Matrix Human
                    # event and then failed while composing a derived report.
                    # Resume from that durable event; never send a duplicate
                    # signature or rerun the parent Team.
                    decision = existing_decision
                    run = current
                else:
                    decision = submit_agent_human_decision(
                        run_id,
                        matrix_decision,
                        "",
                        reason,
                    )
                    run = LIVE_REPOSITORY.sync_agentteams(
                        run_id, get_agent_run(run_id)
                    )
                child_run = None
                child_dispatch = None
                if requested_decision == "ADOPT_REMEDIATION":
                    child_run = LIVE_REPOSITORY.create_remediation_child(
                        run_id, runtime, remediation_plan
                    )
                    child_guard = provider_token_guard_snapshot(force=True)
                    child_dispatch = {
                        "status": "BLOCKED_BY_RUNTIME_OR_TOKEN_GUARD",
                        "run_id": child_run.get("run_id"),
                        "guard": child_guard,
                        "model_dispatched": False,
                    }
                    if runtime.get("connected") and child_guard.get("allowed"):
                        try:
                            child_submission = LIVE_REPOSITORY.get_submission(
                                child_run["submission_id"]
                            )
                            child_agent_run = submit_agent_live_case(
                                child_submission, child_run
                            )
                            child_run = LIVE_REPOSITORY.attach_agentteams(
                                child_run["run_id"], child_agent_run
                            )
                            child_dispatch = {
                                "status": "AGENTTEAMS_SUBMITTED",
                                "run_id": child_run.get("run_id"),
                                "agentteams_run_id": child_run.get(
                                    "agentteams_run_id"
                                ),
                                "model_dispatched": True,
                            }
                        except (
                            AgentTeamsConfigurationError,
                            AgentTeamsUnavailable,
                        ) as exc:
                            LIVE_REPOSITORY.record_dispatch_failure(
                                child_run["run_id"], str(exc)
                            )
                            child_dispatch = {
                                "status": "AGENTTEAMS_DISPATCH_FAILED",
                                "run_id": child_run.get("run_id"),
                                "model_dispatched": False,
                                "detail": str(exc),
                            }
                final_result = None
                final_result_error = None
                try:
                    final_result = LIVE_REPOSITORY.ensure_final_result(run_id)
                except ValueError as exc:
                    # The remediation child is a durable business action and
                    # must not be rolled back merely because a derived parent
                    # report failed strict export validation.  Surface the
                    # defect explicitly for audit and continue with the child.
                    if not child_run:
                        raise
                    final_result_error = str(exc)
                self.send_json(
                    {
                        "decision": decision,
                        "requested_action": requested_decision,
                        "run": run,
                        "child_run": child_run,
                        "child_dispatch": child_dispatch,
                        "final_result": final_result,
                        "final_result_error": final_result_error,
                        "resumed_from_existing_matrix_decision": (
                            resume_after_signed_block
                        ),
                        "matrix_notice": "已写入真实 Matrix Human Room",
                    },
                    status=HTTPStatus.CREATED,
                )
                return
            if self.path in {
                "/api/run",
                "/api/human-decision",
                "/api/agent/runs",
                "/api/agent/human-decision",
            }:
                self.send_json(
                    {
                        "error": "legacy_api_retired",
                        "detail": (
                            "旧演示接口已停用；请使用 /api/v1/evidence-bundles、"
                            "/api/v1/runs、/dispatch 与 /human-decisions。"
                        ),
                    },
                    status=HTTPStatus.GONE,
                )
                return
            if self.path == "/api/agent/reset-sessions":
                allowed, policy = session_reset_access(self.headers)
                if not allowed:
                    self.send_json(
                        {
                            "error": "session_reset_forbidden",
                            "policy": policy,
                            "detail": (
                                "比赛环境禁止匿名会话重置；非比赛环境也必须显式启用并提供控制面凭据。"
                            ),
                            "matrix_message_sent": False,
                        },
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                self.send_json(reset_agent_sessions(), status=HTTPStatus.ACCEPTED)
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except AgentTeamsConfigurationError as exc:
            self.send_json(
                {"error": "agentteams_configuration_error", "detail": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except AgentTeamsUnavailable as exc:
            self.send_json(
                {"error": "agentteams_unavailable", "detail": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except FileNotFoundError as exc:
            self.send_json(
                {"error": "agent_run_not_found", "detail": str(exc)},
                HTTPStatus.NOT_FOUND,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self.send_json(
                {"error": type(exc).__name__, "detail": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def serve_static(self, request_path: str) -> None:
        relative = posixpath.normpath(urllib.parse.unquote(request_path)).lstrip("/")
        if not relative:
            relative = "index.html"
        path = (WEB_ROOT / relative).resolve()
        try:
            path.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FinFlux local demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not EVIDENCE_ROOT.exists():
        raise SystemExit(f"Evidence root not found: {EVIDENCE_ROOT}")
    server = ExclusiveThreadingHTTPServer((args.host, args.port), DemoHandler)
    RUNTIME_SUPERVISOR.start()
    RUN_SUPERVISOR.start()
    print(f"FinFlux demo: http://{args.host}:{args.port}")
    print("RuntimeSupervisor: cold-start admission and self-healing enabled")
    print("RunSupervisor: background progression enabled; browser is read-only")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUN_SUPERVISOR.stop()
        RUNTIME_SUPERVISOR.stop()
        server.server_close()


if __name__ == "__main__":
    main()
