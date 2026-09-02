from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BINDING_PROTOCOL = "FINFLUX_MODEL_GATEWAY_RUN_BINDING_V1"
ROUTE_ATTESTATION_PROTOCOL = "FINFLUX_MODEL_GATEWAY_ROUTE_ATTESTATION_V1"


class ModelGatewayControlError(RuntimeError):
    pass


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelGatewayControlError(reason) from exc
    if not isinstance(payload, dict):
        raise ModelGatewayControlError(reason)
    return payload


def _usage_totals(snapshot: dict[str, Any]) -> dict[str, int]:
    """Extract cumulative provider counters without inventing zero values."""
    source = snapshot.get("daily") if isinstance(snapshot.get("daily"), dict) else snapshot
    totals: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count"):
        value = source.get(key)
        if isinstance(value, bool):
            raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_UNREADABLE")
        try:
            totals[key] = int(value)
        except (TypeError, ValueError) as exc:
            raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_INCOMPLETE:" + key) from exc
        if totals[key] < 0:
            raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_NEGATIVE:" + key)
    if totals["total_tokens"] != totals["prompt_tokens"] + totals["completion_tokens"]:
        raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_TOTAL_MISMATCH")
    return totals


def validate_route_attestation(control_root: Path) -> dict[str, Any]:
    payload = _read_json(
        control_root / "route-attestation.json", "MODEL_GATEWAY_ROUTE_ATTESTATION_UNREADABLE"
    )
    if payload.get("protocol") != ROUTE_ATTESTATION_PROTOCOL:
        raise ModelGatewayControlError("MODEL_GATEWAY_ROUTE_ATTESTATION_PROTOCOL_MISMATCH")
    if payload.get("status") != "READY":
        raise ModelGatewayControlError("MODEL_GATEWAY_ROUTE_NOT_READY")
    expected = str(payload.get("attestation_sha256") or "")
    material = {key: value for key, value in payload.items() if key != "attestation_sha256"}
    if expected != _sha(material):
        raise ModelGatewayControlError("MODEL_GATEWAY_ROUTE_ATTESTATION_HASH_MISMATCH")
    if str(payload.get("provider_custom_url") or "").rstrip("/") != str(
        payload.get("expected_gateway_url") or ""
    ).rstrip("/"):
        raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_ROUTE_BYPASSES_SIDECAR")
    return payload


def prepare_model_gateway_run(
    control_root: Path,
    *,
    run_id: str,
    provider_token_hard_cap: int,
    max_model_calls: int,
    max_output_tokens_per_call: int,
    max_wall_time_seconds: int,
    provider_usage_baseline: dict[str, Any],
    actor_identities: dict[str, str] | None = None,
    actor_task_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the sidecar to exactly one Run before any Matrix/model event."""

    route = validate_route_attestation(control_root)
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ModelGatewayControlError("MODEL_GATEWAY_RUN_ID_REQUIRED")
    if not isinstance(provider_usage_baseline, dict) or not provider_usage_baseline.get(
        "date_utc"
    ):
        raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_REQUIRED")
    if not provider_usage_baseline.get("captured_at_utc"):
        raise ModelGatewayControlError("MODEL_GATEWAY_PROVIDER_BASELINE_TIMESTAMP_REQUIRED")
    baseline_totals = _usage_totals(provider_usage_baseline)
    if not isinstance(actor_identities, dict) or not actor_identities:
        raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_IDENTITIES_REQUIRED")
    actor_identity_sha256: dict[str, str] = {}
    for actor, identity in actor_identities.items():
        actor_name = str(actor or "").strip()
        secret = str(identity or "")
        if not actor_name or not secret:
            raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_IDENTITY_INVALID")
        if actor_name in actor_identity_sha256:
            raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_IDENTITY_DUPLICATE")
        actor_identity_sha256[actor_name] = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if not isinstance(actor_task_ids, dict) or set(actor_task_ids) != set(actor_identity_sha256):
        raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_TASK_SET_MISMATCH")
    normalized_task_ids: dict[str, str] = {}
    for actor, task_id in actor_task_ids.items():
        normalized = str(task_id or "").strip()
        if not normalized:
            raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_TASK_ID_REQUIRED")
        normalized_task_ids[str(actor)] = normalized
    if len(set(normalized_task_ids.values())) != len(normalized_task_ids):
        raise ModelGatewayControlError("MODEL_GATEWAY_ACTOR_TASK_IDS_NOT_UNIQUE")
    baseline_record = {
        "protocol": "FINFLUX_PROVIDER_USAGE_BASELINE_V1",
        "run_id": run_id,
        "captured_before_model_dispatch": True,
        "snapshot": provider_usage_baseline,
        "cumulative_totals": baseline_totals,
    }
    baseline_record["baseline_sha256"] = _sha(baseline_record)
    binding_path = control_root / "active_run.json"
    if binding_path.exists():
        existing = _read_json(binding_path, "MODEL_GATEWAY_EXISTING_BINDING_UNREADABLE")
        existing_hash = str(existing.get("binding_sha256") or "")
        existing_unsigned = {
            key: value for key, value in existing.items() if key != "binding_sha256"
        }
        if existing_hash != _sha(existing_unsigned):
            raise ModelGatewayControlError("MODEL_GATEWAY_EXISTING_BINDING_HASH_MISMATCH")
        if existing.get("state") == "ACTIVE" and existing.get("run_id") != run_id:
            raise ModelGatewayControlError(
                "MODEL_GATEWAY_ALREADY_BOUND_TO_OTHER_RUN:" + str(existing.get("run_id"))
            )
        if existing.get("state") == "ACTIVE" and existing.get("run_id") == run_id:
            return existing
        if existing.get("state") == "CLOSED" and existing.get("run_id") == run_id:
            raise ModelGatewayControlError("MODEL_GATEWAY_RUN_ALREADY_CLOSED:" + run_id)
        if existing.get("state") not in {"ACTIVE", "CLOSED"}:
            raise ModelGatewayControlError(
                "MODEL_GATEWAY_EXISTING_BINDING_STATE_INVALID:"
                + str(existing.get("state") or "")
            )

    ledger_path = control_root / "gateway_ledger.json"
    if ledger_path.exists():
        old = _read_json(ledger_path, "MODEL_GATEWAY_PREVIOUS_LEDGER_UNREADABLE")
        old_run_id = str(old.get("run_id") or "UNKNOWN")
        history = control_root / "history"
        history.mkdir(parents=True, exist_ok=True)
        archive = history / f"{old_run_id}-{_sha(old)[:12]}.json"
        if archive.exists():
            raise ModelGatewayControlError("MODEL_GATEWAY_LEDGER_ARCHIVE_COLLISION")
        os.replace(ledger_path, archive)

    # Baseline is made durable before the ACTIVE binding is published.  A
    # sidecar request therefore cannot be authorized while the baseline is
    # missing, even if the caller crashes between Matrix dispatch steps.
    _atomic(control_root / "provider_usage_baseline.json", baseline_record)

    started = datetime.now(timezone.utc)
    binding = {
        "protocol": BINDING_PROTOCOL,
        "run_id": run_id,
        "state": "ACTIVE",
        "started_at_utc": started.isoformat(),
        "expires_at_utc": (
            started + timedelta(seconds=int(max_wall_time_seconds))
        ).isoformat(),
        "provider_token_hard_cap": int(provider_token_hard_cap),
        "max_model_calls": int(max_model_calls),
        "max_output_tokens_per_call": int(max_output_tokens_per_call),
        "route_attestation_sha256": route["attestation_sha256"],
        "provider_usage_baseline_sha256": baseline_record["baseline_sha256"],
        "provider_usage_baseline_total_tokens": baseline_totals["total_tokens"],
        "actor_identity_sha256": actor_identity_sha256,
        "actor_task_ids": normalized_task_ids,
    }
    if min(
        binding["provider_token_hard_cap"],
        binding["max_model_calls"],
        binding["max_output_tokens_per_call"],
        int(max_wall_time_seconds),
    ) <= 0:
        raise ModelGatewayControlError("MODEL_GATEWAY_LIMITS_MUST_BE_POSITIVE")
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return binding


def close_model_gateway_run(control_root: Path, *, run_id: str, state: str) -> dict[str, Any]:
    binding_path = control_root / "active_run.json"
    binding = _read_json(binding_path, "MODEL_GATEWAY_BINDING_UNREADABLE")
    if binding.get("run_id") != run_id:
        raise ModelGatewayControlError("MODEL_GATEWAY_CLOSE_RUN_MISMATCH")
    declared_hash = str(binding.get("binding_sha256") or "")
    unsigned = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if declared_hash != _sha(unsigned):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")
    terminal_state = str(state or "").strip()
    if not terminal_state:
        raise ModelGatewayControlError("MODEL_GATEWAY_TERMINAL_STATE_REQUIRED")
    # Closing a Run is an immutable boundary.  Page refreshes and repeated
    # status polls must return the original receipt byte-for-byte instead of
    # rewriting closed_at_utc and changing the evidence hash.
    if binding.get("state") == "CLOSED":
        if binding.get("terminal_state") != terminal_state:
            raise ModelGatewayControlError(
                "MODEL_GATEWAY_ALREADY_CLOSED_WITH_DIFFERENT_STATE:"
                + str(binding.get("terminal_state") or "")
            )
        return binding
    if binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError(
            "MODEL_GATEWAY_BINDING_STATE_INVALID:" + str(binding.get("state") or "")
        )
    binding["state"] = "CLOSED"
    binding["terminal_state"] = terminal_state
    binding["closed_at_utc"] = datetime.now(timezone.utc).isoformat()
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return binding


def rearm_model_gateway_after_transport_failure(
    control_root: Path, *, run_id: str, requested_by: str, reason: str
) -> dict[str, Any]:
    """Re-open the *same* active Run after a recoverable provider disconnect.

    This deliberately preserves the cumulative call and Token counters.  It
    does not create a new Run, reset the budget, or permit semantic retries
    after a deterministic/financial failure.  Stale in-flight reservations
    are released only because the sidecar has already sealed a transport-
    failure record and stopped accepting calls for the Run.
    """

    binding = _read_json(
        control_root / "active_run.json", "MODEL_GATEWAY_BINDING_UNREADABLE"
    )
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError("MODEL_GATEWAY_REARM_REQUIRES_SAME_ACTIVE_RUN")
    declared_binding_hash = str(binding.get("binding_sha256") or "")
    binding_material = {
        key: value for key, value in binding.items() if key != "binding_sha256"
    }
    if declared_binding_hash != _sha(binding_material):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")

    ledger_path = control_root / "gateway_ledger.json"
    ledger = _read_json(ledger_path, "MODEL_GATEWAY_LEDGER_UNREADABLE")
    if ledger.get("run_id") != run_id:
        raise ModelGatewayControlError("MODEL_GATEWAY_LEDGER_RUN_MISMATCH")
    declared_ledger_hash = str(ledger.get("ledger_sha256") or "")
    ledger_material = {
        key: value for key, value in ledger.items() if key != "ledger_sha256"
    }
    if declared_ledger_hash and declared_ledger_hash != _sha(ledger_material):
        raise ModelGatewayControlError("MODEL_GATEWAY_LEDGER_HASH_MISMATCH")
    if ledger.get("status") != "FUSE_TRIPPED" or ledger.get("fuse_reason") != "UPSTREAM_TRANSPORT_FAILURE":
        raise ModelGatewayControlError("MODEL_GATEWAY_FAILURE_NOT_REARMABLE")

    reservations = ledger.get("reservations") or {}
    if not isinstance(reservations, dict):
        raise ModelGatewayControlError("MODEL_GATEWAY_RESERVATIONS_UNREADABLE")
    released = [
        {
            "reservation_id": reservation_id,
            "actor": str((reservation or {}).get("actor") or ""),
            "task_id": str((reservation or {}).get("task_id") or ""),
            "maximum_exposure_tokens": int(
                (reservation or {}).get("maximum_exposure_tokens", 0) or 0
            ),
        }
        for reservation_id, reservation in sorted(reservations.items())
        if isinstance(reservation, dict)
    ]
    original_expires_at = str(binding.get("expires_at_utc") or "")
    renewed_at = datetime.now(timezone.utc)
    binding["expires_at_utc"] = (renewed_at + timedelta(seconds=900)).isoformat()
    binding_history = list(binding.get("recovery_window_extensions") or [])
    window_event = {
        "run_id": run_id,
        "reason": "UPSTREAM_TRANSPORT_FAILURE",
        "previous_expires_at_utc": original_expires_at,
        "extended_at_utc": renewed_at.isoformat(),
        "new_expires_at_utc": binding["expires_at_utc"],
        "token_and_call_counters_reset": False,
    }
    window_event["event_sha256"] = _sha(window_event)
    binding_history.append(window_event)
    binding["recovery_window_extensions"] = binding_history
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(control_root / "active_run.json", binding)

    event = {
        "protocol": "FINFLUX_MODEL_GATEWAY_SAME_RUN_REARM_V1",
        "run_id": run_id,
        "decision": "REARM_AFTER_UPSTREAM_TRANSPORT_FAILURE",
        "requested_by": str(requested_by or "demo.operator"),
        "reason": str(reason or "")[:240],
        "previous_ledger_sha256": declared_ledger_hash,
        "preserved_request_attempt_count": int(
            ledger.get("request_attempt_count", 0) or 0
        ),
        "preserved_provider_call_count": int(
            ledger.get("provider_call_count", 0) or 0
        ),
        "preserved_total_tokens": int(ledger.get("total_tokens", 0) or 0),
        "released_stale_reservations": released,
        "recovery_window_extension": window_event,
        "rearmed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    event["event_sha256"] = _sha(event)
    history = list(ledger.get("recovery_events") or [])
    history.append(event)
    ledger["recovery_events"] = history
    ledger["status"] = "ACTIVE"
    ledger.pop("fuse_reason", None)
    ledger["reservations"] = {}
    ledger["in_flight_reserved_tokens"] = 0
    ledger["updated_at_utc"] = event["rearmed_at_utc"]
    ledger.pop("ledger_sha256", None)
    ledger["ledger_sha256"] = _sha(ledger)
    _atomic(ledger_path, ledger)
    return event


def rearm_model_gateway_expired_before_first_call(
    control_root: Path, *, run_id: str, requested_by: str, reason: str
) -> dict[str, Any]:
    """Renew the same Run when its first model call arrived after the lease.

    Matrix delivery can be delayed even though the local dispatch was
    successful.  This recovery is allowed only when the binding is expired and
    the gateway has recorded no provider call or Token.  It never creates a new
    Run and never resets an existing usage counter.
    """

    binding_path = control_root / "active_run.json"
    binding = _read_json(binding_path, "MODEL_GATEWAY_BINDING_UNREADABLE")
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError(
            "MODEL_GATEWAY_EXPIRED_REARM_REQUIRES_SAME_ACTIVE_RUN"
        )
    declared = str(binding.get("binding_sha256") or "")
    material = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if declared != _sha(material):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")
    try:
        expiry = datetime.fromisoformat(str(binding.get("expires_at_utc") or ""))
    except ValueError as exc:
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_EXPIRY_INVALID") from exc
    now = datetime.now(timezone.utc)
    if expiry > now:
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_NOT_EXPIRED")

    ledger_path = control_root / "gateway_ledger.json"
    if ledger_path.exists():
        ledger = _read_json(ledger_path, "MODEL_GATEWAY_LEDGER_UNREADABLE")
        if ledger.get("run_id") != run_id:
            raise ModelGatewayControlError("MODEL_GATEWAY_LEDGER_RUN_MISMATCH")
        if any(
            int(ledger.get(key, 0) or 0) > 0
            for key in (
                "request_attempt_count",
                "provider_call_count",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            )
        ):
            raise ModelGatewayControlError(
                "MODEL_GATEWAY_EXPIRED_REARM_REQUIRES_ZERO_USAGE"
            )

    event = {
        "protocol": "FINFLUX_MODEL_GATEWAY_EXPIRED_BEFORE_FIRST_CALL_REARM_V1",
        "run_id": run_id,
        "decision": "REARM_SAME_RUN_BEFORE_FIRST_PROVIDER_CALL",
        "requested_by": str(requested_by or "demo.operator"),
        "reason": str(reason or "")[:240],
        "previous_expires_at_utc": expiry.isoformat(),
        "rearmed_at_utc": now.isoformat(),
        "new_expires_at_utc": (now + timedelta(seconds=900)).isoformat(),
        "provider_call_count_before_rearm": 0,
        "provider_tokens_before_rearm": 0,
        "usage_counters_reset": False,
        "new_run_created": False,
    }
    event["event_sha256"] = _sha(event)
    history = list(binding.get("recovery_window_extensions") or [])
    history.append(event)
    binding["recovery_window_extensions"] = history
    binding["expires_at_utc"] = event["new_expires_at_utc"]
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return event


def rotate_model_gateway_actor_identities(
    control_root: Path,
    *,
    run_id: str,
    actor_identity_sha256: dict[str, str],
    requested_by: str,
    reason: str,
) -> dict[str, Any]:
    """Rotate volatile actor credentials while preserving the same Run budget."""

    binding_path = control_root / "active_run.json"
    binding = _read_json(binding_path, "MODEL_GATEWAY_BINDING_UNREADABLE")
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError("MODEL_GATEWAY_ROTATION_REQUIRES_SAME_ACTIVE_RUN")
    declared = str(binding.get("binding_sha256") or "")
    material = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if declared != _sha(material):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")
    current = binding.get("actor_identity_sha256") or {}
    if set(actor_identity_sha256) != set(current):
        raise ModelGatewayControlError("MODEL_GATEWAY_ROTATION_ACTOR_SET_MISMATCH")
    if not all(
        isinstance(value, str) and len(value) == 64
        for value in actor_identity_sha256.values()
    ):
        raise ModelGatewayControlError("MODEL_GATEWAY_ROTATION_IDENTITY_HASH_INVALID")
    ledger_path = control_root / "gateway_ledger.json"
    if ledger_path.exists():
        ledger = _read_json(ledger_path, "MODEL_GATEWAY_LEDGER_UNREADABLE")
        if ledger.get("run_id") != run_id:
            raise ModelGatewayControlError("MODEL_GATEWAY_LEDGER_RUN_MISMATCH")
        if ledger.get("reservations") or int(ledger.get("in_flight_reserved_tokens", 0) or 0):
            raise ModelGatewayControlError("MODEL_GATEWAY_ROTATION_IN_FLIGHT")
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "protocol": "FINFLUX_MODEL_GATEWAY_ACTOR_IDENTITY_ROTATION_V1",
        "run_id": run_id,
        "requested_by": str(requested_by or "demo.operator"),
        "reason": str(reason or "")[:240],
        "previous_identity_set_sha256": _sha(current),
        "new_identity_set_sha256": _sha(actor_identity_sha256),
        "usage_counters_reset": False,
        "new_run_created": False,
        "rotated_at_utc": now,
    }
    event["event_sha256"] = _sha(event)
    binding["actor_identity_sha256"] = dict(actor_identity_sha256)
    history = list(binding.get("actor_identity_rotations") or [])
    history.append(event)
    binding["actor_identity_rotations"] = history
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return event


def extend_model_gateway_same_run_recovery_window(
    control_root: Path, *, run_id: str, reason: str
) -> dict[str, Any]:
    """Extend an already re-armed Run without resetting usage or identities."""

    binding_path = control_root / "active_run.json"
    binding = _read_json(binding_path, "MODEL_GATEWAY_BINDING_UNREADABLE")
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError("MODEL_GATEWAY_EXTENSION_REQUIRES_SAME_ACTIVE_RUN")
    declared = str(binding.get("binding_sha256") or "")
    material = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if declared != _sha(material):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")
    history = list(binding.get("recovery_window_extensions") or [])
    if len(history) >= 3:
        raise ModelGatewayControlError("MODEL_GATEWAY_RECOVERY_EXTENSION_LIMIT_REACHED")
    ledger = _read_json(
        control_root / "gateway_ledger.json", "MODEL_GATEWAY_LEDGER_UNREADABLE"
    )
    if ledger.get("run_id") != run_id or ledger.get("status") != "ACTIVE":
        raise ModelGatewayControlError("MODEL_GATEWAY_RECOVERY_LEDGER_NOT_ACTIVE")
    calls = int(ledger.get("provider_call_count") or 0)
    tokens = int(ledger.get("total_tokens") or 0)
    if calls >= int(binding.get("max_model_calls") or 0):
        raise ModelGatewayControlError("MODEL_GATEWAY_CALL_CAP_ALREADY_REACHED")
    if tokens >= int(binding.get("provider_token_hard_cap") or 0):
        raise ModelGatewayControlError("MODEL_GATEWAY_TOKEN_CAP_ALREADY_REACHED")
    now = datetime.now(timezone.utc)
    event = {
        "run_id": run_id,
        "reason": str(reason or "SAME_RUN_RECOVERY")[:160],
        "previous_expires_at_utc": str(binding.get("expires_at_utc") or ""),
        "extended_at_utc": now.isoformat(),
        "new_expires_at_utc": (now + timedelta(seconds=900)).isoformat(),
        "token_and_call_counters_reset": False,
        "provider_calls_before_extension": calls,
        "provider_tokens_before_extension": tokens,
        "extension_sequence": len(history) + 1,
    }
    event["event_sha256"] = _sha(event)
    history.append(event)
    binding["recovery_window_extensions"] = history
    binding["expires_at_utc"] = event["new_expires_at_utc"]
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return event


def raise_model_gateway_same_run_budget(
    control_root: Path,
    *,
    run_id: str,
    new_provider_token_hard_cap: int,
    new_max_model_calls: int,
    requested_by: str,
    reason: str,
) -> dict[str, Any]:
    """Increase a live Run fuse without resetting any observed usage."""

    binding_path = control_root / "active_run.json"
    binding = _read_json(binding_path, "MODEL_GATEWAY_BINDING_UNREADABLE")
    if binding.get("run_id") != run_id or binding.get("state") != "ACTIVE":
        raise ModelGatewayControlError("MODEL_GATEWAY_BUDGET_RAISE_REQUIRES_SAME_ACTIVE_RUN")
    declared = str(binding.get("binding_sha256") or "")
    material = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if declared != _sha(material):
        raise ModelGatewayControlError("MODEL_GATEWAY_BINDING_HASH_MISMATCH")
    old_cap = int(binding.get("provider_token_hard_cap", 0) or 0)
    old_calls = int(binding.get("max_model_calls", 0) or 0)
    new_cap = int(new_provider_token_hard_cap)
    new_calls = int(new_max_model_calls)
    if new_cap < old_cap or new_calls < old_calls:
        raise ModelGatewayControlError("MODEL_GATEWAY_BUDGET_CANNOT_DECREASE_IN_ACTIVE_RUN")
    event = {
        "protocol": "FINFLUX_MODEL_GATEWAY_BUDGET_EXTENSION_V1",
        "run_id": run_id,
        "requested_by": str(requested_by or "demo.operator"),
        "reason": str(reason or "")[:240],
        "old_provider_token_hard_cap": old_cap,
        "new_provider_token_hard_cap": new_cap,
        "old_max_model_calls": old_calls,
        "new_max_model_calls": new_calls,
        "usage_counters_reset": False,
        "extended_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    event["event_sha256"] = _sha(event)
    history = list(binding.get("budget_extensions") or [])
    history.append(event)
    binding["budget_extensions"] = history
    binding["provider_token_hard_cap"] = new_cap
    binding["max_model_calls"] = new_calls
    binding.pop("binding_sha256", None)
    binding["binding_sha256"] = _sha(binding)
    _atomic(binding_path, binding)
    return event
