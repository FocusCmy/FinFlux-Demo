from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from change_control import canonical_sha256, resolve_downstream_lineage
from task_identity import TaskIdentityError, run_task_scope


ROLE = "downstream-impact-analyst"
OUTPUT_FILE = "downstream_impact_result.json"


def _validate_task_binding(case_id: str, run_id: str, task_id: str) -> str:
    try:
        expected = f"{run_task_scope(case_id, run_id)}-{ROLE}"
    except TaskIdentityError as exc:
        raise ValueError(f"invalid live task identity: {exc}") from exc
    if task_id != expected:
        raise ValueError("task_id is not the exact downstream task for this Live Run")
    return expected


def _task_root() -> Path:
    return Path(
        os.environ.get(
            "FINFLUX_TASK_ROOT",
            "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/tasks",
        )
    ).resolve()


def _safe_output_dir(task_id: str, output_dir: str | None) -> Path:
    root = _task_root()
    target = Path(output_dir).resolve() if output_dir else (root / task_id).resolve()
    if target.parent != root or target.name != task_id:
        raise ValueError("output directory must be the exact bounded task directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _decode_payload(value: str) -> dict[str, Any]:
    if not value or len(value) > 200_000:
        raise ValueError("change payload is missing or exceeds 200KB")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(value + padding)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid change payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("change payload must be an object")
    if not isinstance(payload.get("change_set"), dict):
        raise ValueError("change_set is required")
    if not isinstance(payload.get("downstream_tasks"), list):
        raise ValueError("downstream_tasks is required")
    return payload


def execute(
    payload: dict[str, Any],
    case_id: str,
    run_id: str,
    task_id: str,
    policy_id: str,
) -> dict[str, Any]:
    change_set = payload["change_set"]
    downstream_tasks = payload["downstream_tasks"]
    graph = resolve_downstream_lineage(change_set, downstream_tasks)
    skill_input = {
        "change_set": change_set,
        "downstream_tasks": downstream_tasks,
    }
    skill_output_hash = canonical_sha256(graph)
    definition = (
        "resolve-downstream-lineage@1.0.0|按显式依赖解析变更影响范围；"
        "缺失血缘必须标记 UNKNOWN_IMPACT"
    )
    invocation = {
        "skill_id": "resolve-downstream-lineage",
        "version": "1.0.0",
        "digest": hashlib.sha256(definition.encode("utf-8")).hexdigest(),
        "discovered_at_runtime": True,
        "input_sha256": canonical_sha256(skill_input),
        "output_sha256": skill_output_hash,
        "provider_tokens": 0,
    }
    summary = graph["summary"]
    recommendation = (
        "NEEDS_HUMAN_LINEAGE_REVIEW"
        if summary["unknown_impact_tasks"]
        else "RECOMPUTE_AFFECTED_TASKS"
        if summary["affected_tasks"]
        else "NO_IMPACT_BY_DECLARED_DEPENDENCIES"
    )
    return {
        "protocol": "FINFLUX_BOUNDED_CHANGE_WORKER_RESULT_V1.0",
        "role": ROLE,
        "case_id": case_id,
        "run_id": run_id,
        "task_id": task_id,
        "execution_policy_id": policy_id,
        "change_bundle_id": payload.get("change_bundle_id"),
        "status": "SUCCESS",
        "impact_graph": graph,
        "recommendation": recommendation,
        "skill_invocations": [invocation],
        "deterministic": True,
        "model_generated_financial_truth": False,
        "production_approved": False,
        "tool_run_id": hashlib.sha256(
            f"{ROLE}|{case_id}|{run_id}|{task_id}|{skill_output_hash}".encode(
                "utf-8"
            )
        ).hexdigest()[:24],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--change-payload-b64", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    _validate_task_binding(args.case_id, args.run_id, args.task_id)
    fault_mode = os.environ.get("FINFLUX_FAULT_INJECTION", "").strip()
    if fault_mode:
        if fault_mode != "TOOL_TIMEOUT" or not args.case_id.startswith("FI-"):
            raise ValueError("fault injection is restricted to FI-* acceptance cases")
        delay_seconds = int(
            os.environ.get("FINFLUX_FAULT_DELAY_SECONDS", "3")
        )
        if not 2 <= delay_seconds <= 30:
            raise ValueError("fault injection delay must be between 2 and 30 seconds")
        time.sleep(delay_seconds)
    output_dir = _safe_output_dir(args.task_id, args.output_dir)
    result = execute(
        _decode_payload(args.change_payload_b64),
        args.case_id,
        args.run_id,
        args.task_id,
        args.policy_id,
    )
    json_path = output_dir / OUTPUT_FILE
    result_path = output_dir / "result.md"
    with json_path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    lines = [
        "# FinFlux Bounded Change Impact Result",
        "",
        f"- case_id: {args.case_id}",
        f"- run_id: {args.run_id}",
        f"- task_id: {args.task_id}",
        f"- role: {ROLE}",
        f"- status: {result['status']}",
        f"- recommendation: {result['recommendation']}",
        f"- result_json: {json_path.name}",
    ]
    with result_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "status": result["status"],
                "result_path": str(result_path),
                "json_path": str(json_path),
                "tool_run_id": result["tool_run_id"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
