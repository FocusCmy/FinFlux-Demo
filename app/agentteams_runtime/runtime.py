from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from .config import (
    AgentTeamsUnavailable,
    CONTROLLER,
    CONTEXT_ROOT,
    CORE_WORKERS,
    DOCKER_CONTEXT,
    MANAGER,
    ROLE_LABELS,
    TEAM_NAME,
    VERSION,
    env_path,
    execution_policy,
    read_env,
)
from .store import RunStore


def docker(*args: str, timeout: int = 20, check: bool = True) -> str:
    executable = shutil.which("docker")
    if not executable:
        raise AgentTeamsUnavailable("Docker CLI不存在")
    completed = subprocess.run(
        [executable, "--context", DOCKER_CONTEXT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise AgentTeamsUnavailable(detail or "Docker命令失败")
    return completed.stdout.strip()


def container_names() -> set[str]:
    return {row.strip() for row in docker("ps", "--format", "{{.Names}}").splitlines() if row.strip()}


def resource(kind: str, name: str) -> dict[str, Any] | None:
    try:
        return json.loads(docker("exec", CONTROLLER, "agt", "get", kind, name, "-o", "json"))
    except (AgentTeamsUnavailable, json.JSONDecodeError):
        return None


def phase(value: dict[str, Any] | None) -> str:
    if not value:
        return "MISSING"
    raw = value.get("phase")
    if raw is None and isinstance(value.get("status"), dict):
        raw = value["status"].get("phase")
    return str(raw or "PENDING").upper()


def status() -> dict[str, Any]:
    env = read_env()
    policy = execution_policy()
    try:
        containers = container_names()
        docker_ready, docker_error = True, None
    except AgentTeamsUnavailable as exc:
        containers, docker_ready, docker_error = set(), False, str(exc)
    manager = resource("managers", "default") if CONTROLLER in containers else None
    team = resource("teams", TEAM_NAME) if CONTROLLER in containers else None
    team_status = team or {}
    if isinstance(team_status.get("status"), dict):
        team_status = team_status["status"]
    required_containers = {
        "agentteams-worker-finchange-case-lead",
        *(f"agentteams-worker-{role}" for role in CORE_WORKERS),
    }
    config_ready = all(
        env.get(key, "").strip()
        for key in ("AGENTTEAMS_LLM_PROVIDER", "AGENTTEAMS_DEFAULT_MODEL", "AGENTTEAMS_LLM_API_KEY")
    )
    provider_url = str(env.get("AGENTTEAMS_OPENAI_BASE_URL") or "").strip()
    parsed_provider_url = urllib.parse.urlsplit(provider_url) if provider_url else None
    safe_provider_endpoint = (
        urllib.parse.urlunsplit(
            (
                parsed_provider_url.scheme,
                parsed_provider_url.netloc,
                parsed_provider_url.path.rstrip("/"),
                "",
                "",
            )
        )
        if parsed_provider_url and parsed_provider_url.scheme and parsed_provider_url.netloc
        else None
    )
    connected = bool(
        docker_ready
        and CONTROLLER in containers
        and MANAGER in containers
        and manager
        and team
        and phase(team) == "ACTIVE"
        and required_containers.issubset(containers)
    )
    if connected:
        state, note = "CONNECTED", "AgentTeams核心Team、Run存储与Matrix传输已就绪。"
    elif not config_ready:
        state, note = "RUNTIME_CONFIG_REQUIRED", "模型配置未完成；确定性Skill仍可使用。"
    elif not docker_ready:
        state, note = "DOCKER_ACCESS_UNAVAILABLE", docker_error or "Docker不可访问"
    else:
        state, note = "RESOURCES_NOT_READY", "AgentTeams核心资源尚未全部Ready。"
    topology = [
        {"name": "default", "label": "Global Manager", "role": "manager", "phase": phase(manager)},
        {"name": "finchange-case-lead", "label": "FinFlux Case Lead", "role": "team_leader", "phase": "RUNNING" if "agentteams-worker-finchange-case-lead" in containers else "MISSING"},
        *[
            {"name": role, "label": ROLE_LABELS[role][0], "role": "worker", "phase": "RUNNING" if f"agentteams-worker-{role}" in containers else "MISSING"}
            for role in CORE_WORKERS
        ],
        {"name": "finchange-data-owner", "label": "FinFlux Data Owner", "role": "human", "phase": "READY" if env.get("FINCHANGE_MATRIX_HUMAN_PASSWORD") else "MISSING"},
    ]
    return {
        "platform_version": VERSION,
        "api_group": "agentteams.io/v1beta1",
        "connected": connected,
        "status": state,
        "truthful_note": note,
        "docker_ready": docker_ready,
        "docker_error": docker_error,
        "runtime_config_ready": config_ready,
        "runtime_config_source": str(env_path()),
        "model_connection": {
            "provider": env.get("AGENTTEAMS_LLM_PROVIDER") or None,
            "default_model": env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
            "provider_endpoint": safe_provider_endpoint,
            "api_key_configured": bool(env.get("AGENTTEAMS_LLM_API_KEY")),
            "gateway_endpoint": "http://127.0.0.1:18082/v1",
            "traffic_via_run_gateway": True,
            "role_models": {
                "manager": env.get("FINCHANGE_MANAGER_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
                "case_lead": env.get("FINCHANGE_LEADER_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
                "evidence": env.get("FINCHANGE_EVIDENCE_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
                "semantic": env.get("FINCHANGE_ANALYST_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
                "validator": env.get("FINCHANGE_VALIDATOR_MODEL") or env.get("AGENTTEAMS_DEFAULT_MODEL") or None,
            },
            "secret_exposed": False,
        },
        "human_credentials_ready": bool(env.get("FINCHANGE_MATRIX_HUMAN_PASSWORD")),
        "human_identity": env.get("FINCHANGE_MATRIX_HUMAN_USER") or None,
        "team": {"name": TEAM_NAME, "phase": phase(team), "ready_workers": team_status.get("readyWorkers"), "total_workers": team_status.get("totalWorkers")},
        "topology": topology,
        "bounded_execution": {"ready": True, "policy_id": policy["policy_id"], "mode": policy["mode"], "limits": policy.get("run_limits", {})},
        "element_url": "http://127.0.0.1:18088/",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def provider_guard(store: RunStore, force: bool = False) -> dict[str, Any]:
    """Cheap dispatch guard; provider usage is read from persisted per-Run ledgers.

    This deliberately does not scan every Agent container. Actual provider
    usage is attributed by the model gateway and projected into each Run.
    """
    active = store.active()
    policy = execution_policy()
    maximum = int((policy.get("run_limits") or {}).get("max_active_runs", 1))
    allowed = len(active) < maximum
    active_runs = [
        {
            "run_id": item.get("run_id"),
            "case_id": item.get("case_id"),
            "state": item.get("state"),
            "human_state": (item.get("human_gate") or {}).get("state"),
            "agentteams_bound": bool(item.get("agentteams_run_id")),
            "workers_completed": int(
                ((item.get("agent_result") or {}).get("workers_completed") or 0)
            ),
            "workers_required": int(
                ((item.get("agent_result") or {}).get("workers_required") or 0)
            ),
        }
        for item in active
    ]
    return {
        "protocol": "FINFLUX_PROVIDER_DISPATCH_GUARD_V2",
        "status": "READY" if allowed else "BLOCKED",
        "allowed": allowed,
        "active_run_count": len(active),
        "active_run_ids": [item.get("run_id") for item in active],
        "active_runs": active_runs,
        "provider_usage_captured": all(bool(item.get("provider_usage")) for item in active),
        "reasons": [] if allowed else [f"ACTIVE_RUN_LIMIT:{len(active)}>={maximum}"],
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "PERSISTED_RUN_GATEWAY_LEDGERS",
        "force_requested": bool(force),
    }


def publish_context(handle: dict[str, Any], workers: tuple[str, ...]) -> dict[str, Any]:
    """Copy one content-addressed role slice to its owning Worker container."""
    shared = "/root/agentteams-fs/teams/finchange-cross-asset-review/shared/context-capsules"
    slices = handle.get("role_slice_handles") or {}
    if set(slices) != set(workers):
        raise AgentTeamsUnavailable("Context Slice集合与Worker路由不一致")
    replicas = []
    for role in workers:
        digest = str((slices[role] or {}).get("slice_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AgentTeamsUnavailable(f"{role} Context Slice哈希无效")
        source = CONTEXT_ROOT / f"{digest}.json"
        if not source.is_file():
            raise AgentTeamsUnavailable(f"{role} Context Slice不存在")
        container = f"agentteams-worker-{role}"
        docker("exec", container, "mkdir", "-p", shared)
        docker("cp", str(source), f"{container}:{shared}/{digest}.json")
        replicas.append({"role": role, "container": container, "slice_sha256": digest})
    return {"status": "PUBLISHED", "replicas": replicas, "replica_count": len(replicas)}
