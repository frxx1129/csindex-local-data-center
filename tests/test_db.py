from pathlib import Path
import sqlite3

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
