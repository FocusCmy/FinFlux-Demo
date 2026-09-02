from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from bounded_worker_task import (
    _canonical_sha256,
    _discover_skill_registry,
    _execute_verified_skill,
    _seal_worker_artifact,
)


class WorkerSkillRuntimeTests(unittest.TestCase):
    def test_runtime_manifest_loads_verified_skill_versions(self) -> None:
        registry = _discover_skill_registry("semantic-impact-analyst")
        self.assertIn("semantic-contract-resolver@1.1.0", registry["skills"])
        self.assertEqual(len(registry["manifest_sha256"]), 64)
        self.assertEqual(len(registry["entrypoint_sha256"]), 64)

    def test_unlisted_or_wrong_version_is_fail_closed(self) -> None:
        registry = _discover_skill_registry("evidence-investigator")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            _execute_verified_skill(registry, "evidence-integrity", "9.9.9", {})

    def test_manifest_callable_is_executed_before_success_receipt(self) -> None:
        registry = _discover_skill_registry("evidence-investigator")
        output, receipt = _execute_verified_skill(
            registry,
            "rights-gate",
            "1.0.0",
            {"rights_state": "PASS"},
        )
        self.assertEqual(output["status"], "PASS")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["entrypoint_callable"], "skill_rights_gate")
        self.assertEqual(receipt["output_sha256"], _canonical_sha256(output))
        self.assertEqual(len(receipt["tool_run_id"]), 24)
        artifact = _seal_worker_artifact(
            {
                "status": "PASS",
                "skill_invocations": [receipt],
                "context_skill_invocations": [
                    {"skill_id": "context-capsule-loader", "status": "SUCCESS"}
                ],
            }
        )
        self.assertEqual(
            artifact["artifact_sha256"],
            _canonical_sha256(
                {
                    key: value
                    for key, value in artifact.items()
                    if key != "artifact_sha256"
                }
            ),
        )

    def test_timed_out_callable_emits_no_success_receipt(self) -> None:
        registry = _discover_skill_registry("evidence-investigator")

        def slow(_: dict) -> dict:
            time.sleep(0.05)
            return {"status": "PASS"}

        registry["skills"]["rights-gate@1.0.0"]["callable_obj"] = slow
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            _execute_verified_skill(
                registry,
                "rights-gate",
                "1.0.0",
                {"rights_state": "PASS"},
                timeout_seconds=0.001,
            )

    def test_tampered_instruction_digest_is_fail_closed(self) -> None:
        source = (
            Path(__file__).resolve().parent
            / "runtime-skill-manifests"
            / "evidence-investigator.json"
        )
        manifest = json.loads(source.read_text(encoding="utf-8"))
        manifest["skills"][0]["instruction_sha256"] = "0" * 64
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        manifest["manifest_sha256"] = _canonical_sha256(unsigned)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            dir=source.parent,
            encoding="utf-8",
            delete=False,
        ) as handle:
            json.dump(manifest, handle)
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "instruction digest mismatch"):
                _discover_skill_registry("evidence-investigator", path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
