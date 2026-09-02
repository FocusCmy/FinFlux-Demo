from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEMO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = DEMO_ROOT / "data"
OUTPUT_ROOT = DATA_ROOT / "evaluation_seed_cases_v1"
EQUITY_SOURCE = DATA_ROOT / "evidence_2026_batch" / "batch_verified_2026_final.json"
FUTURES_SOURCE = (
    DATA_ROOT
    / "evidence_cross_asset"
    / "futures_if2608_20260814"
    / "akshare_cffex_20260814_normalized.csv"
)
OPTION_ROOT = DATA_ROOT / "evidence_cross_asset" / "options_510300_20260119"
OPTION_SOURCE = OPTION_ROOT / "sse_510300_adjustment_terms.csv"
OPTION_ANNOUNCEMENT = OPTION_ROOT / "sse_510300_adjustment_announcement.html"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seed(
    *,
    seed_id: str,
    asset: str,
    locator: str,
    source_path: Path,
    observed: dict[str, Any],
    candidate: dict[str, Any],
    facts: dict[str, Any],
    expected_route: str,
    expected_recommendation: str,
    truth_basis: str,
) -> dict[str, Any]:
    source_hash = sha256_file(source_path)
    record = {
        "protocol": "FINFLUX_SEED_CASE_V0.1",
        "seed_case_id": seed_id,
        "asset_class": asset,
        "source_record_locator": locator,
        "source_evidence": {
            "relative_path": str(source_path.relative_to(DEMO_ROOT)).replace("\\", "/"),
            "sha256": source_hash,
            "rights_note": (
                "公开来源仅用于竞赛POC核验；保留来源与哈希，不主张原始数据再分发权，"
                "生产使用须由机构复核授权和许可。"
            ),
        },
        "observed_facts": observed,
        "candidate_ingress_configuration": candidate,
        "manager_input_facts": facts,
        "expected": {
            "route": expected_route,
            "machine_recommendation": expected_recommendation,
        },
        "truth_basis": truth_basis,
        "counterfactual_configuration": True,
        "raw_market_data_mutated": False,
    }
    record["case_sha256"] = canonical_sha256(record)
    return record


def equity_cases() -> list[dict[str, Any]]:
    payload = json.loads(EQUITY_SOURCE.read_text(encoding="utf-8"))
    output: list[dict[str, Any]] = []
    for index, item in enumerate(payload["cases"], 1):
        common = {
            "instrument": item["instrument"],
            "company": item["company"],
            "event_date": item["event_date"],
            "corporate_action": item["metadata"]["FHcontent"],
            "unadjusted_return_pct": item["returns"]["unadjusted_event_return_pct"],
            "qfq_return_pct": item["returns"]["qfq_event_return_pct"],
            "difference_percentage_points": item["returns"]["difference_percentage_points"],
            "tencent_raw_sha256": item["tencent_raw_sha256"],
            "sina_unadjusted_sha256": item["sina_unadjusted_sha256"],
        }
        for variant, mapping, route, decision in (
            ("qfq-aligned", "qfq_with_corporate_action", "CODE_ONLY_PRECHECK", "PASS"),
            ("none-label", "NONE_drop_corporate_action", "FULL_TEAM_REVIEW", "BLOCK"),
        ):
            facts = {
                "case_id": f"EQUITY-{item['instrument']}-{item['event_date']}",
                "run_id": f"EVAL-EQ-{index:03d}-{variant}",
                "submission_id": f"SEED-EQ-{index:03d}-{variant}",
                "asset_class": "equity",
                "evidence_profile": "equity_corporate_action_return",
                "declared_downstream_use": "event_return_and_backtest",
                "rights_status": "PASS",
                "evidence_status": "VERIFIED",
                "evidence_hash_valid": True,
                "candidate_mapping": mapping,
                "required_mapping": "qfq_with_corporate_action",
                "precheck_recommendation": decision,
                "precheck_sha256": canonical_sha256({"item": index, "variant": variant}),
                "evidence_sha256": sha256_file(EQUITY_SOURCE),
                "evidence_root_hash": canonical_sha256(common),
                "impact_cny": None,
                "budget_available": True,
            }
            output.append(
                seed(
                    seed_id=f"SEED-EQUITY-{index:03d}-{variant.upper()}",
                    asset="equity",
                    locator=f"cases[{index - 1}]/{item['instrument']}/{item['event_date']}",
                    source_path=EQUITY_SOURCE,
                    observed=common,
                    candidate={"adjustment_semantics": mapping},
                    facts=facts,
                    expected_route=route,
                    expected_recommendation=decision,
                    truth_basis="同一真实公司行动日前后，未复权与前复权收益率已由原始响应复算。",
                )
            )
    return output


def futures_cases() -> list[dict[str, Any]]:
    with FUTURES_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    output: list[dict[str, Any]] = []
    multipliers = {"IF": 300.0, "IH": 300.0, "IC": 200.0, "IM": 200.0}
    for index, item in enumerate(rows, 1):
        close = float(item["close"])
        settle = float(item["settle"])
        multiplier = multipliers.get(item["variety"], 1.0)
        observed = {
            "symbol": item["symbol"],
            "date": item["date"],
            "close": close,
            "settle": settle,
            "pre_settle": float(item["pre_settle"]),
            "contract_multiplier_for_poc": multiplier,
            "absolute_close_settle_impact_cny": round(abs(close - settle) * multiplier, 6),
        }
        for variant, mapping, route, decision in (
            ("settle-aligned", "settle", "CODE_ONLY_PRECHECK", "PASS"),
            ("close-for-settlement", "close", "FULL_TEAM_REVIEW", "BLOCK"),
        ):
            facts = {
                "case_id": f"FUTURES-{item['symbol']}-{item['date']}-SETTLEMENT",
                "run_id": f"EVAL-FU-{index:03d}-{variant}",
                "submission_id": f"SEED-FU-{index:03d}-{variant}",
                "asset_class": "futures",
                "evidence_profile": "futures_settlement",
                "declared_downstream_use": "daily_settlement_pnl",
                "rights_status": "PASS",
                "evidence_status": "VERIFIED",
                "evidence_hash_valid": True,
                "candidate_mapping": mapping,
                "required_mapping": "settle",
                "precheck_recommendation": decision,
                "precheck_sha256": canonical_sha256({"item": index, "variant": variant}),
                "evidence_sha256": sha256_file(FUTURES_SOURCE),
                "evidence_root_hash": canonical_sha256(observed),
                "impact_cny": 0.0 if mapping == "settle" else observed["absolute_close_settle_impact_cny"],
                "budget_available": True,
            }
            output.append(
                seed(
                    seed_id=f"SEED-FUTURES-{index:03d}-{variant.upper()}",
                    asset="futures",
                    locator=f"row[{index}]/{item['symbol']}/{item['date']}",
                    source_path=FUTURES_SOURCE,
                    observed=observed,
                    candidate={"daily_settlement_price_field": mapping},
                    facts=facts,
                    expected_route=route,
                    expected_recommendation=decision,
                    truth_basis="同一真实中金所行情行的 close 与 settle 保持原值，仅改变拟接入字段映射。",
                )
            )
    return output


def option_cases() -> list[dict[str, Any]]:
    with OPTION_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    source_hash = sha256_file(OPTION_SOURCE)
    announcement_hash = sha256_file(OPTION_ANNOUNCEMENT)
    output: list[dict[str, Any]] = []
    variants = (
        ("adjusted-aligned", "adjusted_identity_and_unit", "adjusted_identity_and_unit", True, "VERIFIED", "PASS", "CODE_ONLY_PRECHECK", "PASS"),
        ("stale-unit", "original_unit_10000", "adjusted_identity_and_unit", True, "VERIFIED", "BLOCK", "FULL_TEAM_REVIEW", "BLOCK"),
        ("ignored-version", "identity_without_adjustment_version", "adjusted_identity_and_unit", True, "VERIFIED", "BLOCK", "FULL_TEAM_REVIEW", "BLOCK"),
        ("contract-incomplete", "partial_contract", "", True, "VERIFIED", "PENDING", "NEEDS_EVIDENCE", "NEEDS_EVIDENCE"),
        ("hash-mismatch", "adjusted_identity_and_unit", "adjusted_identity_and_unit", False, "VERIFIED", "PENDING", "NEEDS_EVIDENCE", "NEEDS_EVIDENCE"),
    )
    for index, item in enumerate(rows, 1):
        original_strike = float(item["原行权价"])
        adjusted_strike = float(item["调整后行权价"])
        original_unit = int(item["原合约单位"])
        adjusted_unit = int(item["调整后合约单位"])
        observed = {
            "underlying": "510300 ETF",
            "effective_date": "2026-01-19",
            "original_strike": original_strike,
            "adjusted_strike": adjusted_strike,
            "original_contract_unit": original_unit,
            "adjusted_contract_unit": adjusted_unit,
            "unit_difference_shares": adjusted_unit - original_unit,
            "official_announcement_sha256": announcement_hash,
        }
        for (
            variant,
            mapping,
            required,
            hash_valid,
            evidence_status,
            precheck,
            route,
            recommendation,
        ) in variants:
            facts = {
                "case_id": f"OPTION-510300-20260119-{index:02d}",
                "run_id": f"EVAL-OP-{index:03d}-{variant}",
                "submission_id": f"SEED-OP-{index:03d}-{variant}",
                "asset_class": "option",
                "evidence_profile": "option_adjusted_contract_identity",
                "declared_downstream_use": "covered_position_and_exposure",
                "rights_status": "PASS",
                "evidence_status": evidence_status,
                "evidence_hash_valid": hash_valid,
                "candidate_mapping": mapping,
                "required_mapping": required,
                "precheck_recommendation": precheck,
                "precheck_sha256": canonical_sha256({"item": index, "variant": variant}),
                "evidence_sha256": source_hash,
                "evidence_root_hash": canonical_sha256(observed),
                "impact_cny": None,
                "budget_available": True,
            }
            output.append(
                seed(
                    seed_id=f"SEED-OPTION-{index:03d}-{variant.upper()}",
                    asset="option",
                    locator=f"adjustment_terms[{index - 1}]/strike={original_strike}",
                    source_path=OPTION_SOURCE,
                    observed=observed,
                    candidate={
                        "contract_identity_mapping": mapping,
                        "candidate_evidence_hash_matches": hash_valid,
                    },
                    facts=facts,
                    expected_route=route,
                    expected_recommendation=recommendation,
                    truth_basis="上交所真实调整公告与17组调整条款保持原值，仅改变拟接入身份/单位配置或完整性声明。",
                )
            )
    return output


def main() -> None:
    cases = equity_cases() + futures_cases() + option_cases()
    if len(cases) < 120:
        raise SystemExit(f"seed case count {len(cases)} is below 120")
    if any(not item["counterfactual_configuration"] for item in cases):
        raise SystemExit("all variants must declare counterfactual configuration")
    if any(item["raw_market_data_mutated"] for item in cases):
        raise SystemExit("raw market data mutation is forbidden")
    ids = [item["seed_case_id"] for item in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate seed_case_id")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    jsonl = OUTPUT_ROOT / "seed_cases.jsonl"
    jsonl.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in cases),
        encoding="utf-8",
    )
    manifest = {
        "protocol": "FINFLUX_SEED_CORPUS_MANIFEST_V0.1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "counts_by_asset": dict(Counter(item["asset_class"] for item in cases)),
        "counts_by_expected_route": dict(
            Counter(item["expected"]["route"] for item in cases)
        ),
        "raw_market_data_mutated": False,
        "configuration_variants_are_counterfactual": True,
        "source_files": [
            {"path": str(path.relative_to(DEMO_ROOT)).replace("\\", "/"), "sha256": sha256_file(path)}
            for path in (EQUITY_SOURCE, FUTURES_SOURCE, OPTION_SOURCE, OPTION_ANNOUNCEMENT)
        ],
        "seed_cases_jsonl_sha256": sha256_file(jsonl),
        "limitations": [
            "这是公开证据驱动的离线种子语料，不等同金融机构生产试点样本。",
            "反事实对象是拟接入配置，不是行情、公告或研报原始值。",
            "许可和生产使用边界仍需数据所有权人及机构法务复核。",
        ],
    }
    (OUTPUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
