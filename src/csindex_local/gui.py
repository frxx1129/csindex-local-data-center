"""Tkinter dashboard for the local CSIndex crawler.

The crawler owns all network and persistence work.  This module deliberately
keeps Tk on its main thread: worker threads can only put immutable
``CrawlEvent`` objects onto ``event_queue``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from .config import AppConfig
from .crawler import CrawlControl, Crawler
from .db import Database
from .excel_exporter import ExcelExporter
from .models import CrawlEvent, RunProgress, ScopeSelection, UpdateMode


_SYNTHETIC_PROGRESS = RunProgress(-1, -1, -1, -1)
_COUNTS_ROW = 4
_CURRENT_ROW = 5
_DETAILS_ROW = 6


def enqueue_event(events: Queue[CrawlEvent], event: CrawlEvent) -> None:
    """Thread-safe event sink used by crawler and GUI worker threads."""

    events.put(event)


@dataclass(frozen=True)
class UiState:
    """Pure, testable projection of crawler events for the dashboard."""

    status_text: str
    progress_value: int
    progress_max: int
    success_count: int
    failed_count: int
    pending_count: int
    current_index: str
    current_endpoint: str
    cooldown_seconds: int
    cooldown_until: datetime | None
    eta_text: str
    logs: tuple[str, ...]
    can_start: bool
    can_pause: bool
    can_resume: bool
    can_stop: bool
    can_export: bool

    @classmethod
    def initial(cls) -> "UiState":
        return cls(
            status_text="准备就绪",
            progress_value=0,
            progress_max=0,
            success_count=0,
            failed_count=0,
            pending_count=0,
            current_index="--",
            current_endpoint="--",
            cooldown_seconds=0,
            cooldown_until=None,
            eta_text="--",
            logs=(),
            can_start=True,
            can_pause=False,
            can_resume=False,
            can_stop=False,
            can_export=True,
        )

    def reduce(self, event: CrawlEvent, *, now: datetime | None = None) -> "UiState":
        """Return a new state; this method never accesses Tk or a worker."""

        current_time = _as_utc(now)
        progress = event.progress
        current_index = event.index_code or self.current_index
        current_endpoint = _endpoint_label(event.endpoint) if event.endpoint else self.current_endpoint
        if _has_real_progress(progress):
            total = max(0, progress.total_tasks)
            # The dashboard's primary progress is successful endpoint
            # collection; failures stay visible in their own counter.
            next_state = replace(
                self,
                progress_value=min(total, progress.success_tasks),
                progress_max=total,
                success_count=progress.success_tasks,
                failed_count=progress.failed_tasks,
                pending_count=progress.pending_tasks,
                current_index=current_index,
                current_endpoint=current_endpoint,
                eta_text=_eta_text(progress.pending_tasks),
            )
        else:
            # UI/worker/export notifications do not own crawler progress.
            # Retain the latest real counts instead of replacing them with 0.
            next_state = replace(
                self,
                current_index=current_index,
                current_endpoint=current_endpoint,
            )

        kind = event.kind
        if kind in {"ui_started", "run_prepared", "task_started"}:
            next_state = replace(
                next_state,
                status_text="正在抓取",
                cooldown_seconds=0,
                cooldown_until=None,
                can_start=False,
                can_pause=True,
                can_resume=False,
                can_stop=True,
                can_export=False,
            )
        elif kind == "ui_paused":
            next_state = replace(
                next_state,
                status_text="已暂停",
                can_pause=False,
                can_resume=True,
                can_stop=True,
            )
        elif kind == "ui_resumed":
            next_state = replace(
                next_state,
                status_text="正在抓取",
                can_pause=True,
                can_resume=False,
                can_stop=True,
            )
        elif kind == "ui_stopped":
            next_state = replace(
                next_state,
                status_text="正在停止",
                can_pause=False,
                can_resume=False,
                can_stop=False,
            )
        elif kind == "task_blocked":
            deadline = _parse_available_at(event.available_at)
            next_state = replace(
                next_state,
                status_text="官网限流冷却中",
                cooldown_seconds=_seconds_until(deadline, current_time),
                cooldown_until=deadline,
                can_pause=False,
                can_resume=False,
                can_stop=True,
            )
        elif kind == "task_retry":
            next_state = replace(next_state, status_text="请求稍后重试")
        elif kind == "run_waiting":
            next_state = replace(
                next_state,
                status_text="等待重试或冷却结束",
                can_start=True,
                can_pause=False,
                can_resume=False,
                can_stop=False,
                can_export=True,
            )
        elif kind in {"run_completed", "run_completed_with_failures"}:
            next_state = replace(
                next_state,
                status_text="抓取完成" if kind == "run_completed" else "抓取完成（含失败项）",
                cooldown_seconds=0,
                cooldown_until=None,
                can_start=True,
                can_pause=False,
                can_resume=False,
                can_stop=False,
                can_export=True,
                eta_text="0 秒",
            )
        elif kind == "run_stopped":
            next_state = replace(
                next_state,
                status_text="已停止",
                can_start=True,
                can_pause=False,
                can_resume=False,
                can_stop=False,
                can_export=True,
            )
        elif kind == "worker_failed":
            next_state = replace(
                next_state,
                status_text="操作失败",
                can_start=True,
                can_pause=False,
                can_resume=False,
                can_stop=False,
                can_export=True,
            )

        message = _event_log(event)
        if message:
            next_state = replace(next_state, logs=(next_state.logs + (message,))[-200:])
        return next_state

    def tick(self, now: datetime | None = None) -> "UiState":
        """Advance the displayed cooldown with the Tk main-loop clock only."""

        if self.cooldown_until is None:
            return self
        remaining = _seconds_until(self.cooldown_until, _as_utc(now))
        if remaining == self.cooldown_seconds:
            return self
        status = self.status_text
        if remaining == 0 and status == "官网限流冷却中":
            status = "冷却结束，等待恢复抓取"
        return replace(self, cooldown_seconds=remaining, status_text=status)


@dataclass(frozen=True)
class GuiServices:
    config: AppConfig
    database: Database
    crawler: Crawler
    exporter: ExcelExporter


class AppWindow:
    """A small Windows dashboard; all widget changes happen on Tk's thread."""

    def __init__(self, root: tk.Misc, services: GuiServices | None) -> None:
        self.root = root
        self.services = services
        self.event_queue: Queue[CrawlEvent] = Queue()
        self.state = UiState.initial()
        self._control: CrawlControl | None = None
        self._worker: threading.Thread | None = None
        self._closing_since: float | None = None

        self.scope_var = tk.StringVar(master=root, value="1000")
        self.mode_var = tk.StringVar(master=root, value="仅缺失")
        self.status_var = tk.StringVar(master=root)
        self.counts_var = tk.StringVar(master=root)
        self.current_var = tk.StringVar(master=root)
        self.cooldown_var = tk.StringVar(master=root)
        self.eta_var = tk.StringVar(master=root)
        self._build_widgets()
        self._render()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll_events)

    def _build_widgets(self) -> None:
        self.root.title("中证指数本地数据中心")
        self.root.minsize(900, 520)
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(_DETAILS_ROW, weight=1)

        ttk.Label(frame, text="抓取范围：").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            frame,
            textvariable=self.scope_var,
            values=("1000", "2000", "全部"),
            state="normal",
            width=12,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text="可手动输入任意正整数").grid(
            row=0, column=2, sticky="w", padx=(8, 18)
        )
        ttk.Label(frame, text="更新方式：").grid(row=0, column=3, sticky="w")
        ttk.Combobox(frame, textvariable=self.mode_var, values=("仅缺失", "更新当天", "强制刷新"), state="readonly", width=12).grid(row=0, column=4, sticky="w")

        actions = ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=5, sticky="w", pady=(10, 10))
        self.start_button = ttk.Button(actions, text="开始", command=self.start)
        self.pause_button = ttk.Button(actions, text="暂停", command=self.pause)
        self.resume_button = ttk.Button(actions, text="继续", command=self.resume)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop)
        self.export_button = ttk.Button(actions, text="导出 Excel", command=self.export)
        for button in (self.start_button, self.pause_button, self.resume_button, self.stop_button, self.export_button):
            button.pack(side="left", padx=(0, 8))

        ttk.Label(frame, textvariable=self.status_var, font=("Microsoft YaHei UI", 11, "bold")).grid(row=2, column=0, columnspan=5, sticky="w")
        self.progress = ttk.Progressbar(frame, orient="horizontal", mode="determinate")
        self.progress.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 4))
        ttk.Label(frame, textvariable=self.counts_var).grid(row=_COUNTS_ROW, column=0, columnspan=5, sticky="w")
        ttk.Label(frame, textvariable=self.current_var).grid(row=_CURRENT_ROW, column=0, columnspan=5, sticky="w", pady=(2, 0))

        details = ttk.Frame(frame)
        details.grid(row=_DETAILS_ROW, column=0, columnspan=5, sticky="nsew", pady=(10, 0))
        details.columnconfigure(0, weight=1)
        details.rowconfigure(2, weight=1)
        ttk.Label(details, textvariable=self.cooldown_var).grid(row=0, column=0, sticky="w")
        ttk.Label(details, textvariable=self.eta_var).grid(row=1, column=0, sticky="w")
        self.log_text = tk.Text(details, height=12, state="disabled", wrap="word")
        self.log_text.grid(row=2, column=0, sticky="nsew", pady=(6, 0))

    def start(self) -> None:
        if self.services is None:
            self.start_demo()
            return
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            selection = _scope_selection(self.scope_var.get())
        except ValueError as exc:
            messagebox.showerror("范围输入错误", str(exc))
            return
        self._control = CrawlControl()
        self.event_queue.put(_ui_event("ui_started", progress=RunProgress(0, 0, 0, 0)))
        mode = _mode_selection(self.mode_var.get())
        self._worker = threading.Thread(
            target=self._crawl_worker,
            args=(selection, mode),
            name="csindex-crawl-worker",
            daemon=False,
        )
        self._worker.start()

    def _crawl_worker(self, selection: ScopeSelection, mode: UpdateMode) -> None:
        assert self.services is not None
        assert self._control is not None
        try:
            run_id = self.services.crawler.prepare_run(selection, mode)
            self.event_queue.put(_ui_event("run_prepared", run_id=run_id))
            self.services.crawler.run(run_id, self._control, lambda item: enqueue_event(self.event_queue, item))
        except Exception as exc:
            self.event_queue.put(_ui_event("worker_failed", message=worker_error_message("抓取", exc)))

    def pause(self) -> None:
        if self._control is not None:
            self._control.pause()
            self.event_queue.put(_ui_event("ui_paused"))

    def resume(self) -> None:
        if self._control is not None:
            self._control.resume()
            self.event_queue.put(_ui_event("ui_resumed"))

    def stop(self) -> None:
        if self._control is not None:
            self._control.stop()
            self.event_queue.put(_ui_event("ui_stopped"))

    def export(self) -> None:
        if self.services is None:
            return
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("暂不可导出", "抓取进行中，请完成、停止或暂停到安全状态后再导出。")
            return
        try:
            scope_id = _scope_id(self.scope_var.get())
        except ValueError as exc:
            messagebox.showerror("范围输入错误", str(exc))
            return
        output = Path(self.services.config.export_dir) / f"{scope_id.replace(':', '_')}_one_year.xlsx"
        worker = threading.Thread(
            target=self._export_worker,
            args=(scope_id, output),
            name="csindex-export-worker",
            daemon=False,
        )
        worker.start()

    def _export_worker(self, scope_id: str, output: Path) -> None:
        assert self.services is not None
        try:
            result = self.services.exporter.export(scope_id, output, None)
            self.event_queue.put(_ui_event("exported", message=f"已导出：{result}"))
        except Exception as exc:
            self.event_queue.put(_ui_event("worker_failed", message=worker_error_message("导出", exc)))

    def start_demo(self) -> None:
        """Schedule offline fake events; it creates neither HTTP clients nor requests."""

        base = RunProgress(4, 0, 0, 4)
        later = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1800)
        ).isoformat()
        events = (
            (0, _ui_event("ui_started", progress=base)),
            (300, _ui_event("task_started", progress=base, index_code="000300", endpoint="yield")),
            (600, _ui_event("ui_paused", progress=base)),
            (900, _ui_event("ui_resumed", progress=base)),
            (1200, _ui_event("task_blocked", progress=base, index_code="000300", endpoint="yield", available_at=later, message="演示冷却")),
            (1800, _ui_event("task_success", progress=RunProgress(4, 3, 0, 1), index_code="000300", endpoint="volatility")),
            (2200, _ui_event("run_completed", progress=RunProgress(4, 4, 0, 0))),
        )
        for delay, item in events:
            self.root.after(delay, lambda value=item: self.event_queue.put(value))

    def _poll_events(self) -> None:
        try:
            while True:
                self._apply_event(self.event_queue.get_nowait())
        except Empty:
            pass
        ticked = self.state.tick()
        if ticked is not self.state:
            self.state = ticked
            self._render()
        if self._closing_since is not None:
            self._check_shutdown()
        else:
            self.root.after(100, self._poll_events)

    def _apply_event(self, event: CrawlEvent) -> None:
        self.state = self.state.reduce(event)
        self._render()
        if event.kind == "worker_failed":
            messagebox.showerror("操作失败", display_error_message(event.message))
        elif event.kind == "exported":
            messagebox.showinfo("导出完成", event.message or "Excel 已导出。")

    def _render(self) -> None:
        state = self.state
        self.status_var.set(f"状态：{state.status_text}")
        self.counts_var.set(f"成功：{state.success_count}    失败：{state.failed_count}    待处理：{state.pending_count}")
        self.current_var.set(f"当前指数：{state.current_index}    当前接口：{state.current_endpoint}")
        self.cooldown_var.set(f"冷却倒计时：{_seconds_text(state.cooldown_seconds)}")
        self.eta_var.set(f"预计剩余时间：{state.eta_text}")
        self.progress.configure(maximum=max(1, state.progress_max), value=state.progress_value)
        for button, enabled in (
            (self.start_button, state.can_start),
            (self.pause_button, state.can_pause),
            (self.resume_button, state.can_resume),
            (self.stop_button, state.can_stop),
            (self.export_button, state.can_export),
        ):
            button.configure(state="normal" if enabled else "disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(state.logs))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def close(self) -> None:
        if self._closing_since is not None:
            return
        if self._control is not None:
            self._control.stop()
        if self._worker is None or not self._worker.is_alive():
            self.root.destroy()
            return
        self._closing_since = time.monotonic()
        self.status_var.set("状态：正在安全退出，等待当前请求结束…")
        self.root.after(100, self._poll_events)

    def _check_shutdown(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self.root.destroy()
            return
        assert self._closing_since is not None
        if time.monotonic() - self._closing_since >= 25:
            self.status_var.set("状态：正在安全退出，当前请求尚未结束，请稍候…")
        self.root.after(100, self._poll_events)


def _ui_event(kind: str, *, run_id: str = "", progress: RunProgress | None = None, **kwargs: object) -> CrawlEvent:
    return CrawlEvent(kind=kind, run_id=run_id, progress=progress if progress is not None else _SYNTHETIC_PROGRESS, **kwargs)


def _scope_selection(value: str) -> ScopeSelection:
    normalized = value.strip()
    if normalized.lower() in {"全部", "all"}:
        return ScopeSelection("all", 0)
    if not normalized.isdecimal() or int(normalized) < 1:
        raise ValueError("抓取数量必须是正整数，或填写“全部”。")
    return ScopeSelection("fixed_count", int(normalized))


def _scope_id(value: str) -> str:
    selection = _scope_selection(value)
    return "fixed:all" if selection.kind == "all" else f"fixed:{selection.value}"


def _mode_selection(value: str) -> UpdateMode:
    return {"仅缺失": UpdateMode.MISSING, "更新当天": UpdateMode.UPDATE, "强制刷新": UpdateMode.FORCE}[value]


def _endpoint_label(endpoint: str | None) -> str:
    return {"yield": "收益率", "volatility": "年化波动率"}.get(endpoint or "", endpoint or "--")


def _parse_available_at(available_at: str | None) -> datetime | None:
    if not available_at:
        return None
    try:
        target = datetime.fromisoformat(available_at.replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target.astimezone(timezone.utc)
    except ValueError:
        return None


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _seconds_until(deadline: datetime | None, now: datetime) -> int:
    if deadline is None:
        return 0
    return max(0, int((deadline - now).total_seconds()))


def _has_real_progress(progress: RunProgress) -> bool:
    return progress.total_tasks >= 0


def _eta_text(pending: int) -> str:
    return _seconds_text(pending * 8) if pending else "0 秒"


def _seconds_text(seconds: int) -> str:
    if seconds <= 0:
        return "0 秒"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def _event_log(event: CrawlEvent) -> str:
    labels = {
        "ui_started": "开始抓取",
        "ui_paused": "已暂停",
        "ui_resumed": "继续抓取",
        "ui_stopped": "请求停止",
        "run_prepared": "抓取任务已创建",
        "task_started": "请求开始",
        "task_success": "请求成功",
        "task_failed": "请求失败",
        "task_retry": "请求将重试",
        "task_blocked": "官网限流，进入冷却",
        "run_completed": "抓取完成",
        "run_completed_with_failures": "抓取完成，含失败项",
        "run_waiting": "任务等待重试或冷却",
        "run_stopped": "任务已停止",
        "worker_failed": "后台操作失败",
        "exported": "导出完成",
    }
    label = labels.get(event.kind)
    if not label:
        return ""
    target = " ".join(item for item in (event.index_code, _endpoint_label(event.endpoint) if event.endpoint else None) if item)
    details = event.message or ""
    return "：".join(item for item in (label, target, details) if item)


def worker_error_message(operation: str, error: Exception) -> str:
    """Give background failures a Chinese user-facing context, retaining detail."""

    detail = str(error).strip() or error.__class__.__name__
    return f"后台{operation}失败：{detail}"


def display_error_message(message: str | None) -> str:
    if not message:
        return "后台操作失败：发生未知错误。"
    return message if message.startswith("后台") else f"后台操作失败：{message}"
