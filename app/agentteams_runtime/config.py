from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_ROOT.parent
AGENTTEAMS_ROOT = PROJECT_ROOT / "agentteams"
DEFAULT_ENV_PATH = AGENTTEAMS_ROOT / ".env"
RUNS_ROOT = APP_ROOT / "runtime" / "agent_runs"
FORMAL_RUNS_ROOT = APP_ROOT / "runtime" / "live_intake" / "runs"
CONTEXT_ROOT = APP_ROOT / "runtime" / "context_cache"
POLICY_PATH = AGENTTEAMS_ROOT / "config" / "execution_policy.json"

DOCKER_CONTEXT = "desktop-linux"
CONTROLLER = "agentteams-controller"
MANAGER = "agentteams-manager"
TEAM_NAME = "finchange-cross-asset-review"
VERSION = "v1.2.2"
CORE_WORKERS = (
    "evidence-investigator",
    "semantic-impact-analyst",
    "independent-validator",
)
ROLE_LABELS = {
    "manager": ("Manager", "manager"),
    "finchange-case-lead": ("FinFlux Case Lead", "team_leader"),
    "evidence-investigator": ("Evidence Investigator", "worker"),
    "semantic-impact-analyst": ("Semantic Impact Analyst", "worker"),
    "independent-validator": ("Independent Validator", "worker"),
    "finchange-data-owner": ("FinFlux Data Owner", "human"),
    "admin": ("Admin Relay", "system_relay"),
}


class AgentTeamsUnavailable(RuntimeError):
    """The deployed AgentTeams or Matrix transport cannot be reached."""


class AgentTeamsConfigurationError(RuntimeError):
    """A local contract or required runtime setting is invalid."""


def env_path() -> Path:
    configured = os.environ.get("FINFLUX_RUNTIME_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if DEFAULT_ENV_PATH.is_file():
        return DEFAULT_ENV_PATH
    # Public checkouts never search sibling projects for legacy credentials.
    # A real runtime must be selected explicitly or placed in this checkout's
    # git-ignored agentteams/.env.
    return DEFAULT_ENV_PATH


def read_env() -> dict[str, str]:
    path = env_path()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def execution_policy() -> dict[str, Any]:
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentTeamsConfigurationError(f"执行策略不可读取: {POLICY_PATH}") from exc
    if policy.get("mode") != "FAIL_CLOSED" or not policy.get("policy_id"):
        raise AgentTeamsConfigurationError("执行策略必须为FAIL_CLOSED并声明policy_id")
    return policy
