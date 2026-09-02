from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))

from manager_routing import decide_root_route  # noqa: E402


CORPUS_ROOT = DEMO_ROOT / "data" / "evaluation_seed_cases_v1"


def legacy_schema_only(facts: dict[str, Any]) -> dict[str, str]:
    if facts.get("rights_status") != "PASS":
        return {"route": "REJECT_AT_INTAKE", "machine_recommendation": "REJECT"}
    if facts.get("evidence_status") != "VERIFIED" or not facts.get("evidence_hash_valid"):
        return {"route": "NEEDS_EVIDENCE", "machine_recommendation": "NEEDS_EVIDENCE"}
    return {"route": "CODE_ONLY_PRECHECK", "machine_recommendation": "PASS"}


def evaluate(name: str, cases: list[dict[str, Any]], runner: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    rows = []
    latencies_ms = []
    for case in cases:
        started = time.perf_counter_ns()
        actual = runner(case["manager_input_facts"])
        latencies_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        expected = case["expected"]
        rows.append(
            {
                "seed_case_id": case["seed_case_id"],
                "expected_route": expected["route"],
                "actual_route": actual["route"],
                "expected_recommendation": expected["machine_recommendation"],
                "actual_recommendation": actual["machine_recommendation"],
            }
        )
    total = len(rows)
    expected_pass = [row["expected_recommendation"] == "PASS" for row in rows]
    actual_pass = [row["actual_recommendation"] == "PASS" for row in rows]
    false_release = sum((not expected) and actual for expected, actual in zip(expected_pass, actual_pass))
    false_block = sum(expected and (not actual) for expected, actual in zip(expected_pass, actual_pass))
    recommendation_correct = sum(
        row["expected_recommendation"] == row["actual_recommendation"] for row in rows
    )
    route_correct = sum(row["expected_route"] == row["actual_route"] for row in rows)
    return {
        "system": name,
        "case_count": total,
        "recommendation_accuracy": round(recommendation_correct / total, 6),
        "route_accuracy": round(route_correct / total, 6),
        "false_release_count": false_release,
        "false_release_rate": round(false_release / max(1, sum(not value for value in expected_pass)), 6),
        "false_block_count": false_block,
        "false_block_rate": round(false_block / max(1, sum(expected_pass)), 6),
        "latency_scope": "local Python policy wall time; not production end-to-end latency",
        "latency_ms": {
            "median": round(statistics.median(latencies_ms), 6),
            "p95": round(sorted(latencies_ms)[max(0, int(len(latencies_ms) * 0.95) - 1)], 6),
            "max": round(max(latencies_ms), 6),
        },
        "actual_route_counts": dict(Counter(row["actual_route"] for row in rows)),
        "mismatches": [row for row in rows if row["expected_route"] != row["actual_route"]][:20],
    }


def main() -> None:
    corpus_path = CORPUS_ROOT / "seed_cases.jsonl"
    cases = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = {
        "protocol": "FINFLUX_OFFLINE_EVALUATION_REPORT_V0.1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "case_count": len(cases),
            "counts_by_asset": dict(Counter(item["asset_class"] for item in cases)),
            "raw_market_data_mutated": False,
            "configuration_variants_are_counterfactual": True,
        },
        "executed_systems": [
            evaluate("legacy_schema_only", cases, legacy_schema_only),
            evaluate("finflux_manager_route_policy_v0.2.0", cases, decide_root_route),
        ],
        "not_executed": [
            {
                "system": "single_agent_model_baseline",
                "status": "NOT_EXECUTED",
                "reason": "本轮禁止新增模型调用，避免伪造效果和额外Token成本。",
            },
            {
                "system": "agentteams_end_to_end",
                "status": "NOT_EXECUTED",
                "reason": "120+语料先做离线门禁评测；精选Case再进入受控真实Run。",
            },
        ],
        "limitations": [
            "expected labels由公开证据、金融用途契约和明确的反事实接入配置共同定义。",
            "FinFlux策略与标签使用同一版显式语义契约，1.0仅证明实现与该契约一致，不是独立外部有效性证明。",
            "本报告不能替代金融机构试点、人工双盲标注或生产SLA验证。",
            "本轮没有调用任何大模型，也没有声称供应商计费Token或模型效果。",
        ],
    }
    (CORPUS_ROOT / "evaluation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
