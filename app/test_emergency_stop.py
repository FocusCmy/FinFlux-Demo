from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentteams_adapter as adapter
from agentteams_runtime.envelope import sha256
from emergency_stop import (
    EmergencyStopLedger,
    canonical_sha256,
    progress_watchdog_decision,
    running_guard_decision,
)


def evidence_ref() -> dict:
    item = {
        "evidence_type": "RUNTIME_STATUS_SNAPSHOT",
        "action": "ALREADY_STOPPED_EXTERNALLY_REPORTED",
        "status": "CONTAINERS_NOT_RUNNING",
    }
    item["evidence_sha256"] = canonical_sha256(item)
    return item


class EmergencyStopTests(unittest.TestCase):
    @staticmethod
    def _closed_terminal_run(run_id: str) -> dict:
        binding_sha = "b" * 64
        actor_tasks = {
            "manager": "TASK-MANAGER",
            "finchange-case-lead": "TASK-LEADER",
        }
        cleanup = {
            "protocol": "FINFLUX_MODEL_ACTOR_IDENTITY_CLEANUP_V1",
            "status": "CLEARED",
            "run_id": run_id,
            "gateway_binding_sha256": binding_sha,
            "actor_task_ids": actor_tasks,
            "roles": [
                {"role": role, "finflux_headers_remaining": 0}
                for role in actor_tasks
            ],
            "finflux_headers_remaining": 0,
            "model_or_provider_called": False,
        }
        cleanup["receipt_sha256"] = sha256(cleanup)
        return {
            "run_id": run_id,
            "case_id": "FUTURES-TEST",
            "state": "BUDGET_EXCEEDED",
            "case_envelope_sha256": "a" * 64,
            "human_decision": None,
            "datapass": None,
            "provider_usage": {
                "status": "PROVIDER_REPORTED",
                "run_delta_tokens": 120001,
            },
            "model_gateway_binding": {
                "state": "CLOSED",
                "run_id": run_id,
                "binding_sha256": binding_sha,
                "actor_task_ids": actor_tasks,
            },
            "model_gateway_identity_cleanup": cleanup,
        }

    def test_ledger_is_append_only_hash_chained_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            ledger = EmergencyStopLedger(Path(root))
            kwargs = {
                "run_id": "RUN-LIVE-TEST-1",
                "case_id": "FUTURES-TEST",
                "terminal_state": "STOPPED_BY_GATE",
                "actor": "operator:test",
                "reason": "containers were already stopped externally",
                "token_guard_snapshot": {
                    "status": "BLOCKED",
                    "provider_usage_captured": False,
                },
                "container_action_evidence_refs": [evidence_ref()],
            }
            first = ledger.append(**kwargs)
            duplicate = ledger.append(**kwargs)
            self.assertEqual(first["record_id"], duplicate["record_id"])
            self.assertFalse(first["human_decision_created"])
            self.assertFalse(first["datapass_created"])

            second = ledger.append(
                **{
                    **kwargs,
                    "terminal_state": "BUDGET_EXCEEDED",
                    "reason": "daily hard cap exhausted",
                }
            )
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(
                second["previous_record_sha256"], first["record_sha256"]
            )
            self.assertEqual(len(ledger.records("RUN-LIVE-TEST-1")), 2)

    def test_progress_watchdog_stops_exhausted_non_manager_role_without_output(self) -> None:
        run = {
            "state": "RUNNING",
            "prompt_budget_readiness": {
                "runtime_readbacks": [
                    {"role": "manager", "max_iters": 1},
                    {"role": "finchange-case-lead", "max_iters": 2},
                ]
            },
            "provider_usage": {
                "by_agent": [
                    {"agent_id": "global-manager", "role": "manager", "call_count": 1},
                    {
                        "agent_id": "finchange-case-lead",
                        "role": "team_leader",
                        "call_count": 2,
                    },
                ]
            },
        }
        decision = progress_watchdog_decision(
            run,
            worker_artifact_count=0,
            skill_invocation_count=0,
            stalled_seconds=12,
            no_artifact_progress_timeout_seconds=180,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["terminal_state"], "STOPPED_BY_GATE")
        self.assertIn(
            "ROLE_MODEL_BUDGET_EXHAUSTED_WITHOUT_ARTIFACT:finchange-case-lead:2/2",
            decision["reason_codes"],
        )
        self.assertFalse(decision["diagnostics"]["model_called_by_watchdog"])

    def test_progress_watchdog_does_not_stop_after_expected_manager_call(self) -> None:
        run = {
            "state": "RUNNING",
            "prompt_budget_readiness": {
                "runtime_readbacks": [{"role": "manager", "max_iters": 1}]
            },
            "provider_usage": {
                "by_agent": [
                    {"agent_id": "global-manager", "role": "manager", "call_count": 1}
                ]
            },
        }
        self.assertIsNone(
            progress_watchdog_decision(
                run,
                worker_artifact_count=0,
                skill_invocation_count=0,
                stalled_seconds=30,
                no_artifact_progress_timeout_seconds=180,
            )
        )

    def test_progress_watchdog_stops_on_no_artifact_progress_timeout(self) -> None:
        decision = progress_watchdog_decision(
            {"state": "RUNNING", "provider_usage": {"by_agent": []}},
            worker_artifact_count=1,
            skill_invocation_count=2,
            stalled_seconds=181,
            no_artifact_progress_timeout_seconds=180,
        )
        self.assertIsNotNone(decision)
        self.assertIn(
            "NO_ARTIFACT_PROGRESS_TIMEOUT:181>=180", decision["reason_codes"]
        )

    def test_progress_watchdog_never_treats_human_wait_as_stall(self) -> None:
        self.assertIsNone(
            progress_watchdog_decision(
                {"state": "AWAITING_HUMAN", "provider_usage": {"by_agent": []}},
                worker_artifact_count=3,
                skill_invocation_count=5,
                stalled_seconds=3600,
                no_artifact_progress_timeout_seconds=180,
            )
        )

    def test_ledger_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            ledger = EmergencyStopLedger(Path(root))
            record = ledger.append(
                run_id="RUN-LIVE-TEST-2",
                case_id="FUTURES-TEST",
                terminal_state="STOPPED_BY_GATE",
                actor="operator:test",
                reason="manual stop",
                token_guard_snapshot={"status": "BLOCKED"},
                container_action_evidence_refs=[evidence_ref()],
            )
            path = next((Path(root) / record["run_id"]).glob("*.json"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["reason"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                ledger.records(record["run_id"])

    def test_running_guard_stops_unavailable_usage_and_hard_caps(self) -> None:
        base = {
            "provider_usage_captured": True,
            "daily": {"total_tokens": 100, "hard_cap": 300000},
            "per_run_hard_cap": 120000,
            "active_run_provider_tokens": 100,
            "active_run_count": 1,
            "active_run_ids": ["RUN-LIVE-TEST-3"],
        }
        self.assertIsNone(
            running_guard_decision("RUN-LIVE-TEST-3", "RUNNING", base)
        )
        unavailable = {**base, "provider_usage_captured": False}
        decision = running_guard_decision(
            "RUN-LIVE-TEST-3", "RUNNING", unavailable
        )
        self.assertEqual(decision["terminal_state"], "STOPPED_BY_GATE")
        exhausted = {
            **base,
            "daily": {"total_tokens": 300000, "hard_cap": 300000},
        }
        decision = running_guard_decision(
            "RUN-LIVE-TEST-3", "RUNNING", exhausted
        )
        self.assertEqual(decision["terminal_state"], "BUDGET_EXCEEDED")
        multiple = {
            **base,
            "active_run_count": 2,
            "active_run_ids": ["RUN-LIVE-TEST-3", "RUN-LIVE-OTHER"],
        }
        decision = running_guard_decision(
            "RUN-LIVE-TEST-3", "RUNNING", multiple
        )
        self.assertIn("ACTIVE_RUN_STATE_INCONSISTENT", decision["reason"])

    @unittest.skip("retired private-adapter white-box contract; covered by AgentTeamsService tests")
    def test_adapter_stop_does_not_forge_human_or_datapass(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            runs = root_path / "runs"
            runs.mkdir()
            run_id = "RUN-LIVE-TEST-4"
            (runs / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "case_id": "FUTURES-TEST",
                        "state": "RUNNING",
                        "case_envelope_sha256": "a" * 64,
                        "human_decision": None,
                        "datapass": None,
                    }
                ),
                encoding="utf-8",
            )
            ledger = EmergencyStopLedger(root_path / "ledger")
            with patch.object(adapter, "RUNS_ROOT", runs), patch.object(
                adapter, "EMERGENCY_STOP_LEDGER", ledger
            ):
                stopped = adapter.record_emergency_stop(
                    run_id,
                    terminal_state="STOPPED_BY_GATE",
                    actor="operator:test",
                    reason="container action already completed",
                    token_guard_snapshot={"status": "BLOCKED"},
                    container_action_evidence_refs=[evidence_ref()],
                )
                persisted = json.loads(
                    (runs / f"{run_id}.json").read_text(encoding="utf-8")
                )
            self.assertEqual(stopped["state"], "STOPPED_BY_GATE")
            self.assertIsNone(persisted["human_decision"])
            self.assertIsNone(persisted["datapass"])
            self.assertFalse(persisted["budget_stop"]["matrix_message_sent"])
            self.assertFalse(persisted["budget_stop"]["model_called"])

    @unittest.skip("retired private-adapter white-box contract; covered by AgentTeamsService tests")
    def test_terminal_control_gap_recovery_is_zero_traffic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            runs = root_path / "runs"
            runs.mkdir()
            run_id = "RUN-LIVE-TERMINAL-RECOVERY-1"
            (runs / f"{run_id}.json").write_text(
                json.dumps(self._closed_terminal_run(run_id)), encoding="utf-8"
            )
            ledger = EmergencyStopLedger(root_path / "ledger")
            guard = {
                "status": "BLOCKED",
                "provider_usage_captured": True,
                "active_run_provider_tokens": 120001,
            }
            with patch.object(adapter, "RUNS_ROOT", runs), patch.object(
                adapter, "EMERGENCY_STOP_LEDGER", ledger
            ), patch.object(adapter, "_send_message") as matrix_send, patch.object(
                adapter, "_docker"
            ) as docker, patch.object(
                adapter, "close_model_gateway_run"
            ) as close_gateway:
                before = adapter.terminal_control_status(run_id)
                recovered = adapter.recover_terminal_control_gap(
                    run_id,
                    terminal_state="BUDGET_EXCEEDED",
                    actor="operator:test",
                    reason="complete interrupted terminal control projection",
                    reason_codes=["TERMINAL_CONTROL_RECORD_MISSING"],
                    token_guard_snapshot=guard,
                )
                replayed = adapter.recover_terminal_control_gap(
                    run_id,
                    terminal_state="BUDGET_EXCEEDED",
                    actor="operator:test",
                    reason="a retry must not append another record",
                )
                after = adapter.terminal_control_status(run_id)

            self.assertTrue(before["recovery_eligible"])
            self.assertFalse(before["has_emergency_stop_record"])
            self.assertEqual(recovered["state"], "BUDGET_EXCEEDED")
            self.assertEqual(recovered["model_execution_seal_status"], "SEALED")
            self.assertEqual(
                recovered["emergency_stop_record"]["actor_source"],
                "CONTROL_PLANE_TERMINAL_RECOVERY",
            )
            self.assertFalse(
                recovered["terminal_control_recovery"][
                    "model_called_by_recovery"
                ]
            )
            self.assertFalse(
                recovered["terminal_control_recovery"][
                    "matrix_message_sent_by_recovery"
                ]
            )
            self.assertIsNone(recovered["human_decision"])
            self.assertIsNone(recovered["datapass"])
            self.assertEqual(
                replayed["emergency_stop_record"]["record_sha256"],
                recovered["emergency_stop_record"]["record_sha256"],
            )
            self.assertEqual(len(ledger.records(run_id)), 1)
            self.assertTrue(after["has_emergency_stop_record"])
            self.assertFalse(after["recovery_eligible"])
            matrix_send.assert_not_called()
            docker.assert_not_called()
            close_gateway.assert_not_called()

    @unittest.skip("retired private-adapter white-box contract; covered by AgentTeamsService tests")
    def test_terminal_control_gap_cannot_rewrite_persisted_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            runs = root_path / "runs"
            runs.mkdir()
            run_id = "RUN-LIVE-TERMINAL-RECOVERY-2"
            (runs / f"{run_id}.json").write_text(
                json.dumps(self._closed_terminal_run(run_id)), encoding="utf-8"
            )
            with patch.object(adapter, "RUNS_ROOT", runs), patch.object(
                adapter,
                "EMERGENCY_STOP_LEDGER",
                EmergencyStopLedger(root_path / "ledger"),
            ):
                with self.assertRaisesRegex(
                    adapter.AgentTeamsConfigurationError, "不得改写"
                ):
                    adapter.recover_terminal_control_gap(
                        run_id,
                        terminal_state="STOPPED_BY_GATE",
                        actor="operator:test",
                        reason="unsafe rewrite",
                        token_guard_snapshot={"status": "BLOCKED"},
                    )
            persisted = json.loads(
                (runs / f"{run_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["state"], "BUDGET_EXCEEDED")
            self.assertNotIn("emergency_stop_record", persisted)

    @unittest.skip("running guard now lives at dispatch boundary, not read projection")
    def test_get_run_fails_current_run_closed_before_runtime_or_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            runs = root_path / "runs"
            runs.mkdir()
            run_id = "RUN-LIVE-TEST-5"
            (runs / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "case_id": "FUTURES-TEST",
                        "state": "RUNNING",
                        "submitted_at_ms": 1,
                        "human_decision": None,
                        "datapass": None,
                    }
                ),
                encoding="utf-8",
            )
            guard = {
                "status": "BLOCKED",
                "provider_usage_captured": False,
                "daily": {"total_tokens": None, "hard_cap": 300000},
                "per_run_hard_cap": 120000,
                "active_run_provider_tokens": None,
                "active_run_count": 1,
                "active_run_ids": [run_id],
            }
            with patch.object(adapter, "RUNS_ROOT", runs), patch.object(
                adapter,
                "EMERGENCY_STOP_LEDGER",
                EmergencyStopLedger(root_path / "ledger"),
            ), patch.object(
                adapter, "provider_token_guard_snapshot", return_value=guard
            ), patch.object(adapter, "runtime_status") as runtime_status, patch.object(
                adapter, "_login"
            ) as login:
                stopped = adapter.get_run(run_id)
            self.assertEqual(stopped["state"], "STOPPED_BY_GATE")
            runtime_status.assert_not_called()
            login.assert_not_called()
            self.assertEqual(
                stopped["trace_source"], "APPEND_ONLY_EMERGENCY_CONTROL_LEDGER"
            )


if __name__ == "__main__":
    unittest.main()
