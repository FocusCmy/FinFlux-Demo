from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentteams_runtime.config import CONTEXT_ROOT
from agentteams_runtime.runtime import publish_context
from prompt_budget_readiness import WORKER_TOOL_GATEWAY_ENTRY
from context_capsule import build_run_context_capsule, canonical_sha256


PROTOCOL = "FINFLUX_DEPLOYED_GATEWAY_ZERO_MODEL_PROBE_V1"
WORKERS = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "independent-validator",
)
RECIPE_ID = "FINFLUX_SIGNED_MEMORY_GUARDED_V1"
POLICY_ID = "FINFLUX-BOUNDED-EXECUTION-V0.1"


def _load_source_payload(index_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    handles = dict(index.get("role_slice_handles") or {})
    if set(handles) != set(WORKERS):
        raise ValueError("source capsule must contain the three futures Worker slices")
    payload: dict[str, Any] = {}
    for role in WORKERS:
        digest = str((handles[role] or {}).get("slice_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid source slice digest: {role}")
        slice_path = index_path.parent / f"{digest}.json"
        source_slice = json.loads(slice_path.read_text(encoding="utf-8-sig"))
        if source_slice.get("role") != role:
            raise ValueError(f"source slice role mismatch: {role}")
        for key, value in dict(source_slice.get("payload") or {}).items():
            if key == "ph":
                continue
            if key in payload and payload[key] != value:
                raise ValueError(f"source slice payload conflict: {key}")
            payload[key] = value
    payload["ph"] = canonical_sha256(payload)
    return payload, index


def _docker_worker(
    *, role: str, case_id: str, run_id: str, task_id: str, slice_ref: str
) -> dict[str, Any]:
    container = f"agentteams-worker-{role}"
    workspace = f"/root/agentteams-fs/agents/{role}/.qwenpaw/workspaces/default"
    command = [
        "docker",
        "exec",
        "-w",
        workspace,
        container,
        "python3",
        WORKER_TOOL_GATEWAY_ENTRY,
        "--entry",
        "signed-worker",
        "--timeout-s",
        "60",
        "--",
        "--role",
        role,
        "--context-capsule-ref",
        slice_ref,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    task_dir = (
        "/root/agentteams-fs/teams/finchange-cross-asset-review/"
        f"shared/tasks/{task_id}"
    )
    receipt_result = subprocess.run(
        ["docker", "exec", container, "cat", f"{task_dir}/tool_execution_receipt.json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    receipt = (
        json.loads(receipt_result.stdout)
        if receipt_result.returncode == 0 and receipt_result.stdout.strip()
        else {}
    )
    return {
        "role": role,
        "container": container,
        "task_id": task_id,
        "return_code": completed.returncode,
        "stdout_sha256": canonical_sha256(completed.stdout),
        "stderr_sha256": canonical_sha256(completed.stderr),
        "receipt": receipt,
        "passed": (
            completed.returncode == 0
            and receipt.get("status") == "SUCCEEDED"
            and receipt.get("context_capsule_sha256") == slice_ref
            and receipt.get("execution_recipe_id") == RECIPE_ID
            and receipt.get("execution_recipe_source")
            == "SIGNED_ROLE_CONTEXT_SLICE"
            and receipt.get("provider_tokens") == 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the deployed Worker gateway and deterministic Skills without a model."
    )
    parser.add_argument("--source-capsule-index", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    source_index = args.source_capsule_index.resolve()
    payload, index = _load_source_payload(source_index)
    case_id = str((index.get("identity") or {}).get("case_id") or "")
    if not case_id:
        raise ValueError("source capsule case identity is missing")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    run_id = f"RUN-ZERO-GATEWAY-{stamp}"
    _, handle = build_run_context_capsule(
        case_id=case_id,
        run_id=run_id,
        payload=payload,
        selected_workers=WORKERS,
        execution_policy_id=POLICY_ID,
        root_route_decision_handle=dict(index.get("root_route_decision_handle") or {}),
        execution_recipe_id=RECIPE_ID,
        local_root=CONTEXT_ROOT,
    )
    publish_receipt = publish_context(handle, WORKERS)
    role_handles = dict(handle.get("role_slice_handles") or {})
    results = []
    for role in WORKERS:
        task_id = f"task-{case_id}-ZERO-{stamp}-{role}"
        results.append(
            _docker_worker(
                role=role,
                case_id=case_id,
                run_id=run_id,
                task_id=task_id,
                slice_ref=str(role_handles[role]["slice_sha256"]),
            )
        )
    body = {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "case_id": case_id,
        "source_capsule_sha256": index.get("capsule_sha256"),
        "new_capsule_sha256": handle.get("capsule_sha256"),
        "execution_recipe_id": RECIPE_ID,
        "context_publish_receipt": publish_receipt,
        "worker_results": results,
        "model_calls": 0,
        "provider_tokens": 0,
        "status": "PASS" if all(item["passed"] for item in results) else "FAIL",
        "truth_boundary": (
            "This probe verifies only deployed deterministic Worker execution. "
            "It is not an AgentTeams model Run and must not be presented as one."
        ),
    }
    body["receipt_sha256"] = canonical_sha256(body)
    rendered = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if body["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
