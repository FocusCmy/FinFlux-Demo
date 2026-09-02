from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DEMO_ROOT))

from finchange_gate_core import ASSETS, verify_evidence_bundle  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument("--asset", required=True, choices=ASSETS)
args = parser.parse_args()
print(json.dumps(verify_evidence_bundle(args.asset), ensure_ascii=False, indent=2))
