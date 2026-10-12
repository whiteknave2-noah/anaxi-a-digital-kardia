"""CPI0: additive schema for CANONICAL PERSON IDENTITY & RELATIONAL PROVENANCE V0.

Same discipline as dc0_schema_migration.py: nothing existing is touched; three new append-only
tables.  Deliberately, NO table or column here records a relationship category, closeness,
trust, importance, affection, mood, personality or any other host model of a person -- only:

  canonical_persons                   WHO (identity), independent of authority
  canonical_person_identifier_events  which stable external identifier is owner-bound to which person
  canonical_person_statement_records  how one EXACT, already-canonical utterance is to be framed
                                      (self-report / standing request / third-party report /
                                      Clark reflection / correction), with revision history

Every table is append-only (UPDATE/DELETE abort).  Current state is always re-derived from the
ledgers.
"""
import sqlite3

NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS canonical_persons (
    person_id            TEXT PRIMARY KEY,
    entity_kind             TEXT NOT NULL CHECK (entity_kind IN ('human','ai_digital')),
    display_label              TEXT NOT NULL,
    linked_actor_id               TEXT UNIQUE REFERENCES actors(actor_id),
    requester_actor_id               TEXT NOT NULL,
    occurred_at                         INTEGER NOT NULL,
    created_at                             INTEGER NOT NULL,
    CHECK (linked_actor_id IS NULL OR entity_kind = 'human')
);
CREATE TRIGGER IF NOT EXISTS trg_canonical_persons_no_update
    BEFORE UPDATE ON canonical_persons BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_canonical_persons_no_delete
    BEFORE DELETE ON canonical_persons BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS canonical_person_identifier_events (
    binding_event_id     TEXT PRIMARY KEY,
    person_id               TEXT NOT NULL REFERENCES canonical_persons(person_id),
    identifier_kind            TEXT NOT NULL CHECK (identifier_kind IN ('discord_author_id')),
    identifier_value              TEXT NOT NULL,
    action                           TEXT NOT NULL CHECK (action IN ('bind','revoke')),
    username_snapshot                   TEXT,
    requester_actor_id                     TEXT NOT NULL,
    occurred_at                               INTEGER NOT NULL,
    created_at                                   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cpi_identifier_events_value
    ON canonical_person_identifier_events(identifier_kind, identifier_value);
CREATE TRIGGER IF NOT EXISTS trg_cpi_identifier_events_no_update
    BEFORE UPDATE ON canonical_person_identifier_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_cpi_identifier_events_no_delete
    BEFORE DELETE ON canonical_person_identifier_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS canonical_person_statement_records (
    record_id               TEXT PRIMARY KEY,
    action                     TEXT NOT NULL CHECK (action IN ('record','revise','withdraw')),
    statement_class               TEXT NOT NULL CHECK (statement_class IN
        ('self_report','standing_request','third_party_report','clark_reflection','correction')),
    speaker_person_id                TEXT REFERENCES canonical_persons(person_id),
    speaker_basis                       TEXT NOT NULL CHECK (speaker_basis IN
        ('actor_link','discord_author_binding','clark_actor')),
    speaker_identifier_value               TEXT,
    referent_person_id                        TEXT REFERENCES canonical_persons(person_id),
    source_event_id                              TEXT NOT NULL REFERENCES events(event_id),
    source_component_id                             INTEGER NOT NULL,
    quoted_text                                        TEXT NOT NULL,
    scope_text                                            TEXT,
    prompted_by_event_id                                     TEXT REFERENCES events(event_id),
    references_record_id                                        TEXT REFERENCES canonical_person_statement_records(record_id),
    effective_at                                                   INTEGER NOT NULL,
    recorded_by_actor_id                                              TEXT NOT NULL,
    recorded_at                                                          INTEGER NOT NULL,
    CHECK ((statement_class = 'clark_reflection') = (speaker_person_id IS NULL)),
    CHECK ((speaker_basis = 'clark_actor') = (statement_class = 'clark_reflection')),
    CHECK (statement_class != 'clark_reflection' OR referent_person_id IS NOT NULL),
    CHECK (statement_class != 'third_party_report' OR referent_person_id IS NOT NULL),
    CHECK (statement_class != 'correction' OR prompted_by_event_id IS NOT NULL),
    CHECK (statement_class != 'standing_request' OR action = 'withdraw' OR (scope_text IS NOT NULL AND scope_text != '')),
    CHECK ((action = 'record') = (references_record_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cpi_statement_first_record
    ON canonical_person_statement_records(source_component_id, statement_class, quoted_text)
    WHERE action = 'record';
CREATE INDEX IF NOT EXISTS idx_cpi_statement_source
    ON canonical_person_statement_records(source_event_id, source_component_id);
CREATE INDEX IF NOT EXISTS idx_cpi_statement_speaker
    ON canonical_person_statement_records(speaker_person_id);
CREATE TRIGGER IF NOT EXISTS trg_cpi_statement_thread_integrity
    BEFORE INSERT ON canonical_person_statement_records
    WHEN NEW.action != 'record'
BEGIN
    SELECT RAISE(ABORT, 'revision must reference a record of the same class by the same speaker')
    WHERE NOT EXISTS (
        SELECT 1 FROM canonical_person_statement_records prior
        WHERE prior.record_id = NEW.references_record_id
          AND prior.statement_class = NEW.statement_class
          AND prior.speaker_person_id IS NEW.speaker_person_id
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_cpi_statement_no_update
    BEFORE UPDATE ON canonical_person_statement_records BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_cpi_statement_no_delete
    BEFORE DELETE ON canonical_person_statement_records BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def apply_migration_on_connection(conn: sqlite3.Connection) -> None:
    """Idempotent; ensures the additive tables on an already-open connection."""
    conn.executescript(NEW_TABLES_DDL)


def tables_present(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('canonical_persons','canonical_person_identifier_events','canonical_person_statement_records')"
    ).fetchone()[0] == 3
