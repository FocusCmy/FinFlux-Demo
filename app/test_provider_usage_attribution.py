from __future__ import annotations

import unittest

from provider_usage_attribution import (
    ProviderUsageAttributionError,
    attribute_exclusive_run_delta,
)


def snapshot(prompt: int, completion: int, calls: int) -> dict:
    return {
        "date_utc": "2026-08-31",
        "by_agent": [
            {
                "agent_id": "semantic-impact-analyst",
                "role": "worker",
                "models": [
                    {
                        "provider_id": "agentteams-gateway",
                        "model_name": "deepseek-v4-flash",
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "call_count": calls,
                    }
                ],
            }
        ],
    }


class ProviderUsageAttributionTests(unittest.TestCase):
    def test_includes_global_manager_and_every_actual_actor(self) -> None:
        actors = [
            ("global-manager", "manager", 771_668, 1_644, 12),
            ("finchange-case-lead", "team_leader", 903_799, 16_565, 14),
            ("evidence-investigator", "worker", 358_332, 11_463, 8),
            ("semantic-impact-analyst", "worker", 2_149_600, 11_393, 18),
            ("independent-validator", "worker", 1_711_863, 8_845, 15),
        ]

        def full_snapshot(zero: bool) -> dict:
            return {
                "date_utc": "2026-08-31",
                "by_agent": [
                    {
                        "agent_id": agent_id,
                        "role": role,
                        "models": [
                            {
                                "provider_id": "agentteams-gateway",
                                "model_name": "deepseek-v4-flash",
                                "prompt_tokens": 0 if zero else prompt,
                                "completion_tokens": 0 if zero else completion,
                                "total_tokens": 0 if zero else prompt + completion,
                                "call_count": 0 if zero else calls,
                            }
                        ],
                    }
                    for agent_id, role, prompt, completion, calls in actors
                ],
            }

        result = attribute_exclusive_run_delta(
            full_snapshot(True),
            full_snapshot(False),
            run_id="RUN-LIVE-HISTORICAL-AUDIT",
            exclusive_active_run=True,
        )
        self.assertEqual(result["total_tokens"], 5_945_172)
        self.assertEqual(result["call_count"], 67)
        self.assertEqual(len(result["by_agent"]), 5)
        self.assertEqual(result["by_agent"][0]["agent_id"], "evidence-investigator")

    def test_attributes_all_calls_not_only_final_session_line(self) -> None:
        result = attribute_exclusive_run_delta(
            snapshot(0, 0, 0),
            snapshot(2_149_600, 11_393, 18),
            run_id="RUN-LIVE-1",
            exclusive_active_run=True,
        )
        self.assertEqual(result["prompt_tokens"], 2_149_600)
        self.assertEqual(result["completion_tokens"], 11_393)
        self.assertEqual(result["total_tokens"], 2_160_993)
        self.assertEqual(result["call_count"], 18)
        self.assertEqual(result["by_agent"][0]["total_tokens"], 2_160_993)
        self.assertEqual(result["by_agent"][0]["call_count"], 18)
        self.assertRegex(result["baseline_snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["current_snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["attribution_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            result["attribution_status"], "DAILY_DELTA_EXCLUSIVE_ACTIVE_RUN"
        )

    def test_subtracts_nonzero_launch_baseline(self) -> None:
        result = attribute_exclusive_run_delta(
            snapshot(100, 20, 2),
            snapshot(350, 45, 5),
            run_id="RUN-LIVE-2",
            exclusive_active_run=True,
        )
        self.assertEqual(result["prompt_tokens"], 250)
        self.assertEqual(result["completion_tokens"], 25)
        self.assertEqual(result["call_count"], 3)

    def test_fails_closed_when_run_is_not_exclusive(self) -> None:
        with self.assertRaises(ProviderUsageAttributionError):
            attribute_exclusive_run_delta(
                snapshot(0, 0, 0),
                snapshot(1, 1, 1),
                run_id="RUN-LIVE-3",
                exclusive_active_run=False,
            )

    def test_fails_closed_on_counter_regression(self) -> None:
        with self.assertRaises(ProviderUsageAttributionError):
            attribute_exclusive_run_delta(
                snapshot(100, 20, 2),
                snapshot(99, 20, 2),
                run_id="RUN-LIVE-4",
                exclusive_active_run=True,
            )

    def test_fails_closed_across_utc_day_boundary(self) -> None:
        current = snapshot(1, 1, 1)
        current["date_utc"] = "2026-09-01"
        with self.assertRaises(ProviderUsageAttributionError):
            attribute_exclusive_run_delta(
                snapshot(0, 0, 0),
                current,
                run_id="RUN-LIVE-5",
                exclusive_active_run=True,
            )


if __name__ == "__main__":
    unittest.main()
