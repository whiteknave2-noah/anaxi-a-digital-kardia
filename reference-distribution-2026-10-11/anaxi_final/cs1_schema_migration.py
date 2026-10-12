"""CS1 -- general correspondence surfaces: additive, append-only ledgers.

Applied idempotently on the writing connection (the dc0 idiom), so production picks it up on first
use with no manual step.  Nothing here rewrites or deletes a row: every table is a ledger reduced at
read time (correspondence_surfaces.py).

  correspondence_surface_policy_events   owner: may an authorized destination admit sources that
                                         are not mapped ANAXI principals; may Clark independently
                                         initiate there (default: neither)
  correspondence_source_block_events     owner: hard safety block of one stable source id (never
                                         relational meaning)
  correspondent_standing_events          CLARK ONLY: establish / revoke ongoing correspondence with
                                         one stable source; bound to the canonical waking turn in
                                         which he typed that choice (DB-enforced)
  correspondence_occasion_attempts       host: one row per failed waking attempt for an inbound
                                         occasion (durable: retry exhaustion survives restart)
  correspondence_occasion_owner_acts     owner: administratively close / reopen one inbound occasion
  correspondence_outbound_lifecycle_events
                                         Clark withdrawal (bound to his canonical turn) or owner
                                         administrative closure of a still-undelivered authored send
"""
import sqlite3

# derive_stable_id("actor", "clark") -- the same literal od1_schema_migration pins.
CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS correspondence_surface_policy_events (
    policy_event_id                TEXT PRIMARY KEY,
    destination_id                 TEXT NOT NULL,
    allow_unknown_sources          INTEGER NOT NULL CHECK (allow_unknown_sources IN (0, 1)),
    permit_independent_initiation  INTEGER NOT NULL CHECK (permit_independent_initiation IN (0, 1)),
    requester_actor_id             TEXT NOT NULL,
    occurred_at                    INTEGER NOT NULL,
    created_at                     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS correspondence_source_block_events (
    block_event_id      TEXT PRIMARY KEY,
    source_kind         TEXT NOT NULL,
    source_value        TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('block', 'unblock')),
    requester_actor_id  TEXT NOT NULL,
    occurred_at         INTEGER NOT NULL,
    created_at          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS correspondent_standing_events (
    standing_event_id     TEXT PRIMARY KEY,
    source_kind           TEXT NOT NULL,
    source_value          TEXT NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('establish', 'revoke')),
    requester_actor_id    TEXT NOT NULL,
    waking_turn_event_id  TEXT NOT NULL UNIQUE,
    occurred_at           INTEGER NOT NULL,
    created_at            INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS correspondence_occasion_attempts (
    attempt_id        TEXT PRIMARY KEY,
    inbound_event_id  TEXT NOT NULL,
    failure_class     TEXT NOT NULL,
    attempted_at      INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS correspondence_occasion_owner_acts (
    act_id              TEXT PRIMARY KEY,
    inbound_event_id    TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('close', 'reopen')),
    requester_actor_id  TEXT NOT NULL,
    attempt_watermark   INTEGER NOT NULL,   -- rowid of the last attempt row when the act was recorded
    occurred_at         INTEGER NOT NULL,
    created_at          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS correspondence_outbound_lifecycle_events (
    lifecycle_event_id    TEXT PRIMARY KEY,
    send_turn_event_id    TEXT NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('withdrawn_by_clark', 'closed_by_owner',
                                                          'host_resume_failed')),
    requester_actor_id    TEXT NOT NULL,
    evidence_event_id     TEXT,
    detail                TEXT,
    occurred_at           INTEGER NOT NULL,
    created_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs1_standing_source ON correspondent_standing_events(source_kind, source_value);
CREATE INDEX IF NOT EXISTS idx_cs1_attempts_inbound ON correspondence_occasion_attempts(inbound_event_id);
CREATE INDEX IF NOT EXISTS idx_cs1_owner_acts_inbound ON correspondence_occasion_owner_acts(inbound_event_id);
CREATE INDEX IF NOT EXISTS idx_cs1_outbound_turn ON correspondence_outbound_lifecycle_events(send_turn_event_id);

CREATE TRIGGER IF NOT EXISTS trg_cs1_standing_is_clarks_typed_choice
    BEFORE INSERT ON correspondent_standing_events
BEGIN
    SELECT RAISE(ABORT, 'correspondent standing is Clark-controlled only')
    WHERE NEW.requester_actor_id != 'actor-80eee447ad46e1a1e9b2ea65b5';
    SELECT RAISE(ABORT, 'standing must be bound to the canonical waking turn in which Clark chose it')
    WHERE 1 != (
        SELECT COUNT(*) FROM event_components c JOIN events e ON e.event_id = c.event_id
        WHERE c.event_id = NEW.waking_turn_event_id AND e.event_type = 'waking_turn'
          AND c.component_kind = 'correspondent_standing_request'
          AND c.creator_actor_id = 'actor-80eee447ad46e1a1e9b2ea65b5'
          AND c.component_text = NEW.action || ':' || NEW.source_kind || ':' || NEW.source_value
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_cs1_withdrawal_is_clarks_typed_choice
    BEFORE INSERT ON correspondence_outbound_lifecycle_events
    WHEN NEW.action = 'withdrawn_by_clark'
BEGIN
    SELECT RAISE(ABORT, 'a withdrawal must be bound to the canonical waking turn in which Clark chose it')
    WHERE NEW.requester_actor_id != 'actor-80eee447ad46e1a1e9b2ea65b5' OR 1 != (
        SELECT COUNT(*) FROM event_components c JOIN events e ON e.event_id = c.event_id
        WHERE c.event_id = NEW.evidence_event_id AND e.event_type = 'waking_turn'
          AND c.component_kind = 'withdraw_pending_send_request'
          AND c.creator_actor_id = 'actor-80eee447ad46e1a1e9b2ea65b5'
          AND c.component_text = NEW.send_turn_event_id
    );
END;
"""

_TABLES = ("correspondence_surface_policy_events", "correspondence_source_block_events",
           "correspondent_standing_events", "correspondence_occasion_attempts",
           "correspondence_occasion_owner_acts", "correspondence_outbound_lifecycle_events")
_APPEND_ONLY_DDL = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS trg_{t}_no_update BEFORE UPDATE ON {t}
    BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_{t}_no_delete BEFORE DELETE ON {t}
    BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"""
    for t in _TABLES)


def apply_migration_on_connection(conn: sqlite3.Connection) -> None:
    """Idempotent; executescript commits any open transaction first -- call outside one."""
    conn.executescript(NEW_TABLES_DDL + _APPEND_ONLY_DDL)


def tables_present(conn) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (%s)" % ",".join("?" * len(_TABLES)),
        _TABLES).fetchone()[0] == len(_TABLES)
