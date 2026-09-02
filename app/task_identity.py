from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


PROTOCOL = "FINFLUX_BOUNDED_TASK_IDENTITIES_V1"
_RUN_ID = re.compile(
    r"^RUN-LIVE-(?P<stamp>\d{14})-(?P<nonce>[0-9a-f]{6})$"
)
_SAFE_CASE_ID = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,180}$")
_SAFE_ROLE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")


class TaskIdentityError(ValueError):
    pass


def run_task_scope(case_id: str, run_id: str) -> str:
    case_id = str(case_id or "").strip()
    run_id = str(run_id or "").strip()
    if not _SAFE_CASE_ID.fullmatch(case_id):
        raise TaskIdentityError("case_id is not a bounded task-safe identifier")
    match = _RUN_ID.fullmatch(run_id)
    if not match:
        raise TaskIdentityError("run_id is not a FINFLUX live Run identifier")
    # The timestamp alone is not a namespace: two accepted submissions can be
    # created during the same second.  Bind the controller-issued nonce too so
    # every exact task directory is injectively derived from the complete Run
    # identity.  This also lets collection fail closed without directory
    # globs or "latest task" heuristics.
    return (
        f"task-{case_id}-LIVE-{match.group('stamp')}-"
        f"{match.group('nonce')}"
    )


def build_role_task_ids(
    case_id: str,
    run_id: str,
    selected_workers: Iterable[str],
) -> dict[str, Any]:
    scope = run_task_scope(case_id, run_id)
    workers = [str(item or "").strip() for item in selected_workers]
    if not workers or len(workers) != len(set(workers)):
        raise TaskIdentityError("selected_workers must be non-empty and unique")
    task_ids: dict[str, str] = {}
    for role in workers:
        if not _SAFE_ROLE.fullmatch(role):
            raise TaskIdentityError(f"invalid worker role: {role}")
        task_id = f"{scope}-{role}"
        if not task_id.startswith(scope + "-"):
            raise TaskIdentityError("task id escaped its Run namespace")
        task_ids[role] = task_id
    return {
        "protocol": PROTOCOL,
        "case_id": case_id,
        "run_id": run_id,
        "task_scope": scope,
        "task_ids": task_ids,
    }


def validate_role_task_ids(
    payload: Mapping[str, Any],
    *,
    case_id: str,
    run_id: str,
    selected_workers: Iterable[str],
) -> dict[str, Any]:
    """Validate a transported task-identity map against local derivation.

    Task identifiers are not free-form model output.  The application derives
    them once, transports the complete role map, and accepts artifacts only
    from those exact directories.  Returning the locally derived object keeps
    callers from accidentally trusting additional keys in an untrusted map.
    """

    expected = build_role_task_ids(case_id, run_id, selected_workers)
    observed = {
        "protocol": str(payload.get("protocol") or ""),
        "case_id": str(payload.get("case_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "task_scope": str(payload.get("task_scope") or ""),
        "task_ids": {
            str(role): str(task_id)
            for role, task_id in dict(payload.get("task_ids") or {}).items()
        },
    }
    if observed != expected:
        raise TaskIdentityError("transported role task identities do not match derivation")
    return expected


def task_id_for_role(
    payload: Mapping[str, Any],
    role: str,
    *,
    case_id: str,
    run_id: str,
    selected_workers: Iterable[str],
) -> str:
    validated = validate_role_task_ids(
        payload,
        case_id=case_id,
        run_id=run_id,
        selected_workers=selected_workers,
    )
    try:
        return str(validated["task_ids"][role])
    except KeyError as exc:
        raise TaskIdentityError(f"role has no bounded task id: {role}") from exc
