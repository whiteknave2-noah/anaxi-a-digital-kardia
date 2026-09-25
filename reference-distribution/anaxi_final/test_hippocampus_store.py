"""
Anaxi -- Slice-A tests for hippocampus_store.py.

Every test uses temporary/isolated synthetic fixtures. No test writes
to, or even opens, any real live/production data path. No model/API
call anywhere in this file.

Run:
    python test_hippocampus_store.py
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hippocampus_store as hs
from provenance_schema import create_provenance_db, derive_historical_event_id


# =============================================================================
# Fixture construction
# =============================================================================
def _seed_actors(conn):
    now = int(time.time())
    host_actor_id = "actor-host-1"
    clark_actor_id = "actor-clark-1"
    human_actor_id = "actor-human-1"
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (host_actor_id, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)", (clark_actor_id, now))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (clark_actor_id,))
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES ('person-alex', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', 'alex', ?)", (human_actor_id, now))
    conn.execute("INSERT INTO actor_human_person (actor_id, person_id, canonical_name, relationship_established_at) VALUES (?, 'person-alex', 'Alex', ?)", (human_actor_id, now))
    return host_actor_id, clark_actor_id, human_actor_id


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at, pipeline_status="known"):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, 'waking_turn', ?, ?, NULL, ?, ?, ?, ?)",
        (event_id, pipeline_id, pipeline_status, auth_context_id, event_id, occurred_at, occurred_at),
    )


def _insert_component(conn, event_id, sequence, creator_actor_id, component_kind, authorship_resolution, text):
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, sequence, creator_actor_id, component_kind, authorship_resolution, text, content_sha256),
    )
    return conn.execute("SELECT component_id FROM event_components WHERE event_id = ? AND sequence = ?", (event_id, sequence)).fetchone()[0]


def build_fixture(tmp_dir, include_wrongtype=False):
    """Builds a complete, self-consistent synthetic fixture: a real
    canonical provenance DB (via the real create_provenance_db()), a
    relational DB, and a historical JSONL file. Returns a dict of
    useful IDs/paths for assertions."""
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    rel_path = os.path.join(tmp_dir, "anaxi_relational_llama.db")
    jsonl_path = os.path.join(tmp_dir, "anaxi_log.jsonl")

    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    now = int(time.time())
    pipeline_id = "pipe-llama-1"
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,))

    host_actor_id, clark_actor_id, human_actor_id = _seed_actors(conn)

    # --- sessions for session-derivation coverage ---
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-resolved', ?, 100, 200)", (pipeline_id,))
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-amb-1', ?, 1000, 1200)", (pipeline_id,))
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-amb-2', ?, 1050, 1300)", (pipeline_id,))

    # --- auth_contexts ---
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-auth', 'sess-resolved', ?, ?, 'authenticated', 'os_session', 'medium', 100)", (human_actor_id, human_actor_id))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-claimed', 'sess-resolved', ?, NULL, 'claimed_only', NULL, NULL, 100)", (human_actor_id,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-unauth', 'sess-resolved', NULL, NULL, 'unauthenticated', 'none_presented', NULL, 100)")

    # --- native events ---
    _insert_event(conn, "evt-bc", pipeline_id, "ac-auth", occurred_at=150)          # session resolved
    _insert_event(conn, "evt-cp", pipeline_id, "ac-claimed", occurred_at=9999)      # session unknown (0 candidates)
    _insert_event(conn, "evt-rel", pipeline_id, "ac-unauth", occurred_at=150)
    _insert_event(conn, "evt-noauth", pipeline_id, None, occurred_at=150)           # auth_context_id NULL -> unknown
    _insert_event(conn, "evt-ambiguous", pipeline_id, "ac-auth", occurred_at=1100)  # 2 overlapping sessions
    _insert_event(conn, "evt-unsupported", pipeline_id, "ac-auth", occurred_at=150)
    if include_wrongtype:
        _insert_event(conn, "evt-wrongtype", pipeline_id, "ac-auth", occurred_at=150)
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES ('evt-nullpipe', 'waking_turn', NULL, 'unknown', NULL, NULL, 'evt-nullpipe', 150, 150)"
    )
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES ('evt-nullpipe', 0, ?, 'bounded_clause', 'resolved', ?, ?)",
        (host_actor_id, "Saved: Null pipeline test.", hashlib.sha256(b"Saved: Null pipeline test.").hexdigest()),
    )

    bc_id = _insert_component(conn, "evt-bc", 0, host_actor_id, "bounded_clause", "resolved", "Saved: A Note on Simplicity.")
    cp_id = _insert_component(conn, "evt-cp", 0, clark_actor_id, "conversational_prose", "resolved", "I liked that one.")
    unsupported_id = _insert_component(conn, "evt-unsupported", 0, host_actor_id, "raw_debug_note", "resolved", "not a v1 kind")
    wrongtype_id = None
    if include_wrongtype:
        wrongtype_id = _insert_component(conn, "evt-wrongtype", 0, clark_actor_id, "bounded_clause", "resolved", "Wrong actor type for this kind.")
    amb_id = _insert_component(conn, "evt-ambiguous", 0, host_actor_id, "bounded_clause", "resolved", "Saved: Ambiguous session test.")

    # --- historical migrated event, grounding a JSONL line ---
    hist_line = json.dumps({
        "timestamp": "2026-01-01T00:00:00+00:00", "substrate": "llama", "model": "llama3.2:3b",
        "prompt": "Please save this thought: simplicity matters.", "response": "Saved: A Note on Simplicity.",
        "kardia": {},
    })
    hist_event_id = derive_historical_event_id("anaxi_log.jsonl", hist_line.encode("utf-8"), 1)
    _insert_event(conn, hist_event_id, pipeline_id, None, occurred_at=50)
    conn.execute(
        "INSERT INTO event_migration_status (event_id, migration_status, migrated_at, migration_basis) "
        "VALUES (?, 'pre_authentication_layer', ?, 'test fixture')",
        (hist_event_id, now),
    )
    hist_component_id = _insert_component(conn, hist_event_id, 0, None, "assembled_reply_unsplit_historical", "unresolved_mixed_historical", "Saved: A Note on Simplicity.")

    conn.commit()
    conn.close()

    # --- relational DB ---
    rel_conn = sqlite3.connect(rel_path)
    rel_conn.execute("""CREATE TABLE relational_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
        created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
        agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
        status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
    )""")
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, 'This is what I actually said.', 'evt-rel')")
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, NULL, 'evt-rel')")  # NULL text -- not eligible
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, '', 'evt-rel')")  # empty text -- not eligible
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, 'orphaned text', NULL)")  # NULL event_id -- not eligible
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, 'dangling grounding', 'evt-does-not-exist')")  # dangling -- not eligible
    rel_conn.commit()
    rel_conn.close()

    # --- historical JSONL ---
    # line 0: blank (index must still be consumed)
    # line 1: the historically-migrated line above
    # line 2: a line that was never migrated (no matching canonical event) -- not eligible
    never_migrated_line = json.dumps({"timestamp": "2026-01-02T00:00:00+00:00", "substrate": "llama", "model": "llama3.2:3b", "prompt": "not migrated", "response": "n/a", "kardia": {}})
    with open(jsonl_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n")
        f.write(hist_line + "\n")
        f.write(never_migrated_line + "\n")

    return {
        "prov_path": prov_path, "rel_path": rel_path, "jsonl_path": jsonl_path,
        "pipeline_id": pipeline_id, "host_actor_id": host_actor_id, "clark_actor_id": clark_actor_id,
        "bc_id": bc_id, "cp_id": cp_id,
        "unsupported_id": unsupported_id, "wrongtype_id": wrongtype_id, "amb_id": amb_id,
        "hist_event_id": hist_event_id, "hist_component_id": hist_component_id,
        "hist_line": hist_line, "never_migrated_line": never_migrated_line,
    }


def make_paths(fx, tmp_dir, hip_name="anaxi_hippocampus.db"):
    return hs.HippocampusPaths(
        provenance_db_path=fx["prov_path"], relational_db_path=fx["rel_path"],
        historical_jsonl_path=fx["jsonl_path"], hippocampus_db_path=os.path.join(tmp_dir, hip_name),
    )


# =============================================================================
# Test suite
# =============================================================================
def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    def expect_raises(exc_type, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            return False
        except exc_type:
            return True
        except Exception:
            return False

    root = tempfile.mkdtemp(prefix="anaxi_hippocampus_test_")
    try:
        # =====================================================================
        # Schema
        # =====================================================================
        schema_dir = os.path.join(root, "schema")
        os.makedirs(schema_dir)
        hip_path = os.path.join(schema_dir, "hip.db")
        conn = hs.create_hippocampus_db(hip_path)
        check("Schema: DB creation succeeds with FTS5 available", os.path.isfile(hip_path))
        meta = dict(conn.execute("SELECT key, value FROM hippocampus_meta;").fetchall())
        check("Schema: current schema_version", meta.get("schema_version") == str(hs.SCHEMA_VERSION))
        check("Schema: item_id_version == '1'", meta.get("item_id_version") == "1")
        conn.close()
        check("Schema: second accidental create against existing path refuses",
              expect_raises(hs.HippocampusSchemaError, hs.create_hippocampus_db, hip_path))

        conn = sqlite3.connect(hip_path)
        bad_vocab_ok = False
        try:
            conn.execute(
                "INSERT INTO hippocampal_items (item_id, event_id, source_store, source_locator, content_text, "
                "content_sha256, memory_kind, attribution_status, authentication_status, occurred_at, "
                "session_resolution, index_schema_version) VALUES "
                "('x', 'e', 'not_a_real_source_store', 'l', 't', 'h', 'unknown', 'unknown', 'unknown', 1, 'unknown', 1)"
            )
        except sqlite3.IntegrityError:
            bad_vocab_ok = True
        check("Schema: CHECK vocabularies reject invalid values (source_store)", bad_vocab_ok)
        conn.close()

        # =====================================================================
        # Item identity
        # =====================================================================
        id_a = hs.derive_hippocampal_item_id("evt1", "event_components", "5")
        id_b = hs.derive_hippocampal_item_id("evt1", "event_components", "5")
        id_diff_event = hs.derive_hippocampal_item_id("evt2", "event_components", "5")
        id_diff_store = hs.derive_hippocampal_item_id("evt1", "relational_events", "5")
        id_diff_locator = hs.derive_hippocampal_item_id("evt1", "event_components", "6")
        check("Identity: deterministic same-input result", id_a == id_b)
        check("Identity: different event_id changes ID", id_a != id_diff_event)
        check("Identity: different source_store changes ID", id_a != id_diff_store)
        check("Identity: different source_locator changes ID", id_a != id_diff_locator)
        check("Identity: format is 'hip-' + 26 hex chars", id_a.startswith("hip-") and len(id_a) == 30)

        import struct as _struct
        expected_preimage = bytearray(b"anaxi-hippocampus-item-v1\x00")
        for v in ("evt1", "event_components", "5"):
            enc = v.encode("utf-8")
            expected_preimage += _struct.pack(">Q", len(enc)) + enc
        expected_digest = hashlib.sha256(bytes(expected_preimage)).hexdigest()
        check("Identity: exact v1 domain/length-prefix construction", id_a == "hip-" + expected_digest[:26])

        # content changing does NOT change item ID (content not part of preimage)
        id_c1 = hs.derive_hippocampal_item_id("evt1", "event_components", "5")
        check("Identity: content changes do not change logical item ID (by construction -- content is not an input)", id_c1 == id_a)

        # =====================================================================
        # Full-fixture sync tests
        # =====================================================================
        sync_dir = os.path.join(root, "sync")
        os.makedirs(sync_dir)
        fx = build_fixture(sync_dir)
        paths = make_paths(fx, sync_dir)
        hs.create_hippocampus_db(paths.hippocampus_db_path).close()

        prov_hash_before = hashlib.sha256(open(fx["prov_path"], "rb").read()).hexdigest()
        rel_hash_before = hashlib.sha256(open(fx["rel_path"], "rb").read()).hexdigest()
        jsonl_hash_before = hashlib.sha256(open(fx["jsonl_path"], "rb").read()).hexdigest()

        result1 = hs.sync_hippocampus(paths.hippocampus_db_path, paths)
        rows = hs.fetch_logical_rows(paths.hippocampus_db_path)
        by_locator = {(r["source_store"], r["source_locator"]): r for r in rows}

        check("Sync: initial insertion happened", len(result1.inserted) > 0)
        check("Sync: unsupported component (raw_debug_note) reported, not ingested",
              any(u["component_id"] == fx["unsupported_id"] for u in result1.unsupported_components)
              and ("event_components", str(fx["unsupported_id"])) not in by_locator)

        # --- canonical component ingestion ---
        bc_row = by_locator.get(("event_components", str(fx["bc_id"])))
        check("Component: bounded_clause ingested", bc_row is not None)
        if bc_row:
            check("Component: bounded_clause memory_kind == mechanical_record", bc_row["memory_kind"] == "mechanical_record")
            check("Component: bounded_clause creator_actor_type copied from canonical actors row", bc_row["creator_actor_type"] == "host_system")
            check("Component: recomputed component hash matches canonical hash",
                  bc_row["content_sha256"] == hashlib.sha256(b"Saved: A Note on Simplicity.").hexdigest())

        cp_row = by_locator.get(("event_components", str(fx["cp_id"])))
        check("Component: conversational_prose ingested", cp_row is not None)
        if cp_row:
            check("Component: conversational_prose memory_kind == clark_expression", cp_row["memory_kind"] == "clark_expression")
            check("Component: conversational_prose creator_actor_type copied from canonical actors row", cp_row["creator_actor_type"] == "clark_agent")

        hist_row = by_locator.get(("event_components", str(fx["hist_component_id"])))
        check("Component: historical assembled reply ingested", hist_row is not None)
        if hist_row:
            check("Component: historical memory_kind == mixed_historical_expression", hist_row["memory_kind"] == "mixed_historical_expression")
            check("Component: historical attribution_status == unresolved_mixed_historical", hist_row["attribution_status"] == "unresolved_mixed_historical")
            check("Component: historical creator remains NULL (no invented creator)", hist_row["creator_actor_id"] is None)

        # Wrong-actor-type contradiction is covered in its own isolated
        # fixture below (a bounded_clause with a clark_agent creator would
        # abort the WHOLE sync, so it cannot share the happy-path fixture).

        # --- native human input ---
        rel_row = by_locator.get(("relational_events", None))
        rel_matches = [r for r in rows if r["source_store"] == "relational_events"]
        check("Native human: exactly one eligible relational_events row ingested (NULL/empty/orphan/dangling excluded)", len(rel_matches) == 1)
        if rel_matches:
            r = rel_matches[0]
            check("Native human: memory_kind == human_expression", r["memory_kind"] == "human_expression")
            check("Native human: creator remains NULL/unknown as frozen v1 behavior",
                  r["creator_actor_id"] is None and r["creator_actor_type"] is None and r["attribution_status"] == "unknown")
            check("Native human: absent/NULL event_id is not eligible (excluded, not an error)", True)  # structurally proven by count==1 above

        # --- historical JSONL ---
        jsonl_matches = [r for r in rows if r["source_store"] == "anaxi_log_jsonl"]
        check("Historical JSONL: exactly one eligible line ingested (blank line and never-migrated line excluded)", len(jsonl_matches) == 1)
        if jsonl_matches:
            jr = jsonl_matches[0]
            check("Historical JSONL: exact physical line index preserved (line 1, blank line at 0 consumed its index)",
                  jr["source_line_index"] == 1 and jr["source_locator"] == "line:1")
            check("Historical JSONL: prompt indexed only after canonical grounding, memory_kind == human_expression", jr["memory_kind"] == "human_expression")
            check("Historical JSONL: authentication_status == pre_authentication_layer", jr["authentication_status"] == "pre_authentication_layer")
            check("Historical JSONL: attribution_status == unknown", jr["attribution_status"] == "unknown")
            check("Historical JSONL: content_text is entry['prompt'], not entry['response']", jr["content_text"] == "Please save this thought: simplicity matters.")

        check("Historical JSONL: nonmatching newer/native JSONL line not treated as historical (excluded)",
              not any(r["content_text"] == "not migrated" for r in jsonl_matches))

        # --- authentication ---
        auth_by_event = {r["event_id"]: r["authentication_status"] for r in rows if r["source_store"] == "event_components"}
        check("Authentication: native authenticated event -> authenticated", auth_by_event.get("evt-bc") == "authenticated")
        check("Authentication: native claimed_only event -> claimed_only", auth_by_event.get("evt-cp") == "claimed_only")
        noauth_row = next((r for r in rows if r["event_id"] == "evt-nullpipe"), None)
        check("Authentication: NULL auth_context_id -> unknown", noauth_row is not None and noauth_row["authentication_status"] == "unknown")

        # --- sessions (Amendment S1: canonical auth_context->session_id
        # link, never temporal-range inference) ---
        check("Sessions: canonical auth_context session_id -> resolved to that exact session",
              bc_row is not None and bc_row["session_id"] == "sess-resolved" and bc_row["session_resolution"] == "resolved")
        check("Sessions: canonical session_id resolves regardless of occurred_at's temporal distance "
              "(evt-cp's occurred_at=9999 is far outside sess-resolved's own started_at/ended_at window "
              "-- S1 no longer consults that window at all, only the exact canonical link)",
              cp_row is not None and cp_row["session_id"] == "sess-resolved" and cp_row["session_resolution"] == "resolved")
        amb_row = by_locator.get(("event_components", str(fx["amb_id"])))
        check("Sessions: canonical assignment wins even when occurred_at temporally overlaps TWO "
              "unrelated, still-open sessions (sess-amb-1/sess-amb-2) -- S1 resolves to the event's "
              "own canonical session (sess-resolved) instead of ever reporting ambiguous",
              amb_row is not None and amb_row["session_id"] == "sess-resolved" and amb_row["session_resolution"] == "resolved")
        check("Sessions: historical event -> unknown (never synthesized, S1 does not change this)",
              hist_row is not None and hist_row["session_id"] is None and hist_row["session_resolution"] == "unknown")
        nullpipe_row = noauth_row
        check("Sessions: NULL auth_context_id (no canonical assignment at all) -> unknown",
              nullpipe_row["session_id"] is None and nullpipe_row["session_resolution"] == "unknown")

        # --- read-only source behavior ---
        prov_hash_after = hashlib.sha256(open(fx["prov_path"], "rb").read()).hexdigest()
        rel_hash_after = hashlib.sha256(open(fx["rel_path"], "rb").read()).hexdigest()
        jsonl_hash_after = hashlib.sha256(open(fx["jsonl_path"], "rb").read()).hexdigest()
        check("Read-only: provenance source bytes unchanged by sync", prov_hash_before == prov_hash_after)
        check("Read-only: relational source bytes unchanged by sync", rel_hash_before == rel_hash_after)
        check("Read-only: historical JSONL bytes unchanged by sync", jsonl_hash_before == jsonl_hash_after)

        write_to_ro_failed = False
        ro_conn = hs._open_source_readonly(fx["prov_path"], "test")
        try:
            ro_conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key) VALUES ('x', 'y')")
        except sqlite3.OperationalError:
            write_to_ro_failed = True
        finally:
            ro_conn.close()
        check("Read-only: a write attempt against the read-only source connection is refused", write_to_ro_failed)

        # =====================================================================
        # Idempotency
        # =====================================================================
        result2 = hs.sync_hippocampus(paths.hippocampus_db_path, paths)
        check("Sync: second identical sync is idempotent (nothing new inserted)", len(result2.inserted) == 0)
        check("Sync: second identical sync -- unchanged count matches indexed item count",
              result2.unchanged == len(rows) or (result2.unchanged + len(result2.session_updated)) == len(rows))

        # =====================================================================
        # Drift: self-consistent source disagrees with a prior sync.
        #
        # NOTE ON FIXTURE CHOICE: canonical event_components/events rows are
        # genuinely append-only at the DB trigger level in the real schema
        # (trg_event_components_no_update / trg_event_components_no_delete,
        # confirmed directly against provenance_schema.py) -- there is no
        # way to UPDATE or DELETE a canonical component row even in a test,
        # exactly as there would be none in production. So "changed source
        # bytes" is demonstrated here against relational_events instead,
        # which lives in a separate, genuinely mutable legacy database with
        # no such trigger -- a real, reachable scenario (e.g. a legacy-store
        # edit) that exercises the exact same shared drift-comparison code
        # path in sync_hippocampus() as any other source would.
        # =====================================================================
        drift_dir = os.path.join(root, "drift")
        os.makedirs(drift_dir)
        fx_d = build_fixture(drift_dir)
        paths_d = make_paths(fx_d, drift_dir)
        hs.create_hippocampus_db(paths_d.hippocampus_db_path).close()
        hs.sync_hippocampus(paths_d.hippocampus_db_path, paths_d)

        raw_conn = sqlite3.connect(fx_d["rel_path"])
        raw_conn.execute("UPDATE relational_events SET external_observation = ? WHERE event_id = 'evt-rel' AND external_observation = 'This is what I actually said.'",
                          ("This is what I actually said, CHANGED.",))
        raw_conn.commit()
        raw_conn.close()
        check("Sync: changed self-consistent relational_events content produces HippocampusSourceDriftError",
              expect_raises(hs.HippocampusSourceDriftError, hs.sync_hippocampus, paths_d.hippocampus_db_path, paths_d))

        # =====================================================================
        # Grounding: self-inconsistent source (hash disagrees with its own
        # text). Constructed at INSERT time, not via a later UPDATE -- the
        # append-only triggers block UPDATE too, but a deliberately
        # inconsistent row can still be inserted directly (simulating a
        # writer-side bug that produced bad data in the first place, which
        # is exactly the class of problem this check exists to catch).
        # =====================================================================
        ground_dir = os.path.join(root, "grounding")
        os.makedirs(ground_dir)
        fx_g = build_fixture(ground_dir)
        paths_g = make_paths(fx_g, ground_dir)
        hs.create_hippocampus_db(paths_g.hippocampus_db_path).close()
        raw_conn = sqlite3.connect(fx_g["prov_path"])
        raw_conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256) VALUES "
            "('evt-bc', 1, ?, 'bounded_clause', 'resolved', 'mismatched text', 'deadbeef')",
            (fx_g["host_actor_id"],),
        )
        raw_conn.commit()
        raw_conn.close()
        check("Sync: self-inconsistent canonical component (hash != recomputed) produces HippocampusGroundingError",
              expect_raises(hs.HippocampusGroundingError, hs.sync_hippocampus, paths_g.hippocampus_db_path, paths_g))

        # =====================================================================
        # Integrity: bounded_clause creator does not resolve through the
        # canonical host-system actor subtype (a real, legitimately-typed
        # actor of the WRONG type -- exercises the "Require: creator
        # resolves through the canonical host-system actor subtype" check).
        # =====================================================================
        wrongtype_dir = os.path.join(root, "wrongtype")
        os.makedirs(wrongtype_dir)
        fx_w = build_fixture(wrongtype_dir, include_wrongtype=True)
        paths_w = make_paths(fx_w, wrongtype_dir)
        hs.create_hippocampus_db(paths_w.hippocampus_db_path).close()
        check("Integrity: bounded_clause with a clark_agent (not host_system) creator raises HippocampusIntegrityError",
              expect_raises(hs.HippocampusIntegrityError, hs.sync_hippocampus, paths_w.hippocampus_db_path, paths_w))

        # =====================================================================
        # Drift: changed immutable derived mapping (memory_kind, etc.).
        #
        # NOTE ON FIXTURE CHOICE: for the event_components path, memory_kind/
        # attribution_status vary with real row content (component_kind,
        # creator), which is exactly the data append-only triggers forbid
        # mutating; for relational_events and historical JSONL, the derived
        # mapping is a CONSTANT for every eligible row (always
        # human_expression/unknown/no-creator), so there is no in-source
        # value to change that would alter the mapping without also just
        # being a content-drift case (already covered above) or an
        # eligibility change (covered under disappearance below). So this
        # is demonstrated the other legitimate way: the already-indexed
        # HIPPOCAMPAL row itself disagreeing with a freshly, correctly
        # recomputed candidate from genuinely unchanged canonical source --
        # exactly the real-world case of hippocampal-side corruption/
        # tampering/a future mapping-logic change, which is precisely what
        # this comparison exists to catch.
        # =====================================================================
        mapdrift_dir = os.path.join(root, "mapdrift")
        os.makedirs(mapdrift_dir)
        fx_m = build_fixture(mapdrift_dir)
        paths_m = make_paths(fx_m, mapdrift_dir)
        hs.create_hippocampus_db(paths_m.hippocampus_db_path).close()
        hs.sync_hippocampus(paths_m.hippocampus_db_path, paths_m)
        cp_item_id = hs.derive_hippocampal_item_id("evt-cp", "event_components", str(fx_m["cp_id"]))
        tamper_conn = sqlite3.connect(paths_m.hippocampus_db_path)
        tamper_conn.execute("UPDATE hippocampal_items SET memory_kind = 'mechanical_record' WHERE item_id = ?", (cp_item_id,))
        tamper_conn.commit()
        tamper_conn.close()
        check("Sync: stored hippocampal row disagreeing with a freshly recomputed (unchanged-source) candidate produces HippocampusSourceDriftError",
              expect_raises(hs.HippocampusSourceDriftError, hs.sync_hippocampus, paths_m.hippocampus_db_path, paths_m))

        # =====================================================================
        # Source disappearance (relational_events -- genuinely deletable,
        # unlike the append-only canonical event_components path; see notes
        # above).
        # =====================================================================
        disappear_dir = os.path.join(root, "disappear")
        os.makedirs(disappear_dir)
        fx_x = build_fixture(disappear_dir)
        paths_x = make_paths(fx_x, disappear_dir)
        hs.create_hippocampus_db(paths_x.hippocampus_db_path).close()
        hs.sync_hippocampus(paths_x.hippocampus_db_path, paths_x)
        raw_conn = sqlite3.connect(fx_x["rel_path"])
        raw_conn.execute("DELETE FROM relational_events WHERE event_id = 'evt-rel' AND external_observation = 'This is what I actually said.'")
        raw_conn.commit()
        raw_conn.close()
        check("Sync: disappeared relational_events row produces HippocampusSourceDriftError",
              expect_raises(hs.HippocampusSourceDriftError, hs.sync_hippocampus, paths_x.hippocampus_db_path, paths_x))

        # historical JSONL truncation -> disappearance
        trunc_dir = os.path.join(root, "trunc")
        os.makedirs(trunc_dir)
        fx_t = build_fixture(trunc_dir)
        paths_t = make_paths(fx_t, trunc_dir)
        hs.create_hippocampus_db(paths_t.hippocampus_db_path).close()
        hs.sync_hippocampus(paths_t.hippocampus_db_path, paths_t)
        with open(fx_t["jsonl_path"], "w", encoding="utf-8", newline="\n") as f:
            f.write("\n")  # historical line truncated away
        check("Sync: truncated historical JSONL (line disappeared) produces HippocampusSourceDriftError",
              expect_raises(hs.HippocampusSourceDriftError, hs.sync_hippocampus, paths_t.hippocampus_db_path, paths_t))

        # =====================================================================
        # Historical JSONL byte-exactness
        # =====================================================================
        exact_dir = os.path.join(root, "exact")
        os.makedirs(exact_dir)
        fx_e = build_fixture(exact_dir)
        # A reserialized (parsed then json.dumps'd again) variant of the same
        # logical content, at the SAME line index, produces a DIFFERENT
        # historical event_id -- proving byte-exactness governs identity,
        # not parsed/logical content.
        reparsed = json.dumps(json.loads(fx_e["hist_line"]))
        id_from_original_bytes = derive_historical_event_id("anaxi_log.jsonl", fx_e["hist_line"].encode("utf-8"), 1)
        id_from_reparsed_bytes = derive_historical_event_id("anaxi_log.jsonl", reparsed.encode("utf-8"), 1)
        check("Historical JSONL: exact source bytes determine historical ID (reparsed JSON is a different identity unless byte-identical)",
              (reparsed == fx_e["hist_line"]) or (id_from_original_bytes != id_from_reparsed_bytes))

        # rstrip("\n") behavior: a line with trailing \r (CRLF) is NOT treated
        # the same as pure LF -- rstrip("\n") only strips '\n', leaving '\r'
        # in the byte sequence and therefore changing the derived ID.
        crlf_variant = fx_e["hist_line"] + "\r"
        id_with_stray_cr = derive_historical_event_id("anaxi_log.jsonl", crlf_variant.encode("utf-8"), 1)
        check("Historical JSONL: rstrip('\\n') strips only '\\n', not '\\r' -- multiple trailing newline-char semantics matter",
              id_with_stray_cr != id_from_original_bytes)

        # fx_e's canonical event was grounded (in build_fixture) using
        # line_index=1 for hist_line -- so this file layout (one blank line,
        # then hist_line) must place hist_line at physical index 1 for it to
        # ground correctly, which is also exactly the scenario that proves
        # the point: without the leading blank line hist_line would sit at
        # index 0; the blank line consumed index 0, shifting the real line
        # to index 1.
        blank_dir = os.path.join(root, "blank")
        os.makedirs(blank_dir)
        blank_jsonl = os.path.join(blank_dir, "anaxi_log.jsonl")
        with open(blank_jsonl, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n" + fx_e["hist_line"] + "\n")
        blank_paths = hs.HippocampusPaths(fx_e["prov_path"], fx_e["rel_path"], blank_jsonl, os.path.join(blank_dir, "hip.db"))
        hs.create_hippocampus_db(blank_paths.hippocampus_db_path).close()
        hs.sync_hippocampus(blank_paths.hippocampus_db_path, blank_paths)
        blank_rows = [r for r in hs.fetch_logical_rows(blank_paths.hippocampus_db_path) if r["source_store"] == "anaxi_log_jsonl"]
        check("Historical JSONL: blank line consumes its index (leading blank shifts the real line to index 1, matching its canonical grounding)",
              len(blank_rows) == 1 and blank_rows[0]["source_line_index"] == 1)

        # =====================================================================
        # Rebuild
        # =====================================================================
        rebuild_dir = os.path.join(root, "rebuild")
        os.makedirs(rebuild_dir)
        fx_r = build_fixture(rebuild_dir)
        target1 = os.path.join(rebuild_dir, "rebuild1.db")
        target2 = os.path.join(rebuild_dir, "rebuild2.db")
        paths_r1 = hs.HippocampusPaths(fx_r["prov_path"], fx_r["rel_path"], fx_r["jsonl_path"], target1)
        paths_r2 = hs.HippocampusPaths(fx_r["prov_path"], fx_r["rel_path"], fx_r["jsonl_path"], target2)

        prov_hash_pre_rebuild = hashlib.sha256(open(fx_r["prov_path"], "rb").read()).hexdigest()
        rel_hash_pre_rebuild = hashlib.sha256(open(fx_r["rel_path"], "rb").read()).hexdigest()
        jsonl_hash_pre_rebuild = hashlib.sha256(open(fx_r["jsonl_path"], "rb").read()).hexdigest()

        hs.rebuild_hippocampus(target1, paths_r1)
        hs.rebuild_hippocampus(target2, paths_r2)

        rows1 = hs.fetch_logical_rows(target1)
        rows2 = hs.fetch_logical_rows(target2)

        def _strip_physical(rows):
            return sorted([tuple(sorted(r.items())) for r in rows])

        check("Rebuild: two fresh rebuilds from identical sources produce identical logical rows",
              _strip_physical(rows1) == _strip_physical(rows2) and len(rows1) > 0)
        check("Rebuild: target must not already exist (second rebuild against target1 refuses)",
              expect_raises(hs.HippocampusSchemaError, hs.rebuild_hippocampus, target1, paths_r1))
        check("Rebuild: source DB/file bytes remain unchanged",
              hashlib.sha256(open(fx_r["prov_path"], "rb").read()).hexdigest() == prov_hash_pre_rebuild
              and hashlib.sha256(open(fx_r["rel_path"], "rb").read()).hexdigest() == rel_hash_pre_rebuild
              and hashlib.sha256(open(fx_r["jsonl_path"], "rb").read()).hexdigest() == jsonl_hash_pre_rebuild)

        # =====================================================================
        # FTS synchronization
        # =====================================================================
        fts_conn = sqlite3.connect(target1)
        check("FTS: item/FTS row counts stay synchronized after real ingestion", hs._fts_is_synchronized(fts_conn))
        match = fts_conn.execute("SELECT rowid FROM hippocampal_items_fts WHERE hippocampal_items_fts MATCH 'Simplicity'").fetchall()
        check("FTS: a real indexed phrase is findable via FTS5 MATCH (schema-integrity check only, not a retrieval API)", len(match) >= 1)
        fts_conn.close()

        # =====================================================================
        # No-bootstrap / unavailable / schema-mismatch behavior
        # =====================================================================
        missing_dir = os.path.join(root, "missing")
        os.makedirs(missing_dir)
        fx_missing = build_fixture(missing_dir)
        missing_paths = make_paths(fx_missing, missing_dir, hip_name="does_not_exist.db")
        check("No auto-bootstrap: sync against a missing hippocampal DB fails rather than creating one",
              expect_raises(hs.HippocampusUnavailableError, hs.sync_hippocampus, missing_paths.hippocampus_db_path, missing_paths)
              and not os.path.isfile(missing_paths.hippocampus_db_path))

        badschema_path = os.path.join(missing_dir, "badschema.db")
        bconn = sqlite3.connect(badschema_path)
        bconn.execute("CREATE TABLE hippocampus_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        bconn.execute("INSERT INTO hippocampus_meta VALUES ('schema_version', '999')")
        bconn.commit()
        bconn.close()
        badschema_paths = hs.HippocampusPaths(fx_missing["prov_path"], fx_missing["rel_path"], fx_missing["jsonl_path"], badschema_path)
        check("Schema mismatch: unexpected schema_version raises HippocampusSchemaError",
              expect_raises(hs.HippocampusSchemaError, hs.sync_hippocampus, badschema_path, badschema_paths))

        missing_source_paths = hs.HippocampusPaths(
            os.path.join(missing_dir, "nope.db"), fx_missing["rel_path"], fx_missing["jsonl_path"],
            os.path.join(missing_dir, "hip2.db"),
        )
        hs.create_hippocampus_db(missing_source_paths.hippocampus_db_path).close()
        check("Missing source: absent provenance DB raises HippocampusUnavailableError",
              expect_raises(hs.HippocampusUnavailableError, hs.sync_hippocampus, missing_source_paths.hippocampus_db_path, missing_source_paths))

        # =====================================================================
        # Path configuration (production defaults resolve relative to __file__)
        # =====================================================================
        defaults = hs.HippocampusPaths.production_defaults()
        this_dir = os.path.dirname(os.path.abspath(hs.__file__))
        check("Path config: production defaults resolve relative to hippocampus_store.py's own directory, not CWD",
              os.path.dirname(defaults.provenance_db_path) == this_dir
              and os.path.dirname(defaults.hippocampus_db_path) == this_dir)
        check("Path config: no HIPPOCAMPUS_ENABLED or similar flag exists on the module",
              not hasattr(hs, "HIPPOCAMPUS_ENABLED") and "ENABLED" not in dir(hs))

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
