from __future__ import annotations

import unittest
from pathlib import Path

from profile_registry import (
    SAFE_COMPONENTS,
    build_profile_projection,
    get_profile,
    list_profiles,
)
from app import compact_provider_usage


ROOT = Path(__file__).resolve().parent


class P0UIContractTests(unittest.TestCase):
    def test_registry_is_versioned_and_hash_bound(self) -> None:
        registry = list_profiles()
        self.assertEqual(registry["protocol"], "FINFLUX_PROFILE_REGISTRY_V1")
        self.assertEqual(registry["source_protocol"], "FINFLUX_PROFILE_REGISTRY_V0.2")
        self.assertEqual(len(registry["source_registry_sha256"]), 64)
        self.assertEqual(registry["count"], 5)
        self.assertEqual(len(registry["registry_sha256"]), 64)
        executable = [item for item in registry["profiles"] if item["live_executable"]]
        self.assertEqual(len(executable), 3)
        for profile in registry["profiles"]:
            self.assertEqual(len(profile["profile_sha256"]), 64)
            self.assertTrue(profile["purpose_bindings"])
            self.assertTrue(profile["presentation_schema"])
            self.assertTrue(
                all(
                    item["component"] in SAFE_COMPONENTS
                    for item in profile["presentation_schema"]
                )
            )

    def test_projection_is_profile_driven(self) -> None:
        submission = {
            "profile": "futures_settlement",
            "parsed": {"instrument": "TEST-INSTRUMENT", "trade_date": "2026-01-01"},
            "metadata": {"declared_purpose": "daily_settlement_pnl"},
        }
        projection = build_profile_projection(
            "futures_settlement", submission, {"machine_recommendation": "WAIT"}
        )
        self.assertEqual(projection["profile_id"], "futures_settlement")
        self.assertEqual(projection["profile_version"], "0.2.0")
        self.assertEqual(
            projection["required_concept"], "official_daily_settlement_price"
        )
        self.assertTrue(projection["purpose_options"])

    def test_unknown_profile_fails_closed_to_universal_presentation(self) -> None:
        projection = build_profile_projection(
            "not-registered", {"parsed": {}, "metadata": {}}, {}
        )
        self.assertEqual(projection["profile_id"], "unregistered_financial_evidence")
        self.assertFalse(projection["live_executable"])

    def test_primary_frontend_has_no_asset_specific_field_branches(self) -> None:
        source = "\n".join(
            (ROOT / "web" / "js" / name).read_text(encoding="utf-8")
            for name in ("views-live.js", "views-changes.js")
        )
        for forbidden in (
            "submission.profile ===",
            "parsed.close",
            "parsed.settle",
            "parsed.qfq",
            "parsed.unit_nav",
        ):
            self.assertNotIn(forbidden, source)

    def test_navigation_exposes_exactly_four_primary_stages(self) -> None:
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("data-stage-route="), 4)
        for route in ("live", "collaboration", "evidence", "changes"):
            self.assertIn(f'data-stage-route="{route}"', html)

    def test_live_launch_does_not_blindly_redirect_to_human_or_collaboration(self) -> None:
        source = (ROOT / "web" / "js" / "views-live.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("ws.run_submission || ws.latest_submission", source)
        self.assertIn("getRunStatus(runId)", source)
        self.assertIn("humanState === \"AWAITING_HUMAN\"", source)
        self.assertNotIn('id="btn-open-blocking-run"', source)
        self.assertIn("本页不会自动跳转", source)

    def test_missing_profile_is_not_returned_as_a_definition(self) -> None:
        self.assertIsNone(get_profile("does-not-exist"))

    def test_polling_usage_keeps_totals_but_not_full_records(self) -> None:
        compact = compact_provider_usage(
            {
                "status": "PROVIDER_REPORTED",
                "total_tokens": 33,
                "call_count": 2,
                "model_gateway_ledger": {
                    "status": "ACTIVE",
                    "records": [{"sequence": 1}, {"sequence": 2}],
                    "ledger_sha256": "a" * 64,
                },
            }
        )
        self.assertEqual(compact["total_tokens"], 33)
        self.assertEqual(compact["model_gateway_ledger"]["record_count"], 2)
        self.assertNotIn("records", compact["model_gateway_ledger"])
        self.assertFalse(compact["model_gateway_ledger"]["records_included"])


if __name__ == "__main__":
    unittest.main()
