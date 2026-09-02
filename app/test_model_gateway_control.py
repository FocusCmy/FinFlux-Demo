from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_gateway_control import (
    ROUTE_ATTESTATION_PROTOCOL,
    ModelGatewayControlError,
    _sha,
    close_model_gateway_run,
    extend_model_gateway_same_run_recovery_window,
    prepare_model_gateway_run,
    rearm_model_gateway_expired_before_first_call,
    rotate_model_gateway_actor_identities,
)


def write_attestation(root: Path, routed: bool = True) -> None:
    payload = {
        "protocol": ROUTE_ATTESTATION_PROTOCOL,
        "status": "READY",
        "provider_name": "openai-compat",
        "provider_custom_url": (
            "http://finflux-model-budget-gateway:8090/v1"
            if routed
            else "https://provider.example/v1"
        ),
        "expected_gateway_url": "http://finflux-model-budget-gateway:8090/v1",
        "verified_at_utc": "2026-09-01T00:00:00+00:00",
    }
    payload["attestation_sha256"] = _sha(payload)
    root.mkdir(parents=True, exist_ok=True)
    (root / "route-attestation.json").write_text(json.dumps(payload), encoding="utf-8")


class ModelGatewayControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, run_id: str = "RUN-1") -> dict:
        return prepare_model_gateway_run(
            self.root,
            run_id=run_id,
            provider_token_hard_cap=120_000,
            max_model_calls=12,
            max_output_tokens_per_call=2048,
            max_wall_time_seconds=600,
            provider_usage_baseline={
                "date_utc": "2026-09-01",
                "captured_at_utc": "2026-09-01T00:00:00+00:00",
                "by_agent": [],
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "total_tokens": 550,
                "call_count": 4,
            },
            actor_identities={"Evidence Investigator": "test-identity-secret"},
            actor_task_ids={"Evidence Investigator": "TASK-EVIDENCE-1"},
        )

    def test_requires_verified_sidecar_route(self) -> None:
        write_attestation(self.root, routed=False)
        with self.assertRaises(ModelGatewayControlError):
            self.prepare()

    def test_binds_exactly_one_run_and_closes_fail_closed(self) -> None:
        write_attestation(self.root)
        binding = self.prepare()
        self.assertEqual(binding["state"], "ACTIVE")
        self.assertNotIn("test-identity-secret", json.dumps(binding))
        self.assertRegex(
            binding["actor_identity_sha256"]["Evidence Investigator"], r"^[0-9a-f]{64}$"
        )
        with self.assertRaises(ModelGatewayControlError):
            self.prepare("RUN-2")
        closed = close_model_gateway_run(self.root, run_id="RUN-1", state="COMPLETED")
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["terminal_state"], "COMPLETED")

    def test_close_is_idempotent_and_terminal_receipt_is_immutable(self) -> None:
        write_attestation(self.root)
        self.prepare("RUN-IDEMPOTENT")
        first = close_model_gateway_run(
            self.root, run_id="RUN-IDEMPOTENT", state="AWAITING_HUMAN"
        )
        second = close_model_gateway_run(
            self.root, run_id="RUN-IDEMPOTENT", state="AWAITING_HUMAN"
        )
        self.assertEqual(second, first)
        with self.assertRaisesRegex(
            ModelGatewayControlError,
            "ALREADY_CLOSED_WITH_DIFFERENT_STATE",
        ):
            close_model_gateway_run(
                self.root, run_id="RUN-IDEMPOTENT", state="COMPLETED"
            )

    def test_close_rejects_tampered_binding(self) -> None:
        write_attestation(self.root)
        self.prepare("RUN-TAMPER")
        path = self.root / "active_run.json"
        binding = json.loads(path.read_text(encoding="utf-8"))
        binding["max_model_calls"] = 999
        path.write_text(json.dumps(binding), encoding="utf-8")
        with self.assertRaisesRegex(ModelGatewayControlError, "HASH_MISMATCH"):
            close_model_gateway_run(
                self.root, run_id="RUN-TAMPER", state="STOPPED_BY_GATE"
            )

    def test_expired_zero_usage_binding_can_rearm_same_run_without_reset(self) -> None:
        write_attestation(self.root)
        self.prepare("RUN-DELAYED")
        path = self.root / "active_run.json"
        binding = json.loads(path.read_text(encoding="utf-8"))
        binding["expires_at_utc"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        binding.pop("binding_sha256", None)
        binding["binding_sha256"] = _sha(binding)
        path.write_text(json.dumps(binding), encoding="utf-8")

        receipt = rearm_model_gateway_expired_before_first_call(
            self.root,
            run_id="RUN-DELAYED",
            requested_by="test.operator",
            reason="delayed Matrix delivery",
        )
        self.assertFalse(receipt["new_run_created"])
        self.assertFalse(receipt["usage_counters_reset"])
        renewed = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreater(
            datetime.fromisoformat(renewed["expires_at_utc"]),
            datetime.now(timezone.utc),
        )

    def test_actor_identity_rotation_preserves_run_and_budget(self) -> None:
        write_attestation(self.root)
        before = self.prepare("RUN-ROTATE")
        receipt = rotate_model_gateway_actor_identities(
            self.root,
            run_id="RUN-ROTATE",
            actor_identity_sha256={"Evidence Investigator": "a" * 64},
            requested_by="test.operator",
            reason="restore volatile runtime headers",
        )
        after = json.loads((self.root / "active_run.json").read_text(encoding="utf-8"))
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["provider_token_hard_cap"], before["provider_token_hard_cap"])
        self.assertFalse(receipt["usage_counters_reset"])

    def test_first_same_run_recovery_extension_preserves_usage_counters(self) -> None:
        write_attestation(self.root)
        before = self.prepare("RUN-RECOVER")
        ledger = {
            "protocol": "FINFLUX_MODEL_GATEWAY_LEDGER_V1",
            "run_id": "RUN-RECOVER",
            "status": "ACTIVE",
            "provider_call_count": 2,
            "total_tokens": 900,
        }
        (self.root / "gateway_ledger.json").write_text(
            json.dumps(ledger), encoding="utf-8"
        )
        receipt = extend_model_gateway_same_run_recovery_window(
            self.root,
            run_id="RUN-RECOVER",
            reason="Case Lead did not dispatch Workers",
        )
        after = json.loads((self.root / "active_run.json").read_text(encoding="utf-8"))
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["provider_token_hard_cap"], before["provider_token_hard_cap"])
        self.assertEqual(receipt["provider_calls_before_extension"], 2)
        self.assertEqual(receipt["provider_tokens_before_extension"], 900)
        self.assertFalse(receipt["token_and_call_counters_reset"])

    def test_closed_run_cannot_be_reopened_and_tampered_existing_binding_blocks_prepare(self) -> None:
        write_attestation(self.root)
        self.prepare("RUN-CLOSED")
        close_model_gateway_run(
            self.root, run_id="RUN-CLOSED", state="AWAITING_HUMAN"
        )
        with self.assertRaisesRegex(ModelGatewayControlError, "RUN_ALREADY_CLOSED"):
            self.prepare("RUN-CLOSED")

        path = self.root / "active_run.json"
        binding = json.loads(path.read_text(encoding="utf-8"))
        binding["run_id"] = "RUN-TAMPERED"
        path.write_text(json.dumps(binding), encoding="utf-8")
        with self.assertRaisesRegex(ModelGatewayControlError, "HASH_MISMATCH"):
            self.prepare("RUN-NEW")

    def test_archives_previous_ledger_before_new_run(self) -> None:
        write_attestation(self.root)
        self.prepare("RUN-1")
        (self.root / "gateway_ledger.json").write_text(
            json.dumps({"protocol": "x", "run_id": "RUN-1", "total_tokens": 1}),
            encoding="utf-8",
        )
        close_model_gateway_run(self.root, run_id="RUN-1", state="COMPLETED")
        binding = self.prepare("RUN-2")
        self.assertEqual(binding["run_id"], "RUN-2")
        self.assertFalse((self.root / "gateway_ledger.json").exists())
        self.assertEqual(len(list((self.root / "history").glob("RUN-1-*.json"))), 1)

    def test_rejects_incomplete_or_inconsistent_provider_baseline(self) -> None:
        write_attestation(self.root)
        baseline = {
            "date_utc": "2026-09-01",
            "captured_at_utc": "2026-09-01T00:00:00+00:00",
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 99,
            "call_count": 1,
        }
        with self.assertRaisesRegex(ModelGatewayControlError, "TOTAL_MISMATCH"):
            prepare_model_gateway_run(
                self.root,
                run_id="RUN-BAD",
                provider_token_hard_cap=100,
                max_model_calls=1,
                max_output_tokens_per_call=10,
                max_wall_time_seconds=60,
                provider_usage_baseline=baseline,
                actor_identities={"Evidence Investigator": "test-identity-secret"},
                actor_task_ids={"Evidence Investigator": "TASK-EVIDENCE-1"},
            )

    def test_actor_and_task_sets_must_match_and_tasks_must_be_unique(self) -> None:
        write_attestation(self.root)
        kwargs = {
            "run_id": "RUN-TASK",
            "provider_token_hard_cap": 100,
            "max_model_calls": 1,
            "max_output_tokens_per_call": 10,
            "max_wall_time_seconds": 60,
            "provider_usage_baseline": {
                "date_utc": "2026-09-01",
                "captured_at_utc": "2026-09-01T00:00:00+00:00",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
            },
            "actor_identities": {"A": "secret-a", "B": "secret-b"},
        }
        with self.assertRaisesRegex(ModelGatewayControlError, "TASK_SET_MISMATCH"):
            prepare_model_gateway_run(**kwargs, control_root=self.root, actor_task_ids={"A": "T1"})
        with self.assertRaisesRegex(ModelGatewayControlError, "TASK_IDS_NOT_UNIQUE"):
            prepare_model_gateway_run(
                **kwargs, control_root=self.root, actor_task_ids={"A": "T1", "B": "T1"}
            )


if __name__ == "__main__":
    unittest.main()
