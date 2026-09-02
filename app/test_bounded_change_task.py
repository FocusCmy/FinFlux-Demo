from __future__ import annotations

import unittest

from bounded_change_task import execute


class BoundedChangeTaskTests(unittest.TestCase):
    def test_worker_resolves_declared_impact_without_model_truth(self) -> None:
        result = execute(
            {
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
                    },
                    {"task_id": "unknown", "dependencies": []},
                ],
            },
            "CASE-1",
            "RUN-1",
            "task-CASE-1-RUN-1-downstream-impact-analyst",
            "POLICY-1",
        )
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(
            result["recommendation"], "NEEDS_HUMAN_LINEAGE_REVIEW"
        )
        self.assertEqual(result["impact_graph"]["summary"]["affected_tasks"], 1)
        self.assertEqual(result["impact_graph"]["summary"]["unknown_impact_tasks"], 1)
        self.assertEqual(result["skill_invocations"][0]["provider_tokens"], 0)
        self.assertFalse(result["production_approved"])


if __name__ == "__main__":
    unittest.main()
