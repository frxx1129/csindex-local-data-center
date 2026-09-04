from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable
from uuid import uuid4

from csindex_local.db import Database
from csindex_local.models import CrawlTask, RunProgress


_CLAIMABLE_STATUSES = ("pending", "retry_wait", "blocked_wait")


class TaskQueue:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] | object | None = None,
    ):
        self._database = database
        self._clock = clock

    def create_run(
        self,
        scope_type: str,
        scope_value: str,
        codes: Iterable[str],
        target_date: str | None,
    ) -> str:
        run_id = str(uuid4())
        unique_codes = tuple(dict.fromkeys(codes))
        now = self._now()
        with self._database._connection() as connection:
            connection.execute(
                """
                INSERT INTO crawl_runs (
                    id, scope_type, scope_value, target_data_date, status,
                    started_at, total_tasks
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, scope_type, scope_value, target_date, now, len(unique_codes) * 2),
            )
            connection.executemany(
                """
                INSERT INTO crawl_tasks (
                    run_id, index_code, endpoint, target_data_date, status,
                    available_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (run_id, code, endpoint, target_date, now, now)
                    for code in unique_codes
                    for endpoint in ("yield", "volatility")
                ],
            )
        return run_id

    def claim_next(self, run_id: str) -> CrawlTask | None:
        now = self._now()
        with self._database._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT * FROM crawl_tasks
                WHERE run_id = ?
                  AND status IN ('pending', 'retry_wait', 'blocked_wait')
                  AND available_at <= ?
                ORDER BY CASE endpoint WHEN 'yield' THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
            if task is None:
                return None
            connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, task["id"]),
            )
            task = connection.execute(
                "SELECT * FROM crawl_tasks WHERE id = ?", (task["id"],)
            ).fetchone()
        return _to_task(task)

    def mark_success(self, task_id: int) -> None:
        with self._database._connection() as connection:
            updated = connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'success', updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (self._now(), task_id),
            )
            if updated.rowcount:
                connection.execute(
                    """
                    UPDATE crawl_runs
                    SET success_tasks = success_tasks + 1
                    WHERE id = (SELECT run_id FROM crawl_tasks WHERE id = ?)
                    """,
                    (task_id,),
                )

    def mark_retry(self, task_id: int, available_at: datetime | str, error: str) -> None:
        with self._database._connection() as connection:
            connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'retry_wait', available_at = ?, last_error = ?,
                    last_http_status = NULL, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (_as_timestamp(available_at), error, self._now(), task_id),
            )

    def mark_blocked(
        self, task_id: int, available_at: datetime | str, status: int
    ) -> None:
        with self._database._connection() as connection:
            connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'blocked_wait', available_at = ?, last_http_status = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (_as_timestamp(available_at), status, self._now(), task_id),
            )

    def mark_failed(self, task_id: int, error: str) -> None:
        with self._database._connection() as connection:
            updated = connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (error, self._now(), task_id),
            )
            if updated.rowcount:
                connection.execute(
                    """
                    UPDATE crawl_runs
                    SET failed_tasks = failed_tasks + 1
                    WHERE id = (SELECT run_id FROM crawl_tasks WHERE id = ?)
                    """,
                    (task_id,),
                )

    def bind_volatility_date(
        self, run_id: str, index_code: str, data_date: str
    ) -> None:
        with self._database._connection() as connection:
            connection.execute(
                """
                UPDATE crawl_tasks
                SET target_data_date = ?, updated_at = ?
                WHERE run_id = ? AND index_code = ? AND endpoint = 'volatility'
                  AND status IN ('pending', 'retry_wait', 'blocked_wait')
                """,
                (data_date, self._now(), run_id, index_code),
            )

    def release_unrequested(self, task_id: int) -> bool:
        """Return a claimed task to pending when no HTTP request was sent."""
        now = self._now()
        with self._database._connection() as connection:
            updated = connection.execute(
                """
                UPDATE crawl_tasks
                SET status = 'pending', attempts = MAX(attempts - 1, 0),
                    available_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, now, task_id),
            )
            return bool(updated.rowcount)

    def endpoint_status(
        self, run_id: str, index_code: str, endpoint: str
    ) -> str | None:
        with self._database._connection() as connection:
            row = connection.execute(
                """
                SELECT status FROM crawl_tasks
                WHERE run_id = ? AND index_code = ? AND endpoint = ?
                """,
                (run_id, index_code, endpoint),
            ).fetchone()
        return None if row is None else str(row["status"])

    def progress(self, run_id: str) -> RunProgress:
        with self._database._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_tasks,
                    SUM(status = 'success') AS success_tasks,
                    SUM(status = 'failed') AS failed_tasks,
                    SUM(status IN ('pending', 'running', 'retry_wait', 'blocked_wait'))
                        AS pending_tasks
                FROM crawl_tasks
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return RunProgress(
            total_tasks=row["total_tasks"],
            success_tasks=row["success_tasks"] or 0,
            failed_tasks=row["failed_tasks"] or 0,
            pending_tasks=row["pending_tasks"] or 0,
        )

    def recover_interrupted(self) -> int:
        return self._database.recover_interrupted_tasks()

    def update_run_state(self, run_id: str, status: str) -> RunProgress:
        finished_at = None if status == "waiting" else self._now()
        return self._database.update_run_state(run_id, status, finished_at)

    def _now(self) -> str:
        if self._clock is None:
            return _utc_now()
        clock = self._clock
        value = clock.now() if hasattr(clock, "now") else clock()
        return _as_timestamp(value)


def _to_task(row: object) -> CrawlTask:
    return CrawlTask(
        id=row["id"],
        run_id=row["run_id"],
        index_code=row["index_code"],
        endpoint=row["endpoint"],
        target_data_date=row["target_data_date"],
        status=row["status"],
        attempts=row["attempts"],
    )


def _as_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
