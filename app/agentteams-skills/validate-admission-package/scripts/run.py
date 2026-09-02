from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT))

from finchange_gate_core import (  # noqa: E402
    ASSETS,
    SCENARIOS,
    validate_admission_package,
)


parser = argparse.ArgumentParser()
parser.add_argument("--asset", required=True, choices=ASSETS)
parser.add_argument("--scenario", default="blocked", choices=SCENARIOS)
args = parser.parse_args()
print(
    json.dumps(
        validate_admission_package(args.asset, args.scenario),
        ensure_ascii=False,
        indent=2,
    )
)
