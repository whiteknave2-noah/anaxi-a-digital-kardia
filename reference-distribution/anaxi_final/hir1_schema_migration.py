"""HIR1-S1: additive schema design for human-registration idempotency.

DESIGNED AND TESTED AGAINST PRODUCTION-SCHEMA COPIES ONLY. This module's
`apply_additive_migration()` is NEVER called against the live
anaxi_provenance.db during HIR1-S1 -- that is explicitly deferred to a
separately authorized HIR1-S2 gate. See the HIR1-S1 final report for
the exact live migration this would perform.

No existing table's columns, CHECK constraints, or triggers are ever
touched. persons/actors/actor_human_person/events/event_subjects/
event_requesters are all used AS-IS via ordinary INSERT statements
(see hir1_registration.py) -- they need no schema change at all. The
only new schema object is the idempotency-key table below, since no
existing production table serves that purpose (same reasoning as
API1-S1's protected_decision_requests).
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS human_registration_requests (
    registration_request_id TEXT PRIMARY KEY,
    aab_actor_id TEXT NOT NULL,
    display_label TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    person_id TEXT,
    actor_id TEXT,
    registration_event_id TEXT,
    result_json TEXT,
    created_at INTEGER NOT NULL
);
"""


def apply_additive_migration(db_path):
    """Idempotent additive migration. NOT invoked against the live DB
    in HIR1-S1 -- see module docstring."""
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
        return {"has_human_registration_requests": "human_registration_requests" in tables}
    finally:
        conn.close()
