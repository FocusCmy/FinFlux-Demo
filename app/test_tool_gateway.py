from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from tool_gateway import execute, validate_tool_args


class ToolGatewayTests(unittest.TestCase):
    def test_rejects_non_allowlisted_argument(self) -> None:
        with self.assertRaises(ValueError):
            validate_tool_args(
                "bounded-change",
                [
                    "--case-id", "CASE-1", "--run-id", "RUN-1",
                    "--task-id", "task-CASE-12345678", "--policy-id", "POLICY",
                    "--change-payload-b64", "e30", "--output-dir", "C:/escape",
                ],
            )

    def test_change_tool_emits_hash_bound_execution_receipt(self) -> None:
        payload = {
            "change_bundle_id": "CB-TEST",
            "change_set": {
                "change_id": "CHG-TEST",
                "change_set_sha256": "a" * 64,
                "changed_paths": ["metadata.candidate_mapping"],
            },
            "downstream_tasks": [
                {
                    "task_id": "daily-settlement",
                    "dependencies": ["metadata.candidate_mapping"],
                }
            ],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii").rstrip("=")
        run_id = "RUN-LIVE-20260901010101-c0ffee"
        task_id = (
            "task-CASE-CHANGE-LIVE-20260901010101-c0ffee-"
            "downstream-impact-analyst"
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"FINFLUX_TASK_ROOT": temp_dir}
        ):
            code, stdout, stderr, receipt = execute(
                "bounded-change",
                [
                    "--case-id", "CASE-CHANGE", "--run-id", run_id,
                    "--task-id", task_id, "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
                    "--change-payload-b64", encoded,
                ],
                30,
            )
            persisted = json.loads(
                (Path(temp_dir) / task_id / "tool_execution_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            cached_code, cached_stdout, cached_stderr, cached = execute(
                "bounded-change",
                [
                    "--case-id", "CASE-CHANGE", "--run-id", run_id,
                    "--task-id", task_id, "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
                    "--change-payload-b64", encoded,
                ],
                30,
            )
            persisted_after_replay = json.loads(
                (Path(temp_dir) / task_id / "tool_execution_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            replay_projection = json.loads(
                (Path(temp_dir) / task_id / "tool_cache_latest.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 0)
        self.assertFalse(stderr)
        self.assertIn("result_path", stdout)
        self.assertEqual(persisted["status"], "SUCCEEDED")
        self.assertEqual(persisted["provider_tokens"], 0)
        self.assertEqual(persisted["receipt_sha256"], receipt["receipt_sha256"])
        self.assertEqual(cached_code, 0)
        self.assertFalse(cached_stderr)
        self.assertIn('"cache_hit": true', cached_stdout)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(
            cached["cache_source_receipt_sha256"], receipt["receipt_sha256"]
        )
        self.assertEqual(persisted_after_replay, persisted)
        self.assertEqual(
            replay_projection["cache_source_receipt_sha256"],
            receipt["receipt_sha256"],
        )
        self.assertEqual(cached["avoided_subprocess_invocations"], 1)
        self.assertEqual(cached["provider_tokens"], 0)
        self.assertEqual(
            cached["artifact_manifest"]["manifest_sha256"],
            receipt["artifact_manifest"]["manifest_sha256"],
        )
        self.assertEqual(
            cached["token_savings_claim"],
            "NO_PROVIDER_TOKEN_CLAIM; DETERMINISTIC_TOOL_REEXECUTION_AVOIDED",
        )

    def test_timeout_fails_closed_and_persists_receipt_without_retry(self) -> None:
        run_id = "RUN-LIVE-20260901010202-dead01"
        task_id = (
            "task-CASE-TIMEOUT-LIVE-20260901010202-dead01-"
            "downstream-impact-analyst"
        )
        arguments = [
            "--case-id", "CASE-TIMEOUT", "--run-id", run_id,
            "--task-id", task_id, "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--change-payload-b64", "e30",
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"FINFLUX_TASK_ROOT": temp_dir}
        ), patch(
            "tool_gateway.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1),
        ):
            code, stdout, stderr, receipt = execute(
                "bounded-change", arguments, 1
            )
            persisted = json.loads(
                (Path(temp_dir) / task_id / "tool_execution_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 124)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertEqual(receipt["status"], "TIMED_OUT")
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["retry_count"], 0)
        self.assertEqual(persisted["receipt_sha256"], receipt["receipt_sha256"])

    def test_cached_artifact_tamper_fails_closed_without_reexecution(self) -> None:
        payload = {
            "change_bundle_id": "CB-TAMPER",
            "change_set": {
                "change_id": "CHG-TAMPER",
                "change_set_sha256": "b" * 64,
                "changed_paths": ["metadata.candidate_mapping"],
            },
            "downstream_tasks": [
                {
                    "task_id": "daily-settlement",
                    "dependencies": ["metadata.candidate_mapping"],
                }
            ],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii").rstrip("=")
        run_id = "RUN-LIVE-20260901010303-badbad"
        task_id = (
            "task-CASE-TAMPER-LIVE-20260901010303-badbad-"
            "downstream-impact-analyst"
        )
        arguments = [
            "--case-id", "CASE-TAMPER", "--run-id", run_id,
            "--task-id", task_id, "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--change-payload-b64", encoded,
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"FINFLUX_TASK_ROOT": temp_dir}
        ):
            first_code, _, _, first = execute("bounded-change", arguments, 30)
            task_dir = Path(temp_dir) / task_id
            original_receipt = (task_dir / "tool_execution_receipt.json").read_bytes()
            (task_dir / "result.md").write_text("tampered", encoding="utf-8")
            with patch("tool_gateway.subprocess.run") as subprocess_run:
                code, stdout, stderr, failure = execute(
                    "bounded-change", arguments, 30
                )
            receipt_after = (task_dir / "tool_execution_receipt.json").read_bytes()
            persisted_failure = json.loads(
                (task_dir / "tool_cache_integrity_failure.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(first_code, 0)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(code, 65)
        self.assertEqual(stdout, "")
        self.assertIn("integrity check failed", stderr)
        self.assertEqual(failure["status"], "CACHE_INTEGRITY_FAILED")
        self.assertFalse(failure["artifact_manifest_valid"])
        self.assertEqual(failure["retry_policy"], "FAIL_CLOSED_NO_REEXECUTION")
        self.assertEqual(persisted_failure["receipt_sha256"], failure["receipt_sha256"])
        self.assertEqual(original_receipt, receipt_after)
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
