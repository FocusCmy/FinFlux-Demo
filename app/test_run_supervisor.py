from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from agentteams_runtime.service import AgentTeamsService
from agentteams_runtime.store import RunStore
from emergency_stop import EmergencyStopLedger, validate_emergency_stop_record
from run_supervisor import RunSupervisor


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class RunSupervisorTests(unittest.TestCase):
    def build(self, agent_run: dict):
        monotonic = Clock()
        wall = Clock(1_000.0)
        repository = Mock()
        repository.sync_agentteams.return_value = {
            "run_id": agent_run["run_id"],
            "state": agent_run.get("state"),
            "datapass": agent_run.get("datapass"),
        }
        get_active = Mock(return_value={"run_id": agent_run["run_id"]})
        get_run = Mock(return_value=agent_run)
        wake = Mock(return_value={"status": "MANAGER_REAWAKENED"})
        dispatch = Mock(return_value={
            "status": "MISSING_WORKERS_REAWAKENED",
            "missing_workers": ["semantic-impact-analyst"],
        })
        stop = Mock(return_value={
            **agent_run,
            "state": "STOPPED_BY_GATE",
            "emergency_stop_record": {"record_sha256": "x"},
        })
        supervisor = RunSupervisor(
            repository=repository,
            get_active_run=get_active,
            get_run=get_run,
            wake_manager=wake,
            dispatch_missing_workers=dispatch,
            stop_wait=stop,
            interval_seconds=1,
            manager_timeout_seconds=10,
            worker_timeout_seconds=20,
            max_runtime_seconds=600,
            max_recoveries=3,
            monotonic=monotonic,
            wall_clock=wall,
        )
        return supervisor, monotonic, repository, get_run, wake, dispatch, stop

    @staticmethod
    def base_run(**fields):
        run = {
            "run_id": "RUN-LIVE-TEST-001",
            "submitted_at_ms": 900_000,
            "state": "RUNNING",
            "manager_dispatch_receipt": {"status": "MANAGER_WAKEUP_SENT"},
            "leader_relay": {"status": "NOT_SENT"},
            "agent_result": {
                "workers_completed": 0,
                "workers_required": 3,
                "worker_artifacts": {},
                "leader_recommendation": "PENDING",
            },
            "provider_usage": {"call_count": 0, "total_tokens": 0},
            "trace": [],
            "supervisor_recovery_attempts": [],
            "manager_supervisor_wakeups": [],
        }
        run.update(fields)
        return run

    def test_syncs_without_an_http_request_and_stops_at_human(self):
        run = self.base_run(
            state="AWAITING_HUMAN",
            datapass={"datapass_sha256": "sealed"},
        )
        supervisor, _, repository, get_run, _, _, _ = self.build(run)
        status = supervisor.step()
        get_run.assert_called_once_with(run["run_id"])
        repository.sync_agentteams.assert_called_once()
        repository.ensure_report_preview.assert_called_once_with(run["run_id"])
        self.assertEqual(status["last_action"], "WAITING_FOR_HUMAN")

    def test_manager_is_reawakened_only_after_timeout(self):
        run = self.base_run()
        supervisor, clock, _, _, wake, _, _ = self.build(run)
        supervisor.step()
        wake.assert_not_called()
        clock.value = 11
        status = supervisor.step()
        wake.assert_called_once()
        self.assertEqual(status["last_action"], "MANAGER_REAWAKENED")

    def test_explicit_queued_run_is_dispatched_when_active_gate_is_free(self):
        repository = Mock()
        queued = {"run_id": "RUN-LIVE-QUEUED-001"}
        dispatch = Mock(
            return_value={
                "status": "BACKGROUND_AGENTTEAMS_DISPATCHED",
                "run_state": "AGENTTEAMS_SUBMITTED",
                "attempt_count": 0,
            }
        )
        supervisor = RunSupervisor(
            repository=repository,
            get_active_run=Mock(return_value=None),
            get_run=Mock(),
            wake_manager=Mock(),
            dispatch_missing_workers=Mock(),
            stop_wait=Mock(),
            get_queued_run=Mock(return_value=queued),
            dispatch_queued_run=dispatch,
            interval_seconds=1,
        )
        status = supervisor.step()
        dispatch.assert_called_once_with(queued["run_id"])
        self.assertEqual(status["last_action"], "BACKGROUND_AGENTTEAMS_DISPATCHED")
        self.assertEqual(status["run_state"], "AGENTTEAMS_SUBMITTED")

    def test_only_missing_workers_are_dispatched_after_stall(self):
        run = self.base_run(
            manager_dispatch_receipt={"status": "MANAGER_AUTHORIZED_DISPATCHED"},
            leader_relay={"status": "SENT"},
            agent_result={
                "workers_completed": 2,
                "workers_required": 3,
                "worker_artifacts": {
                    "evidence-investigator": {"artifact_sha256": "a"},
                    "independent-validator": {"artifact_sha256": "b"},
                },
                "leader_recommendation": "PENDING",
            },
        )
        supervisor, clock, _, _, _, dispatch, _ = self.build(run)
        supervisor.step()
        clock.value = 21
        status = supervisor.step()
        dispatch.assert_called_once()
        self.assertEqual(status["missing_workers"], ["semantic-impact-analyst"])

    def test_three_recoveries_fail_closed_to_wait(self):
        run = self.base_run(
            supervisor_recovery_attempts=[{"status": "X"}] * 3,
        )
        supervisor, _, repository, _, _, _, stop = self.build(run)
        status = supervisor.step()
        stop.assert_called_once()
        self.assertEqual(status["run_state"], "WAIT")
        self.assertEqual(status["last_action"], "FAILED_CLOSED_TO_WAIT")
        self.assertEqual(repository.sync_agentteams.call_count, 2)

    def test_authorized_controller_dispatches_only_missing_workers(self):
        run_id = "RUN-LIVE-SUPERVISOR-DISPATCH-1"
        authorization = "FINFLUX_MANAGER_DISPATCHED CASE-1 " + run_id + " key hash @leader:test"
        run = {
            "run_id": run_id,
            "case_id": "CASE-1",
            "state": "RUNNING",
            "expected_manager_authorization": authorization,
            "leader_room_id": "!leader:test",
            "matrix_handle": {
                "route": "FULL_TEAM_REVIEW",
                "handle_sha256": "h" * 64,
                "role_slice_sha256": {
                    "evidence-investigator": "e" * 64,
                    "semantic-impact-analyst": "s" * 64,
                    "independent-validator": "i" * 64,
                },
                "task_identity": {
                    "task_scope": "TEST",
                    "task_ids": {
                        "evidence-investigator": "task-evidence-0001",
                        "semantic-impact-analyst": "task-semantic-0001",
                        "independent-validator": "task-validator-0001",
                    },
                },
            },
            "case_envelope": {},
            "supervisor_recovery_attempts": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp))
            store.save(run)
            service = AgentTeamsService(store)
            matrix = Mock()
            matrix.ensure_joined.return_value = {"ready": True, "missing": []}
            matrix.mxid.side_effect = lambda role: "@" + role + ":test"
            matrix.send.side_effect = ["$semantic", "$validator"]
            trace = [{
                "event_id": "$manager-auth",
                "actor": {"role": "manager"},
                "body": authorization,
            }]
            sealed = {"evidence-investigator": {"artifact_sha256": "a" * 64}}
            with patch("agentteams_runtime.service.MatrixClient.admin", return_value=matrix), patch.object(
                service, "_trace", return_value=trace
            ), patch.object(service, "_artifacts", return_value=sealed):
                receipt = service.supervisor_dispatch_missing_workers(
                    run_id, "test-supervisor", "worker timeout"
                )
        self.assertEqual(receipt["status"], "MISSING_WORKERS_REAWAKENED")
        self.assertEqual(
            receipt["missing_workers"],
            ["semantic-impact-analyst", "independent-validator"],
        )
        self.assertEqual(matrix.send.call_count, 2)
        self.assertFalse(receipt["new_run_created"])

    def test_manager_supervisor_wakeup_is_single_shot(self):
        run_id = "RUN-LIVE-SUPERVISOR-MANAGER-1"
        run = {
            "run_id": run_id,
            "case_id": "CASE-1",
            "state": "RUNNING",
            "manager_room_id": "!manager:test",
            "manager_id": "@manager:test",
            "expected_manager_authorization": "FINFLUX_MANAGER_DISPATCHED expected",
            "dispatch_hash": "d" * 64,
            "matrix_handle": {"route": "FULL_TEAM_REVIEW", "handle_sha256": "h" * 64},
            "manager_supervisor_wakeups": [],
            "supervisor_recovery_attempts": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp))
            store.save(run)
            service = AgentTeamsService(store)
            matrix = Mock()
            matrix.send.return_value = "$manager-wakeup"
            with patch("agentteams_runtime.service.MatrixClient.admin", return_value=matrix), patch.object(
                service, "_trace", return_value=[]
            ), patch("agentteams_runtime.service.gateway_usage", return_value={"status": "ACTIVE"}), patch(
                "agentteams_runtime.service.extend_gateway_recovery_window",
                return_value={"status": "EXTENDED"},
            ), patch(
                "agentteams_runtime.service.rebind_gateway_same_run_actors",
                return_value={"status": "REBOUND"},
            ):
                first = service.supervisor_wake_manager(
                    run_id, "test-supervisor", "manager timeout"
                )
                second = service.supervisor_wake_manager(
                    run_id, "test-supervisor", "manager timeout"
                )
        self.assertEqual(first["status"], "MANAGER_REAWAKENED")
        self.assertEqual(second["status"], "MANAGER_WAKEUP_LIMIT_REACHED")
        self.assertEqual(matrix.send.call_count, 1)

    def test_supervisor_wait_is_hash_bound_and_creates_no_datapass(self):
        run_id = "RUN-LIVE-SUPERVISOR-WAIT-1"
        run = {
            "run_id": run_id,
            "case_id": "CASE-1",
            "state": "RUNNING",
            "case_envelope_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RunStore(root / "runs")
            store.save(run)
            service = AgentTeamsService(store)
            service.stop_ledger = EmergencyStopLedger(root / "stops")
            with patch(
                "agentteams_runtime.service.gateway_usage",
                return_value={"status": "ACTIVE", "total_tokens": 17},
            ), patch(
                "agentteams_runtime.service.close_gateway",
                return_value={"status": "CLOSED"},
            ):
                result = service.supervisor_wait(
                    run_id,
                    requested_by="finflux-run-supervisor",
                    reason="worker timeout",
                    reason_codes=["RECOVERY_LIMIT_REACHED"],
                )
        validate_emergency_stop_record(result["emergency_stop_record"])
        self.assertEqual(result["supervisor_outcome"]["decision"], "WAIT")
        self.assertFalse(result["supervisor_outcome"]["datapass_created"])
        self.assertNotIn("datapass", result)


if __name__ == "__main__":
    unittest.main()
