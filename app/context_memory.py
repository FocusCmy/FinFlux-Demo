from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


LOOKUP_PROTOCOL = "FINFLUX_CONTEXT_MEMORY_LOOKUP_V1"
COMMIT_PROTOCOL = "FINFLUX_CONTEXT_MEMORY_COMMIT_V1"
REFERENCE_PROTOCOL = "FINFLUX_CONTEXT_MEMORY_REFERENCE_V1"
LOCAL_OBJECT_PROTOCOL = "FINFLUX_LOCAL_CONTEXT_OBJECT_V1"
LOCAL_BINDING_PROTOCOL = "FINFLUX_LOCAL_CONTEXT_BINDING_V1"
OUTBOX_PROTOCOL = "FINFLUX_CONTEXT_MEMORY_OUTBOX_V1"
STATUS_PROTOCOL = "FINFLUX_CONTEXT_MEMORY_STATUS_V1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_APPROVED_DECISIONS = frozenset(
    {
        "APPROVED",
        "APPROVE_PASS",
        "CONFIRM_BLOCK",
        "REQUEST_EVIDENCE",
        "HUMAN_APPROVED",
        "SIGNED_PASS",
    }
)
_MEMORY_ENV_KEYS = frozenset(
    {
        "FINFLUX_CONTEXT_MEMORY_BACKEND",
        "FINFLUX_CONTEXT_MEMORY_LOCAL_ROOT",
        "FINFLUX_CONTEXT_MEMORY_MAX_TOKENS",
        "FINFLUX_OPENVIKING_URL",
        "FINFLUX_OPENVIKING_API_KEY",
        "FINFLUX_OPENVIKING_TIMEOUT_MS",
        "FINFLUX_OPENVIKING_TARGET_ROOT",
        "FINFLUX_OPENVIKING_ACCOUNT",
        "FINFLUX_OPENVIKING_USER",
        "FINFLUX_OPENVIKING_ACTOR_PEER",
        "FINFLUX_OPENVIKING_REMOTE_WRITE",
    }
)


class ContextMemoryError(RuntimeError):
    """Base error for deterministic context-memory operations."""


class ContextMemoryPolicyError(ContextMemoryError):
    """Raised when a memory operation violates the Human/financial boundary."""


class OpenVikingTransportError(ContextMemoryError):
    """Raised for a remote OpenViking transport or response failure."""


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def content_sha256(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(raw).hexdigest()


def _external_memory_env() -> dict[str, str]:
    """Read only allowlisted memory keys from the shared external runtime env."""
    path_value = os.environ.get("FINFLUX_RUNTIME_ENV_FILE", "").strip()
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > 1_048_576:
            return {}
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in _MEMORY_ENV_KEYS:
                values[key] = value.strip()
        return values
    except OSError:
        return {}


def memory_setting(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    return _external_memory_env().get(name, default)


def _token_estimate(text: str) -> int:
    """Conservative, tokenizer-free estimate; CJK is counted one-for-one."""
    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
    )
    return cjk + math.ceil((len(text) - cjk) / 4)


def _safe_segment(value: str, field: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("-", str(value).strip()).strip("-.")
    if not cleaned or len(cleaned) > 160:
        raise ValueError(f"{field} is not a safe context-memory identifier")
    return cleaned


def _validate_sha256(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return normalized


def _validate_reference_uri(value: str) -> str:
    uri = str(value).strip()
    parsed = urlparse(uri)
    if parsed.scheme not in {"https", "http", "viking", "urn", "finflux"}:
        raise ValueError("memory reference URI must be an approved non-file URI")
    if not parsed.netloc and parsed.scheme in {"https", "http", "viking"}:
        raise ValueError("memory reference URI is incomplete")
    if len(uri) > 1024:
        raise ValueError("memory reference URI is too long")
    return uri


@dataclass(frozen=True)
class MemoryBudget:
    max_characters: int = 1200
    max_token_estimate: int = 300
    max_items: int = 4

    def __post_init__(self) -> None:
        if not 32 <= self.max_characters <= 16_000:
            raise ValueError("memory character budget must be between 32 and 16000")
        if not 8 <= self.max_token_estimate <= 4_000:
            raise ValueError("memory token estimate budget must be between 8 and 4000")
        if not 1 <= self.max_items <= 20:
            raise ValueError("memory item budget must be between 1 and 20")


@dataclass(frozen=True)
class MemoryReference:
    """A deliberately narrow reference. Raw financial payloads are not accepted."""

    run_id: str
    role: str
    summary: str
    uri: str
    content_sha256: str

    def __post_init__(self) -> None:
        _safe_segment(self.run_id, "run_id")
        _safe_segment(self.role, "role")
        summary = str(self.summary).strip()
        if not summary or len(summary) > 4096 or "\x00" in summary:
            raise ValueError("memory summary must contain 1..4096 safe characters")
        _validate_reference_uri(self.uri)
        _validate_sha256(self.content_sha256, "content_sha256")

    def public_payload(self) -> dict[str, str]:
        return {
            "summary": str(self.summary).strip(),
            "uri": _validate_reference_uri(self.uri),
            "content_sha256": _validate_sha256(
                self.content_sha256, "content_sha256"
            ),
        }


@dataclass(frozen=True)
class HumanApproval:
    decision: str
    signer_id: str
    signature_sha256: str
    signed_at: str

    def validate_for_commit(self) -> None:
        if str(self.decision).strip().upper() not in _APPROVED_DECISIONS:
            raise ContextMemoryPolicyError("HUMAN_SIGNATURE_REQUIRED")
        if not str(self.signer_id).strip() or not str(self.signed_at).strip():
            raise ContextMemoryPolicyError("HUMAN_SIGNATURE_REQUIRED")
        try:
            _validate_sha256(self.signature_sha256, "signature_sha256")
        except ValueError as exc:
            raise ContextMemoryPolicyError("HUMAN_SIGNATURE_REQUIRED") from exc


def _validate_operational_summary(reference: MemoryReference) -> None:
    """Reject free-form operational memory on the live execution-control role."""
    if reference.role != "finflux-execution-control":
        return
    try:
        payload = json.loads(reference.summary)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContextMemoryPolicyError(
            "OPERATIONAL_MEMORY_MUST_USE_STRUCTURED_ALLOWLIST"
        ) from exc
    allowed = {
        "protocol",
        "source_run_id",
        "profile",
        "declared_purpose",
        "route",
        "reason_codes",
        "worker_roles",
        "skill_versions",
        "execution_recipe_id",
        "execution_policy_id",
        "terminal_state",
        "human_decision",
        "result_code",
        "provider_usage",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or payload.get("protocol")
        != "FINFLUX_SIGNED_OPERATIONAL_EXPERIENCE_V1.0"
        or payload.get("source_run_id") != reference.run_id
        or not isinstance(payload.get("reason_codes", []), list)
        or not isinstance(payload.get("worker_roles"), list)
        or not isinstance(payload.get("skill_versions"), dict)
        or not isinstance(payload.get("provider_usage"), dict)
    ):
        raise ContextMemoryPolicyError(
            "OPERATIONAL_MEMORY_MUST_USE_STRUCTURED_ALLOWLIST"
        )
    if set(payload["provider_usage"]) - {
        "status",
        "total_tokens",
        "call_count",
        "source",
    }:
        raise ContextMemoryPolicyError(
            "OPERATIONAL_MEMORY_PROVIDER_USAGE_NOT_ALLOWLISTED"
        )
    for key in ("total_tokens", "call_count"):
        value = payload["provider_usage"].get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ContextMemoryPolicyError(
                "OPERATIONAL_MEMORY_PROVIDER_USAGE_NOT_ALLOWLISTED"
            )


@dataclass(frozen=True)
class OpenVikingConfig:
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 0.8
    target_root: str = "viking://~/memories/finflux"
    account: str = ""
    user: str = ""
    actor_peer: str = ""
    remote_write_enabled: bool = False

    def __post_init__(self) -> None:
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("OpenViking URL must be an HTTP(S) service URL")
            if (
                parsed.scheme == "http"
                and self.api_key
                and (parsed.hostname or "").lower()
                not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise ValueError(
                    "OpenViking API key requires HTTPS outside the local host"
                )
        if not 0.05 <= float(self.timeout_seconds) <= 5.0:
            raise ValueError("OpenViking timeout must be between 0.05 and 5 seconds")
        target = str(self.target_root).rstrip("/")
        if not target.startswith(
            ("viking://~/", "viking://user/", "viking://agent/")
        ):
            raise ValueError("OpenViking memory target must use user or agent scope")

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip())

    @classmethod
    def from_env(cls) -> "OpenVikingConfig":
        timeout_raw = memory_setting(
            "FINFLUX_OPENVIKING_TIMEOUT_MS", "800"
        ).strip()
        try:
            timeout_seconds = int(timeout_raw) / 1000
        except ValueError as exc:
            raise ValueError("FINFLUX_OPENVIKING_TIMEOUT_MS must be an integer") from exc
        return cls(
            base_url=memory_setting("FINFLUX_OPENVIKING_URL").strip().rstrip("/"),
            api_key=memory_setting("FINFLUX_OPENVIKING_API_KEY").strip(),
            timeout_seconds=timeout_seconds,
            target_root=memory_setting(
                "FINFLUX_OPENVIKING_TARGET_ROOT",
                "viking://~/memories/finflux",
            ).strip().rstrip("/"),
            account=memory_setting("FINFLUX_OPENVIKING_ACCOUNT").strip(),
            user=memory_setting("FINFLUX_OPENVIKING_USER").strip(),
            actor_peer=memory_setting("FINFLUX_OPENVIKING_ACTOR_PEER").strip(),
            remote_write_enabled=memory_setting(
                "FINFLUX_OPENVIKING_REMOTE_WRITE", "0"
            ).strip().lower()
            in {"1", "true", "yes"},
        )


class OpenVikingHTTPClient:
    """Minimal REST client; OpenViking's own provider usage is not inferred here."""

    def __init__(
        self,
        config: OpenVikingConfig,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _request_json(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> dict[str, Any] | list[Any]:
        if not self.enabled:
            raise OpenVikingTransportError("OPENVIKING_DISABLED")
        payload = json.dumps(
            dict(body), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        # OpenViking derives identity from an API key. Trusted identity headers
        # are sent only when API-key mode is not active.
        if self.config.account and not self.config.api_key:
            headers["X-OpenViking-Account"] = self.config.account
        if self.config.user and not self.config.api_key:
            headers["X-OpenViking-User"] = self.config.user
        if self.config.actor_peer:
            headers["X-OpenViking-Actor-Peer"] = self.config.actor_peer
        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}{path}",
            data=payload,
            method=method,
            headers=headers,
        )
        response = None
        try:
            response = self._opener(request, timeout=self.config.timeout_seconds)
            decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, (dict, list)):
                raise OpenVikingTransportError("OPENVIKING_INVALID_RESPONSE")
            return decoded
        except (TimeoutError, socket.timeout) as exc:
            raise OpenVikingTransportError("OPENVIKING_TIMEOUT") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "OPENVIKING_TIMEOUT" if isinstance(reason, TimeoutError) else "OPENVIKING_UNAVAILABLE"
            raise OpenVikingTransportError(code) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OpenVikingTransportError("OPENVIKING_INVALID_RESPONSE") from exc
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()

    def find(
        self, *, query: str, target_uri: str, limit: int
    ) -> dict[str, Any] | list[Any]:
        return self._request_json(
            "POST",
            "/api/v1/search/find",
            {"query": query, "target_uri": target_uri, "limit": limit},
        )

    def write_reference(
        self, *, target_uri: str, reference: Mapping[str, Any]
    ) -> dict[str, Any] | list[Any]:
        if not self.config.remote_write_enabled:
            raise OpenVikingTransportError("OPENVIKING_REMOTE_WRITE_DISABLED")
        # The content is the already-redacted reference, never the source payload.
        return self._request_json(
            "POST",
            "/api/v1/content/write",
            {
                "uri": target_uri,
                "content": json.dumps(
                    dict(reference),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "mode": "replace",
                "wait": False,
            },
        )


class ContextMemoryAdapter:
    """Bounded OpenViking recall with deterministic local failover."""

    def __init__(
        self,
        *,
        client: OpenVikingHTTPClient | Any | None = None,
        local_root: Path | None = None,
        clock: Callable[[], float] = time.perf_counter,
        backend: str | None = None,
    ) -> None:
        self.client = client
        self.backend = str(
            backend or ("openviking" if client is not None else "local")
        ).strip().lower()
        if self.backend not in {"local", "openviking"}:
            raise ValueError("context memory backend must be local or openviking")
        configured_root = memory_setting(
            "FINFLUX_CONTEXT_MEMORY_LOCAL_ROOT"
        ).strip()
        self.local_root = Path(
            local_root
            or configured_root
            or (Path(__file__).resolve().parent / "runtime" / "context_memory")
        ).expanduser().resolve()
        self._clock = clock
        self._run_role_cache: dict[
            tuple[str, str, str, int, int, int], tuple[dict[str, str], ...]
        ] = {}
        self._outbox_drain_lock = threading.Lock()

    @staticmethod
    def _role_scope_uri(config: OpenVikingConfig, role: str) -> str:
        return f"{config.target_root.rstrip('/')}/{_safe_segment(role, 'role')}"

    @classmethod
    def _remote_reference_uri(
        cls, config: OpenVikingConfig, reference: MemoryReference
    ) -> str:
        return (
            f"{cls._role_scope_uri(config, reference.role)}/"
            f"{_safe_segment(reference.run_id, 'run_id')}/"
            f"{reference.content_sha256}.json"
        )

    def _signed_local_reference(
        self, role: str, digest: str, run_id: str | None = None
    ) -> dict[str, str] | None:
        """Resolve a remote candidate back to a locally verified Human binding."""
        safe_role = _safe_segment(role, "role")
        digest = _validate_sha256(digest, "content_sha256")
        if run_id is not None:
            safe_run = _safe_segment(run_id, "run_id")
            candidates = [
                self.local_root
                / "bindings"
                / safe_run
                / safe_role
                / f"{digest}.json"
            ]
        else:
            candidates = sorted(
                (self.local_root / "bindings").glob(
                    f"*/{safe_role}/{digest}.json"
                )
            )
        for path in candidates:
            try:
                binding_record = json.loads(path.read_text(encoding="utf-8-sig"))
                binding = dict(binding_record)
                declared_hash = str(binding.pop("binding_sha256", ""))
                if (
                    binding.get("protocol") != LOCAL_BINDING_PROTOCOL
                    or canonical_sha256(binding) != declared_hash
                    or binding.get("role") != role
                    or (run_id is not None and binding.get("run_id") != run_id)
                    or binding.get("content_sha256") != digest
                    or str(binding.get("human_decision") or "").upper()
                    not in _APPROVED_DECISIONS
                ):
                    continue
                _validate_sha256(
                    str(binding.get("human_signature_sha256") or ""),
                    "human_signature_sha256",
                )
                _validate_sha256(
                    str(binding.get("human_signer_sha256") or ""),
                    "human_signer_sha256",
                )
                object_record = json.loads(
                    self._object_path(digest).read_text(encoding="utf-8-sig")
                )
                object_body = dict(object_record)
                object_hash = str(object_body.pop("object_record_sha256", ""))
                if (
                    object_body.get("protocol") != LOCAL_OBJECT_PROTOCOL
                    or object_body.get("content_sha256") != digest
                    or canonical_sha256(object_body) != object_hash
                ):
                    continue
                return {
                    "summary": str(binding["summary"]),
                    "uri": _validate_reference_uri(binding["uri"]),
                    "content_sha256": digest,
                }
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def resolve_signed_reference(
        self,
        *,
        role: str,
        content_sha256_value: str,
        reference_uri: str | None = None,
    ) -> dict[str, str] | None:
        """Resolve a bounded lookup handle to the complete local signed record.

        Lookup receipts may truncate display text to their candidate budget.  The
        execution-control planner must therefore parse only this locally sealed
        binding, never the remote or truncated abstract.
        """
        resolved = self._signed_local_reference(role, content_sha256_value)
        if resolved is None:
            return None
        if reference_uri is not None and resolved.get("uri") != reference_uri:
            return None
        return resolved

    def _extract_remote_items(
        self, response: dict[str, Any] | list[Any], scope_uri: str
    ) -> list[dict[str, str]]:
        value: Any = response
        if isinstance(value, dict) and "result" in value:
            value = value["result"]
        if isinstance(value, dict):
            for key in ("resources", "items", "results", "matches"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if not isinstance(value, list):
            return []
        sanitized: list[dict[str, str]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            uri = str(raw.get("uri") or metadata.get("uri") or "").strip()
            digest = str(raw.get("content_sha256") or metadata.get("content_sha256") or "").strip()
            try:
                score = float(raw.get("score", 0) or 0)
            except (TypeError, ValueError):
                score = 0
            if not uri.startswith(scope_uri.rstrip("/") + "/") or score <= 0:
                continue
            try:
                _validate_reference_uri(uri)
                relative = uri[len(scope_uri.rstrip("/") + "/") :]
                remote_parts = relative.split("/")
                if (
                    len(remote_parts) != 2
                    or remote_parts[1] != f"{digest}.json"
                ):
                    continue
                signed = self._signed_local_reference(
                    role=scope_uri.rsplit("/", 1)[-1],
                    digest=digest,
                    run_id=remote_parts[0],
                )
                if signed is not None:
                    sanitized.append(signed)
            except ValueError:
                continue
        return sanitized

    def _object_path(self, digest: str) -> Path:
        return self.local_root / "objects" / f"{digest}.json"

    def _binding_path(self, reference: MemoryReference) -> Path:
        return (
            self.local_root
            / "bindings"
            / _safe_segment(reference.run_id, "run_id")
            / _safe_segment(reference.role, "role")
            / f"{reference.content_sha256}.json"
        )

    @staticmethod
    def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, indent=2
        )
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(serialized)
            return "STORED"
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
            if existing != dict(payload):
                raise ContextMemoryError("CONTENT_ADDRESS_COLLISION")
            return "DEDUP_HIT"

    def _local_items(self, role: str, query: str) -> list[dict[str, str]]:
        bindings_root = self.local_root / "bindings"
        safe_role = _safe_segment(role, "role")
        if not bindings_root.is_dir():
            return []
        terms = {
            item.lower()
            for item in re.findall(r"[A-Za-z0-9_\-]{2,}|[\u3400-\u9fff]", query)
        }
        ranked: list[tuple[int, dict[str, str]]] = []
        seen: set[str] = set()
        for path in sorted(bindings_root.glob(f"*/{safe_role}/*.json")):
            try:
                binding = json.loads(path.read_text(encoding="utf-8-sig"))
                declared_hash = str(binding.pop("binding_sha256", ""))
                if (
                    binding.get("protocol") != LOCAL_BINDING_PROTOCOL
                    or canonical_sha256(binding) != declared_hash
                    or binding.get("role") != role
                    or str(binding.get("human_decision") or "").upper()
                    not in _APPROVED_DECISIONS
                ):
                    continue
                _validate_sha256(
                    str(binding.get("human_signature_sha256") or ""),
                    "human_signature_sha256",
                )
                digest = _validate_sha256(binding["content_sha256"], "content_sha256")
                if digest in seen:
                    continue
                object_record = json.loads(
                    self._object_path(digest).read_text(encoding="utf-8-sig")
                )
                object_hash = str(object_record.pop("object_record_sha256", ""))
                if (
                    object_record.get("protocol") != LOCAL_OBJECT_PROTOCOL
                    or object_record.get("content_sha256") != digest
                    or canonical_sha256(object_record) != object_hash
                ):
                    continue
                item = {
                    "summary": str(binding["summary"]),
                    "uri": _validate_reference_uri(binding["uri"]),
                    "content_sha256": digest,
                }
                searchable = f"{item['summary']} {item['uri']}".lower()
                score = sum(term in searchable for term in terms)
                if score <= 0:
                    continue
                ranked.append((score, item))
                seen.add(digest)
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                continue
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["content_sha256"]))
        return [item for _, item in ranked]

    @staticmethod
    def _bounded_items(
        items: list[dict[str, str]], budget: MemoryBudget
    ) -> tuple[list[dict[str, str]], int, int]:
        accepted: list[dict[str, str]] = []
        lines: list[str] = []
        for raw in items:
            if len(accepted) >= budget.max_items:
                break
            digest = _validate_sha256(raw["content_sha256"], "content_sha256")
            uri = _validate_reference_uri(raw["uri"])
            summary = str(raw["summary"]).strip()
            prefix = f"{digest} | {uri} | "
            separator = "\n" if lines else ""
            if len(separator + prefix) > budget.max_characters:
                continue
            low, high = 0, len(summary)
            while low < high:
                middle = (low + high + 1) // 2
                candidate_lines = lines + [prefix + summary[:middle]]
                injected = "\n".join(candidate_lines)
                if (
                    len(injected) <= budget.max_characters
                    and _token_estimate(injected) <= budget.max_token_estimate
                ):
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                continue
            bounded = {
                "summary": summary[:low],
                "uri": uri,
                "content_sha256": digest,
            }
            accepted.append(bounded)
            lines.append(prefix + bounded["summary"])
        injected_text = "\n".join(lines)
        return accepted, len(injected_text), _token_estimate(injected_text)

    def lookup(
        self,
        *,
        run_id: str,
        role: str,
        query: str,
        budget: MemoryBudget | None = None,
    ) -> dict[str, Any]:
        started = self._clock()
        run_id = str(run_id).strip()
        role = str(role).strip()
        _safe_segment(run_id, "run_id")
        _safe_segment(role, "role")
        query = str(query).strip()
        if not query or len(query) > 4096:
            raise ValueError("memory query must contain 1..4096 characters")
        active_budget = budget or MemoryBudget()
        query_sha = content_sha256(query)
        cache_key = (
            run_id,
            role,
            query_sha,
            active_budget.max_characters,
            active_budget.max_token_estimate,
            active_budget.max_items,
        )
        cached = self._run_role_cache.get(cache_key)
        remote_status = "NOT_ATTEMPTED"
        if cached is not None:
            items = [dict(item) for item in cached]
            source = "RUN_ROLE_CACHE"
            cache_hit = True
        else:
            items = []
            source = "LOCAL_CONTENT_ADDRESS_INDEX"
            cache_hit = False
            client_enabled = bool(
                self.client is not None and getattr(self.client, "enabled", True)
            )
            if client_enabled:
                config = getattr(self.client, "config", OpenVikingConfig())
                scope_uri = self._role_scope_uri(config, role)
                try:
                    response = self.client.find(
                        query=query,
                        target_uri=scope_uri,
                        limit=max(active_budget.max_items * 2, active_budget.max_items),
                    )
                    items = self._extract_remote_items(response, scope_uri)
                    if items:
                        source = "OPENVIKING_HTTP_SIGNED_LOCAL_BINDING"
                        remote_status = "SIGNED_CANDIDATE_USED"
                    else:
                        items = self._local_items(role, query)
                        source = "LOCAL_CONTENT_ADDRESS_INDEX"
                        remote_status = "NO_SIGNED_REMOTE_MATCH_FALLBACK"
                except (OpenVikingTransportError, TimeoutError, socket.timeout, OSError):
                    items = self._local_items(role, query)
                    source = "LOCAL_CONTENT_ADDRESS_INDEX"
                    remote_status = "TIMEOUT_OR_ERROR_FALLBACK"
            else:
                items = self._local_items(role, query)
                remote_status = "DISABLED"
            bounded, _, _ = self._bounded_items(items, active_budget)
            self._run_role_cache[cache_key] = tuple(dict(item) for item in bounded)
            items = bounded
        bounded, injected_characters, injected_tokens = self._bounded_items(
            items, active_budget
        )
        latency_ms = round(max(0.0, self._clock() - started) * 1000, 3)
        body = {
            "protocol": LOOKUP_PROTOCOL,
            "run_id": run_id,
            "role": role,
            "query_sha256": query_sha,
            "items": bounded,
            "metrics": {
                "lookup_latency_ms": latency_ms,
                "cache_hit": cache_hit,
                "cache_scope": "EXACT_RUN_ROLE_QUERY_AND_BUDGET",
                "candidate_lookup_calls_avoided": 1 if cache_hit else 0,
                "candidate_characters": injected_characters,
                "candidate_token_estimate": injected_tokens,
                "injected_characters": 0,
                "injected_token_estimate": 0,
                "source": source,
                "remote_status": remote_status,
            },
            "finflux_agent_model_called": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
            "prompt_injected": False,
            "token_savings_claim": (
                "NO_PROVIDER_TOKEN_CLAIM; SAME_RUN_CANDIDATE_LOOKUP_REUSED"
                if cache_hit
                else "NO_PROVIDER_TOKEN_CLAIM"
            ),
            "truth_boundary": {
                "raw_financial_data_returned": False,
                "allowed_item_fields": ["content_sha256", "summary", "uri"],
            },
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}

    def commit_long_term(
        self,
        *,
        reference: MemoryReference,
        approval: HumanApproval,
    ) -> dict[str, Any]:
        """Commit only a Human-decision-bound redacted reference; never source financial data."""
        approval.validate_for_commit()
        _validate_operational_summary(reference)
        public = reference.public_payload()
        object_body = {
            "protocol": LOCAL_OBJECT_PROTOCOL,
            "content_sha256": public["content_sha256"],
        }
        object_record = {
            **object_body,
            "object_record_sha256": canonical_sha256(object_body),
        }
        binding_body = {
            "protocol": LOCAL_BINDING_PROTOCOL,
            "run_id": reference.run_id,
            "role": reference.role,
            **public,
            "human_decision": approval.decision.strip().upper(),
            "human_signer_sha256": content_sha256(approval.signer_id.strip()),
            "human_signature_sha256": _validate_sha256(
                approval.signature_sha256, "signature_sha256"
            ),
            "human_signed_at": approval.signed_at.strip(),
        }
        binding_record = {
            **binding_body,
            "binding_sha256": canonical_sha256(binding_body),
        }
        object_status = self._exclusive_write(
            self._object_path(public["content_sha256"]), object_record
        )
        binding_status = self._exclusive_write(
            self._binding_path(reference), binding_record
        )
        for key in list(self._run_role_cache):
            if key[1] == reference.role:
                self._run_role_cache.pop(key, None)

        remote_status = "DISABLED"
        remote_uri = ""
        client_enabled = bool(
            self.client is not None and getattr(self.client, "enabled", True)
        )
        config = getattr(self.client, "config", OpenVikingConfig()) if client_enabled else None
        if client_enabled and config.remote_write_enabled:
            remote_uri = self._remote_reference_uri(config, reference)
            remote_reference = {
                "protocol": REFERENCE_PROTOCOL,
                "run_id": reference.run_id,
                "role": reference.role,
                **public,
                "human_signature_sha256": binding_body["human_signature_sha256"],
            }
            outbox_body = {
                "protocol": OUTBOX_PROTOCOL,
                "target_uri": remote_uri,
                "reference": remote_reference,
            }
            outbox_sha256 = canonical_sha256(outbox_body)
            outbox_record = {**outbox_body, "outbox_sha256": outbox_sha256}
            outbox_status = self._exclusive_write(
                self.local_root / "outbox" / f"{outbox_sha256}.json",
                outbox_record,
            )
            remote_status = (
                "OUTBOX_QUEUED" if outbox_status == "STORED" else "OUTBOX_DEDUP_HIT"
            )

        body = {
            "protocol": COMMIT_PROTOCOL,
            "run_id": reference.run_id,
            "role": reference.role,
            "content_sha256": public["content_sha256"],
            "reference_uri": public["uri"],
            "human_signature_sha256": binding_body["human_signature_sha256"],
            "object_status": object_status,
            "binding_status": binding_status,
            "remote_status": remote_status,
            "remote_uri": remote_uri,
            "human_signature_format_validated": True,
            "human_signature_cryptographically_verified": False,
            "raw_financial_data_committed": False,
            "finflux_agent_model_called": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
            "prompt_injected": False,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}

    def _last_remote_path(self) -> Path:
        return self.local_root / "state" / "last_remote.json"

    def _record_last_remote(self, payload: Mapping[str, Any]) -> None:
        target = self._last_remote_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)

    def _read_last_remote(self) -> dict[str, Any]:
        try:
            value = json.loads(self._last_remote_path().read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {"status": "NEVER"}
        except (OSError, json.JSONDecodeError):
            return {"status": "NEVER"}

    def drain_outbox(self, *, limit: int = 20) -> dict[str, Any]:
        """Request remote write acceptance for event-bound references outside Human Gate."""
        if not 1 <= int(limit) <= 500:
            raise ValueError("outbox drain limit must be between 1 and 500")
        client_enabled = bool(
            self.client is not None and getattr(self.client, "enabled", True)
        )
        config = getattr(self.client, "config", OpenVikingConfig()) if client_enabled else None
        if not client_enabled or not config.remote_write_enabled:
            body = {
                "status": "REMOTE_WRITE_DISABLED",
                "attempted": 0,
                "delivered": 0,
                "failed": 0,
                "finflux_agent_model_called": False,
                "finflux_agent_llm_tokens": 0,
                "openviking_provider_usage": "NOT_CAPTURED",
                "prompt_injected": False,
            }
            return {**body, "receipt_sha256": canonical_sha256(body)}

        attempted = accepted = failed = 0
        for path in sorted((self.local_root / "outbox").glob("*.json"))[:limit]:
            attempted += 1
            started = self._clock()
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                declared = str(record.pop("outbox_sha256", ""))
                if (
                    record.get("protocol") != OUTBOX_PROTOCOL
                    or canonical_sha256(record) != declared
                    or path.stem != declared
                ):
                    raise ContextMemoryError("OUTBOX_HASH_MISMATCH")
                response = self.client.write_reference(
                    target_uri=str(record["target_uri"]),
                    reference=dict(record["reference"]),
                )
                result = (
                    response.get("result")
                    if isinstance(response, dict)
                    and response.get("status") == "ok"
                    else None
                )
                if (
                    not isinstance(result, dict)
                    or result.get("uri") != record["target_uri"]
                    or result.get("content_updated") is not True
                ):
                    raise OpenVikingTransportError(
                        "OPENVIKING_WRITE_ACK_NOT_VERIFIED"
                    )
                semantic_status = str(
                    result.get("semantic_status") or "NOT_REPORTED"
                ).upper()
                vector_status = str(
                    result.get("vector_status") or "NOT_REPORTED"
                ).upper()
                failed_statuses = {"FAILED", "ERROR", "REJECTED", "CANCELLED"}
                if semantic_status in failed_statuses or vector_status in failed_statuses:
                    raise OpenVikingTransportError(
                        "OPENVIKING_WRITE_INDEXING_REPORTED_FAILURE"
                    )
                accepted_body = {
                    **record,
                    "outbox_sha256": declared,
                    "remote_ack": {
                        "uri": result.get("uri"),
                        "content_updated": True,
                        "semantic_status": semantic_status,
                        "vector_status": vector_status,
                    },
                }
                accepted_record = {
                    **accepted_body,
                    "accepted_sha256": canonical_sha256(accepted_body),
                }
                self._exclusive_write(
                    self.local_root / "accepted" / path.name,
                    accepted_record,
                )
                path.unlink()
                accepted += 1
                self._record_last_remote(
                    {
                        "status": "WRITE_ACCEPTED",
                        "outbox_sha256": declared,
                        "semantic_status": semantic_status,
                        "vector_status": vector_status,
                        "latency_ms": round((self._clock() - started) * 1000, 3),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except (
                ContextMemoryError,
                OpenVikingTransportError,
                TimeoutError,
                socket.timeout,
                OSError,
                KeyError,
                ValueError,
                json.JSONDecodeError,
            ):
                failed += 1
                self._record_last_remote(
                    {
                        "status": "FAILED",
                        "outbox_sha256": path.stem,
                        "latency_ms": round((self._clock() - started) * 1000, 3),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                break
        body = {
            "status": (
                "NO_WORK"
                if attempted == 0
                else "WRITE_ACCEPTED"
                if failed == 0 and accepted == attempted
                else "PARTIAL_OR_FAILED"
            ),
            "attempted": attempted,
            "accepted": accepted,
            "delivered": 0,
            "failed": failed,
            "finflux_agent_model_called": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
            "prompt_injected": False,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}

    def delivery_status(self, run_id: str) -> dict[str, Any]:
        """Return Run-bound remote write-acceptance evidence, not retrieval delivery."""
        safe_run = _safe_segment(run_id, "run_id")
        pending: list[str] = []
        accepted: list[dict[str, Any]] = []
        for path in sorted((self.local_root / "outbox").glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                if (record.get("reference") or {}).get("run_id") == safe_run:
                    pending.append(path.stem)
            except (OSError, json.JSONDecodeError):
                continue
        for path in sorted((self.local_root / "accepted").glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                declared = str(record.pop("accepted_sha256", ""))
                if (
                    canonical_sha256(record) == declared
                    and (record.get("reference") or {}).get("run_id") == safe_run
                ):
                    accepted.append(
                        {
                            "outbox_sha256": record.get("outbox_sha256"),
                            "remote_ack": record.get("remote_ack"),
                        }
                    )
            except (OSError, json.JSONDecodeError):
                continue
        body = {
            "protocol": "FINFLUX_CONTEXT_MEMORY_REMOTE_WRITE_ACCEPTANCE_V1",
            "run_id": safe_run,
            "status": (
                "PENDING"
                if pending
                else "WRITE_ACCEPTED"
                if accepted
                else "NOT_REQUESTED_OR_NOT_CAPTURED"
            ),
            "pending_outbox_sha256": pending,
            "accepted": accepted,
            "delivered": False,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}

    def schedule_outbox_drain(self, *, limit: int = 20) -> dict[str, Any]:
        """Schedule a best-effort daemon drain without blocking the caller."""
        if not 1 <= int(limit) <= 500:
            raise ValueError("outbox drain limit must be between 1 and 500")
        client_enabled = bool(
            self.client is not None and getattr(self.client, "enabled", True)
        )
        config = getattr(self.client, "config", OpenVikingConfig()) if client_enabled else None
        if not client_enabled or not config.remote_write_enabled:
            status = "DISABLED"
        elif not self._outbox_drain_lock.acquire(blocking=False):
            status = "ALREADY_RUNNING"
        else:
            def _run() -> None:
                try:
                    self.drain_outbox(limit=limit)
                finally:
                    self._outbox_drain_lock.release()

            try:
                threading.Thread(
                    target=_run,
                    name="FinFluxContextMemoryOutboxDrain",
                    daemon=True,
                ).start()
                status = "SCHEDULED"
            except RuntimeError:
                self._outbox_drain_lock.release()
                raise
        body = {
            "status": status,
            "limit": int(limit),
            "non_blocking": True,
            "finflux_agent_model_called": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
            "prompt_injected": False,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}

    def status(self) -> dict[str, Any]:
        client_enabled = bool(
            self.client is not None and getattr(self.client, "enabled", True)
        )
        configured = self.backend == "local" or client_enabled
        remote_write_enabled = bool(
            client_enabled
            and getattr(self.client, "config", OpenVikingConfig()).remote_write_enabled
        )
        body = {
            "protocol": STATUS_PROTOCOL,
            "backend": self.backend,
            "configured": configured,
            "status": (
                "LOCAL_ACTIVE"
                if self.backend == "local"
                else "OPENVIKING_CONFIGURED_NOT_PROBED"
                if client_enabled
                else "OPENVIKING_NOT_CONFIGURED_LOCAL_FALLBACK"
            ),
            "remote_enabled": client_enabled,
            "remote_ready": "NOT_PROBED" if client_enabled else False,
            "remote_write_enabled": remote_write_enabled,
            "fallback_active": self.backend == "local" or not client_enabled,
            "local": {
                "objects": len(list((self.local_root / "objects").glob("*.json"))),
                "bindings": len(list((self.local_root / "bindings").glob("*/*/*.json"))),
                "outbox": len(list((self.local_root / "outbox").glob("*.json"))),
                "accepted_remote_writes": len(
                    list((self.local_root / "accepted").glob("*.json"))
                ),
            },
            "cache": {"run_role_query_entries": len(self._run_role_cache)},
            "outbox_drain_running": self._outbox_drain_lock.locked(),
            "last_remote": self._read_last_remote(),
            "prompt_injected": False,
            "finflux_agent_model_called": False,
            "finflux_agent_llm_tokens": 0,
            "openviking_provider_usage": "NOT_CAPTURED",
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}


def build_context_memory_adapter(
    *, local_root: Path | None = None
) -> ContextMemoryAdapter:
    """Build the optional remote adapter from environment configuration."""
    backend = memory_setting(
        "FINFLUX_CONTEXT_MEMORY_BACKEND", "local"
    ).strip().lower()
    if backend not in {"local", "openviking"}:
        raise ValueError("FINFLUX_CONTEXT_MEMORY_BACKEND must be local or openviking")
    config = OpenVikingConfig.from_env()
    client = OpenVikingHTTPClient(config) if backend == "openviking" and config.enabled else None
    return ContextMemoryAdapter(
        client=client,
        local_root=local_root,
        backend=backend,
    )
