"""Command-line adapter for the local CSIndex data services.

The CLI intentionally contains no crawling, querying, or export business
logic.  It only turns arguments into the existing service objects and maps
expected failures to stable process exit codes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Sequence

from .config import AppConfig
from .crawler import CrawlControl, Crawler
from .csindex_client import (
    BlockedError,
    CsindexClient,
    CsindexClientError,
    NetworkError,
    NotFoundError,
    ResponseFormatError,
)
from .db import Database
from .excel_exporter import ExcelExporter
from .models import ScopeSelection, UpdateMode
from .query_service import QueryService


CONFIG_EXIT = 2
NETWORK_EXIT = 3
DATABASE_EXIT = 4
STOPPED_EXIT = 130
ROOT_ENV_VARS = (
    "CSINDEX_LOCAL_ROOT",
    "CSINDEX_ROOT",
    "CSINDEX_APP_ROOT",
    "CSINDEX_DATA_ROOT",
)


class CliUsageError(ValueError):
    """An argument error that should be returned instead of raising SystemExit."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


@dataclass(frozen=True)
class _Context:
    root: Path
    config: AppConfig
    database: Database


def _source_root() -> Path:
    """Locate the project directory in source mode or the executable folder."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # cli.py lives below <project>/src/csindex_local.
    return Path(__file__).resolve().parents[2]


def _resolve_root(value: str | os.PathLike[str] | None) -> Path:
    if value is not None:
        return _validate_storage_path(Path(value).expanduser().resolve(), "程序根目录")
    for name in ROOT_ENV_VARS:
        configured = os.environ.get(name)
        if configured:
            return _validate_storage_path(
                Path(configured).expanduser().resolve(), "程序根目录"
            )
    return _validate_storage_path(_source_root(), "程序根目录")


def _validate_storage_path(path: Path, label: str) -> Path:
    """Reject C: storage paths before any directory or SQLite operation."""

    drive = path.drive.upper().replace("\\?\\", "")
    if drive == "C:":
        raise CliUsageError(
            f"{label}位于 C 盘: {path}。请将程序/路径移到 E 盘或其他非 C 盘后重试。"
        )
    return path


def _database_path(config: AppConfig) -> Path:
    return Path(config.data_dir) / "csindex.db"


def _load_context(root: Path, *, initialize: bool = False) -> _Context:
    root = _validate_storage_path(root.resolve(), "程序根目录")
    config_path = root / "config.json"
    if not config_path.exists():
        if not initialize:
            raise CliUsageError(f"配置文件不存在: {config_path}")
        config = AppConfig.default(root)
        config.save(config_path)
    else:
        config = AppConfig.load(config_path)
    # Hand-written configs may use relative data/export directories. Resolve
    # them against the injected application root instead of the caller's CWD.
    data_dir = Path(config.data_dir)
    export_dir = Path(config.export_dir)
    # Check drive-qualified relative Windows paths (for example C:folder)
    # before joining them to the application root.
    _validate_storage_path(data_dir, "数据目录")
    _validate_storage_path(export_dir, "导出目录")
    if not data_dir.is_absolute():
        config.data_dir = str(root / data_dir)
    if not export_dir.is_absolute():
        config.export_dir = str(root / export_dir)
    config.data_dir = str(
        _validate_storage_path(Path(config.data_dir).expanduser().resolve(), "数据目录")
    )
    config.export_dir = str(
        _validate_storage_path(Path(config.export_dir).expanduser().resolve(), "导出目录")
    )
    if initialize:
        config.ensure_directories()
    database = Database(_database_path(config))
    if initialize:
        database.initialize()
    return _Context(root, config, database)


def _client_and_crawler(context: _Context) -> Crawler:
    client = CsindexClient(timeout_seconds=context.config.http_timeout_seconds)
    return Crawler(client, context.database, config=context.config)


def _scope_selection(raw: str, root: Path) -> tuple[ScopeSelection, str, int | None]:
    """Parse a user scope and return (selection, stable id, expected count)."""

    value = raw.strip()
    if value.lower() == "all":
        return ScopeSelection("all", 0), "fixed:all", None
    if value.isdecimal():
        count = int(value)
        if count < 1:
            raise CliUsageError("scope 必须是正整数、all 或代码文件")
        return ScopeSelection("fixed_count", count), f"fixed:{count}", count

    # Export/status tooling may receive the already-normalized ID emitted by
    # crawl.  It is deliberately accepted without touching the code file.
    fixed_match = re.fullmatch(r"fixed:(all|[1-9][0-9]*)", value, re.IGNORECASE)
    if fixed_match:
        count_text = fixed_match.group(1).lower()
        if count_text == "all":
            return ScopeSelection("all", 0), "fixed:all", None
        count = int(count_text)
        return ScopeSelection("fixed_count", count), f"fixed:{count}", count
    if re.fullmatch(r"codes:[0-9a-fA-F]{12,64}", value):
        return ScopeSelection("codes", tuple()), value.lower(), None

    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise CliUsageError(f"代码文件不存在: {path}")
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
    except OSError as exc:
        raise CliUsageError(f"无法读取代码文件: {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CliUsageError(f"代码文件必须是 UTF-8 文本: {path}") from exc
    codes = tuple(line.strip() for line in text.splitlines() if line.strip())
    if not codes:
        raise CliUsageError("代码文件不能为空")
    if any(any(ch.isspace() for ch in code) for code in codes):
        raise CliUsageError("代码文件每行只能包含一个指数代码")
    digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
    scope_id = f"codes:{digest}"
    return ScopeSelection("codes", codes, scope_id=scope_id), scope_id, len(codes)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="csindex-local")
    parser.add_argument(
        "--root",
        help="程序根目录（也可用 CSINDEX_LOCAL_ROOT 环境变量）",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_ArgumentParser
    )

    subparsers.add_parser("init")

    crawl = subparsers.add_parser("crawl")
    crawl.add_argument("--scope", required=True)
    crawl.add_argument("--mode", choices=[mode.value for mode in UpdateMode], required=True)
    crawl.add_argument("--dry-run", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")

    export = subparsers.add_parser("export")
    export.add_argument("--scope", required=True)
    export.add_argument("--sort", choices=["one_year"], default="one_year")
    export.add_argument("--limit", choices=["all", "100", "500"], default="all")
    export.add_argument("--output", help="可选的 xlsx 输出路径")

    subparsers.add_parser("reset-running")
    return parser


def _extract_root_argument(argv: Sequence[str] | None) -> tuple[str | None, list[str] | None]:
    """Permit the test/runtime root option before or after a subcommand."""

    if argv is None:
        return None, None
    remaining: list[str] = []
    root: str | None = None
    index = 0
    values = list(argv)
    while index < len(values):
        argument = values[index]
        if argument == "--root":
            if index + 1 >= len(values):
                raise CliUsageError("--root 需要一个目录参数")
            root = values[index + 1]
            index += 2
            continue
        if argument.startswith("--root="):
            root = argument.split("=", 1)[1]
            if not root:
                raise CliUsageError("--root 需要一个目录参数")
            index += 1
            continue
        remaining.append(argument)
        index += 1
    return root, remaining


def _status(context: _Context, as_json: bool) -> int:
    try:
        database_path = _database_path(context.config)
        if not database_path.is_file():
            raise RuntimeError(f"数据库不存在: {database_path}")
        with context.database._connection() as connection:
            counts = {
                "indices": connection.execute(
                    "SELECT COUNT(*) FROM indices WHERE is_active = 1"
                ).fetchone()[0],
                "snapshots": connection.execute(
                    "SELECT COUNT(*) FROM metric_snapshots"
                ).fetchone()[0],
                "pending_tasks": connection.execute(
                    """SELECT COUNT(*) FROM crawl_tasks
                       WHERE status IN ('pending', 'running', 'retry_wait', 'blocked_wait')"""
                ).fetchone()[0],
                "running_tasks": connection.execute(
                    "SELECT COUNT(*) FROM crawl_tasks WHERE status = 'running'"
                ).fetchone()[0],
                "failed_tasks": connection.execute(
                    "SELECT COUNT(*) FROM crawl_tasks WHERE status = 'failed'"
                ).fetchone()[0],
            }
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    if as_json:
        print(json.dumps(counts, ensure_ascii=False))
    else:
        print("指数: {indices}，快照: {snapshots}，待处理任务: {pending_tasks}".format(**counts))
    return 0


def _dry_run(context: _Context, scope: ScopeSelection, scope_id: str, expected: int | None) -> int:
    try:
        with context.database._connection() as connection:
            available = connection.execute(
                "SELECT COUNT(*) FROM indices WHERE is_active = 1"
            ).fetchone()[0]
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    selected = expected if expected is not None else available
    if scope.kind == "fixed_count":
        selected = min(selected, available) if available else selected
    print(f"将创建 {selected * 2} 个抓取任务，范围 {scope_id}（dry-run）")
    return 0


def _crawl(context: _Context, raw_scope: str, mode: str, dry_run: bool) -> int:
    selection, scope_id, expected = _scope_selection(raw_scope, context.root)
    if dry_run:
        return _dry_run(context, selection, scope_id, expected)
    crawler = _client_and_crawler(context)
    run_id = crawler.prepare_run(selection, UpdateMode(mode))
    print(f"已创建抓取运行 {run_id}，范围 {scope_id}")
    control = CrawlControl()
    progress = crawler.run(run_id, control, lambda event: None)
    print(
        f"抓取完成：成功 {progress.success_tasks}，失败 {progress.failed_tasks}，"
        f"待处理 {progress.pending_tasks}"
    )
    return 0


def _export(context: _Context, raw_scope: str, sort: str, limit: str, output: str | None) -> int:
    _, scope_id, _ = _scope_selection(raw_scope, context.root)
    ranking_limit = None if limit == "all" else int(limit)
    raw_output = Path(output).expanduser() if output else None
    if raw_output is not None:
        _validate_storage_path(raw_output, "导出文件")
    output_path = (
        (raw_output if raw_output.is_absolute() else context.root / raw_output)
        if raw_output is not None
        else Path(context.config.export_dir) / f"{scope_id.replace(':', '_')}_{sort}.xlsx"
    )
    output_path = _validate_storage_path(output_path.expanduser().resolve(), "导出文件")
    exported = ExcelExporter(context.database).export(scope_id, output_path, ranking_limit)
    print(f"已导出: {exported}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command and return its process exit code."""

    try:
        parser = _build_parser()
        raw_argv: Sequence[str] = sys.argv[1:] if argv is None else argv
        extracted_root, parse_argv = _extract_root_argument(raw_argv)
        args = parser.parse_args(parse_argv)
        root = _resolve_root(extracted_root or args.root)
        if args.command == "init":
            _load_context(root, initialize=True)
            print(f"初始化完成: {root}")
            return 0
        context = _load_context(root)
        if args.command == "status":
            return _status(context, args.as_json)
        if args.command == "reset-running":
            recovered = context.database.recover_interrupted_tasks()
            print(f"已重置 {recovered} 个运行中任务")
            return 0
        if args.command == "crawl":
            return _crawl(context, args.scope, args.mode, args.dry_run)
        if args.command == "export":
            return _export(context, args.scope, args.sort, args.limit, args.output)
        raise CliUsageError(f"未知命令: {args.command}")
    except KeyboardInterrupt:
        print("用户停止操作", file=sys.stderr)
        return STOPPED_EXIT
    except SystemExit as exc:
        # argparse uses SystemExit for --help (0) and a few parser-level
        # failures.  The library-facing main() contract must always return an
        # integer instead of terminating the embedding process.
        code = exc.code
        return code if isinstance(code, int) else CONFIG_EXIT
    except CliUsageError as exc:
        print(f"配置/参数错误: {exc}", file=sys.stderr)
        return CONFIG_EXIT
    except (NetworkError, BlockedError, NotFoundError, ResponseFormatError, CsindexClientError) as exc:
        print(f"网络准备失败: {exc}", file=sys.stderr)
        return NETWORK_EXIT
    except (OSError, ValueError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return CONFIG_EXIT
    except Exception as exc:
        print(f"数据库或运行错误: {exc}", file=sys.stderr)
        return DATABASE_EXIT


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    raise SystemExit(main())
