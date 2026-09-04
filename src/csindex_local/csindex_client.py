"""Strict, retry-free HTTP client for the China Securities Index website."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import IndexRecord, VolatilitySnapshot, YieldSnapshot


DEFAULT_BASE_URL = "https://www.csindex.com.cn/csindex-home"
SITE_ORIGIN = "https://www.csindex.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class CsindexClientError(RuntimeError):
    """Base class for errors whose handling is decided by the crawler layer."""


class BlockedError(CsindexClientError):
    """The site rejected the request with a WAF or access-block page."""


class NotFoundError(CsindexClientError):
    """The requested index or business record does not exist."""


class NetworkError(CsindexClientError):
    """The request could not obtain a usable HTTP response."""


class ResponseFormatError(CsindexClientError):
    """An HTTP success response did not match the documented JSON contract."""


def parse_number(value: object) -> float | None:
    """Parse a CSIndex percentage value without inventing values for blanks."""
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in {"", "--", "-", "null", "None"}:
        return None
    return float(text)


class CsindexClient:
    """Fetch and parse CSIndex list, yield, and volatility endpoints.

    This class deliberately contains no retry, rate limiting, or persistence policy.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_seconds: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch_index_list(self) -> list[IndexRecord]:
        rows: list[IndexRecord] = []
        page_num = 1
        total: int | None = None

        while total is None or len(rows) < total:
            payload = self._post_json(
                "/index-list/query-index-item", self._index_list_payload(page_num)
            )
            page_total, page_rows = self._parse_index_list_page(payload)
            if total is None:
                total = page_total
            elif page_total != total:
                raise ResponseFormatError("index list total changed during pagination")

            if not page_rows:
                break
            rows.extend(page_rows)
            page_num += 1

        return rows[:total] if total is not None else rows

    def fetch_yield(self, code: str) -> YieldSnapshot:
        safe_code = self._validate_code(code)
        payload = self._get_json(f"/perf/get-index-yield-item/{safe_code}", code)
        return self.parse_yield(payload)

    def fetch_volatility(self, code: str, data_date: str) -> VolatilitySnapshot:
        safe_code = self._validate_code(code)
        if not isinstance(data_date, str) or not data_date:
            raise ValueError("data_date must be a non-empty string")
        payload = self._get_json(
            f"/perf/get-index-yield-item-nianHua/{safe_code}", code
        )
        return self.parse_volatility(payload, code, data_date)

    @staticmethod
    def parse_yield(payload: object) -> YieldSnapshot:
        data = CsindexClient._success_data(payload)
        return YieldSnapshot(
            index_code=CsindexClient._required_string(data, "indexCode"),
            data_date=CsindexClient._required_string(data, "endDate"),
            one_month=CsindexClient._metric(data, "oneMonth"),
            three_month=CsindexClient._metric(data, "threeMonth"),
            year_to_date=CsindexClient._metric(data, "thisYear"),
            one_year=CsindexClient._metric(data, "oneYear"),
            three_year=CsindexClient._metric(data, "threeYear"),
            five_year=CsindexClient._metric(data, "fiveYear"),
        )

    @staticmethod
    def parse_volatility(
        payload: object, code: str, data_date: str
    ) -> VolatilitySnapshot:
        data = CsindexClient._success_data(payload)
        return VolatilitySnapshot(
            index_code=code,
            data_date=data_date,
            one_year=CsindexClient._metric(data, "oneYearNianHua"),
            three_year=CsindexClient._metric(data, "threeYearNianHua"),
            five_year=CsindexClient._metric(data, "fiveYearNianHua"),
        )

    @staticmethod
    def classify_http_error(status: int, body: bytes) -> None:
        if status == 403:
            raise BlockedError("CSIndex returned HTTP 403")
        if status == 404:
            text = body.decode("utf-8", errors="replace").strip().lower()
            looks_like_text = not text.startswith(("{", "["))
            waf_markers = ("您的访问被阻断", "访问被阻断", "aliyun", "waf")
            if looks_like_text and any(marker in text for marker in waf_markers):
                raise BlockedError("CSIndex returned a WAF block page")
            raise NotFoundError("CSIndex returned HTTP 404")
        raise NetworkError(f"CSIndex returned HTTP {status}")

    def _get_json(self, path: str, code: str) -> dict[str, Any]:
        request = Request(
            self._url(path),
            headers=self._detail_headers(code),
            method="GET",
        )
        return self._request_json(request)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self._url(path),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                **self._common_headers(),
                "Referer": f"{SITE_ORIGIN}/zh-CN/downloads/index-information",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._request_json(request)

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except HTTPError as error:
            self.classify_http_error(error.code, error.read())
            raise AssertionError("HTTP error classification must raise")
        except (URLError, TimeoutError, OSError) as error:
            raise NetworkError("CSIndex request failed") from error

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResponseFormatError("CSIndex response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ResponseFormatError("CSIndex JSON response must be an object")
        return payload

    @staticmethod
    def _index_list_payload(page_num: int) -> dict[str, Any]:
        return {
            "sorter": {"sortField": None, "sortOrder": None},
            "pager": {"pageNum": page_num, "pageSize": 100},
            "searchInput": None,
            "indexFilter": {
                "index_series": None,
                "index_classify": None,
                "market_coverage": None,
                "hot_spot": None,
                "currency": None,
                "region": None,
            },
        }

    @staticmethod
    def _parse_index_list_page(payload: object) -> tuple[int, list[IndexRecord]]:
        if not isinstance(payload, dict):
            raise ResponseFormatError("index list response must be an object")
        total = payload.get("total")
        data = payload.get("data")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ResponseFormatError("index list response has an invalid total")
        if not isinstance(data, list):
            raise ResponseFormatError("index list response has invalid data")

        rows: list[IndexRecord] = []
        for item in data:
            if not isinstance(item, dict):
                raise ResponseFormatError("index list item must be an object")
            rows.append(
                IndexRecord(
                    index_code=CsindexClient._required_string(item, "indexCode"),
                    index_name=CsindexClient._required_string(item, "indexName"),
                    if_tracked=CsindexClient._optional_string(item, "ifTracked"),
                    raw=item,
                )
            )
        return total, rows

    @staticmethod
    def _success_data(payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("code") != "200":
            raise ResponseFormatError("CSIndex response code is not 200")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ResponseFormatError("CSIndex response has invalid data")
        return data

    @staticmethod
    def _metric(data: dict[str, Any], key: str) -> float | None:
        try:
            return parse_number(data.get(key))
        except (TypeError, ValueError) as error:
            raise ResponseFormatError(f"invalid number for {key}") from error

    @staticmethod
    def _required_string(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ResponseFormatError(f"missing or invalid {key}")
        return value

    @staticmethod
    def _optional_string(data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ResponseFormatError(f"invalid {key}")
        return value

    @staticmethod
    def _validate_code(code: str) -> str:
        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        return quote(code, safe="")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _common_headers() -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": SITE_ORIGIN,
        }

    @classmethod
    def _detail_headers(cls, code: str) -> dict[str, str]:
        return {
            **cls._common_headers(),
            "Referer": f"{SITE_ORIGIN}/zh-CN/indices/index-detail/{quote(code, safe='')}",
        }
