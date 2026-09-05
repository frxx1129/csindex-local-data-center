from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from csindex_local.models import (
    CrawlScope,
    IndexRecord,
    RawResponse,
    RunProgress,
    VolatilitySnapshot,
    YieldSnapshot,
)


_METRIC_COLUMNS = (
    "one_month",
    "three_month",
    "year_to_date",
    "one_year",
    "three_year",
    "five_year",
    "one_year_volatility",
    "three_year_volatility",
    "five_year_volatility",
)
_MISSING_COUNT_SQL = " + ".join(f"({column} IS NULL)" for column in _METRIC_COLUMNS)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS indices (
                    index_code TEXT PRIMARY KEY,
                    index_name TEXT NOT NULL,
                    if_tracked TEXT,
                    index_series TEXT,
                    index_classify TEXT,
                    assets_classify TEXT,
                    publish_date TEXT,
                    list_order INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawl_scopes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawl_scope_members (
                    scope_id TEXT NOT NULL,
                    index_code TEXT NOT NULL,
                    member_order INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (scope_id, index_code),
                    FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                    FOREIGN KEY (index_code) REFERENCES indices(index_code)
                );

                CREATE TABLE IF NOT EXISTS metric_snapshots (
                    index_code TEXT NOT NULL,
                    data_date TEXT NOT NULL,
                    one_month REAL NULL,
                    three_month REAL NULL,
                    year_to_date REAL NULL,
                    one_year REAL NULL,
                    three_year REAL NULL,
                    five_year REAL NULL,
                    one_year_volatility REAL NULL,
                    three_year_volatility REAL NULL,
                    five_year_volatility REAL NULL,
                    yield_fetched_at TEXT NULL,
                    volatility_fetched_at TEXT NULL,
                    missing_count INTEGER NOT NULL DEFAULT 9,
                    is_complete INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (index_code, data_date),
                    FOREIGN KEY (index_code) REFERENCES indices(index_code)
                );

                CREATE TABLE IF NOT EXISTS raw_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    index_code TEXT,
                    endpoint TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    data_date TEXT,
                    payload TEXT,
                    fetched_at TEXT NOT NULL,
                    is_success INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS crawl_runs (
                    id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_value TEXT NOT NULL,
                    target_data_date TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    total_tasks INTEGER NOT NULL,
                    success_tasks INTEGER NOT NULL DEFAULT 0,
                    failed_tasks INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS crawl_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    index_code TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    target_data_date TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'running', 'success', 'retry_wait',
                            'blocked_wait', 'failed', 'cancelled'
                        )
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_http_status INTEGER,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, index_code, endpoint),
                    FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                    FOREIGN KEY (index_code) REFERENCES indices(index_code)
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            raw_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(raw_responses)")
            }
            if "is_success" not in raw_columns:
                connection.execute(
                    "ALTER TABLE raw_responses "
                    "ADD COLUMN is_success INTEGER NOT NULL DEFAULT 0"
                )

    def record_raw_response(self, response: RawResponse) -> None:
        """Persist diagnostics and cap only successful rows per response stream."""

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO raw_responses (
                    index_code, endpoint, http_status, data_date, payload,
                    fetched_at, is_success
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response.index_code,
                    response.endpoint,
                    response.http_status,
                    response.data_date,
                    response.payload,
                    response.fetched_at,
                    int(response.is_success),
                ),
            )
            if not response.is_success:
                return
            if response.index_code is None:
                stream_predicate = "index_code IS NULL"
                parameters: tuple[object, ...] = (response.endpoint,)
            else:
                stream_predicate = "index_code = ?"
                parameters = (response.endpoint, response.index_code)
            connection.execute(
                f"""
                DELETE FROM raw_responses
                WHERE id IN (
                    SELECT id FROM raw_responses
                    WHERE endpoint = ? AND {stream_predicate} AND is_success = 1
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT -1 OFFSET 2
                )
                """,
                parameters,
            )

    def upsert_indices(self, items: list[IndexRecord]) -> None:
        now = _utc_now()
        with self._connection() as connection:
            for list_order, item in enumerate(items):
                raw = item.raw
                connection.execute(
                    """
                    INSERT INTO indices (
                        index_code, index_name, if_tracked, index_series,
                        index_classify, assets_classify, publish_date, list_order,
                        is_active, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(index_code) DO UPDATE SET
                        index_name=excluded.index_name,
                        if_tracked=excluded.if_tracked,
                        index_series=excluded.index_series,
                        index_classify=excluded.index_classify,
                        assets_classify=excluded.assets_classify,
                        publish_date=excluded.publish_date,
                        list_order=excluded.list_order,
                        is_active=1,
                        raw_json=excluded.raw_json,
                        synced_at=excluded.synced_at
                    """,
                    (
                        item.index_code,
                        item.index_name,
                        item.if_tracked,
                        raw.get("indexSeries"),
                        raw.get("indexClassify"),
                        raw.get("assetsClassify"),
                        raw.get("publishDate"),
                        list_order,
                        json.dumps(raw, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )

    def upsert_yield(self, snapshot: YieldSnapshot) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO metric_snapshots (
                    index_code, data_date, one_month, three_month, year_to_date,
                    one_year, three_year, five_year, yield_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_code, data_date) DO UPDATE SET
                    one_month=excluded.one_month,
                    three_month=excluded.three_month,
                    year_to_date=excluded.year_to_date,
                    one_year=excluded.one_year,
                    three_year=excluded.three_year,
                    five_year=excluded.five_year,
                    yield_fetched_at=excluded.yield_fetched_at
                """,
                (
                    snapshot.index_code,
                    snapshot.data_date,
                    snapshot.one_month,
                    snapshot.three_month,
                    snapshot.year_to_date,
                    snapshot.one_year,
                    snapshot.three_year,
                    snapshot.five_year,
                    _utc_now(),
                ),
            )
            self._refresh_completeness(connection, snapshot.index_code, snapshot.data_date)

    def merge_volatility(self, snapshot: VolatilitySnapshot) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO metric_snapshots (
                    index_code, data_date, one_year_volatility,
                    three_year_volatility, five_year_volatility, volatility_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_code, data_date) DO UPDATE SET
                    one_year_volatility=excluded.one_year_volatility,
                    three_year_volatility=excluded.three_year_volatility,
                    five_year_volatility=excluded.five_year_volatility,
                    volatility_fetched_at=excluded.volatility_fetched_at
                """,
                (
                    snapshot.index_code,
                    snapshot.data_date,
                    snapshot.one_year,
                    snapshot.three_year,
                    snapshot.five_year,
                    _utc_now(),
                ),
            )
            self._refresh_completeness(connection, snapshot.index_code, snapshot.data_date)

    def get_snapshot(self, index_code: str, data_date: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT * FROM metric_snapshots
                WHERE index_code = ? AND data_date = ?
                """,
                (index_code, data_date),
            ).fetchone()

    def list_snapshots(self, index_code: str) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT * FROM metric_snapshots
                WHERE index_code = ?
                ORDER BY data_date
                """,
                (index_code,),
            ).fetchall()

    def get_index(self, index_code: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                "SELECT * FROM indices WHERE index_code = ?", (index_code,)
            ).fetchone()

    def create_or_get_scope(
        self, scope_id: str, codes: list[str], regenerate: bool = False
    ) -> CrawlScope:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._load_scope(connection, scope_id)
            if existing is not None and not regenerate:
                return existing

            scope_type, separator, scope_value = scope_id.partition(":")
            if not separator:
                scope_value = scope_id
            now = _utc_now()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO crawl_scopes (
                        id, name, scope_type, scope_value, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (scope_id, scope_id, scope_type, scope_value, now, now),
                )
            else:
                connection.execute(
                    "UPDATE crawl_scopes SET updated_at = ? WHERE id = ?",
                    (now, scope_id),
                )
                connection.execute(
                    "DELETE FROM crawl_scope_members WHERE scope_id = ?", (scope_id,)
                )
            connection.executemany(
                """
                INSERT INTO crawl_scope_members (
                    scope_id, index_code, member_order, added_at
                ) VALUES (?, ?, ?, ?)
                """,
                [(scope_id, code, member_order, now) for member_order, code in enumerate(codes)],
            )
            return CrawlScope(scope_id, scope_id, scope_type, scope_value, tuple(codes))

    def get_runtime_state(self, key: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            raise ValueError(f"runtime state {key!r} must contain a JSON object")
        return value

    def set_runtime_state(self, key: str, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, _utc_now()),
            )

    def delete_runtime_state(self, key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM runtime_state WHERE key = ?", (key,))

    def update_run_state(
        self, run_id: str, status: str, finished_at: str | None
    ) -> RunProgress:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_tasks,
                    SUM(status = 'success') AS success_tasks,
                    SUM(status = 'failed') AS failed_tasks,
                    SUM(status IN (
                        'pending', 'running', 'retry_wait', 'blocked_wait'
                    )) AS pending_tasks
                FROM crawl_tasks WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if counts is None:
                raise KeyError(run_id)
            progress = RunProgress(
                total_tasks=counts["total_tasks"],
                success_tasks=counts["success_tasks"] or 0,
                failed_tasks=counts["failed_tasks"] or 0,
                pending_tasks=counts["pending_tasks"] or 0,
            )
            updated = connection.execute(
                """
                UPDATE crawl_runs
                SET status = ?, finished_at = ?, total_tasks = ?,
                    success_tasks = ?, failed_tasks = ?
                WHERE id = ?
                """,
                (
                    status,
                    finished_at,
                    progress.total_tasks,
                    progress.success_tasks,
                    progress.failed_tasks,
                    run_id,
                ),
            )
            if not updated.rowcount:
                raise KeyError(run_id)
            return progress

    def recover_interrupted_tasks(self) -> int:
        with self._connection() as connection:
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'pending', available_at = ?, updated_at = ?
                WHERE status = 'running'
                """,
                (now, now),
            )
            return cursor.rowcount

    def _load_scope(
        self, connection: sqlite3.Connection, scope_id: str
    ) -> CrawlScope | None:
        scope = connection.execute(
            "SELECT * FROM crawl_scopes WHERE id = ?", (scope_id,)
        ).fetchone()
        if scope is None:
            return None
        codes = tuple(
            row["index_code"]
            for row in connection.execute(
                """
                SELECT index_code FROM crawl_scope_members
                WHERE scope_id = ?
                ORDER BY member_order
                """,
                (scope_id,),
            )
        )
        return CrawlScope(
            scope["id"],
            scope["name"],
            scope["scope_type"],
            scope["scope_value"],
            codes,
        )

    @staticmethod
    def _refresh_completeness(
        connection: sqlite3.Connection, index_code: str, data_date: str
    ) -> None:
        connection.execute(
            f"""
            UPDATE metric_snapshots
            SET missing_count = {_MISSING_COUNT_SQL},
                is_complete = CASE WHEN ({_MISSING_COUNT_SQL}) = 0 THEN 1 ELSE 0 END
            WHERE index_code = ? AND data_date = ?
            """,
            (index_code, data_date),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
