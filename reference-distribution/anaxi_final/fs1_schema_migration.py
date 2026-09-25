"""FS1: additive schema for family-membership administration and
principal visibility-scope attribution.

Follows the established additive-only convention (OC0/WTR0/API1-S1/
SLP2): never drops or alters an existing column, never edits an existing
CHECK constraint or trigger, never deletes or rewrites an existing row,
never creates a second canonical database. FS1 only ever:

  1. creates the two new tables below (CREATE TABLE IF NOT EXISTS), and
  2. attaches a NEW nullable column (`visibility_scope`) to the two
     pre-existing canonical tables (`events`, `auth_contexts`) via the
     same guarded ALTER TABLE ADD COLUMN idiom api1_schema_migration.py
     and slp2_schema_migration.py establish, enforcing a NEW CHECK that
     references only that single column. No existing constraint or
     trigger on those tables is modified.

The new tables:

  family_principal_events -- the canonical, append-only ledger of family
    membership administration acts: designate_owner / enroll /
    deactivate. This table is the SOLE record of family history; every
    state-changing act is appended here permanently and append-only
    triggers forbid UPDATE/DELETE (mirroring slp2's ledger triggers).
    The actor_role/event_kind pairing is CHECK-enforced:
    designate_owner always names an 'owner' principal; enroll and
    deactivate always name a 'family_member' principal -- so the owner
    can never be deactivated structurally. event_id is a deterministic
    derive_stable_id("family_principal_event", canonical_act_json): a
    replayed identical administrative act deduplicates at the row level
    (idempotent replay, mirroring hir1's request-table dedup intent
    without needing a separate request table). Chronology is read from
    rowid (monotonic write sequence; the ledger is append-only), never
    from the content-hash event_id.

  family_membership_state -- the mutable projection caching the CURRENT
    state (owner; active/deactivated members). This is a convenience
    cache, never the source of truth: it is rebuilt from
    family_principal_events by family_membership.refresh_projection(),
    and every authority-bearing read consults the canonical ledger
    (family_membership.designated_owner_actor_id /
    active_family_principals do NOT read this projection).

visibility_scope semantics (frozen, enforced by family_membership):
  NULL               -- no principal-scope classification; governed by
                        the pre-FS1 unbound/legacy delivery rules, and in
                        a scoped session deliverable only to its own
                        author. NEVER interpreted as 'shared'.
  'principal_private'-- visible to its own author principal only.
  'family_shared'    -- visible to any currently-active family principal.

Additive-only discipline (mirrors slp2's Correction-1/2 lesson): the
CREATE TABLE statements below are byte-stable -- any future column or
trigger lands as a separately-named guarded step appended after them,
never by editing the original DDL in place.

Exercised against synthetic/temp databases only (established convention
in this job; no live anaxi_provenance.db is touched here). The
application layer (human_session_binding, native_provenance_writer,
family_membership) also applies this migration idempotently on its own
writable connections via apply_migration_on_connection(), so existing
databases created without FS1 columns converge on the identical final
schema on first scoped write -- additive, never destructive.
"""
import sqlite3

GROUP_KEY_FAMILY = "family_v0"

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS family_principal_events (
    event_id            TEXT PRIMARY KEY,
    group_key           TEXT NOT NULL DEFAULT 'family_v0' CHECK (group_key = 'family_v0'),
    principal_actor_id  TEXT NOT NULL REFERENCES actors(actor_id),
    actor_role          TEXT NOT NULL CHECK (actor_role IN ('owner','family_member')),
    event_kind          TEXT NOT NULL CHECK (event_kind IN ('designate_owner','enroll','deactivate')),
    display_label       TEXT,
    requester_actor_id  TEXT NOT NULL REFERENCES actors(actor_id),
    occurred_at         INTEGER NOT NULL,
    record_created_at   INTEGER NOT NULL,
    source_ref          TEXT,
    CHECK (
        (event_kind = 'designate_owner' AND actor_role = 'owner')
        OR (event_kind = 'enroll' AND actor_role = 'family_member')
        OR (event_kind = 'deactivate' AND actor_role = 'family_member')
    )
);
CREATE TRIGGER IF NOT EXISTS trg_family_principal_events_no_update
    BEFORE UPDATE ON family_principal_events
BEGIN
    SELECT RAISE(ABORT, 'family_principal_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_family_principal_events_no_delete
    BEFORE DELETE ON family_principal_events
BEGIN
    SELECT RAISE(ABORT, 'family_principal_events is append-only');
END;

CREATE TABLE IF NOT EXISTS family_membership_state (
    actor_id            TEXT PRIMARY KEY REFERENCES actors(actor_id),
    group_key           TEXT NOT NULL DEFAULT 'family_v0' CHECK (group_key = 'family_v0'),
    actor_role          TEXT NOT NULL CHECK (actor_role IN ('owner','family_member')),
    status              TEXT NOT NULL CHECK (status IN ('active','deactivated')),
    display_label       TEXT,
    enrolled_at         INTEGER NOT NULL,
    deactivated_at      INTEGER,
    source_event_id     TEXT NOT NULL REFERENCES family_principal_events(event_id),
    updated_at          INTEGER NOT NULL
);
"""

# Single source of truth for connection-scoped application: the exact
# statements, one per element, so apply_migration_on_connection() can run
# them individually via conn.execute() (executescript() issues an
# implicit COMMIT first, which would corrupt a caller-held transaction).
_CREATE_TABLE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS family_principal_events (
    event_id            TEXT PRIMARY KEY,
    group_key           TEXT NOT NULL DEFAULT 'family_v0' CHECK (group_key = 'family_v0'),
    principal_actor_id  TEXT NOT NULL REFERENCES actors(actor_id),
    actor_role          TEXT NOT NULL CHECK (actor_role IN ('owner','family_member')),
    event_kind          TEXT NOT NULL CHECK (event_kind IN ('designate_owner','enroll','deactivate')),
    display_label       TEXT,
    requester_actor_id  TEXT NOT NULL REFERENCES actors(actor_id),
    occurred_at         INTEGER NOT NULL,
    record_created_at   INTEGER NOT NULL,
    source_ref          TEXT,
    CHECK (
        (event_kind = 'designate_owner' AND actor_role = 'owner')
        OR (event_kind = 'enroll' AND actor_role = 'family_member')
        OR (event_kind = 'deactivate' AND actor_role = 'family_member')
    )
);""",
    """CREATE TABLE IF NOT EXISTS family_membership_state (
    actor_id            TEXT PRIMARY KEY REFERENCES actors(actor_id),
    group_key           TEXT NOT NULL DEFAULT 'family_v0' CHECK (group_key = 'family_v0'),
    actor_role          TEXT NOT NULL CHECK (actor_role IN ('owner','family_member')),
    status              TEXT NOT NULL CHECK (status IN ('active','deactivated')),
    display_label       TEXT,
    enrolled_at         INTEGER NOT NULL,
    deactivated_at      INTEGER,
    source_event_id     TEXT NOT NULL REFERENCES family_principal_events(event_id),
    updated_at          INTEGER NOT NULL
);""",
]

_CREATE_TRIGGER_STATEMENTS = [
    """CREATE TRIGGER IF NOT EXISTS trg_family_principal_events_no_update
BEFORE UPDATE ON family_principal_events
BEGIN
    SELECT RAISE(ABORT, 'family_principal_events is append-only');
END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_family_principal_events_no_delete
BEFORE DELETE ON family_principal_events
BEGIN
    SELECT RAISE(ABORT, 'family_principal_events is append-only');
END;""",
]

ALL_DDL_STATEMENTS = _CREATE_TABLE_STATEMENTS + _CREATE_TRIGGER_STATEMENTS

SCOPE_COLUMN_CONSTRAINT = (
    "visibility_scope TEXT "
    "CHECK (visibility_scope IN ('principal_private','family_shared') "
    "OR visibility_scope IS NULL)"
)


def _add_scope_column_if_missing(conn, table):
    """Guarded ALTER TABLE ADD COLUMN idiom (api1/slp2). Returns True
    only when the column was actually added."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "visibility_scope" in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {SCOPE_COLUMN_CONSTRAINT}")
    return True


def apply_migration_on_connection(conn):
    """Idempotent additive migration applied to an OPEN writable
    connection. NEVER commits: the caller owns the transaction, which
    lets binding/validation/writer entry points atomically ensure schema
    + write inside their own BEGIN IMMEDIATE. Uses conn.execute() per
    statement (never executescript(), whose implicit COMMIT would
    interrupt a caller-held transaction)."""
    for stmt in ALL_DDL_STATEMENTS:
        conn.execute(stmt)
    _add_scope_column_if_missing(conn, "events")
    _add_scope_column_if_missing(conn, "auth_contexts")


def apply_additive_migration(db_path):
    """Idempotent additive migration for a database file, for callers
    that hold no transactional connection of their own. Safe to run
    repeatedly and against a bare provenance_schema-derived database or
    one already carrying FS1 schema -- identical final state either way.
    Returns a report dict describing what (if anything) was added."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_tables_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL)
            events_altered = _add_scope_column_if_missing(conn, "events")
            auth_contexts_altered = _add_scope_column_if_missing(conn, "auth_contexts")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        existing_tables_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {
            "new_tables_created": sorted(existing_tables_after - existing_tables_before),
            "events_visibility_scope_added": events_altered,
            "auth_contexts_visibility_scope_added": auth_contexts_altered,
        }
    finally:
        conn.close()


def verify_migration_state(db_path):
    """Read-only. Reports whether the FS1 migration has been applied."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        events_columns = set()
        if "events" in tables:
            events_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        auth_columns = set()
        if "auth_contexts" in tables:
            auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
        return {
            "has_family_principal_events": "family_principal_events" in tables,
            "has_family_membership_state": "family_membership_state" in tables,
            "has_family_principal_events_no_update_trigger":
                "trg_family_principal_events_no_update" in triggers,
            "has_family_principal_events_no_delete_trigger":
                "trg_family_principal_events_no_delete" in triggers,
            "events_has_visibility_scope": "visibility_scope" in events_columns,
            "auth_contexts_has_visibility_scope": "visibility_scope" in auth_columns,
        }
    finally:
        conn.close()