from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from model_gateway_control import (
    close_model_gateway_run,
    extend_model_gateway_same_run_recovery_window,
    prepare_model_gateway_run,
    raise_model_gateway_same_run_budget,
    rearm_model_gateway_after_transport_failure,
    rearm_model_gateway_expired_before_first_call,
    rotate_model_gateway_actor_identities,
)

from .config import APP_ROOT, CORE_WORKERS
from .config import execution_policy
from .runtime import docker


CONTROL_ROOT = APP_ROOT / "runtime" / "model_gateway"
_ACTORS = ("manager", "finchange-case-lead", *CORE_WORKERS)
_CONTAINERS = {
    "manager": ("agentteams-manager", 18799, "/root/manager-workspace/.qwenpaw/token_usage.json"),
    "finchange-case-lead": (
        "agentteams-worker-finchange-case-lead",
        8088,
        "/root/agentteams-fs/agents/finchange-case-lead/.qwenpaw/token_usage.json",
    ),
    **{
        role: (
            f"agentteams-worker-{role}",
            8088,
            f"/root/agentteams-fs/agents/{role}/.qwenpaw/token_usage.json",
        )
        for role in CORE_WORKERS
    },
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_ids(run: dict[str, Any]) -> dict[str, str]:
    identity = run["matrix_handle"]["task_identity"]
    scope = str(identity["task_scope"])
    return {
        "manager": scope + "-manager-dispatch",
        "finchange-case-lead": scope + "-case-lead-aggregate",
        **{role: str(identity["task_ids"][role]) for role in CORE_WORKERS},
    }


def capture_usage() -> dict[str, Any]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    script = (
        "import json,sys,pathlib; p=pathlib.Path(sys.argv[1]); day=sys.argv[2];"
        " data=json.loads(p.read_text()) if p.is_file() else {};"
        " rows=data.get(day,{}) if isinstance(data,dict) else {};"
        " print(json.dumps(rows))"
    )
    by_agent: list[dict[str, Any]] = []
    for actor in _ACTORS:
        container, _port, path = _CONTAINERS[actor]
        raw = docker("exec", container, "python3", "-c", script, path, day, timeout=15)
        rows = json.loads(raw or "{}")
        models = []
        for key, value in sorted(rows.items()):
            if not isinstance(value, dict):
                continue
            prompt = int(value.get("prompt_tokens", 0) or 0)
            completion = int(value.get("completion_tokens", 0) or 0)
            models.append(
                {
                    "model_key": str(key),
                    "provider_id": str(value.get("provider_id") or ""),
                    "model_name": str(value.get("model_name") or ""),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                    "call_count": int(value.get("call_count", 0) or 0),
                }
            )
        by_agent.append(
            {
                "agent_id": actor,
                "role": "team_leader" if actor == "finchange-case-lead" else actor,
                "prompt_tokens": sum(row["prompt_tokens"] for row in models),
                "completion_tokens": sum(row["completion_tokens"] for row in models),
                "total_tokens": sum(row["total_tokens"] for row in models),
                "call_count": sum(row["call_count"] for row in models),
                "models": models,
            }
        )
    prompt = sum(row["prompt_tokens"] for row in by_agent)
    completion = sum(row["completion_tokens"] for row in by_agent)
    return {
        "date_utc": day,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "QWENPAW_TOKEN_USAGE_JSON",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "call_count": sum(row["call_count"] for row in by_agent),
        "by_agent": by_agent,
    }


def _configure_actor(actor: str, headers: dict[str, str]) -> None:
    container, port, _path = _CONTAINERS[actor]
    script = (
        "import json,sys,urllib.request;"
        " body=json.dumps({'custom_headers':json.loads(sys.argv[1])}).encode();"
        " req=urllib.request.Request(sys.argv[2],data=body,method='PUT',"
        " headers={'Content-Type':'application/json'});"
        " r=urllib.request.urlopen(req,timeout=15);"
        " print(json.dumps({'status':r.status}))"
    )
    result = docker(
        "exec",
        container,
        "python3",
        "-c",
        script,
        json.dumps(headers, separators=(",", ":")),
        f"http://127.0.0.1:{port}/api/models/agentteams-gateway/config",
        timeout=25,
    )
    if int((json.loads(result or "{}")).get("status", 0)) != 200:
        raise RuntimeError(f"{actor} model headers were not bound")


def activate(run: dict[str, Any]) -> dict[str, Any]:
    task_ids = _task_ids(run)
    identities = {actor: secrets.token_urlsafe(32) for actor in _ACTORS}
    for actor in _ACTORS:
        _configure_actor(
            actor,
            {
                "X-FinFlux-Run-ID": str(run["run_id"]),
                "X-FinFlux-Actor": actor,
                "X-FinFlux-Identity": identities[actor],
                "X-FinFlux-Task-ID": task_ids[actor],
            },
        )
    baseline = capture_usage()
    provider_policy = execution_policy().get("provider_usage_observation") or {}
    binding = prepare_model_gateway_run(
        CONTROL_ROOT,
        run_id=str(run["run_id"]),
        provider_token_hard_cap=int(
            provider_policy.get("provider_token_hard_cap", 240_000)
        ),
        max_model_calls=int(provider_policy.get("max_model_calls_per_run", 16)),
        max_output_tokens_per_call=int(
            provider_policy.get("max_output_tokens_per_call", 2048)
        ),
        max_wall_time_seconds=600,
        provider_usage_baseline=baseline,
        actor_identities=identities,
        actor_task_ids=task_ids,
    )
    actor_binding_receipt = {
        "protocol": "FINFLUX_MODEL_ACTOR_BINDING_RECEIPT_V1",
        "status": "BOUND",
        "run_id": run["run_id"],
        "actors": list(_ACTORS),
        "actor_task_ids": task_ids,
        "actor_identity_sha256": {key: _sha(value) for key, value in identities.items()},
        "plaintext_identity_persisted_in_run": False,
    }
    actor_binding_receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            actor_binding_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "binding": binding,
        "baseline": baseline,
        "actor_binding_receipt": actor_binding_receipt,
    }


def usage(run_id: str) -> dict[str, Any]:
    path = CONTROL_ROOT / "gateway_ledger.json"
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "NOT_CAPTURED", "source": "MODEL_GATEWAY_LEDGER"}
    if ledger.get("run_id") != run_id:
        return {"status": "NOT_CAPTURED", "source": "MODEL_GATEWAY_LEDGER"}
    return {
        "status": "PROVIDER_REPORTED" if int(ledger.get("provider_call_count", 0)) else "NO_MODEL_CALL",
        "source": "FINFLUX_MODEL_GATEWAY_LEDGER",
        "prompt_tokens": int(ledger.get("prompt_tokens", 0)),
        "completion_tokens": int(ledger.get("completion_tokens", 0)),
        "total_tokens": int(ledger.get("total_tokens", 0)),
        "call_count": int(ledger.get("provider_call_count", 0)),
        "model_gateway_ledger": ledger,
    }


def close(run_id: str, state: str) -> dict[str, Any]:
    receipt = close_model_gateway_run(CONTROL_ROOT, run_id=run_id, state=state)
    for actor in _ACTORS:
        _configure_actor(actor, {})
    return receipt


def rearm_transport_failure(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    return rearm_model_gateway_after_transport_failure(
        CONTROL_ROOT,
        run_id=run_id,
        requested_by=requested_by,
        reason=reason,
    )


def rearm_expired_before_first_call(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    return rearm_model_gateway_expired_before_first_call(
        CONTROL_ROOT,
        run_id=run_id,
        requested_by=requested_by,
        reason=reason,
    )


def rebind_same_run_actors(
    run_id: str, *, requested_by: str, reason: str
) -> dict[str, Any]:
    """Restore volatile container headers without changing the Run identity."""

    try:
        binding = json.loads((CONTROL_ROOT / "active_run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("model gateway binding is unavailable for actor rebind") from exc
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise RuntimeError("actor rebind requires the same active Run")
    task_ids = dict(binding.get("actor_task_ids") or {})
    if set(task_ids) != set(_ACTORS):
        raise RuntimeError("actor rebind task set does not match runtime topology")
    identities = {actor: secrets.token_urlsafe(32) for actor in _ACTORS}
    identity_hashes = {actor: _sha(secret) for actor, secret in identities.items()}
    rotation = rotate_model_gateway_actor_identities(
        CONTROL_ROOT,
        run_id=run_id,
        actor_identity_sha256=identity_hashes,
        requested_by=requested_by,
        reason=reason,
    )
    for actor in _ACTORS:
        _configure_actor(
            actor,
            {
                "X-FinFlux-Run-ID": run_id,
                "X-FinFlux-Actor": actor,
                "X-FinFlux-Identity": identities[actor],
                "X-FinFlux-Task-ID": str(task_ids[actor]),
            },
        )
    return {
        **rotation,
        "actors_rebound": list(_ACTORS),
        "plaintext_identity_persisted": False,
    }


def extend_same_run_recovery_window(run_id: str, *, reason: str) -> dict[str, Any]:
    return extend_model_gateway_same_run_recovery_window(
        CONTROL_ROOT, run_id=run_id, reason=reason
    )


def raise_same_run_budget(
    run_id: str, *, token_cap: int, max_calls: int, requested_by: str, reason: str
) -> dict[str, Any]:
    return raise_model_gateway_same_run_budget(
        CONTROL_ROOT,
        run_id=run_id,
        new_provider_token_hard_cap=token_cap,
        new_max_model_calls=max_calls,
        requested_by=requested_by,
        reason=reason,
    )
