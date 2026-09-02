from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_plane import ControlPlaneSupervisor


class ControlPlaneTests(unittest.TestCase):
    def test_disconnected_runtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            supervisor = ControlPlaneSupervisor(Path(root))
            value = supervisor.inspect({"connected": False, "topology": []}, None)
        self.assertEqual(value["admission_state"], "DEGRADED_NO_DISPATCH")
        self.assertEqual(value["provider_tokens"], 0)

    def test_active_run_is_protected_and_checkpoint_persists(self) -> None:
        runtime = {
            "connected": True,
            "topology": [{"name": "manager", "phase": "RUNNING"}],
        }
        run = {
            "run_id": "RUN-1",
            "state": "AWAITING_HUMAN",
            "lifecycle": {"history_sha256": "a" * 64},
        }
        with tempfile.TemporaryDirectory() as root:
            supervisor = ControlPlaneSupervisor(Path(root))
            value = supervisor.reconcile(runtime, run)
            self.assertTrue((Path(root) / "control_plane" / "latest.json").is_file())
        self.assertEqual(value["admission_state"], "HOLD_ACTIVE_RUN")
        self.assertEqual(value["active_run_id"], "RUN-1")
        self.assertEqual(value["selected_run_id"], "RUN-1")
        self.assertTrue(value["durable_checkpoint_present"])

    def test_token_guard_is_authoritative_over_selected_precheck(self) -> None:
        runtime = {
            "connected": True,
            "topology": [{"name": "manager", "phase": "RUNNING"}],
        }
        selected = {"run_id": "RUN-PRECHECK", "state": "CODE_ONLY_PRECHECK"}
        guard = {
            "status": "BLOCKED",
            "allowed": False,
            "active_run_id": "RUN-JUDGE",
            "active_run_state": "AWAITING_HUMAN",
            "active_run_provider_tokens": 601969,
            "reasons": ["ACTIVE_RUN_EXISTS:RUN-JUDGE"],
        }
        with tempfile.TemporaryDirectory() as root:
            value = ControlPlaneSupervisor(Path(root)).inspect(runtime, selected, guard)
        self.assertEqual(value["admission_state"], "BLOCKED_BY_TOKEN_GUARD")
        self.assertEqual(value["active_run_id"], "RUN-JUDGE")
        self.assertEqual(value["selected_run_id"], "RUN-PRECHECK")
        self.assertEqual(value["admission_source"], "PROVIDER_TOKEN_GUARD")
        self.assertFalse(value["token_guard"]["allowed"])

    def test_closed_selected_run_is_not_reported_as_active_when_guard_has_none(self) -> None:
        runtime = {
            "connected": True,
            "topology": [{"name": "manager", "phase": "RUNNING"}],
        }
        selected = {
            "run_id": "RUN-HISTORICAL",
            "state": "BUDGET_EXCEEDED",
            "lifecycle": {"history_sha256": "b" * 64},
        }
        guard = {
            "status": "BLOCKED",
            "allowed": False,
            "active_run_id": None,
            "active_run_state": None,
            "reasons": ["DAILY_TOKEN_RESERVE_INSUFFICIENT:0<120000"],
        }
        with tempfile.TemporaryDirectory() as root:
            value = ControlPlaneSupervisor(Path(root)).inspect(runtime, selected, guard)
        self.assertIsNone(value["active_run_id"])
        self.assertEqual(value["active_run_state"], "NO_ACTIVE_RUN")
        self.assertEqual(value["selected_run_id"], "RUN-HISTORICAL")
        self.assertEqual(value["checkpoint_run_id"], "RUN-HISTORICAL")
        self.assertEqual(value["admission_state"], "BLOCKED_BY_TOKEN_GUARD")


if __name__ == "__main__":
    unittest.main()
