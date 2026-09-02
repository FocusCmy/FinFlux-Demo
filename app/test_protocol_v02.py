from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from finchange_gate_core import generate_datapass
from protocol_v02 import (
    CASE_ENVELOPE_PROTOCOL,
    DATAPASS_PROTOCOL,
    PROFILE_BY_ASSET,
    ProtocolValidationError,
    adapt_legacy_case_envelope,
    adapt_legacy_datapass,
    build_case_envelope,
    build_datapass_draft,
    canonical_sha256,
    load_profile_registry,
    resolve_profile,
    validate_case_envelope,
    validate_datapass,
)


ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-08-31T00:00:00+00:00"
POLICY = "FINFLUX-BOUNDED-EXECUTION-V0.1"


def evidence(profile_id: str, suffix: str = "1") -> dict:
    profile = resolve_profile(profile_id)
    digest = canonical_sha256({"profile": profile_id, "suffix": suffix})
    return {
        "evidence_id": f"EVID-{profile_id.upper()}-{suffix}",
        "evidence_type": profile["evidence_requirements"][0]["evidence_type"],
        "content_sha256": digest,
        "source_locator": f"immutable://audit/{profile_id}/{suffix}",
        "media_type": "application/json",
        "rights_state": "PUBLIC",
        "version_id": f"sha256:{digest[:16]}",
    }


def envelope_for(profile_id: str) -> dict:
    profile = resolve_profile(profile_id)
    purpose = profile["declared_purposes"][0]
    return build_case_envelope(
        profile_id=profile_id,
        case_id=f"CASE-{profile_id.upper()}",
        run_id=f"RUN-{profile_id.upper()}-AUDIT-001",
        purpose_id=purpose["purpose_id"],
        purpose_statement=purpose["target_decision"],
        evidence_handles=[evidence(profile_id)],
        trigger="P0_A_PROTOCOL_ACCEPTANCE",
        expected_route="FULL_TEAM_REVIEW",
        execution_policy_id=POLICY,
        created_at_utc=NOW,
    )


class ProtocolV02Tests(unittest.TestCase):
    def test_registry_contains_five_hashed_governed_profiles(self) -> None:
        registry = load_profile_registry()
        self.assertEqual(len(registry["profiles"]), 5)
        self.assertEqual(
            {profile["profile_id"] for profile in registry["profiles"]},
            set(PROFILE_BY_ASSET.values()),
        )
        for profile in registry["profiles"]:
            unsigned = {key: value for key, value in profile.items() if key != "profile_sha256"}
            self.assertEqual(profile["profile_sha256"], canonical_sha256(unsigned))
            self.assertTrue(profile["declared_purposes"])
            self.assertTrue(profile["evidence_requirements"])
            self.assertGreaterEqual(len(profile["display_fields"]), 6)

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProtocolValidationError, "unknown profile"):
            resolve_profile("generic_finance_assistant")

    def test_all_governed_profiles_build_strict_case_envelopes(self) -> None:
        for profile_id in PROFILE_BY_ASSET.values():
            value = envelope_for(profile_id)
            self.assertEqual(value["protocol"], CASE_ENVELOPE_PROTOCOL)
            self.assertEqual(len(value["envelope_sha256"]), 64)
            self.assertEqual(validate_case_envelope(value), value)

    def test_case_envelope_rejects_unknown_fields_and_hash_tampering(self) -> None:
        value = envelope_for("futures_settlement")
        unknown = copy.deepcopy(value)
        unknown["raw_data"] = {"close": 4648.4}
        with self.assertRaisesRegex(ProtocolValidationError, "unknown fields"):
            validate_case_envelope(unknown)
        tampered = copy.deepcopy(value)
        tampered["trigger"] = "CHANGED_AFTER_SEAL"
        with self.assertRaisesRegex(ProtocolValidationError, "does not match canonical payload"):
            validate_case_envelope(tampered)

    def test_case_envelope_rejects_unregistered_purpose_and_wrong_evidence(self) -> None:
        profile_id = "futures_settlement"
        with self.assertRaisesRegex(ProtocolValidationError, "not registered"):
            build_case_envelope(
                profile_id=profile_id,
                case_id="CASE-WRONG-PURPOSE",
                run_id="RUN-WRONG-PURPOSE",
                purpose_id="market_forecast",
                purpose_statement="预测涨跌",
                evidence_handles=[evidence(profile_id)],
                trigger="TEST",
                expected_route="FULL_TEAM_REVIEW",
                execution_policy_id=POLICY,
                created_at_utc=NOW,
            )
        wrong_type = evidence(profile_id)
        wrong_type["evidence_type"] = "equity_corporate_action_bundle"
        with self.assertRaisesRegex(ProtocolValidationError, "not accepted"):
            build_case_envelope(
                profile_id=profile_id,
                case_id="CASE-WRONG-EVIDENCE",
                run_id="RUN-WRONG-EVIDENCE",
                purpose_id="daily_settlement_pnl",
                purpose_statement="核验逐日结算输入",
                evidence_handles=[wrong_type],
                trigger="TEST",
                expected_route="FULL_TEAM_REVIEW",
                execution_policy_id=POLICY,
                created_at_utc=NOW,
            )

    def test_datapass_binds_case_profile_purpose_and_hashes(self) -> None:
        for profile_id in PROFILE_BY_ASSET.values():
            envelope = envelope_for(profile_id)
            worker_hash = canonical_sha256({"worker": profile_id})
            skill_in = canonical_sha256({"input": profile_id})
            skill_out = canonical_sha256({"output": profile_id})
            value = build_datapass_draft(
                envelope=envelope,
                machine_recommendation="PASS",
                reason_codes=["CONTRACT_AND_EVIDENCE_VERIFIED"],
                recommendation_summary="证据与已登记语义契约一致，提交责任人审批。",
                evidence_status="VERIFIED",
                evidence_quorum_met=True,
                semantic_status="RESOLVED",
                impact_status="COMPUTED",
                impact_facts={"residual": 0},
                impact_metrics=[
                    {
                        "metric_id": "residual",
                        "label": "剩余差异",
                        "value": 0,
                        "unit": "",
                        "source_kind": "DETERMINISTIC",
                    }
                ],
                required_worker_ids=["independent-validator"],
                worker_receipts=[
                    {
                        "worker_id": "independent-validator",
                        "status": "SEALED",
                        "artifact_sha256": worker_hash,
                    }
                ],
                required_skill_ids=["independent-evidence-validator"],
                skill_invocations=[
                    {
                        "skill_id": "independent-evidence-validator",
                        "version": "1.0.0",
                        "worker_id": "independent-validator",
                        "input_sha256": skill_in,
                        "output_sha256": skill_out,
                        "status": "SUCCEEDED",
                    }
                ],
                generated_at_utc=NOW,
            )
            self.assertEqual(value["protocol"], DATAPASS_PROTOCOL)
            self.assertEqual(value["envelope_sha256"], envelope["envelope_sha256"])
            self.assertEqual(value["workers"]["attestation_status"], "VERIFIED")
            self.assertEqual(value["skills"]["attestation_status"], "VERIFIED")
            self.assertEqual(validate_datapass(value, envelope=envelope), value)

    def _valid_futures_datapass(self) -> tuple[dict, dict]:
        envelope = envelope_for("futures_settlement")
        value = build_datapass_draft(
            envelope=envelope,
            machine_recommendation="PASS",
            reason_codes=["CONTRACT_AND_EVIDENCE_VERIFIED"],
            recommendation_summary="证据与契约均已核验。",
            evidence_status="VERIFIED",
            evidence_quorum_met=True,
            semantic_status="RESOLVED",
            impact_status="COMPUTED",
            impact_facts={"residual": 0},
            impact_metrics=[],
            required_worker_ids=["independent-validator"],
            worker_receipts=[
                {
                    "worker_id": "independent-validator",
                    "status": "SEALED",
                    "artifact_sha256": canonical_sha256(
                        {"worker": "independent-validator"}
                    ),
                }
            ],
            required_skill_ids=["independent-evidence-validator"],
            skill_invocations=[
                {
                    "skill_id": "independent-evidence-validator",
                    "version": "1.0.0",
                    "worker_id": "independent-validator",
                    "input_sha256": canonical_sha256({"input": "futures"}),
                    "output_sha256": canonical_sha256({"output": "futures"}),
                    "status": "SUCCEEDED",
                }
            ],
            generated_at_utc=NOW,
        )
        return envelope, value

    def test_datapass_rejects_fabricated_reported_count_999(self) -> None:
        envelope, value = self._valid_futures_datapass()
        value["workers"]["reported_completed_count"] = 999
        with self.assertRaisesRegex(ProtocolValidationError, "SEALED Worker receipts"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

        envelope, value = self._valid_futures_datapass()
        value["workers"]["reported_required_count"] = 999
        with self.assertRaisesRegex(ProtocolValidationError, "required_worker_ids"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

    def test_datapass_rejects_wrong_skill_version_and_responsible_worker(self) -> None:
        envelope, value = self._valid_futures_datapass()
        value["skills"]["invocations"][0]["version"] = "9.9.9"
        with self.assertRaisesRegex(ProtocolValidationError, "frozen version 1.0.0"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

        envelope, value = self._valid_futures_datapass()
        value["skills"]["invocations"][0]["worker_id"] = "evidence-investigator"
        with self.assertRaisesRegex(ProtocolValidationError, "responsible Worker"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

    def test_datapass_rejects_failed_skill_receipts_even_if_marked_verified(self) -> None:
        envelope, value = self._valid_futures_datapass()
        value["skills"]["invocations"][0]["status"] = "FAILED"
        with self.assertRaisesRegex(ProtocolValidationError, "only SUCCEEDED or CACHE_HIT"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

    def test_datapass_rejects_duplicate_worker_and_skill_receipts(self) -> None:
        envelope, value = self._valid_futures_datapass()
        value["workers"]["receipts"].append(copy.deepcopy(value["workers"]["receipts"][0]))
        with self.assertRaisesRegex(ProtocolValidationError, "duplicate Worker receipt"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

        envelope, value = self._valid_futures_datapass()
        value["skills"]["invocations"].append(copy.deepcopy(value["skills"]["invocations"][0]))
        with self.assertRaisesRegex(ProtocolValidationError, "duplicate Skill receipt"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

    def test_datapass_verified_rejects_missing_skill_receipt(self) -> None:
        envelope, value = self._valid_futures_datapass()
        value["skills"]["invocations"] = []
        with self.assertRaisesRegex(ProtocolValidationError, "VERIFIED contradicts missing"):
            validate_datapass(value, envelope=envelope, verify_hash=False)

    def test_datapass_optionally_binds_worker_artifact_hash(self) -> None:
        envelope, value = self._valid_futures_datapass()
        artifact = {
            "role": "independent-validator",
            "status": "SEALED",
        }
        artifact["artifact_sha256"] = canonical_sha256(artifact)
        value["workers"]["receipts"][0]["artifact_sha256"] = artifact["artifact_sha256"]
        value["datapass_sha256"] = canonical_sha256(
            {key: item for key, item in value.items() if key != "datapass_sha256"}
        )
        self.assertEqual(
            validate_datapass(
                value,
                envelope=envelope,
                worker_artifacts={"independent-validator": artifact},
            ),
            value,
        )

        bad_artifact = copy.deepcopy(artifact)
        bad_artifact["status"] = "CHANGED_AFTER_SEAL"
        with self.assertRaisesRegex(ProtocolValidationError, "canonical payload"):
            validate_datapass(
                value,
                envelope=envelope,
                worker_artifacts={"independent-validator": bad_artifact},
            )

    def test_pass_fails_closed_without_verified_evidence(self) -> None:
        envelope = envelope_for("research_material_rights")
        with self.assertRaisesRegex(ProtocolValidationError, "PASS requires verified evidence quorum"):
            build_datapass_draft(
                envelope=envelope,
                machine_recommendation="PASS",
                reason_codes=["UNSAFE_PASS"],
                recommendation_summary="不应通过",
                evidence_status="PARTIAL",
                evidence_quorum_met=False,
                semantic_status="RESOLVED",
                impact_status="NOT_APPLICABLE",
                impact_facts={},
                impact_metrics=[],
                required_worker_ids=[],
                worker_receipts=[],
                required_skill_ids=[],
                skill_invocations=[],
                generated_at_utc=NOW,
            )

    def test_three_existing_cases_and_datapasses_migrate_without_fabrication(self) -> None:
        if not (ROOT / "agentteams" / "cases").is_dir():
            self.skipTest(
                "legacy fixed-case fixtures are intentionally absent from the public repository"
            )
        for asset in ("equity", "futures", "option"):
            legacy_case = json.loads(
                (ROOT / "agentteams" / "cases" / f"{asset}_case.json").read_text(
                    encoding="utf-8"
                )
            )
            envelope = adapt_legacy_case_envelope(
                legacy_case,
                created_at_utc=NOW,
                source_base=ROOT,
            )
            self.assertEqual(envelope["profile"]["profile_id"], PROFILE_BY_ASSET[asset])
            self.assertEqual(envelope["legacy_reference"]["migration_status"], "BOUNDED_PROJECTION")
            self.assertEqual(
                envelope["legacy_reference"]["content_hash_resolution"],
                "COMPUTED_FROM_LOCAL_EVIDENCE",
            )
            self.assertIn("truth_boundary", envelope["legacy_reference"]["unmapped_fields"])
            legacy_datapass = generate_datapass(asset)
            datapass = adapt_legacy_datapass(
                legacy_datapass,
                envelope=envelope,
                generated_at_utc=NOW,
            )
            self.assertEqual(datapass["legacy_reference"]["source_sha256"], canonical_sha256(legacy_datapass))
            self.assertEqual(datapass["workers"]["attestation_status"], "NOT_AVAILABLE")
            validate_datapass(datapass, envelope=envelope)

    def test_schema_files_are_strict_and_parseable(self) -> None:
        for name in (
            "case-envelope-v0.2.schema.json",
            "datapass-v0.2.schema.json",
            "profile-registry-v0.2.schema.json",
        ):
            schema = json.loads(
                (ROOT / "agentteams" / "protocols" / name).read_text(encoding="utf-8")
            )
            self.assertFalse(schema["additionalProperties"], name)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
