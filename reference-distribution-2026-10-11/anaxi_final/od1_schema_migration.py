"""OD1: additive schema for the Clark-Controlled Reversible Operative
Directive V0.

Additive OD1 tables. Mirrors OC0's/SLP2's/WTR0's additive-schema
convention (guarded CREATE ... IF NOT EXISTS, one transaction,
rollback on error).

  operative_directives -- the CURRENT derived state, one row per actor
  ONLY WHILE a directive is active. Row presence IS the active state;
  row absence IS the valid null state ("NO ACTIVE OPERATIVE
  DIRECTIVE") -- no sentinel value is needed to represent "none",
  which is exactly the smallest deterministic projection O6 asks for.
  PRIMARY KEY(actor_id) structurally guarantees at most one active
  directive per actor -- unlike SLP2's sleep_open_requests, no separate
  uniqueness index is needed, because THIS table's own row IS the
  "currently active" fact (sleep_requests.state, by contrast, has
  multiple non-active values (WITHDRAWN/DECLINED/AUTHORIZED) live in
  the same table, which is why SLP2 needs a second index for
  "unresolved"). ACTIVATE inserts this row; REPLACE updates it in
  place (active_event_id/directive_text/activated_at all change
  together); WITHDRAW deletes it. None of these three operations is
  itself append-only -- durable HISTORY of every transition instead
  lives permanently in operative_directive_transitions below, exactly
  mirroring sleep_requests (mutable cache) vs. sleep_request_transitions
  (permanent ledger).

  operative_directive_transitions -- the permanent, append-only audit
  ledger of every ACTIVATE/REPLACE/WITHDRAW. This is the actual source
  of truth for history; operative_directives is a queryable cache of
  "current state." event_id is UNIQUE: each transition corresponds to
  exactly one canonical clark_operative_directive_* event, enforced to
  actually exist (with the matching event_type for its action) by
  trg_operative_directive_transitions_event_exists. previous_event_id
  is OPTIONAL (nullable): NULL for ACTIVATE (there was nothing before),
  set for REPLACE/WITHDRAW to the directive event that was active
  immediately before this transition -- never enforced against
  operative_directives itself (that row may already have moved on by
  the time this ledger is read), only recorded as a truthful fact about
  what preceded this transition at the moment it happened.
  triggering_waking_turn_event_id is schema-nullable for additive-
  migration compatibility but REQUIRED by the insert trigger. It must
  identify a committed native waking turn in the same session, carrying
  the matching resolved Clark-authored structured request and exact
  directive text. The partial unique index permits at most one directive
  transition per waking turn, including across retries.

Not invoked against the live anaxi_provenance.db by this job -- OD1 is
exercised only against synthetic/temp databases (see
test_operative_directive.py), matching OC0/SLP2/WTR0's own deferral of
any live cutover.
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS operative_directives (
    actor_id         TEXT PRIMARY KEY,
    active_event_id  TEXT NOT NULL,
    directive_text   TEXT NOT NULL,
    activated_at     INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_operative_directives_event_exists_insert
    BEFORE INSERT ON operative_directives
BEGIN
    SELECT RAISE(ABORT, 'active_event_id must reference an existing clark_operative_directive_activated or _replaced event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.active_event_id
        AND event_type IN ('clark_operative_directive_activated', 'clark_operative_directive_replaced')
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_operative_directives_event_exists_update
    BEFORE UPDATE ON operative_directives
BEGIN
    SELECT RAISE(ABORT, 'active_event_id must reference an existing clark_operative_directive_activated or _replaced event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.active_event_id
        AND event_type IN ('clark_operative_directive_activated', 'clark_operative_directive_replaced')
    );
END;

CREATE TABLE IF NOT EXISTS operative_directive_transitions (
    transition_id                     TEXT PRIMARY KEY,
    actor_id                          TEXT NOT NULL,
    action                             TEXT NOT NULL CHECK (action IN ('ACTIVATE','REPLACE','WITHDRAW')),
    event_id                           TEXT NOT NULL UNIQUE,
    previous_event_id                  TEXT,
    directive_text                     TEXT,
    triggering_waking_turn_event_id    TEXT,
    occurred_at                        INTEGER NOT NULL,
    created_at                         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operative_directive_transitions_actor ON operative_directive_transitions(actor_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operative_directive_transitions_waking_turn
    ON operative_directive_transitions(triggering_waking_turn_event_id)
    WHERE triggering_waking_turn_event_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS trg_operative_directive_transitions_event_exists
    BEFORE INSERT ON operative_directive_transitions
BEGIN
    SELECT RAISE(ABORT, 'every directive transition must be bound to one canonical waking turn')
    WHERE NEW.triggering_waking_turn_event_id IS NULL;
    SELECT RAISE(ABORT, 'directive transitions are Clark-originated only')
    WHERE NEW.actor_id != 'actor-80eee447ad46e1a1e9b2ea65b5';
    SELECT RAISE(ABORT, 'event_id must reference an existing canonical event of the matching operative-directive event_type')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.event_id AND (
            (NEW.action = 'ACTIVATE' AND event_type = 'clark_operative_directive_activated')
            OR (NEW.action = 'REPLACE' AND event_type = 'clark_operative_directive_replaced')
            OR (NEW.action = 'WITHDRAW' AND event_type = 'clark_operative_directive_withdrawn')
        )
    );
    SELECT RAISE(ABORT, 'triggering_waking_turn_event_id must reference a committed native waking_turn in the same session not later than this transition')
    WHERE (
        NOT EXISTS (
            SELECT 1 FROM events w
            JOIN auth_contexts wa ON wa.auth_context_id = w.auth_context_id
            JOIN events d ON d.event_id = NEW.event_id
            JOIN auth_contexts da ON da.auth_context_id = d.auth_context_id
            WHERE w.event_id = NEW.triggering_waking_turn_event_id
              AND w.event_type = 'waking_turn'
              AND w.pipeline_provenance_status = 'known'
              AND w.pipeline_id IS NOT NULL
              AND w.input_source_ref IS NOT NULL
              AND wa.session_id = da.session_id
        )
        OR (SELECT occurred_at FROM events WHERE event_id = NEW.triggering_waking_turn_event_id)
           > (SELECT occurred_at FROM events WHERE event_id = NEW.event_id)
    );
    SELECT RAISE(ABORT, 'triggering waking turn is not a complete canonical waking completion')
    WHERE NOT EXISTS (
        SELECT 1 FROM event_model_participation
        WHERE event_id = NEW.triggering_waking_turn_event_id
          AND participation_note IN (
              'Model backing this native waking turn''s pass-2 conversational reply.',
              'Model backing this native waking turn''s pass-1 typed choice; no pass-2 reply was composed.')
    );
    SELECT RAISE(ABORT, 'triggering waking turn lacks the matching explicit Clark structured action')
    WHERE 1 != (
        SELECT COUNT(*) FROM event_components
        WHERE event_id = NEW.triggering_waking_turn_event_id
          AND component_kind = 'operative_directive_request'
          AND creator_actor_id = 'actor-80eee447ad46e1a1e9b2ea65b5'
          AND authorship_resolution = 'resolved'
          AND component_text = CASE WHEN NEW.action = 'WITHDRAW' THEN 'withdraw_directive' ELSE 'set_directive' END
    );
    SELECT RAISE(ABORT, 'triggering waking turn directive text must exactly match the canonical occurrence')
    WHERE (
        NEW.action IN ('ACTIVATE','REPLACE') AND 1 != (
            SELECT COUNT(*) FROM event_components
            WHERE event_id = NEW.triggering_waking_turn_event_id
              AND component_kind = 'operative_directive_text'
              AND creator_actor_id = 'actor-80eee447ad46e1a1e9b2ea65b5'
              AND authorship_resolution = 'resolved'
              AND component_text = NEW.directive_text
        )
    ) OR (
        NEW.action = 'WITHDRAW' AND EXISTS (
            SELECT 1 FROM event_components
            WHERE event_id = NEW.triggering_waking_turn_event_id
              AND component_kind = 'operative_directive_text'
        )
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_operative_directive_transitions_no_update
    BEFORE UPDATE ON operative_directive_transitions
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_operative_directive_transitions_no_delete
    BEFORE DELETE ON operative_directive_transitions
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


# LAWFUL NULL (2026-09-23): a turn completed by Clark's typed no_reply choice is a complete canonical
# waking completion whose participating model is recorded under the pass-1 typed-choice note (no Pass
# 2 ran).  Databases migrated before this carry the transition trigger that accepted only the pass-2
# note, and CREATE TRIGGER IF NOT EXISTS never replaces it, so the trigger is upgraded in place:
# dropped and recreated with the widened predicate inside one transaction.  No row is touched.
_TRANSITION_TRIGGER = "trg_operative_directive_transitions_event_exists"
_NULL_NOTE_FRAGMENT = "pass-1 typed choice; no pass-2 reply was composed."


def upgrade_transition_trigger_on_connection(conn) -> bool:
    """Idempotent. Returns True iff an older transition trigger was replaced. Caller owns the
    connection; must be called outside an open transaction."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                       (_TRANSITION_TRIGGER,)).fetchone()
    if row is None or _NULL_NOTE_FRAGMENT in (row[0] or ""):
        return False
    start = NEW_TABLES_DDL.index(f"CREATE TRIGGER IF NOT EXISTS {_TRANSITION_TRIGGER}")
    end = NEW_TABLES_DDL.index("END;", start) + len("END;")
    ddl = NEW_TABLES_DDL[start:end]
    conn.executescript(f"BEGIN IMMEDIATE;\nDROP TRIGGER {_TRANSITION_TRIGGER};\n{ddl}\nCOMMIT;")
    return True


def apply_additive_migration(db_path: str) -> dict:
    """Idempotent additive migration. Never touches a live/production
    file by contract of this job -- callers point it only at synthetic
    or rehearsal database paths. Safe to run against a fresh database
    or one already carrying this exact schema -- both converge on the
    identical final schema, and no existing row is ever rewritten."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        upgraded = upgrade_transition_trigger_on_connection(conn)
        return {"new_tables_created": sorted(existing_after - existing_before),
                "transition_trigger_upgraded": upgraded}
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
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        return {
            "has_operative_directives": "operative_directives" in tables,
            "has_operative_directive_transitions": "operative_directive_transitions" in tables,
            "has_operative_directives_insert_guard": "trg_operative_directives_event_exists_insert" in triggers,
            "has_operative_directives_update_guard": "trg_operative_directives_event_exists_update" in triggers,
            "has_operative_directive_transitions_event_guard":
                "trg_operative_directive_transitions_event_exists" in triggers,
            "has_operative_directive_transitions_waking_turn_uniqueness":
                "idx_operative_directive_transitions_waking_turn" in {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
                },
            "has_operative_directive_transitions_no_update": "trg_operative_directive_transitions_no_update" in triggers,
            "has_operative_directive_transitions_no_delete": "trg_operative_directive_transitions_no_delete" in triggers,
        }
    finally:
        conn.close()
