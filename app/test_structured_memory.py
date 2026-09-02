from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from structured_memory import StructuredMemoryStore


class StructuredMemoryTests(unittest.TestCase):
    def test_memory_keeps_handles_and_excludes_sensitive_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StructuredMemoryStore(Path(temp_dir))
            run = {
                "run_id": "RUN-MEMORY-1",
                "case_id": "CASE-1",
                "state": "AWAITING_HUMAN",
                "submission_id": "SUB-1",
                "lifecycle": {"current_phase": "AWAITING_HUMAN"},
                "root_route_decision": {
                    "decision_id": "ROUTE-1",
                    "route": "FULL_TEAM_REVIEW",
                    "worker_plan": {"workers": ["Evidence Investigator"]},
                },
                "agent_result": {
                    "worker_artifacts": {
                        "evidence-investigator": {
                            "task_id": "task-1",
                            "status": "SUCCEEDED",
                            "tool_run_id": "tool-1",
                            "raw_secret": "MUST_NOT_BE_COPIED_AS_RAW_MEMORY",
                            "skill_invocations": [
                                {
                                    "skill_id": "evidence-integrity",
                                    "version": "1.0.0",
                                    "digest": "d" * 64,
                                    "input_sha256": "i" * 64,
                                    "output_sha256": "o" * 64,
                                    "status": "SUCCESS",
                                }
                            ],
                        }
                    }
                },
                "datapass": {
                    "machine_recommendation": "PASS",
                    "draft_sha256": "p" * 64,
                },
                "human_gate": {"state": "AWAITING_HUMAN"},
                "provider_usage": {"status": "PROVIDER_REPORTED", "total_tokens": 123},
            }
            submission = {
                "evidence_root_hash": "e" * 64,
                "file": {"sha256": "f" * 64, "raw_csv": "SECRET-FINANCIAL-DATA"},
            }
            memory = store.update_run(run, submission)
            serialized = json.dumps(memory, ensure_ascii=False)
            self.assertNotIn("SECRET-FINANCIAL-DATA", serialized)
            self.assertNotIn("MUST_NOT_BE_COPIED_AS_RAW_MEMORY", serialized)
            self.assertEqual(memory["evidence_handles"]["source_file_sha256"], "f" * 64)
            self.assertFalse(memory["privacy"]["raw_financial_bytes_stored"])
            self.assertEqual(store.status()["skill_cache_entries"], 1)


if __name__ == "__main__":
    unittest.main()
