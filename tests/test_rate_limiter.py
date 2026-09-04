from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from csindex_local.rate_limiter import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)

    def advance_to(self, value: datetime) -> None:
        self.current = value


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def build_limiter(clock: FakeClock, **kwargs: object) -> RateLimiter:
    return RateLimiter(
        clock=clock.now,
        sleep=clock.sleep,
        random_uniform=lambda minimum, maximum: 7.5,
        **kwargs,
    )


def test_first_request_does_not_sleep(fake_clock: FakeClock) -> None:
    limiter = build_limiter(fake_clock)

    limiter.before_request()

    assert fake_clock.sleeps == []


def test_requests_are_separated_by_injected_random_delay(fake_clock: FakeClock) -> None:
    limiter = build_limiter(fake_clock)

    limiter.before_request()
    limiter.after_request()
    limiter.before_request()

    assert fake_clock.sleeps == [7.5]


def test_25_requests_trigger_150_second_rest(fake_clock: FakeClock) -> None:
    limiter = build_limiter(fake_clock)

    for _ in range(25):
        limiter.before_request()
        limiter.after_request()
    limiter.before_request()

    assert fake_clock.sleeps[-1] >= 150


def test_blocked_cooldown_grows_from_30_to_60_minutes(
    fake_clock: FakeClock,
) -> None:
    limiter = build_limiter(fake_clock)

    first = limiter.enter_blocked_cooldown(fake_clock.now())
    fake_clock.advance_to(first)
    second = limiter.enter_blocked_cooldown(fake_clock.now())

    assert (first - datetime(2026, 9, 4, tzinfo=timezone.utc)).total_seconds() == 1800
    assert (second - fake_clock.now()).total_seconds() == 3600


def test_blocked_cooldown_is_persisted_and_clear_resets_escalation(
    fake_clock: FakeClock,
) -> None:
    persisted: list[datetime | None] = []
    limiter = build_limiter(fake_clock, persist_cooldown=persisted.append)

    first = limiter.enter_blocked_cooldown(fake_clock.now())
    limiter.clear_blocked_cooldown()
    next_first = limiter.enter_blocked_cooldown(fake_clock.now())

    assert persisted == [first, None, next_first]
    assert (next_first - fake_clock.now()).total_seconds() == 1800


def test_before_request_waits_for_persisted_blocked_cooldown(
    fake_clock: FakeClock,
) -> None:
    limiter = build_limiter(fake_clock)
    until = limiter.enter_blocked_cooldown(fake_clock.now())

    limiter.before_request()

    assert fake_clock.sleeps[-1] == 1800
    assert fake_clock.now() == until


def test_clear_blocked_cooldown_allows_request_without_waiting(
    fake_clock: FakeClock,
) -> None:
    limiter = build_limiter(fake_clock)
    limiter.enter_blocked_cooldown(fake_clock.now())
    limiter.clear_blocked_cooldown()

    limiter.before_request()

    assert fake_clock.sleeps == []


def test_cooldown_escalation_survives_restart_and_clear_resets_state(
    fake_clock: FakeClock,
) -> None:
    persisted: dict[str, object | None] = {"state": None}

    def save_state(state: object | None) -> None:
        persisted["state"] = state

    instance_a = build_limiter(fake_clock, persist_cooldown_state=save_state)
    first = instance_a.enter_blocked_cooldown(fake_clock.now())

    instance_b = build_limiter(
        fake_clock,
        persist_cooldown_state=save_state,
        cooldown_state=persisted["state"],
    )
    fake_clock.advance_to(first)
    second = instance_b.enter_blocked_cooldown(fake_clock.now())

    assert (second - fake_clock.now()).total_seconds() == 3600

    instance_b.clear_blocked_cooldown()
    instance_c = build_limiter(
        fake_clock,
        persist_cooldown_state=save_state,
        cooldown_state=persisted["state"],
    )
    third = instance_c.enter_blocked_cooldown(fake_clock.now())

    assert (third - fake_clock.now()).total_seconds() == 1800


def test_legacy_datetime_persistence_callback_keeps_datetime_contract(
    fake_clock: FakeClock,
) -> None:
    persisted: list[str | None] = []

    def save_datetime(value: datetime | None) -> None:
        persisted.append(None if value is None else value.isoformat())

    limiter = build_limiter(fake_clock, persist_cooldown=save_datetime)
    first = limiter.enter_blocked_cooldown(fake_clock.now())
    limiter.clear_blocked_cooldown()

    assert persisted == [first.isoformat(), None]


def test_clear_notifies_legacy_and_complete_state_callbacks(
    fake_clock: FakeClock,
) -> None:
    legacy: list[datetime | None] = []
    complete: list[object | None] = []
    limiter = build_limiter(
        fake_clock,
        persist_cooldown=legacy.append,
        persist_cooldown_state=complete.append,
    )

    first = limiter.enter_blocked_cooldown(fake_clock.now())
    limiter.clear_blocked_cooldown()

    assert legacy == [first, None]
    assert complete[0] is not None
    assert complete[0].until == first
    assert complete[0].next_cooldown_seconds == 3600
    assert complete[1] is None
