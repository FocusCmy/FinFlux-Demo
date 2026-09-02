from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


MODULE_ROOT = Path(__file__).resolve().parent
DEMO_ROOT = MODULE_ROOT.parent
CONFIG_ROOT = MODULE_ROOT / "config"
DATA_ROOT = DEMO_ROOT / "data" / "research_data_layer_v1"
REGISTRY_PATH = CONFIG_ROOT / "provider_registry_v1.json"
SCHEMA_PATH = CONFIG_ROOT / "research_item_schema_v1.json"
ITEMS_PATH = DATA_ROOT / "research_items.jsonl"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
QUALITY_PATH = DATA_ROOT / "quality_report.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_json(payload: Any) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_bytes_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def load_provider_registry() -> dict[str, Any]:
    registry = read_json(REGISTRY_PATH)
    providers = registry.get("providers", [])
    provider_ids = [str(item.get("provider_id", "")) for item in providers]
    if len(providers) != 6 or len(provider_ids) != len(set(provider_ids)):
        raise ValueError("Research Provider Registry必须恰好包含六个唯一Provider")
    if set(provider_ids) != {
        "finrpt",
        "eastmoney_report",
        "worldbank",
        "imf",
        "bis",
        "ecb",
    }:
        raise ValueError("Research Provider Registry与v1冻结集合不一致")
    return registry


class RightsGate:
    """Fail-closed content-use policy independent from Agent judgement."""

    def __init__(self, registry: dict[str, Any] | None = None) -> None:
        registry = registry or load_provider_registry()
        self.providers = {
            str(item["provider_id"]): item for item in registry["providers"]
        }

    def evaluate(
        self,
        provider_id: str,
        content_type: str,
        requested_action: str,
    ) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if not provider:
            return self._decision(
                provider_id,
                content_type,
                requested_action,
                "DENY",
                "PROVIDER_NOT_REGISTERED",
                "PROHIBITED",
                "QUARANTINE",
            )
        rights_state = str(provider.get("rights_state", "REVIEW_REQUIRED"))
        if rights_state == "PROHIBITED":
            return self._decision(
                provider_id,
                content_type,
                requested_action,
                "DENY",
                "PROVIDER_PROHIBITED",
                rights_state,
                "QUARANTINE",
            )
        if requested_action == "STORE_METADATA":
            return self._decision(
                provider_id,
                content_type,
                requested_action,
                "ALLOW",
                "METADATA_IS_MINIMUM_PROVENANCE",
                rights_state,
                "METADATA_ONLY",
            )
        if (
            requested_action == "STORE_STRUCTURED_DATA"
            and content_type == "OFFICIAL_STATISTIC"
            and provider_id in {"worldbank", "imf", "bis", "ecb"}
        ):
            return self._decision(
                provider_id,
                content_type,
                requested_action,
                "ALLOW",
                "REGISTERED_OFFICIAL_STATISTIC_WITH_ATTRIBUTION",
                rights_state,
                "FULL_CONTENT",
            )
        return self._decision(
            provider_id,
            content_type,
            requested_action,
            "DENY",
            "FULLTEXT_OR_DERIVATIVE_RIGHTS_NOT_PROVEN",
            rights_state,
            "LINK_ONLY",
        )

    @staticmethod
    def _decision(
        provider_id: str,
        content_type: str,
        requested_action: str,
        decision: str,
        reason: str,
        rights_state: str,
        storage_policy: str,
    ) -> dict[str, Any]:
        payload = {
            "protocol": "FINFLUX_RIGHTS_DECISION_V0.1",
            "provider_id": provider_id,
            "content_type": content_type,
            "requested_action": requested_action,
            "decision": decision,
            "reason": reason,
            "rights_state": rights_state,
            "storage_policy": storage_policy,
            "evaluated_at": utc_now(),
            "policy_version": "research-rights-gate@0.1.0",
        }
        payload["decision_sha256"] = sha256_json(payload)
        return payload


def _parse_datetime(value: str) -> None:
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_research_item(item: dict[str, Any]) -> list[str]:
    schema = read_json(SCHEMA_PATH)
    errors: list[str] = []
    for field in schema.get("required", []):
        if field not in item or item[field] in (None, ""):
            errors.append(f"missing_required:{field}")

    enum_fields = {
        "provider_id": {"finrpt", "eastmoney_report", "worldbank", "imf", "bis", "ecb"},
        "provider_tier": {"OFFICIAL_PRIMARY", "MARKET_RESEARCH", "BENCHMARK_REFERENCE"},
        "content_type": {"OFFICIAL_STATISTIC", "RESEARCH_DOCUMENT", "NEWS_RELEASE", "RESEARCH_CLAIM"},
        "rights_state": {"OPEN_REUSE", "ATTRIBUTION_REQUIRED", "INTERNAL_AUTHORIZED", "LINK_ONLY", "REVIEW_REQUIRED", "PROHIBITED"},
        "storage_policy": {"FULL_CONTENT", "AUTHORIZED_CONTENT", "METADATA_ONLY", "LINK_ONLY", "QUARANTINE"},
        "quality_status": {"DISCOVERED", "VERIFIED_METADATA", "VERIFIED_CONTENT", "CONFLICTED", "REJECTED"},
        "retrieval_method": {"REST_GET", "REST_POST", "SDMX", "RSS", "PINNED_DATASET"},
    }
    for field, allowed in enum_fields.items():
        if item.get(field) not in allowed:
            errors.append(f"invalid_enum:{field}:{item.get(field)}")

    for field in ("raw_response_sha256", "metadata_sha256"):
        value = str(item.get(field, ""))
        if not SHA256_PATTERN.fullmatch(value):
            errors.append(f"invalid_sha256:{field}")
    content_hash = item.get("content_sha256")
    if content_hash is not None and not SHA256_PATTERN.fullmatch(str(content_hash)):
        errors.append("invalid_sha256:content_sha256")

    for field in ("published_at", "captured_at"):
        try:
            _parse_datetime(str(item.get(field, "")))
        except (ValueError, TypeError):
            errors.append(f"invalid_datetime:{field}")

    url = urlparse(str(item.get("source_url", "")))
    if url.scheme not in {"http", "https"} or not url.netloc:
        errors.append("invalid_uri:source_url")

    if item.get("rights_state") in {"LINK_ONLY", "REVIEW_REQUIRED"}:
        if item.get("storage_policy") not in {"METADATA_ONLY", "LINK_ONLY", "QUARANTINE"}:
            errors.append("rights_storage_conflict")
        if item.get("content_sha256") is not None:
            errors.append("restricted_content_hash_must_be_null")
    if item.get("content_type") == "RESEARCH_CLAIM" and not item.get("locator"):
        errors.append("research_claim_without_locator")
    return errors


class ResearchDataStore:
    def __init__(self, root: Path = DATA_ROOT) -> None:
        self.root = root
        self.items_path = root / "research_items.jsonl"
        self.manifest_path = root / "manifest.json"
        self.quality_path = root / "quality_report.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def save_raw(
        self,
        provider_id: str,
        request_key: str,
        body: bytes,
        suffix: str = ".json",
    ) -> dict[str, str]:
        digest = sha256_bytes(body)
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_key).strip("-")[:80]
        relative = Path("raw") / provider_id / f"{safe_key}-{digest[:12]}{suffix}"
        path = self.root / relative
        if path.exists():
            if sha256_bytes(path.read_bytes()) != digest:
                raise ValueError(f"raw evidence collision: {relative.as_posix()}")
        else:
            write_bytes_atomic(path, body)
        return {"sha256": digest, "path": relative.as_posix()}

    def load_items(self) -> list[dict[str, Any]]:
        if not self.items_path.exists():
            return []
        items: list[dict[str, Any]] = []
        for number, line in enumerate(
            self.items_path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {number}") from exc
        return items

    def upsert_items(self, items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        existing = {item["research_item_id"]: item for item in self.load_items()}
        inserted = 0
        unchanged = 0
        for item in items:
            errors = validate_research_item(item)
            if errors:
                raise ValueError(
                    f"ResearchItem {item.get('research_item_id')} invalid: {errors}"
                )
            item_id = str(item["research_item_id"])
            previous = existing.get(item_id)
            if previous is not None:
                if previous.get("metadata_sha256") != item.get("metadata_sha256"):
                    raise ValueError(f"immutable ResearchItem changed: {item_id}")
                unchanged += 1
                continue
            existing[item_id] = item
            inserted += 1

        ordered = sorted(
            existing.values(),
            key=lambda item: (
                str(item.get("provider_id", "")),
                str(item.get("published_at", "")),
                str(item.get("research_item_id", "")),
            ),
        )
        payload = b"".join(canonical_json_bytes(item) + b"\n" for item in ordered)
        write_bytes_atomic(self.items_path, payload)
        report = self._quality_report(ordered)
        write_json_atomic(self.quality_path, report)
        raw_files = []
        for path in sorted((self.root / "raw").rglob("*")):
            if path.is_file():
                raw_files.append(
                    {
                        "path": path.relative_to(self.root).as_posix(),
                        "sha256": sha256_bytes(path.read_bytes()),
                        "bytes": path.stat().st_size,
                    }
                )
        manifest = {
            "protocol": "FINFLUX_RESEARCH_CACHE_MANIFEST_V0.1",
            "dataset_version": "research-cache-v1",
            "generated_at": utc_now(),
            "item_count": len(ordered),
            "items_sha256": sha256_bytes(payload),
            "provider_counts": dict(Counter(item["provider_id"] for item in ordered)),
            "content_type_counts": dict(Counter(item["content_type"] for item in ordered)),
            "rights_state_counts": dict(Counter(item["rights_state"] for item in ordered)),
            "raw_files": raw_files,
            "quality_report": self.quality_path.relative_to(self.root).as_posix(),
        }
        manifest["manifest_sha256"] = sha256_json(manifest)
        write_json_atomic(self.manifest_path, manifest)
        return {
            "inserted": inserted,
            "unchanged": unchanged,
            "total": len(ordered),
            "manifest": manifest,
            "quality": report,
        }

    @staticmethod
    def _quality_report(items: list[dict[str, Any]]) -> dict[str, Any]:
        errors: dict[str, list[str]] = {}
        ids: list[str] = []
        for item in items:
            item_id = str(item.get("research_item_id", ""))
            ids.append(item_id)
            item_errors = validate_research_item(item)
            if item_errors:
                errors[item_id] = item_errors
        duplicate_ids = [item_id for item_id, count in Counter(ids).items() if count > 1]
        provider_counts = Counter(str(item.get("provider_id", "")) for item in items)
        restricted_leaks = [
            item["research_item_id"]
            for item in items
            if item.get("rights_state") in {"LINK_ONLY", "REVIEW_REQUIRED"}
            and item.get("content_sha256") is not None
        ]
        required = read_json(SCHEMA_PATH).get("required", [])
        total_cells = len(items) * len(required)
        missing_cells = sum(
            1
            for item in items
            for field in required
            if field not in item or item[field] in (None, "")
        )
        return {
            "protocol": "FINFLUX_RESEARCH_QUALITY_REPORT_V0.1",
            "generated_at": utc_now(),
            "item_count": len(items),
            "provider_counts": dict(provider_counts),
            "provider_count": len(provider_counts),
            "schema_error_count": len(errors),
            "schema_errors": errors,
            "duplicate_id_count": len(duplicate_ids),
            "duplicate_ids": duplicate_ids,
            "required_field_completeness_rate": (
                1.0 if total_cells == 0 else round((total_cells - missing_cells) / total_cells, 6)
            ),
            "restricted_content_leak_count": len(restricted_leaks),
            "restricted_content_leaks": restricted_leaks,
            "point_in_time_safe_count": sum(
                1 for item in items if item.get("point_in_time_safe") is True
            ),
            "status": (
                "PASS"
                if not errors and not duplicate_ids and not restricted_leaks
                else "FAIL"
            ),
        }

