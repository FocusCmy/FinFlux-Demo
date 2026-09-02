#!/usr/bin/env python3
"""Run five source-bound FinFlux V0.2 representative admissions over HTTP.

The runner never edits market values and never creates synthetic evidence.  It
uploads one of the checked-in, source-bound 50-row CSV files, confirms only
usage/configuration metadata, creates a durable Run, and records the backend's
actual state.  When the provider Token Guard blocks an AgentTeams dispatch,
that guard outcome is retained as evidence rather than reported as completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = APP_DIR / "data" / "real_50x3_v1" / "normalized"
DEFAULT_OUTPUT = APP_DIR.parent / "agentteams" / "evidence" / "three-profile-live-v0.2"


CASES: tuple[dict[str, Any], ...] = (
    {
        "alias": "FUTURES-PASS-SETTLE",
        "file": "futures_50.csv",
        "target": "IC2608",
        "purpose": "daily_settlement_pnl",
        "candidate_mapping": "settle",
        "contract_multiplier": 200,
        "expected": "PASS",
        "instruction": "核验IC2608逐日结算用途所需字段，并给出可追溯准入建议。",
    },
    {
        "alias": "FUTURES-BLOCK-CLOSE",
        "file": "futures_50.csv",
        "target": "IC2608",
        "purpose": "daily_settlement_pnl",
        "candidate_mapping": "close",
        "contract_multiplier": 200,
        "expected": "BLOCK",
        "instruction": "核验IC2608逐日结算用途中候选字段close是否符合结算契约。",
    },
    {
        "alias": "EQUITY-BLOCK-ADJUSTMENT",
        "file": "equity_50.csv",
        "target": "000001",
        "purpose": "return_analysis",
        "candidate_mapping": "declared_corporate_action_terms",
        "expected": "BLOCK",
        "instruction": "核验000001公司行动证据能否直接作为收益率序列复权口径。",
    },
    {
        "alias": "FUND-PASS-UNIT-NAV",
        "file": "fund_50.csv",
        "target": "000001",
        "purpose": "holding_valuation",
        "candidate_mapping": "unit_nav",
        "expected": "PASS",
        "instruction": "核验000001基金持仓估值是否使用单位净值及对应净值日期。",
    },
    {
        "alias": "FUND-WAIT-MISSING-NAV",
        "file": "fund_50.csv",
        "target": "000041",
        "purpose": "holding_valuation",
        "candidate_mapping": "unit_nav",
        "expected": "NEEDS_EVIDENCE",
        "instruction": "核验000041基金持仓估值证据；缺少必要字段时列出补充项，不得推断数值。",
    },
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_json(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    multipart: tuple[Path, dict[str, Any]] | None = None,
    timeout: int = 60,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {"Accept": "application/json"}
    body: bytes | None = None
    if multipart is not None:
        file_path, metadata = multipart
        boundary = f"----FinFlux{secrets.token_hex(12)}"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        chunks = [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="metadata"\r\n\r\n',
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(chunks)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=body, headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(raw)
        except json.JSONDecodeError:
            error = {"error": raw}
        return exc.code, error


def run_case(base_url: str, case: dict[str, Any]) -> dict[str, Any]:
    file_path = DATA_DIR / str(case["file"])
    source_sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
    inspect_status, inspection = request_json(
        base_url,
        "/api/v1/intake/inspect-file",
        multipart=(
            file_path,
            {
                "profile": "auto",
                "entity_query": case["target"],
                "declared_purpose": case["purpose"],
            },
        ),
    )
    if inspect_status != 201:
        raise RuntimeError(f"{case['alias']} inspect failed: {inspect_status} {inspection}")
    confirmations: dict[str, Any] = {
        "declared_source": f"FinFlux source-bound manifest: {file_path.as_posix()}",
        "rights_basis": "公开来源的竞赛研究评测副本；生产使用前由机构权属负责人复核",
        "confidentiality_class": "PUBLIC",
        "declared_purpose": case["purpose"],
        "candidate_mapping": case["candidate_mapping"],
        "entity_query": case["target"],
        "task_instruction": case["instruction"],
    }
    if "contract_multiplier" in case:
        confirmations["contract_multiplier"] = case["contract_multiplier"]
    commit_status, submission = request_json(
        base_url,
        "/api/v1/intake/commit",
        payload={
            "inspection_id": inspection["inspection_id"],
            "confirmations": confirmations,
        },
    )
    if commit_status != 201:
        raise RuntimeError(f"{case['alias']} commit failed: {commit_status} {submission}")
    run_status, run = request_json(
        base_url,
        "/api/v1/runs",
        payload={"submission_id": submission["submission_id"]},
        timeout=180,
    )
    if run_status not in {200, 201, 202, 429}:
        raise RuntimeError(f"{case['alias']} run failed: {run_status} {run}")
    precheck = run.get("precheck") or {}
    observed = str(precheck.get("machine_recommendation") or "")
    return {
        "alias": case["alias"],
        "expected_precheck": case["expected"],
        "observed_precheck": observed,
        "expectation_met": observed == case["expected"],
        "source_file": str(file_path.relative_to(APP_DIR.parent)).replace("\\", "/"),
        "source_sha256": source_sha256,
        "inspection_id": inspection.get("inspection_id"),
        "inspection_status": inspection.get("status"),
        "profile": submission.get("profile"),
        "submission_id": submission.get("submission_id"),
        "evidence_root_hash": submission.get("evidence_root_hash"),
        "execution_readiness": submission.get("execution_readiness"),
        "missing_evidence_fields": (submission.get("metadata") or {}).get("missing_evidence_fields") or [],
        "run_http_status": run_status,
        "run_id": run.get("run_id"),
        "case_id": run.get("case_id"),
        "state": run.get("state"),
        "route": (run.get("root_route_decision") or {}).get("route"),
        "agentteams_run_id": run.get("agentteams_run_id"),
        "dispatch_guard": run.get("dispatch_guard"),
        "token_ledger": (run.get("budget") or {}).get("tokens"),
        "precheck": precheck,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8768")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    for case in CASES:
        result = run_case(args.base_url, case)
        results.append(result)
        print(
            f"{result['alias']}: {result['observed_precheck']} / "
            f"{result['route']} / {result['state']} / {result['run_id']}"
        )
    completed_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "protocol": "FINFLUX_THREE_PROFILE_REPRESENTATIVE_RUNS_V0.2",
        "started_at": started_at,
        "completed_at": completed_at,
        "base_url": args.base_url,
        "truth_boundary": {
            "source_values_synthetic": False,
            "financial_values_mutated": False,
            "configuration_confirmed_separately": True,
            "model_completion_claimed_only_when_agentteams_run_id_exists": True,
            "guarded_dispatch_is_not_completion": True,
        },
        "all_precheck_expectations_met": all(item["expectation_met"] for item in results),
        "cases": results,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = args.output_dir / f"representative-runs-{timestamp}.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = args.output_dir / "latest.json"
    latest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"evidence={target}")
    return 0 if manifest["all_precheck_expectations_met"] else 2


if __name__ == "__main__":
    sys.exit(main())
