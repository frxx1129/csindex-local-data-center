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
