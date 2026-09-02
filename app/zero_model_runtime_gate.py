"""Read-only readiness gate for the public AgentTeams adapter.

No Matrix message is sent and no model identity is invoked. This gate checks
the exact runtime prerequisites used by a later Live Run.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from agentteams_adapter import provider_token_guard_snapshot, runtime_status
from agentteams_runtime.config import CORE_WORKERS, execution_policy
from agentteams_runtime.runtime import container_names


def run_gate(workers: tuple[str, ...] = CORE_WORKERS) -> dict:
    status = runtime_status()
    guard = provider_token_guard_snapshot(force=True)
    containers = container_names() if status.get("docker_ready") else set()
    expected = {
        "agentteams-worker-finchange-case-lead",
        *(f"agentteams-worker-{role}" for role in workers),
    }
    missing = sorted(expected - containers)
    checks = {
        "runtime_connected": bool(status.get("connected")),
        "dispatch_guard_allowed": bool(guard.get("allowed")),
        "core_worker_containers_ready": not missing,
        "policy_fail_closed": execution_policy().get("mode") == "FAIL_CLOSED",
        "exact_worker_set": tuple(workers) == CORE_WORKERS,
    }
    return {
        "protocol": "FINFLUX_ZERO_MODEL_RUNTIME_GATE_V2",
        "status": "PASS" if all(checks.values()) else "BLOCK",
        "checks": checks,
        "missing_containers": missing,
        "runtime": status,
        "dispatch_guard": guard,
        "model_called": False,
        "provider_tokens": 0,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_gate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
