"""Resumable single-request coordinator for incremental index snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import random
import threading
import time
from typing import Callable

from .config import AppConfig
from .csindex_client import (
    BlockedError,
    CsindexClient,
    NetworkError,
    NotFoundError,
    ResponseFormatError,
)
from .db import Database
from .models import (
    CrawlEvent,
    CrawlScope,
    CrawlTask,
    RunProgress,
    ScopeSelection,
    UpdateMode,
)
from .rate_limiter import CooldownState, RateLimiter
from .task_queue import TaskQueue


EventHandler = Callable[[CrawlEvent], None]
_WAF_STATE_KEY = "waf_cooldown"
_RUN_STATE_PREFIX = "crawler_run:"
_RETRY_DELAYS = (30, 120, 300)


class CrawlControl:
    """Thread-safe cooperative pause and stop signals."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._paused = False
        self._stopped = False

    def pause(self) -> None:
        with self._condition:
            if not self._stopped:
                self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._paused = False
            self._condition.notify_all()

    def wait_if_paused(self) -> None:
        with self._condition:
            while self._paused and not self._stopped:
                self._condition.wait()

    @property
    def stopped(self) -> bool:
        with self._condition:
            return self._stopped

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused


class Crawler:
    def __init__(
        self,
        client: CsindexClient,
        database: Database,
        limiter: RateLimiter | None = None,
        *,
        config: AppConfig | None = None,
        clock: Callable[[], datetime] | object | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._client = client
        self._database = database
        self._queue = TaskQueue(database)
        self._clock = clock or _utc_now
        self._limiter = limiter or RateLimiter(
            config=config,
            clock=self._clock,
            sleep=sleep,
            random_uniform=random_uniform,
            cooldown_state=self._load_cooldown_state(),
            persist_cooldown_state=self._persist_cooldown_state,
        )

    def prepare_run(self, scope: ScopeSelection, mode: UpdateMode) -> str:
        mode = UpdateMode(mode)
        indices = self._client.fetch_index_list()
        self._database.upsert_indices(indices)
        previous = {
            item.index_code: self._latest_snapshot(item.index_code) for item in indices
        }

        self._limiter.before_request()
        try:
            probe = self._client.fetch_yield("000300")
        finally:
            self._limiter.after_request()
        if probe.index_code != "000300":
            raise ResponseFormatError("probe response returned a different index code")
        self._database.upsert_yield(probe)

        frozen_scope = self._resolve_scope(scope, indices)
        selected_codes = [
            code
            for code in frozen_scope.codes
            if self._needs_work(code, probe.data_date, mode, previous.get(code))
        ]
        run_id = self._queue.create_run(
            frozen_scope.scope_type,
            frozen_scope.scope_value,
            selected_codes,
            probe.data_date,
        )

        satisfied: list[str] = []
        for code in selected_codes:
            target = self._database.get_snapshot(code, probe.data_date)
            if code == "000300":
                satisfied.append(_endpoint_key(code, "yield"))
            if mode is not UpdateMode.FORCE and target is not None:
                if target["yield_fetched_at"] is not None:
                    satisfied.append(_endpoint_key(code, "yield"))
                if target["volatility_fetched_at"] is not None:
                    satisfied.append(_endpoint_key(code, "volatility"))
        self._database.set_runtime_state(
            _run_state_key(run_id),
            {"mode": mode.value, "satisfied": sorted(set(satisfied))},
        )
        return run_id

    def run(
        self,
        run_id: str,
        control: CrawlControl,
        on_event: EventHandler,
    ) -> RunProgress:
        self._queue.recover_interrupted()
        while not control.stopped:
            control.wait_if_paused()
            if control.stopped:
                break
            task = self._queue.claim_next(run_id)
            if task is None:
                break
            self._execute_claimed(task, on_event)

        progress = self._queue.progress(run_id)
        if control.stopped:
            self._emit(on_event, "run_stopped", run_id, progress)
        elif progress.pending_tasks == 0:
            self._database.delete_runtime_state(_run_state_key(run_id))
            self._emit(on_event, "run_complete", run_id, progress)
        else:
            self._emit(on_event, "run_waiting", run_id, progress)
        return progress

    def run_one(
        self, run_id: str, on_event: EventHandler | None = None
    ) -> RunProgress:
        handler = on_event or (lambda event: None)
        task = self._queue.claim_next(run_id)
        if task is not None:
            self._execute_claimed(task, handler)
        return self._queue.progress(run_id)

    def _execute_claimed(self, task: CrawlTask, on_event: EventHandler) -> None:
        self._emit(
            on_event,
            "task_started",
            task.run_id,
            self._queue.progress(task.run_id),
            task,
        )
        if self._is_satisfied(task):
            self._queue.mark_success(task.id)
            self._emit_task_result(on_event, "task_success", task)
            return

        self._limiter.before_request()
        try:
            mismatch = self._fetch_and_store(task)
        except BlockedError as error:
            until = self._limiter.enter_blocked_cooldown(self._now())
            self._queue.mark_blocked(
                task.id, until, int(getattr(error, "http_status", 403))
            )
            self._emit_task_result(
                on_event, "task_blocked", task, str(error), until.isoformat()
            )
        except NotFoundError as error:
            self._queue.mark_failed(task.id, str(error))
            self._emit_task_result(on_event, "task_failed", task, str(error))
        except (NetworkError, ResponseFormatError) as error:
            if task.attempts <= len(_RETRY_DELAYS):
                available = self._now() + timedelta(
                    seconds=_RETRY_DELAYS[task.attempts - 1]
                )
                self._queue.mark_retry(task.id, available, str(error))
                self._emit_task_result(
                    on_event, "task_retry", task, str(error), available.isoformat()
                )
            else:
                self._queue.mark_failed(task.id, str(error))
                self._emit_task_result(on_event, "task_failed", task, str(error))
        else:
            if mismatch is None:
                self._queue.mark_success(task.id)
                self._emit_task_result(on_event, "task_success", task)
            else:
                self._queue.mark_failed(task.id, mismatch)
                self._emit_task_result(on_event, "task_failed", task, mismatch)
        finally:
            self._limiter.after_request()

    def _fetch_and_store(self, task: CrawlTask) -> str | None:
        if task.endpoint == "yield":
            snapshot = self._client.fetch_yield(task.index_code)
            if snapshot.index_code != task.index_code:
                raise ResponseFormatError("yield response returned a different index code")
            self._database.upsert_yield(snapshot)
            if task.target_data_date and snapshot.data_date != task.target_data_date:
                return (
                    f"data date mismatch: expected {task.target_data_date}, "
                    f"got {snapshot.data_date}"
                )
            return None
        if task.endpoint == "volatility":
            if task.target_data_date is None:
                raise ResponseFormatError("volatility task has no target data date")
            snapshot = self._client.fetch_volatility(
                task.index_code, task.target_data_date
            )
            if snapshot.index_code != task.index_code:
                raise ResponseFormatError(
                    "volatility response returned a different index code"
                )
            if snapshot.data_date != task.target_data_date:
                raise ResponseFormatError(
                    "volatility response returned a different data date"
                )
            self._database.merge_volatility(snapshot)
            return None
        raise ResponseFormatError(f"unsupported endpoint: {task.endpoint}")

    def _resolve_scope(self, selection: ScopeSelection, indices: list) -> CrawlScope:
        available = {item.index_code for item in indices}
        ordered = [item.index_code for item in indices]
        if selection.kind == "fixed_count":
            if isinstance(selection.value, bool) or not isinstance(selection.value, int):
                raise ValueError("fixed_count scope requires an integer value")
            if selection.value < 1:
                raise ValueError("fixed_count scope must be positive")
            scope_id = f"fixed:{selection.value}"
            codes = ordered[: selection.value]
        elif selection.kind == "all":
            scope_id = "fixed:all"
            codes = ordered
        elif selection.kind == "codes":
            if not isinstance(selection.value, tuple) or not selection.value:
                raise ValueError("codes scope requires a non-empty tuple")
            codes = list(dict.fromkeys(selection.value))
            missing = [code for code in codes if code not in available]
            if missing:
                raise ValueError(f"unknown index codes: {', '.join(missing)}")
            digest = sha256("\0".join(codes).encode("utf-8")).hexdigest()[:16]
            scope_id = f"codes:{digest}"
        else:
            raise ValueError(f"unsupported scope kind: {selection.kind}")
        return self._database.create_or_get_scope(
            scope_id, codes, regenerate=selection.regenerate
        )

    def _needs_work(
        self,
        code: str,
        target_date: str,
        mode: UpdateMode,
        previous: object | None,
    ) -> bool:
        if mode is UpdateMode.FORCE:
            return True
        if mode is UpdateMode.MISSING:
            return previous is None or any(
                previous[column] is None
                for column in ("yield_fetched_at", "volatility_fetched_at")
            )
        target = self._database.get_snapshot(code, target_date)
        return target is None or any(
            target[column] is None
            for column in ("yield_fetched_at", "volatility_fetched_at")
        )

    def _latest_snapshot(self, code: str):
        rows = self._database.list_snapshots(code)
        return rows[-1] if rows else None

    def _is_satisfied(self, task: CrawlTask) -> bool:
        state = self._database.get_runtime_state(_run_state_key(task.run_id)) or {}
        return _endpoint_key(task.index_code, task.endpoint) in state.get(
            "satisfied", []
        )

    def _load_cooldown_state(self) -> CooldownState | None:
        value = self._database.get_runtime_state(_WAF_STATE_KEY)
        if value is None:
            return None
        try:
            return CooldownState(
                until=datetime.fromisoformat(str(value["until"])),
                next_cooldown_seconds=float(value["next_cooldown_seconds"]),
            )
        except (KeyError, TypeError, ValueError):
            self._database.delete_runtime_state(_WAF_STATE_KEY)
            return None

    def _persist_cooldown_state(self, state: CooldownState | None) -> None:
        if state is None:
            self._database.delete_runtime_state(_WAF_STATE_KEY)
            return
        self._database.set_runtime_state(
            _WAF_STATE_KEY,
            {
                "until": state.until.isoformat(),
                "next_cooldown_seconds": state.next_cooldown_seconds,
            },
        )

    def _now(self) -> datetime:
        clock = self._clock
        value = clock.now() if hasattr(clock, "now") else clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _emit_task_result(
        self,
        handler: EventHandler,
        kind: str,
        task: CrawlTask,
        message: str | None = None,
        available_at: str | None = None,
    ) -> None:
        self._emit(
            handler,
            kind,
            task.run_id,
            self._queue.progress(task.run_id),
            task,
            message,
            available_at,
        )

    @staticmethod
    def _emit(
        handler: EventHandler,
        kind: str,
        run_id: str,
        progress: RunProgress,
        task: CrawlTask | None = None,
        message: str | None = None,
        available_at: str | None = None,
    ) -> None:
        handler(
            CrawlEvent(
                kind=kind,
                run_id=run_id,
                progress=progress,
                index_code=None if task is None else task.index_code,
                endpoint=None if task is None else task.endpoint,
                message=message,
                available_at=available_at,
            )
        )


def _endpoint_key(code: str, endpoint: str) -> str:
    return f"{code}|{endpoint}"


def _run_state_key(run_id: str) -> str:
    return f"{_RUN_STATE_PREFIX}{run_id}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
