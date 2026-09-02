from __future__ import annotations

import argparse
import json
from pathlib import Path

from finchange_gate_core import ASSETS, generate_datapass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate deterministic FinChange Gate DataPass drafts."
    )
    parser.add_argument(
        "--asset",
        choices=(*ASSETS, "all"),
        default="all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    selected = ASSETS if args.asset == "all" else (args.asset,)
    result = {}
    for asset in selected:
        payload = generate_datapass(asset)
        path = args.output_dir / f"datapass_{asset}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result[asset] = payload
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
