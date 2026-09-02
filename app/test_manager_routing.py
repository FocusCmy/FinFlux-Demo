from __future__ import annotations

import unittest

from manager_routing import decide_root_route, execute_manager_route_skill


class ManagerRoutingTests(unittest.TestCase):
    def test_versioned_manager_skill_emits_hash_bound_zero_model_receipt(self) -> None:
        facts = {
            "case_id": "CASE-SKILL",
            "run_id": "RUN-SKILL",
            "submission_id": "SUB-SKILL",
            "asset_class": "futures",
            "evidence_profile": "futures_settlement",
            "declared_downstream_use": "daily_settlement_pnl",
            "rights_status": "PASS",
            "evidence_status": "VERIFIED",
            "evidence_hash_valid": True,
            "candidate_mapping": "close",
            "required_mapping": "settle",
            "impact_cny": 1200.0,
            "precheck_recommendation": "BLOCK",
            "semantic_conflict_code": "SEMANTIC_MAPPING_CONFLICT",
            "budget_available": True,
        }
        decision, receipt = execute_manager_route_skill(facts)
        self.assertEqual(receipt["route_decision_sha256"], decision["decision_sha256"])
        self.assertEqual(receipt["skill_id"], "manager-route-case")
        self.assertEqual(receipt["version"], "1.0.0")
        self.assertEqual(receipt["provider_tokens"], 0)
        self.assertFalse(receipt["raw_financial_values_read"])
        self.assertEqual(len(receipt["receipt_sha256"]), 64)
        self.assertNotIn("candidate_mapping", decision["input_facts"])
        self.assertNotIn("required_mapping", decision["input_facts"])
        self.assertNotIn("impact_cny", decision["input_facts"])
        self.assertEqual(
            decision["input_facts"]["semantic_conflict_code"],
            "SEMANTIC_MAPPING_CONFLICT",
        )

    def test_version_change_routes_dynamic_blast_radius_team(self) -> None:
        decision = decide_root_route(
            {
                "case_id": "CASE-1",
                "run_id": "RUN-1",
                "submission_id": "SUB-2",
                "asset_class": "futures",
                "evidence_profile": "futures_settlement",
                "declared_downstream_use": "daily_settlement_pnl",
                "rights_status": "PASS",
                "evidence_status": "VERIFIED",
                "evidence_hash_valid": True,
                "candidate_mapping": "settle",
                "required_mapping": "settle",
                "precheck_recommendation": "PASS",
                "semantic_conflict_code": "SEMANTIC_MAPPING_ALIGNED",
                "budget_available": True,
                "change_bundle_id": "CB-123",
                "change_count": 1,
                "affected_task_count": 1,
                "unknown_impact_task_count": 1,
                "financial_semantic_change": True,
            }
        )
        self.assertEqual(decision["route"], "BLAST_RADIUS_REVIEW")
        self.assertEqual(decision["machine_recommendation"], "HOLD")
        self.assertEqual(decision["worker_plan"]["count"], 4)
        self.assertIn(
            "Downstream Impact Analyst", decision["worker_plan"]["workers"]
        )
        self.assertIn(
            "resolve-downstream-lineage", decision["required_skill_versions"]
        )
        self.assertNotIn(
            "validate-remediation-plan", decision["required_skill_versions"]
        )
        self.assertIn("UNKNOWN_LINEAGE_REQUIRES_HUMAN", decision["reason_codes"])

    def test_version_change_without_blast_radius_keeps_code_only_path(self) -> None:
        decision = decide_root_route(
            {
                "case_id": "CASE-2",
                "run_id": "RUN-2",
                "submission_id": "SUB-2",
                "asset_class": "futures",
                "evidence_profile": "futures_settlement",
                "declared_downstream_use": "daily_settlement_pnl",
                "rights_status": "PASS",
                "evidence_status": "VERIFIED",
                "evidence_hash_valid": True,
                "candidate_mapping": "settle",
                "required_mapping": "settle",
                "precheck_recommendation": "PASS",
                "semantic_conflict_code": "SEMANTIC_MAPPING_ALIGNED",
                "budget_available": True,
                "change_bundle_id": "CB-456",
                "change_count": 1,
                "affected_task_count": 0,
                "unknown_impact_task_count": 0,
            }
        )
        self.assertEqual(decision["route"], "CODE_ONLY_PRECHECK")
        self.assertEqual(decision["worker_plan"]["count"], 0)

    def test_standard_conflict_keeps_optional_specialists_off_critical_path(self) -> None:
        decision = decide_root_route(
            {
                "case_id": "CASE-3",
                "run_id": "RUN-3",
                "submission_id": "SUB-3",
                "asset_class": "futures",
                "evidence_profile": "futures_settlement",
                "declared_downstream_use": "daily_settlement_pnl",
                "rights_status": "PASS",
                "evidence_status": "VERIFIED",
                "evidence_hash_valid": True,
                "candidate_mapping": "close",
                "required_mapping": "settle",
                "precheck_recommendation": "BLOCK",
                "semantic_conflict_code": "SEMANTIC_MAPPING_CONFLICT",
                "budget_available": True,
                "confidentiality_class": "PUBLIC",
                "research_context_required": True,
                "operational_risk_review_required": True,
            }
        )
        self.assertEqual(decision["route"], "FULL_TEAM_REVIEW")
        self.assertEqual(decision["worker_plan"]["count"], 3)
        self.assertNotIn("Research Context Analyst", decision["worker_plan"]["workers"])
        self.assertNotIn("Runtime Resilience Auditor", decision["worker_plan"]["workers"])
        self.assertNotIn("Data Rights Steward", decision["worker_plan"]["workers"])
        self.assertEqual(len(decision["required_skill_versions"]), 5)


if __name__ == "__main__":
    unittest.main()
