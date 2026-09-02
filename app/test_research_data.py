from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_data.core import (
    DATA_ROOT,
    ResearchDataStore,
    RightsGate,
    load_provider_registry,
    validate_research_item,
)
from research_data.investigator import inspect_cached_research


class ResearchDataTests(unittest.TestCase):
    def test_registry_is_frozen_to_six_providers(self) -> None:
        registry = load_provider_registry()
        self.assertEqual(len(registry["providers"]), 6)

    def test_rights_gate_is_fail_closed_for_eastmoney_fulltext(self) -> None:
        decision = RightsGate().evaluate(
            "eastmoney_report", "RESEARCH_DOCUMENT", "STORE_FULL_CONTENT"
        )
        self.assertEqual(decision["decision"], "DENY")
        self.assertEqual(decision["storage_policy"], "LINK_ONLY")

    def test_cached_items_are_schema_valid_and_not_synthetic(self) -> None:
        items = ResearchDataStore(DATA_ROOT).load_items()
        if not items:
            self.skipTest("real cache has not been collected")
        self.assertGreaterEqual(len(items), 30)
        self.assertLessEqual(len(items), 40)
        self.assertEqual(
            {item["provider_id"] for item in items},
            {"eastmoney_report", "worldbank"},
        )
        for item in items:
            self.assertFalse(validate_research_item(item), item["research_item_id"])
            self.assertTrue(item["source_url"].startswith("http"))
            self.assertNotIn("synthetic", json.dumps(item, ensure_ascii=False).lower())

    def test_manifest_detects_hash_and_rights_leak(self) -> None:
        items = ResearchDataStore(DATA_ROOT).load_items()
        if not items:
            self.skipTest("real cache has not been collected")
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDataStore(Path(temporary))
            result = store.upsert_items(items)
            self.assertEqual(result["quality"]["status"], "PASS")
            self.assertEqual(result["quality"]["restricted_content_leak_count"], 0)

    def test_investigator_selects_real_research_for_run(self) -> None:
        if not (DATA_ROOT / "research_items.jsonl").exists():
            self.skipTest("real cache has not been collected")
        result = inspect_cached_research("equity")
        self.assertEqual(result["status"], "VERIFIED_METADATA")
        self.assertGreater(result["selected_count"], 0)
        self.assertIn("eastmoney_report", result["provider_counts"])


if __name__ == "__main__":
    unittest.main()
