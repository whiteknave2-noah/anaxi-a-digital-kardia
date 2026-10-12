"""API1-S1: additive-only schema migration for anaxi_provenance.db.

Follows the existing established convention in migrate_historical_data.py
(_add_columns_if_missing: ALTER TABLE ... ADD COLUMN, guarded by a
PRAGMA table_info existence check, idempotent, never destructive) and
extends the same philosophy to whole new tables via CREATE TABLE IF NOT
EXISTS.

This module NEVER:
  - drops or alters an existing column;
  - modifies an existing CHECK constraint or trigger;
  - deletes or overwrites an existing row;
  - creates a second canonical database.

Compatibility note discovered during design (read-only inspection of
provenance_schema.py before writing any DDL): the existing
`auth_contexts.assurance_level` column has a hard CHECK constraint
limited to ('none','low','medium','high') -- AAB1's native assurance
value `os_principal_session_bound` is not in that domain, and altering
a CHECK constraint on an existing SQLite table is not a safe additive
operation (it requires rebuilding the table). Rather than touching the
constraint, this migration adds a new nullable column
(`source_assurance_detail`) to carry the exact AAB1-native string
losslessly, while the existing `assurance_level` column is populated
with a documented coarse mapping (see ASSURANCE_LEVEL_MAP in
api1_control_plane.py) that stays inside the existing allowed domain.
"""
import sqlite3

AUTH_CONTEXTS_ADDITIVE_COLUMNS = {
    "source_aab_auth_context_id": "TEXT",
    "source_aab_session_id": "TEXT",
    "observed_valid_at": "INTEGER",
    "source_assurance_detail": "TEXT",
}

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS protected_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_text TEXT NOT NULL,
    decision_context TEXT,
    owner_actor_id TEXT NOT NULL,
    decision_state TEXT NOT NULL CHECK (decision_state IN ('open','resolved')),
    resolution_kind TEXT CHECK (resolution_kind IN ('chosen','refused') OR resolution_kind IS NULL),
    counterpart_actor_id TEXT NOT NULL,
    created_event_id TEXT NOT NULL,
    last_transition_event_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS protected_decision_requests (
    establishment_request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    decision_id TEXT,
    source_input_id TEXT NOT NULL,
    auth_context_id TEXT,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS protected_decision_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    auth_context_id TEXT,
    session_id TEXT,
    establishment_request_id TEXT,
    previous_owner_actor_id TEXT,
    resulting_owner_actor_id TEXT,
    prior_transition_event_id TEXT,
    related_waking_event_id TEXT,
    occurred_at INTEGER NOT NULL,
    payload_json TEXT
);
"""


def _add_columns_if_missing(conn, table, columns):
    """Same idiom as migrate_historical_data.py's helper of the same
    name (not imported from there to avoid coupling this schema-only
    module to the historical-migration module's unrelated concerns),
    extended to accept an explicit SQL type per column rather than
    always TEXT."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for col, sql_type in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
            added.append(col)
    return added


def apply_additive_migration(db_path):
    """Idempotent. Safe to call repeatedly -- running it twice adds
    nothing the second time. Returns a report dict describing exactly
    what (if anything) was added."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_tables_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        added_columns = _add_columns_if_missing(conn, "auth_contexts", AUTH_CONTEXTS_ADDITIVE_COLUMNS)
        conn.executescript(NEW_TABLES_DDL)
        conn.commit()

        existing_tables_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        new_tables = sorted(existing_tables_after - existing_tables_before)

        return {
            "auth_contexts_columns_added": added_columns,
            "new_tables_created": new_tables,
        }
    finally:
        conn.close()


def verify_migration_state(db_path):
    """Read-only. Reports whether the migration has already been
    applied, for idempotency verification."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)")}
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {
            "auth_contexts_has_all_additive_columns": set(AUTH_CONTEXTS_ADDITIVE_COLUMNS).issubset(auth_columns),
            "has_protected_decisions": "protected_decisions" in tables,
            "has_protected_decision_requests": "protected_decision_requests" in tables,
            "has_protected_decision_events": "protected_decision_events" in tables,
        }
    finally:
        conn.close()
