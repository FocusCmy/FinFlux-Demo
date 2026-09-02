from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifact-root", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
    root = Path(args.artifact_root).resolve()
    checks = []
    for kind, item in (manifest.get("files") or {}).items():
        path = (root / str(item["name"])).resolve()
        path.relative_to(root)
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        checks.append({"kind": kind, "exists": path.is_file(), "expected": item.get("sha256"), "actual": actual, "ok": actual == item.get("sha256")})
    result = {
        "protocol": "FINFLUX_RESULT_ARTIFACT_VERIFICATION_V1.0",
        "run_id": manifest.get("run_id"),
        "stage": manifest.get("stage"),
        "status": "VERIFIED" if checks and all(item["ok"] for item in checks) else "INVALID",
        "checks": checks,
    }
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "VERIFIED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
