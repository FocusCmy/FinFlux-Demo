from __future__ import annotations

import unittest
from unittest import mock

import app as demo_app


class OperatorOccupancyReleaseTests(unittest.TestCase):
    def test_release_fail_closes_active_run_without_creating_result(self) -> None:
        run_id = "RUN-LIVE-OCCUPANCY-TEST-1"
        queued_id = "RUN-LIVE-QUEUED-TEST-2"
        active = {"run_id": run_id, "state": "RUNNING", "case_id": "CASE-1"}
        stopped = {
            **active,
            "state": "STOPPED_BY_GATE",
            "emergency_stop_record": {"record_sha256": "e" * 64},
        }
        with mock.patch.object(
            demo_app, "get_active_agent_run", return_value=active
        ), mock.patch.object(
            demo_app, "stop_agent_run_to_wait", return_value=stopped
        ) as stop_run, mock.patch.object(
            demo_app.LIVE_REPOSITORY,
            "sync_agentteams",
            return_value={"run_id": run_id, "state": "STOPPED_BY_GATE"},
        ) as sync, mock.patch.object(
            demo_app.LIVE_REPOSITORY,
            "next_dispatch_request",
            return_value={"run_id": queued_id},
        ):
            receipt = demo_app.release_active_run_occupancy(
                run_id,
                actor="demo.operator",
                reason="现场验证：Worker长期无产物，人工释放单Run门禁",
            )

        self.assertEqual(receipt["status"], "RELEASED_TO_WAIT")
        self.assertEqual(receipt["released_run_state"], "STOPPED_BY_GATE")
        self.assertEqual(receipt["next_queued_run_id"], queued_id)
        self.assertFalse(receipt["model_called_by_release"])
        self.assertFalse(receipt["datapass_created_by_release"])
        self.assertFalse(receipt["human_decision_created_by_release"])
        stop_run.assert_called_once()
        sync.assert_called_once_with(run_id, stopped)

    def test_awaiting_human_cannot_be_released_as_a_stalled_run(self) -> None:
        run_id = "RUN-LIVE-AWAITING-HUMAN-TEST-1"
        with mock.patch.object(
            demo_app,
            "get_active_agent_run",
            return_value={"run_id": run_id, "state": "AWAITING_HUMAN"},
        ):
            with self.assertRaisesRegex(ValueError, "Human Gate"):
                demo_app.release_active_run_occupancy(
                    run_id,
                    actor="demo.operator",
                    reason="错误尝试：释放已经等待Human的Run",
                )


if __name__ == "__main__":
    unittest.main()
