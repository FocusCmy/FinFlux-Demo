"""Source-verified, zero-model evaluation metric Skill.

This module deliberately separates two evidence layers:

* 150 source-bound market rows measure deterministic intake coverage only;
* 187 labelled contract/configuration scenarios measure routing behaviour.

The second layer is not an institution pilot.  Its labels come from explicit
financial-purpose contracts and counterfactual *configuration* variants; raw
market evidence is never mutated.  Every API call reruns the actual policies
and returns the Skill version, entrypoint digest, input/output digests,
denominators and formulas used to calculate the frontend values.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from manager_routing import decide_root_route


APP_ROOT = Path(__file__).resolve().parent
CORPUS_ROOT = APP_ROOT / "data" / "evaluation_seed_cases_v1"
SKILL_MANIFEST = (
    APP_ROOT.parent
    / "agentteams"
    / "config"
    / "evaluation-metrics-skill-manifest.json"
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def legacy_schema_only(facts: dict[str, Any]) -> dict[str, str]:
    if facts.get("rights_status") != "PASS":
        return {"route": "REJECT_AT_INTAKE", "machine_recommendation": "REJECT"}
    if facts.get("evidence_status") != "VERIFIED" or not facts.get(
        "evidence_hash_valid"
    ):
        return {
            "route": "NEEDS_EVIDENCE",
            "machine_recommendation": "NEEDS_EVIDENCE",
        }
    return {"route": "CODE_ONLY_PRECHECK", "machine_recommendation": "PASS"}


METRIC_CONTRACTS = {
    "route_accuracy": {
        "label": "Case路由准确率",
        "formula": "实际路由与契约标签一致的Case数 / 全部评测Case数",
        "numerator_field": "route_correct_count",
        "denominator_field": "case_count",
        "display_unit": "%",
    },
    "false_release_rate": {
        "label": "误放率",
        "formula": "预期非PASS但系统输出PASS的Case数 / 全部预期非PASS Case数",
        "numerator_field": "false_release_count",
        "denominator_field": "expected_nonpass_count",
        "display_unit": "%",
    },
    "false_block_rate": {
        "label": "误阻率",
        "formula": "预期PASS但系统输出非PASS的Case数 / 全部预期PASS Case数",
        "numerator_field": "false_block_count",
        "denominator_field": "expected_pass_count",
        "display_unit": "%",
    },
    "policy_latency_p95_ms": {
        "label": "本地策略P95",
        "formula": "187次本地Python策略调用耗时按升序排列后的nearest-rank P95",
        "numerator_field": "latency_ms.p95",
        "denominator_field": "case_count",
        "display_unit": "ms",
    },
}


def _verified_inputs() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads((CORPUS_ROOT / "manifest.json").read_text(encoding="utf-8"))
    corpus_bytes = (CORPUS_ROOT / "seed_cases.jsonl").read_bytes()
    if hashlib.sha256(corpus_bytes).hexdigest() != manifest["seed_cases_jsonl_sha256"]:
        raise ValueError("seed_cases.jsonl SHA256与Manifest不一致")
    for source in manifest.get("source_files") or []:
        source_path = APP_ROOT / str(source["path"])
        if not source_path.is_file():
            raise ValueError(f"来源文件不存在: {source['path']}")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"来源文件SHA256不一致: {source['path']}")
    cases = [
        json.loads(line)
        for line in corpus_bytes.decode("utf-8").splitlines()
        if line.strip()
    ]
    if len(cases) != int(manifest["case_count"]):
        raise ValueError("评测Case数与Manifest不一致")
    skill = json.loads(SKILL_MANIFEST.read_text(encoding="utf-8"))
    entrypoint = APP_ROOT.parent / str(skill["entrypoint"])
    actual_entrypoint_sha256 = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    if actual_entrypoint_sha256 != skill["entrypoint_sha256"]:
        raise ValueError("评测Skill入口SHA256不一致")
    return manifest, cases, skill


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _evaluate(
    name: str,
    cases: list[dict[str, Any]],
    runner: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    latencies_ms: list[float] = []
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
    expected_pass_count = sum(expected_pass)
    expected_nonpass_count = total - expected_pass_count
    false_release_count = sum(
        (not expected) and actual
        for expected, actual in zip(expected_pass, actual_pass)
    )
    false_block_count = sum(
        expected and (not actual)
        for expected, actual in zip(expected_pass, actual_pass)
    )
    route_correct_count = sum(
        row["expected_route"] == row["actual_route"] for row in rows
    )
    recommendation_correct_count = sum(
        row["expected_recommendation"] == row["actual_recommendation"]
        for row in rows
    )
    return {
        "system": name,
        "case_count": total,
        "metric_inputs": {
            "route_correct_count": route_correct_count,
            "expected_pass_count": expected_pass_count,
            "expected_nonpass_count": expected_nonpass_count,
            "false_release_count": false_release_count,
            "false_block_count": false_block_count,
        },
        "recommendation_accuracy": round(recommendation_correct_count / total, 6),
        "route_accuracy": round(route_correct_count / total, 6),
        "false_release_count": false_release_count,
        "false_release_rate": round(
            false_release_count / max(1, expected_nonpass_count), 6
        ),
        "false_block_count": false_block_count,
        "false_block_rate": round(
            false_block_count / max(1, expected_pass_count), 6
        ),
        "latency_scope": (
            "local Python policy wall time; not production end-to-end latency"
        ),
        "latency_ms": {
            "median": round(statistics.median(latencies_ms), 6),
            "p95": round(_nearest_rank_p95(latencies_ms), 6),
            "max": round(max(latencies_ms), 6),
        },
        "actual_route_counts": dict(Counter(row["actual_route"] for row in rows)),
        "mismatches": [
            row for row in rows if row["expected_route"] != row["actual_route"]
        ][:20],
    }


def execute_evaluation_metrics_skill() -> dict[str, Any]:
    manifest, cases, skill = _verified_inputs()
    input_payload = {
        "seed_cases_jsonl_sha256": manifest["seed_cases_jsonl_sha256"],
        "source_files": manifest["source_files"],
        "case_count": len(cases),
        "metric_contracts": METRIC_CONTRACTS,
        "manager_policy": "finflux_manager_route_policy_v0.2.0",
    }
    input_sha256 = canonical_sha256(input_payload)
    started = time.perf_counter_ns()
    systems = [
        _evaluate("legacy_schema_only", cases, legacy_schema_only),
        _evaluate(
            "finflux_manager_route_policy_v0.2.0", cases, decide_root_route
        ),
    ]
    skill_wall_time_ms = (time.perf_counter_ns() - started) / 1_000_000
    result = {
        "protocol": "FINFLUX_EVALUATION_METRICS_V1.0",
        "corpus": {
            "case_count": len(cases),
            "counts_by_asset": manifest["counts_by_asset"],
            "counts_by_expected_route": manifest["counts_by_expected_route"],
            "raw_market_data_mutated": manifest["raw_market_data_mutated"],
            "configuration_variants_are_counterfactual": manifest[
                "configuration_variants_are_counterfactual"
            ],
            "seed_cases_jsonl_sha256": manifest["seed_cases_jsonl_sha256"],
        },
        "label_boundary": (
            "标签来自公开证据、显式金融用途契约和反事实接入配置；"
            "不是金融机构人工标注，也不是生产效果。"
        ),
        "metric_contracts": METRIC_CONTRACTS,
        "executed_systems": systems,
        "limitations": manifest["limitations"],
    }
    output_sha256 = canonical_sha256(result)
    result["skill_invocation"] = {
        "skill_id": skill["skill_id"],
        "version": skill["version"],
        "entrypoint": skill["entrypoint"],
        "entrypoint_sha256": skill["entrypoint_sha256"],
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "status": "SUCCESS",
        "model_calls": 0,
        "provider_tokens": 0,
        "skill_wall_time_ms": round(skill_wall_time_ms, 6),
    }
    return result

