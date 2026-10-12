"""
Anaxi -- Slice-B tests for standing_hippocampus_audit.py.

Every test uses isolated temporary fixtures. No test writes to, or
even opens, any real live/production data path. No model/API call
anywhere in this file. Per the governing task's explicit instruction:
schema-faithful corruption methods only where source tables are
append-only (event_components/events cannot be UPDATEd/DELETEd);
hippocampal-side tampering and legitimately mutable source stores
(relational_events, the historical JSONL, direct INSERTs) are the
established, acceptable isolation techniques (Slice A precedent).

Run:
    python test_standing_hippocampus_audit.py
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
import standing_hippocampus_audit as audit
from provenance_schema import create_provenance_db, derive_historical_event_id


# =============================================================================
# Shared fixture construction (mirrors test_hippocampus_store.py's pattern).
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


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, 'waking_turn', ?, 'known', NULL, ?, ?, ?, ?)",
        (event_id, pipeline_id, auth_context_id, event_id, occurred_at, occurred_at),
    )


def build_clean_fixture(tmp_dir):
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    rel_path = os.path.join(tmp_dir, "anaxi_relational_llama.db")
    jsonl_path = os.path.join(tmp_dir, "anaxi_log.jsonl")
    hip_path = os.path.join(tmp_dir, "anaxi_hippocampus.db")

    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    pipeline_id = "pipe-llama-1"
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,))
    host_actor_id, clark_actor_id, human_actor_id = _seed_actors(conn)
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-1', ?, 100, 200)", (pipeline_id,))
    # NOTE: authenticated requires a real, human_person-typed
    # authenticated_actor_id per the frozen CHECK constraint AND trigger,
    # enforced at INSERT time (auth_contexts is also append-only, so this
    # cannot be fixed up via a later UPDATE).
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-auth', 'sess-1', NULL, ?, 'authenticated', 'os_session', 'medium', 100)", (human_actor_id,))

    _insert_event(conn, "evt-bc", pipeline_id, "ac-auth", occurred_at=150)
    _insert_event(conn, "evt-rel", pipeline_id, None, occurred_at=9999)
    bc_text = "Saved: clean audit fixture."
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES ('evt-bc', 0, ?, 'bounded_clause', 'resolved', ?, ?)",
        (host_actor_id, bc_text, hashlib.sha256(bc_text.encode("utf-8")).hexdigest()),
    )
    conn.commit()
    conn.close()

    rel_conn = sqlite3.connect(rel_path)
    rel_conn.execute("""CREATE TABLE relational_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
        created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
        agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
        status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
    )""")
    rel_conn.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) VALUES ('alex', 'llama', 150, 'A clean relational human observation.', 'evt-rel')")
    rel_conn.commit()
    rel_conn.close()

    open(jsonl_path, "w").close()

    hs.create_hippocampus_db(hip_path).close()
    paths = hs.HippocampusPaths(prov_path, rel_path, jsonl_path, hip_path)
    hs.sync_hippocampus(hip_path, paths)

    return {"prov_path": prov_path, "rel_path": rel_path, "jsonl_path": jsonl_path, "hip_path": hip_path,
            "pipeline_id": pipeline_id, "host_actor_id": host_actor_id, "clark_actor_id": clark_actor_id,
            "human_actor_id": human_actor_id}


def run_audit(fx):
    return audit.run_standing_hippocampus_audit(fx["hip_path"], fx["prov_path"], fx["rel_path"], fx["jsonl_path"])


# =============================================================================
# Test suite
# =============================================================================
def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    def other_families_stable(result, exclude):
        return all(result.counts[f] == 0 for f in audit.AUDIT_FAMILIES if f not in exclude)

    root = tempfile.mkdtemp(prefix="anaxi_hippocampus_audit_test_")
    try:
        # =====================================================================
        # Clean audit: all twelve families exactly zero
        # =====================================================================
        clean_dir = os.path.join(root, "clean")
        os.makedirs(clean_dir)
        fx_clean = build_clean_fixture(clean_dir)

        prov_hash_before = hashlib.sha256(open(fx_clean["prov_path"], "rb").read()).hexdigest()
        rel_hash_before = hashlib.sha256(open(fx_clean["rel_path"], "rb").read()).hexdigest()
        jsonl_hash_before = hashlib.sha256(open(fx_clean["jsonl_path"], "rb").read()).hexdigest()
        hip_hash_before = hashlib.sha256(open(fx_clean["hip_path"], "rb").read()).hexdigest()

        result_clean = run_audit(fx_clean)
        check("Clean fixture: all twelve audit families are exactly present", set(result_clean.counts.keys()) == set(audit.AUDIT_FAMILIES) and len(audit.AUDIT_FAMILIES) == 12)
        check("Clean fixture: all twelve audit counts are exactly zero", result_clean.is_clean() and all(v == 0 for v in result_clean.counts.values()))

        prov_hash_after = hashlib.sha256(open(fx_clean["prov_path"], "rb").read()).hexdigest()
        rel_hash_after = hashlib.sha256(open(fx_clean["rel_path"], "rb").read()).hexdigest()
        jsonl_hash_after = hashlib.sha256(open(fx_clean["jsonl_path"], "rb").read()).hexdigest()
        hip_hash_after = hashlib.sha256(open(fx_clean["hip_path"], "rb").read()).hexdigest()
        check("Read-only: provenance source unchanged by audit", prov_hash_before == prov_hash_after)
        check("Read-only: relational source unchanged by audit", rel_hash_before == rel_hash_after)
        check("Read-only: JSONL source unchanged by audit", jsonl_hash_before == jsonl_hash_after)
        check("Read-only: audit leaves the hippocampus database itself unchanged", hip_hash_before == hip_hash_after)

        # =====================================================================
        # 1. dangling_event_id -- hand-crafted hippocampal row, self-
        # consistent item_id, pointing at a nonexistent canonical event.
        # =====================================================================
        d1 = os.path.join(root, "f1_dangling")
        os.makedirs(d1)
        fx1 = build_clean_fixture(d1)
        bogus_event_id = "evt-does-not-exist"
        item_id_1 = hs.derive_hippocampal_item_id(bogus_event_id, "relational_events", "999")
        conn = sqlite3.connect(fx1["hip_path"])
        conn.execute(
            "INSERT INTO hippocampal_items (item_id, event_id, source_store, source_locator, content_text, "
            "content_sha256, memory_kind, attribution_status, authentication_status, occurred_at, "
            "session_resolution, index_schema_version) VALUES (?, ?, 'relational_events', '999', 'x', ?, "
            "'human_expression', 'unknown', 'unknown', 1, 'unknown', 1)",
            (item_id_1, bogus_event_id, hashlib.sha256(b"x").hexdigest()),
        )
        conn.commit()
        conn.close()
        result1 = run_audit(fx1)
        check("1. dangling_event_id: nonexistent canonical event_id is detected", result1.counts["dangling_event_id"] == 1)
        check("1. dangling_event_id: unrelated families remain zero", other_families_stable(result1, {"dangling_event_id"}))
        check("1. Audit performed no repair (offending row still present)",
              sqlite3.connect(fx1["hip_path"]).execute("SELECT COUNT(*) FROM hippocampal_items WHERE item_id = ?", (item_id_1,)).fetchone()[0] == 1)

        # =====================================================================
        # 2. missing_source_record -- real event, real source_store, but a
        # relational_events locator that does not exist.
        # =====================================================================
        d2 = os.path.join(root, "f2_missing")
        os.makedirs(d2)
        fx2 = build_clean_fixture(d2)
        item_id_2 = hs.derive_hippocampal_item_id("evt-rel", "relational_events", "9999")
        conn = sqlite3.connect(fx2["hip_path"])
        conn.execute(
            "INSERT INTO hippocampal_items (item_id, event_id, source_store, source_locator, content_text, "
            "content_sha256, memory_kind, attribution_status, authentication_status, occurred_at, "
            "session_resolution, index_schema_version) VALUES (?, 'evt-rel', 'relational_events', '9999', 'x', ?, "
            "'human_expression', 'unknown', 'unknown', 9999, 'unknown', 1)",
            (item_id_2, hashlib.sha256(b"x").hexdigest()),
        )
        conn.commit()
        conn.close()
        result2 = run_audit(fx2)
        check("2. missing_source_record: nonexistent relational_events locator is detected", result2.counts["missing_source_record"] == 1)
        check("2. missing_source_record: unrelated families remain zero", other_families_stable(result2, {"missing_source_record"}))

        # =====================================================================
        # 3. content_sha256_mismatch -- real relational_events row, wrong
        # stored hash.
        # =====================================================================
        d3 = os.path.join(root, "f3_hash")
        os.makedirs(d3)
        fx3 = build_clean_fixture(d3)
        conn = sqlite3.connect(fx3["hip_path"])
        conn.execute("UPDATE hippocampal_items SET content_sha256 = 'deadbeef' WHERE event_id = 'evt-rel'")
        conn.commit()
        conn.close()
        result3 = run_audit(fx3)
        check("3. content_sha256_mismatch: tampered stored hash is detected", result3.counts["content_sha256_mismatch"] == 1)
        check("3. content_sha256_mismatch: unrelated families remain zero", other_families_stable(result3, {"content_sha256_mismatch"}))

        # =====================================================================
        # 4. historical_event_id_reconstruction_mismatch
        # =====================================================================
        d4 = os.path.join(root, "f4_histmismatch")
        os.makedirs(d4)
        fx4 = build_clean_fixture(d4)
        hist_line = json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "substrate": "llama", "model": "llama3.2:3b", "prompt": "hist prompt", "response": "hist response", "kardia": {}})
        real_hist_event_id = derive_historical_event_id("anaxi_log.jsonl", hist_line.encode("utf-8"), 0)
        wrong_event_id = "hist-wrong-" + real_hist_event_id[-10:]
        conn = sqlite3.connect(fx4["prov_path"])
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
            "auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, 'waking_turn', ?, 'known', NULL, NULL, ?, 50, 50)",
            (wrong_event_id, fx4["pipeline_id"], wrong_event_id),
        )
        # Mark it historical too (matching how a real migrated event would
        # look) -- otherwise authentication/session re-derivation would
        # legitimately disagree with this fabricated row's stored values,
        # leaking into unrelated audit families this test isn't about.
        conn.execute(
            "INSERT INTO event_migration_status (event_id, migration_status, migrated_at, migration_basis) "
            "VALUES (?, 'pre_authentication_layer', 50, 'test fixture')",
            (wrong_event_id,),
        )
        conn.commit()
        conn.close()
        with open(fx4["jsonl_path"], "w", encoding="utf-8", newline="\n") as f:
            f.write(hist_line + "\n")
        item_id_4 = hs.derive_hippocampal_item_id(wrong_event_id, "anaxi_log_jsonl", "line:0")
        conn = sqlite3.connect(fx4["hip_path"])
        conn.execute(
            "INSERT INTO hippocampal_items (item_id, event_id, source_store, source_locator, source_line_index, "
            "content_text, content_sha256, memory_kind, attribution_status, authentication_status, occurred_at, "
            "session_resolution, index_schema_version) VALUES (?, ?, 'anaxi_log_jsonl', 'line:0', 0, 'hist prompt', ?, "
            "'human_expression', 'unknown', 'pre_authentication_layer', 50, 'unknown', 1)",
            (item_id_4, wrong_event_id, hashlib.sha256(b"hist prompt").hexdigest()),
        )
        conn.commit()
        conn.close()
        result4 = run_audit(fx4)
        check("4. historical_event_id_reconstruction_mismatch: stored event_id disagreeing with recomputation is detected",
              result4.counts["historical_event_id_reconstruction_mismatch"] == 1)
        check("4. historical_event_id_reconstruction_mismatch: unrelated families remain zero",
              other_families_stable(result4, {"historical_event_id_reconstruction_mismatch"}))

        # =====================================================================
        # 5. duplicate_logical_item -- stored item_id disagrees with its own
        # deterministic recomputation (everything else internally correct).
        # =====================================================================
        d5 = os.path.join(root, "f5_dup")
        os.makedirs(d5)
        fx5 = build_clean_fixture(d5)
        conn = sqlite3.connect(fx5["hip_path"])
        conn.execute("UPDATE hippocampal_items SET item_id = 'hip-0000000000000000000000000' WHERE event_id = 'evt-rel'")
        conn.commit()
        conn.close()
        result5 = run_audit(fx5)
        check("5. duplicate_logical_item: item_id disagreeing with deterministic recomputation is detected", result5.counts["duplicate_logical_item"] == 1)
        check("5. duplicate_logical_item: unrelated families remain zero", other_families_stable(result5, {"duplicate_logical_item"}))

        # =====================================================================
        # 6. invalid_memory_kind_mapping
        # =====================================================================
        d6 = os.path.join(root, "f6_kind")
        os.makedirs(d6)
        fx6 = build_clean_fixture(d6)
        conn = sqlite3.connect(fx6["hip_path"])
        conn.execute("UPDATE hippocampal_items SET memory_kind = 'mechanical_record' WHERE event_id = 'evt-rel'")
        conn.commit()
        conn.close()
        result6 = run_audit(fx6)
        check("6. invalid_memory_kind_mapping: wrong memory_kind for relational_events source is detected", result6.counts["invalid_memory_kind_mapping"] == 1)
        check("6. invalid_memory_kind_mapping: unrelated families remain zero", other_families_stable(result6, {"invalid_memory_kind_mapping"}))

        # =====================================================================
        # 7. invalid_attribution_mapping
        # =====================================================================
        d7 = os.path.join(root, "f7_attrib")
        os.makedirs(d7)
        fx7 = build_clean_fixture(d7)
        conn = sqlite3.connect(fx7["hip_path"])
        conn.execute("UPDATE hippocampal_items SET creator_actor_id = ?, creator_actor_type = 'host_system' WHERE event_id = 'evt-rel'", (fx7["host_actor_id"],))
        conn.commit()
        conn.close()
        result7 = run_audit(fx7)
        check("7. invalid_attribution_mapping: invented creator on native human expression is detected", result7.counts["invalid_attribution_mapping"] == 1)
        check("7. invalid_attribution_mapping: unrelated families remain zero", other_families_stable(result7, {"invalid_attribution_mapping"}))

        # =====================================================================
        # 8. invalid_authentication_mapping
        # =====================================================================
        d8 = os.path.join(root, "f8_auth")
        os.makedirs(d8)
        fx8 = build_clean_fixture(d8)
        conn = sqlite3.connect(fx8["hip_path"])
        conn.execute("UPDATE hippocampal_items SET authentication_status = 'unknown' WHERE event_id = 'evt-bc'")
        conn.commit()
        conn.close()
        result8 = run_audit(fx8)
        check("8. invalid_authentication_mapping: stale authentication_status (real event is authenticated) is detected", result8.counts["invalid_authentication_mapping"] == 1)
        check("8. invalid_authentication_mapping: unrelated families remain zero", other_families_stable(result8, {"invalid_authentication_mapping"}))

        # =====================================================================
        # 9. invalid_session_resolution -- evt-bc genuinely resolves to
        # sess-1; store it as unknown instead.
        # =====================================================================
        d9 = os.path.join(root, "f9_session")
        os.makedirs(d9)
        fx9 = build_clean_fixture(d9)
        conn = sqlite3.connect(fx9["hip_path"])
        conn.execute("UPDATE hippocampal_items SET session_id = NULL, session_resolution = 'unknown' WHERE event_id = 'evt-bc'")
        conn.commit()
        conn.close()
        result9 = run_audit(fx9)
        check("9. invalid_session_resolution: stale session resolution (real event genuinely resolves) is detected", result9.counts["invalid_session_resolution"] == 1)
        check("9. invalid_session_resolution: unrelated families remain zero", other_families_stable(result9, {"invalid_session_resolution"}))

        # =====================================================================
        # 10. Amendment S1: a second, temporally-overlapping session no
        # longer manufactures ambiguity for a canonically-assigned native
        # event. evt-bc's canonical session (via ac-auth) is sess-1;
        # sess-overlap (100-250) genuinely overlaps sess-1 (100-200) in
        # time, but evt-bc was never canonically assigned to it. A normal
        # re-sync after sess-overlap appears must leave evt-bc's stored
        # session_id/session_resolution untouched (still sess-1/resolved) --
        # ambiguous_session_resolution and invalid_session_resolution both
        # stay zero, proving S1 genuinely ignores temporal overlap once an
        # exact canonical link exists, not merely tolerates it by luck.
        # =====================================================================
        d10 = os.path.join(root, "f10_overlap_ignored")
        os.makedirs(d10)
        fx10 = build_clean_fixture(d10)
        conn = sqlite3.connect(fx10["prov_path"])
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-overlap', ?, 100, 250)", (fx10["pipeline_id"],))
        conn.commit()
        conn.close()
        sync_result10 = hs.sync_hippocampus(
            fx10["hip_path"], hs.HippocampusPaths(fx10["prov_path"], fx10["rel_path"], fx10["jsonl_path"], fx10["hip_path"])
        )
        result10 = run_audit(fx10)
        check("10. S1: an unrelated temporally-overlapping session produces NO session_updated churn "
              "for the canonically-assigned item (it was already correctly resolved to sess-1, and "
              "sess-overlap's mere existence changes nothing about that)",
              sync_result10.session_updated == [])
        check("10. S1: ambiguous_session_resolution stays zero despite genuine temporal overlap "
              "between sess-1 and sess-overlap", result10.counts["ambiguous_session_resolution"] == 0)
        check("10. S1: invalid_session_resolution stays zero -- the stored (canonical) value still "
              "matches independent re-derivation", result10.counts["invalid_session_resolution"] == 0)
        check("10. S1: unrelated families remain zero", other_families_stable(result10, set()))

        # =====================================================================
        # 11. unsupported_component_kind -- a real canonical component with
        # a component_kind outside the v1 whitelist, never even requiring a
        # corresponding hippocampal row.
        # =====================================================================
        d11 = os.path.join(root, "f11_unsupported")
        os.makedirs(d11)
        fx11 = build_clean_fixture(d11)
        conn = sqlite3.connect(fx11["prov_path"])
        conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256) VALUES ('evt-bc', 1, ?, 'raw_debug_note', "
            "'resolved', 'not a v1 kind', ?)",
            (fx11["host_actor_id"], hashlib.sha256(b"not a v1 kind").hexdigest()),
        )
        conn.commit()
        conn.close()
        result11 = run_audit(fx11)
        check("11. unsupported_component_kind: a real canonical component outside the whitelist is detected", result11.counts["unsupported_component_kind"] == 1)
        check("11. unsupported_component_kind: unrelated families remain zero", other_families_stable(result11, {"unsupported_component_kind"}))

        # =====================================================================
        # 12. fts_item_mismatch -- direct removal of one FTS row via FTS5's
        # own 'delete' command, leaving hippocampal_items untouched (a real,
        # reachable corruption mode: an interrupted/partial FTS write).
        # =====================================================================
        d12 = os.path.join(root, "f12_fts")
        os.makedirs(d12)
        fx12 = build_clean_fixture(d12)
        conn = sqlite3.connect(fx12["hip_path"])
        row = conn.execute("SELECT row_id, content_text FROM hippocampal_items WHERE event_id = 'evt-rel'").fetchone()
        conn.execute("INSERT INTO hippocampal_items_fts(hippocampal_items_fts, rowid, content_text) VALUES('delete', ?, ?)", row)
        conn.commit()
        conn.close()
        result12 = run_audit(fx12)
        check("12. fts_item_mismatch: a missing FTS row for a real indexed item is detected", result12.counts["fts_item_mismatch"] == 1)
        check("12. fts_item_mismatch: unrelated families remain zero", other_families_stable(result12, {"fts_item_mismatch"}))

        # =====================================================================
        # No repair anywhere -- spot check across several of the fixtures
        # above that the audit never mutated the hippocampus DB it inspected.
        # =====================================================================
        for label, fx, path_key in (("f3", fx3, "hip_path"), ("f6", fx6, "hip_path"), ("f9", fx9, "hip_path")):
            before = hashlib.sha256(open(fx[path_key], "rb").read()).hexdigest()
            run_audit(fx)
            after = hashlib.sha256(open(fx[path_key], "rb").read()).hexdigest()
            check(f"No-repair: running the audit twice on fixture {label} does not change the hippocampus DB", before == after)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
