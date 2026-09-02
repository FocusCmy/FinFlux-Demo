from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(APP_ROOT))

from context_capsule import load_role_context_slice  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capsule-ref", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = load_role_context_slice(
        args.capsule_ref,
        args.role,
        case_id=args.case_id,
        run_id=args.run_id,
        root=Path(args.root),
    )
    result = {"payload": payload, "provider_tokens": 0}
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "context_capsule_sha256": payload["context_capsule_sha256"],
        "context_slice_sha256": payload["context_slice_sha256"],
        "provider_tokens": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
