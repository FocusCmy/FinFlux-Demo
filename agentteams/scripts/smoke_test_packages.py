from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
PACKAGES = BUILD / "packages"
SOURCE_PACKAGES = ROOT / "packages"

BOUNDED_ROLES = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "data-rights-steward",
    "research-context-analyst",
    "runtime-resilience-auditor",
    "independent-validator",
)
ALL_ROLES = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "downstream-impact-analyst",
    "data-rights-steward",
    "research-context-analyst",
    "runtime-resilience-auditor",
    "independent-validator",
    "result-composer",
)
RESULT_FILE_BY_ROLE = {
    "evidence-investigator": "evidence_result.json",
    "semantic-impact-analyst": "semantic_impact_result.json",
    "data-rights-steward": "rights_review_result.json",
    "research-context-analyst": "research_context_result.json",
    "runtime-resilience-auditor": "runtime_resilience_result.json",
    "independent-validator": "independent_validation.json",
}


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_sha256(value: Any) -> bool:
    token = str(value or "")
    return len(token) == 64 and all(char in "0123456789abcdef" for char in token)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), f"JSON root must be an object: {path}")
    return value


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "package command failed\n"
            f"command={command!r}\n"
            f"stdout={completed.stdout!r}\n"
            f"stderr={completed.stderr!r}"
        )
    return completed.stdout


def package_skills(role: str) -> list[str]:
    source = read_json(SOURCE_PACKAGES / role / "skills.map.json")
    skills = source.get("skills")
    require(isinstance(skills, list) and skills, f"empty skills.map for {role}")
    require(len(skills) == len(set(skills)), f"duplicate Skill in skills.map for {role}")
    return [str(item) for item in skills]


def verify_archive_entries(archive: zipfile.ZipFile, role: str, skills: list[str]) -> None:
    names = archive.namelist()
    require(names, f"empty ZIP: {role}")
    for name in names:
        pure = Path(name.replace("\\", "/"))
        require("\\" not in name, f"non-portable ZIP entry: {name}")
        require(not pure.is_absolute(), f"absolute ZIP entry: {name}")
        require(".." not in pure.parts, f"path traversal ZIP entry: {name}")
    required = {
        "manifest.json",
        "config/AGENTS.md",
        "config/SOUL.md",
        "task_identity.py",
        *(f"skills/{skill}/SKILL.md" for skill in skills),
    }
    if role in BOUNDED_ROLES:
        required.update(
            {
                "bounded_worker_task.py",
                "task_identity.py",
                "context_capsule.py",
                "tool_gateway.py",
                "runtime-skill-manifest.json",
            }
        )
    elif role == "downstream-impact-analyst":
        required.update(
            {"bounded_change_task.py", "change_control.py", "tool_gateway.py"}
        )
    else:
        required.update(
            {
                "result_composer_agent.py",
                "decision_reports.py",
                *(f"skills/{skill}/scripts/run.py" for skill in skills),
            }
        )
    missing = sorted(required - set(names))
    require(not missing, f"{role} ZIP missing files: {missing}")


def verify_agent_manifest(extract_root: Path, role: str) -> dict[str, Any]:
    manifest_path = extract_root / "manifest.json"
    manifest = read_json(manifest_path)
    worker = manifest.get("worker") or {}
    require(worker.get("suggested_name") == role, f"agent manifest role mismatch: {role}")
    require(bool(worker.get("runtime")), f"agent runtime missing: {role}")
    return {
        "path": "manifest.json",
        "sha256": file_sha256(manifest_path),
        "runtime": worker.get("runtime"),
    }


def verify_task_identity_contract(extract_root: Path, role: str) -> dict[str, Any]:
    path = extract_root / "task_identity.py"
    require(path.is_file(), f"task_identity.py missing after package build: {role}")
    spec = importlib.util.spec_from_file_location(
        f"finflux_packaged_task_identity_{role.replace('-', '_')}", path
    )
    require(spec is not None and spec.loader is not None, f"cannot load task identity: {role}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case_id = "FUTURES-IF2608-20260814-PACKAGE-SMOKE"
    run_id = "RUN-LIVE-20260831010101-acde01"
    other_run_id = "RUN-LIVE-20260831010101-acde02"
    identity = module.build_role_task_ids(case_id, run_id, (role,))
    expected_scope = f"task-{case_id}-LIVE-20260831010101-acde01"
    expected_task_id = f"{expected_scope}-{role}"
    require(identity.get("task_scope") == expected_scope, f"Run nonce lost: {role}")
    require(
        (identity.get("task_ids") or {}).get(role) == expected_task_id,
        f"exact role task id mismatch: {role}",
    )
    try:
        module.validate_role_task_ids(
            identity,
            case_id=case_id,
            run_id=other_run_id,
            selected_workers=(role,),
        )
    except module.TaskIdentityError:
        cross_run_rejected = True
    else:
        cross_run_rejected = False
    require(cross_run_rejected, f"same-second cross-Run task identity accepted: {role}")
    return {
        "path": "task_identity.py",
        "sha256": file_sha256(path),
        "task_scope": expected_scope,
        "exact_task_id": expected_task_id,
        "same_second_other_nonce_rejected": cross_run_rejected,
    }


def verify_runtime_manifest(
    extract_root: Path, role: str, expected_skills: list[str]
) -> tuple[dict[str, Any], list[str]]:
    path = extract_root / "runtime-skill-manifest.json"
    signed = read_json(path)
    declared = str(signed.get("manifest_sha256", ""))
    unsigned = {key: value for key, value in signed.items() if key != "manifest_sha256"}
    require(is_sha256(declared), f"runtime manifest digest missing: {role}")
    require(canonical_sha256(unsigned) == declared, f"runtime manifest digest mismatch: {role}")
    require(
        signed.get("protocol") == "FINFLUX_WORKER_SKILL_MANIFEST_V1.0",
        f"runtime manifest protocol mismatch: {role}",
    )
    require(signed.get("role") == role, f"runtime manifest role mismatch: {role}")
    require(signed.get("package_root") == ".", f"runtime package_root must be '.': {role}")
    entry = signed.get("entrypoint") or {}
    entry_path = extract_root / str(entry.get("path", ""))
    require(
        entry_path.resolve().parent == extract_root.resolve(),
        f"runtime entrypoint escaped package: {role}",
    )
    require(
        file_sha256(entry_path) == entry.get("sha256"),
        f"runtime entrypoint digest mismatch: {role}",
    )

    identities: list[str] = []
    discovered_ids: list[str] = []
    for item in signed.get("skills") or []:
        skill_id = str(item.get("skill_id", ""))
        version = str(item.get("version", ""))
        require(skill_id and version, f"invalid runtime Skill identity: {role}")
        instruction = extract_root / str(item.get("instruction_path", ""))
        instruction.resolve().relative_to(extract_root.resolve())
        require(
            file_sha256(instruction) == item.get("instruction_sha256"),
            f"Skill instruction digest mismatch: {role}/{skill_id}",
        )
        require(bool(item.get("callable")), f"Skill callable missing: {role}/{skill_id}")
        require(
            is_sha256(item.get("callable_sha256")),
            f"Skill callable digest missing: {role}/{skill_id}",
        )
        discovered_ids.append(skill_id)
        identities.append(f"{skill_id}@{version}")
    require(discovered_ids == expected_skills, f"runtime Skill set/order mismatch: {role}")
    return (
        {
            "path": "runtime-skill-manifest.json",
            "sha256": file_sha256(path),
            "signed_manifest_sha256": declared,
            "entrypoint_sha256": entry.get("sha256"),
        },
        identities,
    )


def live_payload_b64() -> str:
    payload: dict[str, Any] = {
        "p": "FINFLUX_LIVE_WORKER_PAYLOAD_V0.1",
        "s": "SUB-PACKAGE-SMOKE-20260831",
        "f": "futures_settlement",
        "h": "1" * 64,
        "r": "2" * 64,
        "g": "PASS",
        "i": "IF2608",
        "d": "2026-08-14",
        "c": 4648.4,
        "t": 4652.4,
        "m": "close",
        "x": 300,
        "ps": "3" * 64,
        "pi": 1200.0,
        "pr": "BLOCK",
        "rm": "4" * 64,
        "rb": "5" * 64,
        "rc": 12,
        "cl": "PUBLIC",
        "gb": "Public exchange evidence; deterministic package acceptance only",
        "us": "EVALUATION_ONLY",
        "ew": 600,
        "et": 90,
        "er": 0,
        "ec": True,
    }
    payload["ph"] = canonical_sha256(payload)
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")


def verify_receipts(
    receipts: Any, expected_identities: list[str], manifest_sha256: str
) -> list[dict[str, Any]]:
    require(isinstance(receipts, list) and receipts, "runtime emitted no Skill receipts")
    actual = [f"{item.get('skill_id')}@{item.get('version')}" for item in receipts]
    require(actual == expected_identities, f"runtime receipt Skill set/order mismatch: {actual}")
    summaries: list[dict[str, Any]] = []
    for item in receipts:
        identity = f"{item.get('skill_id')}@{item.get('version')}"
        require(item.get("status") == "SUCCESS", f"Skill did not succeed: {identity}")
        require(
            item.get("discovered_at_runtime") is True,
            f"Skill was not runtime-discovered: {identity}",
        )
        require(
            item.get("manifest_sha256") == manifest_sha256,
            f"receipt manifest mismatch: {identity}",
        )
        require(
            item.get("execution_channel") == "WORKER_DETERMINISTIC_SKILL_RUNTIME",
            f"receipt channel mismatch: {identity}",
        )
        require(item.get("exit_code") == 0, f"Skill exit_code is not zero: {identity}")
        require(
            item.get("provider_tokens") == 0,
            f"zero-model smoke consumed provider tokens: {identity}",
        )
        for field in (
            "digest",
            "input_sha256",
            "output_sha256",
            "entrypoint_sha256",
            "callable_sha256",
            "instruction_sha256",
            "receipt_sha256",
        ):
            require(is_sha256(item.get(field)), f"invalid {field}: {identity}")
        require(
            len(str(item.get("tool_run_id", ""))) == 24,
            f"invalid tool_run_id: {identity}",
        )
        unsigned = {key: value for key, value in item.items() if key != "receipt_sha256"}
        require(
            canonical_sha256(unsigned) == item["receipt_sha256"],
            f"receipt digest mismatch: {identity}",
        )
        summaries.append(
            {
                "skill": identity,
                "status": item["status"],
                "tool_run_id": item["tool_run_id"],
                "input_sha256": item["input_sha256"],
                "output_sha256": item["output_sha256"],
                "receipt_sha256": item["receipt_sha256"],
                "provider_tokens": item["provider_tokens"],
            }
        )
    return summaries


def run_bounded_role(
    extract_root: Path,
    temp_root: Path,
    role: str,
    expected_identities: list[str],
    manifest_sha256: str,
) -> dict[str, Any]:
    timestamp = "20260831000000"
    case_id = "FUTURES-IF2608-20260814-PACKAGE-SMOKE"
    nonce = hashlib.sha256(role.encode("utf-8")).hexdigest()[:6]
    run_id = f"RUN-LIVE-{timestamp}-{nonce}"
    task_id = f"task-{case_id}-LIVE-{timestamp}-{nonce}-{role}"
    task_root = temp_root / "tasks" / role
    task_root.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["FINFLUX_TASK_ROOT"] = str(task_root)
    stdout = run_checked(
        [
            sys.executable,
            str(extract_root / "bounded_worker_task.py"),
            "--role",
            role,
            "--asset",
            "futures",
            "--case-id",
            case_id,
            "--run-id",
            run_id,
            "--task-id",
            task_id,
            "--policy-id",
            "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--scenario",
            "blocked",
            "--live-payload-b64",
            live_payload_b64(),
        ],
        env=environment,
    )
    command_result = json.loads(stdout)
    require(command_result.get("ok") is True, f"bounded Worker CLI did not report ok: {role}")
    result = read_json(task_root / task_id / RESULT_FILE_BY_ROLE[role])
    require(result.get("role") == role, f"bounded Worker result role mismatch: {role}")
    require(result.get("run_id") == run_id, f"bounded Worker result run mismatch: {role}")
    require(result.get("task_id") == task_id, f"bounded Worker result task mismatch: {role}")
    require(result.get("deterministic") is True, f"bounded Worker is not deterministic: {role}")
    require(
        result.get("model_generated_financial_truth") is False,
        f"model truth boundary violated: {role}",
    )
    receipts = verify_receipts(
        result.get("skill_invocations"), expected_identities, manifest_sha256
    )
    return {
        "execution": "bounded_worker_task.py --live-payload-b64",
        "run_id": run_id,
        "task_id": task_id,
        "result_status": result.get("status"),
        "tool_run_id": result.get("tool_run_id"),
        "receipts": receipts,
    }


def run_downstream_role(extract_root: Path, temp_root: Path) -> dict[str, Any]:
    timestamp = "20260831000000"
    case_id = "CHANGE-PACKAGE-SMOKE"
    nonce = hashlib.sha256(b"downstream-impact-analyst").hexdigest()[:6]
    run_id = f"RUN-LIVE-{timestamp}-{nonce}"
    task_id = f"task-{case_id}-LIVE-{timestamp}-{nonce}-downstream-impact-analyst"
    task_root = temp_root / "tasks" / "downstream-impact-analyst"
    task_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "change_bundle_id": "CB-PACKAGE-SMOKE",
        "change_set": {
            "change_id": "CHG-PACKAGE-SMOKE",
            "change_set_sha256": "a" * 64,
            "changed_paths": ["metadata.candidate_mapping"],
        },
        "downstream_tasks": [
            {
                "task_id": "daily-settlement",
                "dependencies": ["metadata.candidate_mapping"],
            },
            {"task_id": "unregistered-consumer", "dependencies": []},
        ],
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    environment = dict(os.environ)
    environment["FINFLUX_TASK_ROOT"] = str(task_root)
    run_checked(
        [
            sys.executable,
            str(extract_root / "bounded_change_task.py"),
            "--case-id",
            case_id,
            "--run-id",
            run_id,
            "--task-id",
            task_id,
            "--policy-id",
            "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--change-payload-b64",
            encoded,
        ],
        env=environment,
    )
    result = read_json(task_root / task_id / "downstream_impact_result.json")
    require(result.get("status") == "SUCCESS", "downstream bounded task failed")
    receipts = result.get("skill_invocations") or []
    require(len(receipts) == 1, "downstream task must emit one Skill invocation")
    receipt = receipts[0]
    require(
        receipt.get("skill_id") == "resolve-downstream-lineage",
        "downstream Skill mismatch",
    )
    require(receipt.get("version") == "1.0.0", "downstream Skill version mismatch")
    require(
        receipt.get("discovered_at_runtime") is True,
        "downstream Skill was not runtime-discovered",
    )
    require(
        receipt.get("provider_tokens") == 0,
        "downstream zero-model smoke consumed tokens",
    )
    for field in ("digest", "input_sha256", "output_sha256"):
        require(is_sha256(receipt.get(field)), f"downstream receipt {field} invalid")
    return {
        "execution": "bounded_change_task.py",
        "run_id": run_id,
        "task_id": task_id,
        "result_status": result.get("status"),
        "recommendation": result.get("recommendation"),
        "tool_run_id": result.get("tool_run_id"),
        "receipts": [receipt],
        "receipt_boundary": "FINFLUX_BOUNDED_CHANGE_WORKER_RESULT_V1.0",
    }


COMPOSER_PROBE = r'''
import json
import sys
from pathlib import Path

package_root = Path(sys.argv[1]).resolve()
artifact_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(package_root))
from result_composer_agent import ResultComposerAgent

run = {
    "run_id": "RUN-LIVE-20260831000000-acde01",
    "case_id": "FUTURES-IF2608-20260814-PACKAGE-SMOKE",
    "submission_id": "SUB-PACKAGE-SMOKE-20260831",
    "state": "AWAITING_HUMAN",
    "created_at": "2026-08-31T00:00:00+00:00",
    "lineage": {},
    "precheck": {
        "candidate_mapping": "close",
        "required_field": "settle",
        "close": 4648.4,
        "settle": 4652.4,
        "impact_cny_per_contract": 1200.0,
        "sha256": "3" * 64,
    },
    "datapass": {
        "protocol": "FINFLUX_DATAPASS_DRAFT_V0.2",
        "machine_recommendation": "BLOCK",
        "draft_sha256": "4" * 64,
        "skill_invocations": [],
    },
    "human_gate": {"state": "AWAITING_HUMAN"},
    "agent_result": {
        "leader_recommendation": "BLOCK",
        "workers_completed": 3,
        "workers_required": 3,
        "worker_artifacts": {},
    },
    "provider_usage": {
        "status": "ZERO_MODEL",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "source": "package-smoke",
    },
}
submission = {
    "file": {"name": "acceptance.csv", "sha256": "1" * 64},
    "metadata": {"declared_purpose": "daily_settlement_pnl"},
    "parsed": {"instrument": "IF2608", "trade_date": "2026-08-14"},
    "rights_gate": {"status": "PASS"},
    "evidence_root_hash": "2" * 64,
}
result = ResultComposerAgent(artifact_root).compose(run, submission, stage="preview")
print(json.dumps(result, ensure_ascii=False))
'''


def run_result_composer(extract_root: Path, temp_root: Path) -> dict[str, Any]:
    stdout = run_checked(
        [
            sys.executable,
            "-c",
            COMPOSER_PROBE,
            str(extract_root),
            str(temp_root / "result-artifacts"),
        ]
    )
    result = json.loads(stdout)
    require(result.get("agent_id") == "result-composer", "Result Composer identity mismatch")
    strategy = result.get("strategy") or {}
    require(strategy.get("model_called") is False, "Result Composer called a model")
    require(strategy.get("provider_tokens") == 0, "Result Composer consumed provider tokens")
    receipts = result.get("skill_invocations") or []
    expected = [f"{skill}@1.0.0" for skill in package_skills("result-composer")]
    actual = [f"{item.get('skill_id')}@{item.get('version')}" for item in receipts]
    require(actual == expected, f"Result Composer Skill set/order mismatch: {actual}")
    for item in receipts:
        identity = f"{item.get('skill_id')}@{item.get('version')}"
        require(item.get("status") == "SUCCESS", f"Result Composer Skill failed: {identity}")
        require(
            item.get("discovered_at_runtime") is True,
            f"Result Composer Skill not discovered: {identity}",
        )
        require(
            is_sha256(item.get("input_sha256")),
            f"Result Composer input hash invalid: {identity}",
        )
        require(
            is_sha256(item.get("output_sha256")),
            f"Result Composer output hash invalid: {identity}",
        )
    verification = result.get("verification") or {}
    require(
        verification.get("status") == "VERIFIED",
        "Result Composer artifact verification failed",
    )
    require(
        is_sha256(verification.get("manifest_sha256")),
        "Result Composer manifest hash invalid",
    )
    return {
        "execution": "ResultComposerAgent.compose(stage=preview)",
        "run_id": result.get("run_id"),
        "result_status": verification.get("status"),
        "strategy": strategy.get("strategy"),
        "provider_tokens": strategy.get("provider_tokens"),
        "artifact_manifest_sha256": verification.get("manifest_sha256"),
        "receipts": receipts,
        "receipt_boundary": "FINFLUX_RESULT_COMPOSER_AGENT_RUN_V1.0",
    }


def main() -> int:
    index = read_json(BUILD / "package-index.json")
    index_rows = {str(item.get("role")): item for item in index.get("packages") or []}
    require(
        tuple(index_rows) == ALL_ROLES,
        "package-index roles/order do not match the frozen eight-Agent set",
    )
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="finflux-package-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        for role in ALL_ROLES:
            row = index_rows[role]
            skills = package_skills(role)
            package_path = PACKAGES / f"{role}.zip"
            package_result: dict[str, Any] = {
                "role": role,
                "package": package_path.name,
                "skills": skills,
                "passed": False,
            }
            try:
                require(package_path.is_file(), f"package missing: {package_path}")
                require(
                    package_path.stat().st_size == row.get("bytes"),
                    f"package bytes mismatch: {role}",
                )
                require(
                    file_sha256(package_path) == row.get("sha256"),
                    f"package-index SHA256 mismatch: {role}",
                )
                extract_root = temp_root / "packages" / role
                with zipfile.ZipFile(package_path) as archive:
                    verify_archive_entries(archive, role, skills)
                    archive.extractall(extract_root)
                require(row.get("skills") == skills, f"package-index Skill map mismatch: {role}")
                package_result["zip_sha256"] = row.get("sha256")
                package_result["agent_manifest"] = verify_agent_manifest(extract_root, role)
                package_result["task_identity"] = verify_task_identity_contract(
                    extract_root, role
                )

                if role in BOUNDED_ROLES:
                    runtime_manifest, identities = verify_runtime_manifest(
                        extract_root, role, skills
                    )
                    package_result["runtime_manifest"] = runtime_manifest
                    package_result["execution"] = run_bounded_role(
                        extract_root,
                        temp_root,
                        role,
                        identities,
                        runtime_manifest["signed_manifest_sha256"],
                    )
                elif role == "downstream-impact-analyst":
                    package_result["runtime_manifest"] = {
                        "boundary": (
                            "bounded_change_task.py owns one deterministic lineage Skill; "
                            "it does not impersonate a financial Worker manifest"
                        )
                    }
                    package_result["execution"] = run_downstream_role(
                        extract_root, temp_root
                    )
                else:
                    package_result["runtime_manifest"] = {
                        "boundary": (
                            "ResultComposerAgent owns its four report Skills and never "
                            "emits a Human decision"
                        )
                    }
                    package_result["execution"] = run_result_composer(
                        extract_root, temp_root
                    )
                package_result["passed"] = True
            except Exception as exc:  # keep evidence for every ZIP before failing
                package_result["error"] = f"{type(exc).__name__}: {exc}"
            results.append(package_result)

    report = {
        "protocol": "FINFLUX_AGENT_PACKAGE_SMOKE_V2.0",
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "truth_boundary": (
            "Package-local deterministic acceptance only. No API/model call, "
            "no Matrix collaboration claim, and no Human authorization claim."
        ),
        "api_or_model_called": False,
        "provider_tokens": 0,
        "packages_passed": sum(bool(item["passed"]) for item in results),
        "packages_total": len(results),
        "passed": all(bool(item["passed"]) for item in results),
        "results": results,
    }
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "package-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
