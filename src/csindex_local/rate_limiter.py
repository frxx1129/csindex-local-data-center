"""Global request pacing and WAF cooldown state.

The limiter owns only timing policy.  It does not know about HTTP clients or
the crawl queue, which keeps it usable by both a crawler and deterministic
tests using a virtual clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import time
from typing import Callable

from .config import AppConfig


Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
RandomUniform = Callable[[float, float], float]
PersistCooldown = Callable[[datetime | None], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RateLimiter:
    """Enforce the configured global request interval and WAF cooldown.

    ``clock``, ``sleep`` and ``random_uniform`` are injectable so callers can
    test the policy without waiting in real time.  ``persist_cooldown`` is
    called whenever the blocked deadline changes; passing a deadline to the
    constructor restores a deadline saved by an earlier process.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        clock: Clock | object | None = None,
        sleep: Sleep = time.sleep,
        random_uniform: RandomUniform = random.uniform,
        persist_cooldown: PersistCooldown | None = None,
        cooldown_until: datetime | None = None,
    ) -> None:
        self._config = config or AppConfig(data_dir="", export_dir="")
        self._clock = clock if clock is not None else _utc_now
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._persist_cooldown = persist_cooldown

        self._request_count = 0
        self._next_request_at: datetime | None = None
        self._batch_rest_until: datetime | None = None
        self._blocked_until = (
            self._as_aware(cooldown_until) if cooldown_until is not None else None
        )
        self._next_blocked_seconds = float(
            self._config.blocked_initial_cooldown_seconds
        )

    def before_request(self) -> None:
        """Wait until a request is allowed by all active timing rules."""
        now = self._now()
        deadlines = (
            deadline
            for deadline in (
                self._blocked_until,
                self._batch_rest_until,
                self._next_request_at,
            )
            if deadline is not None and deadline > now
        )
        deadline = max(deadlines, default=None)
        if deadline is None:
            return

        seconds = (deadline - now).total_seconds()
        if seconds > 0:
            self._sleep(seconds)

        # A virtual sleep implementation may advance the clock itself.  The
        # deadlines remain harmless when it does not; the next call observes
        # the same policy and production ``time.sleep`` always advances time.
        if self._batch_rest_until is not None and self._now() >= self._batch_rest_until:
            self._batch_rest_until = None

    def after_request(self) -> None:
        """Record one completed request and schedule the next request."""
        now = self._now()
        self._request_count += 1
        delay = float(
            self._random_uniform(
                self._config.request_delay_min_seconds,
                self._config.request_delay_max_seconds,
            )
        )
        self._next_request_at = now + timedelta(seconds=max(0.0, delay))

        if self._request_count >= self._config.batch_size:
            self._request_count = 0
            self._batch_rest_until = now + timedelta(
                seconds=self._config.batch_rest_seconds
            )

    def enter_blocked_cooldown(self, now: datetime) -> datetime:
        """Enter or extend WAF cooldown and return its new deadline.

        The first block waits for the configured initial duration.  Each
        subsequent block doubles that duration, capped at the configured
        maximum.  A new block starts its deadline at the supplied ``now``.
        """
        current = self._as_aware(now)
        seconds = min(
            self._next_blocked_seconds,
            float(self._config.blocked_max_cooldown_seconds),
        )
        self._blocked_until = current + timedelta(seconds=seconds)
        self._next_blocked_seconds = min(
            seconds * 2.0,
            float(self._config.blocked_max_cooldown_seconds),
        )
        if self._persist_cooldown is not None:
            self._persist_cooldown(self._blocked_until)
        return self._blocked_until

    def clear_blocked_cooldown(self) -> None:
        """Clear the active WAF deadline and reset escalation for new blocks."""
        self._blocked_until = None
        self._next_blocked_seconds = float(
            self._config.blocked_initial_cooldown_seconds
        )
        if self._persist_cooldown is not None:
            self._persist_cooldown(None)

    @property
    def blocked_until(self) -> datetime | None:
        """Return the currently persisted/active WAF deadline, if any."""
        return self._blocked_until

    def _now(self) -> datetime:
        clock = self._clock
        value = clock.now() if hasattr(clock, "now") else clock()  # type: ignore[operator]
        return self._as_aware(value)

    @staticmethod
    def _as_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
