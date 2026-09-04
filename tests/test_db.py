from pathlib import Path
import sqlite3
import threading
import time

from csindex_local.db import Database
from csindex_local.models import IndexRecord, VolatilitySnapshot, YieldSnapshot


def make_index(code: str = "000300", name: str = "沪深300") -> IndexRecord:
    return IndexRecord(code, name, "是", {"indexSeries": "规模指数"})


def test_snapshot_is_merged_by_code_and_date(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([make_index()])
    db.upsert_yield(YieldSnapshot("000300", "2026-09-03", 1, 2, 3, 4, 5, 6))
    db.merge_volatility(VolatilitySnapshot("000300", "2026-09-03", 7, 8, 9))

    row = db.get_snapshot("000300", "2026-09-03")

    assert row["one_year"] == 4
    assert row["five_year_volatility"] == 9
    assert row["missing_count"] == 0
    assert row["is_complete"] == 1


def test_different_dates_do_not_overwrite(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([make_index()])
    db.upsert_yield(YieldSnapshot("000300", "2026-09-02", 1, 2, 3, 4, 5, 6))
    db.upsert_yield(YieldSnapshot("000300", "2026-09-03", 2, 3, 4, 5, 6, 7))

    assert len(db.list_snapshots("000300")) == 2


def test_initialize_creates_all_specification_tables_and_preserves_list_order(
    tmp_path: Path,
):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([make_index("000300"), make_index("000905")])

    with sqlite3.connect(db.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(indices)")
        }

    assert {
        "indices",
        "crawl_scopes",
        "crawl_scope_members",
        "metric_snapshots",
        "raw_responses",
        "crawl_runs",
        "crawl_tasks",
    }.issubset(tables)
    assert "list_order" in columns
    assert db.get_index("000300")["list_order"] == 0
    assert db.get_index("000905")["list_order"] == 1


def test_scope_keeps_its_original_order_when_requested_again(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([make_index("000300"), make_index("000905")])

    created = db.create_or_get_scope("fixed:2", ["000905", "000300"])
    existing = db.create_or_get_scope("fixed:2", ["000300"])

    assert created.id == "fixed:2"
    assert created.codes == ("000905", "000300")
    assert existing == created


def test_recover_interrupted_tasks_requeues_running_rows(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_indices([make_index()])

    with sqlite3.connect(db.path) as connection:
        connection.execute(
            """
            INSERT INTO crawl_runs (
                id, scope_type, scope_value, status, started_at, total_tasks
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("run-1", "fixed", "1", "running", "2026-09-04T00:00:00+00:00", 1),
        )
        connection.execute(
            """
            INSERT INTO crawl_tasks (
                run_id, index_code, endpoint, status, available_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "run-1",
                "000300",
                "yield",
                "running",
                "2026-09-04T00:00:00+00:00",
                "2026-09-04T00:00:00+00:00",
            ),
        )

    assert db.recover_interrupted_tasks() == 1

    with sqlite3.connect(db.path) as connection:
        status = connection.execute(
            "SELECT status FROM crawl_tasks WHERE run_id = ?", ("run-1",)
        ).fetchone()[0]
    assert status == "pending"


def test_concurrent_first_scope_creation_returns_the_same_frozen_scope(tmp_path: Path):
    path = tmp_path / "test.db"
    first = Database(path)
    second = Database(path)
    first.initialize()
    first.upsert_indices([make_index("000300"), make_index("000905")])

    start = threading.Barrier(2)
    results = []
    errors = []

    for database in (first, second):
        original_load_scope = database._load_scope

        def delayed_load_scope(connection, scope_id, original=original_load_scope):
            scope = original(connection, scope_id)
            if scope is None:
                time.sleep(0.05)
            return scope

        database._load_scope = delayed_load_scope

    def create_scope(database: Database, codes: list[str]) -> None:
        try:
            start.wait(timeout=2)
            results.append(database.create_or_get_scope("fixed:1", codes))
        except Exception as error:
            errors.append(error)

    first_thread = threading.Thread(target=create_scope, args=(first, ["000300"]))
    second_thread = threading.Thread(target=create_scope, args=(second, ["000905"]))
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
