from __future__ import annotations

import ipaddress
import json
import mimetypes
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol_v02 import load_profile_registry


MAX_TEXT_BYTES = 1024 * 1024
MAX_URL_BYTES = 10 * 1024 * 1024
CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "research_data_layer_v1"
    / "research_items.jsonl"
)
REGISTRY_PATH = (
    Path(__file__).resolve().parent
    / "research_data"
    / "config"
    / "provider_registry_v1.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalog_items() -> list[dict[str, Any]]:
    if not CATALOG_PATH.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in CATALOG_PATH.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                items.append(value)
    return items


def provider_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.is_file():
        return {"providers": []}
    value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {"providers": []}


def intake_capabilities() -> dict[str, Any]:
    catalog = _catalog_items()
    providers = provider_registry().get("providers", [])
    frozen_profiles = load_profile_registry().get("profiles", [])
    provider_counts: dict[str, int] = {}
    for item in catalog:
        provider_id = str(item.get("provider_id", "unknown"))
        provider_counts[provider_id] = provider_counts.get(provider_id, 0) + 1
    return {
        "protocol": "FINFLUX_UNIFIED_INTAKE_CAPABILITIES_V0.2",
        "case_input_protocol": "FINFLUX_CASE_INPUT_V0.2",
        "inputs": [
            {"id": "file_plus_intent", "label": "文件 + 任务指令", "status": "AVAILABLE"},
            {"id": "public_url_plus_intent", "label": "公开 URL + 任务指令", "status": "AVAILABLE_BOUNDED"},
            {"id": "research_catalog_plus_intent", "label": "真实资料库 + 任务指令", "status": "AVAILABLE"},
            {"id": "existing_evidence_plus_intent", "label": "已有证据 + 新任务指令", "status": "AVAILABLE"},
        ],
        "accepted_extensions": [
            ".csv", ".json", ".txt", ".md", ".xml", ".html",
            ".htm", ".xlsx", ".pdf", ".zip",
        ],
        "profiles": [
            *[
                {
                    "profile": profile["profile_id"],
                    "profile_version": profile["profile_version"],
                    "profile_sha256": profile["profile_sha256"],
                    "asset_class": profile["asset_class"],
                    "display_name": profile["display_name"],
                    "execution_readiness": (
                        "AGENTTEAMS_EXECUTABLE"
                        if profile["profile_id"] in {
                            "futures_settlement",
                            "equity_corporate_action",
                            "fund_nav_admission",
                        }
                        else "ZERO_MODEL_ACCEPTED_NOT_LIVE"
                    ),
                    "declared_purposes": [
                        purpose["purpose_id"]
                        for purpose in profile["declared_purposes"]
                    ],
                    "required_semantics": [
                        field["field_id"] for field in profile["display_fields"]
                    ],
                }
                for profile in frozen_profiles
            ],
            {
                "profile": "universal_financial_evidence",
                "execution_readiness": "WAIT_FOR_PROFILE",
                "required_semantics": [],
            },
        ],
        "provider_registry": [
            {
                "provider_id": item.get("provider_id"),
                "display_name": item.get("display_name"),
                "implementation_status": item.get("implementation_status", "UNKNOWN"),
                "cached_real_items": provider_counts.get(str(item.get("provider_id")), 0),
                "rights_state": item.get("rights_state", "REVIEW_REQUIRED"),
            }
            for item in providers
        ],
        "catalog_real_item_count": len(catalog),
        "truth_boundary": (
            "任务指令不是金融证据；文件、URL或ResearchItem先按原始字节固化。"
            "二者以哈希绑定为同一Case，只有已注册金融语义契约的Profile才能进入"
            "确定性计算，并在Token Guard允许时派发AgentTeams。"
        ),
    }


def search_research_catalog(
    query: str = "",
    provider_id: str = "",
    asset_class: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    query = query.strip().lower()
    provider_id = provider_id.strip().lower()
    asset_class = asset_class.strip().lower()
    terms = [part for part in query.replace("，", " ").split() if part]
    results: list[dict[str, Any]] = []
    for item in _catalog_items():
        if provider_id and str(item.get("provider_id", "")).lower() != provider_id:
            continue
        haystack = " ".join(
            [
                str(item.get("title", "")),
                str(item.get("publisher", "")),
                " ".join(str(value) for value in item.get("entities", []) or []),
                " ".join(str(value) for value in item.get("topics", []) or []),
                json.dumps(item.get("attributes", {}), ensure_ascii=False),
            ]
        ).lower()
        if terms and not all(term in haystack for term in terms):
            continue
        if asset_class and asset_class not in haystack:
            continue
        results.append(
            {
                "research_item_id": item.get("research_item_id"),
                "provider_id": item.get("provider_id"),
                "provider_tier": item.get("provider_tier"),
                "title": item.get("title"),
                "publisher": item.get("publisher"),
                "published_at": item.get("published_at"),
                "entities": item.get("entities", []),
                "topics": item.get("topics", []),
                "rights_state": item.get("rights_state"),
                "storage_policy": item.get("storage_policy"),
                "source_url": item.get("source_url"),
                "metadata_sha256": item.get("metadata_sha256"),
            }
        )
        if len(results) >= max(1, min(int(limit), 50)):
            break
    return {
        "protocol": "FINFLUX_RESEARCH_CATALOG_SEARCH_V0.1",
        "query": query,
        "provider_id": provider_id or None,
        "asset_class": asset_class or None,
        "count": len(results),
        "items": results,
        "source": "LOCAL_CACHE_OF_REAL_PROVIDER_RESPONSES",
        "synthetic_records": 0,
    }


def selected_research_items(item_ids: list[str]) -> list[dict[str, Any]]:
    wanted = {str(value).strip() for value in item_ids if str(value).strip()}
    if not wanted:
        raise ValueError("至少选择一条真实 ResearchItem")
    if len(wanted) > 20:
        raise ValueError("单次最多选择20条ResearchItem")
    found = [
        item for item in _catalog_items()
        if str(item.get("research_item_id", "")) in wanted
    ]
    missing = sorted(wanted - {str(item.get("research_item_id")) for item in found})
    if missing:
        raise ValueError(f"ResearchItem不存在: {', '.join(missing[:3])}")
    return found


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_public_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许带明确主机名的HTTP/HTTPS公开URL")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("禁止采集本机或内网URL")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"URL主机无法解析: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("禁止采集本机、内网、保留地址或链路本地URL")
    return parsed


def fetch_public_url(url: str, timeout_seconds: int = 20) -> dict[str, Any]:
    parsed = validate_public_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        parsed.geturl(),
        headers={
            "User-Agent": "FinFlux-UnifiedIntake/0.1 bounded-public-evidence",
            "Accept": "application/json,text/csv,text/plain,text/html,application/pdf,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=max(3, min(int(timeout_seconds), 30))) as response:
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > MAX_URL_BYTES:
                raise ValueError("远程内容超过10MB接入上限")
            body = response.read(MAX_URL_BYTES + 1)
            if len(body) > MAX_URL_BYTES:
                raise ValueError("远程内容超过10MB接入上限")
            content_type = response.headers.get_content_type() or "application/octet-stream"
            final_url = response.geturl()
            validate_public_url(final_url)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"公开URL返回HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"公开URL采集失败: {exc.reason}") from exc
    if not body:
        raise ValueError("公开URL返回空内容")
    suffix = Path(urllib.parse.urlparse(final_url).path).suffix.lower()
    if suffix not in {".csv", ".json", ".txt", ".md", ".xml", ".html", ".htm", ".xlsx", ".pdf", ".zip"}:
        suffix = mimetypes.guess_extension(content_type) or ".bin"
    if suffix == ".bin":
        if content_type.startswith("text/"):
            suffix = ".txt"
        elif content_type == "application/json":
            suffix = ".json"
        elif content_type == "application/pdf":
            suffix = ".pdf"
        elif content_type in {
            "application/zip",
            "application/x-zip-compressed",
        }:
            suffix = ".zip"
        elif content_type in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            suffix = ".xlsx"
        else:
            raise ValueError(f"公开URL内容类型暂不支持: {content_type}")
    filename = Path(urllib.parse.urlparse(final_url).path).name or f"remote-evidence{suffix}"
    if not Path(filename).suffix:
        filename += suffix
    return {
        "filename": filename,
        "body": body,
        "content_type": content_type,
        "final_url": final_url,
        "captured_at": utc_now(),
    }
