from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_intake import (
    AGENTTEAMS_ACCEPTANCE_FIELDS,
    LiveIntakeRepository,
    _json_atomic,
)


CSV = b"instrument,trade_date,close,settle\nIF2608,2026-08-14,4648.4,4652.4\n"


def metadata() -> dict:
    return {
        "profile": "futures_settlement",
        "declared_source": "CFFEX public market data",
        "rights_basis": "public data for competition POC",
        "declared_purpose": "daily_settlement_pnl",
        "provider": "test upload",
        "candidate_mapping": "close",
        "target_instrument": "IF2608",
        "contract_multiplier": 300,
        "multiplier_source": "contract specification",
    }


class V02ProjectionAndExportGateTests(unittest.TestCase):
    def test_attach_projects_every_strict_acceptance_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = LiveIntakeRepository(Path(temp))
            submission = repo.create_submission("if2608.csv", CSV, metadata())
            run = repo.create_run(submission["submission_id"], {"connected": True})
            adapter_run = {
                "run_id": run["run_id"],
                "state": "SUBMITTED",
                "trace": [{"event_id": "$manager"}],
                "leader_room_id": "!leader:test",
            }
            for index, field in enumerate(AGENTTEAMS_ACCEPTANCE_FIELDS, start=1):
                adapter_run[field] = {"field": field, "sequence": index}
            projected = repo.attach_agentteams(run["run_id"], adapter_run)
            for field in AGENTTEAMS_ACCEPTANCE_FIELDS:
                self.assertEqual(projected[field], adapter_run[field])
            self.assertEqual(projected["agentteams_trace"], adapter_run["trace"])
            self.assertEqual(
                projected["agentteams"]["leader_room_id"], "!leader:test"
            )

    def test_native_v02_cannot_compose_final_from_human_state_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = LiveIntakeRepository(Path(temp))
            submission = repo.create_submission("if2608.csv", CSV, metadata())
            run = repo.create_run(submission["submission_id"], {"connected": True})
            run["state"] = "COMPLETED"
            run["human_gate"].update(
                {
                    "state": "APPROVED",
                    "decision": "APPROVE_PASS",
                    "human_actor_id": "@reviewer:test",
                    "decided_at": "2026-09-01T00:00:00+00:00",
                }
            )
            _json_atomic(repo.runs / f"{run['run_id']}.json", run)
            with self.assertRaisesRegex(ValueError, "V0.2严格验收未通过"):
                repo.ensure_final_result(run["run_id"])


if __name__ == "__main__":
    unittest.main()
