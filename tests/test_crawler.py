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
    RunProgress,
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
        self.volatility_requests: list[tuple[str, str]] = []
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
        self.volatility_requests.append((code, data_date))
        default = VolatilitySnapshot(code, data_date, 7, 8, 9)
        return self._next("volatility", code, default)

    def _next(self, endpoint: str, code: str, default: object):
        queued = self.responses.get((endpoint, code))
        value = queued.pop(0) if queued else default
        if isinstance(value, BaseException):
            raise value
        return value


class GateLimiter:
    """Blocks the first run request after allowing prepare's probe."""

    blocked_until = None

    def __init__(self) -> None:
        self.calls = 0
        self.waiting = threading.Event()
        self.release = threading.Event()

    def before_request(self) -> None:
        self.calls += 1
        if self.calls > 1:
            self.waiting.set()
            assert self.release.wait(2)

    def after_request(self) -> None:
        pass

    def enter_blocked_cooldown(self, now: datetime) -> datetime:
        self.blocked_until = now + timedelta(seconds=1800)
        return self.blocked_until

    def clear_blocked_cooldown(self) -> None:
        self.blocked_until = None


class BlockingClient(FakeClient):
    def __init__(self, blocked_code: str) -> None:
        super().__init__()
        self.blocked_code = blocked_code
        self.request_started = threading.Event()
        self.release_request = threading.Event()

    def fetch_yield(self, code: str) -> YieldSnapshot:
        if code != self.blocked_code:
            return super().fetch_yield(code)
        self.calls.append(("yield", code))
        self.request_started.set()
        assert self.release_request.wait(2)
        return YieldSnapshot(code, TARGET_DATE, 1, 2, 3, 4, 5, 6)


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


def no_wait_config() -> AppConfig:
    return AppConfig(
        data_dir="",
        export_dir="",
        request_delay_min_seconds=0,
        request_delay_max_seconds=0,
        batch_size=100,
        batch_rest_seconds=0,
    )


def build_persistent_crawler(
    client: FakeClient, database: Database, clock: FakeClock
) -> Crawler:
    return Crawler(
        client,
        database,
        config=no_wait_config(),
        clock=clock.now,
        sleep=clock.sleep,
        random_uniform=lambda minimum, maximum: 0,
    )


def task_rows(database: Database, run_id: str):
    with database._connection() as connection:
        return connection.execute(
            "SELECT * FROM crawl_tasks WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()


def run_row(database: Database, run_id: str):
    with database._connection() as connection:
        return connection.execute(
            "SELECT * FROM crawl_runs WHERE id = ?", (run_id,)
        ).fetchone()


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


def test_date_mismatch_binds_volatility_to_actual_yield_date_and_continues(
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
        ScopeSelection("codes", ("000905", "000852")), UpdateMode.FORCE
    )

    progress = crawler.run(run_id, CrawlControl(), lambda event: None)

    actual = database.get_snapshot("000905", "2026-09-02")
    assert actual["is_complete"] == 1
    assert database.get_snapshot("000905", TARGET_DATE) is None
    assert database.get_snapshot("000852", TARGET_DATE)["is_complete"] == 1
    assert progress.success_tasks == 2
    assert progress.failed_tasks == 2


def test_existing_target_volatility_is_not_satisfied_by_wrong_date_yield(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    database.upsert_indices(fake_client.indices)
    database.merge_volatility(VolatilitySnapshot("000905", TARGET_DATE, 7, 8, 9))
    fake_client.queue(
        "yield", "000905", YieldSnapshot("000905", "2026-09-02", 1, 2, 3, 4, 5, 6)
    )
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.UPDATE
    )

    progress = crawler.run(run_id, CrawlControl(), lambda event: None)

    target = database.get_snapshot("000905", TARGET_DATE)
    actual = database.get_snapshot("000905", "2026-09-02")
    assert progress.success_tasks == 0
    assert progress.failed_tasks == 2
    assert target["yield_fetched_at"] is None
    assert actual["is_complete"] == 1
    assert fake_client.volatility_requests == [("000905", "2026-09-02")]


def test_cooldown_survives_restart_and_successful_probe_resets_escalation(
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
    ).total_seconds() == 1800


def test_prepare_probe_block_enters_and_persists_cooldown(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("yield", "000300", BlockedError("probe blocked"))
    crawler = build_persistent_crawler(fake_client, database, fake_clock)

    with pytest.raises(BlockedError, match="probe blocked"):
        crawler.prepare_run(ScopeSelection("fixed_count", 1), UpdateMode.UPDATE)

    state = database.get_runtime_state("waf_cooldown")
    assert (
        datetime.fromisoformat(state["until"]) - fake_clock.now()
    ).total_seconds() == 1800
    assert state["next_cooldown_seconds"] == 3600


def test_run_block_stops_ordinary_work_and_successful_probe_clears_cooldown(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("yield", "000905", BlockedError("task blocked"))
    crawler = build_persistent_crawler(fake_client, database, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905", "000852")), UpdateMode.FORCE
    )

    waiting = crawler.run(run_id, CrawlControl(), lambda event: None)

    assert waiting.pending_tasks == 4
    assert fake_client.calls.count(("yield", "000905")) == 1
    assert fake_client.calls.count(("yield", "000852")) == 0
    assert database.get_runtime_state("waf_cooldown") is not None

    finished = crawler.run(run_id, CrawlControl(), lambda event: None)

    assert fake_client.calls.count(("yield", "000300")) == 2
    assert fake_client.calls.count(("yield", "000852")) == 1
    assert database.get_runtime_state("waf_cooldown") is None
    assert finished.pending_tasks == 0


def test_failed_cooldown_probe_escalates_without_ordinary_requests(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("yield", "000905", BlockedError("task blocked"))
    crawler = build_persistent_crawler(fake_client, database, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905", "000852")), UpdateMode.FORCE
    )
    crawler.run(run_id, CrawlControl(), lambda event: None)
    first_until = datetime.fromisoformat(
        database.get_runtime_state("waf_cooldown")["until"]
    )
    fake_client.queue("yield", "000300", BlockedError("probe still blocked"))

    waiting = crawler.run(run_id, CrawlControl(), lambda event: None)

    state = database.get_runtime_state("waf_cooldown")
    assert fake_clock.now() == first_until
    assert (
        datetime.fromisoformat(state["until"]) - fake_clock.now()
    ).total_seconds() == 3600
    assert fake_client.calls.count(("yield", "000300")) == 2
    assert fake_client.calls.count(("yield", "000852")) == 0
    assert waiting.pending_tasks == 4


def test_run_one_uses_baseline_probe_before_resuming_blocked_task(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("volatility", "000905", BlockedError("task blocked"))
    crawler = build_persistent_crawler(fake_client, database, fake_clock)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    crawler.run_one(run_id)
    crawler.run_one(run_id)
    cooldown_until = datetime.fromisoformat(
        database.get_runtime_state("waf_cooldown")["until"]
    )
    fake_clock.current = cooldown_until

    crawler.run_one(run_id)

    assert fake_client.calls.count(("yield", "000300")) == 2
    assert fake_client.calls.count(("volatility", "000905")) == 1
    assert database.get_runtime_state("waf_cooldown") is None


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


def test_stop_during_rate_wait_releases_claim_without_sending_request(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    limiter = GateLimiter()
    crawler = Crawler(fake_client, database, limiter, clock=fake_clock.now)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    control = CrawlControl()
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(crawler.run(run_id, control, lambda event: None))
    )
    worker.start()
    assert limiter.waiting.wait(1)

    control.stop()
    limiter.release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert fake_client.calls.count(("yield", "000905")) == 0
    row = task_rows(database, run_id)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_pause_during_rate_wait_defers_request_until_resume(
    fake_client: FakeClient,
    database: Database,
    fake_clock: FakeClock,
) -> None:
    limiter = GateLimiter()
    crawler = Crawler(fake_client, database, limiter, clock=fake_clock.now)
    run_id = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    control = CrawlControl()
    worker = threading.Thread(
        target=lambda: crawler.run(run_id, control, lambda event: None)
    )
    worker.start()
    assert limiter.waiting.wait(1)

    control.pause()
    limiter.release.set()
    worker.join(0.05)
    assert worker.is_alive()
    assert fake_client.calls.count(("yield", "000905")) == 0

    control.resume()
    worker.join(2)

    assert not worker.is_alive()
    assert fake_client.calls.count(("yield", "000905")) == 1


def test_second_concurrent_run_cannot_start_another_detail_request(
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    client = BlockingClient("000905")
    crawler = build_crawler(client, database, fast_limiter, fake_clock)
    first_run = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    second_run = crawler.prepare_run(
        ScopeSelection("codes", ("000852",)), UpdateMode.FORCE
    )
    first_worker = threading.Thread(
        target=lambda: crawler.run(first_run, CrawlControl(), lambda event: None)
    )
    first_worker.start()
    assert client.request_started.wait(1)
    second_result: list[object] = []
    second_worker = threading.Thread(
        target=lambda: second_result.append(
            crawler.run(second_run, CrawlControl(), lambda event: None)
        )
    )

    second_worker.start()
    second_worker.join(1)
    client.release_request.set()
    first_worker.join(2)

    assert not second_worker.is_alive()
    assert client.calls.count(("yield", "000852")) == 0
    assert second_result[0].pending_tasks == 2
    assert not first_worker.is_alive()


def test_preconstructed_crawler_refreshes_cooldown_after_acquiring_worker_lock(
    database: Database,
    fake_clock: FakeClock,
) -> None:
    first_client = FakeClient()
    second_client = FakeClient()
    first = build_persistent_crawler(first_client, database, fake_clock)
    second = build_persistent_crawler(second_client, database, fake_clock)
    first_client.queue("yield", "000905", BlockedError("blocked later"))
    first_run = first.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    second_run = second.prepare_run(
        ScopeSelection("codes", ("000852",)), UpdateMode.FORCE
    )

    first.run(first_run, CrawlControl(), lambda event: None)
    second.run(second_run, CrawlControl(), lambda event: None)

    assert fake_clock.sleeps == [1800]
    assert second_client.calls.count(("yield", "000300")) == 2
    assert second_client.calls.index(("yield", "000300"), 2) < second_client.calls.index(
        ("yield", "000852")
    )
    assert database.get_runtime_state("waf_cooldown") is None


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

    assert events[-1] == CrawlEvent("run_completed", run_id, progress)
    assert {event.kind for event in events} >= {"task_started", "task_success"}
    with pytest.raises(FrozenInstanceError):
        events[-1].kind = "changed"


def test_completed_run_persists_terminal_status_counts_and_event(
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
    row = run_row(database, run_id)

    assert progress == row_progress(row)
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert events[-1].kind == "run_completed"


def test_failed_run_persists_completed_with_failures_and_matching_event(
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
    events: list[CrawlEvent] = []

    progress = crawler.run(run_id, CrawlControl(), events.append)
    row = run_row(database, run_id)

    assert progress.failed_tasks == 2
    assert row["status"] == "completed_with_failures"
    assert row["failed_tasks"] == 2
    assert row["finished_at"] is not None
    assert events[-1].kind == "run_completed_with_failures"


def test_waiting_and_stopped_runs_persist_distinct_noncompleted_states(
    fake_client: FakeClient,
    database: Database,
    fast_limiter: RateLimiter,
    fake_clock: FakeClock,
) -> None:
    fake_client.queue("yield", "000905", NetworkError("offline"))
    crawler = build_crawler(fake_client, database, fast_limiter, fake_clock)
    waiting_run = crawler.prepare_run(
        ScopeSelection("codes", ("000905",)), UpdateMode.FORCE
    )
    waiting_events: list[CrawlEvent] = []
    crawler.run(waiting_run, CrawlControl(), waiting_events.append)

    waiting = run_row(database, waiting_run)
    assert waiting["status"] == "waiting"
    assert waiting["finished_at"] is None
    assert waiting_events[-1].kind == "run_waiting"

    stopped_run = crawler.prepare_run(
        ScopeSelection("codes", ("000852",)), UpdateMode.FORCE
    )
    control = CrawlControl()
    control.stop()
    stopped_events: list[CrawlEvent] = []
    crawler.run(stopped_run, control, stopped_events.append)

    stopped = run_row(database, stopped_run)
    assert stopped["status"] == "stopped"
    assert stopped["finished_at"] is not None
    assert stopped_events[-1].kind == "run_stopped"


def row_progress(row) -> RunProgress:
    return RunProgress(
        total_tasks=row["total_tasks"],
        success_tasks=row["success_tasks"],
        failed_tasks=row["failed_tasks"],
        pending_tasks=row["total_tasks"] - row["success_tasks"] - row["failed_tasks"],
    )


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
