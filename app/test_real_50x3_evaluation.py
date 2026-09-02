from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

from app import load_source_bound_evaluation


ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "app" / "data" / "real_50x3_v1"


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class Real50x3EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (DATA_ROOT / "manifest.json").read_text(encoding="utf-8-sig")
        )
        cls.results = json.loads(
            (DATA_ROOT / "precheck_results.json").read_text(encoding="utf-8-sig")
        )
        cls.report = json.loads(
            (DATA_ROOT / "evaluation_report.json").read_text(encoding="utf-8-sig")
        )

    def test_manifest_contains_exactly_50_real_rows_per_asset(self) -> None:
        records = self.manifest["records"]
        self.assertEqual(len(records), 150)
        self.assertEqual(
            Counter(item["asset_class"] for item in records),
            {"futures": 50, "equity": 50, "fund": 50},
        )
        self.assertEqual(self.manifest["model_generated_records"], 0)
        self.assertFalse(self.manifest["raw_market_data_mutated"])
        self.assertEqual(len({item["record_sha256"] for item in records}), 150)

    def test_manifest_and_every_record_hash_are_reproducible(self) -> None:
        unsigned = {
            key: value
            for key, value in self.manifest.items()
            if key != "manifest_sha256"
        }
        self.assertEqual(
            self.manifest["manifest_sha256"], canonical_sha256(unsigned)
        )
        for record in self.manifest["records"]:
            declared = record["record_sha256"]
            unsigned_record = {
                key: value for key, value in record.items() if key != "record_sha256"
            }
            self.assertEqual(declared, canonical_sha256(unsigned_record))

    def test_source_artifacts_are_attested_but_not_redistributed(self) -> None:
        for source in self.manifest["sources"]:
            path = ROOT / source["artifact_path"]
            self.assertFalse(path.exists(), source["artifact_path"])
            self.assertTrue(str(source["source_url"]).startswith(("http://", "https://")))
            self.assertEqual(len(source["artifact_sha256"]), 64)
            self.assertEqual(source["rights_state"], "REVIEW_REQUIRED")

    def test_precheck_is_zero_model_and_binds_each_source_record(self) -> None:
        records = {item["record_id"]: item for item in self.manifest["records"]}
        self.assertEqual(len(self.results), 150)
        for result in self.results:
            self.assertIn(result["record_id"], records)
            self.assertEqual(result["input_sha256"], records[result["record_id"]]["record_sha256"])
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["provider_tokens"], 0)
            declared = result["result_sha256"]
            unsigned_result = {
                key: value for key, value in result.items() if key != "result_sha256"
            }
            self.assertEqual(declared, canonical_sha256(unsigned_result))

    def test_report_does_not_claim_unmeasured_error_rates(self) -> None:
        pipeline = self.report["executed_pipeline"]
        self.assertEqual(pipeline["processed_count"], 150)
        self.assertEqual(pipeline["model_calls"], 0)
        self.assertNotIn("route_accuracy", pipeline)
        self.assertNotIn("false_release_rate", pipeline)
        self.assertTrue(
            any(
                item["system"] == "financial-institution pilot metrics"
                and item["status"] == "NOT_EXECUTED"
                for item in self.report["not_executed"]
            )
        )

    def test_backend_loader_fails_closed_and_accepts_current_hash_chain(self) -> None:
        report, error = load_source_bound_evaluation()
        self.assertIsNone(error)
        self.assertEqual(report["protocol"], "FINFLUX_SOURCE_BOUND_EVALUATION_V1")
        self.assertEqual(report["corpus"]["case_count"], 150)


if __name__ == "__main__":
    unittest.main()
