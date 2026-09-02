from __future__ import annotations

import unittest

from run_lifecycle import bootstrap_lifecycle, record_transition


class RunLifecycleTests(unittest.TestCase):
    def test_happy_path_is_append_only_and_hash_bound(self) -> None:
        run = {
            "run_id": "RUN-LIFECYCLE-1",
            "state": "READY_FOR_AGENTTEAMS",
            "human_gate": {"state": "NOT_OPENED"},
        }
        bootstrap_lifecycle(run)
        self.assertEqual(run["lifecycle"]["current_phase"], "READY_FOR_DISPATCH")
        for raw, target in (
            ("AGENTTEAMS_SUBMITTED", "DISPATCHED"),
            ("ACTIVE", "WORKERS_RUNNING"),
            ("COMPLETED", "DATAPASS_DRAFTED"),
            ("AWAITING_HUMAN", "AWAITING_HUMAN"),
        ):
            record_transition(run, raw, actor="test", reason=raw)
            self.assertEqual(run["lifecycle"]["current_phase"], target)
        run["human_gate"]["state"] = "APPROVED"
        record_transition(
            run,
            "COMPLETED",
            actor="human:test",
            reason="approved",
            target_phase="APPROVED",
        )
        self.assertTrue(run["lifecycle"]["terminal"])
        self.assertEqual(len(run["lifecycle"]["history"][-1]["transition_sha256"]), 64)

    def test_illegal_terminal_reopen_is_rejected(self) -> None:
        run = {
            "run_id": "RUN-LIFECYCLE-2",
            "state": "REJECT_AT_INTAKE",
            "human_gate": {"state": "NOT_OPENED"},
        }
        bootstrap_lifecycle(run)
        self.assertEqual(run["lifecycle"]["current_phase"], "BLOCKED")
        with self.assertRaises(ValueError):
            record_transition(
                run,
                "AGENTTEAMS_SUBMITTED",
                actor="test",
                reason="must not bypass a terminal block",
            )

    def test_change_bundle_can_escalate_code_only_pass(self) -> None:
        run = {
            "run_id": "RUN-LIFECYCLE-3",
            "state": "CODE_ONLY_PRECHECK",
            "human_gate": {"state": "NOT_OPENED"},
        }
        bootstrap_lifecycle(run)
        record_transition(
            run,
            "READY_FOR_AGENTTEAMS",
            actor="global-manager",
            reason="new ChangeBundle expands blast radius",
        )
        self.assertEqual(run["lifecycle"]["current_phase"], "READY_FOR_DISPATCH")


if __name__ == "__main__":
    unittest.main()
