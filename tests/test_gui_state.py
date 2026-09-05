from __future__ import annotations

from datetime import datetime, timedelta, timezone
from queue import Queue

import pytest

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


def test_synthetic_ui_events_keep_existing_crawler_progress() -> None:
    from csindex_local.gui import _ui_event, UiState

    state = UiState.initial().reduce(
        event("task_success", progress=RunProgress(400, 120, 2, 278))
    )
    paused = state.reduce(_ui_event("ui_paused"))
    resumed = paused.reduce(_ui_event("ui_resumed"))
    stopped = resumed.reduce(_ui_event("ui_stopped"))

    for result in (paused, resumed, stopped):
        assert (result.progress_value, result.progress_max) == (120, 400)
        assert (result.success_count, result.failed_count, result.pending_count) == (
            120,
            2,
            278,
        )


def test_cooldown_tick_counts_down_without_sleep() -> None:
    from csindex_local.gui import UiState

    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    state = UiState.initial().reduce(
        event("task_blocked", available_at=(now + timedelta(seconds=5)).isoformat()),
        now=now,
    )
    ticking = state.tick(now + timedelta(seconds=2))
    expired = ticking.tick(now + timedelta(seconds=6))

    assert ticking.cooldown_seconds == 3
    assert expired.cooldown_seconds == 0
    assert expired.status_text == "冷却结束，等待恢复抓取"


def test_worker_error_message_has_chinese_context_and_keeps_detail() -> None:
    from csindex_local.gui import worker_error_message

    text = worker_error_message("抓取", RuntimeError("connection reset"))

    assert text == "后台抓取失败：connection reset"


def test_dashboard_rows_do_not_overlap() -> None:
    from csindex_local.gui import _COUNTS_ROW, _CURRENT_ROW, _DETAILS_ROW

    assert _COUNTS_ROW < _CURRENT_ROW < _DETAILS_ROW


@pytest.mark.parametrize("value,count", [("1", 1), ("1500", 1500), (" 2000 ", 2000)])
def test_manual_positive_scope_is_accepted(value: str, count: int) -> None:
    from csindex_local.gui import _scope_id, _scope_selection

    selection = _scope_selection(value)

    assert selection.kind == "fixed_count"
    assert selection.value == count
    assert _scope_id(value) == f"fixed:{count}"


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "一千"])
def test_invalid_manual_scope_has_clear_error(value: str) -> None:
    from csindex_local.gui import _scope_selection

    with pytest.raises(ValueError, match="正整数"):
        _scope_selection(value)


@pytest.mark.parametrize("value", ["全部", "all", "ALL"])
def test_all_scope_aliases_are_accepted(value: str) -> None:
    from csindex_local.gui import _scope_id, _scope_selection

    assert _scope_selection(value).kind == "all"
    assert _scope_id(value) == "fixed:all"
