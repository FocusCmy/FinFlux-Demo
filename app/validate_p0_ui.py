from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_intake import build_run_presentation, project_worker_plan
from profile_registry import list_profiles


ROOT = Path(__file__).resolve().parent


def check(condition: bool, code: str, detail: Any) -> dict[str, Any]:
    return {"code": code, "passed": bool(condition), "detail": detail}


def main() -> int:
    from app import LIVE_REPOSITORY, compact_provider_usage

    gates: list[dict[str, Any]] = []
    registry = list_profiles()
    executable = [item for item in registry["profiles"] if item["live_executable"]]
    gates.append(check(registry["count"] == 5, "UI-A-PROFILE-REGISTRY", registry["count"]))
    gates.append(
        check(
            registry.get("source_protocol") == "FINFLUX_PROFILE_REGISTRY_V0.2"
            and len(str(registry.get("source_registry_sha256") or "")) == 64,
            "UI-A-FORMAL-REGISTRY-BINDING",
            registry.get("source_registry_sha256") or "NOT_BOUND",
        )
    )
    gates.append(check(len(executable) == 3, "UI-A-LIVE-PROFILES", [item["profile_id"] for item in executable]))

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    gates.append(check(html.count("data-stage-route=") == 4, "UI-C-FOUR-STAGES", html.count("data-stage-route=")))
    frontend = "\n".join(
        (ROOT / "web" / "js" / name).read_text(encoding="utf-8")
        for name in ("views-live.js", "views-changes.js")
    )
    forbidden = [
        item
        for item in ("submission.profile ===", "parsed.close", "parsed.settle", "parsed.qfq", "parsed.unit_nav")
        if item in frontend
    ]
    gates.append(check(not forbidden, "UI-B-NO-ASSET-BRANCH-IN-PRIMARY-UI", forbidden))

    candidate = None
    for summary in LIVE_REPOSITORY.list_runs(500):
        if not summary.get("judge_eligible"):
            continue
        run = LIVE_REPOSITORY.get_run(str(summary["run_id"]))
        usage = run.get("provider_usage") or {}
        plan = project_worker_plan(run)
        if (
            run.get("agentteams_run_id")
            and plan["complete"]
            and usage.get("status") == "PROVIDER_REPORTED"
            and int(usage.get("total_tokens") or 0) > 0
        ):
            candidate = run
            break
    gates.append(check(candidate is not None, "UI-0-REAL-BASELINE", candidate.get("run_id") if candidate else "NOT_FOUND"))

    if candidate:
        submission = LIVE_REPOSITORY.get_submission(candidate["submission_id"])
        presentation = build_run_presentation(candidate, submission)
        plan = project_worker_plan(candidate)
        used_skills = [
            item
            for item in LIVE_REPOSITORY.list_skills(candidate, None)
            if item.get("channel") == "AgentTeams Worker runtime"
            and int(item.get("runtime_invocations") or 0) > 0
        ]
        gates.extend(
            [
                check(presentation.get("protocol") == "FINFLUX_PRESENTATION_V1", "UI-B-PRESENTATION-ENVELOPE", presentation.get("protocol")),
                check(
                    bool(candidate.get("root_route_decision"))
                    and (candidate.get("manager_dispatch_receipt") or {}).get("status")
                    == "MANAGER_AUTHORIZED_DISPATCHED",
                    "UI-D-MANAGER-ROUTE",
                    (candidate.get("root_route_decision") or {}).get("route") or "NOT_FOUND",
                ),
                check(
                    (candidate.get("leader_relay") or {}).get("status") == "SENT",
                    "UI-D-CASE-LEAD-RELAY",
                    (candidate.get("leader_relay") or {}).get("event_id") or "NOT_FOUND",
                ),
                check(plan["required_count"] >= 3 and plan["complete"], "UI-D-WORKER-ARTIFACTS", f"{plan['completed_count']}/{plan['required_count']}"),
                check(len(presentation.get("artifacts") or []) == plan["required_count"], "UI-D-ARTIFACT-MATRIX", len(presentation.get("artifacts") or [])),
                check(len(used_skills) >= 5, "UI-D-RUNTIME-SKILLS", len(used_skills)),
                check(bool(candidate.get("datapass")), "UI-E-DATAPASS", (candidate.get("datapass") or {}).get("machine_recommendation")),
                check((candidate.get("human_gate") or {}).get("state") in {"AWAITING_HUMAN", "APPROVED", "REJECTED", "RETURNED"}, "UI-E-HUMAN-GATE", (candidate.get("human_gate") or {}).get("state")),
                check((candidate.get("provider_usage") or {}).get("status") == "PROVIDER_REPORTED", "UI-F-TRUE-TOKEN", (candidate.get("provider_usage") or {}).get("total_tokens")),
                check(
                    "records"
                    not in (
                        compact_provider_usage(candidate.get("provider_usage"))
                        .get("model_gateway_ledger")
                        or {}
                    ),
                    "UI-F-LIGHTWEIGHT-POLLING",
                    "SUMMARY_ONLY_FULL_LEDGER_IN_TRACE_AND_AUDIT",
                ),
                check(bool(candidate.get("events")), "UI-F-TRACE", len(candidate.get("events") or [])),
            ]
        )

    payload = {
        "protocol": "FINFLUX_P0_UI_ACCEPTANCE_V1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if all(item["passed"] for item in gates) else "FAIL",
        "passed": sum(1 for item in gates if item["passed"]),
        "total": len(gates),
        "gates": gates,
        "truth_boundary": "只读取版本化Profile、前端源码和持久化真实Run；不创建Run、不调用模型、不补写缺失结果。",
    }
    output_path = ROOT.parent / "artifacts" / "acceptance" / "p0-ui-acceptance.json"
    payload["acceptance_artifact"] = "artifacts/acceptance/p0-ui-acceptance.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
