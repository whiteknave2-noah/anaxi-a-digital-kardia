"""SLP1-S1 acceptance tests for sleep_watermark.py. Every test uses a
temporary directory. No production watermark file is ever touched.
"""
import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import sleep_watermark as sw

TEST_ROOT = tempfile.mkdtemp(prefix="slp1s1_watermark_test_")


def fresh_paths(name):
    return sw.SleepWatermarkPaths(watermark_db_path=os.path.join(TEST_ROOT, name, sw.WATERMARK_DB_FILENAME))


def test_missing_file_returns_initial_state():
    paths = fresh_paths("missing_file")
    result = sw.read_watermark(paths)
    assert result == sw.INITIAL_WATERMARK_STATE
    assert not os.path.exists(paths.watermark_db_path)  # never created by reading


def test_reading_never_creates_the_file():
    paths = fresh_paths("no_create")
    sw.read_watermark(paths)
    sw.read_watermark(paths)
    assert not os.path.exists(paths.watermark_db_path)


def test_existing_file_with_no_table_returns_initial_state():
    paths = fresh_paths("empty_db")
    os.makedirs(os.path.dirname(paths.watermark_db_path), exist_ok=True)
    conn = sqlite3.connect(paths.watermark_db_path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    result = sw.read_watermark(paths)
    assert result == sw.INITIAL_WATERMARK_STATE


def test_existing_table_with_no_row_returns_initial_state():
    paths = fresh_paths("empty_table")
    os.makedirs(os.path.dirname(paths.watermark_db_path), exist_ok=True)
    conn = sqlite3.connect(paths.watermark_db_path)
    conn.execute(sw._INIT_SQL)
    conn.commit()
    conn.close()
    result = sw.read_watermark(paths)
    assert result == sw.INITIAL_WATERMARK_STATE


def test_existing_row_is_returned_exactly():
    paths = fresh_paths("real_row")
    os.makedirs(os.path.dirname(paths.watermark_db_path), exist_ok=True)
    conn = sqlite3.connect(paths.watermark_db_path)
    conn.execute(sw._INIT_SQL)
    conn.execute(
        "INSERT INTO sleep_v1_watermark (id, last_processed_component_id, last_successful_cycle_id, "
        "last_successful_at, cycle_count) VALUES (1, 42, 'cycle-abc', 12345, 3)"
    )
    conn.commit()
    conn.close()
    result = sw.read_watermark(paths)
    assert result == {
        "last_processed_component_id": 42,
        "last_successful_cycle_id": "cycle-abc",
        "last_successful_at": 12345,
        "cycle_count": 3,
    }


def test_read_watermark_never_writes_even_against_an_existing_populated_db():
    paths = fresh_paths("no_mutate_existing")
    os.makedirs(os.path.dirname(paths.watermark_db_path), exist_ok=True)
    conn = sqlite3.connect(paths.watermark_db_path)
    conn.execute(sw._INIT_SQL)
    conn.execute(
        "INSERT INTO sleep_v1_watermark (id, last_processed_component_id, last_successful_cycle_id, "
        "last_successful_at, cycle_count) VALUES (1, 7, NULL, NULL, 1)"
    )
    conn.commit()
    conn.close()
    before = os.path.getmtime(paths.watermark_db_path)
    sw.read_watermark(paths)
    sw.read_watermark(paths)
    after = os.path.getmtime(paths.watermark_db_path)
    assert before == after


def test_old_file_based_path_still_has_no_advance_function():
    # SLP1-S1 froze this module as read-only against its OWN separate
    # file-based database (SleepWatermarkPaths/WATERMARK_DB_FILENAME/
    # read_watermark() above) -- that constraint is superseded by
    # SLP1-C, which explicitly authorizes this module's first write
    # behavior, but ONLY against the shared anchored anaxi_provenance.db
    # via an already-open connection (advance_watermark_in_conn()), and
    # ONLY as the last step of a caller-owned atomic commit transaction
    # (see sleep_cycle.py). This test now proves the narrower, still-
    # true invariant: no function writes to the OLD separate per-file
    # database at all -- that storage path remains permanently inert.
    with open(os.path.join(ANAXI_FINAL, "sleep_watermark.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    function_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "advance_watermark_in_conn" in function_names, (
        "SLP1-C authorizes exactly this one connection-based write function"
    )
    for forbidden in ("write_watermark", "set_watermark", "update_watermark", "commit_watermark"):
        assert forbidden not in function_names
    # The one authorized write path takes an already-open CONNECTION
    # and writes no per-file database of its own -- SleepWatermarkPaths/
    # WATERMARK_DB_FILENAME (the old separate-file convention) is never
    # referenced by the write path at all.
    import inspect
    write_fn_source = inspect.getsource(sw.advance_watermark_in_conn)
    assert "SleepWatermarkPaths" not in write_fn_source
    assert "WATERMARK_DB_FILENAME" not in write_fn_source
    assert "sqlite3.connect" not in write_fn_source, (
        "advance_watermark_in_conn must operate on an already-open connection, "
        "never open (or create) a database of its own"
    )


def test_production_defaults_not_the_old_legacy_watermark_table():
    defaults = sw.SleepWatermarkPaths.production_defaults()
    assert defaults.watermark_db_path == os.path.join(ANAXI_FINAL, sw.WATERMARK_DB_FILENAME)
    assert "anaxi_mind" not in defaults.watermark_db_path
    assert not os.path.exists(defaults.watermark_db_path), "production SLP1 watermark must not exist yet -- this gate never creates it"


def test_read_watermark_from_conn_returns_initial_state_before_any_row():
    conn = sqlite3.connect(":memory:")
    conn.execute(sw._INIT_SQL)
    conn.commit()
    assert sw.read_watermark_from_conn(conn) == sw.INITIAL_WATERMARK_STATE
    conn.close()


def test_advance_watermark_in_conn_writes_exact_values_and_increments_cycle_count():
    conn = sqlite3.connect(":memory:")
    conn.execute(sw._INIT_SQL)
    conn.commit()
    sw.advance_watermark_in_conn(conn, new_last_processed_component_id=42, cycle_id="cycle-1", now=1000)
    conn.commit()
    result = sw.read_watermark_from_conn(conn)
    assert result == {
        "last_processed_component_id": 42,
        "last_successful_cycle_id": "cycle-1",
        "last_successful_at": 1000,
        "cycle_count": 1,
    }
    sw.advance_watermark_in_conn(conn, new_last_processed_component_id=99, cycle_id="cycle-2", now=2000)
    conn.commit()
    result2 = sw.read_watermark_from_conn(conn)
    assert result2 == {
        "last_processed_component_id": 99,
        "last_successful_cycle_id": "cycle-2",
        "last_successful_at": 2000,
        "cycle_count": 2,
    }
    conn.close()


ALL_TESTS = [
    test_missing_file_returns_initial_state,
    test_reading_never_creates_the_file,
    test_existing_file_with_no_table_returns_initial_state,
    test_existing_table_with_no_row_returns_initial_state,
    test_existing_row_is_returned_exactly,
    test_read_watermark_never_writes_even_against_an_existing_populated_db,
    test_old_file_based_path_still_has_no_advance_function,
    test_production_defaults_not_the_old_legacy_watermark_table,
    test_read_watermark_from_conn_returns_initial_state_before_any_row,
    test_advance_watermark_in_conn_writes_exact_values_and_increments_cycle_count,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
