from __future__ import annotations

import re
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "agentteams"
    / "scripts"
    / "Test-WorkerRecovery.ps1"
)


class WorkerRecoveryScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8-sig")

    def test_token_guard_is_captured_before_any_container_stop(self) -> None:
        capture = self.source.index('$tokenBefore = Capture-TokenGuard "before"')
        stop = self.source.index("docker stop --time 10")
        self.assertLess(capture, stop)
        self.assertIn('Get-FinFluxJson "/api/v1/token-guard"', self.source)
        self.assertIn('safety_gate = "NO_ACTIVE_AGENTTEAMS_RUN"', self.source)

    def test_before_and_after_snapshots_are_hash_bound(self) -> None:
        self.assertIn('Capture-TokenGuard "before"', self.source)
        self.assertIn('Capture-TokenGuard "after"', self.source)
        self.assertIn("Get-FileHash -Algorithm SHA256", self.source)
        self.assertIn("PROVIDER_USAGE_SOURCE_CHANGED", self.source)
        self.assertIn("provider_tokens_delta", self.source)
        self.assertIn("provider_call_delta", self.source)

    def test_missing_usage_fails_closed_and_no_zero_is_hard_coded(self) -> None:
        self.assertIn('status = "NOT_CAPTURED"', self.source)
        self.assertIn('zero_token_claim = "FORBIDDEN"', self.source)
        self.assertNotRegex(self.source, re.compile(r"provider_tokens\s*=\s*0\b"))
        self.assertNotRegex(self.source, re.compile(r"model_calls\s*=\s*0\b"))

    def test_claim_is_limited_to_process_and_topology_recovery(self) -> None:
        self.assertIn("未证明Session恢复、跨节点迁移或高可用", self.source)
        self.assertIn("未在活动Run中断Worker", self.source)


if __name__ == "__main__":
    unittest.main()
