"""WTR0: additive schema for Waking Turn Recovery foundation.

Two new tables only. No existing table's columns, CHECK constraints,
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

`wtr0_recovery` is the durable one-recovery-attempt ledger. Exactly one
row may ever exist per human_input_event_id -- enforced by the UNIQUE
constraint below, not merely by application-level check-then-insert,
so that a second reservation for the same H is refused by SQLite
itself even under concurrent/racing invocations (see
wtr0_waking_recovery.reserve_recovery()'s BEGIN IMMEDIATE transaction).
Once a row exists at all -- regardless of its terminal_state -- no
second recovery attempt for that H is ever possible again. This row
never becomes an eternal authorization to run generation later, either:
reservation happens in the same transaction as the immediate fail-closed
eligibility re-check that precedes it.

Neither table is canonical human/Clark provenance (events/
event_components) and neither is ever treated as if it were: these are
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
"""


def apply_additive_migration(db_path):
    """Idempotent additive migration -- CREATE TABLE/INDEX IF NOT EXISTS
    only. Never touches or derives rows from existing canonical events;
    applying it to an established database therefore creates a
    truthfully empty prospective WTR0 ledger, with no historical
    backfill."""
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
        return {
            "has_waking_failure_evidence": "waking_failure_evidence" in tables,
            "has_wtr0_recovery": "wtr0_recovery" in tables,
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
