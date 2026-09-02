from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .core import (
    ResearchDataStore,
    RightsGate,
    canonical_json_bytes,
    sha256_json,
    utc_now,
)


ADAPTER_VERSION = "0.1.0"
PARSER_VERSION = "research-parser@0.1.0"
NORMALIZER_VERSION = "research-normalizer@0.1.0"


@dataclass(frozen=True)
class HttpCapture:
    request_url: str
    request_method: str
    status: int
    headers: dict[str, str]
    body: bytes
    captured_at: str


class HttpClient:
    def __init__(self, timeout: int = 30, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries
        self.user_agent = "FinFlux-ResearchDataLayer/0.1 metadata-only"

    def request(
        self,
        url: str,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> HttpCapture:
        data = None
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if json_body is not None:
            data = canonical_json_bytes(json_body)
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    return HttpCapture(
                        request_url=url,
                        request_method=method,
                        status=int(response.status),
                        headers={
                            key.lower(): value
                            for key, value in response.headers.items()
                            if key.lower() in {"content-type", "date", "etag", "last-modified"}
                        },
                        body=body,
                        captured_at=utc_now(),
                    )
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"provider request failed: {method} {url}: {last_error}")


def _metadata_hash(item: dict[str, Any]) -> str:
    excluded = {"metadata_sha256", "research_item_id"}
    return sha256_json({key: value for key, value in item.items() if key not in excluded})


def _eastmoney_datetime(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    return parsed.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()


def _worldbank_datetime(value: str) -> str:
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        parsed = datetime.strptime(raw, "%Y/%m/%d %H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc).isoformat()


class EastMoneyReportProvider:
    provider_id = "eastmoney_report"
    endpoint = "https://reportapi.eastmoney.com/report/list2"

    def __init__(
        self,
        store: ResearchDataStore,
        client: HttpClient | None = None,
        rights_gate: RightsGate | None = None,
    ) -> None:
        self.store = store
        self.client = client or HttpClient()
        self.rights_gate = rights_gate or RightsGate()

    def fetch_stock_reports(
        self,
        codes: list[str],
        per_code: int = 4,
        begin_time: str = "2026-01-01",
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        end_time = end_time or date.today().isoformat()
        results: list[dict[str, Any]] = []
        for index, code in enumerate(codes):
            body = {
                "pageSize": max(per_code, 10),
                "pageNo": 1,
                "p": 1,
                "pageNum": 1,
                "pageNumber": 1,
                "beginTime": begin_time,
                "endTime": end_time,
                "code": code,
                "industryCode": "*",
                "rating": None,
                "ratingChange": None,
                "orgCode": None,
                "rcode": "",
            }
            capture = self.client.request(self.endpoint, method="POST", json_body=body)
            raw = self.store.save_raw(
                self.provider_id,
                f"stock-{code}-{begin_time}-{end_time}",
                capture.body,
            )
            payload = json.loads(capture.body.decode("utf-8-sig"))
            rows = payload.get("data", [])
            if not isinstance(rows, list):
                raise ValueError(f"EastMoney response data is not a list for {code}")
            for row in rows[:per_code]:
                results.append(self._normalize(row, capture, raw, code))
            if index + 1 < len(codes):
                time.sleep(0.4)
        return results

    def _normalize(
        self,
        row: dict[str, Any],
        capture: HttpCapture,
        raw: dict[str, str],
        requested_code: str,
    ) -> dict[str, Any]:
        source_id = str(row.get("infoCode", "")).strip()
        if not source_id:
            raise ValueError("EastMoney report missing infoCode")
        published_at = _eastmoney_datetime(str(row.get("publishDate", "")))
        authors = []
        for value in row.get("author", []) or []:
            name = str(value).split(".", 1)[-1].strip()
            if name and name not in authors:
                authors.append(name)
        if not authors:
            authors = [
                name.strip()
                for name in str(row.get("researcher", "")).split(",")
                if name.strip()
            ]
        rights = self.rights_gate.evaluate(
            self.provider_id, "RESEARCH_DOCUMENT", "STORE_METADATA"
        )
        item: dict[str, Any] = {
            "research_item_id": "",
            "provider_id": self.provider_id,
            "provider_tier": "MARKET_RESEARCH",
            "content_type": "RESEARCH_DOCUMENT",
            "source_document_id": source_id,
            "source_url": f"https://data.eastmoney.com/report/zw_stock.jshtml?infocode={urllib.parse.quote(source_id)}",
            "title": str(row.get("title", "")).strip(),
            "publisher": str(row.get("orgName") or row.get("orgSName") or "UNKNOWN").strip(),
            "authors": authors,
            "language": "zh-CN",
            "published_at": published_at,
            "captured_at": capture.captured_at,
            "effective_at": None,
            "as_of": published_at,
            "observation_period": None,
            "frequency": None,
            "unit": None,
            "adjustment": None,
            "document_version": published_at,
            "content_sha256": None,
            "raw_response_sha256": raw["sha256"],
            "raw_response_path": raw["path"],
            "metadata_sha256": "",
            "rights_state": rights["rights_state"],
            "storage_policy": rights["storage_policy"],
            "citation_text": f"东方财富研究报告索引：{row.get('title', '')}，{row.get('orgName', '')}，{str(row.get('publishDate', ''))[:10]}，{source_id}",
            "parser_version": PARSER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "adapter_version": f"eastmoney-report-adapter@{ADAPTER_VERSION}",
            "retrieval_method": "REST_POST",
            "quality_status": "VERIFIED_METADATA",
            "point_in_time_safe": True,
            "entities": [
                value
                for value in (
                    str(row.get("stockCode") or requested_code).strip(),
                    str(row.get("stockName", "")).strip(),
                    str(row.get("industryName", "")).strip(),
                )
                if value
            ],
            "topics": ["A股", "个股研报"],
            "attributes": {
                "stock_code": str(row.get("stockCode") or requested_code),
                "stock_name": str(row.get("stockName", "")),
                "industry_code": str(row.get("industryCode", "")),
                "industry_name": str(row.get("industryName", "")),
                "rating_name": str(row.get("emRatingName", "")),
                "rating_change": row.get("ratingChange"),
                "organization_code": str(row.get("orgCode", "")),
                "attachment_pages": row.get("attachPages"),
                "attachment_size_kb": row.get("attachSize"),
                "requested_code": requested_code,
                "rights_decision_sha256": rights["decision_sha256"],
            },
            "locator": None,
            "supersedes": None,
            "run_id": None,
            "tool_call_id": None,
        }
        metadata_hash = _metadata_hash(item)
        item["metadata_sha256"] = metadata_hash
        item["research_item_id"] = (
            f"RDI-eastmoney-report-{source_id}-{metadata_hash[:12]}"
        )
        return item


class WorldBankProvider:
    provider_id = "worldbank"
    documents_endpoint = "https://search.worldbank.org/api/v3/wds"
    indicators_base = "https://api.worldbank.org/v2"

    def __init__(
        self,
        store: ResearchDataStore,
        client: HttpClient | None = None,
        rights_gate: RightsGate | None = None,
    ) -> None:
        self.store = store
        self.client = client or HttpClient()
        self.rights_gate = rights_gate or RightsGate()

    def fetch_documents(
        self,
        query: str = "financial stability",
        count: int = 10,
        start_date: str = "2025-01-01",
    ) -> list[dict[str, Any]]:
        params = {
            "format": "json",
            "qterm": query,
            "rows": str(count),
            "os": "0",
            "strdate": start_date,
            "sort": "docdt",
            "order": "desc",
            "fl": "docdt,docty,abstracts,count,lang,authr,repnb",
        }
        url = self.documents_endpoint + "?" + urllib.parse.urlencode(params)
        capture = self.client.request(url)
        raw = self.store.save_raw(
            self.provider_id,
            f"documents-{query}-{start_date}-{count}",
            capture.body,
        )
        payload = json.loads(capture.body.decode("utf-8-sig"))
        documents = payload.get("documents", {})
        if not isinstance(documents, dict):
            raise ValueError("World Bank documents response is not an object")
        rows = [value for value in documents.values() if isinstance(value, dict)]
        rows.sort(key=lambda row: _worldbank_datetime(str(row.get("docdt", "1970-01-01T00:00:00Z"))), reverse=True)
        return [self._normalize_document(row, capture, raw) for row in rows[:count]]

    def fetch_indicators(
        self,
        country: str,
        indicators: list[str],
        observations_per_indicator: int = 5,
        start_year: int = 2018,
        end_year: int | None = None,
    ) -> list[dict[str, Any]]:
        end_year = end_year or date.today().year
        results: list[dict[str, Any]] = []
        for indicator in indicators:
            params = {
                "format": "json",
                "date": f"{start_year}:{end_year}",
                "per_page": "100",
            }
            url = (
                f"{self.indicators_base}/country/{urllib.parse.quote(country)}/indicator/"
                f"{urllib.parse.quote(indicator)}?{urllib.parse.urlencode(params)}"
            )
            capture = self.client.request(url)
            raw = self.store.save_raw(
                self.provider_id,
                f"indicator-{country}-{indicator}-{start_year}-{end_year}",
                capture.body,
            )
            payload = json.loads(capture.body.decode("utf-8-sig"))
            if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
                raise ValueError(f"World Bank indicator response invalid: {indicator}")
            rows = [row for row in payload[1] if row.get("value") is not None]
            rows.sort(key=lambda row: str(row.get("date", "")), reverse=True)
            for row in rows[:observations_per_indicator]:
                results.append(self._normalize_indicator(row, indicator, capture, raw))
        return results

    def _normalize_document(
        self,
        row: dict[str, Any],
        capture: HttpCapture,
        raw: dict[str, str],
    ) -> dict[str, Any]:
        source_id = str(row.get("id", "")).strip()
        published_at = _worldbank_datetime(str(row.get("docdt", "")))
        authors_map = row.get("authors", {})
        authors = []
        if isinstance(authors_map, dict):
            authors = [
                str(value.get("author", "")).strip()
                for value in authors_map.values()
                if isinstance(value, dict) and str(value.get("author", "")).strip()
            ]
        rights = self.rights_gate.evaluate(
            self.provider_id, "RESEARCH_DOCUMENT", "STORE_METADATA"
        )
        source_url = str(row.get("url") or row.get("pdfurl") or capture.request_url)
        if source_url.startswith("http://"):
            source_url = "https://" + source_url[len("http://") :]
        item: dict[str, Any] = {
            "research_item_id": "",
            "provider_id": self.provider_id,
            "provider_tier": "OFFICIAL_PRIMARY",
            "content_type": "RESEARCH_DOCUMENT",
            "source_document_id": source_id,
            "source_url": source_url,
            "title": str(row.get("display_title", "")).strip(),
            "publisher": "World Bank",
            "authors": authors,
            "language": str(row.get("lang") or "English"),
            "published_at": published_at,
            "captured_at": capture.captured_at,
            "effective_at": None,
            "as_of": published_at,
            "observation_period": None,
            "frequency": None,
            "unit": None,
            "adjustment": None,
            "document_version": str(row.get("entityids", {}).get("entityid") or published_at),
            "content_sha256": None,
            "raw_response_sha256": raw["sha256"],
            "raw_response_path": raw["path"],
            "metadata_sha256": "",
            "rights_state": rights["rights_state"],
            "storage_policy": rights["storage_policy"],
            "citation_text": f"World Bank Documents & Reports: {row.get('display_title', '')}, {published_at[:10]}, document {source_id}",
            "parser_version": PARSER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "adapter_version": f"worldbank-documents-adapter@{ADAPTER_VERSION}",
            "retrieval_method": "REST_GET",
            "quality_status": "VERIFIED_METADATA",
            "point_in_time_safe": True,
            "entities": [str(row.get("count", "")).strip()] if row.get("count") else [],
            "topics": ["financial stability", str(row.get("docty", "")).strip()],
            "attributes": {
                "document_type": row.get("docty"),
                "report_number": row.get("repnb"),
                "country": row.get("count"),
                "pdf_url": row.get("pdfurl"),
                "abstract_available": bool(row.get("abstracts")),
                "rights_decision_sha256": rights["decision_sha256"],
            },
            "locator": None,
            "supersedes": None,
            "run_id": None,
            "tool_call_id": None,
        }
        metadata_hash = _metadata_hash(item)
        item["metadata_sha256"] = metadata_hash
        item["research_item_id"] = f"RDI-worldbank-document-{source_id}-{metadata_hash[:12]}"
        return item

    def _normalize_indicator(
        self,
        row: dict[str, Any],
        indicator: str,
        capture: HttpCapture,
        raw: dict[str, str],
    ) -> dict[str, Any]:
        country = str(row.get("countryiso3code") or row.get("country", {}).get("id") or "")
        period = str(row.get("date", ""))
        source_id = f"{country}/{indicator}/{period}"
        published_at = capture.captured_at
        rights = self.rights_gate.evaluate(
            self.provider_id, "OFFICIAL_STATISTIC", "STORE_STRUCTURED_DATA"
        )
        observation = {
            "country": country,
            "indicator": indicator,
            "period": period,
            "value": row.get("value"),
            "unit": row.get("unit", ""),
            "obs_status": row.get("obs_status", ""),
            "decimal": row.get("decimal"),
        }
        item: dict[str, Any] = {
            "research_item_id": "",
            "provider_id": self.provider_id,
            "provider_tier": "OFFICIAL_PRIMARY",
            "content_type": "OFFICIAL_STATISTIC",
            "source_document_id": source_id,
            "source_url": capture.request_url,
            "title": f"{row.get('indicator', {}).get('value', indicator)} — {row.get('country', {}).get('value', country)} — {period}",
            "publisher": "World Bank",
            "authors": [],
            "language": "en",
            "published_at": published_at,
            "captured_at": capture.captured_at,
            "effective_at": None,
            "as_of": None,
            "observation_period": period,
            "frequency": "ANNUAL",
            "unit": str(row.get("unit", "") or "PROVIDER_DEFINED"),
            "adjustment": None,
            "document_version": f"captured-{capture.captured_at}",
            "content_sha256": sha256_json(observation),
            "raw_response_sha256": raw["sha256"],
            "raw_response_path": raw["path"],
            "metadata_sha256": "",
            "rights_state": rights["rights_state"],
            "storage_policy": rights["storage_policy"],
            "citation_text": f"World Bank Indicator {indicator}, {country}, {period}, captured {capture.captured_at}",
            "parser_version": PARSER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "adapter_version": f"worldbank-indicator-adapter@{ADAPTER_VERSION}",
            "retrieval_method": "REST_GET",
            "quality_status": "VERIFIED_CONTENT",
            "point_in_time_safe": False,
            "entities": [country, indicator],
            "topics": ["official statistic", "macro", "World Bank indicator"],
            "attributes": {
                **observation,
                "source_note": row.get("sourceNote"),
                "point_in_time_limitation": "API observation does not expose original release timestamp or vintage; safe for current context only.",
                "rights_decision_sha256": rights["decision_sha256"],
            },
            "locator": {"page": None, "paragraph": None, "table": None, "field_path": "response[1][].value"},
            "supersedes": None,
            "run_id": None,
            "tool_call_id": None,
        }
        metadata_hash = _metadata_hash(item)
        item["metadata_sha256"] = metadata_hash
        safe_source_id = source_id.replace("/", "-")
        item["research_item_id"] = f"RDI-worldbank-stat-{safe_source_id}-{metadata_hash[:12]}"
        return item

