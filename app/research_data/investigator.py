from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .core import DATA_ROOT, ResearchDataStore, sha256_json, utc_now, write_json_atomic


DEMO_ROOT = Path(__file__).resolve().parent.parent
RUN_RESEARCH_ROOT = DEMO_ROOT / "runtime" / "research_runs"
PROTOCOL = "FINFLUX_RESEARCH_EVIDENCE_BUNDLE_V0.1"


ASSET_ENTITIES = {
    "equity": {"000001", "600519", "300750", "601318", "600030", "A股"},
    "futures": {"CHN", "NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "macro"},
    "option": {"CHN", "NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "macro"},
}


def inspect_cached_research(
    asset: str,
    max_items: int = 12,
    root: Path = DATA_ROOT,
) -> dict[str, Any]:
    if asset not in ASSET_ENTITIES:
        raise ValueError("asset must be equity, futures, or option")
    store = ResearchDataStore(root)
    items = store.load_items()
    if not items:
        return {
            "status": "NEEDS_EVIDENCE",
            "reason": "RESEARCH_CACHE_EMPTY",
            "items": [],
            "manifest_sha256": None,
        }
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8-sig"))
    entities = ASSET_ENTITIES[asset]

    def score(item: dict[str, Any]) -> tuple[int, str]:
        item_entities = {str(value) for value in item.get("entities", [])}
        item_topics = {str(value) for value in item.get("topics", [])}
        relevance = len(entities.intersection(item_entities | item_topics))
        if asset == "equity" and item.get("provider_id") == "eastmoney_report":
            relevance += 3
        if asset != "equity" and item.get("content_type") == "OFFICIAL_STATISTIC":
            relevance += 3
        if item.get("provider_tier") == "OFFICIAL_PRIMARY":
            relevance += 1
        return relevance, str(item.get("published_at", ""))

    selected = sorted(items, key=score, reverse=True)[:max_items]
    references = [
        {
            "research_item_id": item["research_item_id"],
            "provider_id": item["provider_id"],
            "content_type": item["content_type"],
            "title": item["title"],
            "published_at": item["published_at"],
            "source_url": item["source_url"],
            "rights_state": item["rights_state"],
            "storage_policy": item["storage_policy"],
            "metadata_sha256": item["metadata_sha256"],
            "raw_response_sha256": item["raw_response_sha256"],
            "point_in_time_safe": item["point_in_time_safe"],
        }
        for item in selected
    ]
    return {
        "status": "VERIFIED_METADATA",
        "reason": "CACHE_MANIFEST_AND_ITEM_HASHES_AVAILABLE",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "cache_items_sha256": manifest.get("items_sha256"),
        "selected_count": len(references),
        "provider_counts": dict(Counter(item["provider_id"] for item in selected)),
        "content_type_counts": dict(Counter(item["content_type"] for item in selected)),
        "items": references,
    }


def build_run_research_bundle(
    asset: str,
    case_id: str,
    run_id: str,
    root: Path = DATA_ROOT,
) -> dict[str, Any]:
    investigation = inspect_cached_research(asset, root=root)
    if investigation["status"] != "VERIFIED_METADATA":
        return investigation
    bundle: dict[str, Any] = {
        "protocol": PROTOCOL,
        "case_id": case_id,
        "run_id": run_id,
        "asset_class": asset,
        "investigator_role": "evidence-investigator",
        "investigator_display_name": "Research Evidence Investigator",
        "execution_mode": "DETERMINISTIC_CACHE_SELECTION_FOR_AGENT_REVIEW",
        "financial_truth_generated_by_model": False,
        "created_at": utc_now(),
        **investigation,
    }
    bundle["bundle_sha256"] = sha256_json(bundle)
    RUN_RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    write_json_atomic(RUN_RESEARCH_ROOT / f"{run_id}.json", bundle)
    return bundle

