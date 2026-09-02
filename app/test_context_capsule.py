from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_capsule import (
    build_run_context_capsule,
    canonical_sha256,
    load_role_context_slice,
)
from tool_gateway import execute, validate_tool_args


class ContextCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "context"
        self.tasks = Path(self.temp.name) / "tasks"
        self.case_id = "FUTURES-IF2608-20260814-SETTLEMENT"
        self.run_id = "RUN-LIVE-20260831120000-ca5e01"
        payload = {
            "p": "FINFLUX_LIVE_WORKER_PAYLOAD_V0.1",
            "s": "SUB-CAPSULE-001",
            "f": "futures_settlement",
            "a": "futures",
            "h": "1" * 64,
            "r": "2" * 64,
            "g": "PASS",
            "i": "IF2608",
            "d": "20260814",
            "c": 4648.4,
            "t": 4652.4,
            "m": "close",
            "x": 300.0,
            "ps": "3" * 64,
            "pi": 1200.0,
            "pr": "BLOCK",
            "rm": "4" * 64,
            "rb": "5" * 64,
            "rc": 12,
            "cl": "PUBLIC",
            "gb": "exchange public data",
            "us": "EVALUATION_ONLY",
            "ew": 600,
            "et": 90,
            "er": 0,
            "ec": True,
        }
        payload["ph"] = canonical_sha256(payload)
        self.payload = payload
        self.roles = [
            "evidence-investigator",
            "semantic-impact-analyst",
            "data-rights-steward",
            "research-context-analyst",
            "runtime-resilience-auditor",
            "independent-validator",
        ]
        self.capsule, self.handle = build_run_context_capsule(
            case_id=self.case_id,
            run_id=self.run_id,
            payload=self.payload,
            selected_workers=self.roles,
            execution_policy_id="FINFLUX-BOUNDED-EXECUTION-V0.1",
            root_route_decision_handle={"decision_sha256": "6" * 64},
            local_root=self.root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_role_slices_are_hash_bound_and_minimal(self) -> None:
        rights = load_role_context_slice(
            self.handle["role_slice_handles"]["data-rights-steward"]["slice_sha256"],
            "data-rights-steward",
            case_id=self.case_id,
            run_id=self.run_id,
            root=self.root,
        )
        runtime = load_role_context_slice(
            self.handle["role_slice_handles"]["runtime-resilience-auditor"]["slice_sha256"],
            "runtime-resilience-auditor",
            case_id=self.case_id,
            run_id=self.run_id,
            root=self.root,
        )
        semantic = load_role_context_slice(
            self.handle["role_slice_handles"]["semantic-impact-analyst"]["slice_sha256"],
            "semantic-impact-analyst",
            case_id=self.case_id,
            run_id=self.run_id,
            root=self.root,
        )
        self.assertNotIn("c", rights)
        self.assertNotIn("t", rights)
        self.assertNotIn("c", runtime)
        self.assertEqual(semantic["t"], 4652.4)
        self.assertEqual(
            semantic["context_execution_recipe_id"],
            "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
        )
        self.assertEqual(rights["context_cache_status"], "HIT_ROLE_ISOLATED_SLICE")
        self.assertNotIn("role_slices", self.capsule)
        self.assertEqual(self.handle["skill_invocation_count"], 1)

    def test_cross_role_slice_access_is_denied(self) -> None:
        rights_ref = self.handle["role_slice_handles"]["data-rights-steward"]["slice_sha256"]
        with self.assertRaisesRegex(ValueError, "access denied"):
            load_role_context_slice(
                rights_ref,
                "semantic-impact-analyst",
                case_id=self.case_id,
                run_id=self.run_id,
                root=self.root,
            )

    def test_second_build_is_content_addressed_cache_hit(self) -> None:
        _, handle = build_run_context_capsule(
            case_id=self.case_id,
            run_id=self.run_id,
            payload=self.payload,
            selected_workers=self.roles,
            execution_policy_id="FINFLUX-BOUNDED-EXECUTION-V0.1",
            root_route_decision_handle={"decision_sha256": "6" * 64},
            local_root=self.root,
        )
        self.assertEqual(handle["capsule_sha256"], self.handle["capsule_sha256"])
        self.assertEqual(handle["cache_status"], "CAPSULE_HIT")

    def test_tampered_capsule_is_rejected(self) -> None:
        slice_ref = self.handle["role_slice_handles"]["evidence-investigator"]["slice_sha256"]
        path = self.root / f"{slice_ref}.json"
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["case_id"] = "TAMPERED"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_role_context_slice(
                slice_ref,
                "evidence-investigator",
                case_id=self.case_id,
                run_id=self.run_id,
                root=self.root,
            )

    def test_gateway_loads_capsule_and_records_zero_model_transport(self) -> None:
        task_id = (
            f"task-{self.case_id}-LIVE-20260831120000-ca5e01-"
            "semantic-impact-analyst"
        )
        raw_args = [
            "--",
            "--role", "semantic-impact-analyst",
            "--asset", "futures",
            "--case-id", self.case_id,
            "--run-id", self.run_id,
            "--task-id", task_id,
            "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--scenario", "blocked",
            "--context-capsule-ref", self.handle["role_slice_handles"]["semantic-impact-analyst"]["slice_sha256"],
        ]
        with patch.dict(
            os.environ,
            {
                "FINFLUX_TASK_ROOT": str(self.tasks),
                "FINFLUX_CONTEXT_CAPSULE_ROOT": str(self.root),
            },
            clear=False,
        ):
            code, _, _, receipt = execute("bounded-worker", raw_args, 30)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["provider_tokens"], 0)
        self.assertEqual(receipt["context_transport"], "CONTENT_ADDRESSED_ROLE_SLICE")
        self.assertEqual(
            receipt["execution_recipe_id"],
            "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
        )
        self.assertEqual(
            receipt["execution_recipe_source"], "SIGNED_ROLE_CONTEXT_SLICE"
        )
        result = json.loads(
            (self.tasks / task_id / "semantic_impact_result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(result["impact"]["financial_misstatement_cny_per_contract"], 1200.0)
        self.assertEqual(result["context_cache_status"], "HIT_ROLE_ISOLATED_SLICE")
        self.assertEqual(result["context_skill_invocations"][0]["status"], "SUCCESS")
        self.assertEqual(
            result["operational_memory_recipe_id"],
            "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
        )

    def test_signed_gateway_derives_complete_invocation_from_role_slice(self) -> None:
        slice_ref = self.handle["role_slice_handles"]["independent-validator"][
            "slice_sha256"
        ]
        task_id = (
            f"task-{self.case_id}-LIVE-20260831120000-ca5e01-"
            "independent-validator"
        )
        with patch.dict(
            os.environ,
            {
                "FINFLUX_TASK_ROOT": str(self.tasks),
                "FINFLUX_CONTEXT_CAPSULE_ROOT": str(self.root),
            },
            clear=False,
        ):
            code, _, _, receipt = execute(
                "signed-worker",
                [
                    "--",
                    "--role", "independent-validator",
                    "--context-capsule-ref", slice_ref,
                ],
                30,
            )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["task_id"], task_id)
        self.assertEqual(receipt["run_id"], self.run_id)
        self.assertEqual(receipt["case_id"], self.case_id)
        self.assertEqual(receipt["context_capsule_sha256"], slice_ref)
        self.assertEqual(receipt["provider_tokens"], 0)

    def test_gateway_rejects_unsigned_execution_recipe_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            validate_tool_args(
                "bounded-worker",
                [
                    "--role", "semantic-impact-analyst",
                    "--asset", "futures",
                    "--case-id", self.case_id,
                    "--run-id", self.run_id,
                    "--task-id", f"task-{self.case_id}-LIVE-20260831120000-ca5e01-semantic-impact-analyst",
                    "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
                    "--scenario", "blocked",
                    "--context-capsule-ref", self.handle["role_slice_handles"]["semantic-impact-analyst"]["slice_sha256"],
                    "--execution-recipe-id", "FINFLUX_SIGNED_MEMORY_GUARDED_V1",
                ],
            )

    def test_gateway_derives_guarded_recipe_from_signed_slice(self) -> None:
        _, guarded = build_run_context_capsule(
            case_id=self.case_id,
            run_id=self.run_id,
            payload=self.payload,
            selected_workers=["semantic-impact-analyst"],
            execution_policy_id="FINFLUX-BOUNDED-EXECUTION-V0.1",
            root_route_decision_handle={"decision_sha256": "6" * 64},
            execution_recipe_id="FINFLUX_SIGNED_MEMORY_GUARDED_V1",
            local_root=self.root,
        )
        task_id = (
            f"task-{self.case_id}-LIVE-20260831120000-ca5e01-"
            "semantic-impact-analyst"
        )
        raw_args = [
            "--", "--role", "semantic-impact-analyst",
            "--asset", "futures", "--case-id", self.case_id,
            "--run-id", self.run_id, "--task-id", task_id,
            "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "--scenario", "blocked",
            "--context-capsule-ref",
            guarded["role_slice_handles"]["semantic-impact-analyst"]["slice_sha256"],
        ]
        with patch.dict(
            os.environ,
            {
                "FINFLUX_TASK_ROOT": str(self.tasks),
                "FINFLUX_CONTEXT_CAPSULE_ROOT": str(self.root),
            },
            clear=False,
        ):
            code, _, _, receipt = execute("bounded-worker", raw_args, 30)
        self.assertEqual(code, 0)
        self.assertEqual(
            receipt["execution_recipe_id"],
            "FINFLUX_SIGNED_MEMORY_GUARDED_V1",
        )
        self.assertEqual(
            receipt["execution_recipe_source"], "SIGNED_ROLE_CONTEXT_SLICE"
        )

    def test_gateway_rejects_mixed_context_transports(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            validate_tool_args(
                "bounded-worker",
                [
                    "--role", "semantic-impact-analyst",
                    "--asset", "futures",
                    "--case-id", self.case_id,
                    "--run-id", self.run_id,
                    "--task-id", f"task-{self.case_id}-20260831120000-mixed",
                    "--policy-id", "FINFLUX-BOUNDED-EXECUTION-V0.1",
                    "--scenario", "blocked",
                    "--live-payload-b64", "legacy",
                    "--context-capsule-ref", self.handle["capsule_sha256"],
                ],
            )


if __name__ == "__main__":
    unittest.main()
