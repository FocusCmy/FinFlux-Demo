from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


AGENT_DEMO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = AGENT_DEMO_ROOT.parent
APP_ROOT = next(
    (
        candidate
        for candidate in (PROJECT_ROOT / "app", PROJECT_ROOT / "demo")
        if candidate.is_dir()
    ),
    PROJECT_ROOT / "app",
)
BUILD_ROOT = AGENT_DEMO_ROOT / "build"
TARGET_VERSION = "v1.2.2"
TARGET_COMMIT = "849182af8e017168a5a200a87b1062142caf462d"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_declared_project_path(value: str) -> Path:
    """Resolve app-relative evidence in both source and submission layouts."""
    declared = Path(value)
    if declared.parts and declared.parts[0] in {"app", "demo"}:
        return APP_ROOT.joinpath(*declared.parts[1:])
    return PROJECT_ROOT / declared


errors: list[str] = []
checks: list[dict] = []


def check(name: str, condition: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(condition), "detail": detail})
    if not condition:
        errors.append(f"{name}: {detail}")


config = load_json(AGENT_DEMO_ROOT / "config" / "agent_demo.json")
workers = config["team"]["workers"]
check(
    "agentteams_v122_pinned",
    config["runtime"]["required_platform"] == "AgentTeams"
    and config["runtime"]["pinned_version"] == TARGET_VERSION
    and config["runtime"]["pinned_commit"] == TARGET_COMMIT,
    "AgentTeams v1.2.2 tag and commit must be explicit",
)
check(
    "agentteams_v122_api_group",
    config["runtime"]["api_group"] == "agentteams.io/v1beta1",
    config["runtime"]["api_group"],
)
check(
    "seven_specialists_plus_result_composer",
    len(workers) == 8
    and len(set(workers)) == 8
    and "downstream-impact-analyst" in workers
    and "data-rights-steward" in workers
    and "research-context-analyst" in workers
    and "runtime-resilience-auditor" in workers
    and "result-composer" in workers,
    f"workers={workers}",
)
check(
    "human_is_not_agent",
    config["team"]["human"] not in workers,
    "Human approval must remain a separate resource",
)
check(
    "dynamic_code_only_branch",
    any(
        not rule["activate"] and rule["route"] == "CODE_ONLY_PASS"
        for rule in config["dynamic_activation"]
    ),
    "A no-Agent branch is required",
)

vendored_source = PROJECT_ROOT / "vendor" / "AgentTeams-v1.2.2"
notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
check(
    "official_v122_source_not_vendored",
    not vendored_source.exists(),
    "public repository integrates upstream AgentTeams without embedding its Git tree",
)
check(
    "official_v122_provenance_declared",
    TARGET_VERSION in notices and TARGET_COMMIT in notices,
    "version and commit must remain auditable in THIRD_PARTY_NOTICES.md",
)

template = (
    AGENT_DEMO_ROOT / "resources" / "finchange-resources.yaml.template"
).read_text(encoding="utf-8")
deploy_script = (
    AGENT_DEMO_ROOT / "scripts" / "Deploy-AgentTeams.ps1"
).read_text(encoding="utf-8")
runtime_patch_gate_path = AGENT_DEMO_ROOT / "scripts" / "runtime_patch_gate.py"
runtime_patch_gate = (
    runtime_patch_gate_path.read_text(encoding="utf-8")
    if runtime_patch_gate_path.is_file()
    else ""
)
check(
    "v122_crd_kinds",
    all(f"kind: {kind}" in template for kind in ("Manager", "Worker", "Team", "Human")),
    "Manager, independent Worker, Team and Human CRDs must exist",
)
check(
    "v122_api_group_in_template",
    template.count("apiVersion: agentteams.io/v1beta1") == 12
    and "hiclaw.io/" not in template,
    "Expected Manager + 5 Workers + Team + Human on agentteams.io/v1beta1",
)
check(
    "independent_worker_crs",
    template.count("kind: Worker") == 9 and "workerMembers:" in template,
    "Leader, seven specialists and Result Composer must be standalone Worker CRs referenced by Team",
)
check(
    "one_team_leader_reference",
    template.count("role: team_leader") == 1,
    "Team must reference exactly one leader",
)
check(
    "role_model_placeholders",
    all(
        f"__{role}_MODEL_ID__" in template
        for role in ("MANAGER", "LEADER", "EVIDENCE", "ANALYST", "VALIDATOR", "RESULT")
    ),
    "Every Agent role needs an overridable model slot",
)
check(
    "resource_template_has_no_key",
    "api_key" not in template.lower(),
    "Resource template must not contain provider credentials",
)
check(
    "worker_package_uris",
    "file:///tmp/finchange-packages/" not in template
    and "agt apply worker" in deploy_script
    and "--zip \"/tmp/finchange-packages/$Role.zip\"" in deploy_script,
    "Specialist packages must be uploaded through AgentTeams so the controller stores oss:// URIs",
)
check(
    "runtime_patch_gate_is_pid1_bound",
    runtime_patch_gate_path.is_file()
    and 'Path("/proc/1/environ")' in runtime_patch_gate
    and 'Path("/proc/1/cwd")' in runtime_patch_gate
    and "PID1_CWD:{child_name}" in runtime_patch_gate
    and "INSTALL_VERIFIED" in runtime_patch_gate
    and "POST_RESTART_READBACK_VERIFIED" in runtime_patch_gate,
    "Runtime patch install/readback must resolve the actual PID1 workspace and emit explicit receipts",
)
check(
    "deployment_executes_hash_bound_runtime_patch_gate",
    "runtime_patch_gate.py" in deploy_script
    and "--gate-sha256 $RuntimePatchGateHash" in deploy_script
    and "install teamharness" in deploy_script
    and "install manager" in deploy_script
    and "readback teamharness" in deploy_script
    and "readback manager" in deploy_script,
    "Deployment must install and post-restart read back both runtime patches through the hash-bound gate",
)

execution_policy = load_json(AGENT_DEMO_ROOT / "config" / "execution_policy.json")
check(
    "bounded_execution_policy",
    execution_policy.get("mode") == "FAIL_CLOSED"
    and execution_policy.get("policy_id") == "FINFLUX-BOUNDED-EXECUTION-V0.1"
    and execution_policy.get("run_limits", {}).get("max_active_runs") == 1
    and execution_policy.get("role_limits", {}).get("manager", {}).get("max_messages") == 1,
    "Bounded execution must fail closed with one active Run and a one-message Manager authorization ceiling",
)
case_envelope_schema = load_json(
    AGENT_DEMO_ROOT / "protocols" / "case-envelope.schema.json"
)
check(
    "bounded_case_envelope_schema",
    case_envelope_schema.get("additionalProperties") is False
    and "execution_policy_id" in case_envelope_schema.get("required", [])
    and "evidence_handles" in case_envelope_schema.get("required", [])
    and "precheck_attestation" in case_envelope_schema.get("required", [])
    and "dispatch_idempotency_key" in case_envelope_schema.get("required", [])
    and case_envelope_schema.get("properties", {})
    .get("precheck_attestation", {})
    .get("additionalProperties")
    is False,
    "CaseEnvelope must reject unknown fields and bind every run to evidence handles, a deterministic precheck, an idempotency key, and a policy",
)

rendered_for_schema_check = template
for role in ("MANAGER", "LEADER", "EVIDENCE", "ANALYST", "VALIDATOR", "RESULT"):
    rendered_for_schema_check = rendered_for_schema_check.replace(
        f"__{role}_MODEL_ID__", "'offline-schema-check-model'"
    )
    rendered_for_schema_check = rendered_for_schema_check.replace(
        f"__{role}_MODEL_PROVIDER_FIELD__", ""
    )
try:
    rendered_documents = list(yaml.safe_load_all(rendered_for_schema_check))
except yaml.YAMLError as exc:
    rendered_documents = []
    check("rendered_yaml_parses", False, str(exc))
else:
    check("rendered_yaml_parses", len(rendered_documents) == 12, f"documents={len(rendered_documents)}")
    rendered_kinds = [document.get("kind") for document in rendered_documents]
    check(
        "rendered_resource_order",
        rendered_kinds == ["Manager", *("Worker" for _ in range(9)), "Team", "Human"],
        str(rendered_kinds),
    )
    worker_names = {
        document["metadata"]["name"]
        for document in rendered_documents
        if document.get("kind") == "Worker"
    }
    team_document = next(document for document in rendered_documents if document.get("kind") == "Team")
    referenced_workers = {
        member["name"] for member in team_document["spec"]["workerMembers"]
    }
    check(
        "team_references_existing_workers",
        worker_names == referenced_workers,
        f"workers={sorted(worker_names)}, references={sorted(referenced_workers)}",
    )

example_env = (AGENT_DEMO_ROOT / ".env.example").read_text(encoding="utf-8")
for secret_name in (
    "AGENTTEAMS_LLM_PROVIDER",
    "AGENTTEAMS_DEFAULT_MODEL",
    "AGENTTEAMS_OPENAI_BASE_URL",
    "AGENTTEAMS_LLM_API_KEY",
    "AGENTTEAMS_ADMIN_PASSWORD",
):
    match = re.search(rf"^{re.escape(secret_name)}=(.*)$", example_env, re.MULTILINE)
    check(
        f"blank_{secret_name.lower()}",
        bool(match) and not match.group(1).strip(),
        f"{secret_name} must be blank in .env.example",
    )
check(
    "env_version_v122",
    "AGENTTEAMS_VERSION=v1.2.2" in example_env and "HICLAW_" not in example_env,
    "Environment contract must use v1.2.2 AgentTeams names only",
)
check(
    "windows_qwenpaw_runtime_selectors",
    "AGENTTEAMS_MANAGER_RUNTIME=copaw" in example_env
    and "AGENTTEAMS_DEFAULT_WORKER_RUNTIME=qwenpaw" in example_env
    and "runtime: copaw" in template
    and template.count("runtime: qwenpaw") == 9,
    "Manager uses the Windows installer compatibility selector; nine Worker CRs use qwenpaw",
)

case_ids: set[str] = set()
for case_path in sorted((AGENT_DEMO_ROOT / "cases").glob("*_case.json")):
    case = load_json(case_path)
    case_id = case["case_id"]
    check(f"unique_case_{case_path.stem}", case_id not in case_ids, case_id)
    case_ids.add(case_id)
    check(
        f"full_team_{case_path.stem}",
        case["expected_route"] == "FULL_TEAM_REVIEW" and case["human_gate"] is True,
        "Cross-asset demo cases require full review and Human gate",
    )
    for evidence in case["evidence"]:
        evidence_path = resolve_declared_project_path(evidence["path"])
        source_path = resolve_declared_project_path(evidence["sha256_source"])
        check(
            f"evidence_exists_{evidence['evidence_id']}",
            evidence_path.is_file(),
            str(evidence_path),
        )
        check(
            f"hash_source_exists_{evidence['evidence_id']}",
            source_path.is_file(),
            str(source_path),
        )

for role in workers:
    package_root = AGENT_DEMO_ROOT / "packages" / role
    manifest = load_json(package_root / "manifest.json")
    skill_map = load_json(package_root / "skills.map.json")
    check(
        f"package_config_{role}",
        all(
            (package_root / path).is_file()
            for path in (
                "manifest.json",
                "config/SOUL.md",
                "config/AGENTS.md",
                "skills.map.json",
            )
        ),
        str(package_root),
    )
    check(
        f"package_v122_qwenpaw_{role}",
        manifest["source"]["created_for"] == "AgentTeams v1.2.2"
        and manifest["worker"]["runtime"] == "qwenpaw",
        json.dumps(manifest, ensure_ascii=False),
    )
    for skill in skill_map["skills"]:
        skill_path = APP_ROOT / "agentteams-skills" / skill / "SKILL.md"
        check(f"skill_{role}_{skill}", skill_path.is_file(), str(skill_path))

deterministic_results = {}
public_manifest_path = APP_ROOT / "data" / "real_50x3_v1" / "manifest.json"
public_manifest = load_json(public_manifest_path)
unsigned_manifest = {
    key: value for key, value in public_manifest.items() if key != "manifest_sha256"
}
computed_manifest_sha256 = hashlib.sha256(
    json.dumps(
        unsigned_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
counts = {"futures": 0, "equity": 0, "fund": 0}
for record in public_manifest.get("records") or []:
    asset_class = str(record.get("asset_class") or "")
    if asset_class in counts:
        counts[asset_class] += 1
check(
    "public_manifest_hash",
    public_manifest.get("manifest_sha256") == computed_manifest_sha256,
    computed_manifest_sha256,
)
check(
    "public_manifest_50x3",
    counts == {"futures": 50, "equity": 50, "fund": 50},
    json.dumps(counts, ensure_ascii=False),
)
check(
    "public_manifest_rights_fail_closed",
    all(
        source.get("rights_state") == "REVIEW_REQUIRED"
        and str(source.get("source_url") or "").startswith(("http://", "https://"))
        and len(str(source.get("artifact_sha256") or "")) == 64
        for source in public_manifest.get("sources") or []
    ),
    "raw source snapshots are referenced by URL/SHA256 and are not redistributed",
)
deterministic_results["public_manifest"] = {
    "manifest_sha256": computed_manifest_sha256,
    "counts_by_asset": counts,
    "source_artifacts_bundled": False,
}

packages = []
for package_path in sorted((BUILD_ROOT / "packages").glob("*.zip")):
    with zipfile.ZipFile(package_path) as archive:
        entry_names = archive.namelist()
        task_identity_source = (
            archive.read("task_identity.py").decode("utf-8")
            if "task_identity.py" in entry_names
            else ""
        )
    portable = not any("\\" in name for name in entry_names)
    prompt_entries_present = {
        "config/AGENTS.md",
        "config/SOUL.md",
    }.issubset(entry_names)
    check(
        f"portable_zip_{package_path.stem}",
        portable and prompt_entries_present,
        "ZIP entries must use POSIX separators and include config/AGENTS.md plus config/SOUL.md",
    )
    gateway_required = package_path.stem != "result-composer"
    gateway_present = "tool_gateway.py" in entry_names
    check(
        f"allowlist_tool_gateway_{package_path.stem}",
        gateway_present if gateway_required else not gateway_present,
        "Financial Worker ZIPs must contain the allowlist Tool Gateway; the deterministic Result Composer must not claim that execution path.",
    )
    exact_task_identity = (
        "task_identity.py" in entry_names
        and "match.group('nonce')" in task_identity_source
        and 'task_id = f"{scope}-{role}"' in task_identity_source
        and "transported role task identities do not match derivation" in task_identity_source
    )
    check(
        f"exact_task_identity_{package_path.stem}",
        exact_task_identity,
        "Every built Worker ZIP must preserve nonce-bound exact role task identity validation",
    )
    packages.append(
        {
            "file": package_path.name,
            "bytes": package_path.stat().st_size,
            "sha256": sha256(package_path),
            "portable_entries": portable,
            "prompt_entries_present": prompt_entries_present,
            "exact_task_identity_present": exact_task_identity,
        }
    )
check("eight_packages_built", len(packages) == 8, f"package_count={len(packages)}")

status = (
    "V122_BOUNDED_ASSETS_READY_FOR_RUNTIME_APPLY"
    if not errors
    else "V122_OFFLINE_VALIDATION_FAILED"
)
report = {
    "validated_at_utc": datetime.now(timezone.utc).isoformat(),
    "status": status,
    "target_version": TARGET_VERSION,
    "target_commit": TARGET_COMMIT,
    "api_or_model_called": False,
    "runtime_mutated_by_validation": False,
    "runtime_state_checked": False,
    "real_trace_checked": False,
    "checks_passed": sum(item["passed"] for item in checks),
    "checks_total": len(checks),
    "errors": errors,
    "packages": packages,
    "deterministic_results": deterministic_results,
    "next_gate": "Execute selected same-evidence single-Agent and AgentTeams baselines within an approved token budget; add Worker-process interruption and durable-recovery evidence.",
}
BUILD_ROOT.mkdir(parents=True, exist_ok=True)
(BUILD_ROOT / "readiness.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
