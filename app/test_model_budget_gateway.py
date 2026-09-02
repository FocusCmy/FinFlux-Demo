from __future__ import annotations

import json
import hashlib
import http.client
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from model_budget_gateway import (
    BINDING_PROTOCOL,
    BudgetLedger,
    BudgetGatewayEngine,
    GatewayBlocked,
    build_handler,
    enforce_request_output_cap,
    ensure_stream_usage,
    parse_provider_usage,
    _canonical_sha256,
)


def write_binding(root: Path, *, cap: int = 10_000, calls: int = 2) -> None:
    baseline = {
        "protocol": "FINFLUX_PROVIDER_USAGE_BASELINE_V1",
        "run_id": "RUN-LIVE-GATEWAY-1",
        "captured_before_model_dispatch": True,
        "snapshot": {"date_utc": "2026-09-01", "by_agent": []},
        "cumulative_totals": {
            "prompt_tokens": 500,
            "completion_tokens": 50,
            "total_tokens": 550,
            "call_count": 4,
        },
    }
    baseline["baseline_sha256"] = _canonical_sha256(baseline)
    binding = {
        "protocol": BINDING_PROTOCOL,
        "run_id": "RUN-LIVE-GATEWAY-1",
        "state": "ACTIVE",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "provider_token_hard_cap": cap,
        "max_model_calls": calls,
        "max_output_tokens_per_call": 256,
        "provider_usage_baseline_sha256": baseline["baseline_sha256"],
        "provider_usage_baseline_total_tokens": 550,
        "actor_identity_sha256": {
            "Evidence Investigator": hashlib.sha256(b"test-identity-secret").hexdigest()
        },
        "actor_task_ids": {"Evidence Investigator": "TASK-EVIDENCE-1"},
    }
    binding["binding_sha256"] = _canonical_sha256(binding)
    root.mkdir(parents=True, exist_ok=True)
    (root / "provider_usage_baseline.json").write_text(
        json.dumps(baseline), encoding="utf-8"
    )
    (root / "active_run.json").write_text(json.dumps(binding), encoding="utf-8")


class FakeProviderHandler(BaseHTTPRequestHandler):
    calls = 0
    include_usage = True
    last_headers: dict[str, str] = {}
    last_body = b""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        type(self).last_headers = {key.lower(): value for key, value in self.headers.items()}
        type(self).last_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = {"id": "fake", "choices": [{"message": {"content": "ok"}}]}
        if type(self).include_usage:
            payload["usage"] = {
                "prompt_tokens": 120,
                "completion_tokens": 10,
                "total_tokens": 130,
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        FakeProviderHandler.calls = 0
        FakeProviderHandler.include_usage = True
        FakeProviderHandler.last_headers = {}
        FakeProviderHandler.last_body = b""
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
        self.provider_thread = threading.Thread(
            target=self.provider.serve_forever, daemon=True
        )
        self.provider_thread.start()

    def tearDown(self) -> None:
        self.provider.shutdown()
        self.provider.server_close()
        self.temporary.cleanup()

    def start_gateway(self) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
        engine = BudgetGatewayEngine(
            f"http://127.0.0.1:{self.provider.server_port}/v1", self.root
        )
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(engine))
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        return gateway, thread, f"http://127.0.0.1:{gateway.server_port}"

    def post(self, base_url: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            base_url + "/v1/chat/completions",
            data=json.dumps(
                {"model": "fake", "messages": [{"role": "user", "content": "x"}], "max_tokens": 20}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-FinFlux-Run-ID": "RUN-LIVE-GATEWAY-1",
                "X-FinFlux-Actor": "Evidence Investigator",
                "X-FinFlux-Identity": "test-identity-secret",
                "X-FinFlux-Task-ID": "TASK-EVIDENCE-1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    @staticmethod
    def request_headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-FinFlux-Run-ID": "RUN-LIVE-GATEWAY-1",
            "X-FinFlux-Actor": "Evidence Investigator",
            "X-FinFlux-Identity": "test-identity-secret",
            "X-FinFlux-Task-ID": "TASK-EVIDENCE-1",
        }

    def chunked_post(self, base_url: str, chunks: list[bytes]) -> tuple[int, dict]:
        parsed = urllib.parse.urlparse(base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=iter(chunks),
                headers=self.request_headers(),
                encode_chunked=True,
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def framed_post(
        self,
        base_url: str,
        framing_headers: dict[str, str],
        wire_body: bytes = b"",
    ) -> tuple[int, dict]:
        parsed = urllib.parse.urlparse(base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/chat/completions")
            for key, value in {**self.request_headers(), **framing_headers}.items():
                connection.putheader(key, value)
            connection.endheaders()
            if wire_body:
                connection.send(wire_body)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_valid_chunked_request_is_buffered_and_forwarded_with_content_length(self) -> None:
        write_binding(self.root, calls=1)
        gateway, _, base = self.start_gateway()
        body = json.dumps(
            {
                "model": "fake",
                "messages": [{"role": "user", "content": "chunked"}],
                "max_tokens": 20,
            },
            separators=(",", ":"),
        ).encode()
        try:
            status, payload = self.chunked_post(
                base, [body[:11], body[11:37], body[37:]]
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["id"], "fake")
            self.assertEqual(FakeProviderHandler.calls, 1)
            self.assertEqual(FakeProviderHandler.last_body, body)
            self.assertEqual(
                int(FakeProviderHandler.last_headers["content-length"]), len(body)
            )
            self.assertNotIn("transfer-encoding", FakeProviderHandler.last_headers)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_provider_receives_enforced_output_cap(self) -> None:
        write_binding(self.root, calls=1)
        gateway, _, base = self.start_gateway()
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(
                {"model": "fake", "messages": [], "max_tokens": 4096}
            ).encode(),
            headers=self.request_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            forwarded = json.loads(FakeProviderHandler.last_body)
            self.assertEqual(forwarded["max_tokens"], 256)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_valid_chunk_extensions_and_trailer_are_accepted_but_not_forwarded(self) -> None:
        write_binding(self.root, calls=1)
        gateway, _, base = self.start_gateway()
        body = json.dumps(
            {"model": "fake", "messages": [], "max_tokens": 20},
            separators=(",", ":"),
        ).encode()
        wire = (
            f"{len(body):X} ;source=httpx; note=\"safe value\"\r\n".encode()
            + body
            + b"\r\n0\r\nX-Trace-Note: accepted\r\n\r\n"
        )
        try:
            status, _ = self.framed_post(
                base, {"Transfer-Encoding": "chunked", "Trailer": "X-Trace-Note"}, wire
            )
            self.assertEqual(status, 200)
            self.assertEqual(FakeProviderHandler.calls, 1)
            self.assertEqual(FakeProviderHandler.last_body, body)
            self.assertNotIn("trailer", FakeProviderHandler.last_headers)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_chunked_framing_errors_fail_before_provider(self) -> None:
        write_binding(self.root, calls=5)
        gateway, _, base = self.start_gateway()
        cases = [
            (
                "content-length-and-chunked",
                {"Content-Length": "1", "Transfer-Encoding": "chunked"},
                b"0\r\n\r\n",
                400,
                "MODEL_REQUEST_FRAMING_CONFLICT",
            ),
            (
                "unsupported-transfer-coding",
                {"Transfer-Encoding": "gzip, chunked"},
                b"0\r\n\r\n",
                400,
                "MODEL_REQUEST_TRANSFER_ENCODING_UNSUPPORTED",
            ),
            (
                "malformed-chunk-size",
                {"Transfer-Encoding": "chunked"},
                b"not-hex\r\nx\r\n0\r\n\r\n",
                400,
                "MODEL_REQUEST_CHUNKED_MALFORMED",
            ),
            (
                "malformed-data-terminator",
                {"Transfer-Encoding": "chunked"},
                b"1\r\nxX\n0\r\n\r\n",
                400,
                "MODEL_REQUEST_CHUNKED_MALFORMED",
            ),
            (
                "malformed-trailer",
                {"Transfer-Encoding": "chunked"},
                b"1\r\nx\r\n0\r\nmissing-colon\r\n\r\n",
                400,
                "MODEL_REQUEST_CHUNKED_MALFORMED",
            ),
            (
                "forbidden-framing-trailer",
                {"Transfer-Encoding": "chunked"},
                b"1\r\nx\r\n0\r\nContent-Length: 1\r\n\r\n",
                400,
                "MODEL_REQUEST_CHUNKED_MALFORMED",
            ),
            (
                "decoded-body-over-limit",
                {"Transfer-Encoding": "chunked"},
                b"800001\r\n",
                413,
                "MODEL_REQUEST_BODY_TOO_LARGE",
            ),
        ]
        try:
            for name, headers, wire_body, expected_status, expected_code in cases:
                with self.subTest(name=name):
                    status, payload = self.framed_post(base, headers, wire_body)
                    self.assertEqual(status, expected_status)
                    self.assertEqual(payload["error"]["code"], expected_code)
            self.assertEqual(FakeProviderHandler.calls, 0)
            self.assertFalse((self.root / "gateway_ledger.json").exists())
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_each_call_is_recorded_and_request_count_fuse_blocks_next_call(self) -> None:
        write_binding(self.root, calls=1)
        gateway, _, base = self.start_gateway()
        try:
            status, _ = self.post(base)
            self.assertEqual(status, 200)
            ledger = json.loads((self.root / "gateway_ledger.json").read_text())
            self.assertEqual(ledger["request_attempt_count"], 1)
            self.assertEqual(ledger["provider_call_count"], 1)
            self.assertEqual(ledger["total_tokens"], 130)
            self.assertEqual(ledger["baseline_cumulative_usage"]["total_tokens"], 550)
            self.assertEqual(ledger["current_cumulative_usage"]["total_tokens"], 680)
            self.assertEqual(ledger["records"][0]["actor"], "Evidence Investigator")
            self.assertEqual(ledger["records"][0]["run_id"], "RUN-LIVE-GATEWAY-1")
            self.assertEqual(ledger["records"][0]["task_id"], "TASK-EVIDENCE-1")
            self.assertFalse(
                any(key.startswith("x-finflux-") for key in FakeProviderHandler.last_headers)
            )
            self.assertEqual(ledger["in_flight_reserved_tokens"], 0)
            self.assertEqual(ledger["reservations"], {})
            status, payload = self.post(base)
            self.assertEqual(status, 429)
            self.assertIn("MODEL_CALL_COUNT_FUSE", payload["error"]["code"])
            self.assertEqual(FakeProviderHandler.calls, 1)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_missing_usage_trips_durable_fuse_and_output_is_not_released(self) -> None:
        write_binding(self.root, calls=3)
        FakeProviderHandler.include_usage = False
        gateway, _, base = self.start_gateway()
        try:
            status, payload = self.post(base)
            self.assertEqual(status, 502)
            self.assertEqual(payload["error"]["code"], "PROVIDER_USAGE_UNREADABLE")
            ledger = json.loads((self.root / "gateway_ledger.json").read_text())
            self.assertEqual(ledger["status"], "FUSE_TRIPPED")
            self.assertEqual(ledger["in_flight_reserved_tokens"], 0)
            self.assertEqual(ledger["reservations"], {})
            status, _ = self.post(base)
            self.assertEqual(status, 503)
            self.assertEqual(FakeProviderHandler.calls, 1)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_concurrent_authorize_reserves_budget_before_provider_returns(self) -> None:
        # Each request exposes more than half of this cap.  With a durable
        # reservation exactly one concurrent caller may be authorized, even
        # though provider-reported exact usage is still zero for both callers.
        write_binding(self.root, cap=60, calls=4)
        ledger = BudgetLedger(self.root)
        body = json.dumps(
            {
                "model": "fake",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 20,
            }
        ).encode()
        barrier = threading.Barrier(3)
        results: list[tuple[str, str]] = []
        results_lock = threading.Lock()

        def authorize() -> None:
            barrier.wait()
            try:
                _, exposure = ledger.authorize(
                    body,
                    "RUN-LIVE-GATEWAY-1",
                    "Evidence Investigator",
                    "test-identity-secret",
                    "TASK-EVIDENCE-1",
                )
                result = ("allowed", str(exposure["reservation_id"]))
            except GatewayBlocked as exc:
                result = ("blocked", exc.reason)
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=authorize) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual([item[0] for item in results].count("allowed"), 1)
        self.assertEqual([item[0] for item in results].count("blocked"), 1)
        self.assertIn(
            "PRE_CALL_MAXIMUM_EXPOSURE_EXCEEDS_RUN_CAP",
            next(item[1] for item in results if item[0] == "blocked"),
        )
        persisted = json.loads((self.root / "gateway_ledger.json").read_text())
        self.assertEqual(persisted["request_attempt_count"], 1)
        self.assertEqual(len(persisted["reservations"]), 1)
        self.assertGreater(persisted["in_flight_reserved_tokens"], 0)

    def test_upstream_transport_failure_releases_reservation_and_trips_fuse(self) -> None:
        write_binding(self.root, cap=10_000, calls=3)
        # Reserve a port and immediately close it so connect() deterministically
        # fails locally.  No external network and no model are involved.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        unavailable_port = probe.getsockname()[1]
        probe.close()
        engine = BudgetGatewayEngine(
            f"http://127.0.0.1:{unavailable_port}/v1",
            self.root,
            timeout_seconds=1,
        )
        body = json.dumps(
            {
                "model": "fake",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 20,
            }
        ).encode()
        with self.assertRaisesRegex(GatewayBlocked, "UPSTREAM_TRANSPORT_FAILURE"):
            engine.forward(
                "/v1/chat/completions",
                {
                    "Content-Type": "application/json",
                    "X-FinFlux-Run-ID": "RUN-LIVE-GATEWAY-1",
                    "X-FinFlux-Actor": "Evidence Investigator",
                    "X-FinFlux-Identity": "test-identity-secret",
                    "X-FinFlux-Task-ID": "TASK-EVIDENCE-1",
                },
                body,
            )
        persisted = json.loads((self.root / "gateway_ledger.json").read_text())
        self.assertEqual(persisted["status"], "FUSE_TRIPPED")
        self.assertEqual(persisted["fuse_reason"], "UPSTREAM_TRANSPORT_FAILURE")
        self.assertEqual(persisted["in_flight_reserved_tokens"], 0)
        self.assertEqual(persisted["reservations"], {})
        self.assertEqual(
            persisted["records"][-1]["decision"],
            "FUSE_TRIPPED_UPSTREAM_TRANSPORT_FAILURE",
        )
        with self.assertRaisesRegex(GatewayBlocked, "FUSE_ALREADY_TRIPPED"):
            engine.ledger.authorize(
                body,
                "RUN-LIVE-GATEWAY-1",
                "Evidence Investigator",
                "test-identity-secret",
                "TASK-EVIDENCE-1",
            )

    def test_pre_call_exposure_blocks_before_provider(self) -> None:
        write_binding(self.root, cap=10, calls=2)
        gateway, _, base = self.start_gateway()
        try:
            status, payload = self.post(base)
            self.assertEqual(status, 429)
            self.assertIn("PRE_CALL_MAXIMUM_EXPOSURE", payload["error"]["code"])
            self.assertEqual(FakeProviderHandler.calls, 0)
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_missing_run_or_actor_fails_before_provider(self) -> None:
        write_binding(self.root, calls=2)
        ledger = BudgetLedger(self.root)
        body = json.dumps({"messages": [], "max_tokens": 1}).encode()
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_RUN_ID_REQUIRED"):
            ledger.authorize(body, "", "Evidence Investigator", "test-identity-secret", "TASK-EVIDENCE-1")
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_ACTOR_REQUIRED"):
            ledger.authorize(body, "RUN-LIVE-GATEWAY-1", "", "test-identity-secret", "TASK-EVIDENCE-1")
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_IDENTITY_REQUIRED"):
            ledger.authorize(body, "RUN-LIVE-GATEWAY-1", "Evidence Investigator", "")
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_IDENTITY_INVALID"):
            ledger.authorize(
                body, "RUN-LIVE-GATEWAY-1", "Evidence Investigator", "wrong-secret"
            )
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_ACTOR_NOT_AUTHORIZED"):
            ledger.authorize(
                body, "RUN-LIVE-GATEWAY-1", "Unknown Worker", "test-identity-secret"
            )
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_TASK_ID_REQUIRED"):
            ledger.authorize(
                body, "RUN-LIVE-GATEWAY-1", "Evidence Investigator", "test-identity-secret", ""
            )
        with self.assertRaisesRegex(GatewayBlocked, "REQUEST_TASK_ID_MISMATCH"):
            ledger.authorize(
                body,
                "RUN-LIVE-GATEWAY-1",
                "Evidence Investigator",
                "test-identity-secret",
                "TASK-WRONG",
            )
        self.assertFalse((self.root / "gateway_ledger.json").exists())


class ProviderUsageParserTests(unittest.TestCase):
    def test_request_output_cap_overrides_larger_client_value(self) -> None:
        body, receipt = enforce_request_output_cap(
            json.dumps({"model": "fake", "max_tokens": 4096}).encode(), 256
        )
        payload = json.loads(body)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(receipt["client_declared_max_output_tokens"], 4096)
        self.assertEqual(receipt["enforced_max_output_tokens"], 256)

    def test_request_output_cap_is_added_when_client_omits_it(self) -> None:
        body, receipt = enforce_request_output_cap(
            json.dumps({"model": "fake", "messages": []}).encode(), 256
        )
        payload = json.loads(body)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(receipt["enforced_max_output_tokens"], 256)

    def test_stream_requests_force_final_usage_chunk(self) -> None:
        payload = json.loads(
            ensure_stream_usage(
                json.dumps({"stream": True, "messages": []}).encode()
            )
        )
        self.assertTrue(payload["stream_options"]["include_usage"])

    def test_reads_final_sse_usage(self) -> None:
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(
            parse_provider_usage(body, "text/event-stream"),
            {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        )

    def test_rejects_malformed_total(self) -> None:
        body = json.dumps(
            {"usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 5}}
        ).encode()
        with self.assertRaises(ValueError):
            parse_provider_usage(body, "application/json")


if __name__ == "__main__":
    unittest.main()
