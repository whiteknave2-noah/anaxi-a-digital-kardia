"""WTR0: additive schema for Waking Turn Recovery foundation.

Additive tables only. No existing table's columns, CHECK constraints,
or triggers are touched -- events/event_components/auth_contexts/etc.
are read via ordinary SELECTs (see wtr0_waking_recovery.py) and never
altered. This mirrors HIR1-S1's own additive-schema convention
(hir1_schema_migration.py) and OC0's (oc0_schema_migration.py).

`waking_failure_evidence` is the prospective, bounded record of a
mechanically-classified waking-attempt failure tied to one canonical
human_waking_input event (H). It is written by
waking_turn_failure_capture.py immediately after an instrumented
waking call raises, never retroactively for historical turns, and
never claims semantic/subjective failure interpretation -- only a
bounded failure_class from waking_failure_evidence.py's own fixed
allowlist plus the mechanical basis for that classification.

`wtr0_recovery` remains the one durable recovery identity per H. Exactly one
row may ever exist per human_input_event_id -- enforced by the UNIQUE
constraint below, so a second independent identity for the same H is
refused by SQLite even under concurrent/racing invocations.
`wtr0_recovery_attempt` appends one receipt per physical execution. At
most one attempt can be ACTIVE, and a second attempt is permitted only by
the narrow same-identity resume law. The original failed attempt is never
deleted or rewritten away.

None of these tables is canonical human/Clark provenance (events/
event_components) or treated as if it were: these are
purely mechanical WTR0 bookkeeping rows, never a substitute for the
canonical human_waking_input (H) or waking_turn (X) events, and never
mutate them.
"""
import argparse
import json
import os
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS waking_failure_evidence (
    failure_evidence_id TEXT PRIMARY KEY,
    human_input_event_id TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    retry_safe INTEGER NOT NULL CHECK (retry_safe IN (0, 1)),
    basis TEXT NOT NULL,
    detail TEXT,
    occurred_at INTEGER,
    occurred_at_unavailable INTEGER NOT NULL DEFAULT 0 CHECK (occurred_at_unavailable IN (0, 1)),
    recorded_at INTEGER NOT NULL,
    CHECK (
        (occurred_at_unavailable = 0 AND occurred_at IS NOT NULL)
        OR (occurred_at_unavailable = 1 AND occurred_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wfe_human_input_event_id
    ON waking_failure_evidence(human_input_event_id);

CREATE TABLE IF NOT EXISTS wtr0_recovery (
    recovery_id TEXT PRIMARY KEY,
    human_input_event_id TEXT NOT NULL UNIQUE,
    failure_evidence_id TEXT NOT NULL,
    eligibility_decision TEXT NOT NULL,
    eligibility_basis TEXT NOT NULL,
    reserved_at INTEGER NOT NULL,
    reset_status TEXT NOT NULL DEFAULT 'not_attempted',
    reset_detail TEXT,
    reset_at INTEGER,
    generation_status TEXT NOT NULL DEFAULT 'not_attempted',
    generation_detail TEXT,
    generation_at INTEGER,
    persistence_status TEXT NOT NULL DEFAULT 'not_attempted',
    persistence_detail TEXT,
    canonical_x_event_id TEXT,
    terminal_state TEXT NOT NULL DEFAULT 'PENDING',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wtr0_recovery_human_input_event_id
    ON wtr0_recovery(human_input_event_id);

CREATE TABLE IF NOT EXISTS wtr0_recovery_attempt (
    recovery_id TEXT NOT NULL,
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
    actor_id TEXT NOT NULL,
    visibility_scope TEXT,
    started_at INTEGER NOT NULL,
    reset_status TEXT NOT NULL DEFAULT 'not_attempted',
    reset_detail TEXT,
    reset_at INTEGER,
    generation_status TEXT NOT NULL DEFAULT 'not_attempted',
    generation_detail TEXT,
    generation_at INTEGER,
    failure_stage TEXT,
    failure_code TEXT,
    pre_staging_proven INTEGER CHECK (pre_staging_proven IN (0, 1)),
    subject_output_released INTEGER CHECK (subject_output_released IN (0, 1)),
    persistence_status TEXT NOT NULL DEFAULT 'not_attempted',
    persistence_detail TEXT,
    canonical_x_event_id TEXT,
    retry_safe_resume INTEGER NOT NULL DEFAULT 0 CHECK (retry_safe_resume IN (0, 1)),
    terminal_state TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (recovery_id, attempt_ordinal),
    FOREIGN KEY (recovery_id) REFERENCES wtr0_recovery(recovery_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wtr0_one_active_attempt
    ON wtr0_recovery_attempt(recovery_id) WHERE terminal_state = 'ACTIVE';
"""

_LEGACY_RETRY_SAFE_DETAIL = (
    "ConversationDirectionFailure('conversation direction failed at pass2: "
    "COMPLETION_LIMIT_REACHED')"
)


def _backfill_existing_attempt_receipts(conn):
    """Append attempt 1 for identities created before attempt receipts.

    The old WTR0 writer itself authored ``generation_detail=repr(exc)``; this
    accepts only its exact concrete ConversationDirectionFailure rendering in
    combination with the independently structured reset/generation/
    persistence fields. Arbitrary GENERATION_FAILED text never qualifies.
    """
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    scope_expr = "e.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    rows = conn.execute(
        "SELECT r.recovery_id, r.human_input_event_id, r.reserved_at, "
        "r.reset_status, r.reset_detail, r.reset_at, r.generation_status, "
        "r.generation_detail, r.generation_at, r.persistence_status, "
        "r.persistence_detail, r.canonical_x_event_id, r.terminal_state, r.updated_at, "
        f"ec.creator_actor_id, {scope_expr} "
        "FROM wtr0_recovery r "
        "JOIN events e ON e.event_id=r.human_input_event_id "
        "JOIN event_components ec ON ec.event_id=e.event_id AND ec.sequence=0 "
        "AND ec.component_kind='human_conversational_input' "
        "WHERE NOT EXISTS (SELECT 1 FROM wtr0_recovery_attempt a "
        "WHERE a.recovery_id=r.recovery_id)"
    ).fetchall()
    for row in rows:
        exact_completion_limit = (
            row[12] == "GENERATION_FAILED"
            and row[3] == "SUCCEEDED"
            and row[6] == "FAILED"
            and row[7] == _LEGACY_RETRY_SAFE_DETAIL
            and row[9] == "not_attempted"
            and row[11] is None
        )
        conn.execute(
            "INSERT INTO wtr0_recovery_attempt "
            "(recovery_id, attempt_ordinal, actor_id, visibility_scope, started_at, "
            "reset_status, reset_detail, reset_at, generation_status, generation_detail, "
            "generation_at, failure_stage, failure_code, pre_staging_proven, "
            "subject_output_released, persistence_status, persistence_detail, "
            "canonical_x_event_id, retry_safe_resume, terminal_state, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row[0], row[14], row[15], row[2], row[3], row[4], row[5], row[6],
                row[7], row[8], "pass2" if exact_completion_limit else None,
                "COMPLETION_LIMIT_REACHED" if exact_completion_limit else None,
                1 if exact_completion_limit else None,
                0 if exact_completion_limit else None,
                row[9], row[10], row[11], 1 if exact_completion_limit else 0,
                "ACTIVE" if row[12] in ("PENDING", "RESERVED") else row[12], row[13],
            ),
        )


def apply_additive_migration(db_path):
    """Idempotent additive migration.

    Existing recovery identities receive one appended attempt-1 receipt;
    their rows and canonical H/X events are not edited. The exact legacy
    Pass-2 completion-limit representation is classified narrowly, with no
    identifier-specific special case.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        conn.executescript(NEW_TABLES_DDL)
        _backfill_existing_attempt_receipts(conn)
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
        return {
            "has_waking_failure_evidence": "waking_failure_evidence" in tables,
            "has_wtr0_recovery": "wtr0_recovery" in tables,
            "has_wtr0_recovery_attempt": "wtr0_recovery_attempt" in tables,
        }
    finally:
        conn.close()


def bookkeeping_row_counts(db_path):
    """Read-only postcondition receipt. Nonzero is valid after WTR0 has
    operated; a first production deployment expects both values to be
    zero and can verify that no historical backfill occurred."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            "waking_failure_evidence": conn.execute(
                "SELECT COUNT(*) FROM waking_failure_evidence"
            ).fetchone()[0],
            "wtr0_recovery": conn.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0],
            "wtr0_recovery_attempt": conn.execute(
                "SELECT COUNT(*) FROM wtr0_recovery_attempt"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def _validate_deployment_target(db_path):
    """Refuse a typo/empty SQLite target before the CLI can create it."""
    if not os.path.isfile(db_path):
        raise ValueError("target database does not exist")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    required = {"events", "event_components"}
    if not required.issubset(tables):
        raise ValueError("target is not an ANAXI canonical provenance database")


def main(argv=None):
    """Explicit deployment surface. The database path is required so
    migration can never silently target an adjacent/default production
    file. Prints both the additive receipt and a read-only postcondition
    suitable for the pre-wake deployment check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Exact anaxi_provenance.db path to migrate")
    args = parser.parse_args(argv)
    try:
        _validate_deployment_target(args.db)
    except ValueError as exc:
        parser.error(str(exc))
    receipt = apply_additive_migration(args.db)
    state = verify_migration_state(args.db)
    counts = bookkeeping_row_counts(args.db)
    print(json.dumps({"migration": receipt, "state": state, "row_counts": counts}, indent=2, sort_keys=True))
    return 0 if all(state.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
