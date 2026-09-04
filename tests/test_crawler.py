from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from csindex_local.config import AppConfig
from csindex_local.crawler import CrawlControl, Crawler
from csindex_local.csindex_client import (
    BlockedError,
    NetworkError,
    NotFoundError,
    ResponseFormatError,
)
from csindex_local.db import Database
from csindex_local.models import (
    CrawlEvent,
    IndexRecord,
    ScopeSelection,
    UpdateMode,
    VolatilitySnapshot,
    YieldSnapshot,
)
from csindex_local.rate_limiter import RateLimiter


TARGET_DATE = "2026-09-03"


def make_index(code: str, name: str) -> IndexRecord:
    return IndexRecord(code, name, "是", {"indexSeries": "规模指数"})


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self) -> None:
        self.indices = [
            make_index("000300", "沪深300"),
            make_index("000905", "中证500"),
            make_index("000852", "中证1000"),
        ]
        self.calls: list[tuple[str, str | None]] = []
        self.responses: dict[tuple[str, str], list[object]] = {}

    def queue(self, endpoint: str, code: str, *responses: object) -> None:
        self.responses[(endpoint, code)] = list(responses)

    def fetch_index_list(self) -> list[IndexRecord]:
        self.calls.append(("list", None))
        return list(self.indices)

    def fetch_yield(self, code: str) -> YieldSnapshot:
        self.calls.append(("yield", code))
        default = YieldSnapshot(code, TARGET_DATE, 1, 2, 3, 4, 5, 6)
        return self._next("yield", code, default)

    def fetch_volatility(self, code: str, data_date: str) -> VolatilitySnapshot:
        self.calls.append(("volatility", code))
        default = VolatilitySnapshot(code, data_date, 7, 8, 9)
        return self._next("volatility", code, default)

    def _next(self, endpoint: str, code: str, default: object):
        queued = self.responses.get((endpoint, code))
        value = queued.pop(0) if queued else default
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "crawler.db")
    value.initialize()
    return value


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fast_limiter(fake_clock: FakeClock) -> RateLimiter:
    config = AppConfig(
        data_dir="",
        export_dir="",
        request_delay_min_seconds=0,
        request_delay_max_seconds=0,
        batch_size=100,
        batch_rest_seconds=0,
    )
    return RateLimiter(
        config,
        clock=fake_clock.now,
        sleep=fake_clock.sleep,
        random_uniform=lambda minimum, maximum: 0,
    )


def build_crawler(
    client: FakeClient,
    database: Database,
    limiter: RateLimiter,
    clock: FakeClock,
) -> Crawler:
    return Crawler(client, database, limiter, clock=clock.now)


def task_rows(database: Database, run_id: str):
    with database._connection() as connection:
        return connection.execute(
            "SELECT * FROM crawl_tasks WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()


def make_retry_claimable(database: Database, run_id: str) -> None:
    with database._connection() as connection:
        connection.execute(
            """
            UPDATE crawl_tasks SET available_at = '2000-01-01T00:00:00+00:00'
            WHERE run_id = ? AND endpoint = 'yield' AND status = 'retry_wait'
            """,
            (run_id,),
        )


def test_two_codes_fetch_all_nine_metrics(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000300", "000905")), UpdateMode.MISSING
    )

    progress = crawler.run(run_id, CrawlControl(), lambda event: None)

    assert progress.success_tasks == 4
    assert database.get_snapshot("000300", TARGET_DATE)["is_complete"] == 1
    assert database.get_snapshot("000905", TARGET_DATE)["is_complete"] == 1


def test_probe_result_is_reused_and_restart_does_not_repeat_successful_task(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000300",)), UpdateMode.MISSING
    )
    crawler.run_one(run_id)

    restarted = build_crawler(fake_client, database, fast_limiter, fake_clock)
    progress = restarted.run(run_id, CrawlControl(), lambda event: None)

    assert progress.success_tasks == 2
    assert fake_client.calls.count(("yield", "000300")) == 1


def test_fixed_scope_is_reused_until_explicitly_regenerated(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    crawler.prepare_run(ScopeSelection("fixed_count", 1), UpdateMode.FORCE)
    fake_client.indices = [fake_client.indices[1], fake_client.indices[0]]

    crawler.prepare_run(ScopeSelection("fixed_count", 1), UpdateMode.FORCE)
    reused = database.create_or_get_scope("fixed:1", ["000905"])
    crawler.prepare_run(
        ScopeSelection("fixed_count", 1, regenerate=True), UpdateMode.FORCE
    )
    regenerated = database.create_or_get_scope("fixed:1", ["000300"])

    assert reused.codes == ("000300",)
    assert regenerated.codes == ("000905",)


def test_missing_skips_old_complete_but_update_and_force_create_two_tasks(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    database.upsert_indices(fake_client.indices)
    database.upsert_yield(YieldSnapshot("000905", "2026-09-02", 1, 2, 3, 4, 5, 6))
    database.merge_volatility(
        VolatilitySnapshot("000905", "2026-09-02", 7, 8, 9)
    )
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    selection = ScopeSelection("codes", ("000905",))

    missing = crawler.prepare_run(selection, UpdateMode.MISSING)
    update = crawler.prepare_run(selection, UpdateMode.UPDATE)
    force = crawler.prepare_run(selection, UpdateMode.FORCE)

    assert len(task_rows(database, missing)) == 0
    assert len(task_rows(database, update)) == 2
    assert len(task_rows(database, force)) == 2


def test_target_endpoint_already_fetched_is_not_requested_again(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    database.upsert_indices(fake_client.indices)
    database.upsert_yield(YieldSnapshot("000905", TARGET_DATE, 1, 2, 3, 4, 5, 6))
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.UPDATE
    )

    progress = crawler.run(run_id, CrawlControl(), lambda event: None)

    assert progress.success_tasks == 2
    assert fake_client.calls.count(("yield", "000905")) == 0
    assert fake_client.calls.count(("volatility", "000905")) == 1


@pytest.mark.parametrize("error_type", [NetworkError, ResponseFormatError])
def test_transient_errors_back_off_30_120_300_then_fail(
    error_type: type[Exception],
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue(
        "yield",
        "000905",
        error_type("one"),
        error_type("two"),
        error_type("three"),
        error_type("four"),
    )
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )

    expected_delays = (30, 120, 300)
    for expected in expected_delays:
        crawler.run_one(run_id)
        row = next(row for row in task_rows(database, run_id) if row["endpoint"] == "yield")
        available = datetime.fromisoformat(row["available_at"])
        assert row["status"] == "retry_wait"
        assert (available - fake_clock.now()).total_seconds() == expected
        make_retry_claimable(database, run_id)

    crawler.run_one(run_id)
    row = next(row for row in task_rows(database, run_id) if row["endpoint"] == "yield")

    assert row["status"] == "failed"
    assert row["attempts"] == 4


def test_not_found_fails_without_retry(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("yield", "000905", NotFoundError("gone"))
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )

    crawler.run_one(run_id)
    row = next(row for row in task_rows(database, run_id) if row["endpoint"] == "yield")

    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_date_mismatch_saves_actual_date_and_fails_target_task(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue(
        "yield", "000905", YieldSnapshot("000905", "2026-09-02", 1, 2, 3, 4, 5, 6)
    )
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )

    crawler.run_one(run_id)
    row = next(row for row in task_rows(database, run_id) if row["endpoint"] == "yield")

    assert database.get_snapshot("000905", "2026-09-02") is not None
    assert row["status"] == "failed"
    assert "2026-09-02" in row["last_error"]


def test_blocked_cooldown_state_and_escalation_survive_restart(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    config = AppConfig(
        data_dir="",
        export_dir="",
        request_delay_min_seconds=0,
        request_delay_max_seconds=0,
        batch_size=100,
        batch_rest_seconds=0,
    )
    fake_client.queue("volatility", "000905", BlockedError("blocked"))
    first = Crawler(
        fake_client,
        database,
        config=config,
        clock=fake_clock.now,
        sleep=fake_clock.sleep,
        random_uniform=lambda minimum, maximum: 0,
    )
    run_id = first.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    first.run_one(run_id)
    first.run_one(run_id)

    state = database.get_runtime_state("waf_cooldown")
    first_until = datetime.fromisoformat(state["until"])
    assert (first_until - fake_clock.now()).total_seconds() == 1800
    assert state["next_cooldown_seconds"] == 3600
    assert task_rows(database, run_id)[1]["last_http_status"] == 403

    fake_clock.current = first_until
    second_client = FakeClient()
    second_client.queue("volatility", "000905", BlockedError("blocked again"))
    second = Crawler(
        second_client,
        database,
        config=config,
        clock=fake_clock.now,
        sleep=fake_clock.sleep,
        random_uniform=lambda minimum, maximum: 0,
    )
    second_run = second.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    second.run_one(second_run)
    second.run_one(second_run)
    second_state = database.get_runtime_state("waf_cooldown")

    assert (
        datetime.fromisoformat(second_state["until"]) - fake_clock.now()
    ).total_seconds() == 3600


def test_control_pause_resume_and_stop_are_thread_safe() -> None:
    control = CrawlControl()
    released = threading.Event()
    control.pause()
    waiter = threading.Thread(
        target=lambda: (control.wait_if_paused(), released.set()), daemon=True
    )
    waiter.start()

    assert not released.wait(0.05)
    control.resume()
    assert released.wait(1)

    released.clear()
    control.pause()
    stopper_waiter = threading.Thread(
        target=lambda: (control.wait_if_paused(), released.set()), daemon=True
    )
    stopper_waiter.start()
    control.stop()

    assert released.wait(1)
    assert control.stopped


def test_pre_stopped_run_leaves_tasks_pending(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    control = CrawlControl()
    control.stop()

    progress = crawler.run(run_id, control, lambda event: None)

    assert progress.pending_tasks == 2
    assert fake_client.calls.count(("yield", "000905")) == 0


def test_events_have_stable_frozen_shape(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000300",)), UpdateMode.MISSING
    )
    events: list[CrawlEvent] = []

    progress = crawler.run(run_id, CrawlControl(), events.append)

    assert events[-1] == CrawlEvent("run_complete", run_id, progress)
    assert {event.kind for event in events} >= {"task_started", "task_success"}
    with pytest.raises(FrozenInstanceError):
        events[-1].kind = "changed"


def test_runtime_state_round_trip_and_delete(database: Database) -> None:
    value = {
        "until": "2026-09-04T00:30:00+00:00",
        "next_cooldown_seconds": 3600,
    }

    database.set_runtime_state("waf_cooldown", value)
    loaded = database.get_runtime_state("waf_cooldown")
    database.delete_runtime_state("waf_cooldown")

    assert loaded == value
    assert database.get_runtime_state("waf_cooldown") is None

