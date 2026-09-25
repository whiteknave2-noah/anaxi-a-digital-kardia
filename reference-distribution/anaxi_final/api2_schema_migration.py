"""API2-S1: additive schema for the disabled canonical TX2/HDI2 execution
seam (prepare -> Pass 1 -> stage -> Pass 2 -> TX2).

DESIGNED AND TESTED AGAINST PRODUCTION-SCHEMA COPIES FIRST. Applying
`apply_additive_migration()` against the live anaxi_provenance.db is
authorized in THIS gate (API2-S1 section 58) ONLY as a schema-only
step -- zero attempt/snapshot/stage rows are ever written live here.

No existing table's columns, CHECK constraints, or triggers are ever
touched. Three new tables only, mirroring the API1/HIR1 additive
convention (`_add_columns_if_missing` idiom reused where applicable;
here nothing needs a new column, only new tables, so the idiom is a
plain `CREATE TABLE IF NOT EXISTS`).
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS protected_decision_attempts (
    attempt_id                         TEXT PRIMARY KEY,
    decision_id                        TEXT NOT NULL,
    expected_last_transition_event_id  TEXT NOT NULL,
    status                             TEXT NOT NULL CHECK (
        status IN ('created','preparing_context','context_ready','prepare_failed','aborted')
    ),
    started_at                         INTEGER NOT NULL,
    updated_at                         INTEGER NOT NULL,
    snapshot_id                        TEXT,
    failure_code                       TEXT,
    failure_detail                     TEXT
);

CREATE TABLE IF NOT EXISTS protected_decision_attempt_snapshots (
    snapshot_id                 TEXT PRIMARY KEY,
    attempt_id                  TEXT NOT NULL UNIQUE REFERENCES protected_decision_attempts(attempt_id),
    snapshot_format              TEXT NOT NULL,
    snapshot_version               TEXT NOT NULL,
    prepared_context_json            TEXT NOT NULL,
    snapshot_hash                       TEXT NOT NULL,
    system_context_hash                    TEXT NOT NULL,
    model_name                                TEXT NOT NULL,
    think                                        INTEGER NOT NULL CHECK (think IN (0, 1)),
    generation_controls_json                        TEXT NOT NULL,
    created_at                                         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS protected_decision_stages (
    stage_id                            TEXT PRIMARY KEY,
    attempt_id                          TEXT NOT NULL UNIQUE REFERENCES protected_decision_attempts(attempt_id),
    decision_id                         TEXT NOT NULL,
    expected_last_transition_event_id   TEXT NOT NULL,
    prepared_snapshot_id                TEXT NOT NULL REFERENCES protected_decision_attempt_snapshots(snapshot_id),
    prepared_snapshot_hash               TEXT NOT NULL,
    action_id                               TEXT NOT NULL UNIQUE,
    actor_id                                   TEXT NOT NULL,
    raw_action_json                               TEXT NOT NULL,
    validated_action_json                            TEXT NOT NULL,
    status                                              TEXT NOT NULL CHECK (
        status IN ('awaiting_expression','expression_ready','committed','aborted')
    ),
    selected_at                                            INTEGER NOT NULL,
    expression_text                                           TEXT,
    expression_hash                                              TEXT,
    expression_created_at                                           INTEGER,
    committed_event_id                                                 TEXT,
    failure_code                                                          TEXT,
    failure_detail                                                          TEXT
);
"""

NEW_TABLE_NAMES = (
    "protected_decision_attempts",
    "protected_decision_attempt_snapshots",
    "protected_decision_stages",
)


def apply_additive_migration(db_path):
    """Idempotent additive migration. Creates the three new tables if
    absent. Touches nothing else."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        conn.executescript(NEW_TABLES_DDL)
        conn.commit()
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {"new_tables_created": sorted(existing_after - existing_before)}
    finally:
        conn.close()


def verify_migration_state(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {name: (name in tables) for name in NEW_TABLE_NAMES}
    finally:
        conn.close()
