from __future__ import annotations

import unittest

from change_control import (
    detect_version_change,
    resolve_downstream_lineage,
    validate_remediation_plan,
)


def submission(submission_id: str, mapping: str) -> dict:
    return {
        "submission_id": submission_id,
        "profile": "futures_settlement",
        "file": {"name": "cffex.csv", "sha256": "a" * 64, "size_bytes": 100},
        "metadata": {
            "candidate_mapping": mapping,
            "declared_purpose": "daily_settlement_pnl",
        },
        "parsed": {"instrument": "IF2608", "close": 4648.4, "settle": 4652.4},
        "rights_gate": {"status": "PASS"},
        "evidence_root_hash": ("b" if mapping == "close" else "c") * 64,
        "raw_evidence_mutated": False,
    }


class ChangeControlTests(unittest.TestCase):
    def test_change_and_impact_are_derived_from_explicit_facts(self) -> None:
        baseline = submission("SUB-BASE", "close")
        candidate = submission("SUB-CANDIDATE", "settle")
        change = detect_version_change(baseline, candidate)
        self.assertFalse(change["raw_file_changed"])
        self.assertIn("metadata.candidate_mapping", change["changed_paths"])
        self.assertEqual(change["financial_decision"], "NOT_PERFORMED")

        graph = resolve_downstream_lineage(
            change,
            [
                {
                    "task_id": "daily-settlement",
                    "label": "日终记账",
                    "owner": "settlement-team",
                    "purpose": "daily_settlement_pnl",
                    "criticality": "high",
                    "dependencies": ["metadata.candidate_mapping", "parsed.settle"],
                },
                {
                    "task_id": "market-screen",
                    "label": "行情展示",
                    "owner": "market-data-team",
                    "purpose": "display",
                    "criticality": "low",
                    "dependencies": ["parsed.close"],
                },
                {
                    "task_id": "unknown-consumer",
                    "label": "未登记任务",
                    "owner": "UNASSIGNED",
                    "dependencies": [],
                },
            ],
        )
        states = {node["task_id"]: node["impact_state"] for node in graph["nodes"]}
        self.assertEqual(states["daily-settlement"], "AFFECTED")
        self.assertEqual(states["market-screen"], "NOT_AFFECTED_BY_DECLARED_DEPENDENCIES")
        self.assertEqual(states["unknown-consumer"], "UNKNOWN_IMPACT")
        self.assertFalse(graph["summary"]["safe_to_claim_no_impact"])

    def test_remediation_requires_rollback_and_actions(self) -> None:
        baseline = submission("SUB-BASE", "close")
        remediation = submission("SUB-REMEDIATION", "settle")
        change = detect_version_change(baseline, remediation)
        graph = resolve_downstream_lineage(
            change,
            [
                {
                    "task_id": "daily-settlement",
                    "dependencies": ["metadata.candidate_mapping"],
                    "criticality": "high",
                }
            ],
        )
        invalid = validate_remediation_plan(
            baseline,
            remediation,
            graph,
            {"expected_candidate_mapping": "settle", "task_actions": []},
        )
        self.assertEqual(invalid["status"], "NEEDS_REVISION")
        self.assertFalse(invalid["production_approved"])

        valid = validate_remediation_plan(
            baseline,
            remediation,
            graph,
            {
                "expected_candidate_mapping": "settle",
                "rollback_submission_id": "SUB-BASE",
                "task_actions": [
                    {"task_id": "daily-settlement", "action": "RECOMPUTE"}
                ],
            },
        )
        self.assertEqual(valid["status"], "VALIDATED_FOR_REVIEW")
        self.assertTrue(valid["human_gate_required"])
        self.assertFalse(valid["production_approved"])


if __name__ == "__main__":
    unittest.main()

