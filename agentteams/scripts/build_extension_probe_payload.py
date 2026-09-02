from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a real, run-scoped FinFlux payload for extension Agent acceptance."
    )
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]
    app_root = project_root / "app"
    sys.path.insert(0, str(app_root))

    from agentteams_adapter import _live_worker_payload
    from context_capsule import build_run_context_capsule
    from app import LIVE_REPOSITORY
    from research_data.investigator import build_run_research_bundle

    source_run = LIVE_REPOSITORY.get_run(args.source_run_id)
    submission = LIVE_REPOSITORY.get_submission(source_run["submission_id"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    probe_run_id = f"RUN-EXT-PROBE-{timestamp}-{secrets.token_hex(2)}"
    probe_run = dict(source_run)
    probe_run["run_id"] = probe_run_id
    research = build_run_research_bundle(
        "futures", str(probe_run["case_id"]), probe_run_id
    )
    payload, encoded = _live_worker_payload(submission, probe_run, research)
    selected_workers = [
        "evidence-investigator",
        "semantic-impact-analyst",
        "data-rights-steward",
        "research-context-analyst",
        "runtime-resilience-auditor",
        "independent-validator",
    ]
    context_root = project_root / "agentteams" / "build" / "context-probe"
    _, context_handle = build_run_context_capsule(
        case_id=str(probe_run["case_id"]),
        run_id=probe_run_id,
        payload=payload,
        selected_workers=selected_workers,
        execution_policy_id="FINFLUX-BOUNDED-EXECUTION-V0.1",
        root_route_decision_handle={
            "decision_sha256": str(
                (source_run.get("root_route_decision") or {}).get(
                    "decision_sha256", ""
                )
            )
        },
        local_root=context_root,
    )
    capsule_sha256 = context_handle["capsule_sha256"]

    output = {
        "protocol": "FINFLUX_EXTENSION_PROBE_INPUT_V0.2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_run_id": args.source_run_id,
        "probe_run_id": probe_run_id,
        "case_id": str(probe_run["case_id"]),
        "submission_id": str(submission["submission_id"]),
        "instrument": str(payload["i"]),
        "trade_date": str(payload["d"]),
        "candidate_mapping": str(payload["m"]),
        "research_item_count": int(payload["rc"]),
        "file_sha256": str(payload["h"]),
        "evidence_root_hash": str(payload["r"]),
        "worker_payload_sha256": str(payload["ph"]),
        "live_payload_b64": encoded,
        "context_capsule_sha256": capsule_sha256,
        "context_capsule_local_path": str(
            (context_root / f"{capsule_sha256}.json").resolve()
        ),
        "context_capsule_shared_path": context_handle["shared_path"],
        "context_skill_invocation_count": context_handle[
            "skill_invocation_count"
        ],
    }
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "probe_run_id": probe_run_id,
                "case_id": output["case_id"],
                "worker_payload_sha256": output["worker_payload_sha256"],
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
