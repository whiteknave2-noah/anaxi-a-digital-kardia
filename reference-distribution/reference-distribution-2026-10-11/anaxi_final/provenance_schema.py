"""
Anaxi -- Identity/relational-provenance schema DDL, transcribed
VERBATIM from the frozen canonical specification
(DESIGN_NOTE_identity_provenance_schema.md, SHA-256
7761d132aded85f18ca519aac6b189d541d22eee336755f8b8b3e02c885751c2),
sections 2.0 through 2.11. Twelve SQL blocks, in the same order the
spec presents them.

SLP2-CORRECTION-2 adds independently named identity guards below the
historical DDL; the original trigger definitions remain unchanged.

This module only creates schema. It never touches any live/production
file -- create_provenance_db() takes an explicit db_path and is meant
to be pointed at a fresh file in an isolated rehearsal directory, per
the frozen spec's §9 "copy-only migration rehearsal" step, until a
live cutover is separately authorized.

Run (creates a fresh anaxi_provenance.db at the given path):
    python provenance_schema.py <path>
"""

import hashlib
import sqlite3
import sys


def derive_historical_event_id(source_id: str, source_line_bytes: bytes, line_index: int) -> str:
    """The one canonical deterministic event_id derivation for
    migration-sourced events (frozen spec §7 rule 9), transcribed
    verbatim. NOT used for native (post-cutover) events, which get a
    real random ULID -- only migrated rows need to be re-derivable
    identically on every re-run, so a partial migration can resume
    safely without duplicate-detection guesswork. The hist- prefix
    makes migrated vs. native origin visible from the ID alone.

    Domain-separated, length-prefixed encoding: each field is prefixed
    with its own fixed-width (8-byte big-endian) length before being
    concatenated, and a fixed domain tag is included, so no two
    distinct (source_id, source_line_bytes, line_index) triples can
    ever share a preimage -- e.g. (b"abc1", 2) and (b"abc", 12), which
    naive concatenation would collide on ("abc" + "12" == "abc1" + "2"),
    now hash to different values."""
    domain = b"anaxi-historical-event-id-v1"
    source_id_bytes = source_id.encode("utf-8")
    preimage = (
        domain + b"|"
        + len(source_id_bytes).to_bytes(8, "big") + source_id_bytes + b"|"
        + len(source_line_bytes).to_bytes(8, "big") + source_line_bytes + b"|"
        + line_index.to_bytes(8, "big")
    )
    digest = hashlib.sha256(preimage).hexdigest()
    return f"hist-{digest[:26]}"


def derive_stable_id(prefix: str, *parts: str) -> str:
    """Opaque, collision-safe, deterministic ID for permanent
    reference/model rows (pipelines, actors, model_revisions) --
    replaces earlier hand-picked human-readable placeholder strings
    like 'pipe_a'/'actor_clark', which were neither opaque nor
    collision-resistant. Same domain-separated, length-prefixed
    encoding as derive_historical_event_id(), so two different natural
    keys can never collide regardless of what characters they contain,
    and re-deriving from the same natural key (pipeline_key, stable_key,
    or a model's identity fields) always reproduces the same ID --
    which is what makes reference-data seeding idempotent without
    needing a separate lookup-by-natural-key round trip first."""
    domain = f"anaxi-stable-id-v1:{prefix}".encode("utf-8")
    preimage = domain
    for part in parts:
        part_bytes = part.encode("utf-8")
        preimage += b"|" + len(part_bytes).to_bytes(8, "big") + part_bytes
    digest = hashlib.sha256(preimage).hexdigest()
    return f"{prefix}-{digest[:26]}"

# =============================================================================
# 2.1 Actors, persons, subtypes
# =============================================================================
DDL_2_1_ACTORS = """
CREATE TABLE persons (
    person_id        TEXT PRIMARY KEY,
    created_at         INTEGER NOT NULL,
    notes                TEXT
);
CREATE TRIGGER trg_persons_no_update BEFORE UPDATE ON persons
    BEGIN SELECT RAISE(ABORT, 'persons rows are immutable once established'); END;
CREATE TRIGGER trg_persons_no_delete BEFORE DELETE ON persons
    BEGIN SELECT RAISE(ABORT, 'persons rows are never deleted'); END;

CREATE TABLE actors (
    actor_id           TEXT PRIMARY KEY,
    actor_type           TEXT NOT NULL CHECK (actor_type IN ('human_person','clark_agent','host_system')),
    stable_key             TEXT NOT NULL UNIQUE,
    display_label            TEXT,
    created_at                 INTEGER NOT NULL,
    retired_at                   INTEGER,
    CHECK (retired_at IS NULL OR retired_at >= created_at)
);
CREATE TRIGGER trg_actors_immutable_identity
    BEFORE UPDATE OF actor_type, stable_key, display_label, created_at ON actors
    BEGIN SELECT RAISE(ABORT, 'only retired_at may be updated on actors'); END;
CREATE TRIGGER trg_actors_retired_at_one_way BEFORE UPDATE OF retired_at ON actors
BEGIN
    SELECT RAISE(ABORT, 'retired_at may only transition NULL to a real timestamp >= created_at, exactly once')
    WHERE OLD.retired_at IS NOT NULL
       OR NEW.retired_at IS NULL
       OR NEW.retired_at < NEW.created_at;
END;
CREATE TRIGGER trg_actors_no_delete
    BEFORE DELETE ON actors
    BEGIN SELECT RAISE(ABORT, 'actors rows are never deleted; use retired_at'); END;

CREATE TABLE actor_human_person (
    actor_id                     TEXT PRIMARY KEY REFERENCES actors(actor_id),
    person_id                      TEXT NOT NULL UNIQUE REFERENCES persons(person_id),
    canonical_name                   TEXT,
    relationship_established_at        INTEGER NOT NULL
);
CREATE TRIGGER trg_actor_human_person_type_check BEFORE INSERT ON actor_human_person BEGIN
    SELECT RAISE(ABORT, 'actor_type mismatch: actor is not human_person')
    WHERE (SELECT actor_type FROM actors WHERE actor_id = NEW.actor_id) IS NOT 'human_person';
END;
CREATE TRIGGER trg_actor_human_person_exclusive BEFORE INSERT ON actor_human_person BEGIN
    SELECT RAISE(ABORT, 'actor already has a conflicting subtype row')
    WHERE EXISTS (SELECT 1 FROM actor_clark_agent WHERE actor_id = NEW.actor_id)
       OR EXISTS (SELECT 1 FROM actor_host_system WHERE actor_id = NEW.actor_id);
END;
CREATE TRIGGER trg_actor_human_person_no_update BEFORE UPDATE ON actor_human_person
    BEGIN SELECT RAISE(ABORT, 'person-actor linkage is immutable once established'); END;
CREATE TRIGGER trg_actor_human_person_no_delete BEFORE DELETE ON actor_human_person
    BEGIN SELECT RAISE(ABORT, 'person-actor linkage cannot be deleted'); END;

CREATE TABLE actor_clark_agent (
    actor_id           TEXT PRIMARY KEY REFERENCES actors(actor_id),
    canonical_key         TEXT NOT NULL UNIQUE DEFAULT 'clark'
);
CREATE TRIGGER trg_actor_clark_agent_type_check BEFORE INSERT ON actor_clark_agent BEGIN
    SELECT RAISE(ABORT, 'actor_type mismatch: actor is not clark_agent')
    WHERE (SELECT actor_type FROM actors WHERE actor_id = NEW.actor_id) IS NOT 'clark_agent';
END;
CREATE TRIGGER trg_actor_clark_agent_exclusive BEFORE INSERT ON actor_clark_agent BEGIN
    SELECT RAISE(ABORT, 'actor already has a conflicting subtype row')
    WHERE EXISTS (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.actor_id)
       OR EXISTS (SELECT 1 FROM actor_host_system WHERE actor_id = NEW.actor_id);
END;
CREATE TRIGGER trg_actor_clark_agent_no_update BEFORE UPDATE ON actor_clark_agent
    BEGIN SELECT RAISE(ABORT, 'linkage is immutable once established'); END;
CREATE TRIGGER trg_actor_clark_agent_no_delete BEFORE DELETE ON actor_clark_agent
    BEGIN SELECT RAISE(ABORT, 'linkage cannot be deleted'); END;

CREATE TABLE actor_host_system (
    actor_id           TEXT PRIMARY KEY REFERENCES actors(actor_id),
    subsystem_key         TEXT NOT NULL UNIQUE
);
CREATE TRIGGER trg_actor_host_system_type_check BEFORE INSERT ON actor_host_system BEGIN
    SELECT RAISE(ABORT, 'actor_type mismatch: actor is not host_system')
    WHERE (SELECT actor_type FROM actors WHERE actor_id = NEW.actor_id) IS NOT 'host_system';
END;
CREATE TRIGGER trg_actor_host_system_exclusive BEFORE INSERT ON actor_host_system BEGIN
    SELECT RAISE(ABORT, 'actor already has a conflicting subtype row')
    WHERE EXISTS (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.actor_id)
       OR EXISTS (SELECT 1 FROM actor_clark_agent WHERE actor_id = NEW.actor_id);
END;
CREATE TRIGGER trg_actor_host_system_no_update BEFORE UPDATE ON actor_host_system
    BEGIN SELECT RAISE(ABORT, 'linkage is immutable once established'); END;
CREATE TRIGGER trg_actor_host_system_no_delete BEFORE DELETE ON actor_host_system
    BEGIN SELECT RAISE(ABORT, 'linkage cannot be deleted'); END;
"""

# =============================================================================
# 2.2 Sessions, authentication context
# (depends on pipelines -- created before 2.3 here only for FK ordering;
#  pipelines itself has no FK dependency on sessions/auth_contexts)
# =============================================================================
DDL_2_3_PIPELINES_FIRST = """
CREATE TABLE pipelines (
    pipeline_id              TEXT PRIMARY KEY,
    pipeline_key                TEXT NOT NULL UNIQUE,
    legacy_substrate_label         TEXT,
    description                      TEXT
);
CREATE TRIGGER trg_pipelines_immutable
    BEFORE UPDATE ON pipelines
    BEGIN SELECT RAISE(ABORT, 'pipelines rows are fully immutable once established'); END;
CREATE TRIGGER trg_pipelines_no_delete BEFORE DELETE ON pipelines
    BEGIN SELECT RAISE(ABORT, 'pipelines are never deleted'); END;
"""

DDL_2_2_SESSIONS_AUTH = """
CREATE TABLE sessions (
    session_id       TEXT PRIMARY KEY,
    pipeline_id         TEXT REFERENCES pipelines(pipeline_id),
    started_at             INTEGER NOT NULL,
    ended_at                  INTEGER,
    device_ref                  TEXT,
    notes                         TEXT
);
CREATE TRIGGER trg_sessions_no_delete BEFORE DELETE ON sessions
    BEGIN SELECT RAISE(ABORT, 'sessions are never deleted'); END;
CREATE TRIGGER trg_sessions_immutable_except_end
    BEFORE UPDATE OF session_id, pipeline_id, started_at, device_ref, notes ON sessions
    BEGIN SELECT RAISE(ABORT, 'only ended_at may be updated on sessions'); END;
CREATE TRIGGER trg_sessions_ended_at_one_way BEFORE UPDATE OF ended_at ON sessions
BEGIN
    SELECT RAISE(ABORT, 'ended_at may only transition NULL to a real timestamp >= started_at, exactly once')
    WHERE OLD.ended_at IS NOT NULL
       OR NEW.ended_at IS NULL
       OR NEW.ended_at < NEW.started_at;
END;

CREATE TABLE auth_contexts (
    auth_context_id           TEXT PRIMARY KEY,
    session_id                   TEXT NOT NULL REFERENCES sessions(session_id),
    claimed_actor_id               TEXT REFERENCES actors(actor_id),
    authenticated_actor_id           TEXT REFERENCES actors(actor_id),
    auth_state                         TEXT NOT NULL CHECK (auth_state IN ('unauthenticated','claimed_only','authenticated','unknown')),
    auth_method                          TEXT,
    assurance_level                        TEXT CHECK (assurance_level IN ('none','low','medium','high') OR assurance_level IS NULL),
    device_ref                               TEXT,
    established_at                             INTEGER NOT NULL,
    supersedes_auth_context_id                   TEXT REFERENCES auth_contexts(auth_context_id),
    CHECK (
        (auth_state = 'unknown'
            AND claimed_actor_id IS NULL AND authenticated_actor_id IS NULL
            AND auth_method IS NULL AND assurance_level IS NULL)
        OR (auth_state = 'claimed_only'
            AND claimed_actor_id IS NOT NULL AND authenticated_actor_id IS NULL
            AND auth_method IS NULL
            AND (assurance_level IS NULL OR assurance_level = 'none'))
        OR (auth_state = 'unauthenticated'
            AND authenticated_actor_id IS NULL
            AND auth_method IS NOT NULL
            AND (assurance_level IS NULL OR assurance_level = 'none'))
        OR (auth_state = 'authenticated'
            AND authenticated_actor_id IS NOT NULL
            AND auth_method IS NOT NULL
            AND assurance_level IN ('low','medium','high')
            AND (claimed_actor_id IS NULL OR claimed_actor_id = authenticated_actor_id))
    )
);
CREATE UNIQUE INDEX idx_auth_contexts_supersedes_unique
    ON auth_contexts(supersedes_auth_context_id) WHERE supersedes_auth_context_id IS NOT NULL;
CREATE TRIGGER trg_auth_context_human_only BEFORE INSERT ON auth_contexts BEGIN
    SELECT RAISE(ABORT, 'claimed_actor_id must be human') WHERE NEW.claimed_actor_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.claimed_actor_id);
    SELECT RAISE(ABORT, 'authenticated_actor_id must be human') WHERE NEW.authenticated_actor_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.authenticated_actor_id);
END;
CREATE TRIGGER trg_auth_context_supersedes_same_session BEFORE INSERT ON auth_contexts
    WHEN NEW.supersedes_auth_context_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supersedes_auth_context_id must reference a context in the same session')
    WHERE NOT EXISTS (
        SELECT 1 FROM auth_contexts prior
        WHERE prior.auth_context_id = NEW.supersedes_auth_context_id
          AND prior.session_id = NEW.session_id
    );
END;
CREATE TRIGGER trg_auth_contexts_no_update BEFORE UPDATE ON auth_contexts
    BEGIN SELECT RAISE(ABORT, 'auth_contexts is append-only'); END;
CREATE TRIGGER trg_auth_contexts_no_delete BEFORE DELETE ON auth_contexts
    BEGIN SELECT RAISE(ABORT, 'auth_contexts is append-only'); END;
"""

# =============================================================================
# 2.3 Pipelines, architecture epochs (pipelines itself created above,
# ordered before sessions/auth_contexts for FK dependency reasons)
# =============================================================================
DDL_2_3_EPOCHS = """
CREATE TABLE architecture_epochs (
    epoch_id                 TEXT PRIMARY KEY,
    pipeline_id                 TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    epoch_key                     TEXT NOT NULL UNIQUE,
    epoch_kind                      TEXT NOT NULL CHECK (epoch_kind IN ('prospective','retrospective')),
    epoch_status                      TEXT CHECK (epoch_status IN ('transitional','stable') OR epoch_status IS NULL),
    effective_begin_at                  INTEGER,
    record_created_at                     INTEGER NOT NULL,
    boundary_evidence_type                  TEXT CHECK (boundary_evidence_type IN ('git_commit','retrospective_inference','none')),
    boundary_evidence                         TEXT,
    migration_basis                             TEXT,
    predecessor_epoch_id                          TEXT REFERENCES architecture_epochs(epoch_id),
    CHECK (
        (epoch_kind = 'prospective'
            AND epoch_status IS NOT NULL
            AND effective_begin_at IS NOT NULL
            AND boundary_evidence_type IS NOT NULL AND boundary_evidence_type != 'none'
            AND boundary_evidence IS NOT NULL)
        OR
        (epoch_kind = 'retrospective'
            AND epoch_status IS NULL
            AND migration_basis IS NOT NULL)
    )
);
CREATE TRIGGER trg_epoch_predecessor_same_pipeline BEFORE INSERT ON architecture_epochs
    WHEN NEW.predecessor_epoch_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'predecessor_epoch_id must be in the same pipeline')
    WHERE NOT EXISTS (
        SELECT 1 FROM architecture_epochs p
        WHERE p.epoch_id = NEW.predecessor_epoch_id AND p.pipeline_id = NEW.pipeline_id
    );
END;
CREATE TRIGGER trg_epochs_no_update BEFORE UPDATE ON architecture_epochs
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_epochs_no_delete BEFORE DELETE ON architecture_epochs
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.4 Model revisions
# =============================================================================
DDL_2_4_MODEL_REVISIONS = """
CREATE TABLE model_revisions (
    model_revision_id         TEXT PRIMARY KEY,
    tag                          TEXT NOT NULL,
    ollama_model_digest            TEXT,
    modelfile_sha256                 TEXT,
    provider_artifact_digest           TEXT,
    provider_version_ref                 TEXT,
    identity_confidence                    TEXT NOT NULL CHECK (identity_confidence IN (
                                                'artifact_digest_verified',
                                                'provider_version_verified',
                                                'tag_only_degraded'
                                            )),
    first_observed_at                        INTEGER NOT NULL,
    notes                                      TEXT,
    digest_uniqueness_key                     TEXT GENERATED ALWAYS AS (
                                                    tag || ':' || COALESCE(ollama_model_digest, provider_artifact_digest, provider_version_ref, '__NO_DIGEST__')
                                                ) STORED,
    UNIQUE (digest_uniqueness_key),
    CHECK (
        (identity_confidence = 'artifact_digest_verified' AND (ollama_model_digest IS NOT NULL OR provider_artifact_digest IS NOT NULL))
        OR (identity_confidence = 'provider_version_verified' AND provider_version_ref IS NOT NULL)
        OR identity_confidence = 'tag_only_degraded'
    )
);
CREATE TRIGGER trg_model_revisions_no_update BEFORE UPDATE ON model_revisions
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_model_revisions_no_delete BEFORE DELETE ON model_revisions
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.5 Events, migration status, model participation, components
# =============================================================================
DDL_2_5_EVENTS = """
CREATE TABLE events (
    event_id                  TEXT PRIMARY KEY,
    event_type                  TEXT NOT NULL,
    pipeline_id                    TEXT REFERENCES pipelines(pipeline_id),
    pipeline_provenance_status       TEXT NOT NULL CHECK (pipeline_provenance_status IN ('known','unknown')),
    epoch_id                           TEXT REFERENCES architecture_epochs(epoch_id),
    auth_context_id                       TEXT REFERENCES auth_contexts(auth_context_id),
    input_source_ref                        TEXT,
    occurred_at                             INTEGER NOT NULL,
    record_created_at                         INTEGER NOT NULL,
    CHECK (
        (pipeline_provenance_status = 'known' AND pipeline_id IS NOT NULL)
        OR (pipeline_provenance_status = 'unknown' AND pipeline_id IS NULL)
    )
);
CREATE TRIGGER trg_events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_migration_status (
    event_id           TEXT PRIMARY KEY REFERENCES events(event_id),
    migration_status       TEXT NOT NULL CHECK (migration_status IN ('pre_authentication_layer','backfilled_partial')),
    migrated_at               INTEGER NOT NULL,
    migration_basis             TEXT NOT NULL
);
CREATE TRIGGER trg_event_migration_status_no_update BEFORE UPDATE ON event_migration_status
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_migration_status_no_delete BEFORE DELETE ON event_migration_status
    BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_model_participation (
    event_model_participation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                          TEXT NOT NULL REFERENCES events(event_id),
    model_revision_id                    TEXT NOT NULL REFERENCES model_revisions(model_revision_id),
    participation_note                     TEXT
);
CREATE TRIGGER trg_event_model_participation_no_update BEFORE UPDATE ON event_model_participation
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_model_participation_no_delete BEFORE DELETE ON event_model_participation
    BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_components (
    component_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT NOT NULL REFERENCES events(event_id),
    sequence                   INTEGER NOT NULL,
    creator_actor_id              TEXT REFERENCES actors(actor_id),
    component_kind                  TEXT NOT NULL,
    authorship_resolution              TEXT NOT NULL DEFAULT 'resolved'
                                          CHECK (authorship_resolution IN ('resolved','unresolved_mixed_historical')),
    component_text                       TEXT NOT NULL,
    content_sha256                         TEXT NOT NULL,
    span_start                               INTEGER,
    span_end                                   INTEGER,
    model_revision_id                          TEXT REFERENCES model_revisions(model_revision_id),
    CHECK (span_start IS NULL OR span_end IS NULL OR span_end >= span_start),
    CHECK (
        (authorship_resolution = 'resolved' AND creator_actor_id IS NOT NULL)
        OR
        (authorship_resolution = 'unresolved_mixed_historical' AND creator_actor_id IS NULL AND model_revision_id IS NULL)
    )
);
CREATE UNIQUE INDEX idx_event_components_seq ON event_components(event_id, sequence);
CREATE TRIGGER trg_event_components_no_update BEFORE UPDATE ON event_components
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_components_no_delete BEFORE DELETE ON event_components
    BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.6 Normalized per-event provenance relations
# =============================================================================
DDL_2_6_EVENT_RELATIONS = """
CREATE TABLE event_subjects (
    event_subject_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT NOT NULL REFERENCES events(event_id),
    subject_actor_id           TEXT REFERENCES actors(actor_id),
    subject_description           TEXT,
    role                             TEXT,
    CHECK (subject_actor_id IS NOT NULL OR subject_description IS NOT NULL)
);
CREATE TRIGGER trg_event_subjects_no_update BEFORE UPDATE ON event_subjects BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_subjects_no_delete BEFORE DELETE ON event_subjects BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_requesters (
    event_requester_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                 TEXT NOT NULL REFERENCES events(event_id),
    requester_actor_id           TEXT REFERENCES actors(actor_id),
    requester_description           TEXT,
    CHECK (requester_actor_id IS NOT NULL OR requester_description IS NOT NULL)
);
CREATE TRIGGER trg_event_requesters_no_update BEFORE UPDATE ON event_requesters BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_requesters_no_delete BEFORE DELETE ON event_requesters BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_relied_upon_consent (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL REFERENCES events(event_id),
    consent_event_id     TEXT NOT NULL REFERENCES consent_events(consent_event_id)
);
CREATE TRIGGER trg_event_relied_consent_no_update BEFORE UPDATE ON event_relied_upon_consent BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_relied_consent_no_delete BEFORE DELETE ON event_relied_upon_consent BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_relied_upon_guardian_authorization (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                    TEXT NOT NULL REFERENCES events(event_id),
    guardian_auth_event_id         TEXT NOT NULL REFERENCES guardian_authorization_events(guardian_auth_event_id)
);
CREATE TRIGGER trg_event_relied_guardian_no_update BEFORE UPDATE ON event_relied_upon_guardian_authorization BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_relied_guardian_no_delete BEFORE DELETE ON event_relied_upon_guardian_authorization BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_relied_upon_persistence_authorization (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                      TEXT NOT NULL REFERENCES events(event_id),
    persistence_auth_event_id        TEXT NOT NULL REFERENCES persistence_authorization_events(persistence_auth_event_id)
);
CREATE TRIGGER trg_event_relied_persistence_no_update BEFORE UPDATE ON event_relied_upon_persistence_authorization BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_relied_persistence_no_delete BEFORE DELETE ON event_relied_upon_persistence_authorization BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE event_relationship_context (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                    TEXT NOT NULL REFERENCES events(event_id),
    relationship_record_id         TEXT NOT NULL REFERENCES relationship_records(relationship_record_id)
);
CREATE TRIGGER trg_event_relationship_ctx_no_update BEFORE UPDATE ON event_relationship_context BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_event_relationship_ctx_no_delete BEFORE DELETE ON event_relationship_context BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.7 Propositions, assertions, evidence, typed relations
# =============================================================================
DDL_2_7_PROPOSITIONS = """
CREATE TABLE propositions (
    proposition_id       TEXT PRIMARY KEY,
    content                  TEXT NOT NULL,
    created_at                  INTEGER NOT NULL,
    pipeline_id                    TEXT NOT NULL REFERENCES pipelines(pipeline_id)
);
CREATE TRIGGER trg_propositions_no_update BEFORE UPDATE ON propositions BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_propositions_no_delete BEFORE DELETE ON propositions BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE proposition_subjects (
    proposition_subject_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    proposition_id               TEXT NOT NULL REFERENCES propositions(proposition_id),
    subject_actor_id                TEXT REFERENCES actors(actor_id),
    subject_description                TEXT,
    subject_role                         TEXT,
    CHECK (subject_actor_id IS NOT NULL OR subject_description IS NOT NULL)
);
CREATE TRIGGER trg_proposition_subjects_no_update BEFORE UPDATE ON proposition_subjects BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_proposition_subjects_no_delete BEFORE DELETE ON proposition_subjects BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE assertions (
    assertion_id         TEXT PRIMARY KEY,
    proposition_id           TEXT NOT NULL REFERENCES propositions(proposition_id),
    asserting_actor_id           TEXT REFERENCES actors(actor_id),
    asserting_event_id             TEXT REFERENCES events(event_id),
    asserting_component_id           INTEGER REFERENCES event_components(component_id),
    asserted_at                        INTEGER NOT NULL,
    pipeline_id                          TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    epoch_id                               TEXT REFERENCES architecture_epochs(epoch_id)
);
CREATE TRIGGER trg_assertions_no_update BEFORE UPDATE ON assertions BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_assertions_no_delete BEFORE DELETE ON assertions BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE assertion_evidence_sources (
    evidence_source_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    assertion_id                TEXT NOT NULL REFERENCES assertions(assertion_id),
    evidence_mode                  TEXT NOT NULL CHECK (evidence_mode IN (
                                        'direct_statement','relayed_statement','retrieved_historical_record','inference'
                                    )),
    source_actor_id                   TEXT REFERENCES actors(actor_id),
    source_event_id                     TEXT REFERENCES events(event_id),
    source_component_id                   INTEGER REFERENCES event_components(component_id),
    source_artifact_ref                     TEXT,
    recorded_at                               INTEGER NOT NULL,
    notes                                       TEXT,
    CHECK (source_actor_id IS NOT NULL OR source_event_id IS NOT NULL
           OR source_component_id IS NOT NULL OR source_artifact_ref IS NOT NULL)
);
CREATE TRIGGER trg_evidence_sources_no_update BEFORE UPDATE ON assertion_evidence_sources BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_evidence_sources_no_delete BEFORE DELETE ON assertion_evidence_sources BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE assertion_relations (
    assertion_relation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    from_assertion_id            TEXT NOT NULL REFERENCES assertions(assertion_id),
    to_assertion_id                 TEXT NOT NULL REFERENCES assertions(assertion_id),
    relation_type                     TEXT NOT NULL CHECK (relation_type IN ('supports','contradicts','corrects','retracts','supersedes')),
    recorded_at                          INTEGER NOT NULL,
    basis                                  TEXT,
    CHECK (from_assertion_id != to_assertion_id)
);
CREATE TRIGGER trg_assertion_relations_no_update BEFORE UPDATE ON assertion_relations BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_assertion_relations_no_delete BEFORE DELETE ON assertion_relations BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.8 Authorization scopes; consent, guardian, persistence authorization
# streams (two blocks: scopes, then the three streams)
# =============================================================================
DDL_2_8_SCOPES = """
CREATE TABLE authorization_scopes (
    scope_id       TEXT PRIMARY KEY,
    scope_key         TEXT NOT NULL UNIQUE,
    description          TEXT
);
CREATE TRIGGER trg_authz_scopes_immutable
    BEFORE UPDATE ON authorization_scopes
    BEGIN SELECT RAISE(ABORT, 'authorization_scopes rows are fully immutable once established'); END;
CREATE TRIGGER trg_authz_scopes_no_delete BEFORE DELETE ON authorization_scopes
    BEGIN SELECT RAISE(ABORT, 'authorization_scopes are never deleted'); END;
"""

DDL_2_8_AUTHORIZATION_STREAMS = """
CREATE TABLE consent_events (
    consent_event_id          TEXT PRIMARY KEY,
    lineage_id                   TEXT NOT NULL,
    scope_id                       TEXT NOT NULL REFERENCES authorization_scopes(scope_id),
    policy_version                    TEXT NOT NULL,
    detail                               TEXT,
    consenting_actor_id                     TEXT NOT NULL REFERENCES actors(actor_id),
    event_kind                                TEXT NOT NULL CHECK (event_kind IN ('grant','revocation','expiration','review')),
    prior_event_id                              TEXT REFERENCES consent_events(consent_event_id),
    effective_at                                  INTEGER NOT NULL,
    recorded_at                                     INTEGER NOT NULL,
    basis                                             TEXT,
    source_event_id                                     TEXT REFERENCES events(event_id),
    CHECK (
        (event_kind = 'grant' AND prior_event_id IS NULL AND lineage_id = consent_event_id)
        OR (event_kind != 'grant' AND prior_event_id IS NOT NULL AND lineage_id != consent_event_id)
    )
);
CREATE INDEX idx_consent_events_lineage ON consent_events(lineage_id, effective_at);
CREATE UNIQUE INDEX idx_consent_events_prior_unique
    ON consent_events(prior_event_id) WHERE prior_event_id IS NOT NULL;
CREATE TRIGGER trg_consent_human_only BEFORE INSERT ON consent_events BEGIN
    SELECT RAISE(ABORT, 'consenting_actor_id must be human') WHERE NOT EXISTS
        (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.consenting_actor_id);
END;
CREATE TRIGGER trg_consent_lineage_integrity BEFORE INSERT ON consent_events
    WHEN NEW.event_kind != 'grant'
BEGIN
    SELECT RAISE(ABORT, 'prior_event_id must belong to the same lineage, scope, and consenting actor')
    WHERE NOT EXISTS (
        SELECT 1 FROM consent_events prior
        WHERE prior.consent_event_id = NEW.prior_event_id
          AND prior.lineage_id = NEW.lineage_id
          AND prior.scope_id = NEW.scope_id
          AND prior.consenting_actor_id = NEW.consenting_actor_id
    );
END;
CREATE TRIGGER trg_consent_no_update BEFORE UPDATE ON consent_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_consent_no_delete BEFORE DELETE ON consent_events BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE guardian_authorization_events (
    guardian_auth_event_id      TEXT PRIMARY KEY,
    lineage_id                     TEXT NOT NULL,
    scope_id                          TEXT NOT NULL REFERENCES authorization_scopes(scope_id),
    policy_version                       TEXT NOT NULL,
    detail                                  TEXT,
    authorizing_actor_id                       TEXT NOT NULL REFERENCES actors(actor_id),
    ward_actor_id                                 TEXT NOT NULL REFERENCES actors(actor_id),
    event_kind                                      TEXT NOT NULL CHECK (event_kind IN ('grant','revocation','expiration','review')),
    prior_event_id                                    TEXT REFERENCES guardian_authorization_events(guardian_auth_event_id),
    effective_at                                        INTEGER NOT NULL,
    recorded_at                                           INTEGER NOT NULL,
    basis                                                   TEXT,
    source_event_id                                           TEXT REFERENCES events(event_id),
    CHECK (authorizing_actor_id != ward_actor_id),
    CHECK (
        (event_kind = 'grant' AND prior_event_id IS NULL AND lineage_id = guardian_auth_event_id)
        OR (event_kind != 'grant' AND prior_event_id IS NOT NULL AND lineage_id != guardian_auth_event_id)
    )
);
CREATE INDEX idx_guardian_auth_lineage ON guardian_authorization_events(lineage_id, effective_at);
CREATE UNIQUE INDEX idx_guardian_auth_prior_unique
    ON guardian_authorization_events(prior_event_id) WHERE prior_event_id IS NOT NULL;
CREATE TRIGGER trg_guardian_human_only BEFORE INSERT ON guardian_authorization_events BEGIN
    SELECT RAISE(ABORT, 'authorizing_actor_id must be human') WHERE NOT EXISTS
        (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.authorizing_actor_id);
    SELECT RAISE(ABORT, 'ward_actor_id must be human') WHERE NOT EXISTS
        (SELECT 1 FROM actor_human_person WHERE actor_id = NEW.ward_actor_id);
END;
CREATE TRIGGER trg_guardian_auth_lineage_integrity BEFORE INSERT ON guardian_authorization_events
    WHEN NEW.event_kind != 'grant'
BEGIN
    SELECT RAISE(ABORT, 'prior_event_id must belong to the same lineage, scope, and ward')
    WHERE NOT EXISTS (
        SELECT 1 FROM guardian_authorization_events prior
        WHERE prior.guardian_auth_event_id = NEW.prior_event_id
          AND prior.lineage_id = NEW.lineage_id
          AND prior.scope_id = NEW.scope_id
          AND prior.ward_actor_id = NEW.ward_actor_id
    );
END;
CREATE TRIGGER trg_guardian_auth_no_update BEFORE UPDATE ON guardian_authorization_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_guardian_auth_no_delete BEFORE DELETE ON guardian_authorization_events BEGIN SELECT RAISE(ABORT,'append-only'); END;

CREATE TABLE persistence_authorization_events (
    persistence_auth_event_id    TEXT PRIMARY KEY,
    lineage_id                       TEXT NOT NULL,
    scope_id                            TEXT NOT NULL REFERENCES authorization_scopes(scope_id),
    policy_version                         TEXT NOT NULL,
    detail                                    TEXT,
    granting_actor_id                            TEXT NOT NULL REFERENCES actors(actor_id),
    subject_actor_id                                TEXT REFERENCES actors(actor_id),
    event_kind                                        TEXT NOT NULL CHECK (event_kind IN ('grant','revocation','expiration','review')),
    prior_event_id                                      TEXT REFERENCES persistence_authorization_events(persistence_auth_event_id),
    effective_at                                          INTEGER NOT NULL,
    recorded_at                                             INTEGER NOT NULL,
    basis                                                     TEXT,
    source_event_id                                             TEXT REFERENCES events(event_id),
    CHECK (
        (event_kind = 'grant' AND prior_event_id IS NULL AND lineage_id = persistence_auth_event_id)
        OR (event_kind != 'grant' AND prior_event_id IS NOT NULL AND lineage_id != persistence_auth_event_id)
    )
);
CREATE INDEX idx_persistence_auth_lineage ON persistence_authorization_events(lineage_id, effective_at);
CREATE UNIQUE INDEX idx_persistence_auth_prior_unique
    ON persistence_authorization_events(prior_event_id) WHERE prior_event_id IS NOT NULL;
CREATE TRIGGER trg_persistence_auth_lineage_integrity BEFORE INSERT ON persistence_authorization_events
    WHEN NEW.event_kind != 'grant'
BEGIN
    SELECT RAISE(ABORT, 'prior_event_id must belong to the same lineage, scope, and subject')
    WHERE NOT EXISTS (
        SELECT 1 FROM persistence_authorization_events prior
        WHERE prior.persistence_auth_event_id = NEW.prior_event_id
          AND prior.lineage_id = NEW.lineage_id
          AND prior.scope_id = NEW.scope_id
          AND prior.subject_actor_id IS NEW.subject_actor_id
    );
END;
CREATE TRIGGER trg_persistence_auth_no_update BEFORE UPDATE ON persistence_authorization_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_persistence_auth_no_delete BEFORE DELETE ON persistence_authorization_events BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.9 Relationship records
# =============================================================================
DDL_2_9_RELATIONSHIP_RECORDS = """
CREATE TABLE relationship_records (
    relationship_record_id      TEXT PRIMARY KEY,
    actor_a_id                     TEXT NOT NULL REFERENCES actors(actor_id),
    actor_b_id                        TEXT NOT NULL REFERENCES actors(actor_id),
    relationship_type                    TEXT NOT NULL,
    record_kind                             TEXT NOT NULL CHECK (record_kind IN ('asserted','supersedes','retracts')),
    references_record_id                       TEXT REFERENCES relationship_records(relationship_record_id),
    effective_at                                 INTEGER NOT NULL,
    recorded_at                                    INTEGER NOT NULL,
    basis                                            TEXT,
    source_event_id                                    TEXT REFERENCES events(event_id),
    CHECK (actor_a_id != actor_b_id),
    CHECK ((record_kind = 'asserted' AND references_record_id IS NULL)
        OR (record_kind != 'asserted' AND references_record_id IS NOT NULL))
);
CREATE TRIGGER trg_relationship_records_thread_integrity BEFORE INSERT ON relationship_records
    WHEN NEW.record_kind != 'asserted'
BEGIN
    SELECT RAISE(ABORT, 'references_record_id must belong to the same relationship thread (same actor pair, same direction, same relationship_type)')
    WHERE NOT EXISTS (
        SELECT 1 FROM relationship_records prior
        WHERE prior.relationship_record_id = NEW.references_record_id
          AND prior.actor_a_id = NEW.actor_a_id
          AND prior.actor_b_id = NEW.actor_b_id
          AND prior.relationship_type = NEW.relationship_type
    );
END;
CREATE TRIGGER trg_relationship_records_no_update BEFORE UPDATE ON relationship_records BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_relationship_records_no_delete BEFORE DELETE ON relationship_records BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.10 Legacy routing provenance
# =============================================================================
DDL_2_10_LEGACY_ROUTING = """
CREATE TABLE legacy_routing_provenance (
    legacy_routing_id        TEXT PRIMARY KEY,
    event_id                     TEXT NOT NULL REFERENCES events(event_id),
    routing_constant_value          TEXT NOT NULL,
    routing_mechanism                  TEXT NOT NULL,
    source_code_reference                 TEXT,
    recorded_at                              INTEGER NOT NULL
);
CREATE TRIGGER trg_legacy_routing_no_update BEFORE UPDATE ON legacy_routing_provenance BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_legacy_routing_no_delete BEFORE DELETE ON legacy_routing_provenance BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# =============================================================================
# 2.11 Guardian-requirement policy
# =============================================================================
DDL_2_11_GUARDIAN_REQUIREMENT = """
CREATE TABLE guardian_requirement_policies (
    policy_id                    TEXT PRIMARY KEY,
    lineage_id                      TEXT NOT NULL,
    subject_actor_id                   TEXT REFERENCES actors(actor_id),
    subject_description                    TEXT,
    scope_id                                  TEXT NOT NULL REFERENCES authorization_scopes(scope_id),
    requires_guardian_authorization              INTEGER NOT NULL CHECK (requires_guardian_authorization IN (0,1)),
    policy_version                                  TEXT NOT NULL,
    event_kind                                        TEXT NOT NULL CHECK (event_kind IN ('determined','revised','retired')),
    prior_event_id                                      TEXT REFERENCES guardian_requirement_policies(policy_id),
    effective_at                                          INTEGER NOT NULL,
    recorded_at                                             INTEGER NOT NULL,
    basis                                                     TEXT,
    determined_by_actor_id                                      TEXT NOT NULL REFERENCES actors(actor_id),
    source_event_id                                               TEXT REFERENCES events(event_id),
    CHECK (subject_actor_id IS NOT NULL OR subject_description IS NOT NULL),
    CHECK (
        (event_kind = 'determined' AND prior_event_id IS NULL AND lineage_id = policy_id)
        OR (event_kind != 'determined' AND prior_event_id IS NOT NULL AND lineage_id != policy_id)
    ),
    CHECK (event_kind != 'retired' OR requires_guardian_authorization = 1)
);
CREATE INDEX idx_guardian_requirement_lineage ON guardian_requirement_policies(lineage_id, effective_at);
CREATE UNIQUE INDEX idx_guardian_requirement_prior_unique
    ON guardian_requirement_policies(prior_event_id) WHERE prior_event_id IS NOT NULL;
CREATE TRIGGER trg_guardian_requirement_lineage_integrity BEFORE INSERT ON guardian_requirement_policies
    WHEN NEW.event_kind != 'determined'
BEGIN
    SELECT RAISE(ABORT, 'prior_event_id must belong to the same lineage, scope, and subject')
    WHERE NOT EXISTS (
        SELECT 1 FROM guardian_requirement_policies prior
        WHERE prior.policy_id = NEW.prior_event_id
          AND prior.lineage_id = NEW.lineage_id
          AND prior.scope_id = NEW.scope_id
          AND prior.subject_actor_id IS NEW.subject_actor_id
          AND prior.subject_description IS NEW.subject_description
    );
END;
CREATE TRIGGER trg_guardian_requirement_no_update BEFORE UPDATE ON guardian_requirement_policies BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trg_guardian_requirement_no_delete BEFORE DELETE ON guardian_requirement_policies BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""

# Twelve SQL blocks, in the exact dependency-respecting order needed for
# CREATE TABLE statements referencing earlier tables to succeed. This
# ordering is an execution-order necessity only -- it does not change
# any table/column/constraint definition from the frozen spec.
ALL_DDL_BLOCKS = [
    ("2.1 actors/persons/subtypes", DDL_2_1_ACTORS),
    ("2.3 pipelines (created early for FK ordering)", DDL_2_3_PIPELINES_FIRST),
    ("2.2 sessions/auth_contexts", DDL_2_2_SESSIONS_AUTH),
    ("2.3 architecture_epochs", DDL_2_3_EPOCHS),
    ("2.4 model_revisions", DDL_2_4_MODEL_REVISIONS),
    ("2.5 events/migration_status/model_participation/components", DDL_2_5_EVENTS),
    ("2.8 authorization_scopes (created before streams and event_relied_upon_* need it)", DDL_2_8_SCOPES),
    ("2.9 relationship_records (created before event_relationship_context needs it)", DDL_2_9_RELATIONSHIP_RECORDS),
    ("2.8 consent/guardian/persistence authorization streams", DDL_2_8_AUTHORIZATION_STREAMS),
    ("2.6 normalized per-event provenance relations", DDL_2_6_EVENT_RELATIONS),
    ("2.7 propositions/assertions/evidence/relations", DDL_2_7_PROPOSITIONS),
    ("2.10 legacy_routing_provenance", DDL_2_10_LEGACY_ROUTING),
    ("2.11 guardian_requirement_policies", DDL_2_11_GUARDIAN_REQUIREMENT),
]


# Canonical identity is append-only (design invariant 2), except for
# actors.retired_at. Existing writers SELECT/check before reapplication;
# none relies on duplicate INSERT, REPLACE, or UPSERT of identity rows.
# Reject duplicate identities BEFORE SQLite handles conflicts: REPLACE's
# implicit deletion need not fire DELETE triggers when recursive_triggers
# is OFF. Cover every UNIQUE key AND explicit rowid collisions, including
# UPDATE OR REPLACE of an actor's rowid. No connection pragma is trusted.
# Exact duplicate SQL inserts are rejected too; lawful seeding/registration
# remains idempotent through its existing read/check path. No rows change.
IDENTITY_GUARD_DDL = (
    """CREATE TRIGGER IF NOT EXISTS trg_actors_canonical_key_immutable
    BEFORE UPDATE ON actors
    WHEN NEW.actor_id IS NOT OLD.actor_id OR NEW.rowid IS NOT OLD.rowid
    BEGIN SELECT RAISE(ABORT, 'canonical actor key is immutable'); END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_actors_no_identity_replacement
    BEFORE INSERT ON actors
    WHEN EXISTS (SELECT 1 FROM actors
                 WHERE actor_id = NEW.actor_id OR stable_key = NEW.stable_key
                    OR rowid = NEW.rowid)
    BEGIN SELECT RAISE(ABORT, 'canonical actor identity already exists'); END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_actor_human_person_no_identity_replacement
    BEFORE INSERT ON actor_human_person
    WHEN EXISTS (SELECT 1 FROM actor_human_person
                 WHERE actor_id = NEW.actor_id OR person_id = NEW.person_id
                    OR rowid = NEW.rowid)
    BEGIN SELECT RAISE(ABORT, 'canonical person-actor linkage already exists'); END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_persons_no_identity_replacement
    BEFORE INSERT ON persons
    WHEN EXISTS (SELECT 1 FROM persons
                 WHERE person_id = NEW.person_id OR rowid = NEW.rowid)
    BEGIN SELECT RAISE(ABORT, 'canonical person identity already exists'); END;""",
)


def install_identity_guards(conn: sqlite3.Connection) -> None:
    """Add guards to fresh or historical canonical schema, in the caller's
    transaction. Never executescript/commit here: SLP2 migration must be
    able to roll back identity guards with the rest of its upgrade.
    Persons already rejects ALL updates/deletes, including metadata;
    this closes replacement, not a new policy for mutable profiles.
    """
    for ddl in IDENTITY_GUARD_DDL:
        conn.execute(ddl)


def create_provenance_db(db_path: str) -> sqlite3.Connection:
    """Creates a fresh anaxi_provenance.db at db_path with the complete
    schema from the frozen spec. Refuses to run against an existing
    file with any tables already present -- this function creates
    schema, it never migrates or alters an existing populated DB."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if existing:
        conn.close()
        raise RuntimeError(
            f"{db_path} already has tables {existing} -- refusing to run DDL "
            f"against a non-empty database. Use a fresh path."
        )
    for label, ddl in ALL_DDL_BLOCKS:
        try:
            conn.executescript(ddl)
        except sqlite3.Error as e:
            conn.close()
            raise RuntimeError(f"DDL block {label!r} failed: {e}") from e
    install_identity_guards(conn)
    conn.commit()
    return conn


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python provenance_schema.py <path-to-new-db-file>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    conn = create_provenance_db(path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    print(f"Created {path} with {len(tables)} tables:")
    for (t,) in tables:
        print(f"  {t}")
    conn.close()
