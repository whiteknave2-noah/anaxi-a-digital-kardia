"""SLP1-S1: successful-Sleep watermark substrate (read-only in this
gate).

Freeze (spec section 13):

    building evidence != processing evidence
    reading evidence != consolidating evidence
    model call != successful consolidation
    only a successful future canonical SLP1 commit may advance the watermark

This module therefore implements ONLY watermark storage layout and a
read function. No advance/write function exists anywhere in this file
-- not merely "unused," genuinely absent -- so "evidence construction
cannot advance the watermark" is true by construction, not by
discipline. Writing the watermark is deferred to the gate that
actually performs a canonical commit (SLP1-S2/P1).

A brand new, dedicated SQLite database, deliberately NOT
`anaxi_mind_llama.db::sleep_watermarks` (the old system's own
watermark table) and NOT any table inside `anaxi_provenance.db` --
old 08/12/08/29 Sleep history does not and must not establish an SLP1
watermark position (spec section 12). Position is tracked as the
highest canonical `event_components.component_id` a successful SLP1
cycle has ever fully processed -- see sleep_evidence.py's own
component_id-ordered batching, which this module's position is
directly compatible with.
"""
import dataclasses
import os
import sqlite3


WATERMARK_DB_FILENAME = "sleep_v1_watermark.db"


@dataclasses.dataclass(frozen=True)
class SleepWatermarkPaths:
    watermark_db_path: str

    @staticmethod
    def production_defaults() -> "SleepWatermarkPaths":
        base = os.path.dirname(os.path.abspath(__file__))
        return SleepWatermarkPaths(watermark_db_path=os.path.join(base, WATERMARK_DB_FILENAME))


# The one row this table will ever logically have (id=1). Kept as a
# real table rather than a bare file so a future writer can use a
# single SQLite transaction spanning watermark-advance and any other
# state this same small database might need -- not exercised in this
# gate.
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS sleep_v1_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_component_id INTEGER NOT NULL,
    last_successful_cycle_id TEXT,
    last_successful_at INTEGER,
    cycle_count INTEGER NOT NULL
);
"""

# Explicit, fail-safe initial state (spec section 12): no prior SLP1
# cycle has ever succeeded, so nothing is considered already processed
# -- component_id=0 is smaller than every real component_id (an
# AUTOINCREMENT INTEGER PRIMARY KEY starting at 1), so "after the
# watermark" naturally means "all canonically eligible material from
# the beginning."
INITIAL_WATERMARK_STATE = {
    "last_processed_component_id": 0,
    "last_successful_cycle_id": None,
    "last_successful_at": None,
    "cycle_count": 0,
}


def read_watermark(paths: SleepWatermarkPaths) -> dict:
    """Strictly read-only -- never creates the watermark database
    file, never creates the table, never writes a row, regardless of
    whether the file already exists. If the file is entirely absent,
    or exists but has no schema/row yet, returns
    INITIAL_WATERMARK_STATE (a fresh dict, safe to mutate) -- the
    mechanically defined initial state (spec section 12), never
    inferred from legacy 08/12/08/29 Sleep history, never guessed.
    Table creation is deliberately left to whichever future function
    performs the first real watermark write (SLP1-S2/P1), as part of
    that write's own transaction -- not to this reader."""
    if not os.path.isfile(paths.watermark_db_path):
        return dict(INITIAL_WATERMARK_STATE)
    conn = sqlite3.connect(f"file:{paths.watermark_db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON;")
        tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sleep_v1_watermark'"
        ).fetchone()
        if tables is None:
            return dict(INITIAL_WATERMARK_STATE)
        row = conn.execute(
            "SELECT last_processed_component_id, last_successful_cycle_id, "
            "last_successful_at, cycle_count FROM sleep_v1_watermark WHERE id = 1"
        ).fetchone()
        if row is None:
            return dict(INITIAL_WATERMARK_STATE)
        last_processed_component_id, last_successful_cycle_id, last_successful_at, cycle_count = row
        return {
            "last_processed_component_id": last_processed_component_id,
            "last_successful_cycle_id": last_successful_cycle_id,
            "last_successful_at": last_successful_at,
            "cycle_count": cycle_count,
        }
    finally:
        conn.close()


# ================================================================
# SLP1-C: the FIRST authorized write behavior for this module.
#
# SLP1-S1 froze this module as read-only against its OWN separate
# database (WATERMARK_DB_FILENAME above). SLP1-C's own mandate
# explicitly names this file as "the point at which the previously
# read-only sleep_watermark.py stub may receive its first authorized
# successful-write behavior" -- and requires the watermark to live in
# the SAME anchored `anaxi_provenance.db` as the new sleep_cycles/
# sleep_derivations tables, because only one database can share ONE
# atomic SQLite transaction with them (ATOMIC FINAL COMMIT: derivations
# committed iff cycle completed iff watermark advanced -- no state
# where one commits without the others). Cross-database transactions
# cannot give that guarantee, so the watermark position now lives in
# `sleep_v1_watermark` inside anaxi_provenance.db (sleep_c_schema.py),
# not the separate file above. The old file-based
# SleepWatermarkPaths/WATERMARK_DB_FILENAME/read_watermark() are left
# completely untouched (dead, but harmless, and still exactly what
# SLP1-S1 tested) -- nothing calls them from here on.
#
# The two functions below take an ALREADY-OPEN connection and perform
# NO transaction management of their own -- exactly like
# sleep_lease.verify_lease_for_commit(). The caller (sleep_cycle.py)
# is responsible for wrapping both the lease-fencing check and this
# write inside its own single atomic commit transaction.
# ================================================================


def read_watermark_from_conn(conn):
    """Reads the current successful-Sleep watermark from the anchored
    provenance DB's own `sleep_v1_watermark` table. Returns
    INITIAL_WATERMARK_STATE (a fresh dict) if the table exists but has
    no row yet -- the table itself is created additively by
    sleep_c_schema.ensure_schema(), not by this function."""
    row = conn.execute(
        "SELECT last_processed_component_id, last_successful_cycle_id, "
        "last_successful_at, cycle_count FROM sleep_v1_watermark WHERE id = 1"
    ).fetchone()
    if row is None:
        return dict(INITIAL_WATERMARK_STATE)
    last_processed_component_id, last_successful_cycle_id, last_successful_at, cycle_count = row
    return {
        "last_processed_component_id": last_processed_component_id,
        "last_successful_cycle_id": last_successful_cycle_id,
        "last_successful_at": last_successful_at,
        "cycle_count": cycle_count,
    }


def advance_watermark_in_conn(conn, *, new_last_processed_component_id, cycle_id, now):
    """Writes the new successful-Sleep watermark position. Performs no
    transaction management and no validation of ordering (the caller,
    sleep_cycle.py, is responsible for calling this ONLY as the last
    step of its own already-lease-verified atomic commit transaction,
    with new_last_processed_component_id monotonically >= the value it
    just read). Never called anywhere else in this codebase, and never
    called merely by evidence construction or a model call succeeding
    -- only a successful, fully-committed cycle may reach this."""
    previous = read_watermark_from_conn(conn)
    conn.execute(
        "INSERT INTO sleep_v1_watermark (id, last_processed_component_id, last_successful_cycle_id, "
        "last_successful_at, cycle_count) VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_processed_component_id = excluded.last_processed_component_id, "
        "last_successful_cycle_id = excluded.last_successful_cycle_id, "
        "last_successful_at = excluded.last_successful_at, "
        "cycle_count = excluded.cycle_count",
        (new_last_processed_component_id, cycle_id, now, previous["cycle_count"] + 1),
    )
