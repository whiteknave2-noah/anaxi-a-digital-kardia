"""SLP2: additive schema for Sleep-timing agency + reciprocal Knock.

Additive SLP2 tables and guards. Correction-2 also installs the canonical
identity guards from provenance_schema on existing databases. Historical
trigger definitions and data remain unchanged. Mirrors OC0's and WTR0's
additive-schema convention.

  sleep_requests -- one mutable summary row per canonical Clark
  root Sleep Request act (event_type='clark_sleep_request'). `state`
  is the current intentional lifecycle state (PENDING/DEFERRED/
  AUTHORIZED/WITHDRAWN/DECLINED); every transition is applied under
  BEGIN IMMEDIATE with a compare-and-swap UPDATE (see
  sleep_timing_knock.py) and is ALSO appended, permanently, to
  sleep_request_transitions below -- this table is a convenience
  cache of "current state," never the sole record of history.
  `triggering_human_event_id` is OPTIONAL (nullable): if present, it
  must reference an existing human_waking_input event that is
  canonically not later than this request (the human turn Clark was
  answering when he made this request) -- never fabricated, never
  required.

  sleep_open_requests -- durable "at most one unresolved request per
  actor" enforcement. UNIQUE(actor_id) is the actual backstop against
  a race between two simultaneous REQUEST attempts for the same actor
  (see sleep_timing_knock.record_sleep_request()'s BEGIN IMMEDIATE
  transaction, which attempts this INSERT FIRST, before writing any
  canonical event row, so a rejected duplicate REQUEST creates ZERO
  rows anywhere -- not even a dangling canonical event). The row is
  removed only when the referenced request reaches a terminal state
  (AUTHORIZED/WITHDRAWN/DECLINED); it survives PENDING and DEFERRED,
  since DEFERRED is explicitly still "unresolved" (frozen semantic
  principle: deferred means "not authorized or declined yet").

  sleep_knocks -- append-only canonical linkage from each distinct
  Clark-originated Knock act (event_type='clark_sleep_knock') to its
  root request. trg_sleep_knocks_event_exists enforces the referenced
  event actually exists with the right event_type at insert time --
  never a dangling TEXT reference. knock_sequence is allocated by the
  writer under BEGIN IMMEDIATE (1, 2, 3, ...); UNIQUE(request_id,
  knock_sequence) is the durable backstop.

  sleep_withdrawals -- append-only canonical linkage from a Clark-
  originated Withdrawal act (event_type='clark_sleep_withdrawal') to
  its root request. UNIQUE(request_id): a request can be withdrawn at
  most once, structurally.

  sleep_request_transitions -- the permanent, append-only audit
  ledger of every state-changing act against a request: REQUEST,
  KNOCK, WITHDRAW (Clark-originated), DEFER, AUTHORIZE, DECLINE
  (owner-originated). This is the actual source of truth for history;
  sleep_requests.state is a queryable cache of its latest entry. An
  optional owner `note` is stored here, attached to its own DEFER/
  AUTHORIZE/DECLINE row, with its own provenance -- it is never
  parsed or used to decide the transition itself (frozen semantic
  principle 8: note contents never determine a mechanical transition).

  sleep_execution_attempts -- append-only receipts for an explicit,
  separately-invoked attempt to execute Sleep against an AUTHORIZED
  request through the existing manual Sleep pathway.
  trg_sleep_execution_attempts_authorized enforces, at insert time,
  that the referenced request is (at that instant) in state
  'AUTHORIZED' -- REQUEST/KNOCK can never reach this table, and
  neither can PENDING/DEFERRED/WITHDRAWN/DECLINED. Recording SUCCEEDED
  or FAILED here never rewrites sleep_requests.state (frozen semantic
  principle 9: authorization != successful Sleep; a failed attempt
  leaves the request historically AUTHORIZED, not DECLINED/DEFERRED).
  SLP2-CORRECTION-1 (owner-frozen policy): AUTHORIZATION IS NOT
  CONSUMED BY A SLEEP ATTEMPT -- there is no uniqueness constraint
  bounding how many attempt rows may exist per request_id (only
  UNIQUE(request_id, attempt_sequence), an ordering/identity
  constraint, never a count cap); a single AUTHORIZED request may
  support any number of separately-invoked attempts. Each carries its
  own operator_actor_id (added by this correction via a guarded ALTER
  TABLE, see _add_columns_if_missing below, and enforced NOT NULL at
  insert time by the separately-named trg_sleep_execution_attempts_
  operator_required trigger) -- every attempt is durably attributed to
  the exact registered human actor who explicitly invoked it.

Not invoked against the live anaxi_provenance.db by this job -- SLP2
is exercised only against synthetic/temp databases (see
test_sleep_timing_knock.py), matching OC0/WTR0's own deferral of any
live cutover.
"""
import sqlite3

from provenance_schema import IDENTITY_GUARD_DDL, install_identity_guards

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS sleep_requests (
    request_id                  TEXT PRIMARY KEY,
    actor_id                    TEXT NOT NULL,
    state                       TEXT NOT NULL CHECK (state IN ('PENDING','DEFERRED','AUTHORIZED','WITHDRAWN','DECLINED')),
    triggering_human_event_id   TEXT,
    requested_at                INTEGER NOT NULL,
    updated_at                  INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_sleep_requests_event_exists
    BEFORE INSERT ON sleep_requests
BEGIN
    SELECT RAISE(ABORT, 'request_id must reference an existing clark_sleep_request event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.request_id AND event_type = 'clark_sleep_request'
    );
    SELECT RAISE(ABORT, 'triggering_human_event_id must reference an existing human_waking_input event not canonically later than this request')
    WHERE NEW.triggering_human_event_id IS NOT NULL AND (
        NOT EXISTS (
            SELECT 1 FROM events WHERE event_id = NEW.triggering_human_event_id AND event_type = 'human_waking_input'
        )
        OR (SELECT occurred_at FROM events WHERE event_id = NEW.triggering_human_event_id)
           > (SELECT occurred_at FROM events WHERE event_id = NEW.request_id)
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_requests_no_delete
    BEFORE DELETE ON sleep_requests BEGIN SELECT RAISE(ABORT, 'sleep_requests rows are never deleted'); END;

CREATE TABLE IF NOT EXISTS sleep_open_requests (
    actor_id     TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sleep_knocks (
    knock_id         TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL,
    knock_sequence   INTEGER NOT NULL CHECK (knock_sequence >= 1),
    occurred_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sleep_knocks_seq ON sleep_knocks(request_id, knock_sequence);
CREATE TRIGGER IF NOT EXISTS trg_sleep_knocks_event_exists
    BEFORE INSERT ON sleep_knocks
BEGIN
    SELECT RAISE(ABORT, 'knock_id must reference an existing clark_sleep_knock event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.knock_id AND event_type = 'clark_sleep_knock'
    );
    SELECT RAISE(ABORT, 'request_id must reference an existing sleep_requests row')
    WHERE NOT EXISTS (SELECT 1 FROM sleep_requests WHERE request_id = NEW.request_id);
END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_knocks_no_update
    BEFORE UPDATE ON sleep_knocks BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_knocks_no_delete
    BEFORE DELETE ON sleep_knocks BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS sleep_withdrawals (
    withdrawal_id   TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL UNIQUE,
    occurred_at     INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_sleep_withdrawals_event_exists
    BEFORE INSERT ON sleep_withdrawals
BEGIN
    SELECT RAISE(ABORT, 'withdrawal_id must reference an existing clark_sleep_withdrawal event')
    WHERE NOT EXISTS (
        SELECT 1 FROM events WHERE event_id = NEW.withdrawal_id AND event_type = 'clark_sleep_withdrawal'
    );
    SELECT RAISE(ABORT, 'request_id must reference an existing sleep_requests row')
    WHERE NOT EXISTS (SELECT 1 FROM sleep_requests WHERE request_id = NEW.request_id);
END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_withdrawals_no_update
    BEFORE UPDATE ON sleep_withdrawals BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_withdrawals_no_delete
    BEFORE DELETE ON sleep_withdrawals BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS sleep_request_transitions (
    transition_id   TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('REQUEST','KNOCK','WITHDRAW','DEFER','AUTHORIZE','DECLINE')),
    actor_id        TEXT NOT NULL,
    actor_role      TEXT NOT NULL CHECK (actor_role IN ('clark','owner')),
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    occurred_at     INTEGER NOT NULL,
    note            TEXT,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sleep_transitions_request ON sleep_request_transitions(request_id);
CREATE TRIGGER IF NOT EXISTS trg_sleep_transitions_no_update
    BEFORE UPDATE ON sleep_request_transitions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_transitions_no_delete
    BEFORE DELETE ON sleep_request_transitions BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS sleep_execution_attempts (
    execution_attempt_id   TEXT PRIMARY KEY,
    request_id             TEXT NOT NULL,
    attempt_sequence       INTEGER NOT NULL CHECK (attempt_sequence >= 1),
    attempted_at           INTEGER NOT NULL,
    outcome                TEXT NOT NULL CHECK (outcome IN ('attempted','succeeded','failed','not_established')),
    detail                 TEXT,
    created_at             INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sleep_execution_attempts_seq ON sleep_execution_attempts(request_id, attempt_sequence);
CREATE TRIGGER IF NOT EXISTS trg_sleep_execution_attempts_authorized
    BEFORE INSERT ON sleep_execution_attempts
BEGIN
    SELECT RAISE(ABORT, 'request_id must reference a sleep_requests row currently in state AUTHORIZED')
    WHERE NOT EXISTS (
        SELECT 1 FROM sleep_requests WHERE request_id = NEW.request_id AND state = 'AUTHORIZED'
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_execution_attempts_no_update
    BEFORE UPDATE ON sleep_execution_attempts BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sleep_execution_attempts_no_delete
    BEFORE DELETE ON sleep_execution_attempts BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# SLP2-CORRECTION-1: sleep_execution_attempts originally shipped
# (initial SLP2 BUILT, 5f375b25) with no column recording WHO invoked
# a given Sleep execution attempt. The owner's frozen policy requires
# "each attempt must be attributable to the explicit operator execution
# action according to existing actor/provenance conventions." Since
# ALTER TABLE ... ADD COLUMN on an existing table is not expressible
# via CREATE TABLE IF NOT EXISTS, this follows the SAME guarded idiom
# api1_schema_migration.py/migrate_historical_data.py already
# establish (PRAGMA table_info existence check, then ALTER TABLE ADD
# COLUMN, idempotent, never destructive) rather than editing the
# CREATE TABLE statement above in place -- that statement must stay
# byte-identical so CREATE TABLE IF NOT EXISTS keeps correctly treating
# an already-migrated (pre-Correction-1) database as "table already
# exists" and falls through to this guarded column-add step, exactly
# the upgrade path this correction needs to support.
#
# The enforcing trigger is DELIBERATELY a new, previously-nonexistent
# name (trg_sleep_execution_attempts_operator_required), never added to
# trg_sleep_execution_attempts_authorized's own body above -- the exact
# lesson this codebase already learned once (OC0's reply-link
# chronology correction): CREATE TRIGGER IF NOT EXISTS is a no-op once
# a trigger of that name exists, regardless of whether its definition
# changed, so a database already migrated by the pre-Correction-1
# trigger definition would never gain new logic added to that
# trigger's body on re-migration. A new name installs cleanly via
# CREATE TRIGGER IF NOT EXISTS on both a fresh database and one
# migrated by the original SLP2 schema.
_OPERATOR_COLUMN = {"operator_actor_id": "TEXT"}

_OPERATOR_REQUIRED_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_sleep_execution_attempts_operator_required
    BEFORE INSERT ON sleep_execution_attempts
BEGIN
    SELECT RAISE(ABORT, 'operator_actor_id must be recorded for every Sleep execution attempt')
    WHERE NEW.operator_actor_id IS NULL;
END;
"""

# Correction-2: positive canonical registration, not a spelling heuristic
# or a claim about who is presently at the keyboard. Prospective only:
# historical NULL/invalid provenance is neither guessed nor rewritten.
_OPERATOR_HUMAN_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_sleep_execution_attempts_registered_human
    BEFORE INSERT ON sleep_execution_attempts
BEGIN
    SELECT RAISE(ABORT, 'operator_actor_id must resolve to a canonical registered human_person')
    WHERE typeof(NEW.operator_actor_id) != 'text'
       OR trim(NEW.operator_actor_id, ' ' || char(9) || char(10) || char(13)) = ''
       OR NOT EXISTS (
           SELECT 1 FROM actors a
           JOIN actor_human_person h ON h.actor_id = a.actor_id
           JOIN persons p ON p.person_id = h.person_id
           WHERE a.actor_id = NEW.operator_actor_id AND a.actor_type = 'human_person'
       );
END;
"""


def _add_columns_if_missing(conn, table, columns):
    """Same idiom as api1_schema_migration.py's helper of the same
    name (not imported from there, to keep this schema-only module
    dependency-free of that unrelated module)."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for col, sql_type in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
            added.append(col)
    return added


def apply_additive_migration(db_path: str) -> dict:
    """Idempotent additive migration. Never touches a live/production
    file by contract of this job -- callers point it only at synthetic
    or rehearsal database paths. Safe to run against a fresh database,
    a database already migrated by the original SLP2 schema (pre-
    Correction-1), or one already carrying this correction's own
    column/trigger -- each case converges on the identical final
    schema, and no existing row is ever rewritten."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        try:
            # One transaction for tables, columns, and both guard layers.
            # executescript starts it; subsequent execute calls do not commit.
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL)
            added_columns = _add_columns_if_missing(conn, "sleep_execution_attempts", _OPERATOR_COLUMN)
            conn.execute(_OPERATOR_REQUIRED_TRIGGER_DDL.strip())
            install_identity_guards(conn)
            conn.execute(_OPERATOR_HUMAN_TRIGGER_DDL.strip())
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        return {
            "new_tables_created": sorted(existing_after - existing_before),
            "sleep_execution_attempts_columns_added": added_columns,
        }
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
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        columns = set()
        if "sleep_execution_attempts" in tables:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(sleep_execution_attempts)").fetchall()}
        return {
            "has_sleep_requests": "sleep_requests" in tables,
            "has_sleep_open_requests": "sleep_open_requests" in tables,
            "has_sleep_knocks": "sleep_knocks" in tables,
            "has_sleep_withdrawals": "sleep_withdrawals" in tables,
            "has_sleep_request_transitions": "sleep_request_transitions" in tables,
            "has_sleep_execution_attempts": "sleep_execution_attempts" in tables,
            "has_sleep_execution_attempts_operator_actor_id": "operator_actor_id" in columns,
            "has_sleep_execution_attempts_operator_required_trigger":
                "trg_sleep_execution_attempts_operator_required" in triggers,
            "has_sleep_execution_attempts_registered_human_trigger":
                "trg_sleep_execution_attempts_registered_human" in triggers,
            "has_canonical_identity_guards": all(
                ddl.split()[5] in triggers for ddl in IDENTITY_GUARD_DDL
            ),
        }
    finally:
        conn.close()
