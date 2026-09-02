from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROTOCOL = "FINFLUX_MODEL_BUDGET_GATEWAY_V1"
BINDING_PROTOCOL = "FINFLUX_MODEL_GATEWAY_RUN_BINDING_V1"
LEDGER_PROTOCOL = "FINFLUX_MODEL_GATEWAY_LEDGER_V1"
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
_MAX_CHUNK_LINE_BYTES = 8 * 1024
_MAX_TRAILER_BYTES = 64 * 1024
_MAX_TRAILER_FIELDS = 100
_TCHAR_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_FORBIDDEN_TRAILER_FIELDS = {
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "host",
    "proxy-authorization",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


class GatewayBlocked(RuntimeError):
    def __init__(self, reason: str, status: int = 429) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _chunked_malformed() -> GatewayBlocked:
    return GatewayBlocked("MODEL_REQUEST_CHUNKED_MALFORMED", 400)


def _read_crlf_line(stream: Any, limit: int) -> bytes:
    """Read one bounded HTTP line and require canonical CRLF framing."""

    try:
        line = stream.readline(limit + 1)
    except OSError as exc:
        raise _chunked_malformed() from exc
    if not line or len(line) > limit or not line.endswith(b"\r\n"):
        raise _chunked_malformed()
    return line[:-2]


def _skip_ows(value: bytes, index: int) -> int:
    while index < len(value) and value[index] in (0x20, 0x09):
        index += 1
    return index


def _consume_token(value: bytes, index: int) -> int:
    start = index
    while index < len(value) and value[index] in _TCHAR_BYTES:
        index += 1
    if index == start:
        raise _chunked_malformed()
    return index


def _validate_chunk_extensions(value: bytes) -> None:
    """Validate RFC-style chunk extensions without retaining their values."""

    index = 0
    while True:
        index = _skip_ows(value, index)
        if index == len(value):
            return
        if value[index] != ord(";"):
            raise _chunked_malformed()
        index = _skip_ows(value, index + 1)
        index = _consume_token(value, index)
        index = _skip_ows(value, index)
        if index == len(value) or value[index] == ord(";"):
            continue
        if value[index] != ord("="):
            raise _chunked_malformed()
        index = _skip_ows(value, index + 1)
        if index == len(value):
            raise _chunked_malformed()
        if value[index] != ord('"'):
            index = _consume_token(value, index)
            continue
        index += 1
        closed = False
        while index < len(value):
            byte = value[index]
            if byte == ord('"'):
                index += 1
                closed = True
                break
            if byte == ord("\\"):
                index += 1
                if index == len(value):
                    raise _chunked_malformed()
                escaped = value[index]
                if (
                    escaped not in (0x09, 0x20)
                    and not 0x21 <= escaped <= 0x7E
                    and escaped < 0x80
                ):
                    raise _chunked_malformed()
                index += 1
                continue
            if (
                byte in (0x09, 0x20, 0x21)
                or 0x23 <= byte <= 0x5B
                or 0x5D <= byte <= 0x7E
                or byte >= 0x80
            ):
                index += 1
                continue
            raise _chunked_malformed()
        if not closed:
            raise _chunked_malformed()


def _parse_chunk_size(line: bytes) -> int:
    match = re.match(rb"[0-9A-Fa-f]+", line)
    if match is None:
        raise _chunked_malformed()
    size_text = match.group(0)
    extensions = line[match.end() :]
    if extensions:
        _validate_chunk_extensions(extensions)
    significant = size_text.lstrip(b"0") or b"0"
    if len(significant) > len(f"{MAX_REQUEST_BODY_BYTES:x}"):
        raise GatewayBlocked("MODEL_REQUEST_BODY_TOO_LARGE", 413)
    size = int(significant, 16)
    if size > MAX_REQUEST_BODY_BYTES:
        raise GatewayBlocked("MODEL_REQUEST_BODY_TOO_LARGE", 413)
    return size


def _validate_trailer_line(line: bytes) -> None:
    if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
        raise _chunked_malformed()
    name, value = line.split(b":", 1)
    if not name or any(byte not in _TCHAR_BYTES for byte in name):
        raise _chunked_malformed()
    try:
        normalized_name = name.decode("ascii").lower()
    except UnicodeDecodeError as exc:
        raise _chunked_malformed() from exc
    if normalized_name in _FORBIDDEN_TRAILER_FIELDS:
        raise _chunked_malformed()
    if any(byte < 0x20 and byte != 0x09 for byte in value) or 0x7F in value:
        raise _chunked_malformed()


def read_chunked_request_body(stream: Any) -> bytes:
    """Decode one HTTP/1.1 chunked body with bounded framing and trailers."""

    decoded = bytearray()
    while True:
        chunk_size = _parse_chunk_size(_read_crlf_line(stream, _MAX_CHUNK_LINE_BYTES))
        if chunk_size == 0:
            trailer_bytes = 0
            trailer_fields = 0
            while True:
                trailer = _read_crlf_line(stream, _MAX_CHUNK_LINE_BYTES)
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > _MAX_TRAILER_BYTES:
                    raise _chunked_malformed()
                if not trailer:
                    return bytes(decoded)
                trailer_fields += 1
                if trailer_fields > _MAX_TRAILER_FIELDS:
                    raise _chunked_malformed()
                _validate_trailer_line(trailer)
        if len(decoded) + chunk_size > MAX_REQUEST_BODY_BYTES:
            raise GatewayBlocked("MODEL_REQUEST_BODY_TOO_LARGE", 413)
        try:
            chunk = stream.read(chunk_size)
            terminator = stream.read(2)
        except OSError as exc:
            raise _chunked_malformed() from exc
        if len(chunk) != chunk_size or terminator != b"\r\n":
            raise _chunked_malformed()
        decoded.extend(chunk)


def read_request_body(headers: Any, stream: Any) -> bytes:
    """Read a bounded request body with unambiguous HTTP framing."""

    transfer_values = list(headers.get_all("Transfer-Encoding", []) or [])
    content_length_values = list(headers.get_all("Content-Length", []) or [])
    if transfer_values and content_length_values:
        raise GatewayBlocked("MODEL_REQUEST_FRAMING_CONFLICT", 400)
    if transfer_values:
        transfer_codings = [
            token.strip().lower()
            for value in transfer_values
            for token in str(value).split(",")
        ]
        if transfer_codings != ["chunked"]:
            raise GatewayBlocked("MODEL_REQUEST_TRANSFER_ENCODING_UNSUPPORTED", 400)
        body = read_chunked_request_body(stream)
        if not body:
            raise GatewayBlocked("MODEL_REQUEST_SIZE_INVALID", 400)
        return body
    if len(content_length_values) != 1:
        raise GatewayBlocked("MODEL_REQUEST_SIZE_INVALID", 400)
    raw_length = str(content_length_values[0]).strip()
    if re.fullmatch(r"[0-9]+", raw_length) is None:
        raise GatewayBlocked("MODEL_REQUEST_SIZE_INVALID", 400)
    length = int(raw_length)
    if length <= 0:
        raise GatewayBlocked("MODEL_REQUEST_SIZE_INVALID", 400)
    if length > MAX_REQUEST_BODY_BYTES:
        raise GatewayBlocked("MODEL_REQUEST_BODY_TOO_LARGE", 413)
    try:
        body = stream.read(length)
    except OSError as exc:
        raise GatewayBlocked("MODEL_REQUEST_BODY_TRUNCATED", 400) from exc
    if len(body) != length:
        raise GatewayBlocked("MODEL_REQUEST_BODY_TRUNCATED", 400)
    return body


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def parse_provider_usage(body: bytes, content_type: str = "") -> dict[str, int]:
    """Return provider-reported usage from JSON or buffered SSE.

    The gateway intentionally buffers streaming responses.  Model output is not
    released to AgentTeams until a provider usage object has been observed.
    That trades a little latency for a hard truth boundary: missing usage trips
    the fuse instead of silently under-reporting the Run.
    """

    candidates: list[dict[str, Any]] = []
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type.lower() or text.lstrip().startswith("data:"):
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("usage"), dict):
                candidates.append(item["usage"])
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("provider response is not valid JSON/SSE") from exc
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            candidates.append(payload["usage"])

    if not candidates:
        raise ValueError("provider response has no readable usage object")
    usage = candidates[-1]
    prompt = _integer(
        usage.get("prompt_tokens", usage.get("input_tokens", 0)), "prompt_tokens"
    )
    completion = _integer(
        usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "completion_tokens",
    )
    total_raw = usage.get("total_tokens")
    total = prompt + completion if total_raw is None else _integer(total_raw, "total_tokens")
    if total != prompt + completion:
        raise ValueError("provider total_tokens does not equal prompt+completion")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def estimate_request_exposure(body: bytes, default_max_output_tokens: int) -> dict[str, int]:
    """Conservative pre-call estimate used only to prevent overshoot.

    This estimate is never reported as provider usage.  Exact accounting comes
    exclusively from the provider response and the QwenPaw cumulative ledger.
    """

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayBlocked("MODEL_REQUEST_BODY_UNREADABLE", 400) from exc
    if not isinstance(payload, dict):
        raise GatewayBlocked("MODEL_REQUEST_BODY_NOT_OBJECT", 400)
    max_output = payload.get("max_tokens", payload.get("max_completion_tokens"))
    max_output = (
        default_max_output_tokens
        if max_output is None
        else _integer(max_output, "max_output_tokens")
    )
    # Include all structured input, not only `messages`: tool schemas and
    # response formats can dominate prompt size.
    estimated_input = max(1, (len(body) + 3) // 4)
    return {
        "estimated_input_tokens": estimated_input,
        "declared_max_output_tokens": max_output,
        "maximum_exposure_tokens": estimated_input + max_output,
    }


def enforce_request_output_cap(
    body: bytes, max_output_tokens_per_call: int
) -> tuple[bytes, dict[str, int]]:
    """Rewrite the provider request so the declared per-call cap is effective.

    The earlier gateway treated ``max_output_tokens_per_call`` only as the
    default used for exposure accounting.  A client-supplied larger
    ``max_tokens`` therefore reached the provider unchanged.  This function
    makes the bound operational while preserving every other request field.
    """

    cap = _integer(max_output_tokens_per_call, "max_output_tokens_per_call")
    if cap <= 0:
        raise GatewayBlocked("MAX_OUTPUT_TOKEN_CAP_INVALID", 503)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayBlocked("MODEL_REQUEST_BODY_UNREADABLE", 400) from exc
    if not isinstance(payload, dict):
        raise GatewayBlocked("MODEL_REQUEST_BODY_NOT_OBJECT", 400)

    declared_values: list[int] = []
    for field in ("max_tokens", "max_completion_tokens"):
        if field in payload and payload[field] is not None:
            declared_values.append(_integer(payload[field], field))
            payload[field] = min(_integer(payload[field], field), cap)
    if not declared_values:
        payload["max_tokens"] = cap
        declared = cap
    else:
        declared = max(declared_values)
    enforced = min(declared, cap)
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        {
            "client_declared_max_output_tokens": declared,
            "enforced_max_output_tokens": enforced,
        },
    )


def ensure_stream_usage(body: bytes) -> bytes:
    """Require OpenAI-compatible streams to end with a provider usage chunk."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayBlocked("MODEL_REQUEST_BODY_UNREADABLE", 400) from exc
    if not isinstance(payload, dict):
        raise GatewayBlocked("MODEL_REQUEST_BODY_NOT_OBJECT", 400)
    if payload.get("stream") is True:
        options = payload.get("stream_options")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise GatewayBlocked("STREAM_OPTIONS_NOT_OBJECT", 400)
        options["include_usage"] = True
        payload["stream_options"] = options
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    return body


class BudgetLedger:
    def __init__(self, control_root: Path) -> None:
        self.control_root = control_root
        self.binding_path = control_root / "active_run.json"
        self.ledger_path = control_root / "gateway_ledger.json"
        self._lock = threading.Lock()

    def binding(self) -> dict[str, Any]:
        try:
            binding = json.loads(self.binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayBlocked("ACTIVE_RUN_BINDING_UNREADABLE", 503) from exc
        if binding.get("protocol") != BINDING_PROTOCOL:
            raise GatewayBlocked("ACTIVE_RUN_BINDING_PROTOCOL_MISMATCH", 503)
        if binding.get("state") != "ACTIVE":
            raise GatewayBlocked("NO_ACTIVE_MODEL_RUN", 503)
        run_id = str(binding.get("run_id") or "").strip()
        if not run_id:
            raise GatewayBlocked("ACTIVE_RUN_ID_MISSING", 503)
        try:
            expiry = datetime.fromisoformat(str(binding.get("expires_at_utc")))
        except (TypeError, ValueError) as exc:
            raise GatewayBlocked("ACTIVE_RUN_EXPIRY_UNREADABLE", 503) from exc
        if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry.astimezone(timezone.utc):
            raise GatewayBlocked("ACTIVE_RUN_BINDING_EXPIRED", 503)
        for key in ("provider_token_hard_cap", "max_model_calls", "max_output_tokens_per_call"):
            _integer(binding.get(key), key)
        expected = str(binding.get("binding_sha256") or "")
        material = {key: value for key, value in binding.items() if key != "binding_sha256"}
        if expected != _canonical_sha256(material):
            raise GatewayBlocked("ACTIVE_RUN_BINDING_HASH_MISMATCH", 503)
        try:
            baseline = json.loads(
                (self.control_root / "provider_usage_baseline.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayBlocked("PROVIDER_USAGE_BASELINE_UNREADABLE", 503) from exc
        baseline_hash = str(baseline.get("baseline_sha256") or "")
        baseline_material = {
            key: value for key, value in baseline.items() if key != "baseline_sha256"
        }
        if (
            baseline.get("run_id") != run_id
            or baseline_hash != _canonical_sha256(baseline_material)
            or baseline_hash != binding.get("provider_usage_baseline_sha256")
        ):
            raise GatewayBlocked("PROVIDER_USAGE_BASELINE_BINDING_MISMATCH", 503)
        totals = baseline.get("cumulative_totals")
        if not isinstance(totals, dict):
            raise GatewayBlocked("PROVIDER_USAGE_BASELINE_TOTALS_MISSING", 503)
        values = {
            key: _integer(totals.get(key), "baseline_" + key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "call_count")
        }
        if values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]:
            raise GatewayBlocked("PROVIDER_USAGE_BASELINE_TOTAL_MISMATCH", 503)
        if _integer(binding.get("provider_usage_baseline_total_tokens"), "binding_baseline_total") != values["total_tokens"]:
            raise GatewayBlocked("PROVIDER_USAGE_BASELINE_TOTAL_BINDING_MISMATCH", 503)
        identities = binding.get("actor_identity_sha256")
        if not isinstance(identities, dict) or not identities:
            raise GatewayBlocked("ACTOR_IDENTITY_BINDING_MISSING", 503)
        for actor, identity_hash in identities.items():
            if not str(actor).strip() or len(str(identity_hash)) != 64:
                raise GatewayBlocked("ACTOR_IDENTITY_BINDING_INVALID", 503)
        task_ids = binding.get("actor_task_ids")
        if not isinstance(task_ids, dict) or set(task_ids) != set(identities):
            raise GatewayBlocked("ACTOR_TASK_BINDING_SET_MISMATCH", 503)
        normalized_tasks = [str(value or "").strip() for value in task_ids.values()]
        if not all(normalized_tasks) or len(set(normalized_tasks)) != len(normalized_tasks):
            raise GatewayBlocked("ACTOR_TASK_BINDING_INVALID", 503)
        return binding

    def _read_ledger(self, run_id: str) -> dict[str, Any]:
        if not self.ledger_path.exists():
            try:
                baseline = json.loads(
                    (self.control_root / "provider_usage_baseline.json").read_text(encoding="utf-8")
                )
                baseline_totals = baseline["cumulative_totals"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise GatewayBlocked("PROVIDER_USAGE_BASELINE_TOTALS_UNREADABLE", 503) from exc
            return {
                "protocol": LEDGER_PROTOCOL,
                "run_id": run_id,
                "status": "ACTIVE",
                "request_attempt_count": 0,
                "provider_call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "baseline_cumulative_usage": baseline_totals,
                "current_cumulative_usage": dict(baseline_totals),
                "in_flight_reserved_tokens": 0,
                "reservations": {},
                "records": [],
                "updated_at_utc": utc_now(),
            }
        try:
            ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayBlocked("MODEL_GATEWAY_LEDGER_UNREADABLE", 503) from exc
        if ledger.get("protocol") != LEDGER_PROTOCOL or ledger.get("run_id") != run_id:
            raise GatewayBlocked("MODEL_GATEWAY_LEDGER_RUN_MISMATCH", 503)
        declared_hash = str(ledger.get("ledger_sha256") or "")
        if declared_hash:
            material = {key: value for key, value in ledger.items() if key != "ledger_sha256"}
            if declared_hash != _canonical_sha256(material):
                raise GatewayBlocked("MODEL_GATEWAY_LEDGER_HASH_MISMATCH", 503)
        if ledger.get("status") == "FUSE_TRIPPED":
            raise GatewayBlocked("MODEL_GATEWAY_FUSE_ALREADY_TRIPPED", 503)
        return ledger

    @staticmethod
    def _seal_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
        ledger.pop("ledger_sha256", None)
        ledger["ledger_sha256"] = _canonical_sha256(ledger)
        return ledger

    def authorize(
        self,
        request_body: bytes,
        request_run_id: str = "",
        actor: str = "",
        identity: str = "",
        task_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            binding = self.binding()
            run_id = str(binding["run_id"])
            if not request_run_id:
                raise GatewayBlocked("REQUEST_RUN_ID_REQUIRED", 400)
            if request_run_id != run_id:
                raise GatewayBlocked("REQUEST_RUN_ID_MISMATCH", 409)
            actor = str(actor or "").strip()
            if not actor:
                raise GatewayBlocked("REQUEST_ACTOR_REQUIRED", 400)
            identity = str(identity or "")
            if not identity:
                raise GatewayBlocked("REQUEST_IDENTITY_REQUIRED", 401)
            allowed = binding.get("actor_identity_sha256") or {}
            expected_identity_hash = allowed.get(actor)
            if not expected_identity_hash:
                raise GatewayBlocked("REQUEST_ACTOR_NOT_AUTHORIZED", 403)
            supplied_identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(str(expected_identity_hash), supplied_identity_hash):
                raise GatewayBlocked("REQUEST_IDENTITY_INVALID", 403)
            task_id = str(task_id or "").strip()
            if not task_id:
                raise GatewayBlocked("REQUEST_TASK_ID_REQUIRED", 400)
            expected_task_id = str((binding.get("actor_task_ids") or {}).get(actor) or "")
            if not hmac.compare_digest(expected_task_id, task_id):
                raise GatewayBlocked("REQUEST_TASK_ID_MISMATCH", 403)
            ledger = self._read_ledger(run_id)
            attempts = _integer(ledger.get("request_attempt_count", 0), "request_attempt_count")
            max_calls = _integer(binding["max_model_calls"], "max_model_calls")
            if attempts >= max_calls:
                raise GatewayBlocked(f"MODEL_CALL_COUNT_FUSE:{attempts}>={max_calls}")
            exact_total = _integer(ledger.get("total_tokens", 0), "total_tokens")
            reservations = ledger.get("reservations") or {}
            if not isinstance(reservations, dict):
                raise GatewayBlocked("MODEL_GATEWAY_RESERVATIONS_UNREADABLE", 503)
            reserved = sum(
                _integer(item.get("maximum_exposure_tokens", 0), "reservation")
                for item in reservations.values()
                if isinstance(item, dict)
            )
            declared_reserved = _integer(
                ledger.get("in_flight_reserved_tokens", 0),
                "in_flight_reserved_tokens",
            )
            if declared_reserved != reserved:
                raise GatewayBlocked("MODEL_GATEWAY_RESERVATION_TOTAL_MISMATCH", 503)
            hard_cap = _integer(binding["provider_token_hard_cap"], "provider_token_hard_cap")
            if exact_total >= hard_cap:
                raise GatewayBlocked(f"PROVIDER_TOKEN_FUSE:{exact_total}>={hard_cap}")
            exposure = estimate_request_exposure(
                request_body, _integer(binding["max_output_tokens_per_call"], "max_output_tokens_per_call")
            )
            if exact_total + reserved + exposure["maximum_exposure_tokens"] > hard_cap:
                raise GatewayBlocked(
                    "PRE_CALL_MAXIMUM_EXPOSURE_EXCEEDS_RUN_CAP:"
                    f"{exact_total}+{reserved}+{exposure['maximum_exposure_tokens']}>{hard_cap}"
                )
            reservation_id = "REQ-" + uuid.uuid4().hex
            reservations[reservation_id] = {
                "run_id": run_id,
                "actor": actor,
                "task_id": task_id,
                "maximum_exposure_tokens": exposure["maximum_exposure_tokens"],
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "reserved_at_utc": utc_now(),
            }
            exposure["reservation_id"] = reservation_id
            exposure["actor"] = actor
            exposure["task_id"] = task_id
            ledger["request_attempt_count"] = attempts + 1
            ledger["reservations"] = reservations
            ledger["in_flight_reserved_tokens"] = (
                reserved + exposure["maximum_exposure_tokens"]
            )
            ledger["updated_at_utc"] = utc_now()
            _atomic_json(self.ledger_path, self._seal_ledger(ledger))
            return binding, exposure

    @staticmethod
    def _release_reservation(
        ledger: dict[str, Any], exposure: dict[str, Any]
    ) -> dict[str, Any]:
        reservation_id = str(exposure.get("reservation_id") or "")
        reservations = ledger.get("reservations") or {}
        if not reservation_id or not isinstance(reservations, dict):
            raise GatewayBlocked("MODEL_GATEWAY_RESERVATION_ID_MISSING", 503)
        reservation = reservations.pop(reservation_id, None)
        if not isinstance(reservation, dict):
            raise GatewayBlocked("MODEL_GATEWAY_RESERVATION_NOT_FOUND", 503)
        expected = _integer(
            exposure.get("maximum_exposure_tokens", 0), "maximum_exposure_tokens"
        )
        if _integer(reservation.get("maximum_exposure_tokens", 0), "reservation") != expected:
            raise GatewayBlocked("MODEL_GATEWAY_RESERVATION_EXPOSURE_MISMATCH", 503)
        ledger["reservations"] = reservations
        ledger["in_flight_reserved_tokens"] = sum(
            _integer(item.get("maximum_exposure_tokens", 0), "reservation")
            for item in reservations.values()
            if isinstance(item, dict)
        )
        return ledger

    def record_provider_result(
        self,
        *,
        binding: dict[str, Any],
        exposure: dict[str, Any],
        request_body: bytes,
        response_body: bytes,
        provider_http_status: int,
        content_type: str,
    ) -> dict[str, Any]:
        with self._lock:
            ledger = self._read_ledger(str(binding["run_id"]))
            ledger = self._release_reservation(ledger, exposure)
            previous_hash = str(ledger["records"][-1]["record_sha256"]) if ledger["records"] else "GENESIS"
            try:
                usage = parse_provider_usage(response_body, content_type)
            except ValueError as exc:
                record = {
                    "sequence": len(ledger["records"]) + 1,
                    "run_id": binding["run_id"],
                    "actor": exposure["actor"],
                    "task_id": exposure["task_id"],
                    "decision": "FUSE_TRIPPED_USAGE_UNREADABLE",
                    "provider_http_status": provider_http_status,
                    "request_sha256": hashlib.sha256(request_body).hexdigest(),
                    "response_sha256": hashlib.sha256(response_body).hexdigest(),
                    "maximum_exposure_tokens": exposure["maximum_exposure_tokens"],
                    "previous_record_sha256": previous_hash,
                    "recorded_at_utc": utc_now(),
                }
                record["record_sha256"] = _canonical_sha256(record)
                ledger["records"].append(record)
                ledger["status"] = "FUSE_TRIPPED"
                ledger["fuse_reason"] = "PROVIDER_USAGE_UNREADABLE"
                ledger["updated_at_utc"] = utc_now()
                _atomic_json(self.ledger_path, self._seal_ledger(ledger))
                raise GatewayBlocked("PROVIDER_USAGE_UNREADABLE", 502) from exc

            record = {
                "sequence": len(ledger["records"]) + 1,
                "run_id": binding["run_id"],
                "actor": exposure["actor"],
                "task_id": exposure["task_id"],
                "decision": "PROVIDER_USAGE_RECORDED",
                "provider_http_status": provider_http_status,
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "response_sha256": hashlib.sha256(response_body).hexdigest(),
                "usage": usage,
                "maximum_exposure_tokens": exposure["maximum_exposure_tokens"],
                "previous_record_sha256": previous_hash,
                "recorded_at_utc": utc_now(),
            }
            record["record_sha256"] = _canonical_sha256(record)
            ledger["records"].append(record)
            ledger["provider_call_count"] = _integer(
                ledger.get("provider_call_count", 0), "provider_call_count"
            ) + 1
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                ledger[field] = _integer(ledger.get(field, 0), field) + usage[field]
            baseline = ledger.get("baseline_cumulative_usage")
            if not isinstance(baseline, dict):
                raise GatewayBlocked("MODEL_GATEWAY_BASELINE_LEDGER_MISSING", 503)
            ledger["current_cumulative_usage"] = {
                "prompt_tokens": _integer(baseline.get("prompt_tokens"), "baseline_prompt_tokens") + ledger["prompt_tokens"],
                "completion_tokens": _integer(baseline.get("completion_tokens"), "baseline_completion_tokens") + ledger["completion_tokens"],
                "total_tokens": _integer(baseline.get("total_tokens"), "baseline_total_tokens") + ledger["total_tokens"],
                "call_count": _integer(baseline.get("call_count"), "baseline_call_count") + ledger["provider_call_count"],
            }
            hard_cap = _integer(binding["provider_token_hard_cap"], "provider_token_hard_cap")
            if ledger["total_tokens"] > hard_cap:
                ledger["status"] = "FUSE_TRIPPED"
                ledger["fuse_reason"] = "PROVIDER_TOKEN_CAP_EXCEEDED_AFTER_CALL"
            ledger["updated_at_utc"] = utc_now()
            _atomic_json(self.ledger_path, self._seal_ledger(ledger))
            if ledger["status"] == "FUSE_TRIPPED":
                raise GatewayBlocked("PROVIDER_TOKEN_CAP_EXCEEDED_AFTER_CALL", 429)
            return usage

    def record_transport_failure(
        self,
        *,
        binding: dict[str, Any],
        exposure: dict[str, Any],
        request_body: bytes,
        reason: str,
    ) -> None:
        with self._lock:
            ledger = self._read_ledger(str(binding["run_id"]))
            ledger = self._release_reservation(ledger, exposure)
            previous_hash = (
                str(ledger["records"][-1]["record_sha256"])
                if ledger["records"]
                else "GENESIS"
            )
            record = {
                "sequence": len(ledger["records"]) + 1,
                "run_id": binding["run_id"],
                "actor": exposure["actor"],
                "task_id": exposure["task_id"],
                "decision": "FUSE_TRIPPED_UPSTREAM_TRANSPORT_FAILURE",
                "reason": str(reason)[:160],
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "maximum_exposure_tokens": exposure["maximum_exposure_tokens"],
                "previous_record_sha256": previous_hash,
                "recorded_at_utc": utc_now(),
            }
            record["record_sha256"] = _canonical_sha256(record)
            ledger["records"].append(record)
            ledger["status"] = "FUSE_TRIPPED"
            ledger["fuse_reason"] = "UPSTREAM_TRANSPORT_FAILURE"
            ledger["updated_at_utc"] = utc_now()
            _atomic_json(self.ledger_path, self._seal_ledger(ledger))


class BudgetGatewayEngine:
    def __init__(self, upstream_base_url: str, control_root: Path, timeout_seconds: int = 120) -> None:
        upstream = upstream_base_url.rstrip("/")
        parsed = urllib.parse.urlparse(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upstream_base_url must be absolute HTTP(S)")
        self.upstream_base_url = upstream
        self.control_root = control_root
        self.timeout_seconds = timeout_seconds
        self.ledger = BudgetLedger(control_root)

    def upstream_url(self, request_path: str) -> str:
        incoming = request_path.split("?", 1)[0]
        query = "?" + request_path.split("?", 1)[1] if "?" in request_path else ""
        upstream_path = urllib.parse.urlparse(self.upstream_base_url).path.rstrip("/")
        if upstream_path.endswith("/v1") and incoming.startswith("/v1/"):
            incoming = incoming[3:]
        return self.upstream_base_url + (incoming if incoming.startswith("/") else "/" + incoming) + query

    def forward(
        self, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes, dict[str, int]]:
        body = ensure_stream_usage(body)
        normalized_headers = {str(key).lower(): value for key, value in headers.items()}
        binding, exposure = self.ledger.authorize(
            body,
            str(normalized_headers.get("x-finflux-run-id") or ""),
            str(normalized_headers.get("x-finflux-actor") or ""),
            str(normalized_headers.get("x-finflux-identity") or ""),
            str(normalized_headers.get("x-finflux-task-id") or ""),
        )
        body, output_cap = enforce_request_output_cap(
            body, _integer(binding["max_output_tokens_per_call"], "max_output_tokens_per_call")
        )
        exposure.update(output_cap)
        upstream_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in _HOP_BY_HOP
            and key.lower() != "accept-encoding"
            and not key.lower().startswith("x-finflux-")
        }
        upstream_headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(
            self.upstream_url(path), data=body, headers=upstream_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                response_headers = dict(response.headers.items())
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            response_body = exc.read()
        except (OSError, urllib.error.URLError) as exc:
            self.ledger.record_transport_failure(
                binding=binding,
                exposure=exposure,
                request_body=body,
                reason=type(exc).__name__,
            )
            raise GatewayBlocked("UPSTREAM_TRANSPORT_FAILURE", 502) from exc
        usage = self.ledger.record_provider_result(
            binding=binding,
            exposure=exposure,
            request_body=body,
            response_body=response_body,
            provider_http_status=status,
            content_type=str(response_headers.get("Content-Type", "")),
        )
        return status, response_headers, response_body, usage


def build_handler(engine: BudgetGatewayEngine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FinFluxModelBudgetGateway/1.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            # Never print provider headers, request bodies, or financial data.
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-FinFlux-Gateway-Protocol", PROTOCOL)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/healthz", "/readyz"}:
                self._json(404, {"error": "NOT_FOUND"})
                return
            payload: dict[str, Any] = {"protocol": PROTOCOL, "status": "ALIVE"}
            status = 200
            if self.path == "/readyz":
                try:
                    binding = engine.ledger.binding()
                    payload.update({"status": "READY", "run_id": binding["run_id"]})
                except GatewayBlocked as exc:
                    status = 503
                    payload.update({"status": "BLOCKED", "reason": exc.reason})
            self._json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = read_request_body(self.headers, self.rfile)
                status, response_headers, response_body, usage = engine.forward(
                    self.path, dict(self.headers.items()), body
                )
                self.send_response(status)
                for key, value in response_headers.items():
                    if key.lower() not in _HOP_BY_HOP and not key.lower().startswith(
                        "x-finflux-"
                    ):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("X-FinFlux-Gateway-Protocol", PROTOCOL)
                self.send_header("X-FinFlux-Provider-Tokens", str(usage["total_tokens"]))
                self.end_headers()
                self.wfile.write(response_body)
            except GatewayBlocked as exc:
                # A framing failure can leave unread bytes on the connection.
                # Never reuse that connection for another authenticated call.
                self.close_connection = True
                self._json(
                    exc.status,
                    {
                        "error": {
                            "type": "finflux_budget_gateway_block",
                            "code": exc.reason,
                            "message": "Model request stopped by the FinFlux fail-closed gateway.",
                        }
                    },
                )
            except (OSError, urllib.error.URLError) as exc:
                self._json(
                    502,
                    {
                        "error": {
                            "type": "finflux_upstream_unavailable",
                            "code": type(exc).__name__,
                        }
                    },
                )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="FinFlux fail-closed model budget gateway")
    parser.add_argument("--listen", default=os.getenv("FINFLUX_GATEWAY_LISTEN", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FINFLUX_GATEWAY_PORT", "8090")))
    parser.add_argument(
        "--upstream-base-url", default=os.getenv("FINFLUX_UPSTREAM_BASE_URL", "")
    )
    parser.add_argument(
        "--control-root",
        default=os.getenv("FINFLUX_GATEWAY_CONTROL_ROOT", "/var/lib/finflux-gateway"),
    )
    args = parser.parse_args()
    engine = BudgetGatewayEngine(args.upstream_base_url, Path(args.control_root))
    server = ThreadingHTTPServer((args.listen, args.port), build_handler(engine))
    server.serve_forever()


if __name__ == "__main__":
    main()
