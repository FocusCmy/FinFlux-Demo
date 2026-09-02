from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from context_capsule import load_role_context_slice, resolve_role_context_slice
from task_identity import build_role_task_ids


PROTOCOL = "FINFLUX_TOOL_EXECUTION_RECEIPT_V1.0"
POLICY_ID = "FINFLUX_ALLOWLIST_TOOL_GATEWAY_V1"
ENTRYPOINTS = {
    "bounded-worker": "bounded_worker_task.py",
    "signed-worker": "bounded_worker_task.py",
    "bounded-change": "bounded_change_task.py",
}
ALLOWED_FLAGS = {
    "bounded-worker": {
        "--role",
        "--asset",
        "--case-id",
        "--run-id",
        "--task-id",
        "--policy-id",
        "--scenario",
        "--live-payload-b64",
        "--context-capsule-ref",
        "--proposed-field",
        "--proposed-semantic",
        "--confidence-bps",
        "--reason-code",
        "--uncertainty-code",
    },
    "signed-worker": {
        "--role",
        "--context-capsule-ref",
        "--proposed-field",
        "--proposed-semantic",
        "--confidence-bps",
        "--reason-code",
        "--uncertainty-code",
    },
    "bounded-change": {
        "--case-id",
        "--run-id",
        "--task-id",
        "--policy-id",
        "--change-payload-b64",
    },
}
REQUIRED_FLAGS = {
    "bounded-worker": {
        "--role",
        "--asset",
        "--case-id",
        "--run-id",
        "--task-id",
        "--policy-id",
        "--scenario",
    },
    "signed-worker": {
        "--role",
        "--context-capsule-ref",
    },
    "bounded-change": {
        "--case-id",
        "--run-id",
        "--task-id",
        "--policy-id",
        "--change-payload-b64",
    },
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_tool_args(entry: str, raw_args: list[str]) -> dict[str, str]:
    args = list(raw_args)
    if args and args[0] == "--":
        args = args[1:]
    if not args or len(args) % 2:
        raise ValueError("tool arguments must be flag/value pairs")
    allowed = ALLOWED_FLAGS[entry]
    values: dict[str, str] = {}
    for index in range(0, len(args), 2):
        flag, value = args[index], args[index + 1]
        if flag not in allowed:
            raise ValueError(f"tool argument is not allowlisted: {flag}")
        if flag in values:
            raise ValueError(f"duplicate tool argument: {flag}")
        if not value or "\x00" in value or len(value) > 12000:
            raise ValueError(f"invalid value for {flag}")
        values[flag] = value
    missing = REQUIRED_FLAGS[entry] - values.keys()
    if missing:
        raise ValueError(f"missing required tool arguments: {sorted(missing)}")
    task_id = values.get("--task-id")
    if task_id is not None and not re.fullmatch(r"task-[0-9A-Za-z._-]{8,220}", task_id):
        raise ValueError("task-id is outside the bounded task namespace")
    if entry in {"bounded-worker", "signed-worker"}:
        has_inline = "--live-payload-b64" in values
        has_capsule = "--context-capsule-ref" in values
        if has_inline and has_capsule:
            raise ValueError("live payload and context capsule transports are mutually exclusive")
        capsule_ref = values.get("--context-capsule-ref", "")
        if capsule_ref and not re.fullmatch(r"[0-9a-f]{64}", capsule_ref):
            raise ValueError("context capsule reference must be a lowercase SHA-256")
    return values


def _expand_signed_worker(values: dict[str, str]) -> dict[str, str]:
    """Expand a short Matrix command only from its verified role slice."""

    resolved = resolve_role_context_slice(
        values["--context-capsule-ref"], values["--role"]
    )
    payload = dict(resolved["payload"])
    case_id = str(resolved["case_id"])
    run_id = str(resolved["run_id"])
    role = values["--role"]
    task_id = build_role_task_ids(case_id, run_id, [role])["task_ids"][role]
    gate = str(payload.get("g") or "WAIT").upper()
    scenario = (
        "post_remediation_review"
        if case_id.endswith("-REMEDIATION")
        else "admissible"
        if gate == "PASS"
        else "blocked"
    )
    profile_asset = {
        "futures_settlement": "futures",
        "equity_corporate_action": "equity",
        "option_contract_identity": "option",
    }.get(str(payload.get("f") or ""), "")
    expanded = {
        "--role": role,
        "--asset": str(payload.get("a") or profile_asset),
        "--case-id": case_id,
        "--run-id": run_id,
        "--task-id": task_id,
        "--policy-id": "FINFLUX-BOUNDED-EXECUTION-V0.1",
        "--scenario": scenario,
        "--context-capsule-ref": values["--context-capsule-ref"],
    }
    for flag in (
        "--proposed-field", "--proposed-semantic", "--confidence-bps",
        "--reason-code", "--uncertainty-code",
    ):
        if flag in values:
            expanded[flag] = values[flag]
    # Reuse the complete bounded-worker validator after signed expansion.
    flattened = [item for pair in expanded.items() for item in pair]
    return validate_tool_args("bounded-worker", flattened)


def _bind_signed_context_recipe(values: dict[str, str]) -> str | None:
    """Derive execution policy only from the verified role Context Slice.

    The caller cannot supply ``--execution-recipe-id``.  This prevents a
    Matrix command from retaining a valid slice hash while swapping the
    operational recipe to another allowlisted value.
    """
    slice_ref = values.get("--context-capsule-ref", "")
    if not slice_ref:
        return None
    payload = load_role_context_slice(
        slice_ref,
        values["--role"],
        case_id=values["--case-id"],
        run_id=values["--run-id"],
    )
    recipe_id = str(payload.get("context_execution_recipe_id") or "")
    if not recipe_id:
        raise ValueError("signed context slice has no execution recipe")
    values["--execution-recipe-id"] = recipe_id
    return recipe_id


def _receipt_path(task_id: str) -> Path:
    root = Path(
        os.environ.get(
            "FINFLUX_TASK_ROOT",
            "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/tasks",
        )
    )
    target = (root / task_id).resolve()
    target.relative_to(root.resolve())
    target.mkdir(parents=True, exist_ok=True)
    return target / "tool_execution_receipt.json"


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """Hash a JSON object without trusting its declared receipt hash."""
    unsigned = dict(payload)
    unsigned.pop("receipt_sha256", None)
    return _sha256_bytes(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _artifact_manifest(task_dir: Path) -> dict[str, Any]:
    """Describe immutable task outputs used by the execution cache.

    Cache receipts and temporary files are deliberately excluded.  Reusing a
    successful Tool call is safe only while every output produced by that call
    still has the same byte hash.  This is an execution cache, not a financial
    truth cache: it never changes the Worker result and never calls a model.
    """
    excluded = {
        "tool_execution_receipt.json",
        "tool_cache_latest.json",
    }
    artifacts: list[dict[str, Any]] = []
    if task_dir.is_dir():
        for path in sorted(task_dir.iterdir(), key=lambda item: item.name):
            if (
                not path.is_file()
                or path.name in excluded
                or path.suffix == ".tmp"
            ):
                continue
            content = path.read_bytes()
            artifacts.append(
                {
                    "name": path.name,
                    "size_bytes": len(content),
                    "sha256": _sha256_bytes(content),
                }
            )
    body = {
        "protocol": "FINFLUX_TASK_ARTIFACT_MANIFEST_V1",
        "artifacts": artifacts,
    }
    return {**body, "manifest_sha256": _sha256_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )}


def execute(entry: str, raw_args: list[str], timeout_s: int) -> tuple[int, str, str, dict[str, Any]]:
    values = validate_tool_args(entry, raw_args)
    if entry == "signed-worker":
        values = _expand_signed_worker(values)
    script = Path(__file__).resolve().with_name(ENTRYPOINTS[entry])
    if not script.is_file():
        raise FileNotFoundError(f"allowlisted tool entrypoint is missing: {script.name}")
    args = (
        [item for pair in values.items() for item in pair]
        if entry == "signed-worker"
        else list(raw_args[1:] if raw_args and raw_args[0] == "--" else raw_args)
    )
    signed_recipe_id = (
        _bind_signed_context_recipe(values)
        if entry in {"bounded-worker", "signed-worker"}
        else None
    )
    if signed_recipe_id:
        args.extend(["--execution-recipe-id", signed_recipe_id])
    command = [sys.executable, str(script), *args]
    entrypoint_sha256 = _sha256_bytes(script.read_bytes())
    arguments_sha256 = _sha256_bytes(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    receipt_path = _receipt_path(values["--task-id"])
    result_path = receipt_path.parent / "result.md"
    if receipt_path.is_file() and result_path.is_file():
        try:
            cached = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            cached = {}
        cached_receipt_valid = (
            bool(cached.get("receipt_sha256"))
            and cached.get("receipt_sha256") == _canonical_payload_sha256(cached)
        )
        current_artifacts = _artifact_manifest(receipt_path.parent)
        declared_artifacts = cached.get("artifact_manifest")
        artifacts_valid = (
            not declared_artifacts  # backward-compatible read of V1 receipts
            or declared_artifacts == current_artifacts
        )
        cache_identity_matches = (
            cached.get("status") == "SUCCEEDED"
            and cached.get("entrypoint_sha256") == entrypoint_sha256
            and cached.get("arguments_sha256") == arguments_sha256
        )
        if cache_identity_matches and not (cached_receipt_valid and artifacts_valid):
            failure = {
                "protocol": "FINFLUX_TOOL_CACHE_INTEGRITY_FAILURE_V1",
                "policy_id": POLICY_ID,
                "entry": entry,
                "task_id": values["--task-id"],
                "run_id": values["--run-id"],
                "case_id": values["--case-id"],
                "status": "CACHE_INTEGRITY_FAILED",
                "return_code": 65,
                "source_receipt_hash_valid": cached_receipt_valid,
                "artifact_manifest_valid": artifacts_valid,
                "observed_artifact_manifest": current_artifacts,
                "failed_at_utc": _utc_now(),
                "provider_tokens": 0,
                "retry_policy": "FAIL_CLOSED_NO_REEXECUTION",
                "token_savings_claim": "NO_PROVIDER_TOKEN_CLAIM",
            }
            failure["receipt_sha256"] = _canonical_payload_sha256(failure)
            _write_receipt(
                receipt_path.parent / "tool_cache_integrity_failure.json", failure
            )
            return (
                65,
                "",
                "cached task artifact integrity check failed\n",
                failure,
            )
        if (
            cached_receipt_valid
            and artifacts_valid
            and cache_identity_matches
        ):
            cache_view = dict(cached)
            cache_view["cache_source_receipt_sha256"] = cached.get("receipt_sha256")
            cache_view["cache_hit"] = True
            cache_view["cache_hit_at_utc"] = _utc_now()
            cache_view["cache_policy"] = (
                "TASK_ID_PLUS_ENTRYPOINT_ARGUMENT_AND_ARTIFACT_HASH_V2"
            )
            cache_view["artifact_manifest"] = current_artifacts
            cache_view["avoided_subprocess_invocations"] = 1
            cache_view["provider_tokens"] = 0
            cache_view["token_savings_claim"] = (
                "NO_PROVIDER_TOKEN_CLAIM; DETERMINISTIC_TOOL_REEXECUTION_AVOIDED"
            )
            cache_view.pop("receipt_sha256", None)
            cache_view["receipt_sha256"] = _canonical_payload_sha256(cache_view)
            # Preserve the original execution receipt.  The latest replay is a
            # projection only; the source execution evidence remains immutable.
            _write_receipt(receipt_path.parent / "tool_cache_latest.json", cache_view)
            cache_stdout = json.dumps(
                {
                    "status": "SUCCEEDED",
                    "cache_hit": True,
                    "result_path": str(result_path),
                    "tool_receipt_sha256": cache_view["receipt_sha256"],
                },
                ensure_ascii=False,
            )
            return 0, cache_stdout + "\n", "", cache_view
    started_at = _utc_now()
    started = time.monotonic()
    status = "FAILED"
    return_code = 1
    stdout = ""
    stderr = ""
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            env=os.environ.copy(),
        )
        return_code = int(completed.returncode)
        stdout = completed.stdout
        stderr = completed.stderr
        status = "SUCCEEDED" if return_code == 0 else "FAILED"
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        status = "TIMED_OUT"
    finished_at = _utc_now()
    receipt: dict[str, Any] = {
        "protocol": PROTOCOL,
        "policy_id": POLICY_ID,
        "entry": entry,
        "entrypoint_sha256": entrypoint_sha256,
        "task_id": values["--task-id"],
        "run_id": values["--run-id"],
        "case_id": values["--case-id"],
        "argument_names": sorted(values),
        "arguments_sha256": arguments_sha256,
        "status": status,
        "return_code": return_code,
        "timeout_seconds": timeout_s,
        "timed_out": timed_out,
        "retry_policy": "NO_IMPLICIT_RETRY",
        "retry_count": 0,
        "cache_hit": False,
        "cache_policy": "TASK_ID_PLUS_ENTRYPOINT_ARGUMENT_AND_ARTIFACT_HASH_V2",
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
        "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
        "provider_tokens": 0,
        "context_transport": (
            "CONTENT_ADDRESSED_ROLE_SLICE"
            if "--context-capsule-ref" in values
            else "LEGACY_INLINE_PAYLOAD"
            if "--live-payload-b64" in values
            else "STATIC_CASE"
        ),
        "context_capsule_sha256": values.get("--context-capsule-ref"),
        "execution_recipe_id": signed_recipe_id,
        "execution_recipe_source": (
            "SIGNED_ROLE_CONTEXT_SLICE" if signed_recipe_id else "NOT_APPLICABLE"
        ),
        "artifact_manifest": _artifact_manifest(receipt_path.parent),
    }
    receipt["receipt_sha256"] = _canonical_payload_sha256(receipt)
    _write_receipt(receipt_path, receipt)
    return return_code, stdout, stderr, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="FinFlux allowlist Tool Gateway")
    parser.add_argument("--entry", required=True, choices=sorted(ENTRYPOINTS))
    parser.add_argument("--timeout-s", type=int, default=90)
    parser.add_argument("tool_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.timeout_s <= 300:
        parser.error("--timeout-s must be between 1 and 300")
    try:
        code, stdout, stderr, receipt = execute(
            args.entry, args.tool_args, args.timeout_s
        )
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "REJECTED", "detail": str(exc)}, ensure_ascii=False))
        return 64
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    if code:
        print(
            json.dumps(
                {
                    "status": receipt["status"],
                    "tool_receipt_sha256": receipt["receipt_sha256"],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
