"""SUBJECT-DIRECTED CONSOLIDATION V0: additive canonical schema.

Same convention as sleep_c_schema.py: `apply_additive_migration()` is
idempotent (CREATE TABLE IF NOT EXISTS), touches no existing table's
columns/constraints/triggers, and runs against the SAME anchored
`anaxi_provenance.db` every other canonical writer in this project uses.

WHAT THIS IS. A consolidation record is a SUBJECT-AUTHORED record ABOUT
canonical history. It never replaces, rewrites, or supersedes canonical
history, and it is never a claim that anything is objectively true. The
canonical episode remains exactly where it always was, in `events` /
`event_components`, and remains independently retrievable by its own
original language.

Three tables:

  consolidation_records -- one row per subject-directed consolidation
      ACT. `request` is the closed vocabulary the subject chose
      (select/interpret/revise/de_emphasize). `target_event_id` is the
      EXACT canonical event the act names -- a mechanical binding, never
      a prose approximation. `interpretation_text` is the subject's own
      words, stored verbatim, and is testimony, not fact.
      `prior_record_id` is the earlier record a revise/de_emphasize act
      appends to: both acts receive their own row and never overwrite the
      earlier record. `de_emphasized_at` is populated on the appended
      de-emphasis act -- it deletes nothing.
      There is deliberately NO importance, salience, emotional-weight,
      significance, or confidence column anywhere in this table: the
      host does not score the subject's choice.

  consolidation_cues -- one row per subject-authored retrieval cue.
      Cues AUGMENT retrieval; they never replace the episode's own
      canonical language, which stays an independent retrieval surface.

  consolidation_scope -- the scope a record carries. A record may
      inherit its target's scope or narrow it, never widen it; the
      narrowing is enforced by consolidation.py against the target's
      own canonical scope at write time, and this table records the
      result so a later reader can prove it.

No proposition/assertion machinery is reused: "Clark selected this
episode" and "Clark wrote this interpretation" are acts and testimony,
not assertions that a proposition is true -- the same distinction
sleep_c_schema.py's own docstring draws for derivations.
"""
import dataclasses
import os
import sqlite3


NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS consolidation_records (
    record_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    target_event_id TEXT NOT NULL,
    target_component_id INTEGER,
    interpretation_text TEXT,
    interpretation_sha256 TEXT,
    prior_record_id TEXT REFERENCES consolidation_records(record_id),
    author_actor_id TEXT NOT NULL,
    occasion TEXT NOT NULL,
    occasion_event_id TEXT,
    model_revision_id TEXT,
    created_at INTEGER NOT NULL,
    de_emphasized_at INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS consolidation_records_occasion
    ON consolidation_records(occasion_event_id)
    WHERE occasion_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS consolidation_cues (
    record_id TEXT NOT NULL REFERENCES consolidation_records(record_id),
    cue_index INTEGER NOT NULL,
    cue_text TEXT NOT NULL,
    cue_sha256 TEXT NOT NULL,
    PRIMARY KEY (record_id, cue_index)
);

CREATE TABLE IF NOT EXISTS consolidation_scope (
    record_id TEXT PRIMARY KEY REFERENCES consolidation_records(record_id),
    scope_kind TEXT NOT NULL,
    scope_ref TEXT,
    inherited_from_event_id TEXT NOT NULL,
    narrowed INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS consolidation_records_target
    ON consolidation_records(target_event_id);
CREATE INDEX IF NOT EXISTS consolidation_records_prior
    ON consolidation_records(prior_record_id);
"""


@dataclasses.dataclass(frozen=True)
class ConsolidationPaths:
    provenance_db_path: str

    @staticmethod
    def production_defaults() -> "ConsolidationPaths":
        base = os.path.dirname(os.path.abspath(__file__))
        return ConsolidationPaths(provenance_db_path=os.path.join(base, "anaxi_provenance.db"))


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
    """Same migration, applied against an already-open connection.
    Idempotent; safe to call every time."""
    conn.executescript(NEW_TABLES_DDL)
