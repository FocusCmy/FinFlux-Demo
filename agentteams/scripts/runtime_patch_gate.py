from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping


PROTOCOL = "FINFLUX_RUNTIME_PATCH_GATE_V1"
DEFAULT_WORKSPACE = Path("/root/.qwenpaw")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_pid1_environment(path: Path = Path("/proc/1/environ")) -> dict[str, str]:
    try:
        entries = path.read_bytes().split(b"\0")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if not entry or b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        result[key.decode("utf-8", errors="strict")] = value.decode(
            "utf-8", errors="strict"
        ).strip()
    return result


def read_pid1_cwd(path: Path = Path("/proc/1/cwd")) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _normalise_explicit_workspace(
    value: str, exists: Callable[[Path], bool]
) -> Path:
    base = Path(value).expanduser()
    if base.name in {".qwenpaw", ".copaw"}:
        return base
    for child in (base / ".qwenpaw", base / ".copaw"):
        if exists(child):
            return child
    return base


def resolve_runtime_workspace(
    *,
    environ: Mapping[str, str] | None = None,
    pid1_environ: Mapping[str, str] | None = None,
    pid1_cwd: Path | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> tuple[Path, str]:
    """Resolve the stateful plugin root used by PID1, with provenance.

    ``docker exec`` inherits a different working directory/environment on
    several AgentTeams images.  The runtime authority is PID1.  Explicit
    QwenPaw/CoPaw variables win; otherwise an existing hidden runtime folder
    below ``/proc/1/cwd`` is selected.  Falling back is explicit and auditable.
    """

    environ = dict(os.environ if environ is None else environ)
    pid1_environ = dict(
        read_pid1_environment() if pid1_environ is None else pid1_environ
    )
    pid1_cwd = read_pid1_cwd() if pid1_cwd is None else Path(pid1_cwd)
    exists = (lambda candidate: candidate.exists()) if exists is None else exists
    for name in ("QWENPAW_WORKING_DIR", "COPAW_WORKING_DIR"):
        value = str(pid1_environ.get(name, "")).strip()
        if value:
            return _normalise_explicit_workspace(value, exists), f"PID1_ENV:{name}"
    if pid1_cwd is not None:
        if pid1_cwd.name in {".qwenpaw", ".copaw"}:
            return pid1_cwd, "PID1_CWD:RUNTIME_ROOT"
        for child_name in (".qwenpaw", ".copaw"):
            candidate = pid1_cwd / child_name
            if exists(candidate):
                return candidate, f"PID1_CWD:{child_name}"
    # ``docker exec`` environment is only a final compatibility fallback: it
    # must never override PID1's persisted runtime root.
    for name in ("QWENPAW_WORKING_DIR", "COPAW_WORKING_DIR"):
        value = str(environ.get(name, "")).strip()
        if value:
            return _normalise_explicit_workspace(value, exists), f"EXEC_ENV:{name}"
    return DEFAULT_WORKSPACE, "EXPLICIT_DEFAULT:/root/.qwenpaw"


def patch_targets(kind: str, workspace: Path, root: Path = Path("/")) -> list[Path]:
    if kind == "teamharness":
        image = root / "opt/agentteams/qwenpaw-builtin/plugins/teamharness/teamharness/mcp/server.py"
        stateful = workspace / "plugins/teamharness/teamharness/mcp/server.py"
    elif kind == "manager":
        image = root / "opt/agentteams/plugins/agentteams-manager-tools/plugin.py"
        stateful = workspace / "plugins/agentteams-manager-tools/plugin.py"
    else:
        raise ValueError(f"unknown runtime patch kind: {kind}")
    return [image, stateful]


def install_patch(
    kind: str,
    source: Path,
    expected: str,
    *,
    workspace: Path,
    workspace_source: str,
    pid1_cwd: Path | None,
    root: Path = Path("/"),
) -> dict[str, object]:
    if sha256(source) != expected:
        raise RuntimeError("staged source SHA256 mismatch")
    readback: dict[str, str] = {}
    for target in patch_targets(kind, workspace, root):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        actual = sha256(target)
        if actual != expected:
            raise RuntimeError(f"installed target SHA256 mismatch: {target}")
        readback[str(target)] = actual
    return _receipt(
        "INSTALL_VERIFIED",
        kind,
        expected,
        workspace,
        workspace_source,
        pid1_cwd,
        readback,
    )


def readback_patch(
    kind: str,
    expected: str,
    *,
    workspace: Path,
    workspace_source: str,
    pid1_cwd: Path | None,
    root: Path = Path("/"),
) -> dict[str, object]:
    readback = {
        str(path): sha256(path) for path in patch_targets(kind, workspace, root)
    }
    if not readback or any(value != expected for value in readback.values()):
        raise RuntimeError(
            "post-restart runtime patch readback mismatch: "
            + json.dumps(readback, sort_keys=True)
        )
    return _receipt(
        "POST_RESTART_READBACK_VERIFIED",
        kind,
        expected,
        workspace,
        workspace_source,
        pid1_cwd,
        readback,
    )


def _receipt(
    status: str,
    kind: str,
    expected: str,
    workspace: Path,
    workspace_source: str,
    pid1_cwd: Path | None,
    targets: Mapping[str, str],
) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "status": status,
        "kind": kind,
        "expected_sha256": expected,
        "workspace": str(workspace),
        "workspace_source": workspace_source,
        "pid1_cwd": str(pid1_cwd) if pid1_cwd is not None else None,
        "targets": dict(targets),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("install", "readback"))
    parser.add_argument("kind", choices=("teamharness", "manager"))
    parser.add_argument("--source")
    parser.add_argument("--expected", required=True)
    parser.add_argument("--gate-sha256", required=True)
    args = parser.parse_args()
    if sha256(Path(__file__)) != args.gate_sha256:
        raise SystemExit("runtime patch gate script SHA256 mismatch")
    pid1_cwd = read_pid1_cwd()
    workspace, source = resolve_runtime_workspace(pid1_cwd=pid1_cwd)
    try:
        if args.operation == "install":
            if not args.source:
                raise RuntimeError("install requires --source")
            receipt = install_patch(
                args.kind,
                Path(args.source),
                args.expected,
                workspace=workspace,
                workspace_source=source,
                pid1_cwd=pid1_cwd,
            )
        else:
            receipt = readback_patch(
                args.kind,
                args.expected,
                workspace=workspace,
                workspace_source=source,
                pid1_cwd=pid1_cwd,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
