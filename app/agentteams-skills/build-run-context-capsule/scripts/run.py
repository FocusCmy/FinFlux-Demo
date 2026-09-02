from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(APP_ROOT))

from context_capsule import build_run_context_capsule  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    source = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    capsule, handle = build_run_context_capsule(
        case_id=source["case_id"],
        run_id=source["run_id"],
        payload=source["payload"],
        selected_workers=source["selected_workers"],
        execution_policy_id=source["execution_policy_id"],
        root_route_decision_handle=source["root_route_decision_handle"],
        local_root=Path(args.root),
    )
    result = {"capsule": capsule, "handle": handle, "provider_tokens": 0}
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(handle, ensure_ascii=False))


if __name__ == "__main__":
    main()
