from __future__ import annotations

import hashlib
import json
from typing import Any


PROTOCOL = "FINFLUX_PROVIDER_USAGE_DELTA_V1"


class ProviderUsageAttributionError(ValueError):
    """Raised when a provider ledger cannot be attributed without guessing."""


def _agent_key(item: dict[str, Any]) -> str:
    agent_id = str(item.get("agent_id") or "").strip()
    if not agent_id:
        raise ProviderUsageAttributionError("provider usage row is missing agent_id")
    return agent_id


def _model_key(item: dict[str, Any]) -> tuple[str, str]:
    provider_id = str(item.get("provider_id") or "").strip()
    model_name = str(item.get("model_name") or "").strip()
    if not provider_id or not model_name:
        raise ProviderUsageAttributionError(
            "provider usage model row is missing provider_id/model_name"
        )
    return provider_id, model_name


def _non_negative_int(item: dict[str, Any], field: str) -> int:
    raw = item.get(field, 0)
    if isinstance(raw, bool):
        raise ProviderUsageAttributionError(f"{field} must be an integer")
    try:
        value = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ProviderUsageAttributionError(f"{field} must be an integer") from exc
    if value < 0:
        raise ProviderUsageAttributionError(f"{field} must be non-negative")
    return value


def _flatten(snapshot: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for agent in snapshot.get("by_agent") or []:
        if not isinstance(agent, dict):
            raise ProviderUsageAttributionError("by_agent must contain objects")
        agent_id = _agent_key(agent)
        role = str(agent.get("role") or "unknown")
        for model in agent.get("models") or []:
            if not isinstance(model, dict):
                raise ProviderUsageAttributionError("models must contain objects")
            provider_id, model_name = _model_key(model)
            key = (agent_id, provider_id, model_name)
            if key in rows:
                raise ProviderUsageAttributionError(
                    "duplicate provider usage row: " + "/".join(key)
                )
            prompt = _non_negative_int(model, "prompt_tokens")
            completion = _non_negative_int(model, "completion_tokens")
            total = (
                _non_negative_int(model, "total_tokens")
                if "total_tokens" in model
                else prompt + completion
            )
            if total != prompt + completion:
                raise ProviderUsageAttributionError(
                    "provider total_tokens does not equal prompt+completion: "
                    + "/".join(key)
                )
            rows[key] = {
                "agent_id": agent_id,
                "role": role,
                "provider_id": provider_id,
                "model_name": model_name,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "call_count": _non_negative_int(model, "call_count"),
            }
    return rows


def attribute_exclusive_run_delta(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    run_id: str,
    exclusive_active_run: bool,
) -> dict[str, Any]:
    """Attribute a provider ledger delta to one Run without using text estimates.

    QwenPaw's per-session log only exposes the final aggregate written for a
    Matrix turn.  Its ``token_usage.json`` ledger, however, records every model
    call.  A daily-ledger delta is attributable to FinFlux only when the system
    proves that exactly one model-bearing Run was active for the whole window.
    Otherwise this function fails closed instead of inventing a Run total.
    """

    run_id = str(run_id or "").strip()
    if not run_id:
        raise ProviderUsageAttributionError("run_id is required")
    if not exclusive_active_run:
        raise ProviderUsageAttributionError(
            "daily provider delta is not attributable without an exclusive active Run"
        )
    baseline_day = str(baseline.get("date_utc") or "")
    current_day = str(current.get("date_utc") or "")
    if not baseline_day or baseline_day != current_day:
        raise ProviderUsageAttributionError(
            "provider usage baseline and current snapshot must use the same UTC day"
        )

    before = _flatten(baseline)
    after = _flatten(current)
    keys = sorted(set(before) | set(after))
    deltas: list[dict[str, Any]] = []
    for key in keys:
        previous = before.get(key) or {
            "agent_id": key[0],
            "role": "unknown",
            "provider_id": key[1],
            "model_name": key[2],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
        }
        observed = after.get(key)
        if observed is None:
            raise ProviderUsageAttributionError(
                "provider usage ledger regressed or dropped a model row: "
                + "/".join(key)
            )
        row = {
            "agent_id": observed["agent_id"],
            "role": observed["role"],
            "provider_id": observed["provider_id"],
            "model_name": observed["model_name"],
        }
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "call_count",
        ):
            delta = int(observed[field]) - int(previous[field])
            if delta < 0:
                raise ProviderUsageAttributionError(
                    f"provider usage counter regressed for {'/'.join(key)}:{field}"
                )
            row[field] = delta
        if row["total_tokens"] != row["prompt_tokens"] + row["completion_tokens"]:
            raise ProviderUsageAttributionError(
                "provider delta total does not equal prompt+completion: "
                + "/".join(key)
            )
        if row["call_count"] or row["total_tokens"]:
            deltas.append(row)

    prompt = sum(int(row["prompt_tokens"]) for row in deltas)
    completion = sum(int(row["completion_tokens"]) for row in deltas)
    total = sum(int(row["total_tokens"]) for row in deltas)
    calls = sum(int(row["call_count"]) for row in deltas)
    grouped: dict[str, dict[str, Any]] = {}
    for row in deltas:
        agent_id = str(row["agent_id"])
        agent = grouped.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "role": row["role"],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
                "models": [],
            },
        )
        model = {
            key: row[key]
            for key in (
                "provider_id",
                "model_name",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "call_count",
            )
        }
        agent["models"].append(model)
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "call_count",
        ):
            agent[field] += int(row[field])

    baseline_sha256 = hashlib.sha256(
        json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    current_sha256 = hashlib.sha256(
        json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    result = {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "date_utc": current_day,
        "status": "PROVIDER_REPORTED" if calls else "NO_MODEL_CALL",
        "attribution_status": "DAILY_DELTA_EXCLUSIVE_ACTIVE_RUN",
        "source": "QWENPAW_TOKEN_USAGE_JSON_DELTA",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "call_count": calls,
        "by_model": deltas,
        "by_agent": sorted(grouped.values(), key=lambda item: item["agent_id"]),
        "baseline_snapshot_sha256": baseline_sha256,
        "current_snapshot_sha256": current_sha256,
        "baseline_captured_at_utc": baseline.get("captured_at_utc"),
        "current_captured_at_utc": current.get("captured_at_utc"),
        "truth_boundary": (
            "供应商逐调用累计账本的启动前后差分；仅在全窗口唯一活动模型Run成立时归因。"
            "Matrix消息字符数和单条session usage不参与模型Token计算。"
        ),
    }
    result["attribution_sha256"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return result
