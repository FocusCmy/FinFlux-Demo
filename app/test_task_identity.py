from __future__ import annotations

import unittest

from task_identity import (
    TaskIdentityError,
    build_role_task_ids,
    run_task_scope,
    validate_role_task_ids,
)
from bounded_worker_task import _validate_task_binding as validate_worker_task
from bounded_change_task import _validate_task_binding as validate_change_task


class TaskIdentityTests(unittest.TestCase):
    def test_generates_exact_role_bound_task_ids(self) -> None:
        result = build_role_task_ids(
            "FUTURES-IF2608-20260814-SETTLEMENT",
            "RUN-LIVE-20260831154246-14a21e",
            (
                "evidence-investigator",
                "semantic-impact-analyst",
                "independent-validator",
            ),
        )
        self.assertEqual(
            result["task_scope"],
            "task-FUTURES-IF2608-20260814-SETTLEMENT-LIVE-20260831154246-14a21e",
        )
        self.assertEqual(
            result["task_ids"]["evidence-investigator"],
            "task-FUTURES-IF2608-20260814-SETTLEMENT-LIVE-20260831154246-14a21e-evidence-investigator",
        )
        for task_id in result["task_ids"].values():
            self.assertTrue(task_id.startswith(result["task_scope"] + "-"))

    def test_rejects_non_live_run_id(self) -> None:
        with self.assertRaises(TaskIdentityError):
            run_task_scope("FUTURES-IF2608", "RUN-FUTURES-20260831154246-14a21e")

    def test_rejects_duplicate_worker(self) -> None:
        with self.assertRaises(TaskIdentityError):
            build_role_task_ids(
                "FUTURES-IF2608",
                "RUN-LIVE-20260831154246-14a21e",
                ("evidence-investigator", "evidence-investigator"),
            )

    def test_same_second_runs_have_disjoint_namespaces(self) -> None:
        first = run_task_scope(
            "FUTURES-IF2608",
            "RUN-LIVE-20260831154246-14a21e",
        )
        second = run_task_scope(
            "FUTURES-IF2608",
            "RUN-LIVE-20260831154246-14a21f",
        )
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-14a21e"))
        self.assertTrue(second.endswith("-14a21f"))

    def test_transport_map_is_exact_not_advisory(self) -> None:
        workers = ("evidence-investigator", "independent-validator")
        expected = build_role_task_ids(
            "FUTURES-IF2608",
            "RUN-LIVE-20260831154246-14a21e",
            workers,
        )
        observed = validate_role_task_ids(
            expected,
            case_id="FUTURES-IF2608",
            run_id="RUN-LIVE-20260831154246-14a21e",
            selected_workers=workers,
        )
        self.assertEqual(observed, expected)
        tampered = {**expected, "task_ids": dict(expected["task_ids"])}
        tampered["task_ids"]["evidence-investigator"] += "-other-run"
        with self.assertRaises(TaskIdentityError):
            validate_role_task_ids(
                tampered,
                case_id="FUTURES-IF2608",
                run_id="RUN-LIVE-20260831154246-14a21e",
                selected_workers=workers,
            )

    def test_worker_rejects_same_second_other_nonce(self) -> None:
        case_id = "FUTURES-IF2608"
        run_id = "RUN-LIVE-20260831154246-14a21e"
        wrong_scope = run_task_scope(
            case_id, "RUN-LIVE-20260831154246-14a21f"
        )
        with self.assertRaisesRegex(ValueError, "exact role task"):
            validate_worker_task(
                "evidence-investigator",
                case_id,
                run_id,
                f"{wrong_scope}-evidence-investigator",
            )

    def test_change_worker_requires_exact_role_suffix(self) -> None:
        case_id = "FUTURES-IF2608"
        run_id = "RUN-LIVE-20260831154246-14a21e"
        scope = run_task_scope(case_id, run_id)
        self.assertEqual(
            validate_change_task(
                case_id,
                run_id,
                f"{scope}-downstream-impact-analyst",
            ),
            f"{scope}-downstream-impact-analyst",
        )
        with self.assertRaisesRegex(ValueError, "exact downstream task"):
            validate_change_task(
                case_id,
                run_id,
                f"{scope}-semantic-impact-analyst",
            )


if __name__ == "__main__":
    unittest.main()
