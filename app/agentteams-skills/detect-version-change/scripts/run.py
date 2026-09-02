from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT))

from change_control import detect_version_change  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
args = parser.parse_args()
payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
print(
    json.dumps(
        detect_version_change(
            payload["baseline_submission"], payload["candidate_submission"]
        ),
        ensure_ascii=False,
        indent=2,
    )
)

