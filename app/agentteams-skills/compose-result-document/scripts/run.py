from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT))
from result_composer_agent import ResultComposerAgent  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", required=True)
    parser.add_argument("--submission-json", required=True)
    parser.add_argument("--stage", choices=("preview", "final"), required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    run = json.loads(Path(args.run_json).read_text(encoding="utf-8-sig"))
    submission = json.loads(Path(args.submission_json).read_text(encoding="utf-8-sig"))
    gate = str((run.get("human_gate") or {}).get("state", ""))
    if args.stage == "preview" and gate != "AWAITING_HUMAN":
        raise SystemExit("preview requires AWAITING_HUMAN")
    if args.stage == "final" and gate not in {"APPROVED", "REJECTED", "RETURNED"}:
        raise SystemExit("final requires a Human disposition")
    result = ResultComposerAgent(Path(args.output_root)).compose(run, submission, stage=args.stage)
    print(json.dumps({
        "agent_id": result["agent_id"],
        "stage": result["stage"],
        "strategy": result["strategy"],
        "verification": result["verification"],
        "paths": result["artifacts"]["paths"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
