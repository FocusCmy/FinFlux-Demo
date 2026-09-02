from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT))

from change_control import resolve_downstream_lineage  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
args = parser.parse_args()
payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
print(
    json.dumps(
        resolve_downstream_lineage(
            payload["change_set"], payload["downstream_tasks"]
        ),
        ensure_ascii=False,
        indent=2,
    )
)

