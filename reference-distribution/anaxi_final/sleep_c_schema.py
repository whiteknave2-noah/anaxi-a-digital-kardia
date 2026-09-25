"""SLP1-C: additive canonical schema for dormant Sleep cycle completion.

Same convention as hir1_schema_migration.py: `apply_additive_migration()`
is idempotent (CREATE TABLE IF NOT EXISTS), touches no existing table's
columns/constraints/triggers, and is meant to run against the SAME
anchored `anaxi_provenance.db` every other canonical writer in this
project uses -- never a separate "memory DB," never a CWD-relative path
(see SleepCPaths.production_defaults() below, same convention as
sleep_evidence.SleepEvidencePaths).

Five new tables, all living in this ONE database specifically so a
single SQLite transaction can cover cycle completion + derivations +
watermark advancement + lease release atomically (SLP1-C's own "ATOMIC
FINAL COMMIT" requirement) -- a cross-database transaction could not
give that guarantee, which is why sleep_v1_watermark (previously its
own separate database, per SLP1-S1's now-superseded design) and the new
sleep_v1_lease both move into this same file:

  sleep_v1_lease         -- the ONE fenced single-flight lease row.
  sleep_v1_watermark     -- the ONE successful-Sleep-frontier row
                             (same shape SLP1-S1 defined, now living
                             here instead of a separate database).
  sleep_cycles           -- one row per SUCCESSFULLY COMPLETED cycle.
                             There is no "failed cycle" row anywhere --
                             a failed cycle leaves no trace by design
                             (see the mandate's CRASH MODEL section).
  sleep_derivations      -- one row per validated derivation OCCURRENCE
                             ("Clark derived this text, citing these
                             WMUs, during this cycle" -- never "this
                             text is true"). No belief/truth/confidence/
                             importance column exists anywhere in this
                             table by design.
  sleep_derivation_sources -- one row per (derivation, cited WMU,
                             canonical selection-bearing component,
                             segment) the model actually saw for that
                             citation -- the host's mechanical expansion
                             of a cited wmu_id into the exact canonical
                             provenance behind it, never an independent
                             semantic judgment.

No proposition/assertion machinery is reused here: SLP1-C's own mandate
explicitly forbids force-fitting "Clark derived P" into a schema built
to represent "P is asserted true" (see provenance_schema.py's
`propositions`/`assertions` tables, which encode exactly that stronger
claim and are deliberately left untouched by this migration).
"""
import dataclasses
import os
import sqlite3


NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS sleep_v1_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_v1_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_component_id INTEGER NOT NULL,
    last_successful_cycle_id TEXT,
    last_successful_at INTEGER,
    cycle_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_cycles (
    cycle_id TEXT PRIMARY KEY,
    window_start_component_id INTEGER NOT NULL,
    window_end_component_id INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    derivation_count INTEGER NOT NULL,
    model_tag TEXT,
    started_at INTEGER NOT NULL,
    completed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_derivations (
    derivation_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES sleep_cycles(cycle_id),
    batch_index INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    derived_text TEXT NOT NULL,
    derived_text_sha256 TEXT NOT NULL,
    model_tag TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_derivation_sources (
    derivation_id TEXT NOT NULL REFERENCES sleep_derivations(derivation_id),
    wmu_id TEXT NOT NULL,
    primary_event_id TEXT NOT NULL,
    cite_order INTEGER NOT NULL,
    source_event_id TEXT NOT NULL,
    source_component_id INTEGER NOT NULL,
    segment_id TEXT,
    segment_index INTEGER,
    PRIMARY KEY (derivation_id, wmu_id, source_component_id, segment_id)
);
"""


@dataclasses.dataclass(frozen=True)
class SleepCPaths:
    provenance_db_path: str

    @staticmethod
    def production_defaults() -> "SleepCPaths":
        base = os.path.dirname(os.path.abspath(__file__))
        return SleepCPaths(provenance_db_path=os.path.join(base, "anaxi_provenance.db"))


def apply_additive_migration(db_path):
    """Idempotent additive migration against the anchored provenance
    DB. No existing table is touched."""
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


def ensure_schema(conn):
    """Same migration, applied against an already-open connection --
    used by sleep_cycle.py at the start of every cycle attempt so a
    fresh anaxi_provenance.db copy (as every test uses) never needs a
    separate migration step first. Idempotent; safe to call every time."""
    conn.executescript(NEW_TABLES_DDL)
