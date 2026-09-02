from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import DATA_ROOT, ResearchDataStore
from .providers import EastMoneyReportProvider, WorldBankProvider


DEFAULT_STOCKS = ["000001", "600519", "300750", "601318", "600030"]
DEFAULT_INDICATORS = ["NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG"]


def collect_real_records(root: Path = DATA_ROOT) -> dict[str, object]:
    store = ResearchDataStore(root)
    eastmoney = EastMoneyReportProvider(store)
    worldbank = WorldBankProvider(store)

    eastmoney_items = eastmoney.fetch_stock_reports(
        DEFAULT_STOCKS,
        per_code=4,
        begin_time="2026-01-01",
        end_time="2026-08-27",
    )
    worldbank_documents = worldbank.fetch_documents(
        query="financial stability",
        count=10,
        start_date="2025-01-01",
    )
    worldbank_indicators = worldbank.fetch_indicators(
        country="CHN",
        indicators=DEFAULT_INDICATORS,
        observations_per_indicator=5,
        start_year=2018,
        end_year=2026,
    )
    items = eastmoney_items + worldbank_documents + worldbank_indicators
    if not 30 <= len(items) <= 40:
        raise RuntimeError(f"real record target not met: {len(items)}")
    result = store.upsert_items(items)
    return {
        "requested_real_records": len(items),
        "eastmoney_report_records": len(eastmoney_items),
        "worldbank_document_records": len(worldbank_documents),
        "worldbank_indicator_records": len(worldbank_indicators),
        **result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and cache real FinFlux Research Data Layer records."
    )
    parser.add_argument("--output-root", type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    print(json.dumps(collect_real_records(args.output_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

