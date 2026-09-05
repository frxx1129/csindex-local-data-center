from __future__ import annotations

from datetime import datetime, timedelta, timezone
from queue import Queue

from csindex_local.models import CrawlEvent, RunProgress


def event(kind: str, **kwargs) -> CrawlEvent:
    return CrawlEvent(kind=kind, run_id="run-1", progress=kwargs.pop("progress", RunProgress(0, 0, 0, 0)), **kwargs)


def test_blocked_event_shows_cooldown() -> None:
    from csindex_local.gui import UiState

    available_at = (datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat()
    state = UiState.initial().reduce(event("task_blocked", available_at=available_at))

    assert state.status_text == "官网限流冷却中"
    assert 1798 <= state.cooldown_seconds <= 1800
    assert state.can_pause is False


def test_progress_event_updates_counts() -> None:
    from csindex_local.gui import UiState

    state = UiState.initial().reduce(
        event("task_success", progress=RunProgress(4000, 120, 2, 3878), index_code="000300", endpoint="yield")
    )

    assert state.progress_value == 120
    assert state.progress_max == 4000
    assert state.success_count == 120
    assert state.failed_count == 2
    assert state.pending_count == 3878


def test_pause_resume_stop_and_terminal_events_are_pure() -> None:
    from csindex_local.gui import UiState

    state = UiState.initial()
    paused = state.reduce(event("ui_paused"))
    resumed = paused.reduce(event("ui_resumed"))
    stopped = resumed.reduce(event("run_stopped"))
    completed = state.reduce(event("run_completed", progress=RunProgress(2, 2, 0, 0)))

    assert state.status_text == "准备就绪"
    assert paused.status_text == "已暂停"
    assert resumed.status_text == "正在抓取"
    assert stopped.status_text == "已停止"
    assert completed.status_text == "抓取完成"
    assert completed.can_export is True


def test_worker_event_sink_only_enqueues_events() -> None:
    from csindex_local.gui import enqueue_event

    events: Queue[CrawlEvent] = Queue()
    item = event("task_started", index_code="000300", endpoint="yield")
    enqueue_event(events, item)

    assert events.get_nowait() is item
