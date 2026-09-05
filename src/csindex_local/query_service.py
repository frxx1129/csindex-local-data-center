"""Read-only local queries for the latest snapshot in a crawl scope."""

from __future__ import annotations

from collections.abc import Iterable

from .db import Database
from .models import MetricRow


METRIC_FIELDS = (
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


class QueryService:
    """Query persisted snapshots without changing the local database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def latest_rows(self, scope_id: str) -> list[MetricRow]:
        """Return one newest snapshot per member, retaining frozen scope order."""

        with self._database._connection() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT
                        snapshots.index_code,
                        snapshots.data_date,
                        snapshots.one_month,
                        snapshots.three_month,
                        snapshots.year_to_date,
                        snapshots.one_year,
                        snapshots.three_year,
                        snapshots.five_year,
                        snapshots.one_year_volatility,
                        snapshots.three_year_volatility,
                        snapshots.five_year_volatility,
                        snapshots.missing_count,
                        snapshots.is_complete,
                        ROW_NUMBER() OVER (
                            PARTITION BY members.index_code
                            ORDER BY snapshots.data_date DESC
                        ) AS snapshot_order
                    FROM crawl_scope_members AS members
                    JOIN metric_snapshots AS snapshots
                        ON snapshots.index_code = members.index_code
                    WHERE members.scope_id = ?
                )
                SELECT
                    members.index_code,
                    indices.index_name,
                    latest.data_date,
                    latest.one_month,
                    latest.three_month,
                    latest.year_to_date,
                    latest.one_year,
                    latest.three_year,
                    latest.five_year,
                    latest.one_year_volatility,
                    latest.three_year_volatility,
                    latest.five_year_volatility,
                    latest.missing_count,
                    latest.is_complete
                FROM crawl_scope_members AS members
                JOIN indices ON indices.index_code = members.index_code
                LEFT JOIN latest
                    ON members.index_code = latest.index_code
                    AND latest.snapshot_order = 1
                WHERE members.scope_id = ?
                ORDER BY members.member_order
                """,
                (scope_id, scope_id),
            ).fetchall()
        return [self._metric_row(row) for row in rows]

    def rank(
        self, rows: Iterable[MetricRow], field: str, limit: int | None
    ) -> list[MetricRow]:
        """Rank non-null numeric values in strict descending order."""

        if field not in METRIC_FIELDS:
            raise ValueError(f"unsupported ranking field: {field}")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
        ):
            raise ValueError("ranking limit must be a non-negative integer or None")

        ranked = [row for row in rows if getattr(row, field) is not None]
        ranked.sort(key=lambda row: getattr(row, field), reverse=True)
        return ranked if limit is None else ranked[:limit]

    def exception_rows(self, scope_id: str, rows: Iterable[MetricRow]) -> list[tuple]:
        """Provide export-only diagnostics for missing data and failed requests."""

        latest = list(rows)
        result: list[tuple] = []
        for row in latest:
            if row.data_date == "--":
                result.append(
                    (
                        row.index_code,
                        row.index_name,
                        None,
                        "无本地快照",
                        (),
                        9,
                        "尚未获得可导出的本地指标快照。",
                    )
                )
                continue
            missing = [field for field in METRIC_FIELDS if getattr(row, field) is None]
            if missing:
                result.append(
                    (
                        row.index_code,
                        row.index_name,
                        row.data_date,
                        "官网缺失指标",
                        tuple(missing),
                        row.missing_count,
                        "官网接口空值按 NULL 保存，导出显示为 --。",
                    )
                )

        with self._database._connection() as connection:
            failed = connection.execute(
                """
                SELECT DISTINCT
                    tasks.index_code,
                    indices.index_name,
                    tasks.target_data_date,
                    tasks.last_error
                FROM crawl_tasks AS tasks
                JOIN crawl_scope_members AS members
                    ON members.index_code = tasks.index_code
                JOIN indices ON indices.index_code = tasks.index_code
                JOIN crawl_scopes AS scopes ON scopes.id = members.scope_id
                JOIN crawl_runs AS runs ON runs.id = tasks.run_id
                WHERE members.scope_id = ? AND tasks.status = 'failed'
                  AND runs.scope_type || ':' || runs.scope_value = scopes.id
                ORDER BY tasks.id DESC
                """,
                (scope_id,),
            ).fetchall()

        for row in failed:
            message = row["last_error"] or "请求失败"
            kind = "数据日期不一致" if "data date mismatch" in message else "请求失败"
            result.append(
                (
                    row["index_code"],
                    row["index_name"],
                    row["target_data_date"],
                    kind,
                    (),
                    None,
                    message,
                )
            )
        return result

    @staticmethod
    def _metric_row(row: object) -> MetricRow:
        return MetricRow(
            index_code=row["index_code"],
            index_name=row["index_name"],
            data_date=row["data_date"] or "--",
            one_month=row["one_month"],
            three_month=row["three_month"],
            year_to_date=row["year_to_date"],
            one_year=row["one_year"],
            three_year=row["three_year"],
            five_year=row["five_year"],
            one_year_volatility=row["one_year_volatility"],
            three_year_volatility=row["three_year_volatility"],
            five_year_volatility=row["five_year_volatility"],
            missing_count=int(row["missing_count"] if row["missing_count"] is not None else 9),
            is_complete=bool(row["is_complete"]),
        )
