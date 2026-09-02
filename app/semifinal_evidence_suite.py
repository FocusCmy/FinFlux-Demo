from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol_v02 import (
    canonical_sha256,
    validate_case_envelope,
    validate_datapass,
)
from v02_live_acceptance import verify_audit_zip


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DETERMINISTIC_MANIFEST = (
    PROJECT_ROOT
    / "agentteams"
    / "evidence"
    / "four-profile-zero-model-v0.2"
    / "acceptance_manifest.json"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "agentteams" / "evidence" / "semifinal-suite-v1"
)

SUITE_PROTOCOL = "FINFLUX_SEMIFINAL_EVIDENCE_SUITE_V1.0"
DETERMINISTIC_MANIFEST_PROTOCOL = (
    "FINFLUX_FOUR_PROFILE_ACCEPTANCE_MANIFEST_V0.2"
)
DETERMINISTIC_RESULT_PROTOCOL = "FINFLUX_ZERO_MODEL_PROFILE_ACCEPTANCE_V0.2"
LIVE_RECEIPT_PROTOCOL = "FINFLUX_FUTURES_V02_FINAL_ACCEPTANCE_V1.0"
LIVE_RUN_PROTOCOL = "FINFLUX_LIVE_RUN_V0.2"

EXPECTED_DETERMINISTIC_OUTCOMES = {
    "futures_settlement": "PASS",
    "equity_corporate_action": "BLOCK",
    "option_contract_identity": "BLOCK",
    "research_material_rights": "BLOCK",
}
FINAL_HUMAN_DISPOSITIONS = {
    "APPROVED": "HUMAN_APPROVED",
    "REJECTED": "HUMAN_BLOCKED",
    "RETURNED": "HUMAN_RETURNED",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SemifinalSuiteFailure(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemifinalSuiteFailure(f"{label} is unavailable: {exc}") from exc
    if not isinstance(payload, dict):
        raise SemifinalSuiteFailure(f"{label} must be a JSON object")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return resolved.name


def _safe_child(root: Path, relative: str, label: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise SemifinalSuiteFailure(f"{label} escapes its evidence directory") from exc
    if not path.is_file():
        raise SemifinalSuiteFailure(f"{label} is missing: {relative}")
    return path


def _require_self_hash(payload: dict[str, Any], field: str, label: str) -> None:
    observed = str(payload.get(field) or "")
    unsigned = {key: value for key, value in payload.items() if key != field}
    if not HEX64.fullmatch(observed) or observed != canonical_sha256(unsigned):
        raise SemifinalSuiteFailure(f"{label} self hash is invalid")


def _project_skill_invocations(invocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for item in invocations:
        projected.append(
            {
                "skill_id": item.get("skill_id"),
                "version": item.get("version"),
                "worker_id": item.get("worker_id"),
                "input_sha256": item.get("input_sha256"),
                "output_sha256": item.get("output_sha256"),
                "status": item.get("status"),
            }
        )
    return projected


def _verify_deterministic_manifest(
    manifest_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path, "deterministic acceptance manifest")
    if manifest.get("protocol") != DETERMINISTIC_MANIFEST_PROTOCOL:
        raise SemifinalSuiteFailure("deterministic manifest protocol is not V0.2")
    if manifest.get("status") != "PASS":
        raise SemifinalSuiteFailure("deterministic acceptance did not pass")
    if manifest.get("execution_mode") != "NO_MODEL_CALL":
        raise SemifinalSuiteFailure("deterministic acceptance execution mode is ambiguous")
    if manifest.get("provider_tokens") != 0 or manifest.get("model_calls") != 0:
        raise SemifinalSuiteFailure("deterministic acceptance contains provider usage")
    if manifest.get("case_count") != 4 or len(manifest.get("cases") or []) != 4:
        raise SemifinalSuiteFailure("deterministic acceptance must contain four cases")
    _require_self_hash(manifest, "manifest_sha256", "deterministic manifest")

    manifest_root = manifest_path.parent
    summaries = manifest.get("cases") or []
    observed_profiles = {str(item.get("profile_id") or "") for item in summaries}
    if observed_profiles != set(EXPECTED_DETERMINISTIC_OUTCOMES):
        raise SemifinalSuiteFailure("deterministic profile set is incomplete or duplicated")

    projected_cases: list[dict[str, Any]] = []
    for summary in summaries:
        profile_id = str(summary.get("profile_id") or "")
        expected_recommendation = EXPECTED_DETERMINISTIC_OUTCOMES[profile_id]
        if summary.get("status") != "PASS" or summary.get("verification_status") != "VERIFIED":
            raise SemifinalSuiteFailure(
                f"{profile_id} deterministic structural verification did not pass"
            )
        if summary.get("machine_recommendation") != expected_recommendation:
            raise SemifinalSuiteFailure(
                f"{profile_id} deterministic business outcome is unexpected"
            )
        if summary.get("provider_tokens") != 0 or summary.get("model_calls") != 0:
            raise SemifinalSuiteFailure(
                f"{profile_id} deterministic case contains provider usage"
            )

        json_descriptor = summary.get("json") or {}
        markdown_descriptor = summary.get("markdown") or {}
        json_path = _safe_child(
            manifest_root,
            str(json_descriptor.get("path") or ""),
            f"{profile_id} JSON artifact",
        )
        markdown_path = _safe_child(
            manifest_root,
            str(markdown_descriptor.get("path") or ""),
            f"{profile_id} Markdown artifact",
        )
        if _file_sha256(json_path) != json_descriptor.get("sha256"):
            raise SemifinalSuiteFailure(f"{profile_id} JSON artifact hash mismatch")
        if _file_sha256(markdown_path) != markdown_descriptor.get("sha256"):
            raise SemifinalSuiteFailure(f"{profile_id} Markdown artifact hash mismatch")
        result = _load_json(json_path, f"{profile_id} deterministic result")
        if result.get("protocol") != DETERMINISTIC_RESULT_PROTOCOL:
            raise SemifinalSuiteFailure(f"{profile_id} result protocol is invalid")
        if result.get("acceptance_status") != "PASS" or result.get(
            "verification_status"
        ) != "VERIFIED":
            raise SemifinalSuiteFailure(f"{profile_id} result verification is incomplete")
        expected_disposition = f"MACHINE_{expected_recommendation}_DRAFT"
        if result.get("business_disposition") != expected_disposition:
            raise SemifinalSuiteFailure(f"{profile_id} business disposition is ambiguous")
        if result.get("run_id") != summary.get("run_id") or result.get(
            "case_id"
        ) != summary.get("case_id"):
            raise SemifinalSuiteFailure(f"{profile_id} result identity mismatch")
        if result.get("provider_tokens") != 0 or result.get("model_calls") != 0:
            raise SemifinalSuiteFailure(f"{profile_id} result contains model usage")
        truth = result.get("truth_boundary") or {}
        if (
            truth.get("financial_source_records_real") is not True
            or truth.get("synthetic_financial_records") != 0
            or truth.get("model_called") is not False
            or truth.get("human_approval_claimed") is not False
            or truth.get("candidate_configuration_truth")
            != "EXPLICIT_COUNTERFACTUAL_CONTROL"
        ):
            raise SemifinalSuiteFailure(f"{profile_id} truth boundary is incomplete")

        envelope = result.get("case_envelope") or {}
        datapass = result.get("datapass_draft") or {}
        validate_case_envelope(envelope)
        validate_datapass(datapass, envelope=envelope)
        if datapass.get("machine_recommendation") != expected_recommendation:
            raise SemifinalSuiteFailure(f"{profile_id} DataPass recommendation mismatch")
        route = result.get("manager_route") or {}
        if route.get("route") != summary.get("route") or route.get(
            "machine_recommendation"
        ) != expected_recommendation:
            raise SemifinalSuiteFailure(f"{profile_id} Manager route mismatch")
        workers = result.get("worker_artifacts") or {}
        invocations = (datapass.get("skills") or {}).get("invocations") or []
        if len(workers) != summary.get("worker_count") or len(
            invocations
        ) != summary.get("skill_count"):
            raise SemifinalSuiteFailure(f"{profile_id} Worker or Skill count mismatch")

        projected_cases.append(
            {
                "execution_class": "DETERMINISTIC_ZERO_MODEL",
                "profile_id": profile_id,
                "case_id": result["case_id"],
                "run_id": result["run_id"],
                "verification_status": "VERIFIED",
                "business_disposition": expected_disposition,
                "machine_recommendation": expected_recommendation,
                "human_state": datapass["human_gate"]["state"],
                "manager_route": route.get("route"),
                "evidence": {
                    "path": result["evidence_summary"]["primary_path"],
                    "sha256": result["evidence_summary"]["primary_sha256"],
                    "status": result["evidence_summary"]["status"],
                },
                "workers": [
                    {
                        "worker_id": worker_id,
                        "artifact_sha256": artifact.get("artifact_sha256"),
                    }
                    for worker_id, artifact in sorted(workers.items())
                ],
                "skills": _project_skill_invocations(invocations),
                "datapass_sha256": datapass.get("datapass_sha256"),
                "provider_usage": {
                    "status": "NO_MODEL_CALL",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "call_count": 0,
                    "source": "DETERMINISTIC_ZERO_MODEL_PROCESS",
                },
                "artifacts": {
                    "json_sha256": json_descriptor.get("sha256"),
                    "markdown_sha256": markdown_descriptor.get("sha256"),
                },
                "truth_boundary": (
                    "真实来源记录未改写；候选映射或权属使用方式是显式反事实控制；"
                    "本地确定性Skill验收不等于AgentTeams模型Worker运行，也不构成Human批准。"
                ),
            }
        )

    return (
        {
            "path": _portable_path(manifest_path),
            "file_sha256": _file_sha256(manifest_path),
            "manifest_sha256": manifest["manifest_sha256"],
            "status": "PASS",
            "execution_mode": "NO_MODEL_CALL",
            "provider_tokens": 0,
            "model_calls": 0,
        },
        projected_cases,
    )


def _locate_audit_zip(receipt_path: Path, declared_path: str) -> Path:
    declared = Path(declared_path)
    candidates = [
        declared if declared.is_absolute() else (receipt_path.parent / declared),
        receipt_path.parent / declared.name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SemifinalSuiteFailure("strict V0.2 audit ZIP is missing")


def _verify_live_receipt(receipt_path: Path | None) -> dict[str, Any]:
    if receipt_path is None:
        raise SemifinalSuiteFailure("STRICT_V02_LIVE_ACCEPTANCE_RECEIPT_REQUIRED")
    receipt_path = receipt_path.resolve()
    receipt = _load_json(receipt_path, "strict V0.2 live acceptance receipt")
    if receipt.get("protocol") != LIVE_RECEIPT_PROTOCOL:
        raise SemifinalSuiteFailure("live receipt is not the strict futures V0.2 protocol")
    if receipt.get("status") != "PASS" or (receipt.get("run_validation") or {}).get(
        "status"
    ) != "PASS":
        raise SemifinalSuiteFailure("strict V0.2 live run validation did not pass")
    _require_self_hash(receipt, "receipt_sha256", "strict V0.2 live receipt")
    run_id = str(receipt.get("run_id") or "")
    if not run_id:
        raise SemifinalSuiteFailure("strict V0.2 live receipt has no run_id")
    audit = receipt.get("audit_zip") or {}
    if audit.get("status") != "PASS":
        raise SemifinalSuiteFailure("strict V0.2 audit ZIP was not accepted")
    zip_path = _locate_audit_zip(receipt_path, str(audit.get("path") or ""))
    zip_bytes = zip_path.read_bytes()
    if _file_sha256(zip_path) != audit.get("sha256"):
        raise SemifinalSuiteFailure("strict V0.2 audit ZIP hash mismatch")
    zip_verification = verify_audit_zip(zip_bytes, run_id)
    if zip_verification.get("status") != "PASS":
        raise SemifinalSuiteFailure(
            "strict V0.2 audit ZIP failed re-verification: "
            f"{zip_verification.get('failures')}"
        )
    with zipfile.ZipFile(zip_path) as archive:
        run = json.loads(archive.read("run.json").decode("utf-8-sig"))
    if run.get("protocol") != LIVE_RUN_PROTOCOL:
        raise SemifinalSuiteFailure("archived live run is not FINFLUX_LIVE_RUN_V0.2")
    if run.get("run_id") != run_id or run.get("agentteams_run_id") != run_id:
        raise SemifinalSuiteFailure("archived live AgentTeams run identity mismatch")
    envelope = run.get("case_envelope") or {}
    if (envelope.get("profile") or {}).get("profile_id") != "futures_settlement":
        raise SemifinalSuiteFailure("strict live case is not the futures profile")
    human = run.get("human_gate") or {}
    human_state = str(human.get("state") or "")
    if human_state not in FINAL_HUMAN_DISPOSITIONS:
        raise SemifinalSuiteFailure("strict live run has no final authenticated Human state")
    datapass = run.get("datapass") or {}
    workers = ((run.get("agent_result") or {}).get("worker_artifacts") or {})
    invocations = (datapass.get("skills") or {}).get("invocations") or []
    if len(workers) != 3 or len(invocations) != 5:
        raise SemifinalSuiteFailure("strict live run does not contain 3 Workers and 5 Skills")
    provider = run.get("provider_usage") or {}
    if (
        provider.get("status") != "PROVIDER_REPORTED"
        or int(provider.get("total_tokens") or 0) <= 0
        or int(provider.get("call_count") or 0) <= 0
        or provider.get("source") in {None, "", "MATRIX_MESSAGE_CHARACTER_PROXY"}
    ):
        raise SemifinalSuiteFailure("strict live provider Token attribution is unavailable")
    manager_route = run.get("root_route_decision") or {}
    dispatch = run.get("manager_dispatch_receipt") or {}
    leader_event_id = str(
        (run.get("agent_result") or {}).get("leader_datapass_event_id") or ""
    )
    if manager_route.get("route") != "FULL_TEAM_REVIEW" or not leader_event_id:
        raise SemifinalSuiteFailure("strict live Manager or Case Lead evidence is incomplete")

    return {
        "execution_class": "LIVE_AGENTTEAMS_MODEL_RUN",
        "profile_id": "futures_settlement",
        "case_id": run.get("case_id"),
        "run_id": run_id,
        "verification_status": "VERIFIED_SIGNED",
        "business_disposition": FINAL_HUMAN_DISPOSITIONS[human_state],
        "machine_recommendation": datapass.get("machine_recommendation"),
        "human_state": human_state,
        "human_event_id": ((run.get("agent_result") or {}).get("human_decision") or {}).get(
            "event_id"
        ),
        "manager_route": manager_route.get("route"),
        "manager_decision_sha256": manager_route.get("decision_sha256"),
        "manager_dispatch_receipt_sha256": dispatch.get("receipt_sha256"),
        "leader_datapass_event_id": leader_event_id,
        "evidence": {
            "sha256": [
                item.get("content_sha256")
                for item in envelope.get("evidence_handles") or []
            ],
            "status": (datapass.get("evidence_assessment") or {}).get("status"),
        },
        "workers": [
            {
                "worker_id": worker_id,
                "task_id": artifact.get("task_id"),
                "artifact_sha256": artifact.get("artifact_sha256"),
            }
            for worker_id, artifact in sorted(workers.items())
        ],
        "skills": _project_skill_invocations(invocations),
        "datapass_sha256": datapass.get("datapass_sha256"),
        "provider_usage": {
            "status": provider.get("status"),
            "prompt_tokens": provider.get("prompt_tokens"),
            "completion_tokens": provider.get("completion_tokens"),
            "total_tokens": provider.get("total_tokens"),
            "call_count": provider.get("call_count"),
            "source": provider.get("source"),
            "attribution_status": provider.get("attribution_status"),
        },
        "artifacts": {
            "acceptance_receipt": _portable_path(receipt_path),
            "acceptance_receipt_sha256": receipt.get("receipt_sha256"),
            "audit_zip": _portable_path(zip_path),
            "audit_zip_sha256": audit.get("sha256"),
            "audit_manifest_sha256": zip_verification.get("manifest_sha256"),
        },
        "truth_boundary": (
            "该条是严格V0.2真实AgentTeams模型Run；Token来自模型网关逐调用账本，"
            "Human状态来自同Run认证事件；业务处置与验收状态分开表达。"
        ),
    }


def build_semifinal_evidence_suite(
    *,
    deterministic_manifest_path: Path = DEFAULT_DETERMINISTIC_MANIFEST,
    live_receipt_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at_utc or _utc_now()
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemifinalSuiteFailure("generated_at_utc must be ISO-8601") from exc

    failures: list[dict[str, str]] = []
    deterministic_source: dict[str, Any] | None = None
    deterministic_cases: list[dict[str, Any]] = []
    live_case: dict[str, Any] | None = None
    try:
        deterministic_source, deterministic_cases = _verify_deterministic_manifest(
            deterministic_manifest_path
        )
    except Exception as exc:
        failures.append(
            {
                "component": "DETERMINISTIC_FOUR_PROFILE",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
    try:
        live_case = _verify_live_receipt(live_receipt_path)
    except Exception as exc:
        failures.append(
            {
                "component": "LIVE_AGENTTEAMS_MODEL_RUN",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )

    cases = ([live_case] if live_case is not None else []) + deterministic_cases
    passed = not failures and live_case is not None and len(deterministic_cases) == 4
    manifest_core = {
        "protocol": SUITE_PROTOCOL,
        "schema_version": "1.0.0",
        "generated_at_utc": generated_at,
        "status": "PASS" if passed else "FAIL_CLOSED",
        "counts": {
            "expected_total": 5,
            "verified_total": len(cases),
            "expected_live_agentteams_model_runs": 1,
            "verified_live_agentteams_model_runs": 1 if live_case is not None else 0,
            "expected_deterministic_zero_model_cases": 4,
            "verified_deterministic_zero_model_cases": len(deterministic_cases),
        },
        "sources": {
            "deterministic_manifest": deterministic_source,
            "strict_live_receipt": (
                live_case.get("artifacts", {}).get("acceptance_receipt")
                if live_case is not None
                else None
            ),
        },
        "cases": cases,
        "failures": failures,
        "truth_boundary": {
            "live_and_deterministic_evidence_are_distinct": True,
            "deterministic_cases_claim_agentteams_model_run": False,
            "deterministic_cases_claim_human_approval": False,
            "suite_pass_requires_strict_v02_live_receipt": True,
            "business_disposition_separate_from_verification_status": True,
            "statement": (
                "只有严格V0.2签署Run与四类零模型结构验收同时通过，套件才PASS；"
                "缺少Live回执时即使四类确定性验收完整也必须FAIL_CLOSED。"
            ),
        },
    }
    manifest = {
        **manifest_core,
        "suite_sha256": canonical_sha256(manifest_core),
    }
    output_path = output_dir.resolve() / "suite_manifest.json"
    _atomic_json(output_path, manifest)
    if not passed:
        raise SemifinalSuiteFailure(
            "semifinal evidence suite failed closed: "
            f"{json.dumps(failures, ensure_ascii=False)}"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compose one strict V0.2 Live AgentTeams Run and four deterministic "
            "real-evidence cases into a fail-closed semifinal evidence suite."
        )
    )
    parser.add_argument(
        "--deterministic-manifest",
        type=Path,
        default=DEFAULT_DETERMINISTIC_MANIFEST,
    )
    parser.add_argument("--live-receipt", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()
    try:
        manifest = build_semifinal_evidence_suite(
            deterministic_manifest_path=args.deterministic_manifest,
            live_receipt_path=args.live_receipt,
            output_dir=args.output_dir,
            generated_at_utc=args.generated_at,
        )
    except SemifinalSuiteFailure as exc:
        print(str(exc))
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
