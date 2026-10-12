"""DC0: additive schema migration for Private Discord Correspondence V0.

Same discipline as oc0_schema_migration.py: no existing table's columns,
CHECK constraints, or triggers are ever touched. Inbound/outbound
correspondence canonical facts live in the existing events/
event_components tables AS-IS (ordinary INSERT statements); three new
tables cover the genuinely new shapes:

  discord_destination_events -- append-only ledger of owner-authorized
  destination authorization/revocation occurrences. Canonical: the
  current registry state is ALWAYS reconstructable from this history
  (see discord_correspondence.list_authorized_destinations), so a
  contradictory or tampered projection row can never win.

  discord_destination_registry_state -- an audit projection of the
  ledger's current state. Never authoritative; refreshed on every write
  and re-derivable. Reads in discord_correspondence.py derive from the
  ledger, never trust this table's is_authorized column.

  discord_outbound_parts / discord_outbound_part_attempts -- multipart transport of ONE canonical
  Caret reply (append-only): the deterministic part plan (offsets + hashes, persisted before any
  network dispatch) and a per-part attempt ledger recording each confirmed Discord message id.  The
  single canonical outward act remains the authoritative authored content; parts are projections.

  discord_author_mapping_events -- append-only ledger of owner-administered bindings from a
  stable Discord author id to an EXISTING canonical ANAXI principal (map / revoke).  Canonical:
  the current binding is always re-derived from this history (see discord_author_mapping); the
  Discord username is a non-authoritative snapshot only.

  discord_inbound_seen -- the restart-stable inbound dedupe/cursor
  ledger. One row per (destination_kind, destination_id,
  discord_message_id) actually ingested. UNIQUE by construction, so a
  redelivered transport message can never create a second canonical
  inbound event.

Migration is idempotent and safe to apply on both a fresh database and
an already-migrated one. Callers point it at a real data_dir; unlike
OC0's original synthetic-only scope, this capability's engine ensures it
on the same connection path that ingests/dispatches.
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS discord_destination_events (
    destination_event_id   TEXT PRIMARY KEY,
    destination_id               TEXT NOT NULL,
    destination_kind                TEXT NOT NULL CHECK (destination_kind IN ('channel','dm_channel')),
    discord_snowflake                  TEXT NOT NULL,
    display_label                         TEXT NOT NULL,
    action                                   TEXT NOT NULL CHECK (action IN ('authorize','revoke')),
    requester_actor_id                          TEXT NOT NULL,
    occurred_at                                    INTEGER NOT NULL,
    created_at                                        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discord_destination_events_dest
    ON discord_destination_events(destination_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS trg_discord_destination_events_no_update
    BEFORE UPDATE ON discord_destination_events
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_discord_destination_events_no_delete
    BEFORE DELETE ON discord_destination_events
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS discord_destination_registry_state (
    destination_id     TEXT PRIMARY KEY,
    destination_kind       TEXT NOT NULL CHECK (destination_kind IN ('channel','dm_channel')),
    discord_snowflake          TEXT NOT NULL,
    display_label                  TEXT NOT NULL,
    is_authorized                     INTEGER NOT NULL CHECK (is_authorized IN (0,1)),
    last_event_id                        TEXT NOT NULL,
    updated_at                              INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_discord_destination_registry_state_no_delete
    BEFORE DELETE ON discord_destination_registry_state
    BEGIN SELECT RAISE(ABORT, 'registry projection rows are never deleted'); END;

CREATE TABLE IF NOT EXISTS discord_inbound_seen (
    destination_kind     TEXT NOT NULL,
    destination_id          TEXT NOT NULL,
    discord_message_id         TEXT NOT NULL,
    event_id                       TEXT NOT NULL,
    first_seen_at                     INTEGER NOT NULL,
    PRIMARY KEY (destination_kind, destination_id, discord_message_id)
);
CREATE TRIGGER IF NOT EXISTS trg_discord_inbound_seen_no_update
    BEFORE UPDATE ON discord_inbound_seen
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_discord_inbound_seen_no_delete
    BEFORE DELETE ON discord_inbound_seen
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS discord_outbound_parts (
    outward_event_id   TEXT NOT NULL,
    part_index             INTEGER NOT NULL CHECK (part_index >= 1),
    part_count                INTEGER NOT NULL CHECK (part_count >= 2),
    char_start                   INTEGER NOT NULL CHECK (char_start >= 0),
    char_end                        INTEGER NOT NULL CHECK (char_end > char_start),
    part_sha256                        TEXT NOT NULL,
    plan_version                          INTEGER NOT NULL,
    created_at                               INTEGER NOT NULL,
    PRIMARY KEY (outward_event_id, part_index)
);
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_parts_event_exists
    BEFORE INSERT ON discord_outbound_parts
BEGIN
    SELECT RAISE(ABORT, 'outward_event_id must reference an existing clark_outward_act event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.outward_event_id AND event_type = 'clark_outward_act'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_parts_no_update
    BEFORE UPDATE ON discord_outbound_parts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_parts_no_delete
    BEFORE DELETE ON discord_outbound_parts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS discord_outbound_part_attempts (
    part_attempt_id     TEXT PRIMARY KEY,
    outward_event_id        TEXT NOT NULL,
    part_index                  INTEGER NOT NULL,
    attempt_sequence               INTEGER NOT NULL CHECK (attempt_sequence >= 1),
    status                             TEXT NOT NULL CHECK (status IN ('attempted','succeeded','failed','not_established')),
    discord_message_id                    TEXT,
    detail                                   TEXT,
    attempted_at                                INTEGER NOT NULL,
    observed_at                                    INTEGER NOT NULL,
    created_at                                        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_outbound_part_attempts_seq
    ON discord_outbound_part_attempts(outward_event_id, part_index, attempt_sequence);
CREATE INDEX IF NOT EXISTS idx_discord_outbound_part_attempts_msg
    ON discord_outbound_part_attempts(discord_message_id);
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_part_attempts_plan_exists
    BEFORE INSERT ON discord_outbound_part_attempts
BEGIN
    SELECT RAISE(ABORT, 'part attempt must reference a planned part')
    WHERE NOT EXISTS (
        SELECT 1 FROM discord_outbound_parts
        WHERE outward_event_id = NEW.outward_event_id AND part_index = NEW.part_index
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_part_attempts_no_update
    BEFORE UPDATE ON discord_outbound_part_attempts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_discord_outbound_part_attempts_no_delete
    BEFORE DELETE ON discord_outbound_part_attempts
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS discord_author_mapping_events (
    mapping_event_id       TEXT PRIMARY KEY,
    discord_author_id         TEXT NOT NULL,
    action                       TEXT NOT NULL CHECK (action IN ('map','revoke')),
    principal_actor_id              TEXT NOT NULL,
    principal_display_label            TEXT,
    discord_username_snapshot             TEXT,
    requester_actor_id                       TEXT NOT NULL,
    occurred_at                                 INTEGER NOT NULL,
    created_at                                     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discord_author_mapping_events_author
    ON discord_author_mapping_events(discord_author_id);
CREATE TRIGGER IF NOT EXISTS trg_discord_author_mapping_events_no_update
    BEFORE UPDATE ON discord_author_mapping_events
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_discord_author_mapping_events_no_delete
    BEFORE DELETE ON discord_author_mapping_events
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;

"""


def apply_additive_migration(db_path: str) -> dict:
    """Idempotent additive migration. Never touches an existing table,
    column, constraint, or trigger."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        tables_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # executescript commits a pending transaction before executing its
        # script; BEGIN inside the script makes all DDL atomic.
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL + "\nCOMMIT;")
        except Exception:
            conn.rollback()
            raise
        tables_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {"new_tables_created": sorted(tables_after - tables_before)}
    finally:
        conn.close()


def apply_migration_on_connection(conn: sqlite3.Connection) -> None:
    """Ensure the additive tables on an ALREADY-OPEN connection. Used by
    the canonical engine so an idempotent insert path never races its own
    first-ever write. DDL is transactional on this connection."""
    conn.executescript(NEW_TABLES_DDL)


def verify_migration_state(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {
            "has_discord_destination_events": "discord_destination_events" in tables,
            "has_discord_destination_registry_state": "discord_destination_registry_state" in tables,
            "has_discord_inbound_seen": "discord_inbound_seen" in tables,
            "has_discord_outbound_parts": "discord_outbound_parts" in tables,
            "has_discord_outbound_part_attempts": "discord_outbound_part_attempts" in tables,
            "has_discord_author_mapping_events": "discord_author_mapping_events" in tables,
        }
    finally:
        conn.close()
