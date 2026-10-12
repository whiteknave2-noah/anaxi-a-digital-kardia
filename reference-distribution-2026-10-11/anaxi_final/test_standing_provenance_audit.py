"""
Anaxi -- Standing two-direction soft-reference integrity audit test
(frozen Identity/Provenance Schema §2.0 and §3, extended per §2.0's
own projection-pending acceptance criteria and Amendment A1 §A1.6).

Exercises standing_provenance_audit.py's two directions against real,
temporary, ATTACHed SQLite files -- the actual physical topology the
frozen spec describes (anaxi_provenance.db as one file,
turn_generation_log/nodes/active_kardia/kardia_history/proposals all
sharing anaxi_mind_llama.db, relational_events in its own
anaxi_relational_llama.db), never a Python-side dict/set standing in
for the real cross-file check.

Read-only, detection-only: this suite proves the audit finds what it
claims to find, in both directions, for EVERY §3 soft-reference column
actually present in the migrated schema (not merely event_id), and
that a fault detected in one direction/column is never conflated with
another. It does not repair anything -- reconcile_native_turn()
(native_provenance_writer.py, covered by test_native_provenance_integration.py)
is the separate repair mechanism.

Run:
    python test_standing_provenance_audit.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    tmp_root = tempfile.mkdtemp(prefix="anaxi_test_standing_audit_")

    try:
        from provenance_schema import create_provenance_db
        from migrate_historical_data import build_pipeline_map, seed_reference_data, alter_existing_stores_schema
        import anaxi_protocol_sqlite
        from relational_history import RelationalHistory
        import standing_provenance_audit as audit

        # --- Real physical topology: three separate files, exactly as
        # frozen §2.0/§3 describe -- never one shared connection. ---
        provenance_db_path = os.path.join(tmp_root, "anaxi_provenance.db")
        mind_db_path = os.path.join(tmp_root, "anaxi_mind_llama.db")
        relational_db_path = os.path.join(tmp_root, "anaxi_relational_llama.db")

        create_provenance_db(provenance_db_path).close()
        manifest = {"pipelines": {
            "llama": {"routing_constant_value": "nate"},
            "claude": {"routing_constant_value": "nate"},
        }}
        pipeline_map = build_pipeline_map(manifest)
        now = int(time.time())
        prov_seed_conn = sqlite3.connect(provenance_db_path)
        prov_seed_conn.execute("PRAGMA foreign_keys = ON;")
        seed_reference_data(prov_seed_conn, pipeline_map, now)
        prov_seed_conn.close()
        llama_pipeline_id = pipeline_map["llama"]["pipeline_id"]

        # Real base schema, then the same additive-column migration
        # step production runs before activation.
        mind = anaxi_protocol_sqlite.ConstitutionalMind.__new__(anaxi_protocol_sqlite.ConstitutionalMind)
        mind.db_path = mind_db_path
        mind.conn = sqlite3.connect(mind_db_path, timeout=30.0)
        mind.cursor = mind.conn.cursor()
        mind.cursor.execute("PRAGMA foreign_keys = ON;")
        mind.conn.commit()
        mind._init_core_tables()
        RelationalHistory(relational_db_path).close()
        rel_conn_for_alter = sqlite3.connect(relational_db_path)
        alter_existing_stores_schema(rel_conn_for_alter, mind.conn)
        rel_conn_for_alter.close()
        mind.conn.close()

        # --- Seed one valid row into EVERY canonical target table §3's
        # additive columns point at, so "healthy" fixtures have
        # something real to reference. ---
        prov_conn = sqlite3.connect(provenance_db_path)
        prov_conn.execute("PRAGMA foreign_keys = ON;")

        session_id = "sess-audit-test-1"
        prov_conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, ?, ?)",
            (session_id, llama_pipeline_id, now),
        )
        auth_context_id = "authctx-audit-test-1"
        prov_conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
            "VALUES (?, ?, 'unknown', ?)",
            (auth_context_id, session_id, now),
        )
        epoch_id = "epoch-audit-test-1"
        prov_conn.execute(
            "INSERT INTO architecture_epochs (epoch_id, pipeline_id, epoch_key, epoch_kind, "
            "epoch_status, effective_begin_at, record_created_at, boundary_evidence_type, "
            "boundary_evidence, migration_basis) "
            "VALUES (?, ?, ?, 'retrospective', NULL, NULL, ?, NULL, NULL, 'synthetic audit fixture')",
            (epoch_id, llama_pipeline_id, f"epoch-key-{epoch_id}", now),
        )
        model_revision_id = "modelrev-audit-test-1"
        prov_conn.execute(
            "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
            "VALUES (?, 'synthetic-audit-tag', 'tag_only_degraded', ?)",
            (model_revision_id, now),
        )
        event_id = "event-audit-test-1"
        prov_conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, 'waking_turn', ?, 'known', NULL, ?, 'synthetic-staging-id', ?, ?)",
            (event_id, llama_pipeline_id, auth_context_id, now, now),
        )
        proposition_id = "prop-audit-test-1"
        prov_conn.execute(
            "INSERT INTO propositions (proposition_id, content, created_at, pipeline_id) "
            "VALUES (?, 'synthetic audit fixture proposition', ?, ?)",
            (proposition_id, now, llama_pipeline_id),
        )
        assertion_id = "assertion-audit-test-1"
        prov_conn.execute(
            "INSERT INTO assertions (assertion_id, proposition_id, asserted_at, pipeline_id) "
            "VALUES (?, ?, ?, ?)",
            (assertion_id, proposition_id, now, llama_pipeline_id),
        )
        prov_conn.commit()
        prov_conn.close()

        canonical = {
            "pipeline_id": llama_pipeline_id,
            "model_revision_id": model_revision_id,
            "epoch_id": epoch_id,
            "auth_context_id": auth_context_id,
            "event_id": event_id,
            "asserting_event_id": event_id,
            "source_event_id": event_id,
            "grounding_assertion_id": assertion_id,
        }
        BOGUS = "does-not-exist-anywhere-12345"

        # --- Legacy-row insert helpers, one per §3-bearing store. Each
        # accepts the additive columns as an explicit dict (defaults
        # all None) so tests can set exactly one column at a time. ---
        def insert_relational_event(ref_id, refs):
            conn = sqlite3.connect(relational_db_path)
            conn.execute(
                "INSERT INTO relational_events (user_id, substrate, created_at, external_observation, "
                "agent_response, linked_memories, status, pipeline_id, model_revision_id, epoch_id, "
                "auth_context_id, event_id) "
                "VALUES ('nate', 'llama', ?, ?, 'y', '[]', 'recorded', ?, ?, ?, ?, ?)",
                (now, ref_id, refs.get("pipeline_id"), refs.get("model_revision_id"), refs.get("epoch_id"),
                 refs.get("auth_context_id"), refs.get("event_id")),
            )
            conn.commit()
            conn.close()

        def insert_turn_generation_log(ref_id, refs):
            conn = sqlite3.connect(mind_db_path)
            conn.execute(
                "INSERT INTO turn_generation_log (user_id, timestamp, temperature, top_p, "
                "style_instruction, model_revision_id, pipeline_id, epoch_id, event_id) "
                "VALUES ('nate', ?, 0.4, 0.85, ?, ?, ?, ?, ?)",
                (now, ref_id, refs.get("model_revision_id"), refs.get("pipeline_id"), refs.get("epoch_id"),
                 refs.get("event_id")),
            )
            conn.commit()
            conn.close()

        def insert_node(node_id, refs):
            conn = sqlite3.connect(mind_db_path)
            conn.execute(
                "INSERT INTO nodes (user_id, id, label, type, salience_score, description, "
                "last_accessed_timestamp, asserted_at, pipeline_id, epoch_id, asserting_event_id, "
                "grounding_assertion_id) "
                "VALUES ('nate', ?, ?, 'Test', 1.0, ?, ?, ?, ?, ?, ?, ?)",
                (node_id, node_id, node_id, now, now, refs.get("pipeline_id"), refs.get("epoch_id"),
                 refs.get("asserting_event_id"), refs.get("grounding_assertion_id")),
            )
            conn.commit()
            conn.close()

        def insert_active_kardia(user_id, refs):
            conn = sqlite3.connect(mind_db_path)
            conn.execute(
                "INSERT INTO active_kardia (user_id, moral_valve, volitional_channel, affective_stance, "
                "aesthetic_valve, updated_at, pipeline_id, epoch_id, source_event_id) "
                "VALUES (?, 'x', 'x', 'x', 'x', ?, ?, ?, ?)",
                (user_id, now, refs.get("pipeline_id"), refs.get("epoch_id"), refs.get("source_event_id")),
            )
            conn.commit()
            conn.close()

        def insert_kardia_history(refs):
            conn = sqlite3.connect(mind_db_path)
            conn.execute(
                "INSERT INTO kardia_history (user_id, moral_valve, volitional_channel, affective_stance, "
                "aesthetic_valve, recorded_at, pipeline_id, epoch_id, source_event_id) "
                "VALUES ('nate', 'x', 'x', 'x', 'x', ?, ?, ?, ?)",
                (now, refs.get("pipeline_id"), refs.get("epoch_id"), refs.get("source_event_id")),
            )
            conn.commit()
            conn.close()

        def insert_proposal(refs):
            conn = sqlite3.connect(mind_db_path)
            conn.execute(
                "INSERT INTO proposals (user_id, proposal_type, payload, status, created_at, "
                "pipeline_id, epoch_id) "
                "VALUES ('nate', 'test_type', '{}', 'pending', ?, ?, ?)",
                (now, refs.get("pipeline_id"), refs.get("epoch_id")),
            )
            conn.commit()
            conn.close()

        def audit_mind_table(table):
            conn = sqlite3.connect(mind_db_path)
            audit.attach_provenance_db(conn, provenance_db_path)
            findings = audit.find_dangling_legacy_references(conn, table)
            conn.close()
            return findings

        def audit_relational():
            conn = sqlite3.connect(relational_db_path)
            audit.attach_provenance_db(conn, provenance_db_path)
            findings = audit.find_dangling_legacy_references(conn, "relational_events")
            conn.close()
            return findings

        # =================================================================
        # 1. Healthy baseline: one row per §3-bearing store, every
        # non-NULL soft reference pointing at a real canonical row.
        # Zero dangling findings across all six stores.
        # =================================================================
        insert_relational_event("healthy-rel-1", canonical)
        insert_turn_generation_log("healthy-tgl-1", canonical)
        insert_node("healthy-node-1", canonical)
        insert_active_kardia("healthy-kardia-user-1", canonical)
        insert_kardia_history(canonical)
        insert_proposal(canonical)

        for table in ("turn_generation_log", "nodes", "active_kardia", "kardia_history", "proposals"):
            findings = audit_mind_table(table)
            check(f"1. Healthy case: zero dangling findings in {table} "
                  f"(every populated §3 soft reference resolves)",
                  findings == [])
        check("1. Healthy case: zero dangling findings in relational_events",
              audit_relational() == [])

        # =================================================================
        # 2. Explicit reproduction of the frozen invariant (§2.0's own
        # worked example): a non-NULL relational_events.pipeline_id with
        # no matching canonical pipeline is detected.
        # =================================================================
        insert_relational_event("frozen-invariant-bad-pipeline", {**canonical, "pipeline_id": BOGUS})
        findings = audit_relational()
        pipeline_findings = [f for f in findings if f["column"] == "pipeline_id"]
        check("2. FROZEN INVARIANT (§2.0 worked example): a non-NULL relational_events.pipeline_id "
              "with no matching canonical pipeline IS detected",
              len(pipeline_findings) == 1 and pipeline_findings[0]["store"] == "relational_events"
              and pipeline_findings[0]["target_table"] == "pipelines")
        check("2. Exactly one finding total across relational_events at this point -- the bad "
              "row's OTHER soft references (event_id, model_revision_id, epoch_id, "
              "auth_context_id, all left valid) do NOT also fault",
              len(findings) == 1)

        # =================================================================
        # 3. Malformed-reference coverage, one at a time, for EVERY
        # implemented §3 soft-reference column in EVERY bearing store.
        # =================================================================
        malformed_cases = [
            ("relational_events", lambda col: insert_relational_event(f"bad-rel-{col}", {**canonical, col: BOGUS}), audit_relational),
            ("turn_generation_log", lambda col: insert_turn_generation_log(f"bad-tgl-{col}", {**canonical, col: BOGUS}), lambda: audit_mind_table("turn_generation_log")),
            ("nodes", lambda col: insert_node(f"bad-node-{col}", {**canonical, col: BOGUS}), lambda: audit_mind_table("nodes")),
            ("active_kardia", lambda col: insert_active_kardia(f"bad-kardia-user-{col}", {**canonical, col: BOGUS}), lambda: audit_mind_table("active_kardia")),
            ("kardia_history", lambda col: insert_kardia_history({**canonical, col: BOGUS}), lambda: audit_mind_table("kardia_history")),
            ("proposals", lambda col: insert_proposal({**canonical, col: BOGUS}), lambda: audit_mind_table("proposals")),
        ]
        for store, inserter, auditor in malformed_cases:
            for column, target_table, _target_col in audit.SOFT_REFERENCE_SPECS[store]:
                inserter(column)
                findings = auditor()
                matching = [f for f in findings if f["value"] == BOGUS and f["column"] == column]
                check(f"3. {store}.{column} -> {target_table}: a non-NULL bogus value IS detected "
                      f"as dangling_legacy_reference",
                      len(matching) >= 1 and all(m["target_table"] == target_table for m in matching)
                      and all(m["fault"] == "dangling_legacy_reference" for m in matching))

        # =================================================================
        # 4. Projection pending (canonical -> legacy, native waking
        # events only) -- unchanged direction, still proven for both
        # of the two stores it applies to.
        # =================================================================
        pending_event_id = "pending-event-audit-1"
        pconn = sqlite3.connect(provenance_db_path)
        pconn.execute("PRAGMA foreign_keys = ON;")
        pconn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, 'waking_turn', ?, 'known', NULL, NULL, 'staging-pending', ?, ?)",
            (pending_event_id, llama_pipeline_id, now, now),
        )
        pconn.commit()
        pconn.close()

        mconn = sqlite3.connect(mind_db_path)
        audit.attach_provenance_db(mconn, provenance_db_path)
        pending_tgl = audit.find_projection_pending(mconn, "turn_generation_log")
        mconn.close()
        rconn = sqlite3.connect(relational_db_path)
        audit.attach_provenance_db(rconn, provenance_db_path)
        pending_rel = audit.find_projection_pending(rconn, "relational_events")
        rconn.close()

        check("4. Projection-pending event IS detected for turn_generation_log",
              any(f["event_id"] == pending_event_id for f in pending_tgl))
        check("4. Projection-pending event IS detected for relational_events",
              any(f["event_id"] == pending_event_id for f in pending_rel))
        check("4. Projection-pending findings are tagged with fault='projection_pending'",
              all(f["fault"] == "projection_pending" for f in pending_tgl + pending_rel))

        # =================================================================
        # 5. Scope refusal: 'projection pending' has no defined meaning
        # for nodes/active_kardia/kardia_history/proposals -- calling it
        # there must raise, never silently return an empty, meaningless
        # result that could be mistaken for "healthy".
        # =================================================================
        for table in ("nodes", "active_kardia", "kardia_history", "proposals"):
            conn = sqlite3.connect(mind_db_path)
            audit.attach_provenance_db(conn, provenance_db_path)
            raised = False
            try:
                audit.find_projection_pending(conn, table)
            except ValueError:
                raised = True
            finally:
                conn.close()
            check(f"5. find_projection_pending({table!r}) refuses (raises ValueError) rather than "
                  f"inventing a meaningless empty result",
                  raised)

        # =================================================================
        # 6. Distinguishability: dangling findings and projection-pending
        # findings are never the same fault, and a row's dangling finding
        # for one column never appears as a finding for an unrelated
        # column on the same row.
        # =================================================================
        rel_findings = audit_relational()
        dangling_event_ids = {f["value"] for f in rel_findings if f["column"] == "event_id"}
        pending_event_ids = {f["event_id"] for f in pending_rel}
        check("6. Zero overlap between dangling event_id values and projection_pending event_id values",
              dangling_event_ids.isdisjoint(pending_event_ids))

        # =================================================================
        # 7. Unknown table refuses rather than guessing its soft-reference
        # columns.
        # =================================================================
        conn = sqlite3.connect(mind_db_path)
        audit.attach_provenance_db(conn, provenance_db_path)
        raised = False
        try:
            audit.find_dangling_legacy_references(conn, "not_a_real_table")
        except ValueError:
            raised = True
        finally:
            conn.close()
        check("7. find_dangling_legacy_references() on an unregistered table refuses "
              "(raises ValueError) rather than guessing its soft-reference columns",
              raised)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
