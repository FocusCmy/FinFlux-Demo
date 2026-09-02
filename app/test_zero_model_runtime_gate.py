from __future__ import annotations

import unittest
from unittest.mock import patch

from zero_model_runtime_gate import run_gate


class ZeroModelRuntimeGateTests(unittest.TestCase):
    @patch("zero_model_runtime_gate.container_names")
    @patch("zero_model_runtime_gate.provider_token_guard_snapshot")
    @patch("zero_model_runtime_gate.runtime_status")
    def test_gate_proves_readiness_without_model(
        self, status, guard, containers
    ) -> None:
        status.return_value = {"connected": True, "docker_ready": True}
        guard.return_value = {"allowed": True}
        containers.return_value = {
            "agentteams-worker-finchange-case-lead",
            "agentteams-worker-evidence-investigator",
            "agentteams-worker-semantic-impact-analyst",
            "agentteams-worker-independent-validator",
        }
        result = run_gate()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["model_called"])
        self.assertEqual(result["provider_tokens"], 0)

    @patch("zero_model_runtime_gate.container_names", return_value=set())
    @patch("zero_model_runtime_gate.provider_token_guard_snapshot", return_value={"allowed": False})
    @patch("zero_model_runtime_gate.runtime_status", return_value={"connected": False, "docker_ready": True})
    def test_gate_fails_closed(self, *_mocks) -> None:
        result = run_gate()
        self.assertEqual(result["status"], "BLOCK")
        self.assertTrue(result["missing_containers"])


if __name__ == "__main__":
    unittest.main()
