from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError

import pytest

from csindex_local.csindex_client import CsindexClient, NotFoundError, ResponseFormatError
from csindex_local.db import Database
from csindex_local.models import RawResponse


def raw(
    payload: str,
    *,
    success: bool,
    fetched_at: str,
    index_code: str | None = "000300",
    endpoint: str = "yield",
    status: int = 200,
) -> RawResponse:
    return RawResponse(
        index_code=index_code,
        endpoint=endpoint,
        http_status=status,
        data_date="2026-09-03" if success else None,
        payload=payload,
        fetched_at=fetched_at,
        is_success=success,
    )


def rows(database: Database):
    with database._connection() as connection:
        return connection.execute(
            "SELECT * FROM raw_responses ORDER BY id"
        ).fetchall()


def test_success_retention_keeps_latest_two_and_all_failures(tmp_path: Path) -> None:
    database = Database(tmp_path / "raw.db")
    database.initialize()
    database.record_raw_response(raw("success-1", success=True, fetched_at="2026-09-01T00:00:00+00:00"))
    database.record_raw_response(raw("bad-json", success=False, fetched_at="2026-09-01T00:00:01+00:00"))
    database.record_raw_response(raw("success-2", success=True, fetched_at="2026-09-01T00:00:02+00:00"))
    database.record_raw_response(raw("http-404", success=False, status=404, fetched_at="2026-09-01T00:00:03+00:00"))
    database.record_raw_response(raw("success-3", success=True, fetched_at="2026-09-01T00:00:04+00:00"))

    saved = rows(database)
    assert [row["payload"] for row in saved if row["is_success"]] == ["success-2", "success-3"]
    assert [row["payload"] for row in saved if not row["is_success"]] == ["bad-json", "http-404"]


def test_nullable_list_stream_prunes_successes(tmp_path: Path) -> None:
    database = Database(tmp_path / "raw.db")
    database.initialize()
    for number in range(3):
        database.record_raw_response(
            raw(str(number), success=True, fetched_at=f"2026-09-01T00:00:0{number}+00:00", index_code=None, endpoint="index_list")
        )
    assert [row["payload"] for row in rows(database)] == ["1", "2"]


def test_initialize_migrates_existing_raw_response_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE raw_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                index_code TEXT,
                endpoint TEXT NOT NULL,
                http_status INTEGER NOT NULL,
                data_date TEXT,
                payload TEXT,
                fetched_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO raw_responses(index_code, endpoint, http_status, payload, fetched_at) VALUES(NULL, 'index_list', 500, 'legacy', '2026-09-01')"
        )

    database = Database(path)
    database.initialize()

    with database._connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(raw_responses)")}
        saved = connection.execute("SELECT payload, is_success FROM raw_responses").fetchone()
    assert "is_success" in columns
    assert tuple(saved) == ("legacy", 0)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def test_client_records_malformed_200_as_failure(monkeypatch) -> None:
    observed: list[RawResponse] = []
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(b"{not-json"),
    )
    client = CsindexClient("https://example.test", response_observer=observed.append)

    with pytest.raises(ResponseFormatError):
        client.fetch_yield("000300")

    assert len(observed) == 1
    assert observed[0].http_status == 200
    assert observed[0].payload == "{not-json"
    assert observed[0].is_success is False


def test_client_records_schema_invalid_200_as_failure(monkeypatch) -> None:
    observed: list[RawResponse] = []
    body = b'{"code":"500","data":{}}'
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(body),
    )
    client = CsindexClient("https://example.test", response_observer=observed.append)

    with pytest.raises(ResponseFormatError):
        client.fetch_yield("000300")

    assert observed[0].payload == body.decode()
    assert observed[0].is_success is False


def test_client_records_http_error_body_before_classification(monkeypatch) -> None:
    observed: list[RawResponse] = []
    body = b'{"message":"missing"}'

    def fail(request, timeout):
        raise HTTPError(request.full_url, 404, "missing", {}, FakeResponse(body))

    monkeypatch.setattr("csindex_local.csindex_client.urlopen", fail)
    client = CsindexClient("https://example.test", response_observer=observed.append)

    with pytest.raises(NotFoundError):
        client.fetch_yield("000300")

    assert (observed[0].http_status, observed[0].payload, observed[0].is_success) == (404, body.decode(), False)


def test_response_observer_failure_propagates(monkeypatch) -> None:
    body = json.dumps(
        {"code": "200", "data": {"indexCode": "000300", "endDate": "2026-09-03"}}
    ).encode()
    monkeypatch.setattr(
        "csindex_local.csindex_client.urlopen",
        lambda request, timeout: FakeResponse(body),
    )

    def broken(_record: RawResponse) -> None:
        raise sqlite3.OperationalError("disk full")

    client = CsindexClient("https://example.test", response_observer=broken)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        client.fetch_yield("000300")


def test_response_observer_failure_stops_before_next_http_request(monkeypatch) -> None:
    request_count = 0
    body = json.dumps(
        {
            "total": 2,
            "data": [
                {"indexCode": "000001", "indexName": "测试指数", "ifTracked": "是"}
            ],
        }
    ).encode()

    def respond(request, timeout):
        nonlocal request_count
        request_count += 1
        return FakeResponse(body)

    monkeypatch.setattr("csindex_local.csindex_client.urlopen", respond)

    def broken(_record: RawResponse) -> None:
        raise sqlite3.OperationalError("disk full")

    client = CsindexClient("https://example.test", response_observer=broken)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        client.fetch_index_list()
    assert request_count == 1
