from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT))
from result_composer_agent import compact_result_context  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", required=True)
    parser.add_argument("--submission-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run = json.loads(Path(args.run_json).read_text(encoding="utf-8-sig"))
    submission = json.loads(Path(args.submission_json).read_text(encoding="utf-8-sig"))
    context = compact_result_context(run, submission)
    raw = json.dumps({"run": run, "submission": submission}, ensure_ascii=False, sort_keys=True)
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    result = {
        "protocol": "FINFLUX_RESULT_CONTEXT_V1.0",
        "context": context,
        "context_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_chars": len(raw),
        "compact_chars": len(encoded.decode("utf-8")),
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in result if k != "context"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
