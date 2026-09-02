from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unified_intake import (
    intake_capabilities,
    search_research_catalog,
    selected_research_items,
    validate_public_url,
)
from live_intake import LiveIntakeRepository


class UnifiedIntakeTests(unittest.TestCase):
    @staticmethod
    def futures_csv() -> bytes:
        return (
            "instrument,trade_date,close,settle\n"
            "IF2608,20260814,4648.4,4652.4\n"
        ).encode("utf-8")

    def test_capabilities_expose_truthful_profile_boundary(self) -> None:
        value = intake_capabilities()
        self.assertGreaterEqual(value["catalog_real_item_count"], 40)
        self.assertEqual(value["case_input_protocol"], "FINFLUX_CASE_INPUT_V0.2")
        input_ids = {item["id"] for item in value["inputs"]}
        self.assertIn("file_plus_intent", input_ids)
        self.assertIn("public_url_plus_intent", input_ids)
        self.assertIn("research_catalog_plus_intent", input_ids)
        self.assertNotIn("text", input_ids)
        states = {item["profile"]: item["execution_readiness"] for item in value["profiles"]}
        self.assertEqual(states["futures_settlement"], "AGENTTEAMS_EXECUTABLE")
        self.assertEqual(states["equity_corporate_action"], "AGENTTEAMS_EXECUTABLE")
        self.assertEqual(states["option_contract_identity"], "ZERO_MODEL_ACCEPTED_NOT_LIVE")
        self.assertEqual(states["fund_nav_admission"], "AGENTTEAMS_EXECUTABLE")
        self.assertEqual(states["research_material_rights"], "ZERO_MODEL_ACCEPTED_NOT_LIVE")
        self.assertEqual(states["universal_financial_evidence"], "WAIT_FOR_PROFILE")
        frozen = [
            item for item in value["profiles"]
            if item["profile"] != "universal_financial_evidence"
        ]
        self.assertEqual(len(frozen), 5)
        self.assertTrue(all(len(item["profile_sha256"]) == 64 for item in frozen))

    def test_catalog_search_returns_real_hashed_items(self) -> None:
        value = search_research_catalog(provider_id="eastmoney_report", limit=3)
        self.assertGreater(value["count"], 0)
        self.assertEqual(value["synthetic_records"], 0)
        self.assertTrue(all(len(item["metadata_sha256"]) == 64 for item in value["items"]))
        selected = selected_research_items([value["items"][0]["research_item_id"]])
        self.assertEqual(len(selected), 1)

    def test_private_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_public_url("http://127.0.0.1/private.csv")

    def test_text_evidence_is_immutable_but_not_falsely_executable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            submission = repository.create_submission(
                "announcement.txt",
                "某上市公司披露真实公告文本。".encode("utf-8"),
                {
                    "profile": "universal_financial_evidence",
                    "declared_source": "交易所公告",
                    "rights_basis": "公开公告",
                    "declared_purpose": "evidence_review",
                    "asset_class": "equity",
                },
            )
            self.assertEqual(submission["status"], "VERIFIED")
            self.assertEqual(
                submission["execution_readiness"], "WAIT_FOR_PROFILE"
            )
            with self.assertRaisesRegex(ValueError, "WAIT|Profile"):
                repository.create_run(submission["submission_id"], {"connected": True})

    def test_file_inspection_is_zero_token_and_contract_driven(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            inspection = repository.inspect_file("if2608.csv", self.futures_csv())
            self.assertEqual(inspection["inferred"]["profile"], "futures_settlement")
            self.assertEqual(inspection["inferred"]["candidate_mapping"], "settle")
            self.assertEqual(inspection["inferred"]["contract_multiplier"], 300.0)
            self.assertEqual(inspection["token_usage"]["total_tokens"], 0)
            self.assertEqual(len(inspection["skill_invocations"]), 3)

    def test_known_hash_reuses_source_and_commit_is_executable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            body = self.futures_csv()
            repository.create_submission(
                "known.csv",
                body,
                {
                    "profile": "futures_settlement",
                    "declared_source": "CFFEX public daily file",
                    "rights_basis": "public market data",
                    "declared_purpose": "daily_settlement_pnl",
                    "candidate_mapping": "settle",
                    "contract_multiplier": 300,
                    "confidentiality_class": "PUBLIC",
                },
            )
            inspection = repository.inspect_file("renamed.csv", body)
            self.assertIsNotNone(inspection["known_evidence_match"])
            self.assertNotIn("declared_source", inspection["required_confirmations"])
            submission = repository.commit_inspection(
                inspection["inspection_id"],
                {"declared_purpose": "daily_settlement_pnl"},
            )
            self.assertEqual(submission["execution_readiness"], "AGENTTEAMS_EXECUTABLE")
            self.assertEqual(submission["metadata"]["candidate_mapping"], "settle")
            self.assertEqual(submission["metadata"]["inspection_id"], inspection["inspection_id"])
            run = repository.create_run(submission["submission_id"], {"connected": False})
            self.assertEqual(run["precheck"]["machine_recommendation"], "PASS")
            self.assertEqual(run["budget"]["tokens"]["reported"], 0)

    def test_unknown_text_stays_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            inspection = repository.inspect_file(
                "notice.txt", "一份真实公告文本".encode("utf-8")
            )
            self.assertEqual(
                inspection["execution_readiness_candidate"], "WAIT_FOR_PROFILE"
            )
            self.assertIn("UNKNOWN_FINANCIAL_PROFILE", inspection["wait_reason_codes"])
            self.assertNotEqual(inspection["inferred"]["asset_class"], "futures")

    def test_file_and_task_instruction_form_one_hash_sealed_case(self) -> None:
        instruction = "核验IF2608逐日结算字段语义，并给出可审计DataPass建议。"
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            inspection = repository.inspect_file(
                "if2608.csv",
                self.futures_csv(),
                {
                    "task_instruction": instruction,
                    "input_mode": "FILE_PLUS_INTENT",
                    "declared_source": "CFFEX公开日行情",
                    "rights_basis": "公开数据用于竞赛核验",
                    "confidentiality_class": "PUBLIC",
                },
            )
            submission = repository.commit_inspection(
                inspection["inspection_id"],
                {"declared_purpose": "daily_settlement_pnl"},
            )
        self.assertEqual(submission["case_input"]["task_instruction"], instruction)
        self.assertEqual(submission["case_input"]["input_mode"], "FILE_PLUS_INTENT")
        self.assertEqual(len(submission["case_input"]["task_instruction_sha256"]), 64)
        self.assertEqual(len(submission["case_input"]["case_input_sha256"]), 64)

    def test_existing_evidence_can_bind_new_intent_without_mutating_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = LiveIntakeRepository(Path(root))
            original = repository.create_submission(
                "if2608.csv",
                self.futures_csv(),
                {
                    "profile": "futures_settlement",
                    "declared_source": "CFFEX公开日行情",
                    "rights_basis": "公开数据用于竞赛核验",
                    "declared_purpose": "daily_settlement_pnl",
                    "candidate_mapping": "close",
                    "contract_multiplier": 300,
                },
            )
            derived = repository.derive_submission_with_instruction(
                original["submission_id"], "核验close与settle是否混用。"
            )
        self.assertEqual(derived["file"]["sha256"], original["file"]["sha256"])
        self.assertNotEqual(derived["submission_id"], original["submission_id"])
        self.assertEqual(
            derived["case_input"]["source_submission_id"], original["submission_id"]
        )
        self.assertFalse(derived["raw_evidence_mutated"])

    @patch("unified_intake.socket.getaddrinfo")
    def test_public_url_is_accepted(self, getaddrinfo: object) -> None:
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        parsed = validate_public_url("https://example.com/evidence.csv")
        self.assertEqual(parsed.hostname, "example.com")
