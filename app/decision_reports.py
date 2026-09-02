from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from profile_registry import get_profile


LIVE_INTAKE_ROOT = Path(__file__).resolve().parent / "runtime" / "live_intake"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _plain_outcome(run: dict[str, Any]) -> dict[str, str]:
    gate = run.get("human_gate") or {}
    state = str(gate.get("state", "NOT_OPENED"))
    recommendation = str(
        (run.get("datapass") or {}).get(
            "machine_recommendation",
            (run.get("precheck") or {}).get("machine_recommendation", "PENDING"),
        )
    )
    if state == "APPROVED":
        return {
            "code": "ADMITTED",
            "headline": "可以使用：已获准进入指定下游系统",
            "plain_reason": "机器核验、独立复核和负责人签署均已完成。",
            "next_action": "按本报告限定的用途和数据版本进入生产；如来源、字段或版本变化，必须创建新Run。",
        }
    if state == "REJECTED":
        return {
            "code": "QUARANTINED",
            "headline": "暂不能使用：该批数据已被隔离",
            "plain_reason": "负责人确认当前版本存在不可接受的金融语义风险，系统未允许其进入下游。",
            "next_action": "采用报告中的修正字段创建子Run，或更换证据后重新提交；不得覆盖原始文件。",
        }
    if state == "RETURNED":
        return {
            "code": "EVIDENCE_REQUIRED",
            "headline": "暂不能判断：需要补充证据",
            "plain_reason": "现有材料不足以支持安全准入，当前数据保持隔离。",
            "next_action": "补充来源、授权、规则公告或字段说明后创建新Run，并重新执行核验。",
        }
    if state == "AWAITING_HUMAN":
        return {
            "code": "HUMAN_ACTION_REQUIRED",
            "headline": "机器核验已完成：等待负责人处理",
            "plain_reason": (
                "机器建议为%s；系统已暂停自动流转，尚未形成最终生产授权。"
                % recommendation
            ),
            "next_action": "负责人选择批准、采用修正、隔离或退回补证后，系统才生成最终结论。",
        }
    return {
        "code": "NOT_FINAL",
        "headline": "尚未形成最终业务结论",
        "plain_reason": "AgentTeams协作或Human Gate尚未完成。",
        "next_action": "继续执行当前Run；系统不会用模拟结果补齐缺失环节。",
    }


def _text(value: Any, fallback: str = "未解析") -> str:
    """Render missing protocol values without turning ``None`` into a fact."""

    rendered = str(value or "").strip()
    return rendered if rendered and rendered.lower() != "none" else fallback


def _effective_date_from_immutable_evidence(submission: dict[str, Any]) -> str | None:
    """Read a missing date from the content-addressed source without mutating it.

    Older sealed futures Submissions did not recognise ``observation_date`` as
    a trade-date alias.  The report may recover the display value only after
    verifying the immutable object's SHA256.  The Submission/DataPass/Human
    records remain unchanged.
    """

    file_info = submission.get("file") or {}
    relative = str(file_info.get("immutable_object") or "").replace("\\", "/")
    expected_sha256 = str(file_info.get("sha256") or "").lower()
    if not relative or not expected_sha256 or not relative.startswith("objects/"):
        return None
    candidate = (LIVE_INTAKE_ROOT / Path(relative)).resolve()
    objects_root = (LIVE_INTAKE_ROOT / "objects").resolve()
    try:
        candidate.relative_to(objects_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    body = candidate.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        return None
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("gb18030")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None
    normalized = {str(name).strip().lower(): str(name) for name in reader.fieldnames}
    date_column = next(
        (
            normalized[name]
            for name in (
                "trade_date",
                "observation_date",
                "event_date",
                "nav_date",
                "effective_date",
                "published_at",
                "date",
                "交易日",
                "净值日期",
                "事件日期",
                "日期",
            )
            if name in normalized
        ),
        None,
    )
    if not date_column:
        return None
    instrument_column = next(
        (
            normalized[name]
            for name in (
                "instrument",
                "security_code",
                "fund_code",
                "symbol",
                "code",
                "证券代码",
                "基金代码",
                "合约代码",
            )
            if name in normalized
        ),
        None,
    )
    target = str(
        (submission.get("metadata") or {}).get("target_instrument")
        or (submission.get("parsed") or {}).get("instrument")
        or ""
    ).strip().upper()
    values = [
        str(row.get(date_column) or "").strip()
        for row in reader
        if not target
        or not instrument_column
        or str(row.get(instrument_column) or "").strip().upper() == target
    ]
    return next((value for value in reversed(values) if value), None)


def _profile_context(
    run: dict[str, Any], submission: dict[str, Any]
) -> tuple[dict[str, Any], str, str, str]:
    datapass = run.get("datapass") or {}
    profile_id = str(
        ((datapass.get("profile") or {}).get("profile_id"))
        or ((run.get("profile_resolution") or {}).get("profile_id"))
        or ""
    )
    profile = get_profile(profile_id) or {}
    metadata = submission.get("metadata") or {}
    purpose_id = str(
        ((datapass.get("declared_purpose") or {}).get("purpose_id"))
        or metadata.get("declared_purpose")
        or ""
    )
    purpose = (profile.get("purpose_bindings") or {}).get(purpose_id) or {}
    purpose_label = str(purpose.get("label") or purpose_id or "未声明")
    return profile, profile_id, purpose_id, purpose_label


def _semantic_truth(
    run: dict[str, Any], submission: dict[str, Any]
) -> dict[str, Any]:
    """Project report facts from sealed Worker/DataPass evidence.

    The dispatch-time precheck may intentionally contain ``AUTO_AGENT`` and an
    unresolved required field.  It is therefore only a compatibility fallback,
    never the authority after AgentTeams has sealed its Worker artifacts.
    """

    precheck = run.get("precheck") or {}
    datapass = run.get("datapass") or {}
    artifacts = ((run.get("agent_result") or {}).get("worker_artifacts") or {})
    semantic = artifacts.get("semantic-impact-analyst") or {}
    contract = semantic.get("contract") or {}
    impact = semantic.get("impact") or {}
    profile, _profile_id, _purpose_id, _purpose_label = _profile_context(
        run, submission
    )

    preserve_declared_conflict = (
        str(datapass.get("machine_recommendation") or "").upper() == "BLOCK"
        and str(precheck.get("machine_recommendation") or "").upper() == "BLOCK"
        and bool(precheck.get("candidate_mapping"))
        and bool(precheck.get("required_field"))
        and precheck.get("candidate_mapping") != precheck.get("required_field")
    )
    selected = (
        precheck.get("candidate_mapping") if preserve_declared_conflict else
        contract.get("candidate_mapping")
        or impact.get("selected_price_field")
        or precheck.get("candidate_mapping")
    )
    required = contract.get("required_field") or impact.get("required_field") or precheck.get("required_field")
    selected_value = (
        precheck.get(str(selected)) if preserve_declared_conflict else impact.get("selected_value")
    )
    required_value = (
        precheck.get(str(required)) if preserve_declared_conflict else impact.get("required_value")
    )
    if selected_value is None and selected:
        selected_value = precheck.get(str(selected))
    if required_value is None and required:
        required_value = precheck.get(str(required))

    metrics = ((datapass.get("impact_assessment") or {}).get("metrics") or [])
    purpose_id = str(
        ((datapass.get("declared_purpose") or {}).get("purpose_id"))
        or (submission.get("metadata") or {}).get("declared_purpose")
        or ""
    )
    metric_hint = (
        ((profile.get("purpose_bindings") or {}).get(purpose_id) or {})
        .get("impact_metric", {})
    )
    preferred = list(metric_hint.get("value_paths") or [])
    metric = next(
        (item for key in preferred for item in metrics if item.get("metric_id") == key),
        None,
    ) or next(
        (
            item
            for item in metrics
            if item.get("value") is not None
            and item.get("metric_id") != "impact_not_available"
        ),
        None,
    )
    if metric:
        metric_id = str(metric.get("metric_id") or "impact")
        metric_label = str(metric.get("label") or metric_hint.get("label") or "确定性影响")
        metric_value = metric.get("value")
        metric_unit = str(metric.get("unit") or metric_hint.get("unit") or "")
    else:
        impact_keys = preferred + [
            "financial_misstatement_cny_per_contract",
            "impact_cny_per_10000_units",
            "return_difference_pct_points",
            "field_mapping_difference_points",
        ]
        metric_id = next((key for key in impact_keys if impact.get(key) is not None), "impact")
        metric_label = str(metric_hint.get("label") or "确定性影响")
        metric_value = impact.get(metric_id, precheck.get(metric_id))
        metric_unit = str(metric_hint.get("unit") or "")
    display_units = {
        "CNY/contract": "元/手",
        "CNY_per_contract": "元/手",
        "CNY/10000 units": "元/万份",
        "percentage_points": "个百分点",
        "points": "点",
    }
    metric_unit = display_units.get(metric_unit, metric_unit)
    recommendation = str(
        datapass.get("machine_recommendation")
        or semantic.get("recommendation")
        or impact.get("recommended_decision")
        or precheck.get("machine_recommendation")
        or "WAIT"
    )
    return {
        "candidate_field": _text(selected),
        "required_field": _text(required),
        "candidate_value": selected_value,
        "required_value": required_value,
        "metric_id": metric_id,
        "impact_label": metric_label,
        "impact_value": metric_value,
        "impact_unit": metric_unit,
        "machine_recommendation": recommendation,
        "source": (
            "SEALED_WORKER_ARTIFACT_AND_DATAPASS"
            if semantic and datapass
            else "DISPATCH_PRECHECK_FALLBACK"
        ),
        # Compatibility projections for existing readers. They are populated
        # only from known evidence fields and are not used as the authority.
        "close": precheck.get("close"),
        "settle": precheck.get("settle"),
        "impact_cny_per_contract": (
            float(metric_value)
            if metric_id == "financial_misstatement_cny_per_contract"
            and isinstance(metric_value, (int, float))
            else None
        ),
    }


def build_result_payload(
    run: dict[str, Any], submission: dict[str, Any]
) -> dict[str, Any]:
    precheck = run.get("precheck") or {}
    gate = run.get("human_gate") or {}
    datapass = run.get("datapass") or {}
    agent_result = run.get("agent_result") or {}
    artifacts = agent_result.get("worker_artifacts") or {}
    skill_invocations: list[dict[str, Any]] = []
    for artifact in artifacts.values():
        skill_invocations.extend(artifact.get("skill_invocations") or [])
    provider_usage = run.get("provider_usage") or {}
    outcome = _plain_outcome(run)
    truth = _semantic_truth(run, submission)
    selected = truth["candidate_field"]
    required = truth["required_field"]
    impact = truth.get("impact_value")
    impact_text = (
        f"{float(impact):,.4f} {truth.get('impact_unit') or ''}".strip()
        if isinstance(impact, (int, float))
        else "尚无可复核数值"
    )
    child_run_id = (run.get("lineage") or {}).get("child_run_id")
    profile, profile_id, purpose_id, purpose_label = _profile_context(run, submission)
    parsed = submission.get("parsed") or {}
    instrument = next(
        (
            parsed.get(key)
            for key in (
                "instrument",
                "security_code",
                "fund_code",
                "research_item_id",
                "entity_id",
            )
            if parsed.get(key)
        ),
        (submission.get("metadata") or {}).get("target_instrument"),
    )
    effective_date = next(
        (
            parsed.get(key)
            for key in (
                "trade_date",
                "event_date",
                "nav_date",
                "effective_date",
                "published_at",
            )
            if parsed.get(key) and str(parsed.get(key)).upper() != "UNKNOWN"
        ),
        None,
    ) or _effective_date_from_immutable_evidence(submission) or "未解析"
    fields_match = selected == required and selected != "未解析"
    impact_available = isinstance(impact, (int, float))
    equity_missing_series = (
        profile_id == "equity_corporate_action"
        and truth.get("machine_recommendation") == "NEEDS_EVIDENCE"
        and not impact_available
    )

    payload = {
        "protocol": "FINFLUX_HUMAN_READABLE_RESULT_V1.0",
        "generated_at_utc": gate.get("decided_at") or run.get("created_at") or utc_now(),
        "run_id": run.get("run_id"),
        "case_id": run.get("case_id"),
        "submission_id": run.get("submission_id"),
        "parent_run_id": (run.get("lineage") or {}).get("parent_run_id"),
        "child_run_id": child_run_id,
        "outcome": outcome,
        "business_scope": {
            "profile_id": profile_id,
            "profile_version": ((datapass.get("profile") or {}).get("profile_version")),
            "asset": profile.get("display_name") or profile.get("asset_family") or "金融数据",
            "instrument": instrument,
            "effective_date": effective_date,
            "trade_date": effective_date,
            "declared_purpose_id": purpose_id,
            "declared_purpose": purpose_label,
            "allowed_downstream": (
                [purpose_label] if outcome["code"] == "ADMITTED" else []
            ),
        },
        "plain_language_finding": {
            **truth,
            "explanation": (
                "公司行动条款证据已经核验，但它本身不是收益率行情序列；"
                "当前还缺少同标的、同日期且复权口径明确的价格序列，因此无法计算收益率影响，也没有把未知值写成0。"
                if equity_missing_series
                else
                f"同一Run的专业Agent提出使用 {selected}；已登记的“{purpose_label}”"
                f"语义契约要求 {required}。二者不一致，确定性Skill复算影响为 {impact_text}。"
                if not fields_match
                else f"同一Run的专业Agent提出使用 {selected}，与已登记的“{purpose_label}”"
                f"语义契约一致；确定性Skill复算影响为 {impact_text}。"
            ),
            "recommended_fix": (
                "补充同标的、同日期且复权口径明确的行情序列，并保留公司行动事件条款作为调整依据，然后创建新Run重新核验。"
                if equity_missing_series
                else
                f"保留原始文件不变，新建修订版本并把候选字段改为{required}。"
                if not fields_match and required != "未解析"
                else "补充用途、字段定义或来源证据后重新核验。"
                if required == "未解析"
                else "保持当前字段映射；来源、用途或版本变化时重新核验。"
            ),
        },
        "human_decision": {
            "state": gate.get("state"),
            "decision": gate.get("decision"),
            "actor": gate.get("human_actor_id"),
            "reason": gate.get("reason", ""),
            "decided_at": gate.get("decided_at"),
            "matrix_event_id": (agent_result.get("human_decision") or {}).get(
                "event_id"
            ),
        },
        "multi_agent_evidence": {
            "agentteams_run_id": run.get("agentteams_run_id"),
            "result_source": agent_result.get("result_source"),
            "leader_recommendation": agent_result.get("leader_recommendation"),
            "leader_event_id": agent_result.get("leader_datapass_event_id"),
            "workers_completed": int(agent_result.get("workers_completed", 0) or 0),
            "workers_required": int(agent_result.get("workers_required", 3) or 3),
            "worker_artifact_count": len(artifacts),
            "skill_invocation_count": len(skill_invocations),
            "skill_versions": [
                f"{item.get('skill_id')}@{item.get('version')}"
                for item in skill_invocations
            ],
            "provider_usage": {
                "status": provider_usage.get("status"),
                "prompt_tokens": provider_usage.get("prompt_tokens"),
                "completion_tokens": provider_usage.get("completion_tokens"),
                "total_tokens": provider_usage.get("total_tokens"),
                "call_count": provider_usage.get("call_count"),
                "source": provider_usage.get("source"),
            },
        },
        "tamper_evidence": {
            "source_file_sha256": (submission.get("file") or {}).get("sha256"),
            "evidence_root_hash": submission.get("evidence_root_hash"),
            "precheck_sha256": precheck.get("sha256"),
            "datapass_draft_sha256": (
                datapass.get("datapass_sha256") or datapass.get("draft_sha256")
            ),
            "human_post_decision_hash": gate.get("post_decision_hash"),
        },
    }
    unsigned = dict(payload)
    payload["result_payload_sha256"] = canonical_sha256(unsigned)
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    o = payload["outcome"]
    report_title = (
        "FinFlux 金融数据处理结果（待人工签署）"
        if o["code"] in {"HUMAN_ACTION_REQUIRED", "NOT_FINAL"}
        else "FinFlux 金融数据最终处理结果"
    )
    finding = payload["plain_language_finding"]
    scope = payload["business_scope"]
    human = payload["human_decision"]
    agents = payload["multi_agent_evidence"]
    hashes = payload["tamper_evidence"]
    allowed = "、".join(scope.get("allowed_downstream") or []) or "未授权进入任何下游"
    metric_value = finding.get("impact_value")
    metric_display = (
        f"{float(metric_value):,.4f} {finding.get('impact_unit') or ''}".strip()
        if isinstance(metric_value, (int, float))
        else "尚无可复核数值"
    )
    return f"""# {report_title}

> **{o['headline']}**

## 给业务人员看的结论

- **能不能用：** {o['headline']}
- **为什么：** {o['plain_reason']}
- **现在怎么做：** {o['next_action']}
- **允许进入：** {allowed}

## 本次数据发生了什么

- 资产/合约：{scope.get('asset')} / {scope.get('instrument')}
- 数据日期：{scope.get('effective_date')}
- 目标用途：{scope.get('declared_purpose')}
- 系统发现：{finding.get('explanation')}
- 建议修复：{finding.get('recommended_fix')}
- Agent候选 / 契约要求：{finding.get('candidate_field')} / {finding.get('required_field')}
- 候选值 / 契约值：{finding.get('candidate_value')} / {finding.get('required_value')}
- {finding.get('impact_label')}：{metric_display}
- 金融事实投影来源：{finding.get('source')}

## 人工处理结果

- 状态：{human.get('state')}
- 决定：{human.get('decision')}
- 责任人：{human.get('actor')}
- 理由：{human.get('reason') or '未填写'}
- 时间：{human.get('decided_at')}
- Matrix签署事件：{human.get('matrix_event_id')}

## 真实多Agent协作证据

- AgentTeams Run ID：{payload.get('run_id')}
- Leader建议：{agents.get('leader_recommendation')}
- Worker完成：{agents.get('workers_completed')} / {agents.get('workers_required')}
- Worker产物：{agents.get('worker_artifact_count')}
- 运行时Skill调用：{agents.get('skill_invocation_count')}
- Skill版本：{', '.join(agents.get('skill_versions') or [])}
- 供应商Token：{agents.get('provider_usage', {}).get('total_tokens')}（输入 {agents.get('provider_usage', {}).get('prompt_tokens')} / 输出 {agents.get('provider_usage', {}).get('completion_tokens')} / {agents.get('provider_usage', {}).get('call_count')} 次调用）
- Token来源：{agents.get('provider_usage', {}).get('source')}

## Run血缘与防篡改信息

- 当前Run：{payload.get('run_id')}
- 父Run：{payload.get('parent_run_id')}
- 修订子Run：{payload.get('child_run_id')}
- 原始文件SHA256：{hashes.get('source_file_sha256')}
- EvidenceRoot：{hashes.get('evidence_root_hash')}
- Precheck：{hashes.get('precheck_sha256')}
- DataPassDraft：{hashes.get('datapass_draft_sha256')}
- HumanDecision：{hashes.get('human_post_decision_hash')}
- 本报告结构化结果哈希：{payload.get('result_payload_sha256')}

---

本报告只对所列数据版本、用途和证据有效。Agent不能改写原始金融数据，也不能代替Human签署。
"""


def _find_cjk_font() -> str | None:
    candidates = [
        os.environ.get("FINFLUX_CJK_FONT", ""),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _render_pdf(payload: dict[str, Any], path: Path) -> None:
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("生成PDF需要PyMuPDF，请先安装requirements.txt") from exc

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font_file = _find_cjk_font()
    font_name = "finflux-cjk" if font_file else "china-s"
    if font_file:
        page.insert_font(fontname=font_name, fontfile=font_file)

    navy = (0.04, 0.11, 0.20)
    cyan = (0.04, 0.62, 0.67)
    green = (0.05, 0.55, 0.36)
    red = (0.75, 0.18, 0.25)
    grey = (0.35, 0.42, 0.48)
    y = 42.0

    def new_page() -> None:
        nonlocal page, y
        page = doc.new_page(width=595, height=842)
        if font_file:
            page.insert_font(fontname=font_name, fontfile=font_file)
        y = 42.0

    def block(text: str, size: float = 10.5, color: tuple = navy, gap: float = 8, bold: bool = False) -> None:
        nonlocal y
        width = 511
        chars_per_line = max(14, int(width / (size * 0.92)))
        lines = max(1, sum(max(1, (len(line) + chars_per_line - 1) // chars_per_line) for line in str(text).splitlines()))
        height = lines * (size * 1.62) + 4
        if y + height > 800:
            new_page()
        page.insert_textbox(
            fitz.Rect(42, y, 553, y + height),
            str(text),
            fontname=font_name,
            fontsize=size,
            color=color,
            lineheight=1.35,
            align=0,
        )
        y += height + gap

    outcome = payload["outcome"]
    report_title = (
        "FinFlux 金融数据处理结果（待人工签署）"
        if outcome["code"] in {"HUMAN_ACTION_REQUIRED", "NOT_FINAL"}
        else "FinFlux 金融数据最终处理结果"
    )
    block(report_title, 22, navy, 4, True)
    block(f"Run ID  {payload.get('run_id')}", 8.5, grey, 14)
    outcome_color = green if outcome["code"] == "ADMITTED" else red
    block(outcome["headline"], 16, outcome_color, 8, True)
    block(f"为什么：{outcome['plain_reason']}\n现在怎么做：{outcome['next_action']}", 11, navy, 18)

    finding = payload["plain_language_finding"]
    scope = payload["business_scope"]
    block("1. 这批数据发生了什么", 14, cyan, 6, True)
    block(
        f"资产/标的：{scope.get('asset')} / {scope.get('instrument')}    数据日期：{scope.get('effective_date')}\n"
        f"目标用途：{scope.get('declared_purpose')}\n"
        f"系统发现：{finding.get('explanation')}\n"
        f"Agent候选 / 契约要求：{finding.get('candidate_field')} / {finding.get('required_field')}\n"
        f"建议修复：{finding.get('recommended_fix')}",
        10.5,
        navy,
        14,
    )

    human = payload["human_decision"]
    block("2. 人工处理结果", 14, cyan, 6, True)
    block(
        f"状态：{human.get('state')}    决定：{human.get('decision')}\n"
        f"责任人：{human.get('actor')}    时间：{human.get('decided_at')}\n"
        f"理由：{human.get('reason') or '未填写'}",
        10.5,
        navy,
        14,
    )

    agents = payload["multi_agent_evidence"]
    usage = agents.get("provider_usage") or {}
    block("3. 真实多Agent协作证据", 14, cyan, 6, True)
    block(
        f"Leader建议：{agents.get('leader_recommendation')}\n"
        f"Worker完成：{agents.get('workers_completed')} / {agents.get('workers_required')}    "
        f"Worker产物：{agents.get('worker_artifact_count')}\n"
        f"运行时Skill调用：{agents.get('skill_invocation_count')}\n"
        f"真实模型Token：{usage.get('total_tokens')}（输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} / {usage.get('call_count')} 次）",
        10.5,
        navy,
        14,
    )

    hashes = payload["tamper_evidence"]
    block("4. 防篡改与Run血缘", 14, cyan, 6, True)
    block(
        f"当前Run：{payload.get('run_id')}\n父Run：{payload.get('parent_run_id')}\n修订子Run：{payload.get('child_run_id')}\n"
        f"原始文件SHA256：{hashes.get('source_file_sha256')}\n"
        f"EvidenceRoot：{hashes.get('evidence_root_hash')}\n"
        f"结果哈希：{payload.get('result_payload_sha256')}",
        8.5,
        grey,
        10,
    )
    block("适用边界：本报告只对所列数据版本、用途和证据有效。Agent不能改写原始金融数据，也不能代替Human签署。", 8.5, grey, 0)
    doc.set_metadata(
        {
            "title": f"FinFlux Result {payload.get('run_id')}",
            "author": "FinFlux",
            "subject": payload.get("result_payload_sha256", ""),
        }
    )
    if hasattr(doc, "subset_fonts"):
        doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def write_result_artifacts(
    report_root: Path,
    run: dict[str, Any],
    submission: dict[str, Any],
    *,
    stage: str = "final",
    replace_existing: bool = False,
) -> dict[str, Any]:
    if stage not in {"preview", "final"}:
        raise ValueError("stage must be preview or final")
    payload = build_result_payload(run, submission)
    run_id = str(run["run_id"])
    digest = str(payload["result_payload_sha256"])
    report_root.mkdir(parents=True, exist_ok=True)
    output_dir = report_root / run_id
    label = "Result_Preview" if stage == "preview" else "Result"
    stem = f"FinFlux_{label}_{run_id}_{digest[:12]}"
    file_names = {
        "markdown": f"{stem}.md",
        "pdf": f"{stem}.pdf",
        "json": f"{stem}.json",
    }
    if output_dir.exists():
        try:
            manifest = verify_result_artifacts(
                report_root,
                run_id,
                expected_payload_sha256=digest,
            )
        except RuntimeError:
            if not replace_existing:
                raise
        else:
            paths = {
                kind: str(output_dir / str(manifest["files"][kind]["name"]))
                for kind in file_names
            }
            paths["manifest"] = str(output_dir / "manifest.json")
            route_name = "preview" if stage == "preview" else "result"
            return {
                "payload": payload,
                "manifest": manifest,
                "paths": paths,
                "download_urls": {
                    kind: f"/api/v1/runs/{run_id}/{route_name}.{suffix}"
                    for kind, suffix in {
                        "markdown": "md",
                        "pdf": "pdf",
                        "json": "json",
                    }.items()
                },
            }

    staging = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.staging-", dir=str(report_root))
    )
    md_path = staging / file_names["markdown"]
    pdf_path = staging / file_names["pdf"]
    json_path = staging / file_names["json"]
    manifest_path = staging / "manifest.json"
    try:
        md_path.write_text(render_markdown(payload), encoding="utf-8")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _render_pdf(payload, pdf_path)

        manifest = {
        "protocol": (
            "FINFLUX_RESULT_PREVIEW_MANIFEST_V1.0"
            if stage == "preview"
            else "FINFLUX_RESULT_ARTIFACT_MANIFEST_V1.0"
        ),
        "stage": stage,
        "truth_boundary": (
            "真实Run的自动中间报告；尚未形成Human最终授权。"
            if stage == "preview"
            else "Human决定已落盘的最终处置报告。"
        ),
        "run_id": run_id,
        "result_payload_sha256": digest,
        "created_at_utc": utc_now(),
        "files": {
            "markdown": {
                "name": md_path.name,
                "sha256": hashlib.sha256(md_path.read_bytes()).hexdigest(),
                "media_type": "text/markdown",
            },
            "pdf": {
                "name": pdf_path.name,
                "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                "media_type": "application/pdf",
            },
            "json": {
                "name": json_path.name,
                "sha256": hashlib.sha256(json_path.read_bytes()).hexdigest(),
                "media_type": "application/json",
            },
        },
    }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Verify every staged byte before publishing the directory in one
        # atomic rename.  A crash can leave only a hidden staging directory,
        # never a half-valid signed result directory.
        verify_result_artifacts(
            report_root,
            run_id,
            expected_payload_sha256=digest,
            artifact_dir=staging,
        )
        archived_dir: Path | None = None
        if output_dir.exists():
            old_manifest = verify_result_artifacts(report_root, run_id)
            old_digest = str(old_manifest.get("result_payload_sha256") or "unknown")[:12]
            archive_root = report_root / "_superseded" / run_id
            archive_root.mkdir(parents=True, exist_ok=True)
            archived_dir = archive_root / old_digest
            if archived_dir.exists():
                archived_dir = archive_root / (
                    old_digest + "-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                )
            os.replace(output_dir, archived_dir)
        try:
            os.replace(staging, output_dir)
        except Exception:
            if archived_dir is not None and archived_dir.exists() and not output_dir.exists():
                os.replace(archived_dir, output_dir)
            raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    md_path = output_dir / file_names["markdown"]
    pdf_path = output_dir / file_names["pdf"]
    json_path = output_dir / file_names["json"]
    manifest_path = output_dir / "manifest.json"
    route_name = "preview" if stage == "preview" else "result"
    return {
        "payload": payload,
        "manifest": manifest,
        "paths": {
            "markdown": str(md_path),
            "pdf": str(pdf_path),
            "json": str(json_path),
            "manifest": str(manifest_path),
        },
        "download_urls": {
            "markdown": f"/api/v1/runs/{run_id}/{route_name}.md",
            "pdf": f"/api/v1/runs/{run_id}/{route_name}.pdf",
            "json": f"/api/v1/runs/{run_id}/{route_name}.json",
        },
    }


def verify_result_artifacts(
    report_root: Path,
    run_id: str,
    *,
    expected_payload_sha256: str | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Fail closed unless a published/staged result set is self-consistent."""

    root = (artifact_dir or (report_root / run_id)).resolve()
    allowed_root = report_root.resolve()
    root.relative_to(allowed_root)
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("result artifact manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise RuntimeError("result artifact manifest Run binding is invalid")
    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != canonical_sha256(unsigned):
        raise RuntimeError("result artifact manifest hash mismatch")
    if (
        expected_payload_sha256 is not None
        and manifest.get("result_payload_sha256") != expected_payload_sha256
    ):
        raise RuntimeError("result artifact payload version mismatch")
    files = manifest.get("files") or {}
    expected_kinds = {"markdown", "pdf", "json"}
    if set(files) != expected_kinds:
        raise RuntimeError("result artifact manifest file set mismatch")
    for kind in sorted(expected_kinds):
        descriptor = files.get(kind) or {}
        name = str(descriptor.get("name") or "")
        candidate = (root / name).resolve()
        candidate.relative_to(root)
        if not name or not candidate.is_file():
            raise RuntimeError(f"result artifact missing: {kind}")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != descriptor.get(
            "sha256"
        ):
            raise RuntimeError(f"result artifact hash mismatch: {kind}")
    try:
        json_payload = json.loads(
            (root / str(files["json"]["name"])).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("result JSON is unreadable") from exc
    payload_unsigned = {
        key: value
        for key, value in json_payload.items()
        if key != "result_payload_sha256"
    }
    if (
        json_payload.get("run_id") != run_id
        or json_payload.get("result_payload_sha256")
        != canonical_sha256(payload_unsigned)
        or json_payload.get("result_payload_sha256")
        != manifest.get("result_payload_sha256")
    ):
        raise RuntimeError("result JSON payload hash or Run binding mismatch")
    return manifest
