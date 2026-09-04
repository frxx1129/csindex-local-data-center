import json
from pathlib import Path
from urllib.error import URLError

import pytest

from csindex_local.csindex_client import (
    BlockedError,
    CsindexClient,
    NetworkError,
    NotFoundError,
    ResponseFormatError,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_yield_preserves_code_and_nulls():
    """A parser that coerces codes or treats -- as a number corrupts snapshots."""
    payload = json.loads((FIXTURES / "yield_ok.json").read_text(encoding="utf-8"))

    row = CsindexClient.parse_yield(payload)

    assert row.index_code == "000300"
    assert row.data_date == "2026-09-03"
    assert row.one_month == 1.25
    assert row.three_month is None


def test_parse_volatility_accepts_percentages_and_nulls():
    """A parser that leaves percent strings unnormalised cannot populate SQLite."""
    payload = json.loads(
        (FIXTURES / "volatility_ok.json").read_text(encoding="utf-8")
    )

    row = CsindexClient.parse_volatility(payload, "000300", "2026-09-03")

    assert row.index_code == "000300"
    assert row.data_date == "2026-09-03"
    assert row.one_year == 12.5
    assert row.three_year is None
    assert row.five_year == 8765.4


def test_html_block_page_is_blocked():
    """A WAF HTML 404 must not be mistaken for a missing business record."""
    with pytest.raises(BlockedError):
        CsindexClient.classify_http_error(
            404,
            b"<html>\xe6\x82\xa8\xe7\x9a\x84\xe8\xae\xbf\xe9\x97\xae\xe8\xa2\xab\xe9\x98\xbb\xe6\x96\xad</html>",
        )


def test_plain_business_404_is_not_found():
    """Only a non-WAF 404 represents a missing index or date."""
    with pytest.raises(NotFoundError):
        CsindexClient.classify_http_error(404, b'{"message":"not found"}')


def test_fetch_index_list_posts_paginated_request_and_preserves_raw(monkeypatch):
    """A broken pager would silently omit indices after the first page."""
    responses = [
        {"total": 2, "data": [{"indexCode": "000300", "indexName": "沪深300", "ifTracked": "是", "extra": "first"}]},
        {"total": 2, "data": [{"indexCode": "000905", "indexName": "中证500", "ifTracked": None}]},
    ]
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        payload = responses.pop(0)
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("csindex_local.csindex_client.urlopen", fake_urlopen)
    client = CsindexClient(base_url="https://example.test/csindex-home")

    rows = client.fetch_index_list()

    assert [row.index_code for row in rows] == ["000300", "000905"]
    assert rows[0].raw["extra"] == "first"
    assert len(requests) == 2
    assert requests[0].full_url == (
        "https://example.test/csindex-home/index-list/query-index-item"
    )
    assert json.loads(requests[0].data) == {
        "sorter": {"sortField": None, "sortOrder": None},
        "pager": {"pageNum": 1, "pageSize": 100},
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
    assert requests[0].get_header("Referer") == (
        "https://www.csindex.com.cn/zh-CN/downloads/index-information"
    )


def test_fetch_index_list_rejects_empty_page_before_reported_total(monkeypatch):
    """An empty middle page means the reported index list is incomplete."""
    responses = [
        {
            "total": 2,
            "data": [
                {
                    "indexCode": "000300",
                    "indexName": "沪深300",
                    "ifTracked": "是",
                }
            ],
        },
        {"total": 2, "data": []},
    ]

    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(
            json.dumps(responses.pop(0)).encode("utf-8")
        ),
    )

    with pytest.raises(ResponseFormatError, match="ended before total"):
        CsindexClient(base_url="https://example.test").fetch_index_list()


def test_fetch_index_list_rejects_more_rows_than_reported_total(monkeypatch):
    """Truncating an overfull page would hide an inconsistent server response."""
    payload = {
        "total": 1,
        "data": [
            {"indexCode": "000300", "indexName": "沪深300", "ifTracked": "是"},
            {"indexCode": "000905", "indexName": "中证500", "ifTracked": "是"},
        ],
    }
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8")),
    )

    with pytest.raises(ResponseFormatError, match="more rows than total"):
        CsindexClient(base_url="https://example.test").fetch_index_list()


def test_fetch_yield_uses_code_specific_endpoint_and_referer(monkeypatch):
    """A generic Referer or wrong endpoint can trigger an avoidable WAF block."""
    payload = (FIXTURES / "yield_ok.json").read_bytes()
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse(payload)

    monkeypatch.setattr("csindex_local.csindex_client.urlopen", fake_urlopen)
    client = CsindexClient(base_url="https://example.test/csindex-home")

    row = client.fetch_yield("000300")

    assert row.index_code == "000300"
    assert requests[0].full_url.endswith("/perf/get-index-yield-item/000300")
    assert requests[0].get_header("Referer") == (
        "https://www.csindex.com.cn/zh-CN/indices/index-detail/000300"
    )


def test_fetch_volatility_supplies_code_and_date_absent_from_response(monkeypatch):
    """Volatility responses omit identity fields, so the call arguments define them."""
    payload = (FIXTURES / "volatility_ok.json").read_bytes()

    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(payload),
    )
    client = CsindexClient(base_url="https://example.test/csindex-home")

    row = client.fetch_volatility("000300", "2026-09-03")

    assert row.index_code == "000300"
    assert row.data_date == "2026-09-03"


def test_success_http_with_bad_business_payload_is_response_format_error(monkeypatch):
    """A 200 error envelope must not become an empty valid snapshot."""
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(b'{"code":"500","data":{}}'),
    )

    with pytest.raises(ResponseFormatError):
        CsindexClient(base_url="https://example.test").fetch_yield("000300")


def test_url_error_is_network_error(monkeypatch):
    """Transport errors differ from both WAF blocks and malformed server payloads."""
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(URLError("offline")),
    )

    with pytest.raises(NetworkError):
        CsindexClient(base_url="https://example.test").fetch_yield("000300")


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
