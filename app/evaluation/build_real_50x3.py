from __future__ import annotations

"""Build FinFlux's source-bound 50/50/50 evidence corpus.

This program downloads real public-source observations and freezes the adapter
outputs before any semantic decision is made.  It does not call a language
model, generate market values, or modify source observations.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent
DEFAULT_OUTPUT = APP_ROOT / "data" / "real_50x3_v1"
CORPUS_PROTOCOL = "FINFLUX_SOURCE_BOUND_CORPUS_V1"
REPORT_PROTOCOL = "FINFLUX_SOURCE_BOUND_EVALUATION_V1"
MANIFEST_VERSION = "1.0.0"
TARGET_PER_ASSET = 50

CFFEX_DATES = ("20260813", "20260814")
CFFEX_URL = "http://www.cffex.com.cn/sj/hqsj/rtj/{year_month}/{day}/{date}_1.csv"
EQUITY_DATE = "20251231"
EQUITY_SOURCE_URL = "https://data.eastmoney.com/yjfp/"
FUND_SOURCE_URL = "https://fund.eastmoney.com/fund.html"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        value = value.strip()
        return None if value in {"", "--", "-", "nan", "NaT"} else value
    return value


def number(value: Any) -> float | int | None:
    value = clean(value)
    if value is None:
        return None
    try:
        result = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return int(result) if result.is_integer() else result


def freeze_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return sha256_bytes(path.read_bytes())


def source_descriptor(
    *, provider: str, source_url: str, evidence_path: Path, captured_at: str,
    adapter: str, rights_state: str = "REVIEW_REQUIRED",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "source_url": source_url,
        "captured_at_utc": captured_at,
        "adapter": adapter,
        "artifact_path": evidence_path.relative_to(PROJECT_ROOT).as_posix(),
        "artifact_sha256": sha256_bytes(evidence_path.read_bytes()),
        "rights_state": rights_state,
        "rights_note": (
            "Publicly reachable source used only as a locally frozen POC evidence "
            "reference; redistribution and production use require provider review."
        ),
    }


def fetch_futures(output: Path, captured_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for trade_date in CFFEX_DATES:
        url = CFFEX_URL.format(
            year_month=trade_date[:6], day=trade_date[6:], date=trade_date
        )
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        raw_path = output / "raw" / "futures" / f"cffex_{trade_date}_raw.csv"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(response.content)
        source = source_descriptor(
            provider="China Financial Futures Exchange",
            source_url=url,
            evidence_path=raw_path,
            captured_at=captured_at,
            adapter="direct-http-byte-snapshot",
        )
        sources.append(source)
        frame = pd.read_csv(raw_path, encoding="gb18030")
        for _, row in frame.iterrows():
            instrument = str(clean(row.get("合约代码")) or "").upper()
            if not re.fullmatch(r"[A-Z]{1,2}\d{4}", instrument):
                continue
            record = {
                "asset_class": "futures",
                "instrument": instrument,
                "observation_date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                "business_purpose": "daily_settlement_pnl",
                "candidate_mapping": "settle",
                "open": number(row.get("今开盘")),
                "high": number(row.get("最高价")),
                "low": number(row.get("最低价")),
                "close": number(row.get("今收盘")),
                "settle": number(row.get("今结算")),
                "pre_settle": number(row.get("前结算")),
                "volume": number(row.get("成交量")),
                "turnover": number(row.get("成交金额")),
                "open_interest": number(row.get("持仓量")),
                "source_artifact_sha256": source["artifact_sha256"],
                "source_artifact_path": source["artifact_path"],
                "source_row_key": instrument,
            }
            candidates.append(record)
    candidates.sort(key=lambda item: (item["observation_date"], item["instrument"]))
    if len(candidates) < TARGET_PER_ASSET:
        raise RuntimeError(f"CFFEX returned only {len(candidates)} non-option futures rows")
    return candidates[:TARGET_PER_ASSET], sources


def _akshare_version() -> tuple[Any, str]:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("akshare is required; install requirements-optional.txt") from exc
    return ak, str(getattr(ak, "__version__", "unknown"))


def fetch_equity(output: Path, captured_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ak, version = _akshare_version()
    frame = ak.stock_fhps_em(date=EQUITY_DATE)
    raw_path = output / "raw" / "equity" / f"akshare_stock_fhps_em_{EQUITY_DATE}.csv"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(raw_path, index=False, encoding="utf-8-sig")
    source = source_descriptor(
        provider="EastMoney via AKShare",
        source_url=EQUITY_SOURCE_URL,
        evidence_path=raw_path,
        captured_at=captured_at,
        adapter=f"akshare.stock_fhps_em@{version}; adapter-output snapshot",
    )
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        code = str(clean(row.get("代码")) or "").zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        record = {
            "asset_class": "equity",
            "instrument": code,
            "instrument_name": clean(row.get("名称")),
            "observation_date": clean(row.get("最新公告日期")) or clean(row.get("预案公告日")),
            "business_purpose": "corporate_action_admission",
            "candidate_mapping": "declared_corporate_action_terms",
            "plan_status": clean(row.get("方案进度")),
            "proposal_date": clean(row.get("预案公告日")),
            "record_date": clean(row.get("股权登记日")),
            "ex_date": clean(row.get("除权除息日")),
            "cash_dividend_per_10": number(row.get("现金分红-现金分红比例")),
            "stock_or_transfer_per_10": number(row.get("送转股份-送转总比例")),
            "source_artifact_sha256": source["artifact_sha256"],
            "source_artifact_path": source["artifact_path"],
            "source_row_key": code,
        }
        rows.append(record)
    rows.sort(key=lambda item: item["instrument"])
    if len(rows) < TARGET_PER_ASSET:
        raise RuntimeError(f"EastMoney dividend adapter returned only {len(rows)} equity rows")
    return rows[:TARGET_PER_ASSET], [source]


def fetch_fund(output: Path, captured_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ak, version = _akshare_version()
    frame = ak.fund_open_fund_daily_em()
    unit_columns = [name for name in frame.columns if str(name).endswith("-单位净值")]
    cumulative_columns = [name for name in frame.columns if str(name).endswith("-累计净值")]
    if len(unit_columns) < 2 or len(cumulative_columns) < 2:
        raise RuntimeError("fund adapter did not return current and prior NAV columns")
    unit_columns.sort(reverse=True)
    cumulative_columns.sort(reverse=True)
    current_unit, prior_unit = unit_columns[:2]
    current_cumulative, prior_cumulative = cumulative_columns[:2]
    nav_date = current_unit.removesuffix("-单位净值")
    prior_date = prior_unit.removesuffix("-单位净值")

    raw_path = output / "raw" / "fund" / f"akshare_fund_open_fund_daily_em_{nav_date}.csv"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(raw_path, index=False, encoding="utf-8-sig")
    source = source_descriptor(
        provider="EastMoney via AKShare",
        source_url=FUND_SOURCE_URL,
        evidence_path=raw_path,
        captured_at=captured_at,
        adapter=f"akshare.fund_open_fund_daily_em@{version}; adapter-output snapshot",
    )
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        code = str(clean(row.get("基金代码")) or "").zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        record = {
            "asset_class": "fund",
            "instrument": code,
            "instrument_name": clean(row.get("基金简称")),
            "observation_date": nav_date,
            "business_purpose": "holding_valuation",
            "candidate_mapping": "unit_nav",
            "unit_nav": number(row.get(current_unit)),
            "cumulative_nav": number(row.get(current_cumulative)),
            "prior_nav_date": prior_date,
            "prior_unit_nav": number(row.get(prior_unit)),
            "prior_cumulative_nav": number(row.get(prior_cumulative)),
            "daily_growth_value": number(row.get("日增长值")),
            "daily_growth_rate_pct": number(row.get("日增长率")),
            "subscription_status": clean(row.get("申购状态")),
            "redemption_status": clean(row.get("赎回状态")),
            "fee_rate": clean(row.get("手续费")),
            "source_artifact_sha256": source["artifact_sha256"],
            "source_artifact_path": source["artifact_path"],
            "source_row_key": code,
        }
        rows.append(record)
    rows.sort(key=lambda item: item["instrument"])
    if len(rows) < TARGET_PER_ASSET:
        raise RuntimeError(f"EastMoney fund adapter returned only {len(rows)} fund rows")
    return rows[:TARGET_PER_ASSET], [source]


def precheck(record: dict[str, Any]) -> dict[str, Any]:
    asset = record["asset_class"]
    missing: list[str] = []
    conflicts: list[str] = []
    if asset == "futures":
        required = ("instrument", "observation_date", "settle", "pre_settle")
        if record.get("candidate_mapping") != "settle":
            conflicts.append("SETTLEMENT_PURPOSE_REQUIRES_SETTLE")
    elif asset == "equity":
        required = ("instrument", "plan_status", "proposal_date")
        if record.get("plan_status") == "实施分配":
            required += ("record_date", "ex_date")
    elif asset == "fund":
        required = ("instrument", "observation_date", "unit_nav")
        if record.get("candidate_mapping") != "unit_nav":
            conflicts.append("HOLDING_VALUATION_REQUIRES_UNIT_NAV")
    else:
        raise RuntimeError(f"unsupported asset class: {asset}")
    missing.extend(field for field in required if record.get(field) is None)
    if conflicts:
        decision = "BLOCK"
        reason_codes = conflicts
    elif missing:
        decision = "WAIT"
        reason_codes = [f"MISSING_{field.upper()}" for field in missing]
    else:
        decision = "PASS"
        reason_codes = ["SOURCE_FIELDS_AND_DECLARED_PURPOSE_ALIGNED"]
    return {
        "record_id": record["record_id"],
        "asset_class": asset,
        "instrument": record["instrument"],
        "decision": decision,
        "reason_codes": reason_codes,
        "required_fields": list(required),
        "missing_fields": missing,
        "model_calls": 0,
        "provider_tokens": 0,
        "policy_version": "finflux-deterministic-source-precheck@1.0.0",
        "input_sha256": record["record_sha256"],
    }


def build(output: Path) -> dict[str, Any]:
    started = time.perf_counter()
    captured_at = utc_now()
    output.mkdir(parents=True, exist_ok=True)

    datasets: list[tuple[str, list[dict[str, Any]]]] = []
    sources: list[dict[str, Any]] = []
    for asset, loader in (
        ("futures", fetch_futures),
        ("equity", fetch_equity),
        ("fund", fetch_fund),
    ):
        rows, asset_sources = loader(output, captured_at)
        datasets.append((asset, rows))
        sources.extend(asset_sources)

    records: list[dict[str, Any]] = []
    for asset, rows in datasets:
        for index, row in enumerate(rows, start=1):
            row["record_id"] = f"{asset.upper()}-{index:03d}"
            row["source_rights_state"] = "REVIEW_REQUIRED"
            row["record_sha256"] = canonical_sha256(row)
            records.append(row)
        columns = sorted({key for row in rows for key in row})
        freeze_csv(output / "normalized" / f"{asset}_50.csv", rows, columns)

    if Counter(item["asset_class"] for item in records) != {
        "futures": 50, "equity": 50, "fund": 50
    }:
        raise RuntimeError("corpus does not contain exactly 50 records per asset")
    if len({item["record_sha256"] for item in records}) != 150:
        raise RuntimeError("record hashes are not unique; source-row selection must be audited")

    results = [precheck(item) for item in records]
    result_hashes: list[str] = []
    for result in results:
        result["result_sha256"] = canonical_sha256(result)
        result_hashes.append(result["result_sha256"])

    manifest_unsigned = {
        "protocol": CORPUS_PROTOCOL,
        "schema_version": MANIFEST_VERSION,
        "created_at_utc": captured_at,
        "record_basis": "150_DISTINCT_SOURCE_ROWS",
        "model_generated_records": 0,
        "raw_market_data_mutated": False,
        "selection_policy": {
            "futures": "first 50 lexicographically sorted non-option contract rows across 2026-08-13 and 2026-08-14",
            "equity": "first 50 security-code-sorted rows from AKShare stock_fhps_em(date=20251231)",
            "fund": "first 50 fund-code-sorted rows from AKShare fund_open_fund_daily_em current snapshot",
        },
        "counts_by_asset": dict(Counter(item["asset_class"] for item in records)),
        "sources": sources,
        "records": records,
        "rights_boundary": (
            "Source accessibility is not a redistribution licence. All three source "
            "families remain REVIEW_REQUIRED for production use."
        ),
    }
    manifest_unsigned["records_merkle_sha256"] = canonical_sha256(
        [item["record_sha256"] for item in records]
    )
    manifest = dict(manifest_unsigned)
    manifest["manifest_sha256"] = canonical_sha256(manifest_unsigned)
    write_json(output / "manifest.json", manifest)
    write_json(output / "precheck_results.json", results)

    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    decisions = Counter(item["decision"] for item in results)
    report_unsigned = {
        "protocol": REPORT_PROTOCOL,
        "schema_version": MANIFEST_VERSION,
        "created_at_utc": utc_now(),
        "corpus": {
            "case_count": len(records),
            "counts_by_asset": manifest["counts_by_asset"],
            "record_basis": manifest["record_basis"],
            "model_generated_records": 0,
            "raw_market_data_mutated": False,
            "manifest_sha256": manifest["manifest_sha256"],
            "records_merkle_sha256": manifest["records_merkle_sha256"],
        },
        "executed_pipeline": {
            "system": "deterministic_source_precheck_v1",
            "status": "EXECUTED",
            "processed_count": len(results),
            "decision_counts": {key: decisions.get(key, 0) for key in ("PASS", "WAIT", "BLOCK")},
            "duration_ms": duration_ms,
            "model_calls": 0,
            "provider_tokens": 0,
            "result_merkle_sha256": canonical_sha256(result_hashes),
        },
        "source_artifacts": sources,
        "not_executed": [
            {
                "system": "AgentTeams representative-case evaluation",
                "status": "NOT_EXECUTED",
                "reason": "This phase freezes and prechecks 150 real rows; representative model Runs are a separate evidence stage.",
            },
            {
                "system": "financial-institution pilot metrics",
                "status": "NOT_EXECUTED",
                "reason": "No institution-labelled ground truth is available; false-release and false-block rates are therefore not reported.",
            },
        ],
        "limitations": [
            "Rows are public-source observations, not institution-labelled production incidents.",
            "AKShare CSV files are frozen adapter-output snapshots, not raw EastMoney HTTP bodies.",
            "Public accessibility does not establish redistribution rights; production rights review remains required.",
            "PASS means deterministic source fields align with the declared purpose; it is not Human approval.",
        ],
    }
    report = dict(report_unsigned)
    report["report_sha256"] = canonical_sha256(report_unsigned)
    write_json(output / "evaluation_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build(args.output.resolve())
    print(json.dumps({
        "status": "PASS",
        "report": str((args.output.resolve() / "evaluation_report.json")),
        "counts": report["corpus"]["counts_by_asset"],
        "decisions": report["executed_pipeline"]["decision_counts"],
        "report_sha256": report["report_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
