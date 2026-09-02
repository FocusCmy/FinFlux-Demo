from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def load_worker_module(entrypoint: Path):
    app_root = str(entrypoint.parent)
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    spec = importlib.util.spec_from_file_location(
        "finflux_source_bounded_worker_task",
        entrypoint,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Worker entrypoint: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: freeze_runtime_skill_manifests.py <app-root>")

    app_root = Path(sys.argv[1]).resolve()
    entrypoint = app_root / "bounded_worker_task.py"
    manifests_root = app_root / "runtime-skill-manifests"
    if not entrypoint.is_file() or not manifests_root.is_dir():
        raise RuntimeError("FinFlux Worker source or runtime Skill manifests are missing")

    module = load_worker_module(entrypoint)
    entrypoint_sha256 = sha256_bytes(entrypoint.read_bytes())
    written = 0

    for manifest_path in sorted(manifests_root.glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest.pop("manifest_sha256", None)
        manifest["entrypoint"] = {
            "path": "bounded_worker_task.py",
            "sha256": entrypoint_sha256,
        }

        for skill in manifest.get("skills") or []:
            callable_name = str(skill.get("callable") or "")
            callable_obj = getattr(module, callable_name, None)
            if not callable(callable_obj):
                raise RuntimeError(
                    f"runtime Skill callable is unavailable: {manifest_path.name}:{callable_name}"
                )
            instruction_path = app_root / str(skill.get("instruction_path") or "")
            if not instruction_path.is_file():
                raise RuntimeError(
                    f"runtime Skill instruction is missing: {instruction_path}"
                )
            skill["instruction_sha256"] = sha256_bytes(instruction_path.read_bytes())
            skill["callable_sha256"] = sha256_bytes(
                inspect.getsource(callable_obj).encode("utf-8")
            )

        manifest["manifest_sha256"] = canonical_sha256(manifest)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written += 1

    print(
        f"Frozen {written} source runtime Skill manifests against "
        f"bounded_worker_task.py@{entrypoint_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
