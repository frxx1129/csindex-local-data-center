"""Offline contract tests for the command-line adapter."""

import json
from pathlib import Path

import pytest

from csindex_local import cli
from csindex_local.config import AppConfig
from csindex_local.db import Database
from csindex_local.models import IndexRecord


@pytest.fixture
def initialized_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = AppConfig.default(tmp_path)
    config.save(tmp_path / "config.json")
    database = Database(Path(config.data_dir) / "csindex.db")
    database.initialize()
    database.upsert_indices(
        [IndexRecord("000300", "沪深300", "是", {}), IndexRecord("000905", "中证500", "是", {})]
    )
    monkeypatch.setenv("CSINDEX_LOCAL_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def cli_runner(monkeypatch: pytest.MonkeyPatch):
    def run(argv: list[str]):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(argv)
        return type(
            "Result",
            (),
            {"exit_code": exit_code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()},
        )()

    return run


def test_status_returns_json(cli_runner, initialized_app):
    result = cli_runner(["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {"indices", "snapshots", "pending_tasks"} <= payload.keys()


def test_crawl_accepts_2000_scope(cli_runner, initialized_app):
    result = cli_runner(["crawl", "--scope", "2000", "--mode", "missing", "--dry-run"])
    assert result.exit_code == 0
    assert "将创建" in result.stdout


def test_code_file_scope_uses_content_digest(tmp_path: Path):
    code_file = tmp_path / "codes.txt"
    code_file.write_text("000300\n000905\n", encoding="utf-8")
    selection, scope_id, expected = cli._scope_selection(str(code_file), tmp_path)
    assert scope_id.startswith("codes:")
    assert len(scope_id.split(":", 1)[1]) == 12
    assert selection.value == ("000300", "000905")
    assert expected == 2


def test_reset_running_is_offline(cli_runner, initialized_app):
    result = cli_runner(["reset-running"])
    assert result.exit_code == 0
    assert "重置" in result.stdout


def test_help_and_invalid_arguments_return_codes(cli_runner):
    help_result = cli_runner(["--help"])
    invalid_result = cli_runner(["not-a-command"])
    assert help_result.exit_code == 0
    assert invalid_result.exit_code == 2


def test_c_drive_root_is_rejected_before_initialization(cli_runner):
    result = cli_runner(["init", "--root", r"C:\csindex-local-test"])
    assert result.exit_code == 2
    assert "移到 E 盘或其他非 C 盘" in result.stderr


@pytest.mark.parametrize(
    "raw_path",
    [
        r"\\?\C:\csindex-local-test",
        r"\\localhost\c$\csindex-local-test",
        r"\\127.0.0.1\c$\csindex-local-test",
        r"\\.\c$\csindex-local-test",
        r"\\?\UNC\localhost\c$\csindex-local-test",
    ],
)
def test_local_c_aliases_are_rejected(raw_path: str):
    with pytest.raises(cli.CliUsageError, match="移到 E 盘或其他非 C 盘"):
        cli._validate_storage_path(Path(raw_path), "程序根目录")


def test_remote_shares_are_not_mistaken_for_local_c_drive():
    assert not cli._is_local_c_path(Path(r"\\remote-host\share\folder"))
    assert not cli._is_local_c_path(Path(r"\\remote-host\c$\folder"))


def test_nuitka_build_uses_real_executable_directory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "__compiled__", object(), raising=False)
    monkeypatch.setattr(cli.sys, "executable", r"E:\apps\csindex\csindex.exe")

    assert cli._source_root() == Path(r"E:\apps\csindex")


def test_c_drive_config_directories_are_rejected(cli_runner, tmp_path: Path):
    config = AppConfig.default(tmp_path)
    config.data_dir = r"C:\csindex-local-data"
    config.save(tmp_path / "config.json")
    result = cli_runner(["status", "--root", str(tmp_path)])
    assert result.exit_code == 2
    assert "数据目录位于 C 盘" in result.stderr


def test_c_drive_explicit_export_is_rejected(cli_runner, initialized_app, monkeypatch):
    class UnexpectedExporter:
        def __init__(self, database):
            raise AssertionError("C 盘输出路径应在创建导出器前被拒绝")

    monkeypatch.setattr(cli, "ExcelExporter", UnexpectedExporter)
    result = cli_runner(
        [
            "export",
            "--scope",
            "fixed:1",
            "--output",
            r"C:\csindex-local-export.xlsx",
        ]
    )
    assert result.exit_code == 2
    assert "导出文件位于 C 盘" in result.stderr


def test_status_missing_database_returns_four_without_creating_it(
    cli_runner, tmp_path: Path
):
    config = AppConfig.default(tmp_path)
    config.save(tmp_path / "config.json")
    database_path = Path(config.data_dir) / "csindex.db"
    assert not database_path.exists()
    result = cli_runner(["status", "--json", "--root", str(tmp_path)])
    assert result.exit_code == 4
    assert not database_path.exists()
