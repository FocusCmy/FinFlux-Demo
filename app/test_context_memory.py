from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory import (
    COMMIT_PROTOCOL,
    LOOKUP_PROTOCOL,
    ContextMemoryAdapter,
    ContextMemoryPolicyError,
    HumanApproval,
    MemoryBudget,
    MemoryReference,
    OpenVikingConfig,
    OpenVikingHTTPClient,
    OpenVikingTransportError,
    build_context_memory_adapter,
    content_sha256,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.closed = False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(
        self,
        *,
        results: list[dict[str, object]] | None = None,
        fail_find: bool = False,
        remote_write: bool = False,
    ) -> None:
        self.enabled = True
        self.config = OpenVikingConfig(
            base_url="https://openviking.test:1933",
            api_key="secret",
            timeout_seconds=0.2,
            target_root="viking://~/memories/finflux",
            remote_write_enabled=remote_write,
        )
        self.results = results or []
        self.fail_find = fail_find
        self.find_calls: list[dict[str, object]] = []
        self.write_calls: list[dict[str, object]] = []

    def find(self, **kwargs: object) -> dict[str, object]:
        self.find_calls.append(dict(kwargs))
        if self.fail_find:
            raise OpenVikingTransportError("OPENVIKING_TIMEOUT")
        return {"status": "ok", "result": {"resources": self.results}}

    def write_reference(self, **kwargs: object) -> dict[str, object]:
        self.write_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "result": {
                "uri": kwargs["target_uri"],
                "content_updated": True,
                "semantic_status": "queued",
                "vector_status": "queued",
            },
        }


class _BlockingClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__(remote_write=True)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def write_reference(self, **kwargs: object) -> dict[str, object]:
        self.entered.set()
        self.release.wait(timeout=2)
        try:
            return super().write_reference(**kwargs)
        finally:
            self.completed.set()


class _BadAckClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__(remote_write=True)

    def write_reference(self, **kwargs: object) -> dict[str, object]:
        self.write_calls.append(dict(kwargs))
        return {"status": "ok", "result": {"content_updated": False}}


class ContextMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "context-cache"
        self.run_id = "RUN-LIVE-20260901010101-abc123"
        self.role = "semantic-impact-analyst"
        self.reference = MemoryReference(
            run_id=self.run_id,
            role=self.role,
            summary="已签署结论：日结算用途必须使用交易所结算价。",
            uri="https://example.test/evidence/settlement-rule",
            content_sha256=content_sha256(b"immutable source response"),
        )
        self.approval = HumanApproval(
            decision="APPROVE_PASS",
            signer_id="@finflux-data-owner:matrix.test",
            signature_sha256="a" * 64,
            signed_at="2026-09-01T01:10:00+00:00",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_http_client_uses_configured_service_headers_and_find_contract(self) -> None:
        captured: dict[str, object] = {}
        response = _Response({"status": "ok", "result": {"resources": []}})

        def opener(request: object, *, timeout: float) -> _Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return response

        config = OpenVikingConfig(
            base_url="https://memory.internal.example",
            api_key="dummy-key",
            timeout_seconds=0.35,
            account="desk-a",
            user="reviewer-a",
        )
        client = OpenVikingHTTPClient(config, opener=opener)
        client.find(
            query="settlement definition",
            target_uri="viking://~/memories/finflux/run/role",
            limit=4,
        )
        request = captured["request"]
        self.assertEqual(request.full_url, "https://memory.internal.example/api/v1/search/find")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(captured["timeout"], 0.35)
        self.assertEqual(request.get_header("X-api-key"), "dummy-key")
        self.assertIsNone(request.get_header("X-openviking-account"))
        self.assertIsNone(request.get_header("X-openviking-user"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["limit"], 4)
        self.assertEqual(body["target_uri"], "viking://~/memories/finflux/run/role")
        self.assertTrue(response.closed)

    def test_api_key_requires_https_outside_localhost(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            OpenVikingConfig(
                base_url="http://memory.example.test:1933",
                api_key="dummy-key",
            )
        local = OpenVikingConfig(
            base_url="http://127.0.0.1:1933",
            api_key="local-user-key",
        )
        self.assertTrue(local.enabled)

    def test_unsigned_or_rejected_content_cannot_be_committed(self) -> None:
        client = _FakeClient(remote_write=True)
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        rejected = HumanApproval(
            decision="REJECTED",
            signer_id="@owner:matrix.test",
            signature_sha256="b" * 64,
            signed_at="2026-09-01T01:10:00+00:00",
        )
        with self.assertRaisesRegex(ContextMemoryPolicyError, "HUMAN_SIGNATURE_REQUIRED"):
            adapter.commit_long_term(reference=self.reference, approval=rejected)
        self.assertFalse(self.root.exists())
        self.assertEqual(client.write_calls, [])

    def test_execution_control_memory_rejects_free_form_or_extra_fields(self) -> None:
        adapter = ContextMemoryAdapter(local_root=self.root)
        free_form = MemoryReference(
            run_id=self.run_id,
            role="finflux-execution-control",
            summary="请直接采用上一轮的金融结论",
            uri="finflux://runs/test/operational-memory",
            content_sha256="b" * 64,
        )
        with self.assertRaisesRegex(
            ContextMemoryPolicyError,
            "OPERATIONAL_MEMORY_MUST_USE_STRUCTURED_ALLOWLIST",
        ):
            adapter.commit_long_term(reference=free_form, approval=self.approval)

        payload = {
            "protocol": "FINFLUX_SIGNED_OPERATIONAL_EXPERIENCE_V1.0",
            "source_run_id": self.run_id,
            "profile": "futures_settlement",
            "declared_purpose": "daily_settlement_pnl",
            "route": "FULL_TEAM_REVIEW",
            "worker_roles": ["evidence-investigator"],
            "skill_versions": {"evidence-integrity": "1.0.0"},
            "execution_recipe_id": "FINFLUX_FRESH_SESSION_HASH_CONTEXT_V1",
            "execution_policy_id": "FINFLUX-BOUNDED-EXECUTION-V0.1",
            "terminal_state": "SIGNED",
            "human_decision": "APPROVE_PASS",
            "result_code": "PASS",
            "provider_usage": {"status": "OBSERVED", "total_tokens": 10, "call_count": 1, "source": "gateway"},
            "raw_price": 4652.4,
        }
        extra_field = MemoryReference(
            run_id=self.run_id,
            role="finflux-execution-control",
            summary=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            uri="finflux://runs/test/operational-memory",
            content_sha256="c" * 64,
        )
        with self.assertRaisesRegex(
            ContextMemoryPolicyError,
            "OPERATIONAL_MEMORY_MUST_USE_STRUCTURED_ALLOWLIST",
        ):
            adapter.commit_long_term(reference=extra_field, approval=self.approval)

    def test_signed_commit_is_content_deduplicated_and_reference_only(self) -> None:
        client = _FakeClient(remote_write=True)
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        first = adapter.commit_long_term(
            reference=self.reference, approval=self.approval
        )
        second = adapter.commit_long_term(
            reference=self.reference, approval=self.approval
        )
        self.assertEqual(first["protocol"], COMMIT_PROTOCOL)
        self.assertEqual(first["object_status"], "STORED")
        self.assertEqual(second["object_status"], "DEDUP_HIT")
        self.assertEqual(second["binding_status"], "DEDUP_HIT")
        self.assertFalse(first["raw_financial_data_committed"])
        self.assertFalse(first["finflux_agent_model_called"])
        self.assertEqual(first["finflux_agent_llm_tokens"], 0)
        self.assertEqual(first["openviking_provider_usage"], "NOT_CAPTURED")
        self.assertEqual(first["remote_status"], "OUTBOX_QUEUED")
        self.assertEqual(second["remote_status"], "OUTBOX_DEDUP_HIT")
        self.assertEqual(client.write_calls, [])
        self.assertEqual(adapter.status()["local"]["outbox"], 1)
        drained = adapter.drain_outbox()
        self.assertEqual(drained["accepted"], 1)
        self.assertEqual(drained["delivered"], 0)
        self.assertEqual(len(client.write_calls), 1)
        self.assertEqual(
            adapter.status()["last_remote"]["status"], "WRITE_ACCEPTED"
        )
        delivery = adapter.delivery_status(self.run_id)
        self.assertEqual(
            delivery["protocol"],
            "FINFLUX_CONTEXT_MEMORY_REMOTE_WRITE_ACCEPTANCE_V1",
        )
        self.assertEqual(delivery["status"], "WRITE_ACCEPTED")
        self.assertEqual(len(delivery["accepted"]), 1)
        self.assertFalse(delivery["delivered"])
        self.assertEqual(
            adapter.delivery_status("RUN-OTHER-SIGNED-CONTEXT")["status"],
            "NOT_REQUESTED_OR_NOT_CAPTURED",
        )
        remote_reference = client.write_calls[0]["reference"]
        self.assertEqual(
            set(remote_reference),
            {
                "protocol",
                "run_id",
                "role",
                "summary",
                "uri",
                "content_sha256",
                "human_signature_sha256",
            },
        )
        self.assertNotIn("content", remote_reference)
        self.assertNotIn("payload", remote_reference)

    def test_empty_outbox_is_no_work_not_write_accepted(self) -> None:
        adapter = ContextMemoryAdapter(
            client=_FakeClient(remote_write=True), local_root=self.root
        )
        receipt = adapter.drain_outbox()
        self.assertEqual(receipt["status"], "NO_WORK")
        self.assertEqual(receipt["attempted"], 0)
        self.assertEqual(receipt["accepted"], 0)
        self.assertEqual(receipt["delivered"], 0)

    def test_same_run_role_lookup_is_cached_and_cross_role_result_is_rejected(self) -> None:
        scope = f"viking://~/memories/finflux/{self.role}"
        ContextMemoryAdapter(local_root=self.root).commit_long_term(
            reference=self.reference, approval=self.approval
        )
        client = _FakeClient(
            results=[
                {
                    "uri": f"{scope}/{self.run_id}/{self.reference.content_sha256}.json",
                    "abstract": self.reference.summary,
                    "metadata": {"content_sha256": self.reference.content_sha256},
                    "score": 0.91,
                    "content": "RAW FINANCIAL DATA MUST NEVER BE RETURNED",
                },
                {
                    "uri": f"viking://~/memories/finflux/other-role/historical-run/x.json",
                    "abstract": "cross-role data",
                    "content_sha256": "c" * 64,
                    "score": 0.99,
                },
            ]
        )
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        first = adapter.lookup(
            run_id=self.run_id, role=self.role, query="结算价规则"
        )
        second = adapter.lookup(
            run_id=self.run_id, role=self.role, query="结算价规则"
        )
        self.assertEqual(first["protocol"], LOOKUP_PROTOCOL)
        self.assertEqual(
            first["metrics"]["source"],
            "OPENVIKING_HTTP_SIGNED_LOCAL_BINDING",
        )
        self.assertFalse(first["metrics"]["cache_hit"])
        self.assertEqual(second["metrics"]["source"], "RUN_ROLE_CACHE")
        self.assertTrue(second["metrics"]["cache_hit"])
        self.assertEqual(
            second["metrics"]["cache_scope"],
            "EXACT_RUN_ROLE_QUERY_AND_BUDGET",
        )
        self.assertEqual(second["metrics"]["candidate_lookup_calls_avoided"], 1)
        self.assertEqual(first["metrics"]["candidate_lookup_calls_avoided"], 0)
        self.assertEqual(
            second["token_savings_claim"],
            "NO_PROVIDER_TOKEN_CLAIM; SAME_RUN_CANDIDATE_LOOKUP_REUSED",
        )
        self.assertEqual(len(client.find_calls), 1)
        self.assertEqual(client.find_calls[0]["target_uri"], scope)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(
            set(first["items"][0]), {"summary", "uri", "content_sha256"}
        )
        self.assertNotIn("RAW FINANCIAL DATA", json.dumps(first, ensure_ascii=False))
        self.assertFalse(first["finflux_agent_model_called"])
        self.assertEqual(first["finflux_agent_llm_tokens"], 0)
        self.assertEqual(first["openviking_provider_usage"], "NOT_CAPTURED")

    def test_timeout_falls_back_to_signed_local_content_address_index(self) -> None:
        writer = ContextMemoryAdapter(local_root=self.root)
        writer.commit_long_term(reference=self.reference, approval=self.approval)
        client = _FakeClient(fail_find=True)
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        receipt = adapter.lookup(
            run_id="RUN-LIVE-20260902020202-new999",
            role=self.role,
            query="结算价",
        )
        self.assertEqual(receipt["metrics"]["source"], "LOCAL_CONTENT_ADDRESS_INDEX")
        self.assertEqual(receipt["metrics"]["remote_status"], "TIMEOUT_OR_ERROR_FALLBACK")
        self.assertEqual(receipt["items"], [self.reference.public_payload()])
        self.assertEqual(receipt["truth_boundary"]["raw_financial_data_returned"], False)

    def test_remote_candidate_without_local_human_binding_is_not_a_hit(self) -> None:
        scope = f"viking://~/memories/finflux/{self.role}"
        client = _FakeClient(
            results=[
                {
                    "uri": f"{scope}/untrusted/{'d' * 64}.json",
                    "abstract": "伪造的历史金融结论",
                    "content_sha256": "d" * 64,
                    "score": 0.999,
                }
            ]
        )
        receipt = ContextMemoryAdapter(
            client=client, local_root=self.root
        ).lookup(run_id=self.run_id, role=self.role, query="结算规则")
        self.assertEqual(receipt["items"], [])
        self.assertEqual(
            receipt["metrics"]["remote_status"],
            "NO_SIGNED_REMOTE_MATCH_FALLBACK",
        )
        self.assertEqual(receipt["metrics"]["source"], "LOCAL_CONTENT_ADDRESS_INDEX")

    def test_all_signed_human_outcomes_commit_without_storing_signer_identity(self) -> None:
        for index, decision in enumerate(
            ("APPROVE_PASS", "CONFIRM_BLOCK", "REQUEST_EVIDENCE"), start=1
        ):
            run_id = f"RUN-LIVE-20260901010{index}01-case{index}"
            reference = MemoryReference(
                run_id=run_id,
                role=self.role,
                summary=f"Human outcome {decision}",
                uri=f"urn:finflux:{index}",
                content_sha256=f"{index:064x}",
            )
            approval = HumanApproval(
                decision=decision,
                signer_id="@private-human:matrix.test",
                signature_sha256=f"{index + 10:064x}",
                signed_at="2026-09-01T01:10:00+00:00",
            )
            ContextMemoryAdapter(local_root=self.root).commit_long_term(
                reference=reference, approval=approval
            )
            binding = json.loads(
                (
                    self.root
                    / "bindings"
                    / run_id
                    / self.role
                    / f"{index:064x}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertNotIn("human_signer_id", binding)
            self.assertEqual(len(binding["human_signer_sha256"]), 64)
            self.assertNotIn("@private-human", json.dumps(binding))

    def test_lookup_enforces_character_token_and_item_budgets(self) -> None:
        scope = f"viking://~/memories/finflux/{self.role}"
        results = []
        writer = ContextMemoryAdapter(local_root=self.root)
        for index in range(1, 4):
            digest = f"{index * 64:064x}"
            reference = MemoryReference(
                run_id=f"RUN-HISTORY-{index}",
                role=self.role,
                summary="结算规则摘要" * 100,
                uri=f"urn:finflux:history:{index}",
                content_sha256=digest,
            )
            writer.commit_long_term(reference=reference, approval=self.approval)
            results.append(
                {
                    "uri": f"{scope}/historical-run/{digest}.json",
                    "abstract": reference.summary,
                    "content_sha256": digest,
                    "score": 0.8,
                }
            )
        adapter = ContextMemoryAdapter(
            client=_FakeClient(results=results), local_root=self.root
        )
        budget = MemoryBudget(max_characters=180, max_token_estimate=100, max_items=2)
        receipt = adapter.lookup(
            run_id=self.run_id,
            role=self.role,
            query="规则",
            budget=budget,
        )
        self.assertLessEqual(len(receipt["items"]), 2)
        self.assertLessEqual(receipt["metrics"]["candidate_characters"], 180)
        self.assertLessEqual(receipt["metrics"]["candidate_token_estimate"], 100)
        self.assertEqual(receipt["metrics"]["injected_characters"], 0)
        self.assertEqual(receipt["metrics"]["injected_token_estimate"], 0)

    def test_environment_builder_disables_http_when_url_is_blank(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FINFLUX_OPENVIKING_URL": "",
                "FINFLUX_CONTEXT_MEMORY_LOCAL_ROOT": str(self.root),
                "FINFLUX_CONTEXT_MEMORY_BACKEND": "local",
            },
            clear=False,
        ):
            adapter = build_context_memory_adapter()
        self.assertIsNone(adapter.client)
        self.assertEqual(adapter.backend, "local")
        self.assertTrue(adapter.status()["configured"])
        self.assertEqual(adapter.local_root, self.root.resolve())

    def test_shared_external_runtime_env_configures_openviking_without_copying_secrets(self) -> None:
        env_file = Path(self.temp.name) / "external-runtime.env"
        env_file.write_text(
            "\n".join(
                (
                    "FINFLUX_CONTEXT_MEMORY_BACKEND=openviking",
                    "FINFLUX_OPENVIKING_URL=http://127.0.0.1:1933",
                    "FINFLUX_OPENVIKING_API_KEY=external-secret",
                    "FINFLUX_OPENVIKING_TIMEOUT_MS=450",
                    "FINFLUX_OPENVIKING_TARGET_ROOT=viking://~/memories/finflux",
                    "UNRELATED_SECRET=must-not-be-read",
                )
            ),
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"FINFLUX_RUNTIME_ENV_FILE": str(env_file)},
            clear=True,
        ):
            adapter = build_context_memory_adapter(local_root=self.root)
        self.assertEqual(adapter.backend, "openviking")
        self.assertIsNotNone(adapter.client)
        self.assertEqual(adapter.client.config.api_key, "external-secret")
        self.assertEqual(adapter.client.config.timeout_seconds, 0.45)

    def test_builder_respects_openviking_backend_and_reports_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FINFLUX_CONTEXT_MEMORY_BACKEND": "openviking",
                "FINFLUX_OPENVIKING_URL": "http://memory.test:1933",
                "FINFLUX_CONTEXT_MEMORY_LOCAL_ROOT": str(self.root),
            },
            clear=False,
        ):
            adapter = build_context_memory_adapter()
        self.assertEqual(adapter.backend, "openviking")
        self.assertIsNotNone(adapter.client)
        status = adapter.status()
        self.assertTrue(status["configured"])
        self.assertFalse(status["prompt_injected"])
        self.assertEqual(
            set(status["local"]),
            {"objects", "bindings", "outbox", "accepted_remote_writes"},
        )

    def test_outbox_schedule_is_nonblocking_single_flight_and_not_in_signing_thread(self) -> None:
        client = _BlockingClient()
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        adapter.commit_long_term(reference=self.reference, approval=self.approval)
        self.assertEqual(client.write_calls, [])

        started = time.perf_counter()
        scheduled = adapter.schedule_outbox_drain(limit=20)
        elapsed = time.perf_counter() - started
        self.assertEqual(scheduled["status"], "SCHEDULED")
        self.assertTrue(scheduled["non_blocking"])
        self.assertLess(elapsed, 0.2)
        self.assertTrue(client.entered.wait(timeout=1))
        self.assertTrue(adapter.status()["outbox_drain_running"])
        self.assertEqual(
            adapter.schedule_outbox_drain()["status"], "ALREADY_RUNNING"
        )

        client.release.set()
        self.assertTrue(client.completed.wait(timeout=1))
        deadline = time.time() + 1
        while adapter.status()["outbox_drain_running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(adapter.status()["outbox_drain_running"])
        self.assertEqual(adapter.status()["local"]["outbox"], 0)

    def test_outbox_schedule_is_disabled_without_remote_write(self) -> None:
        adapter = ContextMemoryAdapter(local_root=self.root)
        receipt = adapter.schedule_outbox_drain()
        self.assertEqual(receipt["status"], "DISABLED")
        self.assertFalse(adapter.status()["outbox_drain_running"])

    def test_invalid_openviking_business_ack_keeps_outbox_for_retry(self) -> None:
        client = _BadAckClient()
        adapter = ContextMemoryAdapter(client=client, local_root=self.root)
        adapter.commit_long_term(reference=self.reference, approval=self.approval)
        receipt = adapter.drain_outbox()
        self.assertEqual(receipt["status"], "PARTIAL_OR_FAILED")
        self.assertEqual(receipt["accepted"], 0)
        self.assertEqual(receipt["failed"], 1)
        self.assertEqual(adapter.status()["local"]["outbox"], 1)
        self.assertEqual(adapter.status()["local"]["accepted_remote_writes"], 0)


if __name__ == "__main__":
    unittest.main()
