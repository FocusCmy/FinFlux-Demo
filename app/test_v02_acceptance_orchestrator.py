from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import v02_acceptance_orchestrator as orchestrator
except ModuleNotFoundError:
    from app import v02_acceptance_orchestrator as orchestrator


class V02AcceptanceOrchestratorTests(unittest.TestCase):
    def test_preflight_block_never_launches_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "preflight",
            return_value={"status": "BLOCKED", "reasons": ["TOKEN_GUARD_BLOCKED"]},
        ), mock.patch.object(orchestrator.acceptance, "launch") as launch:
            result = orchestrator.run_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                execute_model=True,
            )
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(0, result["model_runs_created"])
        launch.assert_not_called()

    def test_one_launch_waits_for_human_then_finalizes(self) -> None:
        snapshots = [
            {
                "run": {"run_id": "RUN-1", "state": "RUNNING"},
                "validation": {"status": "PENDING"},
            },
            {
                "run": {"run_id": "RUN-1", "state": "AWAITING_HUMAN"},
                "validation": {"status": "PASS", "worker_artifact_count": 3},
            },
            {
                "run": {"run_id": "RUN-1", "state": "COMPLETED"},
                "validation": {"status": "PASS", "worker_artifact_count": 3},
            },
        ]
        clock = iter(range(20))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "preflight",
            return_value={"status": "READY", "snapshot_sha256": "a" * 64},
        ), mock.patch.object(
            orchestrator.acceptance,
            "launch",
            return_value={"run": {"run_id": "RUN-1"}},
        ) as launch, mock.patch.object(
            orchestrator.acceptance,
            "status",
            side_effect=snapshots,
        ), mock.patch.object(
            orchestrator.acceptance,
            "finalize",
            return_value={"status": "PASS", "receipt_sha256": "b" * 64},
        ) as finalize:
            result = orchestrator.run_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                execute_model=True,
                poll_interval_seconds=0.01,
                sleep=lambda _: None,
                monotonic=lambda: float(next(clock)),
            )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["model_runs_created"])
        self.assertFalse(result["human_decision_automated"])
        launch.assert_called_once()
        finalize.assert_called_once()

    def test_active_projection_gap_snapshot_does_not_end_acceptance(self) -> None:
        snapshots = [
            {
                "run": {"run_id": "RUN-ASYNC", "state": "RUNNING"},
                # Compatibility with a status snapshot produced before the
                # validator began classifying async projection gaps PENDING.
                "validation": {
                    "status": "FAIL",
                    "failures": [
                        "AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING",
                        "MANAGER_TO_LEADER_FINAL_EVENT_BINDING_MISSING",
                    ],
                },
            },
            {
                "run": {"run_id": "RUN-ASYNC", "state": "COMPLETED"},
                "validation": {"status": "PASS"},
            },
        ]
        clock = iter(range(20))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "preflight",
            return_value={"status": "READY", "snapshot_sha256": "a" * 64},
        ), mock.patch.object(
            orchestrator.acceptance,
            "launch",
            return_value={"run": {"run_id": "RUN-ASYNC"}},
        ), mock.patch.object(
            orchestrator.acceptance,
            "status",
            side_effect=snapshots,
        ) as status, mock.patch.object(
            orchestrator.acceptance,
            "finalize",
            return_value={"status": "PASS", "receipt_sha256": "b" * 64},
        ) as finalize:
            result = orchestrator.run_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                execute_model=True,
                poll_interval_seconds=0.01,
                sleep=lambda _: None,
                monotonic=lambda: float(next(clock)),
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(status.call_count, 2)
        finalize.assert_called_once()

    def test_exhausted_leader_without_artifacts_is_emergency_stopped(self) -> None:
        stalled = {
            "run": {
                "run_id": "RUN-STALLED",
                "state": "RUNNING",
                "prompt_budget_readiness": {
                    "runtime_readbacks": [
                        {"role": "manager", "max_iters": 1},
                        {"role": "finchange-case-lead", "max_iters": 2},
                    ]
                },
                "provider_usage": {
                    "by_agent": [
                        {
                            "agent_id": "global-manager",
                            "role": "manager",
                            "call_count": 1,
                        },
                        {
                            "agent_id": "finchange-case-lead",
                            "role": "team_leader",
                            "call_count": 2,
                        },
                    ]
                },
            },
            "validation": {
                "status": "PENDING",
                "worker_artifact_count": 0,
                "skill_invocation_count": 0,
            },
        }
        stop_response = {
            "run": {"run_id": "RUN-STALLED", "state": "STOPPED_BY_GATE"},
            "emergency_stop_record": {"record_sha256": "e" * 64},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "preflight",
            return_value={"status": "READY"},
        ), mock.patch.object(
            orchestrator.acceptance,
            "launch",
            return_value={"run": {"run_id": "RUN-STALLED"}},
        ) as launch, mock.patch.object(
            orchestrator.acceptance, "status", return_value=stalled
        ), mock.patch.object(
            orchestrator.acceptance, "api_json", return_value=stop_response
        ) as api_json, mock.patch.object(
            orchestrator.acceptance, "finalize"
        ) as finalize:
            clock = iter([0.0, 1.0])
            result = orchestrator.run_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                execute_model=True,
                sleep=lambda _: None,
                monotonic=lambda: next(clock),
            )
        self.assertEqual(result["phase"], "PROGRESS_WATCHDOG")
        self.assertEqual(result["run_state"], "STOPPED_BY_GATE")
        self.assertEqual(result["watchdog_model_calls"], 0)
        self.assertEqual(result["emergency_stop_record_sha256"], "e" * 64)
        launch.assert_called_once()
        finalize.assert_not_called()
        _base_url, path, payload = api_json.call_args.args[:3]
        self.assertTrue(path.endswith("/RUN-STALLED/emergency-stop"))
        self.assertEqual(payload["actor"], "system:v02-progress-watchdog")
        self.assertIn(
            "ROLE_MODEL_BUDGET_EXHAUSTED_WITHOUT_ARTIFACT",
            payload["reason"],
        )

    def test_observed_mismatch_and_terminal_gap_still_fail_closed(self) -> None:
        self.assertTrue(
            orchestrator._requires_fail_closed(
                "RUNNING",
                {
                    "status": "FAIL",
                    "failures": ["AUTHORIZED_LEADER_RELAY_TRACE_EVENT_INVALID"],
                },
            )
        )
        self.assertTrue(
            orchestrator._requires_fail_closed(
                "AWAITING_HUMAN",
                {
                    "status": "FAIL",
                    "failures": ["AUTHORIZED_LEADER_RELAY_TRACE_EVENT_MISSING"],
                },
            )
        )
        self.assertTrue(
            orchestrator._requires_fail_closed(
                "BUDGET_EXCEEDED",
                {
                    "status": "PENDING",
                    "failures": [],
                },
            )
        )

    def test_explicit_model_authorization_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "explicit"):
                orchestrator.run_acceptance(
                    base_url="http://127.0.0.1:8768",
                    session_file=Path(tmp) / "session.json",
                    output_dir=Path(tmp) / "evidence",
                )

    def test_resume_existing_session_never_launches_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "load_session",
            return_value={"run_id": "RUN-EXISTING", "submission_id": "SUB-1"},
        ), mock.patch.object(
            orchestrator.acceptance,
            "status",
            return_value={
                "run": {"run_id": "RUN-EXISTING", "state": "COMPLETED"},
                "validation": {"status": "PASS"},
            },
        ), mock.patch.object(
            orchestrator.acceptance,
            "finalize",
            return_value={"status": "PASS", "receipt_sha256": "c" * 64},
        ) as finalize, mock.patch.object(
            orchestrator.acceptance, "preflight"
        ) as preflight, mock.patch.object(
            orchestrator.acceptance, "launch"
        ) as launch:
            result = orchestrator.resume_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                sleep=lambda _: None,
            )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(0, result["model_runs_created"])
        self.assertTrue(result["resumed_existing_session"])
        preflight.assert_not_called()
        launch.assert_not_called()
        finalize.assert_called_once()

    def test_resume_reconciles_unknown_response_without_creating_second_run(self) -> None:
        uncertain = {
            "status": "RUN_CREATE_OUTCOME_UNKNOWN",
            "run_id": None,
            "submission_id": "SUB-1",
            "client_idempotency_key": "FVA-persisted-key-000001",
        }
        recovered_session = {
            **uncertain,
            "status": "RUN_CREATE_RESPONSE_RECOVERED",
            "run_id": "RUN-RECOVERED",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orchestrator.acceptance,
            "load_session",
            return_value=uncertain,
        ), mock.patch.object(
            orchestrator.acceptance,
            "reconcile_session_run_creation",
            return_value={
                "status": "COMMITTED",
                "session": recovered_session,
                "run": {"run_id": "RUN-RECOVERED"},
                "creates_run": False,
                "dispatches_model": False,
            },
        ) as reconcile, mock.patch.object(
            orchestrator.acceptance,
            "status",
            return_value={
                "run": {"run_id": "RUN-RECOVERED", "state": "COMPLETED"},
                "validation": {"status": "PASS"},
            },
        ), mock.patch.object(
            orchestrator.acceptance,
            "finalize",
            return_value={"status": "PASS", "receipt_sha256": "d" * 64},
        ), mock.patch.object(
            orchestrator.acceptance, "launch"
        ) as launch:
            result = orchestrator.resume_acceptance(
                base_url="http://127.0.0.1:8768",
                session_file=Path(tmp) / "session.json",
                output_dir=Path(tmp) / "evidence",
                sleep=lambda _: None,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["run_id"], "RUN-RECOVERED")
        self.assertEqual(result["model_runs_created"], 0)
        reconcile.assert_called_once()
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
