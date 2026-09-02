from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentteams_adapter as adapter
from agentteams_runtime.service import AgentTeamsService
from agentteams_runtime.store import RunStore


class AgentTeamsAdapterPublicContractTests(unittest.TestCase):
    def test_facade_exports_only_supported_operations(self) -> None:
        self.assertEqual(
            set(adapter.__all__),
            {
                "AgentTeamsConfigurationError",
                "AgentTeamsUnavailable",
                "get_active_run",
                "get_persisted_run",
                "get_run",
                "provider_token_guard_snapshot",
                "record_emergency_stop",
                "rearm_same_run_manager",
                "recover_terminal_control_gap",
                "request_same_run_repair",
                "reset_agent_sessions",
                "runtime_status",
                "submit_human_decision",
                "submit_live_case",
                "supervisor_dispatch_missing_workers",
                "supervisor_stop_wait",
                "supervisor_wake_manager",
                "terminal_control_status",
            },
        )

    def test_active_run_reads_one_persisted_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp))
            store.save({"run_id": "RUN-LIVE-TEST-0001", "state": "RUNNING"})
            service = AgentTeamsService(store)
            self.assertEqual(service.active_run()["run_id"], "RUN-LIVE-TEST-0001")

    def test_reset_is_run_scoped_and_zero_model(self) -> None:
        result = AgentTeamsService.reset_sessions()
        self.assertEqual(result["status"], "NOT_REQUIRED")
        self.assertFalse(result["model_called"])
        self.assertEqual(result["provider_tokens"], 0)

    @patch("agentteams_runtime.runtime.execution_policy")
    def test_provider_guard_blocks_second_active_run(self, policy) -> None:
        policy.return_value = {"run_limits": {"max_active_runs": 1}}
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp))
            store.save({"run_id": "RUN-LIVE-TEST-0002", "state": "RUNNING"})
            result = AgentTeamsService(store).provider_guard()
            self.assertFalse(result["allowed"])
            self.assertEqual(result["active_run_count"], 1)


if __name__ == "__main__":
    unittest.main()
