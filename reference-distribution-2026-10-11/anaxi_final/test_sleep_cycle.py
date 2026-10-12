"""
ANAXI SLP1-C -- integration/behavioral regression suite for the dormant
Sleep cycle orchestrator (sleep_cycle.py) and its fenced lease
(sleep_lease.py) and watermark (sleep_watermark.py) collaborators.

Every test uses a temporary, synthetic canonical provenance DB built
via the real create_provenance_db() -- no test touches production
anaxi_provenance.db. No real model call anywhere in this file --
sleep_selection._selection_chat and sleep_transformation._transformation_chat
are monkeypatched to scripted fakes.

Run:
    python test_sleep_cycle.py
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
import sleep_c_schema as schema
import sleep_cycle as cycle_mod
import sleep_evidence as se
import sleep_lease as lease_mod
import sleep_selection
import sleep_transformation as st
import sleep_watermark as watermark_mod

TEST_ROOT = tempfile.mkdtemp(prefix="slp1c_cycle_test_")


# =============================================================== fixtures =


def _seed_actors(conn):
    now = int(time.time())
    host_id, clark_id = "actor-host-1", "actor-clark-1"
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)",
        (host_id, now),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_id,))
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
        (clark_id, now),
    )
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (clark_id,))
    return host_id, clark_id


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at, event_type):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, ?, ?, 'known', NULL, ?, ?, ?, ?)",
        (event_id, event_type, pipeline_id, auth_context_id, event_id, occurred_at, occurred_at),
    )


def _insert_component(conn, event_id, sequence, creator_actor_id, component_kind, text):
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, ?, ?, ?, 'resolved', ?, ?)",
        (event_id, sequence, creator_actor_id, component_kind, text, content_sha256),
    )
    return conn.execute(
        "SELECT component_id FROM event_components WHERE event_id = ? AND sequence = ?", (event_id, sequence)
    ).fetchone()[0]


def fresh_db(name):
    tmp_dir = os.path.join(TEST_ROOT, name)
    os.makedirs(tmp_dir, exist_ok=True)
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    pipeline_id = "pipe-llama-1"
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')",
        (pipeline_id,),
    )
    host_id, clark_id = _seed_actors(conn)
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-1', ?, 100, 200)", (pipeline_id,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-unk', 'sess-1', 'unknown', 100)")
    conn.commit()
    conn.isolation_level = None
    return conn, {"pipeline_id": pipeline_id, "host_id": host_id, "clark_id": clark_id}


def add_eligible_turn(conn, ids, event_id, text, sequence=1, occurred_at=100):
    _insert_event(conn, event_id, ids["pipeline_id"], "ac-unk", occurred_at, "waking_turn")
    return _insert_component(conn, event_id, sequence, ids["clark_id"], "conversational_prose", text)


def add_ineligible_component(conn, ids, event_id, occurred_at=100):
    _insert_event(conn, event_id, ids["pipeline_id"], "ac-unk", occurred_at, "waking_turn")
    return _insert_component(conn, event_id, 0, ids["host_id"], "bounded_clause", "host-mechanical, never selection-bearing")


def install_selection_chat(fn):
    original = sleep_selection._selection_chat
    sleep_selection._selection_chat = fn
    return original


def install_transformation_chat(fn):
    original = st._transformation_chat
    st._transformation_chat = fn
    return original


def restore_selection_chat(original):
    sleep_selection._selection_chat = original


def restore_transformation_chat(original):
    st._transformation_chat = original


def always_fail_if_called(name):
    def _fn(messages):
        raise AssertionError(f"{name} must never be called on this path")
    return _fn


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(bool(cond))

    # === 5. ZERO PATHS (combined table) ===

    # A. no source rows at all -> NO_WORK, no model call, no watermark change.
    conn, ids = fresh_db("zero_a_no_rows")
    orig_sel = install_selection_chat(always_fail_if_called("Selection"))
    orig_tr = install_transformation_chat(always_fail_if_called("Transformation"))
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-a", now=1000)
        check("5A. no canonical rows at all -> NO_WORK", result.status == "no_work")
        wm = watermark_mod.read_watermark_from_conn(conn)
        check("5A. watermark unchanged after NO_WORK", wm == watermark_mod.INITIAL_WATERMARK_STATE)
        cycles = conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0]
        check("5A. no sleep_cycles row created", cycles == 0)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
        conn.close()

    # B. only ineligible rows -> successful zero-selection window, watermark advances, no model call.
    conn, ids = fresh_db("zero_b_ineligible")
    ineligible_component_id = add_ineligible_component(conn, ids, "evt-ineligible", occurred_at=100)
    conn.commit()
    orig_sel = install_selection_chat(always_fail_if_called("Selection"))
    orig_tr = install_transformation_chat(always_fail_if_called("Transformation"))
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-b", now=1000)
        check("5B. all-ineligible window completes successfully", result.status == "completed")
        check("5B. zero selected, zero derivations", result.selected_count == 0 and result.derivation_count == 0)
        check("5B. watermark advances through the fully-processed ineligible window",
              watermark_mod.read_watermark_from_conn(conn)["last_processed_component_id"] == ineligible_component_id)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
        conn.close()

    # C. Selection returns zero -> Transformation never called, successful completion.
    conn, ids = fresh_db("zero_c_selection_zero")
    add_eligible_turn(conn, ids, "evt-x", "Clark's reply.", occurred_at=100)
    conn.commit()
    orig_sel = install_selection_chat(lambda messages: json.dumps({"selected_wmu_ids": []}))
    orig_tr = install_transformation_chat(always_fail_if_called("Transformation"))
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-c", now=1000)
        check("5C. Selection returning zero completes the cycle successfully with zero derivations",
              result.status == "completed" and result.selected_count == 0 and result.derivation_count == 0)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
        conn.close()

    # D. Transformation returns zero -> successful completion, selected_count reflects Selection.
    conn, ids = fresh_db("zero_d_transformation_zero")
    x_component_id = add_eligible_turn(conn, ids, "evt-x", "Clark's reply.", occurred_at=100)
    conn.commit()
    wmu_id_holder = {}

    def selection_selects_everything(messages):
        import re
        offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
        wmu_id_holder["ids"] = offered
        return json.dumps({"selected_wmu_ids": offered})

    orig_sel = install_selection_chat(selection_selects_everything)
    orig_tr = install_transformation_chat(lambda messages: json.dumps({"derivations": []}))
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-d", now=1000)
        check("5D. Transformation returning zero completes the cycle successfully",
              result.status == "completed" and result.selected_count == 1 and result.derivation_count == 0)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
        conn.close()

    # === 4. SOURCE-LINKED CANONICAL PERSISTENCE ===
    conn, ids = fresh_db("persistence")
    x_component_id = add_eligible_turn(conn, ids, "evt-x", "First waking sentence.", occurred_at=100)
    y_component_id = add_eligible_turn(conn, ids, "evt-y", "Second waking sentence.", occurred_at=200)
    conn.commit()

    captured_offered = {}

    def selection_selects_both(messages):
        import re
        offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
        captured_offered["ids"] = offered
        return json.dumps({"selected_wmu_ids": offered})

    def transformation_one_derivation(messages):
        import re
        offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
        return json.dumps({"derivations": [
            {"source_wmu_ids": offered, "derived_text": "these two moments seem connected"}
        ]})

    orig_sel = install_selection_chat(selection_selects_both)
    orig_tr = install_transformation_chat(transformation_one_derivation)
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-persist", now=5000)
        check("4. persistence cycle completes with exactly one derivation",
              result.status == "completed" and result.derivation_count == 1)

        cycle_row = conn.execute(
            "SELECT window_start_component_id, window_end_component_id, selected_count, derivation_count "
            "FROM sleep_cycles WHERE cycle_id = ?", (result.cycle_id,)
        ).fetchone()
        check("4. cycle relation persisted correctly",
              cycle_row == (0, y_component_id, 2, 1))

        deriv_row = conn.execute(
            "SELECT derived_text, derived_text_sha256 FROM sleep_derivations WHERE cycle_id = ?", (result.cycle_id,)
        ).fetchone()
        expected_hash = hashlib.sha256("these two moments seem connected".encode("utf-8")).hexdigest()
        check("4. exact derived text and content hash persisted",
              deriv_row == ("these two moments seem connected", expected_hash))

        source_rows = conn.execute(
            "SELECT wmu_id, source_component_id FROM sleep_derivation_sources "
            "WHERE derivation_id = (SELECT derivation_id FROM sleep_derivations WHERE cycle_id = ?)",
            (result.cycle_id,),
        ).fetchall()
        persisted_component_ids = {r[1] for r in source_rows}
        check("4. cited WMUs' canonical component sources map exactly to what the model actually saw "
              "(both real component ids, nothing else)",
              persisted_component_ids == {x_component_id, y_component_id})
        check("4. exactly two distinct cited wmu_ids persisted (one per WMU, no support-only fact masquerading)",
              len({r[0] for r in source_rows}) == 2)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)

    # === 6. ATOMIC FAILURE ===
    original_advance = watermark_mod.advance_watermark_in_conn

    def failing_advance(*args, **kwargs):
        raise RuntimeError("injected failure just before watermark advance")

    watermark_mod.advance_watermark_in_conn = failing_advance
    try:
        raised = False
        try:
            cycle_mod._commit_successful_cycle(
                conn, owner_id="owner-persist-2", generation=999999, now=5001,
                window_start_component_id=y_component_id, window_end_component_id=y_component_id + 100,
                selected_count=0, derivations=[], id_to_wmu={},
            )
        except Exception:
            raised = True
        check("6. injected failure inside the final transaction raises", raised)
        # This particular call also fails lease verification first (bogus
        # generation) -- prove the INJECTED failure path separately by
        # bypassing the lease check via a real, currently-valid lease.
    finally:
        watermark_mod.advance_watermark_in_conn = original_advance

    # A cleaner, more direct atomic-failure proof: acquire a real lease,
    # then inject the failure, and confirm no sleep_cycles row for the
    # attempted cycle_id and the watermark is untouched by it.
    handle = lease_mod.acquire_lease(conn, owner_id="owner-atomic", now=9000)
    watermark_before = watermark_mod.read_watermark_from_conn(conn)
    watermark_mod.advance_watermark_in_conn = failing_advance
    try:
        attempted_window_end = y_component_id + 100
        raised = False
        try:
            cycle_mod._commit_successful_cycle(
                conn, owner_id="owner-atomic", generation=handle.generation, now=9000,
                window_start_component_id=y_component_id, window_end_component_id=attempted_window_end,
                selected_count=0, derivations=[], id_to_wmu={},
            )
        except RuntimeError:
            raised = True
        check("6. injected mid-transaction failure propagates", raised)
        attempted_cycle_id = cycle_mod._derive_cycle_id("owner-atomic", handle.generation, y_component_id, attempted_window_end)
        row = conn.execute("SELECT 1 FROM sleep_cycles WHERE cycle_id = ?", (attempted_cycle_id,)).fetchone()
        check("6. no partial sleep_cycles row survives the rollback", row is None)
        check("6. watermark left completely unchanged by the failed attempt",
              watermark_mod.read_watermark_from_conn(conn) == watermark_before)
        # The lease itself must still be intact too (release is inside
        # the same rolled-back transaction).
        lease_row = conn.execute("SELECT owner_id, generation FROM sleep_v1_lease WHERE id = 1").fetchone()
        check("6. lease row is also rolled back to its pre-attempt state (release never took effect)",
              lease_row == ("owner-atomic", handle.generation))
    finally:
        watermark_mod.advance_watermark_in_conn = original_advance
        lease_mod.release_lease(conn, owner_id="owner-atomic", generation=handle.generation)
        conn.close()

    # === 7. FENCING ===
    conn, ids = fresh_db("fencing")
    schema.ensure_schema(conn)
    conn.commit()
    try:
        h1 = lease_mod.acquire_lease(conn, owner_id="A", now=1000)
        check("7. owner A acquires generation 1", h1 is not None and h1.generation == 1)
        h_blocked = lease_mod.acquire_lease(conn, owner_id="B", now=1001)
        check("7. owner B cannot acquire while A's lease is still valid", h_blocked is None)
        h2 = lease_mod.acquire_lease(conn, owner_id="B", now=h1.expires_at + 1)
        check("7. owner B acquires generation 2 after A's lease expires", h2 is not None and h2.generation == 2)
        check("7. A's stale generation is rejected by final-commit verification",
              lease_mod.verify_lease_for_commit(conn, owner_id="A", generation=h1.generation, now=h2.expires_at) is False)
        check("7. A cannot clear B's newer lease",
              lease_mod.release_lease(conn, owner_id="A", generation=h1.generation) is False)
        check("7. B's own lease is untouched by A's failed release attempt",
              lease_mod.verify_lease_for_commit(conn, owner_id="B", generation=h2.generation, now=h2.expires_at - 1) is True)
        check("7. B can complete (release its own valid lease)",
              lease_mod.release_lease(conn, owner_id="B", generation=h2.generation) is True)
    finally:
        conn.close()

    # === 8. CRASH-AFTER-COMMIT / IDEMPOTENCY ===
    conn, ids = fresh_db("idempotency")
    add_eligible_turn(conn, ids, "evt-x", "Only one waking sentence.", occurred_at=100)
    conn.commit()
    orig_sel = install_selection_chat(lambda messages: json.dumps({"selected_wmu_ids": []}))
    orig_tr = install_transformation_chat(always_fail_if_called("Transformation"))
    try:
        result1 = cycle_mod.run_sleep_cycle(conn, owner_id="owner-e1", now=1000)
        check("8. first attempt completes successfully", result1.status == "completed")
        cycle_count_after_first = conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0]

        # Simulated "lost return"/process restart: run again against the
        # SAME already-committed DB with no new source rows.
        result2 = cycle_mod.run_sleep_cycle(conn, owner_id="owner-e2", now=2000)
        check("8. re-attempt after successful commit sees NO_WORK (does not reprocess the same window)",
              result2.status == "no_work")
        cycle_count_after_second = conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0]
        check("8. no duplicate sleep_cycles row was created", cycle_count_after_second == cycle_count_after_first)

        # New material arrives -- only the NEW window is processed.
        add_eligible_turn(conn, ids, "evt-x2", "A later waking sentence.", occurred_at=300, sequence=2)
        conn.commit()
        result3 = cycle_mod.run_sleep_cycle(conn, owner_id="owner-e3", now=3000)
        check("8. a later cycle over new material completes normally and does not duplicate the prior cycle",
              result3.status == "completed" and
              conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0] == cycle_count_after_first + 1)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
        conn.close()

    # === 9. WINDOW SNAPSHOT / PARTIAL SEGMENT ===
    conn, ids = fresh_db("window_snapshot")
    long_component_id = add_eligible_turn(conn, ids, "evt-long", "0123456789" * 3, occurred_at=100)  # 30 chars
    conn.commit()
    snapshot_through = cycle_mod._read_max_component_id(conn)
    check("9. snapshot upper bound is exactly the long component's own id before anything later arrives",
          snapshot_through == long_component_id)

    # Simulate material arriving AFTER the snapshot was taken.
    later_component_id = add_eligible_turn(conn, ids, "evt-later", "arrived after snapshot", occurred_at=200)
    conn.commit()
    check("9. a later component genuinely exists past the snapshot in the DB",
          later_component_id > snapshot_through)

    items, window_end = cycle_mod.gather_window_evidence(
        conn, after_component_id=0, through_component_id=snapshot_through,
        max_segments=1, max_chars_per_segment=10, max_aggregate_chars=1000,
    )
    reconstructed = "".join(
        it["expression"] for it in sorted(
            [i for i in items if i["component_id"] == long_component_id], key=lambda i: i["segment_index"]
        )
    )
    check("9. the pending multi-segment component is fully completed by resume, despite max_segments=1",
          reconstructed == "0123456789" * 3)
    check("9. window_end is the pending component's own id once fully complete, never beyond it",
          window_end == long_component_id)
    check("9. the later-arrived component (past the snapshot) is never included in this window's evidence",
          all(it["component_id"] != later_component_id for it in items))
    conn.close()

    # === 10. ANTI-RECURSION / NO IDENTITY (static) ===
    c_source_files = [
        "sleep_cycle.py", "sleep_transformation.py", "sleep_lease.py",
        "sleep_watermark.py", "sleep_c_schema.py",
    ]
    forbidden = ["kardia", "mind_graph", "active_kardia", "govern_identity_revision",
                 "hippocampus_store", "hippocampus_retrieval", "execute_sleep_consolidation"]
    violations = []
    for fname in c_source_files:
        with open(os.path.join(ANAXI_FINAL, fname), "r", encoding="utf-8") as f:
            src = f.read().lower()
        for token in forbidden:
            if token in src:
                violations.append((fname, token))
    check(f"10. no C module imports/calls Kardia, mind-graph, identity-reflection, or hippocampal writers -- "
          f"violations: {violations!r}", violations == [])

    check("10. Sleep-derived event/component shapes are never added to A's positive waking-material allowlist",
          not any("sleep_derivation" in str(t) or "sleep_cycle" in str(t) for t in se.ELIGIBLE_SELECTION_TUPLES))

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    sys.exit(0 if success else 1)
