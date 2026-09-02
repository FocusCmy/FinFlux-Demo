from __future__ import annotations

"""Fail-closed runner for the FinFlux controlled benchmark.

The AgentTeams branch deliberately separates launch, status and finalization:

* launch accepts an already-sealed ``submission_id`` and performs exactly one
  mutating request: ``POST /api/v1/runs``;
* status is GET-only and pauses at the Human Gate;
* finalize is GET-only and verifies the signed V0.2 result and audit bundle.

It never calls the legacy ``/api/run`` or ``/api/agent/runs`` endpoints and it
never submits a Human decision.  The runner is intentionally limited to the
only currently Live profile, ``futures_settlement``.  Other profiles fail
closed instead of being presented as AgentTeams evidence.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from manager_routing import decide_root_route  # noqa: E402
from v02_live_acceptance import (  # noqa: E402
    REQUIRED_SKILLS,
    REQUIRED_WORKERS,
    validate_run,
    verify_audit_zip,
)


PLAN_PATH = ROOT / "selected_benchmark_cases.json"
SEED_PATH = APP_ROOT / "data" / "evaluation_seed_cases_v1" / "seed_cases.jsonl"
LEDGER_PATH = ROOT / "controlled_benchmark_ledger.json"
ARTIFACT_ROOT = ROOT / "controlled_benchmark_artifacts"
LIVE_PROFILE = "futures_settlement"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_HUMAN_STATES = {"APPROVED", "REJECTED", "RETURNED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_hash(value: Any) -> bool:
    return bool(HEX64.fullmatch(str(value or "").lower()))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compatibility wrapper with no retry and an explicit GET/POST split."""

    method = "POST" if payload is not None else "GET"
    body = canonical_bytes(payload) if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{url} did not return a JSON object")
    return result


def request_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}") from exc


class BenchmarkClient:
    """Small injectable HTTP client; methods never retry requests."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_json(self, path: str) -> dict[str, Any]:
        return request_json(self.base_url + path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return request_json(self.base_url + path, payload)

    def get_bytes(self, path: str) -> bytes:
        return request_bytes(self.base_url + path)


def seed_index() -> dict[str, dict[str, Any]]:
    return {
        str(row["seed_case_id"]): row
        for row in (
            json.loads(line)
            for line in SEED_PATH.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        )
    }


def blank_ledger(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": "FINFLUX_CONTROLLED_BENCHMARK_LEDGER_V1.1",
        "plan_sha256": sha(plan),
        "updated_at_utc": utc_now(),
        "results": [],
        "truth_boundary": (
            "NOT_EXECUTED保持显式；single-agent仅接收哈希绑定的机器导出；"
            "AgentTeams只允许futures_settlement Live，一次创建一个Run且必须由真实Human关闭。"
        ),
    }


def persist(ledger: dict[str, Any], path: Path = LEDGER_PATH) -> None:
    ledger["updated_at_utc"] = utc_now()
    ledger.pop("ledger_sha256", None)
    ledger["ledger_sha256"] = sha(ledger)
    atomic_json(path, ledger)


def upsert(ledger: dict[str, Any], row: dict[str, Any]) -> None:
    key = (row.get("system"), row.get("seed_case_id"))
    results = list(ledger.get("results") or [])
    results = [
        item
        for item in results
        if (item.get("system"), item.get("seed_case_id")) != key
    ]
    results.append(row)
    ledger["results"] = results


def _load_ledger(plan: dict[str, Any], path: Path) -> dict[str, Any]:
    if path.is_file():
        value = read_json(path)
        if isinstance(value, dict):
            return value
    return blank_ledger(plan)


def _profile_capability(capabilities: dict[str, Any], profile: str) -> dict[str, Any]:
    for item in capabilities.get("profiles") or []:
        if isinstance(item, dict) and item.get("profile") == profile:
            return item
    return {}


def _submission_by_id(payload: dict[str, Any], submission_id: str) -> dict[str, Any]:
    for item in payload.get("submissions") or []:
        if isinstance(item, dict) and item.get("submission_id") == submission_id:
            return item
    raise RuntimeError(f"submission not found: {submission_id}")


def agentteams_preflight(
    client: BenchmarkClient,
    selected: dict[str, Any],
    submission_id: str,
) -> dict[str, Any]:
    """GET-only launch gate.  It does not create a Run or call a model."""

    reasons: list[str] = []
    if selected.get("asset_class") != "futures":
        reasons.append("BENCHMARK_PROFILE_NOT_LIVE")
    submissions = client.get_json("/api/v1/submissions")
    submission = _submission_by_id(submissions, submission_id)
    if submission.get("profile") != LIVE_PROFILE:
        reasons.append("SUBMISSION_PROFILE_NOT_LIVE")
    if submission.get("execution_readiness") != "AGENTTEAMS_EXECUTABLE":
        reasons.append("SUBMISSION_NOT_AGENTTEAMS_EXECUTABLE")
    capabilities = client.get_json("/api/v1/intake/capabilities")
    capability = _profile_capability(capabilities, LIVE_PROFILE)
    if capability.get("execution_readiness") != "AGENTTEAMS_EXECUTABLE":
        reasons.append("LIVE_PROFILE_ADAPTER_NOT_READY")
    control = client.get_json("/api/v1/control-plane/status")
    if not control.get("runtime_connected"):
        reasons.append("RUNTIME_NOT_CONNECTED")
    if control.get("active_run_id"):
        reasons.append("ACTIVE_RUN_EXISTS")
    if int(control.get("topology_ready") or 0) != int(
        control.get("topology_expected") or -1
    ):
        reasons.append("TOPOLOGY_NOT_READY")
    runtime = client.get_json("/api/agent/status")
    if not runtime.get("human_credentials_ready"):
        reasons.append("HUMAN_CREDENTIALS_NOT_READY")
    guard = client.get_json("/api/v1/token-guard")
    if not guard.get("allowed"):
        reasons.append("TOKEN_GUARD_BLOCKED")
    if guard.get("provider_usage_captured") is False:
        reasons.append("PROVIDER_USAGE_CAPTURE_NOT_READY")
    result = {
        "protocol": "FINFLUX_CONTROLLED_LIVE_PREFLIGHT_V0.2",
        "captured_at_utc": utc_now(),
        "status": "READY" if not reasons else "FAIL_CLOSED",
        "reasons": reasons,
        "submission_id": submission_id,
        "profile": submission.get("profile"),
        "execution_readiness": submission.get("execution_readiness"),
        "evidence_sha256": (submission.get("file") or {}).get("sha256"),
        "capability": capability,
        "control_plane": control,
        "runtime": {
            "connected": runtime.get("connected"),
            "human_credentials_ready": runtime.get("human_credentials_ready"),
            "resources": runtime.get("resources"),
        },
        "token_guard": guard,
        "model_calls": 0,
        "provider_tokens": 0,
        "truth_boundary": "只读检查；未创建Run、未派发Agent、未调用模型。",
    }
    result["preflight_sha256"] = sha(result)
    if reasons:
        raise RuntimeError(f"AgentTeams Live preflight failed closed: {reasons}")
    return result


def launch_agentteams(
    client: BenchmarkClient,
    *,
    selected: dict[str, Any],
    submission_id: str,
    plan: dict[str, Any],
    ledger_path: Path = LEDGER_PATH,
    artifact_root: Path = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Create one Live Run with exactly one POST and persist its identity."""

    if not submission_id.strip():
        raise RuntimeError("--submission-id is required for AgentTeams launch")
    preflight_result = agentteams_preflight(client, selected, submission_id)
    # The backend creates and dispatches atomically.  Never call /dispatch here.
    run = client.post_json("/api/v1/runs", {"submission_id": submission_id})
    run_id = str(run.get("run_id") or "")
    if not re.fullmatch(r"RUN-[0-9A-Za-z-]+", run_id):
        raise RuntimeError("POST /api/v1/runs returned no valid run_id")
    row = {
        "system": "agentteams",
        "seed_case_id": selected["seed_case_id"],
        "status": "SUBMITTED",
        "run_id": run_id,
        "submission_id": submission_id,
        "profile": LIVE_PROFILE,
        "run_protocol": run.get("protocol"),
        "expected_recommendation": selected.get("expected_recommendation"),
        "human_gate_required_before_next_case": True,
        "post_runs_count": 1,
        "legacy_mutation_calls": 0,
        "preflight_sha256": preflight_result["preflight_sha256"],
        "launch_response_sha256": sha(run),
        "launched_at_utc": utc_now(),
    }
    launch_failures: list[str] = []
    if run.get("protocol") != "FINFLUX_LIVE_RUN_V0.2":
        launch_failures.append("RUN_PROTOCOL_NOT_V02")
    if run.get("submission_id") not in {None, submission_id}:
        launch_failures.append("RUN_SUBMISSION_ID_MISMATCH")
    if run.get("agentteams_run_id") not in {None, run_id}:
        launch_failures.append("AGENTTEAMS_RUN_ID_MISMATCH")
    if launch_failures:
        row["status"] = "LAUNCH_FAILED_CLOSED"
        row["failures"] = launch_failures
    ledger = _load_ledger(plan, ledger_path)
    upsert(ledger, row)
    persist(ledger, ledger_path)
    receipt = {
        "protocol": "FINFLUX_CONTROLLED_LIVE_LAUNCH_RECEIPT_V0.2",
        **row,
        "truth_boundary": (
            "Run ID在唯一POST响应后立即持久化；Runner不调用/dispatch，"
            "不轮询模型，不提交Human决定。"
        ),
    }
    receipt["receipt_sha256"] = sha(receipt)
    receipt_path = artifact_root / run_id / "launch-receipt.json"
    atomic_json(receipt_path, receipt)
    if launch_failures:
        raise RuntimeError(
            f"Run {run_id} was persisted but failed V0.2 launch validation: {launch_failures}"
        )
    return {"receipt_path": str(receipt_path), "receipt": receipt, "run": run}


def status_agentteams(client: BenchmarkClient, run_id: str) -> dict[str, Any]:
    """GET-only status snapshot; stop at AWAITING_HUMAN."""

    run = client.get_json(f"/api/v1/runs/{run_id}")
    validation = validate_run(run, require_final=False)
    result = {
        "protocol": "FINFLUX_CONTROLLED_LIVE_STATUS_V0.2",
        "captured_at_utc": utc_now(),
        "run_id": run_id,
        "state": run.get("state"),
        "validation": validation,
        "human_gate": run.get("human_gate") or {},
    }
    if str((run.get("human_gate") or {}).get("state")) == "AWAITING_HUMAN":
        result["next_action"] = "PAUSE_FOR_REAL_HUMAN"
        result["human_decision_automated"] = False
    result["snapshot_sha256"] = sha(result)
    return result


def _normal_agent_id(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _validate_observability(run: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if observed.get("run_id") != run.get("run_id"):
        failures.append("OBSERVABILITY_RUN_ID_MISMATCH")
    agents = {
        _normal_agent_id(item.get("agent_id")): item
        for item in observed.get("agents") or []
        if isinstance(item, dict)
    }
    if "default" not in agents:
        failures.append("MANAGER_OBSERVABILITY_MISSING")
    if "finchange-case-lead" not in agents:
        failures.append("LEADER_OBSERVABILITY_MISSING")
    missing_workers = REQUIRED_WORKERS - set(agents)
    for worker in sorted(missing_workers):
        failures.append(f"WORKER_OBSERVABILITY_MISSING:{worker}")
    result = run.get("agent_result") or {}
    if not result.get("leader_datapass_event_id"):
        failures.append("LEADER_DATAPASS_EVENT_MISSING")
    provider = observed.get("provider_usage") or run.get("provider_usage") or {}
    if provider.get("status") != "PROVIDER_REPORTED":
        failures.append("PROVIDER_USAGE_NOT_REPORTED")
    if not isinstance(provider.get("total_tokens"), int):
        failures.append("PROVIDER_TOTAL_TOKENS_NOT_INTEGER")
    return failures


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _run_latency_ms(run: dict[str, Any]) -> int:
    start = _parse_datetime(run.get("created_at"))
    gate = run.get("human_gate") or {}
    end = _parse_datetime(gate.get("decided_at") or gate.get("decided_at_utc"))
    if end is None:
        manifest = ((run.get("final_result") or {}).get("manifest") or {})
        end = _parse_datetime(manifest.get("created_at_utc"))
    if start is not None and end is not None and end >= start:
        return int(round((end - start).total_seconds() * 1000))
    wallclock = (((run.get("budget") or {}).get("wallclock") or {}).get("used_s"))
    if isinstance(wallclock, (int, float)) and wallclock >= 0:
        return int(round(float(wallclock) * 1000))
    raise RuntimeError("final Run has no auditable end-to-end latency")


def _verify_result_downloads(
    client: BenchmarkClient,
    run: dict[str, Any],
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    manifest = ((run.get("final_result") or {}).get("manifest") or {})
    files = manifest.get("files") or {}
    expected = {
        "markdown": ("result.md", ".md"),
        "pdf": ("result.pdf", ".pdf"),
        "json": ("result.json", ".json"),
    }
    if set(files) != set(expected):
        raise RuntimeError("final result manifest must contain Markdown, PDF and JSON")
    downloaded: dict[str, dict[str, Any]] = {}
    for kind, (endpoint, suffix) in expected.items():
        content = client.get_bytes(f"/api/v1/runs/{run['run_id']}/{endpoint}")
        digest = sha_bytes(content)
        if digest != str((files.get(kind) or {}).get("sha256")):
            raise RuntimeError(f"downloaded {kind} hash does not match manifest")
        target = output_dir / f"result{suffix}"
        target.write_bytes(content)
        downloaded[kind] = {
            "path": str(target),
            "sha256": digest,
            "bytes": len(content),
        }
    return downloaded


def finalize_agentteams(
    client: BenchmarkClient,
    *,
    run_id: str,
    selected: dict[str, Any],
    plan: dict[str, Any],
    ledger_path: Path = LEDGER_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """GET and validate a Human-final V0.2 Run; never signs or dispatches."""

    run = client.get_json(f"/api/v1/runs/{run_id}")
    human_state = str((run.get("human_gate") or {}).get("state") or "")
    if human_state not in TERMINAL_HUMAN_STATES:
        raise RuntimeError(
            "Run is not Human-final; finalize refuses to impersonate or automate Human"
        )
    # This endpoint is GET-only.  For a signed Run it materializes/returns the
    # deterministic, content-addressed report; it never alters the Human decision.
    client.get_json(f"/api/v1/runs/{run_id}/final-result")
    run = client.get_json(f"/api/v1/runs/{run_id}")
    validation = validate_run(run, require_final=True)
    observability = client.get_json(f"/api/v1/runs/{run_id}/observability")
    failures = list(validation.get("failures") or [])
    failures.extend(_validate_observability(run, observability))
    if validation.get("status") != "PASS" or failures:
        raise RuntimeError(f"V0.2 final validation failed: {sorted(set(failures))}")
    output_dir = output_dir or (ARTIFACT_ROOT / run_id / "final")
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = _verify_result_downloads(client, run, output_dir)
    audit_content = client.get_bytes(f"/api/v1/runs/{run_id}/audit-bundle.zip")
    audit_validation = verify_audit_zip(audit_content, run_id)
    if audit_validation.get("status") != "PASS":
        raise RuntimeError(
            f"audit ZIP validation failed: {audit_validation.get('failures')}"
        )
    audit_path = output_dir / f"{run_id}-audit.zip"
    audit_path.write_bytes(audit_content)
    provider_usage = run.get("provider_usage") or {}
    datapass = run.get("datapass") or {}
    latency_ms = _run_latency_ms(run)
    row = {
        "system": "agentteams",
        "seed_case_id": selected["seed_case_id"],
        "status": "FINAL_ACCEPTED",
        "run_id": run_id,
        "submission_id": run.get("submission_id"),
        "profile": LIVE_PROFILE,
        "run_protocol": run.get("protocol"),
        "datapass_protocol": datapass.get("protocol"),
        "datapass_sha256": datapass.get("datapass_sha256") or sha(datapass),
        "datapass": datapass,
        "skill_invocations": ((datapass.get("skills") or {}).get("invocations") or []),
        "actual_route": (run.get("root_route_decision") or {}).get("route"),
        "actual_recommendation": datapass.get("machine_recommendation"),
        "expected_recommendation": selected.get("expected_recommendation"),
        "human_state": human_state,
        "latency_ms": latency_ms,
        "provider_tokens": provider_usage.get("total_tokens"),
        "provider_usage": provider_usage,
        "provider_usage_sha256": sha(provider_usage),
        "result_sha256": {key: value["sha256"] for key, value in downloaded.items()},
        "audit_zip_sha256": sha_bytes(audit_content),
        "audit_manifest_sha256": audit_validation.get("manifest_sha256"),
        "observability_sha256": sha(observability),
        "finalized_at_utc": utc_now(),
    }
    ledger = _load_ledger(plan, ledger_path)
    upsert(ledger, row)
    persist(ledger, ledger_path)
    receipt = {
        "protocol": "FINFLUX_CONTROLLED_LIVE_FINAL_RECEIPT_V0.2",
        **row,
        "benchmark_result_status": row["status"],
        "status": "PASS",
        "run_validation": validation,
        "observability_validation": {"status": "PASS", "failures": []},
        "downloaded": downloaded,
        "audit_zip": {
            **audit_validation,
            "path": str(audit_path),
            "sha256": sha_bytes(audit_content),
            "bytes": len(audit_content),
        },
        "truth_boundary": (
            "所有网络操作均为GET；Human终态来自既有Run。验收器只下载、复算哈希和记录结果。"
        ),
    }
    receipt["receipt_sha256"] = sha(receipt)
    atomic_json(output_dir / "final-acceptance-receipt.json", receipt)
    return receipt


def validate_single_agent_export(
    observed: dict[str, Any],
    *,
    selected: dict[str, Any],
    seed: dict[str, Any],
) -> dict[str, Any]:
    """Validate an external single-Agent result without accepting hand-filled claims."""

    required = {
        "seed_case_id",
        "seed_sha256",
        "evidence_sha256",
        "case_sha256",
        "prompt_sha256",
        "output_sha256",
        "provider_usage",
        "provider_usage_sha256",
        "model",
        "tool_calls",
        "tool_calls_sha256",
        "recommendation",
    }
    missing = sorted(required - set(observed))
    if missing:
        raise RuntimeError(f"single-agent export missing fields: {missing}")
    if observed.get("seed_case_id") != selected.get("seed_case_id"):
        raise RuntimeError("single-agent export seed_case_id mismatch")
    expected_hashes = {
        "seed_sha256": sha(seed),
        "evidence_sha256": str((seed.get("source_evidence") or {}).get("sha256") or ""),
        "case_sha256": str(seed.get("case_sha256") or ""),
        "provider_usage_sha256": sha(observed.get("provider_usage")),
        "tool_calls_sha256": sha(observed.get("tool_calls")),
    }
    for field, expected in expected_hashes.items():
        if not _valid_hash(expected) or observed.get(field) != expected:
            raise RuntimeError(f"single-agent export {field} mismatch")
    for field in ("prompt_sha256", "output_sha256"):
        if not _valid_hash(observed.get(field)):
            raise RuntimeError(f"single-agent export {field} is not SHA256")
    if not isinstance(observed.get("tool_calls"), list):
        raise RuntimeError("single-agent export tool_calls must be a list")
    model = str(observed.get("model") or "").strip()
    if not model:
        raise RuntimeError("single-agent export model is empty")
    usage = observed.get("provider_usage") or {}
    if not isinstance(usage, dict) or not isinstance(usage.get("total_tokens"), int):
        raise RuntimeError("single-agent export provider_usage.total_tokens missing")
    evidence = {
        key: observed[key]
        for key in (
            "seed_sha256",
            "evidence_sha256",
            "case_sha256",
            "prompt_sha256",
            "output_sha256",
            "provider_usage_sha256",
            "tool_calls_sha256",
        )
    }
    return {
        "system": "single-agent",
        "seed_case_id": selected["seed_case_id"],
        "status": "OBSERVED_EXTERNAL_EXECUTION",
        "actual_recommendation": observed["recommendation"],
        "expected_recommendation": selected.get("expected_recommendation"),
        "provider_tokens": usage["total_tokens"],
        "provider_usage": usage,
        "model": model,
        "tool_calls": observed["tool_calls"],
        "evidence": evidence,
        "export_sha256": sha(observed),
    }


def _resolve_finalize_run(args: argparse.Namespace) -> str | None:
    value = args.finalize
    if value and value != "__USE_RUN_ID__":
        return str(value)
    if value == "__USE_RUN_ID__":
        return str(args.run_id or "") or None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="FinFlux controlled benchmark gate")
    parser.add_argument("--system", choices=("rule", "single-agent", "agentteams"))
    parser.add_argument("--case-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8768")
    parser.add_argument("--submission-id")
    parser.add_argument("--status", metavar="RUN_ID")
    parser.add_argument("--finalize", nargs="?", const="__USE_RUN_ID__", metavar="RUN_ID")
    parser.add_argument("--run-id")
    parser.add_argument("--observed-result", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    plan = read_json(PLAN_PATH)
    cases = {str(item["seed_case_id"]): item for item in plan["cases"]}
    if not args.system:
        print(json.dumps({"status": "DRY_RUN", "plan": plan}, ensure_ascii=False, indent=2))
        return 0
    if not args.case_id or args.case_id not in cases:
        parser.error("--case-id must select exactly one case from selected_benchmark_cases.json")
    selected = cases[args.case_id]
    client = BenchmarkClient(args.base_url)

    if args.system == "agentteams" and args.status:
        print(json.dumps(status_agentteams(client, args.status), ensure_ascii=False, indent=2))
        return 0
    finalize_run = _resolve_finalize_run(args)
    if args.system == "agentteams" and args.finalize is not None:
        if not finalize_run:
            parser.error("--finalize requires RUN_ID or --run-id RUN_ID")
        result = finalize_agentteams(
            client,
            run_id=finalize_run,
            selected=selected,
            plan=plan,
            output_dir=args.output_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.execute:
        print(
            json.dumps(
                {"status": "DRY_RUN", "system": args.system, "case": selected},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    ledger = _load_ledger(plan, LEDGER_PATH)
    seed = seed_index()[args.case_id]
    if args.system == "rule":
        started = datetime.now(timezone.utc)
        actual = decide_root_route(seed["manager_input_facts"])
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        row = {
            "system": "rule",
            "seed_case_id": args.case_id,
            "status": "EXECUTED",
            "actual_route": actual["route"],
            "actual_recommendation": actual["machine_recommendation"],
            "expected_recommendation": selected["expected_recommendation"],
            "latency_ms": round(elapsed, 6),
            "provider_tokens": 0,
            "model_calls": 0,
            "evidence": {"decision_sha256": actual.get("decision_sha256")},
        }
    elif args.system == "single-agent":
        if not args.observed_result or not args.observed_result.is_file():
            raise SystemExit(
                "single-agent禁止手填结论；请用--observed-result传入完整哈希绑定的机器导出JSON。"
            )
        row = validate_single_agent_export(
            read_json(args.observed_result), selected=selected, seed=seed
        )
    else:
        if not args.submission_id:
            parser.error("AgentTeams launch requires explicit --submission-id")
        result = launch_agentteams(
            client,
            selected=selected,
            submission_id=args.submission_id,
            plan=plan,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    upsert(ledger, row)
    persist(ledger)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
