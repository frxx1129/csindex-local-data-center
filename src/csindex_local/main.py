"""Windows GUI entry point for the local CSIndex data center."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk

from .cli import CONFIG_EXIT, _load_context, _resolve_root
from .crawler import Crawler
from .csindex_client import CsindexClient
from .excel_exporter import ExcelExporter
from .gui import AppWindow, GuiServices


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csindex-local-gui")
    parser.add_argument("--root", help="程序根目录；不得位于本机 C 盘")
    parser.add_argument("--demo", action="store_true", help="离线演示界面状态，不访问官网")
    return parser


def _services(root: Path) -> GuiServices:
    """Use CLI's one authoritative non-C path validation and initialization."""

    context = _load_context(root, initialize=True)
    client = CsindexClient(timeout_seconds=context.config.http_timeout_seconds)
    crawler = Crawler(client, context.database, config=context.config)
    return GuiServices(context.config, context.database, crawler, ExcelExporter(context.database))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        services = None if args.demo else _services(_resolve_root(args.root))
    except (OSError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return CONFIG_EXIT
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        return 1
    window = AppWindow(root, services)
    if args.demo:
        window.start_demo()
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover - executable entry point
    raise SystemExit(main())
