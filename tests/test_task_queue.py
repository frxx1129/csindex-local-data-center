from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from csindex_local.db import Database
from csindex_local.models import IndexRecord
from csindex_local.task_queue import TaskQueue


@pytest.fixture
def queue(tmp_path: Path) -> TaskQueue:
    database = Database(tmp_path / "queue.db")
    database.initialize()
    database.upsert_indices(
        [
            IndexRecord("000300", "CSI 300", None, {}),
            IndexRecord("000905", "CSI 500", None, {}),
        ]
    )
    return TaskQueue(database)


def test_each_code_gets_two_tasks(queue: TaskQueue) -> None:
    """A missing endpoint task must make the crawl run incomplete."""
    run_id = queue.create_run("fixed", "2", ["000300", "000905"], "2026-09-03")

    assert queue.progress(run_id).total_tasks == 4


def test_claim_is_atomic_and_recovery_requeues_running(queue: TaskQueue) -> None:
    """A process crash must not permanently strand its claimed task."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")

    first = queue.claim_next(run_id)

    assert first is not None
    assert first.status == "running"
    assert queue.recover_interrupted() == 1
    recovered = queue.claim_next(run_id)
    assert recovered is not None
    assert recovered.id == first.id


def test_success_task_is_not_claimed_again_and_updates_progress(queue: TaskQueue) -> None:
    """Completing a task must remove it from the eligible queue exactly once."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    first = queue.claim_next(run_id)

    assert first is not None
    queue.mark_success(first.id)

    progress = queue.progress(run_id)
    assert progress.total_tasks == 2
    assert progress.success_tasks == 1
    assert progress.pending_tasks == 1
    with queue._database._connection() as connection:
        stored_successes = connection.execute(
            "SELECT success_tasks FROM crawl_runs WHERE id = ?", (run_id,)
        ).fetchone()["success_tasks"]
    assert stored_successes == 1


def test_retry_wait_task_is_not_claimed_before_its_available_time(queue: TaskQueue) -> None:
    """Ignoring available_at would cause an immediate retry storm."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    first = queue.claim_next(run_id)

    assert first is not None
    queue.mark_retry(
        first.id,
        datetime.now(timezone.utc) + timedelta(hours=1),
        "temporary outage",
    )

    second = queue.claim_next(run_id)
    assert second is not None
    assert second.endpoint == "volatility"
    queue.mark_success(second.id)
    assert queue.claim_next(run_id) is None


def test_blocked_task_records_http_status_and_waits(queue: TaskQueue) -> None:
    """A blocked response needs a persisted cooldown instead of a fast retry."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    first = queue.claim_next(run_id)

    assert first is not None
    queue.mark_blocked(
        first.id,
        datetime.now(timezone.utc) + timedelta(hours=1),
        403,
    )

    with queue._database._connection() as connection:
        row = connection.execute(
            "SELECT status, last_http_status FROM crawl_tasks WHERE id = ?", (first.id,)
        ).fetchone()
    assert row["status"] == "blocked_wait"
    assert row["last_http_status"] == 403


def test_yield_tasks_are_claimed_before_any_volatility_task(queue: TaskQueue) -> None:
    """Volatility work must not delay the higher-priority yield endpoint."""
    run_id = queue.create_run(
        "fixed", "2", ["000300", "000905"], "2026-09-03"
    )

    first = queue.claim_next(run_id)
    assert first is not None
    queue.mark_success(first.id)
    second = queue.claim_next(run_id)

    assert first.endpoint == "yield"
    assert second is not None
    assert second.endpoint == "yield"


def test_concurrent_claimers_receive_distinct_tasks(queue: TaskQueue) -> None:
    """A non-atomic select/update could hand one task to two workers."""
    run_id = queue.create_run(
        "fixed", "2", ["000300", "000905"], "2026-09-03"
    )
    start = threading.Barrier(2)
    claimed = []
    errors = []

    def claim() -> None:
        try:
            start.wait(timeout=2)
            task = queue.claim_next(run_id)
            assert task is not None
            claimed.append(task)
        except Exception as error:
            errors.append(error)

    workers = [threading.Thread(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert {task.id for task in claimed}.__len__() == 2


def test_failed_task_is_terminal_and_updates_progress(queue: TaskQueue) -> None:
    """A permanent error must be counted and must not re-enter the queue."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    task = queue.claim_next(run_id)

    assert task is not None
    queue.mark_failed(task.id, "index does not exist")
    queue.mark_failed(task.id, "second failure must be ignored")

    progress = queue.progress(run_id)
    assert progress.success_tasks == 0
    assert progress.failed_tasks == 1
    assert progress.pending_tasks == 1
    with queue._database._connection() as connection:
        row = connection.execute(
            "SELECT status, last_error FROM crawl_tasks WHERE id = ?", (task.id,)
        ).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] == "index does not exist"


def test_terminal_success_cannot_be_reopened_as_retry(queue: TaskQueue) -> None:
    """State updates only apply to running tasks, preventing resurrection."""
    run_id = queue.create_run("fixed", "1", ["000300"], "2026-09-03")
    task = queue.claim_next(run_id)

    assert task is not None
    queue.mark_success(task.id)
    queue.mark_retry(task.id, datetime.now(timezone.utc), "late retry")
    queue.mark_blocked(task.id, datetime.now(timezone.utc), 403)

    progress = queue.progress(run_id)
    assert progress.success_tasks == 1
    assert progress.failed_tasks == 0
    assert progress.pending_tasks == 1
    assert queue.claim_next(run_id) is not None


def test_recovery_only_requeues_running_tasks(queue: TaskQueue) -> None:
    """Restart recovery must leave terminal and waiting states untouched."""
    run_id = queue.create_run(
        "fixed", "2", ["000300", "000905"], "2026-09-03"
    )
    running = queue.claim_next(run_id)
    waiting = queue.claim_next(run_id)

    assert running is not None
    assert waiting is not None
    queue.mark_success(waiting.id)
    recovered = queue.recover_interrupted()

    assert recovered == 1
    with queue._database._connection() as connection:
        rows = connection.execute(
            "SELECT id, status FROM crawl_tasks WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    assert {row["status"] for row in rows}.issuperset({"pending", "success"})
