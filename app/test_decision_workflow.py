from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from live_intake import LiveIntakeRepository, _json_atomic


CSV = b"instrument,trade_date,close,settle\nIF2608,2026-08-14,4648.4,4652.4\n"


def metadata(mapping: str = "close") -> dict:
    return {
        "profile": "futures_settlement",
        "declared_source": "CFFEX public market data",
        "rights_basis": "public data for competition POC",
        "declared_purpose": "daily_settlement_pnl",
        "provider": "test upload",
        "candidate_mapping": mapping,
        "target_instrument": "IF2608",
        "contract_multiplier": 300,
        "multiplier_source": "contract specification",
    }


class DecisionWorkflowTests(unittest.TestCase):
    def test_pending_result_is_automatic_zero_model_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = LiveIntakeRepository(Path(temp))
            submission = repo.create_submission("if2608.csv", CSV, metadata("close"))
            run = repo.create_run(submission["submission_id"], {"connected": True})
            # This unit test isolates the deterministic Result Composer. Native
            # V0.2 export is covered by the strict live-acceptance suite and
            # deliberately requires real Manager/Worker/Token/Human receipts.
            run["protocol"] = "FINFLUX_LEGACY_REPORT_UNIT_V0.1"
            run.update(
                {
                    "state": "AWAITING_HUMAN",
                    "datapass": {
                        "machine_recommendation": "BLOCK",
                        "draft_sha256": "d" * 64,
                        "skill_invocations": [],
                    },
                    "provider_usage": {
                        "status": "PROVIDER_REPORTED",
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "call_count": 4,
                        "source": "provider-usage-ledger",
                    },
                    "agent_result": {
                        "leader_recommendation": "BLOCK",
                        "workers_completed": 3,
                        "workers_required": 3,
                        "worker_artifacts": {
                            "evidence-investigator": {},
                            "semantic-impact-analyst": {},
                            "independent-validator": {},
                        },
                    },
                }
            )
            run["human_gate"]["state"] = "AWAITING_HUMAN"
            _json_atomic(repo.runs / f"{run['run_id']}.json", run)

            preview = repo.ensure_report_preview(run["run_id"])
            composer = preview["composer"]
            self.assertEqual(composer["agent_id"], "result-composer")
            self.assertFalse(composer["strategy"]["model_called"])
            self.assertEqual(composer["strategy"]["provider_tokens"], 0)
            self.assertEqual(len(composer["skill_invocations"]), 4)
            self.assertEqual(preview["outcome"]["code"], "HUMAN_ACTION_REQUIRED")
            for kind in ("markdown", "pdf", "json"):
                self.assertTrue(repo.preview_artifact_path(run["run_id"], kind).is_file())
            registered = repo.list_skills(repo.get_run(run["run_id"]))
            self.assertEqual(len(registered), 20)
            self.assertEqual(
                sum(item["owner_role"] == "Result Composer Agent" for item in registered),
                4,
            )
            self.assertEqual(
                sum(
                    item["skill_id"]
                    in {
                        "detect-version-change",
                        "resolve-downstream-lineage",
                        "validate-remediation-plan",
                    }
                    for item in registered
                ),
                3,
            )

    def test_human_remediation_creates_full_team_child_without_mutating_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = LiveIntakeRepository(Path(temp))
            submission = repo.create_submission("if2608.csv", CSV, metadata("close"))
            parent = repo.create_run(submission["submission_id"], {"connected": True})
            parent["human_gate"].update(
                {
                    "state": "REJECTED",
                    "decision": "CONFIRM_BLOCK",
                    "human_actor_id": "reviewer01",
                    "decided_at": "2026-08-29T12:00:00+00:00",
                }
            )
            _json_atomic(repo.runs / f"{parent['run_id']}.json", parent)

            child = repo.create_remediation_child(parent["run_id"], {"connected": True})
            revised = repo.get_submission(child["submission_id"])

            self.assertEqual(child["precheck"]["machine_recommendation"], "PASS")
            self.assertEqual(
                child["root_route_decision"]["route"], "FULL_TEAM_REVIEW"
            )
            self.assertIn(
                "HUMAN_REMEDIATION_REVIEW",
                child["root_route_decision"]["reason_codes"],
            )
            self.assertEqual(revised["metadata"]["candidate_mapping"], "settle")
            self.assertEqual(
                revised["file"]["sha256"], submission["file"]["sha256"]
            )
            self.assertTrue(child["lineage"]["raw_evidence_sha256_unchanged"])

    def test_signed_result_exports_markdown_pdf_json_and_hash_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = LiveIntakeRepository(Path(temp))
            submission = repo.create_submission("if2608.csv", CSV, metadata("settle"))
            run = repo.create_run(submission["submission_id"], {"connected": True})
            run["protocol"] = "FINFLUX_LEGACY_REPORT_UNIT_V0.1"
            invocations = [
                {"skill_id": f"skill-{index}", "version": "1.0.0"}
                for index in range(5)
            ]
            run.update(
                {
                    "agentteams_run_id": run["run_id"],
                    "state": "COMPLETED",
                    "datapass": {
                        "machine_recommendation": "PASS",
                        "datapass_sha256": "d" * 64,
                    },
                    "provider_usage": {
                        "status": "PROVIDER_REPORTED",
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "call_count": 4,
                        "source": "provider-usage-ledger",
                    },
                    "agent_result": {
                        "result_source": "MATRIX",
                        "leader_recommendation": "PASS",
                        "leader_datapass_event_id": "$leader",
                        "workers_completed": 3,
                        "workers_required": 3,
                        "human_decision": {"event_id": "$human"},
                        "worker_artifacts": {
                            "evidence-investigator": {
                                "skill_invocations": invocations[:2]
                            },
                            "semantic-impact-analyst": {
                                "skill_invocations": invocations[2:4]
                            },
                            "independent-validator": {
                                "skill_invocations": invocations[4:]
                            },
                        },
                    },
                }
            )
            run["human_gate"].update(
                {
                    "state": "APPROVED",
                    "decision": "APPROVE_PASS",
                    "human_actor_id": "reviewer01",
                    "reason": "证据、语义契约和独立复核均通过",
                    "decided_at": "2026-08-29T12:10:00+00:00",
                    "post_decision_hash": "h" * 64,
                }
            )
            _json_atomic(repo.runs / f"{run['run_id']}.json", run)

            result = repo.ensure_final_result(run["run_id"])
            self.assertEqual(result["outcome"]["code"], "ADMITTED")
            for kind in ("markdown", "pdf", "json"):
                path = repo.result_artifact_path(run["run_id"], kind)
                self.assertTrue(path.is_file())
                expected = result["manifest"]["files"][kind]["sha256"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertTrue(
                repo.result_artifact_path(run["run_id"], "pdf")
                .read_bytes()
                .startswith(b"%PDF")
            )
            markdown = repo.result_artifact_path(run["run_id"], "markdown").read_text(
                encoding="utf-8"
            )
            self.assertIn("能不能用", markdown)
            self.assertIn("真实多Agent协作证据", markdown)
            self.assertIn("d" * 64, markdown)

            # A persisted manifest is not enough: every subsequent export
            # re-verifies the signed bytes and fails closed on corruption.
            json_path = repo.result_artifact_path(run["run_id"], "json")
            json_path.write_bytes(json_path.read_bytes() + b"\nTAMPERED")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                repo.ensure_final_result(run["run_id"])


if __name__ == "__main__":
    unittest.main()
