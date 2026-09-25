"""
Anaxi -- Real verification of provenance_schema.py and
migrate_historical_data.py against the frozen canonical specification
(DESIGN_NOTE_identity_provenance_schema.md, SHA-256
7761d132aded85f18ca519aac6b189d541d22eee336755f8b8b3e02c885751c2).

Two suites:
  1. Schema checks -- DDL creation + every freeze-blocking malformed
     probe, run against a fresh in-memory DB via the REAL
     create_provenance_db().
  2. Migration integration suite -- calls the REAL migration/helper
     functions (seed_reference_data, migrate_jsonl,
     alter_existing_stores_schema, backfill_*, matches_legacy_pipeline,
     assert_is_rehearsal_dir, parse_iso_timestamp_to_unix,
     derive_stable_id) against small SYNTHETIC fixture files built in
     an isolated temp directory -- never the real 115-row production
     data, and never any live path.

Run:
    python test_provenance_schema.py
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from provenance_schema import create_provenance_db, derive_historical_event_id, derive_stable_id, ALL_DDL_BLOCKS
import migrate_historical_data as mig

results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def raises(fn, exc_types=(sqlite3.IntegrityError, sqlite3.OperationalError)):
    try:
        fn()
        return False
    except exc_types:
        return True


# =============================================================================
# Suite 1: schema checks
# =============================================================================
def run_schema_checks():
    conn = create_provenance_db(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())

    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                 "VALUES ('pipe_a', 'anaxi_orchestration_lineage_a', 'llama', 'llama pipeline')")
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                 "VALUES ('pipe_b', 'anaxi_orchestration_lineage_b', 'claude', 'claude pipeline')")

    conn.execute("INSERT INTO persons (person_id, created_at) VALUES ('person_1', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES ('actor_human_1', 'human_person', 'human_1', ?)", (now,))
    conn.execute("INSERT INTO actor_human_person (actor_id, person_id, relationship_established_at) "
                 "VALUES ('actor_human_1', 'person_1', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES ('actor_human_2', 'human_person', 'human_2', ?)", (now,))
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES ('person_2', ?)", (now,))
    conn.execute("INSERT INTO actor_human_person (actor_id, person_id, relationship_established_at) "
                 "VALUES ('actor_human_2', 'person_2', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES ('actor_clark', 'clark_agent', 'clark', ?)", (now,))
    conn.execute("INSERT INTO actor_clark_agent (actor_id) VALUES ('actor_clark')")
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES ('actor_host', 'host_system', 'bounded_clause_renderer', ?)", (now,))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES ('actor_host', 'bounded_clause_renderer')")

    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES ('sess_1', 'pipe_a', ?)", (now,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                 "VALUES ('auth_1', 'sess_1', 'unknown', ?)", (now,))

    conn.execute("INSERT INTO authorization_scopes (scope_id, scope_key, description) "
                 "VALUES ('scope_1', 'artifact_persistence', 'artifact persistence scope')")
    conn.commit()

    # --- DDL creation ---
    n_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    check("DDL creation: all 32 tables present after 12 DDL blocks", n_tables == 32)

    # --- Actor subtype integrity ---
    check("Actor subtype: wrong-typed subtype row rejected",
          raises(lambda: conn.execute("INSERT INTO actor_clark_agent (actor_id) VALUES ('actor_human_1')")))
    check("Actor subtype: exclusive subtype rejected (already human)",
          raises(lambda: conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES ('actor_human_1', 'x')")))

    # --- Identity immutability ---
    check("Actor immutability: actor_type update rejected",
          raises(lambda: conn.execute("UPDATE actors SET actor_type='clark_agent' WHERE actor_id='actor_human_1'")))
    check("Actor immutability: display_label update rejected",
          raises(lambda: conn.execute("UPDATE actors SET display_label='x' WHERE actor_id='actor_human_1'")))
    check("Person immutability: UPDATE rejected",
          raises(lambda: conn.execute("UPDATE persons SET notes='x' WHERE person_id='person_1'")))
    check("Person immutability: DELETE rejected",
          raises(lambda: conn.execute("DELETE FROM persons WHERE person_id='person_1'")))

    # --- Deterministic historical event ID ---
    id1 = derive_historical_event_id("anaxi_log.jsonl", b"abc1", 2)
    id2 = derive_historical_event_id("anaxi_log.jsonl", b"abc", 12)
    check("Deterministic ID: ambiguous preimage pair (b'abc1',2) vs (b'abc',12) produce DIFFERENT ids", id1 != id2)
    id3 = derive_historical_event_id("src_a", b"x", 1)
    id4 = derive_historical_event_id("src_b", b"x", 1)
    check("Deterministic ID: differing source_id alone produces different ids", id3 != id4)
    check("Deterministic ID: identical input reproduces identical id",
          id1 == derive_historical_event_id("anaxi_log.jsonl", b"abc1", 2))
    check("Deterministic ID: hist- prefix present", id1.startswith("hist-"))

    # --- Opaque reference-row ID collision safety ---
    pid_a = derive_stable_id("pipeline", "anaxi_orchestration_lineage_a")
    pid_b = derive_stable_id("pipeline", "anaxi_orchestration_lineage_b")
    check("derive_stable_id: different natural keys produce different opaque ids", pid_a != pid_b)
    check("derive_stable_id: same natural key reproduces identical id (idempotent seeding)",
          pid_a == derive_stable_id("pipeline", "anaxi_orchestration_lineage_a"))
    check("derive_stable_id: output is opaque, not a readable slug of the input",
          "anaxi_orchestration_lineage_a" not in pid_a)
    modelrev_1 = derive_stable_id("modelrev", "llama3.2:3b", "tag_only_degraded")
    modelrev_2 = derive_stable_id("modelrev", "gemma4:e4b", "tag_only_degraded")
    check("derive_stable_id: different model tags produce different ids", modelrev_1 != modelrev_2)

    # --- Pipeline/scope descriptive-field mutation ---
    check("Pipeline immutability: description update rejected",
          raises(lambda: conn.execute("UPDATE pipelines SET description='x' WHERE pipeline_id='pipe_a'")))
    check("Scope immutability: description update rejected",
          raises(lambda: conn.execute("UPDATE authorization_scopes SET description='x' WHERE scope_id='scope_1'")))

    # --- Session reopen/reclose/end-before-start ---
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) "
                 "VALUES ('sess_closed', 'pipe_a', ?, ?)", (now, now + 100))
    conn.commit()
    check("Session reopen rejected",
          raises(lambda: conn.execute("UPDATE sessions SET ended_at=NULL WHERE session_id='sess_closed'")))
    check("Session re-close rejected",
          raises(lambda: conn.execute("UPDATE sessions SET ended_at=? WHERE session_id='sess_closed'", (now + 200,))))
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES ('sess_open', 'pipe_a', ?)", (now,))
    conn.commit()
    check("Session end-before-start rejected",
          raises(lambda: conn.execute("UPDATE sessions SET ended_at=? WHERE session_id='sess_open'", (now - 10,))))
    conn.execute("UPDATE sessions SET ended_at=? WHERE session_id='sess_open'", (now + 5,))
    conn.commit()
    check("Session valid one-way close succeeds",
          conn.execute("SELECT ended_at FROM sessions WHERE session_id='sess_open'").fetchone()[0] == now + 5)

    # --- Actor unretire/re-retire ---
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at, retired_at) "
                 "VALUES ('actor_retired', 'host_system', 'retired_test', ?, ?)", (now, now + 50))
    conn.commit()
    check("Actor unretire rejected",
          raises(lambda: conn.execute("UPDATE actors SET retired_at=NULL WHERE actor_id='actor_retired'")))
    check("Actor re-retire rejected",
          raises(lambda: conn.execute("UPDATE actors SET retired_at=? WHERE actor_id='actor_retired'", (now + 100,))))

    # --- Branching probes: EVERY table with a unique-predecessor constraint ---
    # consent_events
    conn.execute("INSERT INTO consent_events (consent_event_id, lineage_id, scope_id, policy_version, "
                 "consenting_actor_id, event_kind, effective_at, recorded_at) "
                 "VALUES ('c_grant', 'c_grant', 'scope_1', 'v1', 'actor_human_1', 'grant', ?, ?)", (now, now))
    conn.execute("INSERT INTO consent_events (consent_event_id, lineage_id, scope_id, policy_version, "
                 "consenting_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
                 "VALUES ('c_rev1', 'c_grant', 'scope_1', 'v1', 'actor_human_1', 'revocation', 'c_grant', ?, ?)", (now + 1, now + 1))
    conn.commit()
    check("Branching probe [consent_events]: second claimant of same prior_event_id rejected",
          raises(lambda: conn.execute(
              "INSERT INTO consent_events (consent_event_id, lineage_id, scope_id, policy_version, "
              "consenting_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
              "VALUES ('c_rev2', 'c_grant', 'scope_1', 'v1', 'actor_human_1', 'revocation', 'c_grant', ?, ?)", (now + 2, now + 2))))

    # auth_contexts
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                 "VALUES ('auth_2', 'sess_1', 'unknown', ?)", (now + 1,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at, supersedes_auth_context_id) "
                 "VALUES ('auth_3', 'sess_1', 'unknown', ?, 'auth_1')", (now + 2,))
    conn.commit()
    check("Branching probe [auth_contexts]: second supersession claimant rejected",
          raises(lambda: conn.execute(
              "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at, supersedes_auth_context_id) "
              "VALUES ('auth_2b', 'sess_1', 'unknown', ?, 'auth_1')", (now + 3,))))

    # guardian_authorization_events (previously untested)
    conn.execute("INSERT INTO guardian_authorization_events (guardian_auth_event_id, lineage_id, scope_id, "
                 "policy_version, authorizing_actor_id, ward_actor_id, event_kind, effective_at, recorded_at) "
                 "VALUES ('g_grant', 'g_grant', 'scope_1', 'v1', 'actor_human_1', 'actor_human_2', 'grant', ?, ?)", (now, now))
    conn.execute("INSERT INTO guardian_authorization_events (guardian_auth_event_id, lineage_id, scope_id, "
                 "policy_version, authorizing_actor_id, ward_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
                 "VALUES ('g_rev1', 'g_grant', 'scope_1', 'v1', 'actor_human_1', 'actor_human_2', 'revocation', 'g_grant', ?, ?)", (now + 1, now + 1))
    conn.commit()
    check("Branching probe [guardian_authorization_events]: second claimant of same prior_event_id rejected",
          raises(lambda: conn.execute(
              "INSERT INTO guardian_authorization_events (guardian_auth_event_id, lineage_id, scope_id, "
              "policy_version, authorizing_actor_id, ward_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
              "VALUES ('g_rev2', 'g_grant', 'scope_1', 'v1', 'actor_human_1', 'actor_human_2', 'revocation', 'g_grant', ?, ?)", (now + 2, now + 2))))

    # persistence_authorization_events (previously untested)
    conn.execute("INSERT INTO persistence_authorization_events (persistence_auth_event_id, lineage_id, scope_id, "
                 "policy_version, granting_actor_id, event_kind, effective_at, recorded_at) "
                 "VALUES ('p_grant', 'p_grant', 'scope_1', 'v1', 'actor_host', 'grant', ?, ?)", (now, now))
    conn.execute("INSERT INTO persistence_authorization_events (persistence_auth_event_id, lineage_id, scope_id, "
                 "policy_version, granting_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
                 "VALUES ('p_rev1', 'p_grant', 'scope_1', 'v1', 'actor_host', 'revocation', 'p_grant', ?, ?)", (now + 1, now + 1))
    conn.commit()
    check("Branching probe [persistence_authorization_events]: second claimant of same prior_event_id rejected",
          raises(lambda: conn.execute(
              "INSERT INTO persistence_authorization_events (persistence_auth_event_id, lineage_id, scope_id, "
              "policy_version, granting_actor_id, event_kind, prior_event_id, effective_at, recorded_at) "
              "VALUES ('p_rev2', 'p_grant', 'scope_1', 'v1', 'actor_host', 'revocation', 'p_grant', ?, ?)", (now + 2, now + 2))))

    # guardian_requirement_policies (previously untested for BRANCHING specifically)
    conn.execute("INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
                 "requires_guardian_authorization, policy_version, event_kind, effective_at, recorded_at, determined_by_actor_id) "
                 "VALUES ('grp_det', 'grp_det', 'actor_human_1', 'scope_1', 1, 'v1', 'determined', ?, ?, 'actor_host')", (now, now))
    conn.execute("INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
                 "requires_guardian_authorization, policy_version, event_kind, prior_event_id, effective_at, recorded_at, determined_by_actor_id) "
                 "VALUES ('grp_rev1', 'grp_det', 'actor_human_1', 'scope_1', 0, 'v1', 'revised', 'grp_det', ?, ?, 'actor_host')", (now + 1, now + 1))
    conn.commit()
    check("Branching probe [guardian_requirement_policies]: second claimant of same prior_event_id rejected",
          raises(lambda: conn.execute(
              "INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
              "requires_guardian_authorization, policy_version, event_kind, prior_event_id, effective_at, recorded_at, determined_by_actor_id) "
              "VALUES ('grp_rev2', 'grp_det', 'actor_human_1', 'scope_1', 0, 'v1', 'revised', 'grp_det', ?, ?, 'actor_host')", (now + 2, now + 2))))

    # --- Retired guardian policy: never resolves permissively ---
    check("guardian_requirement_policies: retired row with requires=0 rejected outright",
          raises(lambda: conn.execute(
              "INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
              "requires_guardian_authorization, policy_version, event_kind, effective_at, recorded_at, determined_by_actor_id) "
              "VALUES ('grp_bad', 'grp_bad', 'actor_human_1', 'scope_1', 0, 'v1', 'retired', ?, ?, 'actor_host')", (now, now))))
    conn.execute("INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
                 "requires_guardian_authorization, policy_version, event_kind, effective_at, recorded_at, determined_by_actor_id) "
                 "VALUES ('grp_det2', 'grp_det2', 'actor_human_1', 'scope_1', 1, 'v1', 'determined', ?, ?, 'actor_host')", (now, now))
    conn.execute("INSERT INTO guardian_requirement_policies (policy_id, lineage_id, subject_actor_id, scope_id, "
                 "requires_guardian_authorization, policy_version, event_kind, prior_event_id, effective_at, recorded_at, determined_by_actor_id) "
                 "VALUES ('grp_ret2', 'grp_det2', 'actor_human_1', 'scope_1', 1, 'v1', 'retired', 'grp_det2', ?, ?, 'actor_host')", (now + 1, now + 1))
    conn.commit()
    latest = conn.execute(
        "SELECT event_kind, requires_guardian_authorization FROM guardian_requirement_policies "
        "WHERE lineage_id='grp_det2' ORDER BY effective_at DESC, policy_id DESC LIMIT 1"
    ).fetchone()
    resolved_requires = True if latest is None or latest[0] == 'retired' else bool(latest[1])
    check("guardian_requirement_policies: retired lineage resolves to fail-restrictive default, not a permissive read",
          resolved_requires is True)

    # --- Canonical event transaction rollback, via the REAL create_provenance_db ---
    conn2 = create_provenance_db(tempfile.mktemp(suffix=".db"))
    conn2.execute("PRAGMA foreign_keys = ON;")
    conn2.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label) VALUES ('p', 'lin_a', 'llama')")
    conn2.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES ('clark', 'clark_agent', 'clark', ?)", (now,))
    conn2.execute("INSERT INTO actor_clark_agent (actor_id) VALUES ('clark')")
    conn2.commit()
    try:
        conn2.execute("BEGIN")
        conn2.execute("INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, occurred_at, record_created_at) "
                      "VALUES ('ev_rollback_test', 'waking_turn', 'p', 'known', ?, ?)", (now, now))
        conn2.execute("INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                      "authorship_resolution, component_text, content_sha256) "
                      "VALUES ('ev_rollback_test', 0, NULL, 'clark_prose', 'resolved', 'x', 'y')")
        conn2.commit()
    except sqlite3.IntegrityError:
        conn2.rollback()
    remaining = conn2.execute("SELECT COUNT(*) FROM events WHERE event_id='ev_rollback_test'").fetchone()[0]
    check("Canonical transaction rollback (via real create_provenance_db): zero partial rows survive",
          remaining == 0)
    conn2.close()

    # --- Authentication-state matrix ---
    check("auth_state='unauthenticated' with auth_method=NULL rejected",
          raises(lambda: conn.execute(
              "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
              "VALUES ('auth_bad1', 'sess_1', 'unauthenticated', ?)", (now,))))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, auth_method, established_at) "
                 "VALUES ('auth_ok_unauth', 'sess_1', 'unauthenticated', 'password', ?)", (now,))
    check("auth_state='unauthenticated' with a real auth_method succeeds", True)
    check("auth_state='unauthenticated' with assurance_level='high' still rejected",
          raises(lambda: conn.execute(
              "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, auth_method, assurance_level, established_at) "
              "VALUES ('auth_bad2', 'sess_1', 'unauthenticated', 'password', 'high', ?)", (now,))))

    conn.close()


# =============================================================================
# Suite 2: migration integration -- REAL functions, SYNTHETIC fixtures
# =============================================================================
SYNTHETIC_JSONL_LINES = [
    '{"timestamp": "2026-01-01T12:00:00.000000+00:00", "substrate": "llama", "model": "llama3.2:3b", '
    '"waking_model_tag": "llama3.2:3b", "prompt": "synthetic prompt one", "response": "synthetic response one", "kardia": {}}',
    '{"timestamp": "2026-01-01T13:30:00.000000+05:00", "substrate": "claude", "model": "claude-sonnet-5", '
    '"prompt": "synthetic prompt two", "response": "synthetic response two", "kardia": {}}',
    '{"timestamp": "2026-01-01T14:00:00.000000+00:00", "substrate": "llama", "model": "llama3.2:3b", '
    '"prompt": "synthetic prompt three", "response": "synthetic response three", "kardia": {}}',
]


def build_synthetic_relational_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            agent_observation TEXT,
            external_observation TEXT,
            agent_response TEXT,
            linked_memories TEXT,
            linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded'
        )
    """)
    for _ in range(3):
        conn.execute("INSERT INTO relational_events (user_id, substrate, created_at) VALUES ('synthetic_user', 'llama', 0)")
    conn.commit()
    conn.close()


def build_synthetic_mind_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE nodes (
        user_id TEXT NOT NULL, id TEXT NOT NULL, label TEXT, type TEXT, salience_score REAL,
        description TEXT, last_accessed_timestamp INTEGER, asserted_at INTEGER,
        PRIMARY KEY (user_id, id))""")
    conn.execute("""CREATE TABLE active_kardia (
        user_id TEXT PRIMARY KEY, moral_valve TEXT, volitional_channel TEXT,
        affective_stance TEXT, aesthetic_valve TEXT, updated_at INTEGER)""")
    conn.execute("""CREATE TABLE kardia_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, moral_valve TEXT,
        volitional_channel TEXT, affective_stance TEXT, aesthetic_valve TEXT, recorded_at INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE turn_generation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
        temperature REAL, top_p REAL, style_instruction TEXT)""")
    conn.execute("""CREATE TABLE proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, proposal_type TEXT NOT NULL,
        payload TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL, resolved_at INTEGER)""")
    for _ in range(2):
        conn.execute("INSERT INTO turn_generation_log (user_id, timestamp, temperature, top_p) VALUES ('synthetic_user', 0, 0.4, 0.85)")
    conn.execute("INSERT INTO nodes (user_id, id, label, type, salience_score, description) "
                 "VALUES ('synthetic_user', 'n1', 'test node', 'Fact', 5.0, 'synthetic')")
    conn.execute("INSERT INTO active_kardia (user_id, moral_valve, updated_at) VALUES ('synthetic_user', 'x', 0)")
    conn.execute("INSERT INTO kardia_history (user_id, moral_valve, recorded_at) VALUES ('synthetic_user', 'x', 0)")
    conn.execute("INSERT INTO proposals (user_id, proposal_type, payload, status, created_at) "
                 "VALUES ('synthetic_user', 'delete_node', '{}', 'pending', 0)")
    conn.commit()
    conn.close()


def run_migration_integration_suite():
    tmp_root = tempfile.mkdtemp(prefix="anaxi_provenance_test_")
    try:
        # --- Rehearsal-only structural enforcement ---
        not_a_rehearsal_dir = os.path.join(tmp_root, "plain_dir")
        os.makedirs(not_a_rehearsal_dir)
        check("assert_is_rehearsal_dir: refuses a directory with no marker",
              raises(lambda: mig.assert_is_rehearsal_dir(not_a_rehearsal_dir), (mig.NotARehearsalDirectoryError,)))

        # Entirely simulated: assert_is_rehearsal_dir() now accepts an
        # injectable live_dir parameter specifically so this check can
        # be exercised without ever creating or deleting anything in
        # the real module/repository directory.
        simulated_live_dir = os.path.join(tmp_root, "simulated_live_dir")
        os.makedirs(simulated_live_dir)
        with open(os.path.join(simulated_live_dir, mig.REHEARSAL_MARKER_FILENAME), "w", encoding="utf-8") as f:
            f.write(mig.REHEARSAL_MARKER_CONTENT)
        check("assert_is_rehearsal_dir: refuses a directory that IS the (simulated) live "
              "directory, even with a correct marker present",
              raises(lambda: mig.assert_is_rehearsal_dir(simulated_live_dir, live_dir=simulated_live_dir),
                     (mig.NotARehearsalDirectoryError,)))

        rehearsal_dir = os.path.join(tmp_root, "rehearsal")
        jsonl_src = os.path.join(tmp_root, "src_anaxi_log.jsonl")
        with open(jsonl_src, "w", encoding="utf-8") as f:
            f.write("\n".join(SYNTHETIC_JSONL_LINES) + "\n")
        relational_src = os.path.join(tmp_root, "src_relational.db")
        build_synthetic_relational_db(relational_src)
        mind_src = os.path.join(tmp_root, "src_mind.db")
        build_synthetic_mind_db(mind_src)

        mig.create_rehearsal_dir(rehearsal_dir, {
            "anaxi_log.jsonl": jsonl_src,
            "anaxi_relational_llama.db": relational_src,
            "anaxi_mind_llama.db": mind_src,
        })
        check("assert_is_rehearsal_dir: accepts a properly-created rehearsal directory",
              mig.assert_is_rehearsal_dir(rehearsal_dir) is None)

        # --- create_rehearsal_dir anti-aliasing hardening ---
        check("create_rehearsal_dir: refuses a destination that already exists (not fresh)",
              raises(lambda: mig.create_rehearsal_dir(rehearsal_dir, {"anaxi_log.jsonl": jsonl_src}),
                     (mig.RehearsalSafetyError,)))

        path_bearing_dest = os.path.join(tmp_root, "rehearsal_pathbearing")
        check("create_rehearsal_dir: rejects a path-bearing destination name (traversal/escape attempt)",
              raises(lambda: mig.create_rehearsal_dir(
                  path_bearing_dest, {"../escaped.jsonl": jsonl_src}), (mig.RehearsalSafetyError,)))
        # A failed create_rehearsal_dir call may still leave a freshly-made,
        # empty directory behind (the fresh-dir check runs before the
        # per-file loop) -- clean it up so it doesn't interfere with later checks.
        shutil.rmtree(path_bearing_dest, ignore_errors=True)

        # --- Forced short-write regression test: os.write()'s contract
        # does NOT guarantee a single call writes every requested byte.
        # Monkeypatch os.write to deliberately write only a few bytes
        # per call and confirm _write_all_bytes (via create_rehearsal_dir)
        # still produces a byte-identical destination. ---
        shortwrite_dest = os.path.join(tmp_root, "rehearsal_shortwrite")
        shortwrite_source = os.path.join(tmp_root, "shortwrite_src.jsonl")
        shortwrite_content = ("line %d of forced-short-write test content\n" % i for i in range(500))
        with open(shortwrite_source, "w", encoding="utf-8") as f:
            f.writelines(shortwrite_content)
        with open(shortwrite_source, "rb") as f:
            shortwrite_expected = f.read()

        real_os_write = os.write

        def _short_write(fd, data):
            # Never write more than 7 bytes in one call, forcing
            # _write_all_bytes to loop many times if it is correct,
            # and to silently truncate the file if it is not.
            return real_os_write(fd, data[:7])

        os.write = _short_write
        try:
            mig.create_rehearsal_dir(shortwrite_dest, {"anaxi_log.jsonl": shortwrite_source})
        finally:
            os.write = real_os_write
        with open(os.path.join(shortwrite_dest, "anaxi_log.jsonl"), "rb") as f:
            shortwrite_actual = f.read()
        check("create_rehearsal_dir: survives a forced short-write (os.write() capped at 7 "
              "bytes/call) and still produces a byte-identical destination",
              shortwrite_actual == shortwrite_expected)
        shutil.rmtree(shortwrite_dest, ignore_errors=True)

        symlinked_source_dest = os.path.join(tmp_root, "rehearsal_symlinked_source")
        symlink_source_target = os.path.join(tmp_root, "symlink_target.jsonl")
        with open(symlink_source_target, "w", encoding="utf-8") as f:
            f.write("real content\n")
        symlink_as_source = os.path.join(tmp_root, "symlink_as_source.jsonl")
        symlink_supported = True
        try:
            os.symlink(symlink_source_target, symlink_as_source)
        except (OSError, NotImplementedError):
            symlink_supported = False

        if symlink_supported:
            check("create_rehearsal_dir: refuses a SOURCE that is itself a symlink",
                  raises(lambda: mig.create_rehearsal_dir(
                      symlinked_source_dest, {"anaxi_log.jsonl": symlink_as_source}), (mig.RehearsalSafetyError,)))
            shutil.rmtree(symlinked_source_dest, ignore_errors=True)
        else:
            print("SKIP: create_rehearsal_dir symlink-source test -- symlink creation not permitted in this environment")

        # Hardlink case (works without special privileges on NTFS, unlike
        # symlinks): a rehearsal child that is the SAME FILE as a live
        # source via a hardlink must be refused by samefile(), not merely
        # by a symlink check, which a hardlink would slip past entirely.
        hardlink_dest = os.path.join(tmp_root, "rehearsal_hardlink_test")
        hardlink_source_target = os.path.join(tmp_root, "hardlink_target.jsonl")
        with open(hardlink_source_target, "w", encoding="utf-8") as f:
            f.write("real content\n")
        hardlinked_source = os.path.join(tmp_root, "hardlinked_as_source.jsonl")
        try:
            os.link(hardlink_source_target, hardlinked_source)
            hardlink_supported = True
        except OSError:
            hardlink_supported = False

        if hardlink_supported:
            # A hardlinked SOURCE is not itself a vulnerability: copying
            # via read-bytes-then-write-new-file (with O_EXCL) always
            # produces a genuinely independent destination inode
            # regardless of whether the source path happens to be one of
            # several hardlinks to the same underlying file -- so this is
            # correctly ACCEPTED, not rejected. The real hardlink risk is
            # a REHEARSAL CHILD later aliasing a live file, tested below.
            mig.create_rehearsal_dir(hardlink_dest, {"anaxi_log.jsonl": hardlinked_source})
            check("create_rehearsal_dir: a hardlinked SOURCE still produces a genuinely "
                  "independent (non-aliased) destination copy",
                  not os.path.samefile(os.path.join(hardlink_dest, "anaxi_log.jsonl"), hardlink_source_target))
            shutil.rmtree(hardlink_dest, ignore_errors=True)

            # --- The exact case: root directory legitimate, ONE db child
            # aliases an external/live file via a hardlink placed into an
            # otherwise-properly-created rehearsal directory afterward.
            # Entirely simulated -- a fake temporary "live directory",
            # never the real module/repository directory, exercised via
            # assert_is_rehearsal_dir's injectable live_dir parameter. ---
            simulated_live_dir2 = os.path.join(tmp_root, "simulated_live_dir_for_alias_test")
            os.makedirs(simulated_live_dir2)
            fake_live_file = os.path.join(simulated_live_dir2, "anaxi_mind_llama.db")
            build_synthetic_mind_db(fake_live_file)

            aliased_root = os.path.join(tmp_root, "rehearsal_aliased_child")
            aliased_jsonl_src = os.path.join(tmp_root, "aliased_src.jsonl")
            with open(aliased_jsonl_src, "w", encoding="utf-8") as f:
                f.write("legit content\n")
            mig.create_rehearsal_dir(aliased_root, {"anaxi_log.jsonl": aliased_jsonl_src})
            # Root directory is entirely legitimate (marker present, not
            # the simulated live dir, no symlinks). Alias ONE child to
            # the simulated live file via a hardlink.
            aliased_child_path = os.path.join(aliased_root, "anaxi_mind_llama.db")
            os.link(fake_live_file, aliased_child_path)
            check("assert_is_rehearsal_dir: refuses when the root is otherwise legitimate but "
                  "ONE child is hardlinked to a (simulated) live-directory file of the same name",
                  raises(lambda: mig.assert_is_rehearsal_dir(aliased_root, live_dir=simulated_live_dir2),
                         (mig.NotARehearsalDirectoryError,)))
            shutil.rmtree(aliased_root, ignore_errors=True)

            # --- Symlinked live counterpart must NOT be exempted from
            # comparison. Build a THIRD simulated live dir where the
            # like-named entry is itself a symlink to a real target;
            # confirm a rehearsal child hardlinked to that REAL TARGET
            # is still caught (samefile() resolves through the live
            # symlink to the same underlying file). ---
            simulated_live_dir3 = os.path.join(tmp_root, "simulated_live_dir_symlinked_counterpart")
            os.makedirs(simulated_live_dir3)
            real_target_elsewhere = os.path.join(tmp_root, "real_target_elsewhere.db")
            build_synthetic_mind_db(real_target_elsewhere)
            live_symlink_counterpart = os.path.join(simulated_live_dir3, "anaxi_mind_llama.db")
            try:
                os.symlink(real_target_elsewhere, live_symlink_counterpart)
                symlinked_counterpart_supported = True
            except (OSError, NotImplementedError):
                symlinked_counterpart_supported = False

            if symlinked_counterpart_supported:
                aliased_root2 = os.path.join(tmp_root, "rehearsal_aliased_via_live_symlink")
                mig.create_rehearsal_dir(aliased_root2, {"anaxi_log.jsonl": aliased_jsonl_src})
                aliased_child_path2 = os.path.join(aliased_root2, "anaxi_mind_llama.db")
                os.link(real_target_elsewhere, aliased_child_path2)
                check("assert_is_rehearsal_dir: a SYMLINKED live counterpart is NOT exempted from "
                      "comparison -- still catches a child hardlinked to the symlink's real target",
                      raises(lambda: mig.assert_is_rehearsal_dir(aliased_root2, live_dir=simulated_live_dir3),
                             (mig.NotARehearsalDirectoryError,)))
                shutil.rmtree(aliased_root2, ignore_errors=True)
            else:
                print("SKIP: symlinked-live-counterpart test -- symlink creation not permitted in this environment")

            # --- Fail-closed: if samefile() cannot establish
            # non-aliasing (raises for any reason), that must be
            # treated as "could not rule out aliasing" and refused --
            # never silently passed through as safe. ---
            failclosed_root = os.path.join(tmp_root, "rehearsal_failclosed")
            mig.create_rehearsal_dir(failclosed_root, {"anaxi_log.jsonl": aliased_jsonl_src})
            simulated_live_dir4 = os.path.join(tmp_root, "simulated_live_dir_failclosed")
            os.makedirs(simulated_live_dir4)
            # A like-named entry must exist in the simulated live dir for
            # the samefile() comparison to even be attempted.
            with open(os.path.join(simulated_live_dir4, "anaxi_log.jsonl"), "w", encoding="utf-8") as f:
                f.write("unrelated content\n")

            real_samefile = os.path.samefile

            def _raising_samefile(a, b):
                raise OSError("simulated: samefile() could not be evaluated")

            os.path.samefile = _raising_samefile
            try:
                check("assert_is_rehearsal_dir: FAILS CLOSED when samefile() itself raises "
                      "(inability to prove non-aliasing is treated as aliasing, not as safety)",
                      raises(lambda: mig.assert_is_rehearsal_dir(failclosed_root, live_dir=simulated_live_dir4),
                             (mig.NotARehearsalDirectoryError,)))
            finally:
                os.path.samefile = real_samefile
            shutil.rmtree(failclosed_root, ignore_errors=True)
        else:
            print("SKIP: hardlink tests -- hardlink creation not permitted in this environment")

        # --- Timezone-aware timestamp parsing ---
        # 12:00 UTC == 17:00 in a +05:00 offset (local = UTC + offset).
        ts_utc = mig.parse_iso_timestamp_to_unix("2026-01-01T12:00:00.000000+00:00")
        ts_offset = mig.parse_iso_timestamp_to_unix("2026-01-01T17:00:00.000000+05:00")
        check("parse_iso_timestamp_to_unix: two timestamps that are the SAME real UTC instant "
              "(12:00 UTC == 17:00+05:00) resolve to the identical unix timestamp",
              ts_utc == ts_offset)
        # The exact bug being fixed: the old mktime/strptime[:19] approach
        # discarded the offset entirely and applied the LOCAL machine's
        # timezone instead -- so a naive re-implementation of that bug
        # would very likely disagree with the correct value computed above.
        naive_wrong = int(time.mktime(time.strptime("2026-01-01T17:00:00"[:19], "%Y-%m-%dT%H:%M:%S")))
        check("parse_iso_timestamp_to_unix: correct result is NOT reproduced by the old "
              "offset-discarding mktime/strptime[:19] approach (unless local tz happens to be exactly +05:00)",
              ts_offset != naive_wrong or time.timezone == -5 * 3600)
        check("parse_iso_timestamp_to_unix: an offset-NAIVE timestamp is rejected outright, "
              "never silently interpreted as local time",
              raises(lambda: mig.parse_iso_timestamp_to_unix("2026-01-01T12:00:00.000000"),
                     (mig.MigrationStopCondition,)))

        # --- Synthetic manifest (never the real one) ---
        synthetic_manifest_path = os.path.join(tmp_root, "synthetic_manifest.json")
        import json as _json
        with open(synthetic_manifest_path, "w", encoding="utf-8") as f:
            _json.dump({"pipelines": {"llama": {"routing_constant_value": "synthetic_user"},
                                       "claude": {"routing_constant_value": "synthetic_user"}}}, f)

        provenance_path = os.path.join(rehearsal_dir, "anaxi_provenance.db")
        create_provenance_db(provenance_path)

        manifest = mig.load_migration_manifest(synthetic_manifest_path)
        pipeline_map = mig.build_pipeline_map(manifest)
        check("build_pipeline_map: llama and claude resolve to different opaque pipeline_ids",
              pipeline_map["llama"]["pipeline_id"] != pipeline_map["claude"]["pipeline_id"])
        check("build_pipeline_map: pipeline_id is opaque, not the pipeline_key itself",
              pipeline_map["llama"]["pipeline_id"] != pipeline_map["llama"]["pipeline_key"])

        prov_conn = sqlite3.connect(provenance_path)
        prov_conn.execute("PRAGMA foreign_keys = ON;")
        now = int(time.time())
        mig.seed_reference_data(prov_conn, pipeline_map, now)
        check("seed_reference_data: pipelines seeded", prov_conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0] == 2)
        check("seed_reference_data: authorization_scopes seeded (explicit staging, not silently absent)",
              prov_conn.execute("SELECT COUNT(*) FROM authorization_scopes").fetchone()[0] >= 1)
        seeded_pipelines_before = prov_conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
        mig.seed_reference_data(prov_conn, pipeline_map, now)  # idempotent re-seed
        check("seed_reference_data: re-running does not duplicate reference rows",
              prov_conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0] == seeded_pipelines_before)

        # --- Mismatch detection on an already-existing deterministic ID ---
        # actors.stable_key/actor_type are immutable by trigger, so a real
        # mismatch can only arise from genuine corruption -- simulate it on
        # a throwaway connection with foreign_keys/triggers still active by
        # constructing a SEPARATE row that collides on a hand-crafted ID,
        # proving the verification function itself actually compares fields
        # rather than merely checking existence.
        collision_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        collision_conn.execute("PRAGMA foreign_keys = ON;")
        fake_clark_id = derive_stable_id("actor", "clark")
        collision_conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'not-clark', ?)",
            (fake_clark_id, now),
        )
        collision_conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'not-clark')", (fake_clark_id,))
        collision_conn.commit()
        check("seed_reference_data: an existing row under the SAME deterministic actor_id but with "
              "DIFFERENT fields (simulated collision/corruption) raises MigrationStopCondition",
              raises(lambda: mig.seed_reference_data(collision_conn, pipeline_map, now), (mig.MigrationStopCondition,)))
        collision_conn.close()

        # Partial-initialization detection: an actors row present with no matching subtype linkage.
        partial_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        partial_conn.execute("PRAGMA foreign_keys = ON;")
        partial_conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
            (derive_stable_id("actor", "clark"), now),
        )
        partial_conn.commit()  # deliberately NOT inserting the actor_clark_agent linkage row
        check("seed_reference_data: an actors row present WITHOUT its subtype linkage row "
              "(partial initialization) raises MigrationStopCondition",
              raises(lambda: mig.seed_reference_data(partial_conn, pipeline_map, now), (mig.MigrationStopCondition,)))
        partial_conn.close()

        # Pipeline description mismatch: an existing pipelines row under
        # the SAME deterministic pipeline_id but a DIFFERENT description
        # than _pipeline_description() would derive -- an earlier version
        # verified pipeline_key/legacy_substrate_label only, leaving this
        # deterministic, seeder-supplied field unverified.
        pipeline_desc_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        pipeline_desc_conn.execute("PRAGMA foreign_keys = ON;")
        pipeline_desc_conn.execute(
            "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
            "VALUES (?, ?, ?, ?)",
            (pipeline_map["llama"]["pipeline_id"], pipeline_map["llama"]["pipeline_key"], "llama",
             "a hand-written description that does not match the deterministic derivation"),
        )
        pipeline_desc_conn.commit()
        check("seed_reference_data: an existing pipelines row under the SAME deterministic "
              "pipeline_id but a DIFFERENT description raises MigrationStopCondition",
              raises(lambda: mig.seed_reference_data(pipeline_desc_conn, {"llama": pipeline_map["llama"]}, now),
                     (mig.MigrationStopCondition,)))
        pipeline_desc_conn.close()

        # Clark subtype canonical_key mismatch: actors row correct, but
        # its actor_clark_agent linkage row carries the WRONG
        # canonical_key -- an earlier version checked only that the
        # linkage row existed, not that its value was correct.
        canonical_key_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        canonical_key_conn.execute("PRAGMA foreign_keys = ON;")
        real_clark_id = derive_stable_id("actor", "clark")
        canonical_key_conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
            (real_clark_id, now),
        )
        canonical_key_conn.execute(
            "INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'not-clark')", (real_clark_id,)
        )
        canonical_key_conn.commit()
        check("seed_reference_data: an actor_clark_agent linkage row present under the WRONG "
              "canonical_key raises MigrationStopCondition",
              raises(lambda: mig.seed_reference_data(canonical_key_conn, pipeline_map, now),
                     (mig.MigrationStopCondition,)))
        canonical_key_conn.close()

        # Same verify-or-stop discipline for model_revisions.
        model_collision_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        model_collision_conn.execute("PRAGMA foreign_keys = ON;")
        collided_model_id = derive_stable_id("modelrev", "synthetic-tag", "tag_only_degraded")
        model_collision_conn.execute(
            "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
            "VALUES (?, 'a-different-tag', 'tag_only_degraded', ?)",
            (collided_model_id, now),
        )
        model_collision_conn.commit()
        check("resolve_or_create_model_revision: an existing row under the same deterministic ID but "
              "a DIFFERENT tag raises MigrationStopCondition",
              raises(lambda: mig.resolve_or_create_model_revision(model_collision_conn, "synthetic-tag", now),
                     (mig.MigrationStopCondition,)))
        model_collision_conn.close()

        # --- Real migration against synthetic JSONL ---
        counts = mig.migrate_jsonl(prov_conn, os.path.join(rehearsal_dir, "anaxi_log.jsonl"), pipeline_map)
        check("migrate_jsonl: all 3 synthetic lines processed and inserted",
              counts["processed"] == 3 and counts["inserted"] == 3 and counts["skipped_already_migrated"] == 0)

        counts2 = mig.migrate_jsonl(prov_conn, os.path.join(rehearsal_dir, "anaxi_log.jsonl"), pipeline_map)
        check("migrate_jsonl: idempotent re-run skips all 3, inserts 0",
              counts2["inserted"] == 0 and counts2["skipped_already_migrated"] == 3)

        claude_events = prov_conn.execute(
            "SELECT pipeline_id FROM events e JOIN legacy_routing_provenance lrp ON lrp.event_id = e.event_id "
            "WHERE lrp.source_code_reference = 'claude_anaxi.py:USER_ID'"
        ).fetchall()
        check("migrate_jsonl: the synthetic claude row routes to the claude pipeline_id",
              len(claude_events) == 1 and claude_events[0][0] == pipeline_map["claude"]["pipeline_id"])

        # --- verify_historical_bundle_contract: exact field/cardinality
        # checks beyond the older verify_event_bundle_complete primitive.
        first_line = SYNTHETIC_JSONL_LINES[0]
        first_entry = _json.loads(first_line)
        first_mapping = pipeline_map[first_entry["substrate"]]
        first_event_id = mig.derive_historical_event_id("anaxi_log.jsonl", first_line.encode("utf-8"), 0)

        check("verify_historical_bundle_contract: the real migrated event resolves True "
              "against its own unmodified entry",
              mig.verify_historical_bundle_contract(prov_conn, first_event_id, first_entry, first_mapping))

        mutated_response_entry = dict(first_entry)
        mutated_response_entry["response"] = "a different response than what was actually migrated"
        check("verify_historical_bundle_contract: a MUTATED entry response text (and thus "
              "content_sha256) raises MigrationStopCondition rather than resolving False/True",
              raises(lambda: mig.verify_historical_bundle_contract(
                  prov_conn, first_event_id, mutated_response_entry, first_mapping),
                  (mig.MigrationStopCondition,)))

        mutated_model_entry = dict(first_entry)
        mutated_model_entry["model"] = "a-completely-different-model-tag"
        check("verify_historical_bundle_contract: a MUTATED entry model tag (wrong derived "
              "model_revision_id) raises MigrationStopCondition",
              raises(lambda: mig.verify_historical_bundle_contract(
                  prov_conn, first_event_id, mutated_model_entry, first_mapping),
                  (mig.MigrationStopCondition,)))

        # Cardinality violation: a second event_components row for the
        # SAME already-migrated event_id (schema-legal INSERT -- only
        # UPDATE/DELETE are trigger-forbidden -- but violates the
        # historical single-component contract) must be caught as
        # "exactly one" rather than merely "at least one".
        prov_conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256, model_revision_id) "
            "VALUES (?, 1, NULL, 'assembled_reply_unsplit_historical', 'unresolved_mixed_historical', "
            "'extra spurious component', ?, NULL)",
            (first_event_id, hashlib.sha256(b"extra spurious component").hexdigest()),
        )
        prov_conn.commit()
        check("verify_historical_bundle_contract: a SECOND event_components row for the same "
              "event_id raises MigrationStopCondition (exactly one, not merely at least one)",
              raises(lambda: mig.verify_historical_bundle_contract(
                  prov_conn, first_event_id, first_entry, first_mapping),
                  (mig.MigrationStopCondition,)))

        # --- Direct corruption tests: an otherwise-fully-correct bundle
        # (never a bundle built by inserting-then-corrupting, since
        # every table here is append-only) with exactly ONE field wrong.
        # Built on fresh, isolated connections so they cannot be
        # confused with prov_conn's now-deliberately-contaminated state
        # above. In both cases the corrupted field must cause
        # MigrationStopCondition -- i.e. migrate_jsonl's resume gate
        # must refuse to classify the row as already-migrated, not
        # silently accept it.
        def _build_otherwise_correct_bundle(conn, entry, mapping, event_id, *,
                                              migration_basis=None, model_revision_tag=None):
            migration_basis = mig.HISTORICAL_MIGRATION_BASIS if migration_basis is None else migration_basis
            occurred_at = mig.parse_iso_timestamp_to_unix(entry["timestamp"])
            now_ts = int(time.time())
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, 'waking_turn', ?, 'known', NULL, NULL, ?, ?, ?)",
                (event_id, mapping["pipeline_id"], event_id, occurred_at, now_ts),
            )
            conn.execute(
                "INSERT INTO event_migration_status (event_id, migration_status, migrated_at, migration_basis) "
                "VALUES (?, 'pre_authentication_layer', ?, ?)",
                (event_id, now_ts, migration_basis),
            )
            legacy_routing_id = mig.derive_stable_id("legrt", event_id)
            conn.execute(
                "INSERT INTO legacy_routing_provenance (legacy_routing_id, event_id, routing_constant_value, "
                "routing_mechanism, source_code_reference, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (legacy_routing_id, event_id, mapping["routing_constant_value"],
                 "hardcoded_module_constant", mapping["routing_source"], now_ts),
            )
            model_revision_id = mig.derive_stable_id("modelrev", entry["model"], "tag_only_degraded")
            actual_tag = entry["model"] if model_revision_tag is None else model_revision_tag
            conn.execute(
                "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
                "VALUES (?, ?, 'tag_only_degraded', ?)",
                (model_revision_id, actual_tag, occurred_at),
            )
            conn.execute(
                "INSERT INTO event_model_participation (event_id, model_revision_id, participation_note) "
                "VALUES (?, ?, ?)",
                (event_id, model_revision_id, mig.HISTORICAL_PARTICIPATION_NOTE),
            )
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id) "
                "VALUES (?, 0, NULL, 'assembled_reply_unsplit_historical', 'unresolved_mixed_historical', ?, ?, NULL)",
                (event_id, entry["response"], hashlib.sha256(entry["response"].encode("utf-8")).hexdigest()),
            )
            conn.commit()

        corrupt_basis_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        corrupt_basis_conn.execute("PRAGMA foreign_keys = ON;")
        mig.seed_reference_data(corrupt_basis_conn, {"llama": pipeline_map["llama"]}, now)
        corrupt_basis_entry = _json.loads(SYNTHETIC_JSONL_LINES[0])
        corrupt_basis_event_id = mig.derive_historical_event_id(
            "anaxi_log.jsonl", SYNTHETIC_JSONL_LINES[0].encode("utf-8"), 0)
        _build_otherwise_correct_bundle(
            corrupt_basis_conn, corrupt_basis_entry, pipeline_map["llama"], corrupt_basis_event_id,
            migration_basis="a different, wrong rationale string -- not what migrate_jsonl actually writes")
        check("verify_historical_bundle_contract: an otherwise-correct bundle with the WRONG "
              "event_migration_status.migration_basis raises MigrationStopCondition",
              raises(lambda: mig.verify_historical_bundle_contract(
                  corrupt_basis_conn, corrupt_basis_event_id, corrupt_basis_entry, pipeline_map["llama"]),
                  (mig.MigrationStopCondition,)))
        # And through the actual gate migrate_jsonl uses to decide
        # skip-vs-insert: it must NOT be silently classified as
        # already-migrated.
        corrupt_basis_jsonl = os.path.join(tmp_root, "corrupt_basis.jsonl")
        with open(corrupt_basis_jsonl, "w", encoding="utf-8") as f:
            f.write(SYNTHETIC_JSONL_LINES[0] + "\n")
        check("migrate_jsonl: refuses to classify a WRONG-migration_basis bundle as "
              "already-migrated (raises rather than silently skipping)",
              raises(lambda: mig.migrate_jsonl(corrupt_basis_conn, corrupt_basis_jsonl, {"llama": pipeline_map["llama"]}),
                     (mig.MigrationStopCondition,)))
        corrupt_basis_conn.close()

        corrupt_tag_conn = create_provenance_db(tempfile.mktemp(suffix=".db"))
        corrupt_tag_conn.execute("PRAGMA foreign_keys = ON;")
        mig.seed_reference_data(corrupt_tag_conn, {"llama": pipeline_map["llama"]}, now)
        corrupt_tag_entry = _json.loads(SYNTHETIC_JSONL_LINES[0])
        corrupt_tag_event_id = mig.derive_historical_event_id(
            "anaxi_log.jsonl", SYNTHETIC_JSONL_LINES[0].encode("utf-8"), 0)
        # model_revision_id is the CORRECT deterministic ID derived from
        # entry["model"] -- only the referenced row's own tag column is
        # wrong, proving the FK-ID match alone is not trusted.
        _build_otherwise_correct_bundle(
            corrupt_tag_conn, corrupt_tag_entry, pipeline_map["llama"], corrupt_tag_event_id,
            model_revision_tag="a-completely-different-tag-under-the-same-deterministic-id")
        check("verify_historical_bundle_contract: the correct deterministic model_revision_id "
              "pointing at a row with the WRONG tag raises MigrationStopCondition",
              raises(lambda: mig.verify_historical_bundle_contract(
                  corrupt_tag_conn, corrupt_tag_event_id, corrupt_tag_entry, pipeline_map["llama"]),
                  (mig.MigrationStopCondition,)))
        corrupt_tag_jsonl = os.path.join(tmp_root, "corrupt_tag.jsonl")
        with open(corrupt_tag_jsonl, "w", encoding="utf-8") as f:
            f.write(SYNTHETIC_JSONL_LINES[0] + "\n")
        check("migrate_jsonl: refuses to classify a WRONG-model-revision-tag bundle as "
              "already-migrated (raises rather than silently skipping)",
              raises(lambda: mig.migrate_jsonl(corrupt_tag_conn, corrupt_tag_jsonl, {"llama": pipeline_map["llama"]}),
                     (mig.MigrationStopCondition,)))
        corrupt_tag_conn.close()

        # --- Resume validation proves the COMPLETE bundle, not just the events row ---
        sample_event_id = derive_historical_event_id("anaxi_log.jsonl", SYNTHETIC_JSONL_LINES[0].encode("utf-8"), 0)
        check("verify_event_bundle_complete: a real migrated event resolves True",
              mig.verify_event_bundle_complete(prov_conn, sample_event_id) is True)
        check("verify_event_bundle_complete: a nonexistent event resolves False",
              mig.verify_event_bundle_complete(prov_conn, "hist-nonexistent") is False)
        # event_components is append-only (no DELETE permitted, correctly
        # enforced by the frozen schema) -- so a partial-bundle scenario
        # can't be constructed by deleting from an already-complete one.
        # Instead, build a genuinely partial bundle from scratch on a
        # fresh event_id: insert only events + event_migration_status,
        # deliberately skip the other three tables.
        partial_event_id = "hist-partial-test-0000000000"
        prov_conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "occurred_at, record_created_at) VALUES (?, 'waking_turn', ?, 'known', 0, 0)",
            (partial_event_id, pipeline_map["llama"]["pipeline_id"]),
        )
        prov_conn.execute(
            "INSERT INTO event_migration_status (event_id, migration_status, migrated_at, migration_basis) "
            "VALUES (?, 'pre_authentication_layer', 0, 'test fixture')",
            (partial_event_id,),
        )
        prov_conn.commit()
        check("verify_event_bundle_complete: a genuinely PARTIAL bundle raises IncompleteEventBundleError",
              raises(lambda: mig.verify_event_bundle_complete(prov_conn, partial_event_id),
                     (mig.IncompleteEventBundleError,)))

        # --- A "complete" bundle (all 5 rows present) whose CONTENT disagrees
        # with the source row being processed must still be a stop condition,
        # not silently accepted as already-migrated. ---
        check("verify_event_bundle_complete: complete bundle with a MISMATCHED expected content_sha256 "
              "raises MigrationStopCondition (not silently treated as already-migrated)",
              raises(lambda: mig.verify_event_bundle_complete(
                  prov_conn, sample_event_id,
                  expected={"content_sha256": "0000000000000000000000000000000000000000000000000000000000000000"}),
                  (mig.MigrationStopCondition,)))
        check("verify_event_bundle_complete: complete bundle with a MISMATCHED expected pipeline_id "
              "raises MigrationStopCondition",
              raises(lambda: mig.verify_event_bundle_complete(
                  prov_conn, sample_event_id, expected={"pipeline_id": "some-other-pipeline-id"}),
                  (mig.MigrationStopCondition,)))
        check("verify_event_bundle_complete: complete bundle with matching expected fields resolves True",
              mig.verify_event_bundle_complete(
                  prov_conn, sample_event_id,
                  expected={"pipeline_id": pipeline_map["llama"]["pipeline_id"]}) is True)

        # --- Schema alteration is separate from backfill, and covers every required table ---
        rel_conn = sqlite3.connect(os.path.join(rehearsal_dir, "anaxi_relational_llama.db"))
        mind_conn = sqlite3.connect(os.path.join(rehearsal_dir, "anaxi_mind_llama.db"))
        alter_report = mig.alter_existing_stores_schema(rel_conn, mind_conn)
        check("alter_existing_stores_schema: covers every required table (relational_events, "
              "turn_generation_log, nodes, active_kardia, kardia_history, proposals)",
              set(alter_report.keys()) == {"relational_events", "turn_generation_log", "nodes",
                                            "active_kardia", "kardia_history", "proposals"})
        rel_row = rel_conn.execute("SELECT pipeline_id FROM relational_events LIMIT 1").fetchone()
        check("alter_existing_stores_schema: adds columns but writes NO values (still NULL immediately after)",
              rel_row is not None and rel_row[0] is None)

        # --- Backfill is NULL-only and conflict-detecting ---
        rel_report = mig.backfill_relational_events(rel_conn, pipeline_map)
        check("backfill_relational_events: all 3 synthetic rows updated", rel_report["updated"] == 3)
        rel_report2 = mig.backfill_relational_events(rel_conn, pipeline_map)
        check("backfill_relational_events: idempotent re-run updates 0, reports 3 already_set",
              rel_report2["updated"] == 0 and rel_report2["already_set"] == 3)

        # Conflict detection: manually poison one row with a WRONG existing pipeline_id, confirm it stops.
        rel_conn.execute("UPDATE relational_events SET pipeline_id = 'some-other-pipeline' WHERE id = "
                          "(SELECT id FROM relational_events LIMIT 1)")
        rel_conn.commit()
        check("backfill_relational_events: conflicting pre-existing non-NULL pipeline_id raises MigrationStopCondition",
              raises(lambda: mig.backfill_relational_events(rel_conn, pipeline_map), (mig.MigrationStopCondition,)))

        tgl_report = mig.backfill_turn_generation_log(mind_conn, pipeline_map)
        check("backfill_turn_generation_log: all 2 synthetic rows updated", tgl_report["updated"] == 2)
        tgl_report2 = mig.backfill_turn_generation_log(mind_conn, pipeline_map)
        check("backfill_turn_generation_log: idempotent re-run updates 0",
              tgl_report2["updated"] == 0 and tgl_report2["already_set"] == 2)
        rel_conn.close()
        mind_conn.close()

        # --- Pipeline-aware legacy shim (real functions, not test-local reimplementation) ---
        check("matches_legacy_pipeline: claude entry matches claude pipeline_key",
              mig.matches_legacy_pipeline(prov_conn, {"substrate": "claude"}, pipeline_map["claude"]["pipeline_key"]) is True)
        check("matches_legacy_pipeline: claude entry does NOT match llama pipeline_key",
              mig.matches_legacy_pipeline(prov_conn, {"substrate": "claude"}, pipeline_map["llama"]["pipeline_key"]) is False)
        check("matches_legacy_pipeline: llama entry matches llama pipeline_key",
              mig.matches_legacy_pipeline(prov_conn, {"substrate": "llama"}, pipeline_map["llama"]["pipeline_key"]) is True)

        # --- Native (pipeline_id-bearing) branch, both pipelines --
        # the three checks above only exercise the historical
        # substrate-fallback branch; a native entry with a real
        # pipeline_id must also positively match its own pipeline's
        # key via the OTHER branch of the same function. llama's
        # pipeline_id branch was already exercised via direct
        # end-to-end verification of llama_sleep.py's wiring in an
        # earlier round; this adds the missing Claude-side case,
        # completing the branch matrix for this function without
        # implementing any native Claude writer.
        check("matches_legacy_pipeline: an entry carrying the CLAUDE pipeline's own pipeline_id "
              "positively matches the claude pipeline_key (native/pipeline_id branch, not the "
              "substrate fallback)",
              mig.matches_legacy_pipeline(
                  prov_conn, {"pipeline_id": pipeline_map["claude"]["pipeline_id"]},
                  pipeline_map["claude"]["pipeline_key"]) is True)
        check("matches_legacy_pipeline: an entry carrying the CLAUDE pipeline's own pipeline_id "
              "does NOT match the llama pipeline_key",
              mig.matches_legacy_pipeline(
                  prov_conn, {"pipeline_id": pipeline_map["claude"]["pipeline_id"]},
                  pipeline_map["llama"]["pipeline_key"]) is False)

        prov_conn.close()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def run_end_to_end_orchestration_test():
    """Calls the REAL orchestration entry point, mig.main(), rather than
    only its constituent helpers -- proving the actual wiring works end
    to end, including its own internal ordering (seed -> migrate ->
    alter -> backfill), not merely that each piece works in isolation
    when called directly by a test."""
    tmp_root = tempfile.mkdtemp(prefix="anaxi_e2e_test_")
    try:
        rehearsal_dir = os.path.join(tmp_root, "rehearsal")
        jsonl_src = os.path.join(tmp_root, "src_log.jsonl")
        with open(jsonl_src, "w", encoding="utf-8") as f:
            f.write("\n".join(SYNTHETIC_JSONL_LINES) + "\n")
        relational_src = os.path.join(tmp_root, "src_rel.db")
        build_synthetic_relational_db(relational_src)
        mind_src = os.path.join(tmp_root, "src_mind.db")
        build_synthetic_mind_db(mind_src)

        mig.create_rehearsal_dir(rehearsal_dir, {
            "anaxi_log.jsonl": jsonl_src,
            "anaxi_relational_llama.db": relational_src,
            "anaxi_mind_llama.db": mind_src,
        })

        import json as _json
        manifest_path = os.path.join(tmp_root, "synth_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            _json.dump({"pipelines": {"llama": {"routing_constant_value": "synthetic_user_e2e"},
                                       "claude": {"routing_constant_value": "synthetic_user_e2e"}}}, f)

        provenance_path = os.path.join(rehearsal_dir, "anaxi_provenance.db")
        create_provenance_db(provenance_path)

        # THE REAL end-to-end orchestration entry point.
        mig.main(rehearsal_dir, manifest_path=manifest_path)

        prov = sqlite3.connect(provenance_path)
        check("End-to-end main(): all 3 synthetic events present",
              prov.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3)
        check("End-to-end main(): reference data (pipelines, scopes) seeded",
              prov.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0] == 2
              and prov.execute("SELECT COUNT(*) FROM authorization_scopes").fetchone()[0] >= 1)
        prov.close()

        rel = sqlite3.connect(os.path.join(rehearsal_dir, "anaxi_relational_llama.db"))
        check("End-to-end main(): relational_events backfilled via the real orchestration",
              rel.execute("SELECT COUNT(*) FROM relational_events WHERE pipeline_id IS NOT NULL").fetchone()[0] == 3)
        rel.close()

        mind = sqlite3.connect(os.path.join(rehearsal_dir, "anaxi_mind_llama.db"))
        check("End-to-end main(): turn_generation_log backfilled via the real orchestration",
              mind.execute("SELECT COUNT(*) FROM turn_generation_log WHERE pipeline_id IS NOT NULL").fetchone()[0] == 2)
        cols = {r[1] for r in mind.execute("PRAGMA table_info(nodes)").fetchall()}
        check("End-to-end main(): nodes additive columns present via the real orchestration",
              {"pipeline_id", "epoch_id", "asserting_event_id", "grounding_assertion_id"} <= cols)
        mind.close()

        # Re-running the FULL orchestration end to end must be idempotent.
        mig.main(rehearsal_dir, manifest_path=manifest_path)
        prov2 = sqlite3.connect(provenance_path)
        check("End-to-end main(): re-running the full orchestration is idempotent (no crash, no duplication)",
              prov2.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3)
        prov2.close()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main():
    print("=== Suite 1: schema checks ===")
    run_schema_checks()
    print("\n=== Suite 2: migration integration (synthetic fixtures) ===")
    run_migration_integration_suite()
    print("\n=== Suite 3: end-to-end orchestration (real main()) ===")
    run_end_to_end_orchestration_test()
    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
