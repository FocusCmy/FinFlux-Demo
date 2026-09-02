from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentteams_runtime.config import execution_policy
from agentteams_runtime.envelope import build_handle, build_transport
from bounded_worker_task import _live_result, _seal_worker_artifact
from context_capsule import load_role_context_slice
from live_intake import LiveIntakeRepository
from protocol_v02 import DATAPASS_PROTOCOL, validate_case_envelope, validate_datapass


CSV = b"instrument,trade_date,close,settle\nIF2608,2026-08-14,4648.4,4652.4\n"
INSPECTION_CSV = (
    b"instrument,trade_date,close,settle,candidate_mapping,business_purpose\n"
    b"IF2608,2026-08-14,4648.4,4652.4,settle,daily_settlement_pnl\n"
)


def metadata(mapping: str = "close") -> dict:
    return {
        "profile": "futures_settlement",
        "declared_source": "CFFEX public sample",
        "rights_basis": "public data for evaluation",
        "declared_purpose": "daily_settlement_pnl",
        "candidate_mapping": mapping,
        "contract_multiplier": 300,
        "multiplier_source": "contract specification",
    }


class LiveIntakePublicContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = LiveIntakeRepository(self.root / "live")
        self.context_root = self.root / "context"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upload_preserves_real_bytes_and_hash(self) -> None:
        submission = self.repo.create_submission("if2608.csv", CSV, metadata())
        stored = self.repo.root / submission["file"]["immutable_object"]
        self.assertEqual(stored.read_bytes(), CSV)
        self.assertEqual(len(submission["file"]["sha256"]), 64)
        self.assertFalse(submission["raw_evidence_mutated"])

    def test_live_mapping_confirmation_routes_agents_without_mutating_source(self) -> None:
        inspection = self.repo.inspect_file(
            "if2608-live.csv",
            INSPECTION_CSV,
            {"task_instruction": "核验逐日结算字段映射", "input_mode": "FILE_PLUS_INTENT"},
        )
        self.assertEqual(inspection["inferred"]["candidate_mapping"], "settle")
        self.assertEqual(inspection["token_usage"]["total_tokens"], 0)
        submission = self.repo.commit_inspection(
            inspection["inspection_id"],
            {
                "candidate_mapping": "close",
                "declared_source": "CFFEX public sample",
                "rights_basis": "public data for evaluation",
                "confidentiality_class": "PUBLIC",
                "contract_multiplier": 300,
                "entity_query": "IF2608",
                "task_instruction": "核验逐日结算字段映射",
            },
        )
        stored = self.repo.root / submission["file"]["immutable_object"]
        self.assertEqual(stored.read_bytes(), INSPECTION_CSV)
        self.assertEqual(submission["metadata"]["candidate_mapping"], "close")
        run = self.repo.create_run(submission["submission_id"], {"connected": True})
        self.assertEqual(run["precheck"]["machine_recommendation"], "BLOCK")
        self.assertEqual(run["root_route_decision"]["route"], "FULL_TEAM_REVIEW")

    def test_run_creation_is_idempotent_only_with_client_key(self) -> None:
        submission = self.repo.create_submission("if2608.csv", CSV, metadata())
        runtime = {"connected": False, "truthful_note": "offline"}
        first = self.repo.create_run(submission["submission_id"], runtime, "judge-upload-0001")
        replay = self.repo.create_run(submission["submission_id"], runtime, "judge-upload-0001")
        fresh = self.repo.create_run(submission["submission_id"], runtime)
        self.assertEqual(first["run_id"], replay["run_id"])
        self.assertNotEqual(first["run_id"], fresh["run_id"])

    def test_transport_contains_only_three_core_workers_and_hash_handles(self) -> None:
        submission = self.repo.create_submission("if2608.csv", CSV, metadata())
        run = self.repo.create_run(submission["submission_id"], {"connected": True})
        with patch("agentteams_runtime.envelope.CONTEXT_ROOT", self.context_root):
            transport = build_transport(submission, run, execution_policy())
        handle = build_handle(transport)
        self.assertEqual(
            transport["required_workers"],
            ["evidence-investigator", "semantic-impact-analyst", "independent-validator"],
        )
        self.assertEqual(set(handle["task_identity"]["task_ids"]), set(transport["required_workers"]))
        self.assertNotIn("parsed", transport)
        self.assertNotIn("close", str(handle))
        validate_case_envelope(run["case_envelope"])

    def test_sealed_worker_artifacts_build_formal_datapass(self) -> None:
        submission = self.repo.create_submission("if2608.csv", CSV, metadata())
        run = self.repo.create_run(submission["submission_id"], {"connected": True})
        with patch("agentteams_runtime.envelope.CONTEXT_ROOT", self.context_root):
            transport = build_transport(submission, run, execution_policy())
        artifacts = {}
        task_ids = build_handle(transport)["task_identity"]["task_ids"]
        for role in transport["required_workers"]:
            ref = transport["context_capsule_handle"]["role_slice_handles"][role]["slice_sha256"]
            payload = load_role_context_slice(
                ref, role, case_id=run["case_id"], run_id=run["run_id"], root=self.context_root
            )
            artifact = _live_result(
                role,
                run["case_id"],
                run["run_id"],
                task_ids[role],
                run["execution_policy_id"],
                payload,
            )
            artifacts[role] = _seal_worker_artifact(artifact)
        agent_run = {
            "run_id": run["run_id"],
            "state": "RUNNING",
            "case_envelope": transport,
            "formal_case_envelope_handle": transport["formal_case_envelope_handle"],
            "trace": [],
            "budget": {},
            "provider_usage": {},
            "agent_result": {
                "leader_recommendation": "BLOCK",
                "leader_datapass_event_id": "$leader",
                "worker_artifacts": artifacts,
                "workers_completed": 3,
            },
        }
        synchronized = self.repo.sync_agentteams(run["run_id"], agent_run)
        self.assertEqual(synchronized["datapass"]["protocol"], DATAPASS_PROTOCOL)
        validate_datapass(
            synchronized["datapass"],
            envelope=synchronized["case_envelope"],
            worker_artifacts=artifacts,
        )


if __name__ == "__main__":
    unittest.main()
