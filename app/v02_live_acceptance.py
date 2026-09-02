from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from protocol_v02 import (
        ProtocolValidationError,
        validate_case_envelope as validate_formal_case_envelope,
        validate_datapass as validate_formal_datapass,
    )
except ModuleNotFoundError:  # package import in ``python -m unittest`` mode
    from .protocol_v02 import (
        ProtocolValidationError,
        validate_case_envelope as validate_formal_case_envelope,
        validate_datapass as validate_formal_datapass,
    )

try:
    from task_identity import TaskIdentityError, validate_role_task_ids
except ModuleNotFoundError:  # package import in ``python -m unittest`` mode
    from .task_identity import TaskIdentityError, validate_role_task_ids


DEMO_ROOT = Path(__file__).resolve().parent.parent
LIVE_ROOT = DEMO_ROOT / "app" / "runtime" / "live_intake"
DEFAULT_SOURCE_SUBMISSION_ID = "SUB-20260828214026-0715b24730"
DEFAULT_EXPECTED_EVIDENCE_SHA256 = (
    "0715b24730d09b2a22cbf357d24cbeae05699b7a08a07092409dad5c3203f56d"
)
REQUIRED_SKILLS = {
    "evidence-integrity": "1.0.0",
    "rights-gate": "1.0.0",
    "semantic-contract-resolver": "1.1.0",
    "financial-impact-calculator": "1.0.0",
    "independent-evidence-validator": "1.0.0",
}
REQUIRED_WORKERS = {
    "evidence-investigator",
    "semantic-impact-analyst",
    "independent-validator",
}
SKILL_OWNERS = {
    "evidence-integrity": "evidence-investigator",
    "rights-gate": "evidence-investigator",
    "semantic-contract-resolver": "semantic-impact-analyst",
    "financial-impact-calculator": "semantic-impact-analyst",
    "independent-evidence-validator": "independent-validator",
    "classify-data-rights": "data-rights-steward",
    "enforce-confidentiality-boundary": "data-rights-steward",
    "retrieve-research-context": "research-context-analyst",
    "verify-research-context": "research-context-analyst",
    "guard-execution-budget": "runtime-resilience-auditor",
    "audit-recovery-readiness": "runtime-resilience-auditor",
}
SUPPORTED_SKILLS = {
    **REQUIRED_SKILLS,
    "classify-data-rights": "1.0.0",
    "enforce-confidentiality-boundary": "1.0.0",
    "retrieve-research-context": "1.0.0",
    "verify-research-context": "1.0.0",
    "guard-execution-budget": "1.0.0",
    "audit-recovery-readiness": "1.0.0",
}
FINAL_HUMAN_STATES = {"APPROVED", "REJECTED", "RETURNED"}
AUDIT_PROTOCOL = "FINFLUX_AUDIT_BUNDLE_V0.2"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PROMPT_BUDGET_PROTOCOL = "FINFLUX_PROMPT_BUDGET_READINESS_V1"
TASK_CONVERGENCE_PROTOCOL = "FINFLUX_TASK_CONVERGENCE_RECEIPT_V1"
MANAGER_DISPATCH_PROTOCOL = "FINFLUX_MANAGER_DISPATCH_RECEIPT_V1.0"
GATEWAY_BINDING_PROTOCOL = "FINFLUX_MODEL_GATEWAY_RUN_BINDING_V1"
GATEWAY_LEDGER_PROTOCOL = "FINFLUX_MODEL_GATEWAY_LEDGER_V1"


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _route_execution_contract(
    run: dict[str, Any],
) -> tuple[set[str], dict[str, str], dict[str, str], list[str]]:
    """Resolve the exact Worker/Skill contract selected for this Run.

    V0.2 initially froze one three-Worker/five-Skill competition path.  The
    Manager may now add rights, research or resilience specialists.  The
    acceptance gate remains fail-closed against this Run's sealed route while
    requiring all core financial controls and rejecting unknown contracts.
    """
    failures: list[str] = []
    route = run.get("root_route_decision") or {}
    worker_plan = (route.get("worker_plan") or {}).get("workers") or []
    normalized_rows = [
        str(item).strip().lower().replace(" ", "-") for item in worker_plan
    ]
    workers = {item for item in normalized_rows if item}
    if len(normalized_rows) != len(workers) or not workers:
        failures.append("MANAGER_WORKER_PLAN_INVALID")
    declared_count = (route.get("worker_plan") or {}).get("count")
    if declared_count is not None and int(declared_count) != len(workers):
        failures.append("MANAGER_WORKER_COUNT_MISMATCH")
    if not REQUIRED_WORKERS.issubset(workers):
        failures.append("MANAGER_CORE_WORKER_SET_INCOMPLETE")

    raw_versions = route.get("required_skill_versions") or {}
    if not isinstance(raw_versions, dict):
        failures.append("MANAGER_REQUIRED_SKILLS_INVALID")
        raw_versions = {}
    skill_versions = {
        str(skill_id): str(version)
        for skill_id, version in raw_versions.items()
        if str(skill_id) and str(version)
    }
    if not set(REQUIRED_SKILLS).issubset(skill_versions):
        failures.append("MANAGER_CORE_SKILL_SET_INCOMPLETE")
    owners: dict[str, str] = {}
    for skill_id, version in skill_versions.items():
        supported_version = SUPPORTED_SKILLS.get(skill_id)
        owner = SKILL_OWNERS.get(skill_id)
        if supported_version is None or owner is None:
            failures.append(f"MANAGER_SKILL_UNSUPPORTED:{skill_id}")
            continue
        if version != supported_version:
            failures.append(f"MANAGER_SKILL_VERSION_UNSUPPORTED:{skill_id}")
        if owner not in workers:
            failures.append(f"MANAGER_SKILL_OWNER_NOT_ROUTED:{skill_id}")
        owners[skill_id] = owner
    if workers != set(owners.values()):
        failures.append("MANAGER_WORKER_SKILL_OWNERSHIP_MISMATCH")
    return workers, skill_versions, owners, failures


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 30,
) -> tuple[bytes, dict[str, str]]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc


def api_json(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    body = None
    method = "GET"
    content_type = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
        content_type = "application/json"
    raw, _ = _request(
        base_url,
        path,
        method=method,
        body=body,
        content_type=content_type,
        timeout=timeout,
    )
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return value


def api_bytes(base_url: str, path: str, *, timeout: int = 30) -> bytes:
    return _request(base_url, path, timeout=timeout)[0]


def multipart_body(
    filename: str, content: bytes, metadata: dict[str, Any]
) -> tuple[bytes, str]:
    boundary = "----FinFluxV02" + secrets.token_hex(12)
    chunks = []
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mimetypes.guess_type(filename)[0] or 'application/octet-stream'}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(b"\r\n")
    chunks.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json; charset=utf-8\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    chunks.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_evidence_bundle(
    base_url: str, filename: str, content: bytes, metadata: dict[str, Any]
) -> dict[str, Any]:
    body, content_type = multipart_body(filename, content, metadata)
    raw, _ = _request(
        base_url,
        "/api/v1/evidence-bundles",
        method="POST",
        body=body,
        content_type=content_type,
        timeout=60,
    )
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("evidence bundle endpoint did not return an object")
    return value


def list_submissions(base_url: str) -> list[dict[str, Any]]:
    value = api_json(base_url, "/api/v1/submissions")
    items = value.get("submissions") or []
    if not isinstance(items, list):
        raise RuntimeError("submissions payload is malformed")
    return [item for item in items if isinstance(item, dict)]


def source_submission(
    base_url: str, submission_id: str = DEFAULT_SOURCE_SUBMISSION_ID
) -> dict[str, Any]:
    for item in list_submissions(base_url):
        if item.get("submission_id") == submission_id:
            return item
    raise RuntimeError(f"source submission not found: {submission_id}")


def source_object_path(submission: dict[str, Any]) -> Path:
    relative = str((submission.get("file") or {}).get("immutable_object") or "")
    if not relative:
        raise RuntimeError("source submission has no immutable object")
    path = (LIVE_ROOT / relative).resolve()
    path.relative_to(LIVE_ROOT.resolve())
    if not path.is_file():
        raise RuntimeError(f"immutable source object is missing: {relative}")
    return path


def preflight(
    base_url: str,
    source_submission_id: str = DEFAULT_SOURCE_SUBMISSION_ID,
    expected_evidence_sha256: str = DEFAULT_EXPECTED_EVIDENCE_SHA256,
) -> dict[str, Any]:
    control = api_json(base_url, "/api/v1/control-plane/status")
    guard = api_json(base_url, "/api/v1/token-guard")
    runtime = api_json(base_url, "/api/agent/status")
    source = source_submission(base_url, source_submission_id)
    source_path = source_object_path(source)
    observed_sha = file_sha256(source_path)
    reasons: list[str] = []
    if not control.get("runtime_connected"):
        reasons.append("RUNTIME_NOT_CONNECTED")
    if int(control.get("topology_ready") or 0) != int(
        control.get("topology_expected") or -1
    ):
        reasons.append("TOPOLOGY_NOT_READY")
    if control.get("active_run_id"):
        reasons.append("ACTIVE_RUN_EXISTS")
    if not guard.get("allowed"):
        reasons.append("TOKEN_GUARD_BLOCKED")
    if not guard.get("provider_usage_captured"):
        reasons.append("PROVIDER_USAGE_NOT_CAPTURED")
    if not runtime.get("human_credentials_ready"):
        reasons.append("HUMAN_CREDENTIALS_NOT_READY")
    if source.get("profile") != "futures_settlement":
        reasons.append("SOURCE_PROFILE_MISMATCH")
    if str((source.get("metadata") or {}).get("candidate_mapping")) != "close":
        reasons.append("SOURCE_IS_NOT_CONFLICT_CONTROL")
    if observed_sha != expected_evidence_sha256:
        reasons.append("SOURCE_SHA256_MISMATCH")
    if str((source.get("file") or {}).get("sha256")) != observed_sha:
        reasons.append("SOURCE_RECORD_SHA256_MISMATCH")
    snapshot = {
        "protocol": "FINFLUX_FUTURES_V02_PREFLIGHT_V1.0",
        "captured_at_utc": utc_now(),
        "status": "READY" if not reasons else "BLOCKED",
        "reasons": reasons,
        "base_url": base_url,
        "source_submission_id": source_submission_id,
        "source_file": {
            "name": (source.get("file") or {}).get("name"),
            "sha256": observed_sha,
            "immutable_object": (source.get("file") or {}).get("immutable_object"),
        },
        "expected_route": "FULL_TEAM_REVIEW",
        "expected_workers": sorted(REQUIRED_WORKERS),
        "expected_skill_versions": REQUIRED_SKILLS,
        "control_plane": control,
        "token_guard": guard,
        "runtime": {
            "connected": runtime.get("connected"),
            "human_credentials_ready": runtime.get("human_credentials_ready"),
            "resources": runtime.get("resources"),
        },
        "model_or_api_called": False,
        "provider_tokens_added_by_preflight": 0,
        "truth_boundary": (
            "只读取控制面、Token Guard、Runtime与不可变源证据；不创建Run、不派发Agent、不调用模型。"
        ),
    }
    snapshot["snapshot_sha256"] = canonical_sha256(snapshot)
    return snapshot


def launch_metadata(source: dict[str, Any]) -> dict[str, Any]:
    original = source.get("metadata") or {}
    return {
        "profile": "futures_settlement",
        "declared_source": original.get("declared_source")
        or "AKShare / CFFEX 公开行情（2026-08-14）",
        "rights_basis": original.get("rights_basis")
        or "公开数据，仅用于竞赛POC验证；生产使用须复核许可",
        "declared_purpose": "daily_settlement_pnl",
        "provider": original.get("provider") or "AKShare / CFFEX",
        "candidate_mapping": "close",
        "target_instrument": "IF2608",
        "contract_multiplier": float(original.get("contract_multiplier") or 300),
        "multiplier_source": original.get("multiplier_source")
        or "CFFEX合约规格；竞赛POC固定版本",
        "notes": "V0.2同Run验收；复用不可变真实源字节，保留错误候选映射用于受控核验",
        "review_mode": "STANDARD",
        "asset_class": "futures",
        "confidentiality_class": "PUBLIC",
        "permitted_usage_scope": "EVALUATION_ONLY",
        "rights_review_required": False,
        "research_context_required": False,
        "operational_risk_review_required": False,
        "input_mode": "FILE",
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _sealed_session(value: dict[str, Any]) -> dict[str, Any]:
    sealed = {key: item for key, item in value.items() if key != "session_sha256"}
    sealed["session_sha256"] = canonical_sha256(sealed)
    return sealed


def _write_session(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    sealed = _sealed_session(value)
    atomic_json(path, sealed)
    value.clear()
    value.update(sealed)
    return value


def _reserve_session(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Use the session itself as an exclusive, durable one-shot launch lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sealed = _sealed_session(value)
    payload = json.dumps(
        sealed, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise RuntimeError(
            f"session file already exists; refusing duplicate launch: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Keep a valid launch reservation even when the initial write fails.
        # An operator must inspect it instead of an automatic retry creating a
        # second external submission or Run.
        raise
    value.clear()
    value.update(sealed)
    return value


def _validate_launch_response(run: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    if run.get("protocol") != "FINFLUX_LIVE_RUN_V0.2":
        failures.append("RUN_PROTOCOL_NOT_V02")
    envelope = run.get("case_envelope") or {}
    if envelope.get("protocol") != "FINFLUX_CASE_ENVELOPE_V0.2":
        failures.append("CASE_ENVELOPE_PROTOCOL_INVALID")
    if envelope.get("run_id") != run_id:
        failures.append("CASE_ENVELOPE_RUN_MISMATCH")
    route = run.get("root_route_decision") or {}
    if route.get("route") != "FULL_TEAM_REVIEW":
        failures.append("MANAGER_ROUTE_NOT_FULL_TEAM_REVIEW")
    workers, _skill_versions, _owners, route_failures = (
        _route_execution_contract(run)
    )
    failures.extend(route_failures)
    if not run.get("agentteams_run_id"):
        failures.append("AGENTTEAMS_RUN_ID_MISSING")
    elif run.get("agentteams_run_id") != run_id:
        failures.append("AGENTTEAMS_RUN_ID_MISMATCH")
    task_identity, identity_failures = _expected_task_identity(run, workers)
    failures.extend(identity_failures)
    if task_identity is not None:
        failures.extend(_validate_prompt_budget_readiness(run, task_identity))
        failures.extend(_validate_fresh_session(run, workers))
    return failures


def _failure_provider_usage_snapshot(base_url: str) -> dict[str, Any]:
    """Capture failure-time provider usage without inventing a zero value."""
    captured_at = utc_now()
    try:
        guard = api_json(base_url, "/api/v1/token-guard", timeout=30)
    except Exception as exc:
        return {
            "status": "NOT_CAPTURED",
            "captured_at_utc": captured_at,
            "error_class": type(exc).__name__,
            "http_detail": str(exc),
        }
    captured = guard.get("provider_usage_captured") is True
    snapshot = {
        "status": "CAPTURED" if captured else "NOT_CAPTURED",
        "captured_at_utc": captured_at,
        "provider_usage_captured": captured,
        "token_guard": guard,
    }
    snapshot["snapshot_sha256"] = canonical_sha256(snapshot)
    return snapshot


def _run_create_binding(
    submission_id: str, client_idempotency_key: str
) -> dict[str, str]:
    key = str(client_idempotency_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{16,160}", key):
        raise RuntimeError("client Run idempotency key is invalid")
    key_sha256 = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return {
        "client_idempotency_key_sha256": key_sha256,
        "run_create_request_sha256": canonical_sha256(
            {
                "submission_id": submission_id,
                "client_idempotency_key_sha256": key_sha256,
            }
        ),
    }


def _validate_run_create_binding(
    run: dict[str, Any],
    *,
    submission_id: str,
    client_idempotency_key_sha256: str,
    run_create_request_sha256: str,
) -> list[str]:
    failures: list[str] = []
    receipt = run.get("run_creation_idempotency") or {}
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    if run.get("submission_id") != submission_id:
        failures.append("RUN_CREATE_SUBMISSION_MISMATCH")
    if receipt.get("protocol") != "FINFLUX_RUN_CREATE_IDEMPOTENCY_V1.0":
        failures.append("RUN_CREATE_IDEMPOTENCY_RECEIPT_MISSING")
    if (
        receipt.get("client_idempotency_key_sha256")
        != client_idempotency_key_sha256
    ):
        failures.append("RUN_CREATE_IDEMPOTENCY_KEY_MISMATCH")
    if receipt.get("request_sha256") != run_create_request_sha256:
        failures.append("RUN_CREATE_REQUEST_BINDING_MISMATCH")
    if (
        not _valid_hash(receipt.get("receipt_sha256"))
        or receipt.get("receipt_sha256") != canonical_sha256(unsigned)
    ):
        failures.append("RUN_CREATE_IDEMPOTENCY_RECEIPT_INVALID")
    return failures


def reconcile_run_creation(
    base_url: str,
    *,
    submission_id: str,
    client_idempotency_key: str,
) -> dict[str, Any]:
    """Read the server attempt ledger; this endpoint never creates a Run."""
    return api_json(
        base_url,
        "/api/v1/run-creation-attempts/reconcile",
        {
            "submission_id": submission_id,
            "client_idempotency_key": client_idempotency_key,
        },
        timeout=30,
    )


def _definitive_create_rejection(exc: Exception) -> bool:
    detail = str(exc)
    return bool(re.search(r"HTTP (400|401|403|404|409|422) ", detail))


def launch(
    base_url: str,
    session_file: Path,
    source_submission_id: str = DEFAULT_SOURCE_SUBMISSION_ID,
) -> dict[str, Any]:
    check = preflight(base_url, source_submission_id)
    if check["status"] != "READY":
        raise RuntimeError(f"preflight blocked: {check['reasons']}")
    source = source_submission(base_url, source_submission_id)
    source_path = source_object_path(source)
    session: dict[str, Any] = {
        "protocol": "FINFLUX_FUTURES_V02_ACCEPTANCE_SESSION_V1.0",
        "created_at_utc": utc_now(),
        "status": "LAUNCH_INTENT_RECORDED",
        "base_url": base_url,
        "source_submission_id": source_submission_id,
        "source_sha256": file_sha256(source_path),
        "preflight_sha256": check["snapshot_sha256"],
        "preflight_snapshot": check,
        "submission_id": None,
        "submission": None,
        "run_id": None,
        "launch_response": None,
        "post_runs_count": 0,
        "human_decision_automated": False,
    }
    # This exclusive create is the launch lock.  It happens before the first
    # mutating HTTP request, so two local processes cannot each create a Run.
    _reserve_session(session_file, session)
    submission = post_evidence_bundle(
        base_url,
        str((source.get("file") or {}).get("name") or source_path.name),
        source_path.read_bytes(),
        launch_metadata(source),
    )
    if submission.get("execution_readiness") != "AGENTTEAMS_EXECUTABLE":
        session.update(
            {
                "status": "EVIDENCE_BUNDLE_NOT_EXECUTABLE",
                "submission_id": submission.get("submission_id"),
                "submission": submission,
                "submission_sha256": canonical_sha256(submission),
            }
        )
        _write_session(session_file, session)
        raise RuntimeError("new evidence bundle is not AGENTTEAMS_EXECUTABLE")
    session.update(
        {
            "status": "EVIDENCE_BUNDLE_CREATED",
            "submission_id": submission.get("submission_id"),
            "submission": submission,
            "submission_sha256": canonical_sha256(submission),
        }
    )
    _write_session(session_file, session)
    # This is the only Run-creation call in the runner.  The API automatically
    # dispatches when Runtime and Token Guard are ready; calling /dispatch again
    # would make the acceptance story ambiguous even though the backend is idempotent.
    # ``post_runs_count`` records attempts, not successful responses. Persist
    # it before the only mutating Run request so a 409 cannot disappear behind
    # the earlier EVIDENCE_BUNDLE_CREATED checkpoint.
    client_idempotency_key = "FVA-" + secrets.token_hex(24)
    create_binding = _run_create_binding(
        str(submission["submission_id"]), client_idempotency_key
    )
    session.update(
        {
            "status": "RUN_CREATE_ATTEMPTING",
            "post_runs_count": 1,
            "run_id": None,
            "run_create_attempted_at_utc": utc_now(),
            "client_idempotency_key": client_idempotency_key,
            **create_binding,
        }
    )
    _write_session(session_file, session)
    response_loss_reconciled = False
    reconciliation: dict[str, Any] | None = None
    try:
        run = api_json(
            base_url,
            "/api/v1/runs",
            {
                "submission_id": submission["submission_id"],
                "client_idempotency_key": client_idempotency_key,
            },
            timeout=90,
        )
    except Exception as exc:
        try:
            reconciliation = reconcile_run_creation(
                base_url,
                submission_id=str(submission["submission_id"]),
                client_idempotency_key=client_idempotency_key,
            )
        except Exception as reconcile_exc:
            reconciliation = {
                "protocol": "FINFLUX_RUN_CREATE_RECONCILIATION_V1.0",
                "status": "NOT_CAPTURED",
                "error_class": type(reconcile_exc).__name__,
                "http_detail": str(reconcile_exc),
            }
        reconciled_run = (reconciliation or {}).get("run")
        if (
            (reconciliation or {}).get("status") == "COMMITTED"
            and isinstance(reconciled_run, dict)
        ):
            run = reconciled_run
            response_loss_reconciled = True
        elif (
            _definitive_create_rejection(exc)
            and (reconciliation or {}).get("status") == "NOT_FOUND"
        ):
            session.update(
                {
                    "status": "RUN_CREATE_REJECTED",
                    "post_runs_count": 1,
                    "run_id": None,
                    "agentteams_run_id": None,
                    "launch_response": None,
                    "error_class": type(exc).__name__,
                    "http_detail": str(exc),
                    "failed_at_utc": utc_now(),
                    "run_creation_reconciliation": reconciliation,
                    "provider_usage_snapshot": _failure_provider_usage_snapshot(
                        base_url
                    ),
                }
            )
            _write_session(session_file, session)
            raise
        else:
            session.update(
                {
                    "status": "RUN_CREATE_OUTCOME_UNKNOWN",
                    "post_runs_count": 1,
                    "run_id": None,
                    "agentteams_run_id": None,
                    "launch_response": None,
                    "error_class": type(exc).__name__,
                    "http_detail": str(exc),
                    "failed_at_utc": utc_now(),
                    "run_creation_reconciliation": reconciliation,
                    "provider_usage_snapshot": _failure_provider_usage_snapshot(
                        base_url
                    ),
                }
            )
            _write_session(session_file, session)
            raise RuntimeError(
                "Run creation response is unknown; resume must reconcile the "
                "persisted idempotency key and must not POST a second Run"
            ) from exc
    run_id = str(run.get("run_id") or "")
    if not run_id:
        session.update(
            {
                "status": "RUN_CREATE_RESPONSE_INVALID",
                "launch_response": run,
                "launch_response_sha256": canonical_sha256(run),
            }
        )
        _write_session(session_file, session)
        raise RuntimeError("run endpoint returned no run_id")
    launch_failures = _validate_launch_response(run)
    launch_failures.extend(
        _validate_run_create_binding(
            run,
            submission_id=str(submission["submission_id"]),
            client_idempotency_key_sha256=create_binding[
                "client_idempotency_key_sha256"
            ],
            run_create_request_sha256=create_binding[
                "run_create_request_sha256"
            ],
        )
    )
    session.update(
        {
            "status": "RUN_LAUNCHED" if not launch_failures else "RUN_LAUNCH_REJECTED",
            "run_id": run_id,
            "agentteams_run_id": run.get("agentteams_run_id"),
            "post_runs_count": 1,
            "launch_response": run,
            "launch_response_sha256": canonical_sha256(run),
            "launch_validation_failures": launch_failures,
            "launched_at_utc": utc_now(),
            "response_loss_reconciled": response_loss_reconciled,
            "run_creation_reconciliation": reconciliation,
        }
    )
    _write_session(session_file, session)
    if launch_failures:
        raise RuntimeError(f"V0.2 launch response rejected: {launch_failures}")
    return {"session_file": str(session_file), "session": session, "run": run}


def _valid_hash(value: Any) -> bool:
    return bool(HEX64.fullmatch(str(value or "").lower()))


def _worker_id(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _human_binding_sha256(run_id: str, decision: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "run_id": run_id,
            "datapass_sha256": decision.get("datapass_sha256"),
            "decision": decision.get("decision"),
            "reason": str(decision.get("reason") or "").strip(),
            "decided_at_utc": decision.get("decided_at_utc"),
            "matrix_user_id": decision.get("matrix_user_id"),
            "event_id": decision.get("event_id"),
            "leader_room_receipt_sha256": decision.get(
                "leader_room_receipt_sha256"
            ),
        }
    )


def _parse_event_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_matrix_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize raw adapter traces and public Live Run Matrix projections."""

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source_name, rows in (
        ("agentteams_trace", run.get("agentteams_trace") or []),
        ("trace", run.get("trace") or []),
        ("events", run.get("events") or []),
    ):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            actor = row.get("actor") or {}
            event_id = str(row.get("event_id") or "")
            role = str(
                (actor.get("role") if isinstance(actor, dict) else "")
                or row.get("lane")
                or ""
            )
            sender = str(
                (actor.get("sender") if isinstance(actor, dict) else "")
                or row.get("sender")
                or ""
            )
            if role == "worker" and sender.startswith("@"):
                role = sender.split(":", 1)[0].lstrip("@")
            timestamp = str(row.get("timestamp_utc") or row.get("time") or "")
            room_id = str(row.get("room_id") or "")
            body = str(row.get("body") or row.get("summary") or "")
            key = (event_id, timestamp)
            if not event_id or key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "source": source_name,
                    "event_id": event_id,
                    "role": role,
                    "sender": sender,
                    "timestamp": timestamp,
                    "parsed_time": _parse_event_time(timestamp),
                    "room_id": room_id,
                    "body": body,
                }
            )
    return normalized


def _validate_human(run: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    gate = run.get("human_gate") or {}
    state = gate.get("state")
    decision = (run.get("agent_result") or {}).get("human_decision") or {}
    if state not in FINAL_HUMAN_STATES:
        return ["HUMAN_FINAL_STATE_MISSING"]
    if not isinstance(decision, dict) or not decision:
        return ["HUMAN_MATRIX_DECISION_MISSING"]
    if decision.get("scope") != "AGENTTEAMS_MATRIX_HUMAN_GATE":
        failures.append("HUMAN_SCOPE_INVALID")
    event_id = str(decision.get("event_id") or "")
    matrix_user_id = str(decision.get("matrix_user_id") or "")
    if not event_id.startswith("$"):
        failures.append("HUMAN_MATRIX_EVENT_ID_INVALID")
    if not matrix_user_id.startswith("@"):
        failures.append("HUMAN_MATRIX_USER_ID_INVALID")
    if decision.get("authenticated_principal") != matrix_user_id:
        failures.append("HUMAN_AUTHENTICATED_PRINCIPAL_MISMATCH")
    datapass = run.get("datapass") or {}
    formal_datapass_sha256 = str(datapass.get("datapass_sha256") or "")
    if not _valid_hash(decision.get("datapass_sha256")):
        failures.append("HUMAN_DATAPASS_HASH_INVALID")
    if (
        not _valid_hash(formal_datapass_sha256)
        or decision.get("datapass_sha256") != formal_datapass_sha256
    ):
        failures.append("HUMAN_FORMAL_DATAPASS_BINDING_MISMATCH")
    leader_room_id = str(
        (((run.get("session_hygiene") or {}).get("leader_room") or {}).get("room_id"))
        or ""
    )
    if not leader_room_id.startswith("!") or decision.get("room_id") != leader_room_id:
        failures.append("HUMAN_FRESH_LEADER_ROOM_BINDING_MISMATCH")
    expected_binding = _human_binding_sha256(run_id, decision)
    if decision.get("decision_binding_sha256") != expected_binding:
        failures.append("HUMAN_DECISION_BINDING_HASH_MISMATCH")
    if gate.get("post_decision_hash") != canonical_sha256(decision):
        failures.append("HUMAN_POST_DECISION_HASH_MISMATCH")
    expected_state = {
        "APPROVE_PASS": "APPROVED",
        "CONFIRM_BLOCK": "REJECTED",
        "REQUEST_EVIDENCE": "RETURNED",
    }.get(decision.get("decision"))
    if expected_state != state:
        failures.append("HUMAN_DECISION_STATE_MISMATCH")
    if gate.get("decision") != decision.get("decision"):
        failures.append("HUMAN_GATE_DECISION_MISMATCH")
    if gate.get("human_actor_id") not in {
        decision.get("reviewer"),
        matrix_user_id,
    }:
        failures.append("HUMAN_GATE_ACTOR_MISMATCH")
    if gate.get("decided_at") != decision.get("decided_at_utc"):
        failures.append("HUMAN_GATE_TIMESTAMP_MISMATCH")
    events = _run_matrix_events(run)
    matrix_event = next(
        (
            item
            for item in events
            if item.get("event_id") == event_id
            and item.get("source") in {"agentteams_trace", "trace"}
        ),
        None,
    )
    if not isinstance(matrix_event, dict):
        failures.append("HUMAN_MATRIX_EVENT_NOT_ARCHIVED")
    else:
        required_markers = {
            "HUMAN_DECISION",
            run_id,
            str(decision.get("decision") or ""),
            matrix_user_id,
            formal_datapass_sha256,
        }
        if (
            matrix_event.get("sender") != matrix_user_id
            or matrix_event.get("room_id") != decision.get("room_id")
            or any(
                not marker or marker not in str(matrix_event.get("body") or "")
                for marker in required_markers
            )
        ):
            failures.append("HUMAN_MATRIX_EVENT_CONTENT_INVALID")
        declared_time = _parse_event_time(decision.get("decided_at_utc"))
        event_time = matrix_event.get("parsed_time")
        if (
            declared_time is None
            or event_time is None
            or abs((event_time - declared_time).total_seconds()) > 120
        ):
            failures.append("HUMAN_MATRIX_EVENT_TIMESTAMP_INVALID")
        leader_event_id = str(
            (run.get("agent_result") or {}).get("leader_datapass_event_id") or ""
        )
        leader_event = next(
            (
                item
                for item in events
                if item.get("event_id") == leader_event_id
                and item.get("role") == "team_leader"
            ),
            None,
        )
        if (
            not isinstance(leader_event, dict)
            or leader_event.get("parsed_time") is None
            or event_time is None
            or event_time <= leader_event["parsed_time"]
        ):
            failures.append("HUMAN_DECISION_NOT_AFTER_DATAPASS_DRAFT")
    return failures


def _validate_worker_and_skill_evidence(
    run: dict[str, Any],
    artifacts: dict[str, Any],
    datapass: dict[str, Any],
    task_identity: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    expected_workers, expected_skills, expected_owners, route_failures = (
        _route_execution_contract(run)
    )
    failures.extend(route_failures)
    if set(artifacts) != expected_workers:
        failures.append("WORKER_ARTIFACT_SET_MISMATCH")
    seen_tasks: set[str] = set()
    raw_skills: dict[str, tuple[str, dict[str, Any]]] = {}
    for worker_id in sorted(artifacts):
        artifact = artifacts.get(worker_id)
        if not isinstance(artifact, dict):
            failures.append(f"WORKER_ARTIFACT_INVALID:{worker_id}")
            continue
        if artifact.get("run_id") != run_id:
            failures.append(f"WORKER_RUN_MISMATCH:{worker_id}")
        if _worker_id(artifact.get("worker_id", artifact.get("role"))) != worker_id:
            failures.append(f"WORKER_OWNER_MISMATCH:{worker_id}")
        task_id = str(artifact.get("task_id") or "")
        if task_id != str(task_identity["task_ids"].get(worker_id) or ""):
            failures.append(f"WORKER_TASK_ID_NOT_CANONICAL:{worker_id}")
        elif task_id in seen_tasks:
            failures.append(f"WORKER_TASK_ID_DUPLICATE:{worker_id}")
        else:
            seen_tasks.add(task_id)
        declared = str(artifact.get("artifact_sha256") or "")
        unsigned = {
            key: item for key, item in artifact.items() if key != "artifact_sha256"
        }
        if not _valid_hash(declared) or declared != canonical_sha256(unsigned):
            failures.append(f"WORKER_SELF_HASH_MISMATCH:{worker_id}")
        receipts = artifact.get("skill_invocations") or []
        if not isinstance(receipts, list):
            failures.append(f"WORKER_SKILL_RECEIPTS_INVALID:{worker_id}")
            continue
        for receipt in receipts:
            if not isinstance(receipt, dict):
                failures.append(f"WORKER_SKILL_RECEIPT_INVALID:{worker_id}")
                continue
            skill_id = str(receipt.get("skill_id") or "")
            if not skill_id:
                failures.append(f"WORKER_SKILL_ID_MISSING:{worker_id}")
            elif skill_id in raw_skills:
                failures.append(f"SKILL_ID_DUPLICATE:{skill_id}")
            else:
                raw_skills[skill_id] = (worker_id, receipt)

    invocations = ((datapass.get("skills") or {}).get("invocations") or [])
    if not isinstance(invocations, list) or len(invocations) != len(expected_skills):
        failures.append("DATAPASS_SKILL_COUNT_MISMATCH")
        invocations = invocations if isinstance(invocations, list) else []
    seen: set[str] = set()
    for item in invocations:
        if not isinstance(item, dict):
            failures.append("DATAPASS_SKILL_RECEIPT_INVALID")
            continue
        skill_id = str(item.get("skill_id") or "")
        if skill_id in seen:
            failures.append(f"SKILL_ID_DUPLICATE:{skill_id or 'EMPTY'}")
            continue
        seen.add(skill_id)
        expected_version = expected_skills.get(skill_id)
        expected_owner = expected_owners.get(skill_id)
        if expected_version is None:
            failures.append(f"SKILL_ID_UNEXPECTED:{skill_id or 'EMPTY'}")
            continue
        if str(item.get("version") or "") != expected_version:
            failures.append(f"SKILL_VERSION_MISMATCH:{skill_id}")
        if item.get("worker_id") != expected_owner:
            failures.append(f"SKILL_OWNER_MISMATCH:{skill_id}")
        if item.get("status") not in {"SUCCEEDED", "CACHE_HIT"}:
            failures.append(f"SKILL_STATUS_INVALID:{skill_id}")
        if not _valid_hash(item.get("input_sha256")) or not _valid_hash(
            item.get("output_sha256")
        ):
            failures.append(f"SKILL_IO_HASH_INVALID:{skill_id}")
        raw_owner, raw = raw_skills.get(skill_id, (None, {}))
        if raw_owner != expected_owner:
            failures.append(f"SKILL_RUN_OWNER_BINDING_MISSING:{skill_id}")
        if any(
            str(raw.get(key) or "") != str(item.get(key) or "")
            for key in ("version", "input_sha256", "output_sha256")
        ):
            failures.append(f"SKILL_WORKER_DATAPASS_BINDING_MISMATCH:{skill_id}")
    if seen != set(expected_skills):
        failures.append("DATAPASS_SKILL_SET_OR_VERSION_MISMATCH")

    try:
        validate_formal_datapass(
            datapass,
            envelope=run.get("case_envelope") or None,
            worker_artifacts=artifacts,
        )
    except ProtocolValidationError as exc:
        failures.append(f"FORMAL_DATAPASS_INVALID:{exc.field}")
    except Exception:
        failures.append("FORMAL_DATAPASS_INVALID:UNKNOWN")
    return failures


def _validate_partial_worker_artifacts(
    run: dict[str, Any], artifacts: dict[str, Any], task_identity: dict[str, Any]
) -> list[str]:
    """Validate facts already present without pretending the stage is complete."""
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    expected_workers, expected_skills, expected_owners, route_failures = (
        _route_execution_contract(run)
    )
    failures.extend(route_failures)
    if not set(artifacts).issubset(expected_workers):
        failures.append("WORKER_ARTIFACT_SET_HAS_UNEXPECTED_ROLE")
    task_ids: set[str] = set()
    skill_ids: set[str] = set()
    for worker_id, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            failures.append(f"WORKER_ARTIFACT_INVALID:{worker_id}")
            continue
        if artifact.get("run_id") != run_id:
            failures.append(f"WORKER_RUN_MISMATCH:{worker_id}")
        if _worker_id(artifact.get("worker_id", artifact.get("role"))) != worker_id:
            failures.append(f"WORKER_OWNER_MISMATCH:{worker_id}")
        task_id = str(artifact.get("task_id") or "")
        if task_id != str(task_identity["task_ids"].get(worker_id) or ""):
            failures.append(f"WORKER_TASK_ID_NOT_CANONICAL:{worker_id}")
        elif task_id in task_ids:
            failures.append(f"WORKER_TASK_ID_DUPLICATE:{worker_id}")
        else:
            task_ids.add(task_id)
        unsigned = {
            key: item for key, item in artifact.items() if key != "artifact_sha256"
        }
        if artifact.get("artifact_sha256") != canonical_sha256(unsigned):
            failures.append(f"WORKER_SELF_HASH_MISMATCH:{worker_id}")
        for receipt in artifact.get("skill_invocations") or []:
            if not isinstance(receipt, dict):
                failures.append(f"WORKER_SKILL_RECEIPT_INVALID:{worker_id}")
                continue
            skill_id = str(receipt.get("skill_id") or "")
            if skill_id in skill_ids:
                failures.append(f"SKILL_ID_DUPLICATE:{skill_id or 'EMPTY'}")
            skill_ids.add(skill_id)
            if expected_owners.get(skill_id) != worker_id:
                failures.append(f"SKILL_OWNER_MISMATCH:{skill_id or 'EMPTY'}")
            if str(receipt.get("version") or "") != expected_skills.get(skill_id):
                failures.append(f"SKILL_VERSION_MISMATCH:{skill_id or 'EMPTY'}")
            if not _valid_hash(receipt.get("input_sha256")) or not _valid_hash(
                receipt.get("output_sha256")
            ):
                failures.append(f"SKILL_IO_HASH_INVALID:{skill_id or 'EMPTY'}")
    return failures


def _self_hash_matches(value: Any, hash_field: str) -> bool:
    if not isinstance(value, dict):
        return False
    declared = str(value.get(hash_field) or "")
    unsigned = {key: item for key, item in value.items() if key != hash_field}
    return _valid_hash(declared) and declared == canonical_sha256(unsigned)


def _expected_task_identity(
    run: dict[str, Any], selected_workers: set[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    case_id = str(run.get("case_id") or (run.get("case_envelope") or {}).get("case_id") or "")
    handle = run.get("matrix_case_envelope_handle") or {}
    transported = handle.get("task_identity")
    if not isinstance(transported, dict):
        return None, ["TASK_IDENTITY_MISSING"]
    try:
        expected = validate_role_task_ids(
            transported,
            case_id=case_id,
            run_id=run_id,
            selected_workers=sorted(selected_workers),
        )
    except TaskIdentityError:
        return None, ["TASK_IDENTITY_DERIVATION_MISMATCH"]
    if set(expected["task_ids"]) != selected_workers:
        failures.append("TASK_IDENTITY_ROLE_SET_MISMATCH")
    return expected, failures


def _leader_worker_membership_valid(
    leader_room: dict[str, Any], workers: set[str], run_id: str
) -> bool:
    actor_id = str(leader_room.get("actor_id") or "")
    match = re.fullmatch(r"@finchange-case-lead:(\S+)", actor_id)
    if not match:
        return False
    domain = match.group(1)
    expected = {
        actor_id,
        *(f"@{worker}:{domain}" for worker in workers),
    }
    membership = leader_room.get("membership_receipt")
    return bool(
        isinstance(membership, dict)
        and membership.get("protocol")
        == "FINFLUX_MATRIX_JOINED_MEMBERSHIP_RECEIPT_V1"
        and membership.get("status") == "JOINED"
        and membership.get("run_id") == run_id
        and membership.get("room_id") == leader_room.get("room_id")
        and set(membership.get("expected_joined_actor_ids") or []) == expected
        and expected.issubset(
            set(membership.get("observed_joined_actor_ids") or [])
        )
        and membership.get("missing_actor_ids") == []
        and membership.get("membership_source") == "MATRIX_JOIN_STATE"
        and membership.get("model_called") is False
        and int(membership.get("provider_tokens", -1)) == 0
        and _self_hash_matches(membership, "receipt_sha256")
        and expected.issubset(set(leader_room.get("invited_actor_ids") or []))
    )


def _validate_prompt_budget_readiness(
    run: dict[str, Any], task_identity: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    readiness = run.get("prompt_budget_readiness")
    if not isinstance(readiness, dict):
        return ["PROMPT_BUDGET_READINESS_MISSING"]
    if readiness.get("protocol") != PROMPT_BUDGET_PROTOCOL:
        failures.append("PROMPT_BUDGET_PROTOCOL_INVALID")
    if readiness.get("status") != "READY" or readiness.get("evidence_mode") != "ZERO_MODEL":
        failures.append("PROMPT_BUDGET_NOT_READY")
    if not _self_hash_matches(readiness, "readiness_sha256"):
        failures.append("PROMPT_BUDGET_SELF_HASH_MISMATCH")
    if readiness.get("run_id") != run.get("run_id") or readiness.get("case_id") != run.get("case_id"):
        failures.append("PROMPT_BUDGET_RUN_BINDING_MISMATCH")
    if readiness.get("task_identity") != task_identity:
        failures.append("PROMPT_BUDGET_TASK_IDENTITY_MISMATCH")

    rooms = readiness.get("rooms")
    if not isinstance(rooms, list) or len(rooms) != 2:
        failures.append("PROMPT_BUDGET_FRESH_ROOM_SET_INVALID")
        rooms = []
    room_ids: set[str] = set()
    leader_room: dict[str, Any] | None = None
    for row in rooms:
        if not isinstance(row, dict):
            failures.append("PROMPT_BUDGET_ROOM_RECEIPT_INVALID")
            continue
        room_id = str(row.get("room_id") or "")
        if (
            not room_id.startswith("!")
            or room_id in room_ids
            or row.get("created_for_run_id") != run.get("run_id")
            or row.get("freshly_created") is not True
            or row.get("prior_session_exists") is not False
            or int(row.get("history_limit", -1)) != 0
            or not _self_hash_matches(row, "receipt_sha256")
        ):
            failures.append(f"PROMPT_BUDGET_ROOM_NOT_FRESH:{row.get('role') or 'UNKNOWN'}")
        if row.get("role") == "finchange-case-lead":
            leader_room = row
        room_ids.add(room_id)
    if leader_room is None or not _leader_worker_membership_valid(
        leader_room, set(task_identity["task_ids"]), str(run.get("run_id") or "")
    ):
        failures.append("PROMPT_BUDGET_WORKER_MEMBERSHIP_INVALID")

    expected_roles = {"manager", "finchange-case-lead", *task_identity["task_ids"]}
    readbacks = readiness.get("runtime_readbacks")
    if not isinstance(readbacks, list) or {
        str(item.get("role") or "") for item in readbacks if isinstance(item, dict)
    } != expected_roles:
        failures.append("PROMPT_BUDGET_RUNTIME_ROLE_SET_MISMATCH")
        readbacks = readbacks if isinstance(readbacks, list) else []
    for row in readbacks:
        if not isinstance(row, dict):
            failures.append("PROMPT_BUDGET_RUNTIME_READBACK_INVALID")
            continue
        role = str(row.get("role") or "UNKNOWN")
        tools = row.get("enabled_tools")
        max_iters = int(row.get("max_iters", 0))
        manager_runtime_valid = role != "manager" or (
            max_iters == 1
            and tools == []
            and row.get("system_prompt_files") == ["FINFLUX_MANAGER_PROTOCOL.md"]
            and _valid_hash(row.get("manager_protocol_prompt_sha256"))
        )
        leader_runtime_valid = role != "finchange-case-lead" or (
            max_iters == 3
            and row.get("system_prompt_files")
            == ["FINFLUX_CASE_LEAD_PROTOCOL.md"]
            and _valid_hash(row.get("case_lead_protocol_prompt_sha256"))
        )
        worker_runtime_valid = role in {"manager", "finchange-case-lead"} or (
            row.get("system_prompt_files")
            == ["FINFLUX_BOUNDED_WORKER_PROTOCOL.md"]
            and _valid_hash(row.get("worker_protocol_prompt_sha256"))
        )
        actor_iteration_valid = (
            max_iters == 1
            if role == "manager"
            else max_iters == 3
            if role == "finchange-case-lead"
            else 1 <= max_iters <= 2
        )
        tool_count_valid = (
            len(tools) == 0
            if role == "manager" and isinstance(tools, list)
            else isinstance(tools, list) and 1 <= len(tools) <= 3
        )
        expected_gateway_headers = {
            "X-FinFlux-Actor",
            "X-FinFlux-Identity",
            "X-FinFlux-Run-ID",
            "X-FinFlux-Task-ID",
        }
        if (
            int(row.get("history_limit", -1)) != 0
            or not actor_iteration_valid
            or not 1000 <= int(row.get("max_input_length", 0)) <= 12000
            or row.get("memory_prompt_enabled") is not False
            or row.get("memory_summary_enabled") is not False
            or row.get("force_memory_search") is not False
            or row.get("context_manager_backend") != "light"
            or row.get("memory_manager_backend") != "none"
            or row.get("context_strategy") != "native"
            or row.get("memory_tools_disabled") is not True
            or row.get("prompt_visible_internal_tools") != []
            or row.get("llm_retry_enabled") is not False
            or not tool_count_valid
            or not manager_runtime_valid
            or not leader_runtime_valid
            or not worker_runtime_valid
            or not _valid_hash(row.get("tool_profile_sha256"))
            or not _valid_hash(row.get("effective_config_sha256"))
            or row.get("model_gateway_headers_bound") is not True
            or row.get("model_gateway_run_id") != run.get("run_id")
            or row.get("model_gateway_actor") != role
            or not str(row.get("model_gateway_task_id") or "").startswith(
                str(task_identity.get("task_scope") or "") + "-"
            )
            or not _valid_hash(row.get("model_gateway_identity_sha256"))
            or not expected_gateway_headers.issubset(
                set(row.get("model_gateway_custom_header_names") or [])
            )
            or not str(row.get("model_gateway_provider_id") or "").strip()
        ):
            failures.append(f"PROMPT_BUDGET_RUNTIME_LIMIT_INVALID:{role}")

    namespace = readiness.get("task_namespace_receipt")
    if (
        not isinstance(namespace, dict)
        or namespace.get("task_ids") != task_identity["task_ids"]
        or namespace.get("preexisting_task_ids") != []
        or not _self_hash_matches(namespace, "receipt_sha256")
    ):
        failures.append("PROMPT_BUDGET_TASK_NAMESPACE_NOT_CLEAN")
    zero = readiness.get("zero_model_receipt")
    if not isinstance(zero, dict):
        failures.append("PROMPT_BUDGET_ZERO_MODEL_RECEIPT_MISSING")
    else:
        zero_fields = (
            "provider_requests_delta",
            "provider_tokens_delta",
            "model_triggering_events_delta",
        )
        if (
            any(int(zero.get(field, -1)) != 0 for field in zero_fields)
            or zero.get("provider_usage_captured_before") is not True
            or zero.get("provider_usage_captured_after") is not True
            or zero.get("provider_usage_date_before") != zero.get("provider_usage_date_after")
            or not _self_hash_matches(zero, "receipt_sha256")
        ):
            failures.append("PROMPT_BUDGET_ZERO_MODEL_PROOF_INVALID")
    return failures


def _validate_fresh_session(
    run: dict[str, Any], selected_workers: set[str] | None = None
) -> list[str]:
    receipt = run.get("session_hygiene")
    if not isinstance(receipt, dict):
        return ["FRESH_SESSION_RECEIPT_MISSING"]
    failures: list[str] = []
    if (
        receipt.get("protocol") != "FINFLUX_FRESH_RUN_ISOLATION_V1"
        or receipt.get("status") != "FRESH_ROOMS_AND_SESSIONS_VERIFIED"
        or receipt.get("model_called") is not False
        or receipt.get("legacy_clear_command_used") is not False
    ):
        failures.append("FRESH_SESSION_RECEIPT_INVALID")
    rooms = [receipt.get("manager_room"), receipt.get("leader_room")]
    if any(not isinstance(item, dict) for item in rooms):
        failures.append("FRESH_SESSION_ROOM_RECEIPT_MISSING")
    else:
        room_ids = {str(item.get("room_id") or "") for item in rooms}
        if len(room_ids) != 2:
            failures.append("FRESH_SESSION_ROOM_REUSED")
        for item in rooms:
            if (
                item.get("created_for_run_id") != run.get("run_id")
                or item.get("freshly_created") is not True
                or item.get("prior_session_exists") is not False
                or int(item.get("history_limit", -1)) != 0
                or not _self_hash_matches(item, "receipt_sha256")
            ):
                failures.append(f"FRESH_SESSION_ROOM_INVALID:{item.get('role') or 'UNKNOWN'}")
        leader_room = next(
            (
                item
                for item in rooms
                if item.get("role") == "finchange-case-lead"
            ),
            None,
        )
        workers = selected_workers
        if workers is None:
            workers, _skills, _owners, _route_failures = (
                _route_execution_contract(run)
            )
        if leader_room is None or not _leader_worker_membership_valid(
            leader_room, workers, str(run.get("run_id") or "")
        ):
            failures.append("FRESH_SESSION_WORKER_MEMBERSHIP_INVALID")
    return failures


def _validate_manager_authorization(
    run: dict[str, Any],
    task_identity: dict[str, Any],
    *,
    complete_claimed: bool,
) -> tuple[list[str], list[str]]:
    receipt = run.get("manager_dispatch_receipt")
    if not isinstance(receipt, dict):
        return ["MANAGER_AUTHORIZED_DISPATCH_RECEIPT_MISSING"], []
    failures: list[str] = []
    pending: list[str] = []
    workers = set(task_identity["task_ids"])
    if receipt.get("protocol") != MANAGER_DISPATCH_PROTOCOL:
        failures.append("MANAGER_DISPATCH_PROTOCOL_INVALID")
    if receipt.get("status") != "MANAGER_AUTHORIZED_DISPATCHED":
        failures.append("MANAGER_DISPATCH_NOT_RUNTIME_AUTHORIZED")
    if receipt.get("fallback") is not False:
        failures.append("MANAGER_DISPATCH_APPLICATION_FAILOVER_FORBIDDEN")
    if run.get("manager_dispatch_mode") != "REAL_MANAGER":
        failures.append("MANAGER_DISPATCH_MODE_NOT_REAL_MANAGER")
    if "failover_event_id" in receipt:
        failures.append("MANAGER_DISPATCH_HAS_FAILOVER_HISTORY")
    if receipt.get("run_id") != run.get("run_id") or receipt.get("case_id") != run.get("case_id"):
        failures.append("MANAGER_DISPATCH_RUN_BINDING_MISMATCH")
    if set(receipt.get("selected_workers") or []) != workers:
        failures.append("MANAGER_DISPATCH_WORKER_SET_MISMATCH")
    if receipt.get("task_identity_sha256") != canonical_sha256(task_identity):
        failures.append("MANAGER_DISPATCH_TASK_IDENTITY_HASH_MISMATCH")
    authorization_event_id = str(receipt.get("authorization_event_id") or "")
    leader_event_id = str(receipt.get("leader_room_event_id") or "")
    if not authorization_event_id.startswith("$"):
        failures.append("MANAGER_AUTHORIZATION_EVENT_MISSING")
    if not str(receipt.get("authorized_by") or "").startswith("@manager:"):
        failures.append("MANAGER_AUTHORIZED_ACTOR_INVALID")
    if not leader_event_id.startswith("$"):
        failures.append("MANAGER_TO_LEADER_EVENT_MISSING")
    if authorization_event_id == leader_event_id:
        failures.append("MANAGER_AUTHORIZATION_AND_RELAY_EVENTS_CONFLATED")

    relay = run.get("leader_relay") or (run.get("agentteams") or {}).get(
        "leader_relay"
    )
    manager_room_id = str(
        (((run.get("session_hygiene") or {}).get("manager_room") or {}).get("room_id"))
        or ""
    )
    leader_room_id = str(
        (((run.get("session_hygiene") or {}).get("leader_room") or {}).get("room_id"))
        or ""
    )
    if (
        not isinstance(relay, dict)
        or relay.get("protocol") != "FINFLUX_AUTHORIZED_LEADER_RELAY_V1"
        or relay.get("status") != "RELAY_SENT"
        or relay.get("fallback") is not False
        or relay.get("authorization_event_id") != authorization_event_id
        or relay.get("event_id") != leader_event_id
        or relay.get("room_id") != leader_room_id
        or not _self_hash_matches(relay, "receipt_sha256")
    ):
        failures.append("MANAGER_AUTHORIZATION_RELAY_BINDING_INVALID")

    events = _run_matrix_events(run)
    authorization_event = next(
        (
            item
            for item in events
            if item.get("event_id") == authorization_event_id
            and item.get("source") in {"agentteams_trace", "trace"}
        ),
        None,
    )
    relay_event = next(
        (
            item
            for item in events
            if item.get("event_id") == leader_event_id
            and item.get("source") in {"agentteams_trace", "trace"}
        ),
        None,
    )
    authorization_time = None
    relay_time = None
    if not isinstance(authorization_event, dict):
        target = failures if complete_claimed else pending
        target.append("MANAGER_AUTHORIZATION_TRACE_EVENT_MISSING")
    else:
        authorization_time = authorization_event.get("parsed_time")
        expected_authorization_body = (
            f"FINFLUX_MANAGER_DISPATCHED {run.get('case_id')} {run.get('run_id')} "
            f"{receipt.get('dispatch_idempotency_key')} "
            f"{receipt.get('dispatch_block_sha256')} "
            + str(
                (((run.get("session_hygiene") or {}).get("leader_room") or {}).get("actor_id"))
                or ""
            )
        )
        event_sender = str(authorization_event.get("sender") or "")
        if (
            authorization_event.get("role") != "manager"
            or authorization_event.get("room_id") != manager_room_id
            or event_sender != str(receipt.get("authorized_by") or "")
            or str(authorization_event.get("body") or "").strip()
            != expected_authorization_body
        ):
            failures.append("MANAGER_AUTHORIZATION_TRACE_EVENT_INVALID")
        declared_time = _parse_event_time(receipt.get("authorized_at_utc"))
        if (
            authorization_time is None
            or declared_time is None
            or declared_time != authorization_time
        ):
            failures.append("MANAGER_AUTHORIZATION_TIMESTAMP_INVALID")

    if not isinstance(relay_event, dict):
        target = failures if complete_claimed else pending
        target.append("AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING")
    else:
        relay_time = relay_event.get("parsed_time")
        relay_body = str(relay_event.get("body") or "")
        if (
            relay_event.get("room_id") != leader_room_id
            or str(relay_event.get("sender") or "")
            == str(receipt.get("authorized_by") or "")
            or "FINFLUX_LIVE_RELAY" not in relay_body
            or str(run.get("run_id") or "") not in relay_body
            or str(run.get("case_id") or "") not in relay_body
            or canonical_sha256(relay_body) != str(relay.get("message_sha256") or "")
        ):
            failures.append("AUTHORIZED_LEADER_RELAY_TRACE_EVENT_INVALID")
        if (
            authorization_time is None
            or relay_time is None
            or relay_time < authorization_time
        ):
            failures.append("AUTHORIZED_LEADER_RELAY_ORDER_INVALID")

    if authorization_time is not None:
        downstream_roles = {*task_identity["task_ids"], "team_leader"}
        downstream = [
            item
            for item in events
            if item.get("source") in {"agentteams_trace", "trace"}
            and item.get("role") in downstream_roles
            and item.get("parsed_time") is not None
        ]
        downstream_floor = max(
            item for item in (authorization_time, relay_time) if item is not None
        )
        if any(item["parsed_time"] < downstream_floor for item in downstream):
            failures.append("MANAGER_AUTHORIZATION_AFTER_DOWNSTREAM_EVENT")
        leader_datapass_event_id = str(
            ((run.get("agent_result") or {}).get("leader_datapass_event_id")) or ""
        )
        leader_final = (
            next(
                (
                    item
                    for item in events
                    if item.get("source") in {"agentteams_trace", "trace"}
                    and item.get("event_id") == leader_datapass_event_id
                ),
                None,
            )
            if leader_datapass_event_id
            else None
        )
        if not isinstance(leader_final, dict):
            target = failures if complete_claimed else pending
            target.append("MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING")
        elif (
            leader_final.get("role") != "team_leader"
            or leader_final.get("room_id") != leader_room_id
            or str(leader_final.get("sender") or "")
            != str(
                (((run.get("session_hygiene") or {}).get("leader_room") or {}).get("actor_id"))
                or ""
            )
            or leader_final.get("parsed_time") is None
        ):
            failures.append("MANAGER_TO_LEADER_FINAL_EVENT_BINDING_INVALID")
        elif leader_final["parsed_time"] < authorization_time:
            failures.append("MANAGER_AUTHORIZATION_AFTER_LEADER_FINAL")
    if not _self_hash_matches(receipt, "receipt_sha256"):
        failures.append("MANAGER_DISPATCH_SELF_HASH_MISMATCH")
    return failures, pending


def _validate_task_convergence(
    run: dict[str, Any], task_identity: dict[str, Any], artifacts: dict[str, Any]
) -> list[str]:
    receipt = run.get("task_convergence_receipt")
    if not isinstance(receipt, dict):
        return ["TASK_CONVERGENCE_RECEIPT_MISSING"]
    failures: list[str] = []
    expected = task_identity["task_ids"]
    observed = {
        role: {
            "task_id": str((artifacts.get(role) or {}).get("task_id") or ""),
            "artifact_sha256": str((artifacts.get(role) or {}).get("artifact_sha256") or ""),
        }
        for role in sorted(expected)
    }
    if receipt.get("protocol") != TASK_CONVERGENCE_PROTOCOL:
        failures.append("TASK_CONVERGENCE_PROTOCOL_INVALID")
    if receipt.get("run_id") != run.get("run_id") or receipt.get("case_id") != run.get("case_id"):
        failures.append("TASK_CONVERGENCE_RUN_BINDING_MISMATCH")
    if receipt.get("status") != "CONVERGED":
        failures.append("TASK_CONVERGENCE_NOT_COMPLETE")
    if receipt.get("task_scope") != task_identity.get("task_scope"):
        failures.append("TASK_CONVERGENCE_SCOPE_MISMATCH")
    if receipt.get("task_identity_sha256") != canonical_sha256(task_identity):
        failures.append("TASK_CONVERGENCE_TASK_IDENTITY_HASH_MISMATCH")
    if receipt.get("expected") != expected:
        failures.append("TASK_CONVERGENCE_EXPECTED_SET_MISMATCH")
    if receipt.get("observed") != observed:
        failures.append("TASK_CONVERGENCE_OBSERVED_SET_MISMATCH")
    for key in ("missing", "unexpected", "duplicate"):
        if receipt.get(key) != []:
            failures.append(f"TASK_CONVERGENCE_{key.upper()}_NOT_EMPTY")
    actual_directories = receipt.get("actual_task_directories")
    expected_directories = list(expected.values())
    if (
        not isinstance(actual_directories, list)
        or len(actual_directories) != len(set(actual_directories))
        or set(actual_directories) != set(expected_directories)
    ):
        failures.append("TASK_CONVERGENCE_DIRECTORY_SET_MISMATCH")
    if not _self_hash_matches(receipt, "receipt_sha256"):
        failures.append("TASK_CONVERGENCE_SELF_HASH_MISMATCH")
    return failures


def _validate_gateway_and_provider(run: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    run_id = str(run.get("run_id") or "")
    provider = run.get("provider_usage")
    binding = run.get("model_gateway_binding")
    if not isinstance(provider, dict):
        return ["PROVIDER_USAGE_MISSING"]
    if (
        provider.get("protocol") != "FINFLUX_PROVIDER_USAGE_LEDGER_V0.2"
        or provider.get("status") != "PROVIDER_REPORTED"
        or provider.get("attribution_status") != "DAILY_DELTA_EXCLUSIVE_ACTIVE_RUN"
        or provider.get("source") != "QWENPAW_TOKEN_USAGE_JSON_DELTA"
        or not _valid_hash(provider.get("baseline_snapshot_sha256"))
        or not _valid_hash(provider.get("current_snapshot_sha256"))
        or not _valid_hash(provider.get("attribution_sha256"))
    ):
        failures.append("PROVIDER_USAGE_ATTRIBUTION_INVALID")
    prompt = int(provider.get("prompt_tokens", -1) or 0)
    completion = int(provider.get("completion_tokens", -1) or 0)
    total = int(provider.get("total_tokens", -1) or 0)
    calls = int(provider.get("call_count", -1) or 0)
    if min(prompt, completion, total, calls) < 0 or total != prompt + completion or calls <= 0:
        failures.append("PROVIDER_USAGE_TOTALS_INVALID")
    ledger = provider.get("model_gateway_ledger")
    if not isinstance(ledger, dict):
        failures.append("MODEL_GATEWAY_LEDGER_MISSING")
    else:
        if (
            ledger.get("protocol") != GATEWAY_LEDGER_PROTOCOL
            or ledger.get("run_id") != run_id
            or ledger.get("status") == "FUSE_TRIPPED"
            or int(ledger.get("in_flight_reserved_tokens", -1)) != 0
            or (ledger.get("reservations") or {}) != {}
            or int(ledger.get("provider_call_count", -1)) != calls
            or int(ledger.get("prompt_tokens", -1)) != prompt
            or int(ledger.get("completion_tokens", -1)) != completion
            or int(ledger.get("total_tokens", -1)) != total
            or not _self_hash_matches(ledger, "ledger_sha256")
        ):
            failures.append("MODEL_GATEWAY_LEDGER_RECONCILIATION_FAILED")
    if not isinstance(binding, dict):
        failures.append("MODEL_GATEWAY_BINDING_MISSING")
    else:
        baseline_record = {
            "protocol": "FINFLUX_PROVIDER_USAGE_BASELINE_V1",
            "run_id": run_id,
            "captured_before_model_dispatch": True,
            "snapshot": run.get("provider_usage_baseline"),
            "cumulative_totals": {
                key: int((run.get("provider_usage_baseline") or {}).get(key, -1))
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "call_count",
                )
            },
        }
        baseline_record["baseline_sha256"] = canonical_sha256(baseline_record)
        # The model execution boundary closes before Human review.  A later
        # authenticated Human decision advances the business Run to
        # COMPLETED without reopening or rewriting the immutable gateway
        # receipt, so a completed Run must still bind to AWAITING_HUMAN here.
        expected_model_terminal_state = (
            "AWAITING_HUMAN"
            if run.get("state") == "COMPLETED"
            and isinstance((run.get("agent_result") or {}).get("human_decision"), dict)
            else run.get("state")
        )
        if (
            binding.get("protocol") != GATEWAY_BINDING_PROTOCOL
            or binding.get("run_id") != run_id
            or binding.get("state") != "CLOSED"
            or binding.get("terminal_state") != expected_model_terminal_state
            or binding.get("provider_usage_baseline_sha256") != baseline_record["baseline_sha256"]
            or not _self_hash_matches(binding, "binding_sha256")
        ):
            failures.append("MODEL_GATEWAY_BINDING_INVALID")
        readiness = run.get("prompt_budget_readiness") or {}
        runtime_readbacks = readiness.get("runtime_readbacks") or []
        runtime_identity_hashes = {
            str(item.get("role") or ""): str(
                item.get("model_gateway_identity_sha256") or ""
            )
            for item in runtime_readbacks
            if isinstance(item, dict)
        }
        runtime_task_ids = {
            str(item.get("role") or ""): str(
                item.get("model_gateway_task_id") or ""
            )
            for item in runtime_readbacks
            if isinstance(item, dict)
        }
        bound_identity_hashes = dict(binding.get("actor_identity_sha256") or {})
        bound_task_ids = dict(binding.get("actor_task_ids") or {})
        receipt = run.get("model_gateway_actor_binding_receipt")
        if (
            runtime_identity_hashes != bound_identity_hashes
            or runtime_task_ids != bound_task_ids
            or not isinstance(receipt, dict)
            or receipt.get("protocol")
            != "FINFLUX_MODEL_ACTOR_BINDING_RECEIPT_V1"
            or receipt.get("status") != "BOUND"
            or receipt.get("run_id") != run_id
            or receipt.get("actor_identity_sha256") != bound_identity_hashes
            or receipt.get("actor_task_ids") != bound_task_ids
            or receipt.get("plaintext_identity_persisted_in_run") is not False
            or not _self_hash_matches(receipt, "receipt_sha256")
        ):
            failures.append("MODEL_GATEWAY_ACTOR_BINDING_INVALID")
        cleanup = run.get("model_gateway_identity_cleanup")
        cleanup_rows = (
            (cleanup or {}).get("roles")
            if isinstance((cleanup or {}).get("roles"), list)
            else []
        )
        cleanup_roles = {
            str(item.get("role") or "")
            for item in cleanup_rows
            if isinstance(item, dict)
        }
        rows_clear = bool(cleanup_rows) and all(
            isinstance(item, dict)
            and int(item.get("finflux_headers_remaining", -1)) == 0
            and not any(
                str(name).lower().startswith("x-finflux-")
                for name in (item.get("remaining_custom_header_names") or [])
            )
            for item in cleanup_rows
        )
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("protocol")
            != "FINFLUX_MODEL_ACTOR_IDENTITY_CLEANUP_V1"
            or cleanup.get("status") != "CLEARED"
            or int(cleanup.get("finflux_headers_remaining", -1)) != 0
            or cleanup.get("model_or_provider_called") is not False
            or cleanup.get("run_id") != run_id
            or cleanup.get("gateway_binding_sha256")
            != binding.get("binding_sha256")
            or cleanup.get("actor_task_ids") != bound_task_ids
            or cleanup_roles != set(bound_task_ids)
            or len(cleanup_rows) != len(bound_task_ids)
            or not rows_clear
            or run.get("model_execution_seal_status") != "SEALED"
            or run.get("model_gateway_close_error")
            or run.get("model_gateway_identity_cleanup_error")
            or not _self_hash_matches(cleanup, "receipt_sha256")
        ):
            failures.append("MODEL_GATEWAY_IDENTITY_CLEANUP_INVALID")
    return failures


def validate_run(run: dict[str, Any], *, require_final: bool = False) -> dict[str, Any]:
    failures: list[str] = []
    pending: list[str] = []
    run_id = str(run.get("run_id") or "")
    if run.get("protocol") != "FINFLUX_LIVE_RUN_V0.2":
        failures.append("RUN_PROTOCOL_NOT_V02")
    envelope = run.get("case_envelope") or {}
    if envelope.get("protocol") != "FINFLUX_CASE_ENVELOPE_V0.2":
        failures.append("CASE_ENVELOPE_PROTOCOL_INVALID")
    if envelope.get("run_id") != run_id:
        failures.append("CASE_ENVELOPE_RUN_MISMATCH")
    try:
        validate_formal_case_envelope(envelope)
    except ProtocolValidationError as exc:
        failures.append(f"FORMAL_CASE_ENVELOPE_INVALID:{exc.field}")
    except Exception:
        failures.append("FORMAL_CASE_ENVELOPE_INVALID:UNKNOWN")
    route = run.get("root_route_decision") or {}
    if route.get("route") != "FULL_TEAM_REVIEW":
        failures.append("MANAGER_ROUTE_NOT_FULL_TEAM_REVIEW")
    normalized_plan, _versions, _owners, route_failures = (
        _route_execution_contract(run)
    )
    failures.extend(route_failures)
    if not run.get("agentteams_run_id"):
        failures.append("AGENTTEAMS_RUN_ID_MISSING")
    elif run.get("agentteams_run_id") != run_id:
        failures.append("AGENTTEAMS_RUN_ID_MISMATCH")
    task_identity, identity_failures = _expected_task_identity(run, normalized_plan)
    failures.extend(identity_failures)
    if task_identity is not None:
        failures.extend(_validate_prompt_budget_readiness(run, task_identity))
        failures.extend(_validate_fresh_session(run, normalized_plan))
    result = run.get("agent_result") or {}
    artifacts = result.get("worker_artifacts") or {}
    datapass = run.get("datapass") or {}
    complete_claimed = require_final or run.get("state") in {
        "AWAITING_HUMAN",
        "COMPLETED",
    }
    manager_receipt = run.get("manager_dispatch_receipt")
    if task_identity is not None:
        if isinstance(manager_receipt, dict) and manager_receipt.get("status") == "MANAGER_AUTHORIZED_DISPATCHED":
            manager_failures, manager_pending = _validate_manager_authorization(
                run,
                task_identity,
                complete_claimed=complete_claimed,
            )
            failures.extend(manager_failures)
            pending.extend(manager_pending)
        elif complete_claimed:
            failures.append("MANAGER_AUTHORIZED_DISPATCH_RECEIPT_MISSING")
        else:
            pending.append("MANAGER_AUTHORIZED_DISPATCH_PENDING")
    if not artifacts and not datapass:
        if complete_claimed:
            failures.extend(["WORKER_ARTIFACTS_MISSING", "DATAPASS_MISSING"])
        else:
            pending.extend(["WORKER_ARTIFACTS_PENDING", "DATAPASS_PENDING"])
    elif not artifacts:
        failures.append("WORKER_ARTIFACTS_MISSING")
    elif not datapass:
        if task_identity is None:
            failures.append("WORKER_TASK_IDENTITY_UNAVAILABLE")
        else:
            failures.extend(_validate_partial_worker_artifacts(run, artifacts, task_identity))
        if complete_claimed:
            failures.append("DATAPASS_MISSING")
        else:
            pending.append("DATAPASS_PENDING")
    else:
        if task_identity is None:
            failures.append("WORKER_TASK_IDENTITY_UNAVAILABLE")
        else:
            failures.extend(
                _validate_worker_and_skill_evidence(
                    run, artifacts, datapass, task_identity
                )
            )
            failures.extend(_validate_task_convergence(run, task_identity, artifacts))
        failures.extend(_validate_gateway_and_provider(run))
    provider = run.get("provider_usage") or {}
    if not artifacts and provider.get("status") != "PROVIDER_REPORTED":
        pending.append("PROVIDER_USAGE_PENDING")
    gate = run.get("human_gate") or {}
    if require_final:
        failures.extend(_validate_human(run))
        final_result = run.get("final_result") or {}
        manifest = final_result.get("manifest") or {}
        if manifest.get("run_id") != run_id:
            failures.append("RESULT_MANIFEST_RUN_MISMATCH")
        files = manifest.get("files") or {}
        if set(files) != {"markdown", "pdf", "json"}:
            failures.append("RESULT_FILE_SET_MISMATCH")
        for kind, descriptor in files.items():
            if not _valid_hash((descriptor or {}).get("sha256")):
                failures.append(f"RESULT_HASH_MISSING:{kind}")
    elif gate.get("state") in FINAL_HUMAN_STATES:
        failures.extend(_validate_human(run))
    validation_status = "FAIL" if failures else "PENDING" if pending else "PASS"
    return {
        "protocol": "FINFLUX_V02_LIVE_RUN_VALIDATION_V1.0",
        "validated_at_utc": utc_now(),
        "run_id": run_id,
        "status": validation_status,
        "failures": failures,
        "pending": pending,
        "state": run.get("state"),
        "lifecycle_phase": (run.get("lifecycle") or {}).get("current_phase"),
        "worker_artifact_count": len(artifacts),
        "skill_invocation_count": len(
            ((datapass.get("skills") or {}).get("invocations") or [])
        ),
        "datapass_protocol": datapass.get("protocol"),
        "human_state": gate.get("state"),
        "provider_tokens": provider.get("total_tokens"),
        "provider_usage_status": provider.get("status"),
        "require_final": require_final,
    }


def load_session(path: Path, *, expected_base_url: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RuntimeError("session file is malformed")
    declared = str(value.get("session_sha256") or "")
    unsigned = {key: item for key, item in value.items() if key != "session_sha256"}
    if not _valid_hash(declared) or declared != canonical_sha256(unsigned):
        raise RuntimeError("session integrity validation failed")
    if value.get("protocol") != "FINFLUX_FUTURES_V02_ACCEPTANCE_SESSION_V1.0":
        raise RuntimeError("session protocol is not the V0.2 acceptance protocol")
    if expected_base_url is not None and value.get("base_url") != expected_base_url:
        raise RuntimeError("session base_url does not match the requested control plane")
    if value.get("human_decision_automated") is not False:
        raise RuntimeError("session truth boundary is invalid")
    if int(value.get("post_runs_count") or 0) != 1:
        raise RuntimeError("session does not prove exactly one Run creation call")
    if not _valid_hash(value.get("source_sha256")):
        raise RuntimeError("session source hash is invalid")
    preflight_value = value.get("preflight_snapshot")
    if not isinstance(preflight_value, dict):
        raise RuntimeError("session preflight snapshot is missing")
    preflight_unsigned = {
        key: item for key, item in preflight_value.items() if key != "snapshot_sha256"
    }
    if (
        preflight_value.get("snapshot_sha256") != canonical_sha256(preflight_unsigned)
        or value.get("preflight_sha256") != preflight_value.get("snapshot_sha256")
        or preflight_value.get("source_submission_id")
        != value.get("source_submission_id")
        or ((preflight_value.get("source_file") or {}).get("sha256"))
        != value.get("source_sha256")
    ):
        raise RuntimeError("session preflight binding is invalid")
    submission = value.get("submission")
    if (
        not isinstance(submission, dict)
        or value.get("submission_sha256") != canonical_sha256(submission)
        or submission.get("submission_id") != value.get("submission_id")
        or ((submission.get("file") or {}).get("sha256"))
        != value.get("source_sha256")
    ):
        raise RuntimeError("session submission binding is invalid")
    client_key = str(value.get("client_idempotency_key") or "")
    create_binding = _run_create_binding(
        str(value.get("submission_id") or ""), client_key
    )
    if (
        value.get("client_idempotency_key_sha256")
        != create_binding["client_idempotency_key_sha256"]
        or value.get("run_create_request_sha256")
        != create_binding["run_create_request_sha256"]
    ):
        raise RuntimeError("session Run creation idempotency binding is invalid")
    if value.get("status") in {
        "RUN_CREATE_REJECTED",
        "RUN_CREATE_OUTCOME_UNKNOWN",
    }:
        usage = value.get("provider_usage_snapshot")
        if value.get("run_id") is not None or value.get("launch_response") is not None:
            raise RuntimeError("unreconciled session must not claim a Run")
        if not str(value.get("error_class") or "").strip() or not str(
            value.get("http_detail") or ""
        ).strip():
            raise RuntimeError("Run creation failure evidence is incomplete")
        if not str(value.get("failed_at_utc") or "").strip():
            raise RuntimeError("Run creation failure timestamp is missing")
        if not isinstance(usage, dict) or usage.get("status") not in {
            "CAPTURED",
            "NOT_CAPTURED",
        }:
            raise RuntimeError("Run creation provider usage evidence is missing")
        if usage.get("status") == "CAPTURED":
            unsigned_usage = {
                key: item for key, item in usage.items() if key != "snapshot_sha256"
            }
            if usage.get("snapshot_sha256") != canonical_sha256(unsigned_usage):
                raise RuntimeError("Run creation provider usage snapshot is invalid")
            if not isinstance(usage.get("token_guard"), dict) or (
                usage["token_guard"].get("provider_usage_captured") is not True
            ):
                raise RuntimeError("Run creation provider usage was not captured")
        if value.get("status") == "RUN_CREATE_OUTCOME_UNKNOWN":
            reconciliation = value.get("run_creation_reconciliation")
            if not isinstance(reconciliation, dict) or reconciliation.get(
                "status"
            ) not in {"NOT_CAPTURED", "NOT_FOUND", "PREPARED"}:
                raise RuntimeError(
                    "unknown Run creation outcome lacks reconciliation evidence"
                )
        return value
    launch_response = value.get("launch_response")
    if (
        not isinstance(launch_response, dict)
        or value.get("launch_response_sha256") != canonical_sha256(launch_response)
        or launch_response.get("run_id") != value.get("run_id")
        or launch_response.get("agentteams_run_id") != value.get("agentteams_run_id")
        or launch_response.get("submission_id") != value.get("submission_id")
    ):
        raise RuntimeError("session launch response binding is invalid")
    launch_failures = _validate_launch_response(launch_response)
    launch_failures.extend(
        _validate_run_create_binding(
            launch_response,
            submission_id=str(value.get("submission_id") or ""),
            client_idempotency_key_sha256=create_binding[
                "client_idempotency_key_sha256"
            ],
            run_create_request_sha256=create_binding[
                "run_create_request_sha256"
            ],
        )
    )
    if launch_failures or value.get("launch_validation_failures") not in ([], None):
        raise RuntimeError(f"session contains a rejected launch: {launch_failures}")
    return value


def reconcile_session_run_creation(
    base_url: str, session_file: Path
) -> dict[str, Any]:
    """Bind an uncertain session to its one durable Run without creating work."""
    session = load_session(session_file, expected_base_url=base_url)
    if session.get("run_id"):
        return {
            "status": "ALREADY_BOUND",
            "session": session,
            "run": session.get("launch_response"),
            "creates_run": False,
            "dispatches_model": False,
        }
    if session.get("status") != "RUN_CREATE_OUTCOME_UNKNOWN":
        raise RuntimeError(
            "session is not eligible for response-loss reconciliation"
        )
    reconciliation = reconcile_run_creation(
        base_url,
        submission_id=str(session["submission_id"]),
        client_idempotency_key=str(session["client_idempotency_key"]),
    )
    session["run_creation_reconciliation"] = reconciliation
    session["reconciled_at_utc"] = utc_now()
    run = reconciliation.get("run")
    if reconciliation.get("status") != "COMMITTED" or not isinstance(run, dict):
        _write_session(session_file, session)
        raise RuntimeError(
            "persisted Run creation attempt is not committed; refusing a second POST"
        )
    create_failures = _validate_run_create_binding(
        run,
        submission_id=str(session["submission_id"]),
        client_idempotency_key_sha256=str(
            session["client_idempotency_key_sha256"]
        ),
        run_create_request_sha256=str(session["run_create_request_sha256"]),
    )
    launch_failures = _validate_launch_response(run) + create_failures
    session.update(
        {
            "status": (
                "RUN_CREATE_RESPONSE_RECOVERED"
                if not launch_failures
                else "RUN_LAUNCH_REJECTED"
            ),
            "run_id": run.get("run_id"),
            "agentteams_run_id": run.get("agentteams_run_id"),
            "launch_response": run,
            "launch_response_sha256": canonical_sha256(run),
            "launch_validation_failures": launch_failures,
            "response_loss_reconciled": True,
        }
    )
    _write_session(session_file, session)
    if launch_failures:
        raise RuntimeError(
            f"reconciled Run failed V0.2 launch validation: {launch_failures}"
        )
    return {
        "status": "COMMITTED",
        "session": session,
        "run": run,
        "creates_run": False,
        "dispatches_model": False,
    }


def status(base_url: str, session_file: Path) -> dict[str, Any]:
    session = load_session(session_file, expected_base_url=base_url)
    run_id = str(session.get("run_id") or "")
    if not run_id:
        raise RuntimeError("session has no run_id; do not retry launch automatically")
    run = api_json(base_url, f"/api/v1/runs/{run_id}", timeout=60)
    if run.get("submission_id") != session.get("submission_id"):
        raise RuntimeError("live Run is not bound to the session submission")
    validation = validate_run(run)
    output = {"session_file": str(session_file), "run": run, "validation": validation}
    if run.get("state") == "AWAITING_HUMAN":
        output["next_action"] = (
            "PAUSE_FOR_REAL_HUMAN: open http://127.0.0.1:8768/#/human-gate; "
            "the runner never submits a Human decision"
        )
    return output


def _json_bytes(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"audit ZIP contains invalid JSON: {name}") from exc


def _validate_result_bytes(
    reports: dict[str, bytes],
    run_id: str,
    failures: list[str],
    *,
    expected_run: dict[str, Any] | None = None,
    expected_submission: dict[str, Any] | None = None,
) -> None:
    if not reports.get("pdf", b"").startswith(b"%PDF-"):
        failures.append("RESULT_PDF_MAGIC_INVALID")
    result_json = _json_bytes(reports.get("json", b""), "result/result.json")
    if not isinstance(result_json, dict) or result_json.get("run_id") != run_id:
        failures.append("RESULT_JSON_RUN_ID_MISMATCH")
        result_json = {}
    payload_hash = str(result_json.get("result_payload_sha256") or "")
    payload_unsigned = {
        key: value
        for key, value in result_json.items()
        if key != "result_payload_sha256"
    }
    if not _valid_hash(payload_hash) or payload_hash != canonical_sha256(
        payload_unsigned
    ):
        failures.append("RESULT_JSON_SELF_HASH_MISMATCH")
    markdown = reports.get("markdown", b"")
    if not markdown.strip() or run_id.encode("utf-8") not in markdown:
        failures.append("RESULT_MARKDOWN_RUN_ID_MISSING")
    if isinstance(expected_run, dict):
        datapass_sha = str(
            (expected_run.get("datapass") or {}).get("datapass_sha256") or ""
        )
        human = (expected_run.get("agent_result") or {}).get(
            "human_decision"
        ) or {}
        human_event = str(human.get("event_id") or "")
        human_hash = str(
            (expected_run.get("human_gate") or {}).get("post_decision_hash")
            or ""
        )
        tamper = result_json.get("tamper_evidence") or {}
        result_human = result_json.get("human_decision") or {}
        if result_json.get("case_id") != expected_run.get("case_id"):
            failures.append("RESULT_JSON_CASE_ID_MISMATCH")
        if result_json.get("submission_id") != expected_run.get("submission_id"):
            failures.append("RESULT_JSON_SUBMISSION_ID_MISMATCH")
        if (
            not _valid_hash(datapass_sha)
            or tamper.get("datapass_draft_sha256") != datapass_sha
        ):
            failures.append("RESULT_DATAPASS_HASH_MISMATCH")
        if not human_event or result_human.get("matrix_event_id") != human_event:
            failures.append("RESULT_HUMAN_EVENT_MISMATCH")
        if (
            not _valid_hash(human_hash)
            or tamper.get("human_post_decision_hash") != human_hash
        ):
            failures.append("RESULT_HUMAN_HASH_MISMATCH")
        for value, code in (
            (datapass_sha, "RESULT_MARKDOWN_DATAPASS_HASH_MISSING"),
            (human_hash, "RESULT_MARKDOWN_HUMAN_HASH_MISSING"),
        ):
            if value and value.encode("utf-8") not in markdown:
                failures.append(code)
    if isinstance(expected_submission, dict):
        source_sha = str(
            (expected_submission.get("file") or {}).get("sha256") or ""
        )
        if (
            not _valid_hash(source_sha)
            or (result_json.get("tamper_evidence") or {}).get(
                "source_file_sha256"
            )
            != source_sha
        ):
            failures.append("RESULT_SOURCE_HASH_MISMATCH")
        if source_sha and source_sha.encode("utf-8") not in markdown:
            failures.append("RESULT_MARKDOWN_SOURCE_HASH_MISSING")


def verify_audit_zip(
    content: bytes,
    run_id: str,
    *,
    expected_reports: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise RuntimeError("audit ZIP has no manifest.json")
        manifest = _json_bytes(archive.read("manifest.json"), "manifest.json")
        if not isinstance(manifest, dict):
            raise RuntimeError("audit ZIP manifest is not an object")
        failures: list[str] = []
        required = {
            "run.json",
            "submission.json",
            "lifecycle.json",
            "memory/run-memory.json",
            "observability.json",
            "receipts/skill-receipts.json",
            "receipts/tool-receipts.json",
            "human/human-decision.json",
            "workers/evidence-investigator.json",
            "workers/semantic-impact-analyst.json",
            "workers/independent-validator.json",
            "result/result.md",
            "result/result.pdf",
            "result/result.json",
        }
        for name in sorted(required - names):
            failures.append(f"AUDIT_FILE_MISSING:{name}")
        if manifest.get("protocol") != AUDIT_PROTOCOL:
            failures.append("AUDIT_MANIFEST_PROTOCOL_INVALID")
        manifest_unsigned = {
            key: item
            for key, item in manifest.items()
            if key != "manifest_payload_sha256"
        }
        if manifest.get("manifest_payload_sha256") != canonical_sha256(
            manifest_unsigned
        ):
            failures.append("AUDIT_MANIFEST_SELF_HASH_MISMATCH")
        listed_files = manifest.get("files") or {}
        if not isinstance(listed_files, dict):
            failures.append("AUDIT_MANIFEST_FILES_INVALID")
            listed_files = {}
        if not required.issubset(set(listed_files)):
            failures.append("AUDIT_REQUIRED_FILE_NOT_MANIFESTED")
        if set(listed_files) != names - {"manifest.json"}:
            failures.append("AUDIT_UNMANIFESTED_OR_PHANTOM_FILE")
        if int(manifest.get("file_count") or -1) != len(listed_files):
            failures.append("AUDIT_MANIFEST_FILE_COUNT_MISMATCH")
        for name, descriptor in listed_files.items():
            if name not in names:
                failures.append(f"MANIFEST_FILE_MISSING:{name}")
                continue
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if (
                not isinstance(descriptor, dict)
                or digest != str(descriptor.get("sha256"))
                or int(descriptor.get("bytes") or -1) != len(archive.read(name))
            ):
                failures.append(f"AUDIT_FILE_HASH_MISMATCH:{name}")

        json_entries = {
            "run": "run.json",
            "submission": "submission.json",
            "lifecycle": "lifecycle.json",
            "memory": "memory/run-memory.json",
            "operational_memory_plan": "memory/operational-memory-plan.json",
            "context_memory_lookup_receipt": "memory/context-memory-lookup-receipt.json",
            "context_memory_commit_receipt": "memory/context-memory-commit-receipt.json",
            "context_memory_remote_write_acceptance": "memory/context-memory-remote-write-acceptance.json",
            "observability": "observability.json",
            "skill_receipts": "receipts/skill-receipts.json",
            "tool_receipts": "receipts/tool-receipts.json",
            "human_decision": "human/human-decision.json",
            "emergency_stop": "control/emergency-stop.json",
        }
        components: dict[str, Any] = {}
        for key, name in json_entries.items():
            if name in names:
                components[key] = _json_bytes(archive.read(name), name)
        # ``audit_bundle_payload`` keeps optional components in the canonical
        # bundle payload even when their value is ``None``; it only omits them
        # from the per-component hash map and from the ZIP entries.  Preserve
        # that exact shape here, otherwise an untouched bundle without an
        # emergency-stop record fails its own aggregate hash verification.
        components.setdefault("emergency_stop", None)
        components["change_bundle"] = (
            _json_bytes(archive.read("change_bundle.json"), "change_bundle.json")
            if "change_bundle.json" in names
            else None
        )
        workers: dict[str, Any] = {}
        for worker_id in sorted(REQUIRED_WORKERS):
            name = f"workers/{worker_id}.json"
            if name in names:
                workers[worker_id] = _json_bytes(archive.read(name), name)
        components["worker_artifacts"] = workers

        declared_components = manifest.get("component_sha256") or {}
        if not isinstance(declared_components, dict):
            failures.append("AUDIT_COMPONENT_MANIFEST_INVALID")
            declared_components = {}
        expected_component_keys = {
            key for key, value in components.items() if value is not None
        }
        if set(declared_components) != expected_component_keys:
            failures.append("AUDIT_COMPONENT_SET_MISMATCH")
        for key in sorted(expected_component_keys):
            if declared_components.get(key) != canonical_sha256(components[key]):
                failures.append(f"AUDIT_COMPONENT_HASH_MISMATCH:{key}")

        bundle_payload = {
            "protocol": manifest.get("protocol"),
            **components,
            "component_sha256": declared_components,
            "truth_boundary": manifest.get("truth_boundary"),
        }
        # V0.2 bundles produced before the optional-control component was
        # introduced omitted the key entirely; current bundles bind it as
        # explicit JSON null.  Both encodings remain cryptographically
        # verifiable because the manifest carries the exact aggregate hash.
        bundle_candidates = [bundle_payload]
        if bundle_payload.get("emergency_stop") is None:
            bundle_candidates.append(
                {
                    key: value
                    for key, value in bundle_payload.items()
                    if key != "emergency_stop"
                }
            )
        if manifest.get("bundle_sha256") not in {
            canonical_sha256(candidate) for candidate in bundle_candidates
        }:
            failures.append("AUDIT_BUNDLE_HASH_MISMATCH")

        archived_run = components.get("run")
        if not isinstance(archived_run, dict) or archived_run.get("run_id") != run_id:
            failures.append("AUDIT_RUN_ID_MISMATCH")
        else:
            if (
                archived_run.get("agentteams_adapter_protocol")
                == "FINFLUX_AGENTTEAMS_RUN_V0.3"
            ):
                try:
                    from refactored_acceptance import validate_refactored_run
                except ModuleNotFoundError:  # pragma: no cover - package mode
                    from .refactored_acceptance import validate_refactored_run
                archived_validation = validate_refactored_run(
                    archived_run, require_final=True
                )
            else:
                archived_validation = validate_run(archived_run, require_final=True)
            if archived_validation["status"] != "PASS":
                failures.append("AUDIT_ARCHIVED_RUN_NOT_VALID_V02_FINAL")
            if components.get("human_decision") != archived_run.get("human_gate"):
                failures.append("AUDIT_HUMAN_COMPONENT_RUN_MISMATCH")

        report_bytes = {
            "markdown": archive.read("result/result.md")
            if "result/result.md" in names
            else b"",
            "pdf": archive.read("result/result.pdf")
            if "result/result.pdf" in names
            else b"",
            "json": archive.read("result/result.json")
            if "result/result.json" in names
            else b"",
        }
        _validate_result_bytes(
            report_bytes,
            run_id,
            failures,
            expected_run=archived_run if isinstance(archived_run, dict) else None,
            expected_submission=(
                components.get("submission")
                if isinstance(components.get("submission"), dict)
                else None
            ),
        )
        if expected_reports is not None:
            for kind, observed in report_bytes.items():
                if observed != expected_reports.get(kind):
                    failures.append(f"AUDIT_REPORT_DOWNLOAD_MISMATCH:{kind}")
        return {
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "file_count": len(names),
            "manifest_sha256": canonical_sha256(manifest),
        }


def finalize(base_url: str, session_file: Path, output_dir: Path) -> dict[str, Any]:
    session = load_session(session_file, expected_base_url=base_url)
    run_id = str(session.get("run_id") or "")
    if not run_id:
        raise RuntimeError("session has no run_id")
    run = api_json(base_url, f"/api/v1/runs/{run_id}", timeout=60)
    if run.get("submission_id") != session.get("submission_id"):
        raise RuntimeError("live Run is not bound to the session submission")
    gate = run.get("human_gate") or {}
    if gate.get("state") not in FINAL_HUMAN_STATES:
        raise RuntimeError(
            "Run is not Human-final; finalize refuses to impersonate Human or auto-sign"
        )
    # Refuse to ask the service to compose/export anything until the signed
    # Run itself proves every V0.2 evidence boundary.  This keeps a partial or
    # application-failover Run from manufacturing polished final files first
    # and only failing validation afterwards.
    preexport_validation = validate_run(run)
    if preexport_validation["status"] != "PASS":
        raise RuntimeError(
            "V0.2 pre-export run validation failed: "
            f"{preexport_validation['failures']}"
        )
    api_json(base_url, f"/api/v1/runs/{run_id}/final-result", timeout=60)
    run = api_json(base_url, f"/api/v1/runs/{run_id}", timeout=60)
    validation = validate_run(run, require_final=True)
    if validation["status"] != "PASS":
        raise RuntimeError(f"V0.2 run validation failed: {validation['failures']}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = (run.get("final_result") or {}).get("manifest") or {}
    downloads = {
        "markdown": f"/api/v1/runs/{run_id}/result.md",
        "pdf": f"/api/v1/runs/{run_id}/result.pdf",
        "json": f"/api/v1/runs/{run_id}/result.json",
    }
    downloaded: dict[str, Any] = {}
    report_bytes: dict[str, bytes] = {}
    for kind, endpoint in downloads.items():
        content = api_bytes(base_url, endpoint, timeout=60)
        report_bytes[kind] = content
        descriptor = (manifest.get("files") or {}).get(kind) or {}
        digest = hashlib.sha256(content).hexdigest()
        if digest != descriptor.get("sha256"):
            raise RuntimeError(f"downloaded {kind} hash does not match result manifest")
        suffix = {"markdown": ".md", "pdf": ".pdf", "json": ".json"}[kind]
        target = output_dir / f"result{suffix}"
        target.write_bytes(content)
        downloaded[kind] = {
            "path": str(target),
            "sha256": digest,
            "bytes": len(content),
        }
    result_failures: list[str] = []
    _validate_result_bytes(
        report_bytes,
        run_id,
        result_failures,
        expected_run=run,
    )
    if result_failures:
        raise RuntimeError(f"result artifact validation failed: {result_failures}")
    zip_content = api_bytes(base_url, f"/api/v1/runs/{run_id}/audit-bundle.zip", timeout=60)
    zip_result = verify_audit_zip(
        zip_content, run_id, expected_reports=report_bytes
    )
    if zip_result["status"] != "PASS":
        raise RuntimeError(f"audit ZIP validation failed: {zip_result['failures']}")
    zip_path = output_dir / f"{run_id}-audit.zip"
    zip_path.write_bytes(zip_content)
    receipt = {
        "protocol": "FINFLUX_FUTURES_V02_FINAL_ACCEPTANCE_V1.0",
        "validated_at_utc": utc_now(),
        "run_id": run_id,
        "status": "PASS",
        "run_validation": validation,
        "downloaded": downloaded,
        "audit_zip": {
            **zip_result,
            "path": str(zip_path),
            "sha256": hashlib.sha256(zip_content).hexdigest(),
            "bytes": len(zip_content),
        },
        "truth_boundary": (
            "Human决定取自已认证Matrix主体；验收器只读取、下载和复算哈希，不提交Human决定。"
        ),
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_json(output_dir / "acceptance-receipt.json", receipt)
    session.update(
        {
            "status": "FINALIZED",
            "finalized_at_utc": utc_now(),
            "acceptance_receipt": str(output_dir / "acceptance-receipt.json"),
            "acceptance_receipt_sha256": receipt["receipt_sha256"],
        }
    )
    _write_session(session_file, session)
    return receipt
