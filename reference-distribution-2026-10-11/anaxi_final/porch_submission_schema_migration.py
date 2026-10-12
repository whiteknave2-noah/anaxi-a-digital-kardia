"""Additive schema for durable Porch submission-to-H bindings.

This table is host-mechanical ingress provenance, not a second conversation
store.  Its only purpose is to bind one authenticated device/submission key
to the exact canonical human_waking_input event (H) accepted for it.  Rows are
append-only.  The H and this binding are inserted by
``human_session_binding.record_or_reconcile_porch_submission`` in the same
``BEGIN IMMEDIATE`` transaction.

The migration helpers require an explicit database path or caller-owned
connection.  They never discover or open production state implicitly.
"""
import os
import sqlite3


TABLE_NAME = "porch_mobile_submission_bindings"
PROTOCOL_VERSION = 1

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        authenticated_device_id TEXT NOT NULL,
        submission_id TEXT NOT NULL,
        protocol_version INTEGER NOT NULL CHECK (protocol_version = {PROTOCOL_VERSION}),
        human_input_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
        body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
        body_byte_length INTEGER NOT NULL CHECK (body_byte_length >= 0),
        authenticated_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
        visibility_scope TEXT CHECK (
            visibility_scope IS NULL OR visibility_scope IN ('principal_private', 'family_shared')
        ),
        accepted_at INTEGER NOT NULL,
        PRIMARY KEY (authenticated_device_id, submission_id)
    )""",
    f"""CREATE TRIGGER IF NOT EXISTS trg_{TABLE_NAME}_no_update
        BEFORE UPDATE ON {TABLE_NAME}
        BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS trg_{TABLE_NAME}_no_delete
        BEFORE DELETE ON {TABLE_NAME}
        BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
)

_EXPECTED_COLUMNS = (
    "authenticated_device_id", "submission_id", "protocol_version",
    "human_input_event_id", "body_sha256", "body_byte_length",
    "authenticated_actor_id", "visibility_scope", "accepted_at",
)


def _verify_installed_contract(conn: sqlite3.Connection) -> None:
    columns = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})"))
    if columns != _EXPECTED_COLUMNS:
        raise RuntimeError(
            f"{TABLE_NAME} has unexpected columns {columns!r}; refusing to use an ambiguous binding schema"
        )
    indexes = conn.execute(f"PRAGMA index_list({TABLE_NAME})").fetchall()
    unique_column_sets = {
        tuple(row[2] for row in conn.execute(f"PRAGMA index_info({index_row[1]})"))
        for index_row in indexes if index_row[2]
    }
    required = {
        ("authenticated_device_id", "submission_id"),
        ("human_input_event_id",),
    }
    if not required.issubset(unique_column_sets):
        raise RuntimeError(
            f"{TABLE_NAME} is missing its device/submission or exact-H uniqueness constraint"
        )
    trigger_names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE_NAME,)
        )
    }
    required_triggers = {
        f"trg_{TABLE_NAME}_no_update", f"trg_{TABLE_NAME}_no_delete",
    }
    if not required_triggers.issubset(trigger_names):
        raise RuntimeError(f"{TABLE_NAME} is missing append-only guards")


def install_on_connection(conn: sqlite3.Connection) -> None:
    """Install/verify the additive table inside the caller's transaction.

    This function never commits.  The caller can therefore make first-time
    schema installation, canonical H creation, and submission binding one
    atomic operation on a fresh database.
    """
    core_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('events','actors')"
        )
    }
    if core_tables != {"events", "actors"}:
        raise ValueError("target is not an ANAXI canonical provenance database")
    for statement in _DDL:
        conn.execute(statement)
    _verify_installed_contract(conn)


def apply_additive_migration(db_path: str) -> dict:
    """Explicit deployment helper.  No production path is inferred."""
    if not os.path.isfile(db_path):
        raise ValueError("target database does not exist")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE_NAME,)
        ).fetchone() is not None
        conn.execute("BEGIN IMMEDIATE")
        install_on_connection(conn)
        conn.commit()
        return {"table": TABLE_NAME, "created": not existed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_migration_state(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE_NAME,)
        ).fetchone() is not None
        if present:
            _verify_installed_contract(conn)
        return {"table": TABLE_NAME, "installed": present}
    finally:
        conn.close()
