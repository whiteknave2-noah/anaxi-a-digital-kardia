"""OC0: additive schema migration for the outward-communication
foundation. No existing table's columns, CHECK constraints, or
triggers are ever touched -- events/event_components are used AS-IS
via ordinary INSERT statements (see outward_communication.py), the
same reasoning HIR1-S1 (hir1_schema_migration.py) already used: they
need no schema change at all.

Two new tables only, since no existing production table serves either
purpose:

  outward_projection_attempts -- append-only projection/delivery
  receipts for a canonical 'clark_outward_act' event. Each row is
  pinned to that event's own recorded content hash (never a caller-
  supplied one), so a retry mechanically refers to the same content by
  construction, not by convention; content that actually changes must
  be a new outward act with a new event_id, not a new attempt row.

  outward_act_reply_links -- append-only, explicit-reference-only
  linkage from a later genuine human_waking_input event to an earlier
  clark_outward_act event. trg_outward_reply_links_refs_exist enforces
  that both referenced events actually exist with the expected
  event_type at insert time -- never merely a dangling TEXT foreign
  key -- so a malformed or nonexistent reference is rejected
  mechanically, not by caller discipline alone.
  trg_outward_reply_links_chronology (a SEPARATE trigger, added by a
  later correction) enforces canonical chronology, read from the
  referenced events' own occurred_at columns, never trusted from the
  caller: the human event must not be canonically earlier than the
  outward act, and linked_at must not predate either. (Correction
  history: chronology enforcement was first added directly to
  trg_outward_reply_links_refs_exist's own body, which meant a
  database already migrated by the pre-chronology definition never
  gained it on re-migration -- CREATE TRIGGER IF NOT EXISTS is a no-op
  once a trigger of that name exists, regardless of whether its
  definition changed. The fix is this separately-named trigger: a name
  that never existed before installs cleanly via CREATE TRIGGER IF NOT
  EXISTS on both a fresh database and an already-migrated one, and the
  original trigger is left byte-identical to its pre-chronology
  definition.)

Not invoked against the live anaxi_provenance.db by this job -- OC0 is
a transport-neutral foundation exercised only against synthetic/temp
databases (see test_outward_communication.py). A live cutover is
explicitly out of scope here, same deferral hir1_schema_migration.py
itself documents for its own table.
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS outward_projection_attempts (
    projection_attempt_id   TEXT PRIMARY KEY,
    outward_event_id            TEXT NOT NULL,
    attempt_sequence               INTEGER NOT NULL CHECK (attempt_sequence >= 1),
    content_sha256                    TEXT NOT NULL,
    status                               TEXT NOT NULL CHECK (status IN ('attempted','succeeded','failed','not_established')),
    attempted_at                          INTEGER NOT NULL,
    observed_at                             INTEGER NOT NULL,
    detail                                    TEXT,
    created_at                                  INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outward_projection_attempts_seq
    ON outward_projection_attempts(outward_event_id, attempt_sequence);
CREATE TRIGGER IF NOT EXISTS trg_outward_projection_attempts_event_exists
    BEFORE INSERT ON outward_projection_attempts
BEGIN
    SELECT RAISE(ABORT, 'outward_event_id must reference an existing clark_outward_act event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.outward_event_id AND event_type = 'clark_outward_act'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_outward_projection_attempts_no_update
    BEFORE UPDATE ON outward_projection_attempts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_outward_projection_attempts_no_delete
    BEFORE DELETE ON outward_projection_attempts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS outward_act_reply_links (
    reply_link_id        TEXT PRIMARY KEY,
    outward_event_id         TEXT NOT NULL,
    human_event_id              TEXT NOT NULL UNIQUE,
    linked_at                      INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_outward_reply_links_refs_exist
    BEFORE INSERT ON outward_act_reply_links
BEGIN
    SELECT RAISE(ABORT, 'outward_event_id must reference an existing clark_outward_act event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.outward_event_id AND event_type = 'clark_outward_act'
    );
    SELECT RAISE(ABORT, 'human_event_id must reference an existing human_waking_input event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.human_event_id AND event_type = 'human_waking_input'
    );
END;
-- Chronology, as a SEPARATELY NAMED trigger (correction: a database
-- already migrated by the pre-chronology version of
-- trg_outward_reply_links_refs_exist above never gains new logic
-- added to that trigger's BODY on re-migration -- CREATE TRIGGER IF
-- NOT EXISTS is a no-op once a trigger of that name exists, regardless
-- of whether its definition changed. A new, previously-nonexistent
-- trigger name is what actually installs on such an upgrade; the
-- existing referential-integrity trigger above is left byte-identical
-- to its original definition so upgrading never touches it).
--
-- A reply link asserts that the human event is a reply to (i.e. not
-- canonically earlier than) the outward act. occurred_at is
-- second-resolution and is the only canonical chronology field on
-- events -- there is no monotonic secondary ordering signal (ULIDs
-- here carry a random, not counter-based, sub-millisecond tail, so
-- comparing event_id strings would assert precision the IDs do not
-- actually guarantee). Only the case this comparison can actually
-- PROVE wrong is rejected (human strictly earlier); equal occurred_at
-- values are accepted as "not established as earlier," never invented
-- as a false strict ordering.
CREATE TRIGGER IF NOT EXISTS trg_outward_reply_links_chronology
    BEFORE INSERT ON outward_act_reply_links
BEGIN
    SELECT RAISE(ABORT, 'human_event_id must not be canonically earlier than outward_event_id -- a reply link cannot assert a chronology contradicted by the referenced canonical events')
    WHERE (SELECT occurred_at FROM events WHERE event_id = NEW.human_event_id)
        < (SELECT occurred_at FROM events WHERE event_id = NEW.outward_event_id);
    -- linked_at (when the host recorded the link) must not claim to
    -- predate either event it links -- same strict-only comparison for
    -- the same reason.
    SELECT RAISE(ABORT, 'linked_at must not predate the canonical occurred_at of either referenced event')
    WHERE NEW.linked_at < (SELECT occurred_at FROM events WHERE event_id = NEW.outward_event_id)
       OR NEW.linked_at < (SELECT occurred_at FROM events WHERE event_id = NEW.human_event_id);
END;
CREATE TRIGGER IF NOT EXISTS trg_outward_reply_links_no_update
    BEFORE UPDATE ON outward_act_reply_links BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_outward_reply_links_no_delete
    BEFORE DELETE ON outward_act_reply_links BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def apply_additive_migration(db_path: str) -> dict:
    """Idempotent additive migration. Never touches a live/production
    file by contract of this job -- callers point it only at synthetic
    or rehearsal database paths."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # executescript commits a pending transaction before executing its
        # script. BEGIN must be inside the script to make all DDL atomic.
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL + "\nCOMMIT;")
        except Exception:
            conn.rollback()
            raise
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {"new_tables_created": sorted(existing_after - existing_before)}
    finally:
        conn.close()


def verify_migration_state(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        return {
            "has_outward_projection_attempts": "outward_projection_attempts" in tables,
            "has_outward_act_reply_links": "outward_act_reply_links" in tables,
            "has_reply_link_chronology_trigger": "trg_outward_reply_links_chronology" in triggers,
        }
    finally:
        conn.close()
