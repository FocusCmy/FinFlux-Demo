#!/usr/bin/env python3
"""Launch, observe, and export one real FinFlux AgentTeams profile Run.

The runner is intentionally single-Run and never submits a Human decision.
Financial values come from the checked-in source-bound 50-row CSV snapshots;
``candidate_mapping=AUTO_AGENT`` leaves semantic selection to the routed
Agents and deterministic Skills.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_three_profile_representatives import DATA_DIR, request_json


OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "agentteams" / "evidence" / "live-profile-runs"
FINAL_STATES = {
    "COMPLETED",
    "REJECTED",
    "RETURNED",
    "FAILED_CLOSED",
    "STOPPED_BY_GATE",
    "BUDGET_EXCEEDED",
}
PAUSE_STATES = FINAL_STATES | {"AWAITING_HUMAN"}

PROFILES: dict[str, dict[str, Any]] = {
    "equity": {
        "file": "equity_50.csv",
        "target": "000001",
        "purpose": "return_analysis",
        "instruction": (
            "核验这批公司行动记录能否作为收益率分析输入；分别检查来源、"
            "复权语义、事件字段完整性和下游影响，缺少行情序列时明确列出补证项。"
        ),
    },
    "fund": {
        "file": "fund_50.csv",
        "target": "000001",
        "purpose": "holding_valuation",
        "instruction": (
            "核验这批开放式基金净值记录能否用于持仓估值；独立识别应使用的净值概念、"
            "日期适用性、来源权属和下游金额口径。"
        ),
    },
    "futures": {
        "file": "futures_50.csv",
        "target": "IC2608",
        "purpose": "daily_settlement_pnl",
        "contract_multiplier": 200,
        "instruction": (
            "核验这批股指期货行情能否用于逐日盈亏结算；独立识别所需价格概念，"
            "并用确定性Skill复算字段选择产生的每手金额影响。"
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persist(value: dict[str, Any], name: str) -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_ROOT / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _run_payload(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    if status in {200, 201, 202}:
        return payload
    persisted = payload.get("persisted_run")
    if isinstance(persisted, dict):
        return persisted
    raise RuntimeError(f"Run API failed closed: HTTP {status}: {payload}")


def launch(base_url: str, profile_name: str, timeout_seconds: int) -> dict[str, Any]:
    spec = PROFILES[profile_name]
    file_path = DATA_DIR / str(spec["file"])
    body = file_path.read_bytes()
    source_sha256 = hashlib.sha256(body).hexdigest()
    inspect_status, inspection = request_json(
        base_url,
        "/api/v1/intake/inspect-file",
        multipart=(
            file_path,
            {
                "profile": "auto",
                "entity_query": spec["target"],
                "declared_purpose": spec["purpose"],
                "task_instruction": spec["instruction"],
            },
        ),
    )
    if inspect_status != 201:
        raise RuntimeError(f"inspect failed: HTTP {inspect_status}: {inspection}")
    confirmations = {
        "declared_source": "FinFlux real_50x3_v1 source-bound public-data snapshot",
        "rights_basis": "公开来源竞赛评测副本；生产使用前由机构权属负责人复核",
        "confidentiality_class": "PUBLIC",
        "declared_purpose": spec["purpose"],
        "candidate_mapping": "AUTO_AGENT",
        "entity_query": spec["target"],
        "task_instruction": spec["instruction"],
    }
    if spec.get("contract_multiplier") is not None:
        confirmations["contract_multiplier"] = spec["contract_multiplier"]
    commit_status, submission = request_json(
        base_url,
        "/api/v1/intake/commit",
        payload={"inspection_id": inspection["inspection_id"], "confirmations": confirmations},
    )
    if commit_status != 201:
        raise RuntimeError(f"commit failed: HTTP {commit_status}: {submission}")
    run_status, response = request_json(
        base_url,
        "/api/v1/runs",
        payload={"submission_id": submission["submission_id"]},
        timeout=180,
    )
    run = _run_payload(run_status, response)
    run_id = str(run.get("run_id") or "")
    if not run_id:
        raise RuntimeError("Run API returned no run_id")
    receipt: dict[str, Any] = {
        "protocol": "FINFLUX_SINGLE_LIVE_PROFILE_EXECUTION_V1",
        "phase": "LAUNCHED",
        "created_at_utc": utc_now(),
        "profile_requested": profile_name,
        "source_file": str(file_path),
        "source_sha256": source_sha256,
        "inspection_id": inspection.get("inspection_id"),
        "submission_id": submission.get("submission_id"),
        "submission_profile": submission.get("profile"),
        "submission_evidence_root_hash": submission.get("evidence_root_hash"),
        "run_id": run_id,
        "agentteams_run_id": run.get("agentteams_run_id"),
        "state": run.get("state"),
        "candidate_mapping": "AUTO_AGENT",
        "human_decision_submitted_by_runner": False,
        "model_completion_claimed": bool(run.get("agentteams_run_id")),
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    _persist(receipt, f"{run_id}-launch.json")
    return observe(base_url, run_id, timeout_seconds, launch_receipt=receipt)


def observe(
    base_url: str,
    run_id: str,
    timeout_seconds: int,
    *,
    launch_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    snapshots: list[dict[str, Any]] = []
    while True:
        status, response = request_json(base_url, f"/api/v1/runs/{run_id}")
        run = _run_payload(status, response)
        compact = {
            "captured_at_utc": utc_now(),
            "http_status": status,
            "state": run.get("state"),
            "manager_authorized": str(
                (run.get("manager_dispatch_receipt") or {}).get("status") or ""
            )
            == "MANAGER_AUTHORIZED_DISPATCHED",
            "worker_count": len((run.get("agent_result") or {}).get("worker_artifacts") or {}),
            "skill_count": len(
                (run.get("datapass") or {}).get("skill_invocations")
                or ((run.get("datapass") or {}).get("skills") or {}).get("invocations")
                or run.get("skill_invocations")
                or []
            ),
            "recommendation": (run.get("datapass") or {}).get("machine_recommendation"),
            "human_state": (run.get("human_gate") or {}).get("state"),
            "provider_tokens": (run.get("provider_usage") or {}).get("total_tokens"),
            "provider_calls": (run.get("provider_usage") or {}).get("call_count"),
        }
        if not snapshots or compact != {**snapshots[-1], "captured_at_utc": compact["captured_at_utc"]}:
            snapshots.append(compact)
            print(json.dumps(compact, ensure_ascii=False))
        state = str(run.get("state") or "")
        timed_out = state not in PAUSE_STATES and time.monotonic() >= deadline
        if state in PAUSE_STATES or timed_out:
            result = {
                "protocol": "FINFLUX_SINGLE_LIVE_PROFILE_OBSERVATION_V1",
                "observed_at_utc": utc_now(),
                "run_id": run_id,
                "state": state,
                "observation_status": (
                    "TIMEBOX_EXPIRED_ANALYSIS_REQUIRED" if timed_out else "STATE_REACHED"
                ),
                "requires_human_action": state == "AWAITING_HUMAN",
                "human_decision_submitted_by_runner": False,
                "timebox_seconds": timeout_seconds,
                "next_action": (
                    "停止轮询；分析当前角色、最后事件、Provider usage与Runtime状态，"
                    "只允许在同一Run内恢复。"
                    if timed_out
                    else "按当前状态继续人工处置或导出。"
                ),
                "launch_receipt": launch_receipt,
                "snapshots": snapshots,
                "final_snapshot": compact,
            }
            result["observation_sha256"] = canonical_sha256(result)
            _persist(result, f"{run_id}-observation.json")
            return result
        time.sleep(5)


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"download failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc


def export(base_url: str, run_id: str) -> dict[str, Any]:
    status, response = request_json(base_url, f"/api/v1/runs/{run_id}")
    run = _run_payload(status, response)
    if str((run.get("human_gate") or {}).get("state")) not in {"APPROVED", "REJECTED", "RETURNED"}:
        raise RuntimeError("Run has no real final Human decision; export refused")
    output_dir = OUTPUT_ROOT / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for endpoint, filename in (
        ("result.md", "result.md"),
        ("result.pdf", "result.pdf"),
        ("result.json", "result.json"),
        ("audit-bundle.zip", "audit.zip"),
    ):
        content = _download(f"{base_url.rstrip('/')}/api/v1/runs/{run_id}/{endpoint}")
        path = output_dir / filename
        path.write_bytes(content)
        files[filename] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    receipt = {
        "protocol": "FINFLUX_SIGNED_LIVE_PROFILE_EXPORT_V1",
        "exported_at_utc": utc_now(),
        "run_id": run_id,
        "state": run.get("state"),
        "human_gate": run.get("human_gate"),
        "provider_usage": run.get("provider_usage"),
        "files": files,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    _persist(receipt, f"{run_id}-export.json")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run exactly one source-bound FinFlux AgentTeams case")
    parser.add_argument("--base-url", default="http://127.0.0.1:8768")
    parser.add_argument("--phase", choices=("launch", "status", "export"), required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), help="required for launch")
    parser.add_argument("--run-id", help="required for status/export")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="单Run观察上限，默认600秒；到期停止轮询并转入原因分析",
    )
    args = parser.parse_args()
    if args.phase == "launch":
        if not args.profile:
            parser.error("--profile is required for launch")
        result = launch(args.base_url, args.profile, args.timeout_seconds)
    elif args.phase == "status":
        if not args.run_id:
            parser.error("--run-id is required for status")
        result = observe(args.base_url, args.run_id, args.timeout_seconds)
    else:
        if not args.run_id:
            parser.error("--run-id is required for export")
        result = export(args.base_url, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
