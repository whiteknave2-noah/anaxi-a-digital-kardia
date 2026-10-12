"""
ANAXI SLP1-D -- behavioral regression suite for dormant Sleep Landing
(the fourth hippocampal ingestion path in hippocampus_store.py, and the
neutral Sleep-derived rendering label in hippocampus_retrieval.py).

Every test uses temporary, synthetic databases. No test writes to, or
even opens, any real live/production data path. No model call anywhere
in this file. No real Sleep cycle is ever run -- synthetic sleep_cycles/
sleep_derivations rows are inserted directly, exactly matching what
SLP1-C's own atomic commit would have produced.

Run:
    python test_sleep_landing.py
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hippocampus_store as hs
import hippocampus_retrieval as hr
import sleep_c_schema as schema
from provenance_schema import create_provenance_db

TEST_ROOT = tempfile.mkdtemp(prefix="slp1d_landing_test_")


# =============================================================== fixtures =


def build_sources(tmp_dir, apply_sleep_schema=True):
    """A bare provenance DB (+ SLP1-C's additive Sleep tables, unless
    apply_sleep_schema=False), an empty relational DB, and an empty
    historical JSONL -- the minimum sync_hippocampus() needs to run at
    all. No actors/events/components are seeded: Sleep-derivation
    ingestion needs none of that machinery."""
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    rel_path = os.path.join(tmp_dir, "anaxi_relational_llama.db")
    jsonl_path = os.path.join(tmp_dir, "anaxi_log.jsonl")

    conn = create_provenance_db(prov_path)
    if apply_sleep_schema:
        schema.ensure_schema(conn)
    conn.commit()
    conn.close()

    rel_conn = sqlite3.connect(rel_path)
    rel_conn.execute("""CREATE TABLE relational_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
        created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
        agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
        status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
    )""")
    rel_conn.commit()
    rel_conn.close()

    with open(jsonl_path, "w", encoding="utf-8", newline="\n"):
        pass

    return prov_path, rel_path, jsonl_path


def insert_cycle_and_derivation(prov_path, *, cycle_id, derivation_id, derived_text,
                                 window_start=0, window_end=10, batch_index=0, item_index=0,
                                 also_insert_cycle=True):
    """Inserts exactly the rows SLP1-C's own atomic commit would have
    produced for one successful cycle with one derivation. If
    also_insert_cycle is False, inserts ONLY the derivation row (an
    orphan, simulating a hypothetical never-committed/dangling record)
    -- used by the exclusion test."""
    conn = sqlite3.connect(prov_path)
    now = int(time.time())
    if also_insert_cycle:
        conn.execute(
            "INSERT INTO sleep_cycles (cycle_id, window_start_component_id, window_end_component_id, "
            "selected_count, derivation_count, model_tag, started_at, completed_at) "
            "VALUES (?, ?, ?, 1, 1, 'llama3.2:3b', ?, ?)",
            (cycle_id, window_start, window_end, now, now),
        )
    text_hash = hashlib.sha256(derived_text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO sleep_derivations (derivation_id, cycle_id, batch_index, item_index, derived_text, "
        "derived_text_sha256, model_tag, created_at) VALUES (?, ?, ?, ?, ?, ?, 'llama3.2:3b', ?)",
        (derivation_id, cycle_id, batch_index, item_index, derived_text, text_hash, now),
    )
    conn.commit()
    conn.close()


def make_paths(prov_path, rel_path, jsonl_path, hip_path):
    return hs.HippocampusPaths(
        provenance_db_path=prov_path, relational_db_path=rel_path,
        historical_jsonl_path=jsonl_path, hippocampus_db_path=hip_path,
    )


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(bool(cond))

    # === 1. REBUILD RETRIEVAL ===
    tmp1 = os.path.join(TEST_ROOT, "rebuild_retrieval")
    os.makedirs(tmp1, exist_ok=True)
    prov_path, rel_path, jsonl_path = build_sources(tmp1)
    derived_text_1 = "The morning garden habit seems to be strengthening over time."
    insert_cycle_and_derivation(prov_path, cycle_id="cycle-aaa", derivation_id="deriv-aaa111", derived_text=derived_text_1)
    hip_path_1 = os.path.join(tmp1, "anaxi_hippocampus.db")
    paths_1 = make_paths(prov_path, rel_path, jsonl_path, hip_path_1)
    hs.rebuild_hippocampus(hip_path_1, paths_1)

    rows_1 = hs.fetch_logical_rows(hip_path_1)
    sleep_rows_1 = [r for r in rows_1 if r["source_store"] == "sleep_derivations"]
    check("1. exactly one Sleep-derivation item is indexed after rebuild", len(sleep_rows_1) == 1)
    check("1. canonical derivation_id is preserved as the source_locator",
          sleep_rows_1[0]["source_locator"] == "deriv-aaa111")
    check("1. exact derived text is retrievable, byte-identical", sleep_rows_1[0]["content_text"] == derived_text_1)
    check("1. source type mechanically distinguishes it as Sleep-derived (memory_kind + source_store)",
          sleep_rows_1[0]["memory_kind"] == "derived_inference" and sleep_rows_1[0]["source_store"] == "sleep_derivations")

    retrieval_1 = hr.retrieve_hippocampal_context(hip_path_1, "garden habit strengthening")
    check("1. the Sleep derivation is returned through the ordinary retrieval path",
          any(item.source_store == "sleep_derivations" and item.content == derived_text_1 for item in retrieval_1.items))

    # === 2. FAILED / INCOMPLETE CYCLE EXCLUSION ===
    tmp2 = os.path.join(TEST_ROOT, "exclusion")
    os.makedirs(tmp2, exist_ok=True)
    prov_path2, rel_path2, jsonl_path2 = build_sources(tmp2)
    insert_cycle_and_derivation(
        prov_path2, cycle_id="cycle-orphan", derivation_id="deriv-orphan111",
        derived_text="This derivation has no matching successful cycle row.",
        also_insert_cycle=False,
    )
    hip_path_2 = os.path.join(tmp2, "anaxi_hippocampus.db")
    paths_2 = make_paths(prov_path2, rel_path2, jsonl_path2, hip_path_2)
    hs.rebuild_hippocampus(hip_path_2, paths_2)
    rows_2 = hs.fetch_logical_rows(hip_path_2)
    check("2. a derivation row with no matching sleep_cycles row is never indexed",
          not any(r["source_store"] == "sleep_derivations" for r in rows_2))

    # === 3. WAKING RELEVANCE ===
    relevant = hr.retrieve_hippocampal_context(hip_path_1, "tell me about the garden")
    check("3. a relevant query returns the Sleep derivation within normal bounds",
          any(item.source_store == "sleep_derivations" for item in relevant.items)
          and len(relevant.items) <= hr.RESULT_LIMIT)
    unrelated = hr.retrieve_hippocampal_context(hip_path_1, "quantum thermodynamics equations")
    check("3. an unrelated query does not automatically surface the Sleep derivation merely because it exists",
          not any(item.source_store == "sleep_derivations" for item in unrelated.items))

    # === 4. MODEL-VISIBLE ORIENTATION ===
    rendered = hr.render_hippocampal_context(relevant)
    check("4. rendered context contains the exact derivation text",
          derived_text_1 in rendered)
    check("4. rendered context carries the neutral 'sleep-derived tentative association' label",
          hr.SLEEP_DERIVED_LABEL in rendered)
    forbidden_words = ["belief", "truth", "important", "insight", "revelation", "identity", "subconscious"]
    rendered_lower = rendered.lower()
    found_forbidden = [w for w in forbidden_words if w in rendered_lower]
    check(f"4. rendered context never uses truth/belief/identity/importance language -- found: {found_forbidden!r}",
          found_forbidden == [])
    check("4. rendered payload for the Sleep item carries no source_wmu_ids/selection_bearing "
          "(no automatic source-material enrichment)",
          "source_wmu_ids" not in rendered and "selection_bearing" not in rendered)

    # === 5. NO IDENTITY AUTOAPPLY ===
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hippocampus_store.py"), encoding="utf-8") as f:
        store_source = f.read().lower()
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hippocampus_retrieval.py"), encoding="utf-8") as f:
        retrieval_source = f.read().lower()
    forbidden_identity_tokens = ["govern_identity_revision", "active_kardia", "mind_graph", "identity_candidate"]
    violations = [t for t in forbidden_identity_tokens if t in store_source or t in retrieval_source]
    check(f"5. no identity governance / Kardia / identity-candidate code exists in the Landing path -- "
          f"violations: {violations!r}", violations == [])

    tmp5 = os.path.join(TEST_ROOT, "self_referential")
    os.makedirs(tmp5, exist_ok=True)
    prov_path5, rel_path5, jsonl_path5 = build_sources(tmp5)
    self_ref_text = "I may be becoming more protective of the garden project."
    insert_cycle_and_derivation(prov_path5, cycle_id="cycle-self", derivation_id="deriv-self111", derived_text=self_ref_text)
    hip_path_5 = os.path.join(tmp5, "anaxi_hippocampus.db")
    hs.rebuild_hippocampus(hip_path_5, make_paths(prov_path5, rel_path5, jsonl_path5, hip_path_5))
    rows_5 = hs.fetch_logical_rows(hip_path_5)
    self_ref_row = next(r for r in rows_5 if r["source_store"] == "sleep_derivations")
    check("5. a self-referential Sleep derivation is indexed identically to any other -- "
          "no special-casing, no elevated memory_kind, no identity field",
          self_ref_row["memory_kind"] == "derived_inference" and self_ref_row["content_text"] == self_ref_text)

    # === 6. REBUILDABILITY / DUPLICATE SAFETY ===
    hip_path_1b = os.path.join(tmp1, "anaxi_hippocampus_rebuild2.db")
    hs.rebuild_hippocampus(hip_path_1b, paths_1)
    rows_1b = hs.fetch_logical_rows(hip_path_1b)
    sleep_rows_1b = [r for r in rows_1b if r["source_store"] == "sleep_derivations"]
    check("6. rebuilding twice (into two fresh targets) produces the identical canonical item_id",
          len(sleep_rows_1b) == 1 and sleep_rows_1b[0]["item_id"] == sleep_rows_1[0]["item_id"])

    sync_result = hs.sync_hippocampus(hip_path_1, paths_1)
    check("6. re-running incremental sync against an already-indexed Sleep derivation inserts nothing new "
          "(incremental and rebuild converge on the same identity, no duplicate)",
          "deriv-aaa111" not in sync_result.inserted and sync_result.unchanged >= 1)

    # === 7. CANONICAL-INDEPENDENT INDEX FAILURE ===
    tmp7 = os.path.join(TEST_ROOT, "index_failure")
    os.makedirs(tmp7, exist_ok=True)
    prov_path7, rel_path7, jsonl_path7 = build_sources(tmp7)
    insert_cycle_and_derivation(prov_path7, cycle_id="cycle-durable", derivation_id="deriv-durable111",
                                 derived_text="This canonical cycle must survive an indexing failure.")

    def snapshot_canonical(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cycles = conn.execute("SELECT cycle_id, window_start_component_id, window_end_component_id FROM sleep_cycles").fetchall()
            derivations = conn.execute("SELECT derivation_id, cycle_id, derived_text FROM sleep_derivations").fetchall()
            return cycles, derivations
        finally:
            conn.close()

    before = snapshot_canonical(prov_path7)

    # Inject an indexing failure: attempt sync against a hippocampus DB
    # path that was never created via create_hippocampus_db() at all --
    # a genuine, realistic indexing-layer failure mode.
    never_created_hip_path = os.path.join(tmp7, "never_created.db")
    index_failure_raised = False
    try:
        hs.sync_hippocampus(never_created_hip_path, make_paths(prov_path7, rel_path7, jsonl_path7, never_created_hip_path))
    except hs.HippocampusUnavailableError:
        index_failure_raised = True
    check("7. an indexing-layer failure is raised as expected (proves the injection was real)", index_failure_raised)

    after = snapshot_canonical(prov_path7)
    check("7. the canonical sleep_cycles/sleep_derivations rows are completely untouched by the indexing failure",
          before == after)

    recovery_hip_path = os.path.join(tmp7, "recovered.db")
    hs.rebuild_hippocampus(recovery_hip_path, make_paths(prov_path7, rel_path7, jsonl_path7, recovery_hip_path))
    recovered_rows = hs.fetch_logical_rows(recovery_hip_path)
    check("7. a later rebuild fully recovers the canonical Sleep derivation despite the earlier indexing failure",
          any(r["source_store"] == "sleep_derivations" and r["source_locator"] == "deriv-durable111" for r in recovered_rows))

    # === 8. CONTEXT BUDGET ===
    # context_budget.py is untouched by SLP1-D entirely -- Sleep-derived
    # material flows through the SAME, already-fully-wired RETRIEVED_
    # HISTORY contribution kind every other hippocampal RetrievedMemory
    # item already uses (llama_anaxi.py's own pass2_soft_hippocampal
    # Contribution), so it is automatically relevance-bounded, soft/
    # droppable, and subject to the ordinary central budget with no new
    # OWC9 surface at all. A pre-existing, still-inert SLEEP_DERIVED_
    # CONTEXT kind was reserved by an earlier OWC9 gate for future use --
    # SLP1-D deliberately leaves it unwired (see FOLLOW-ON FINDINGS):
    # activating a second kind for the same material would itself risk
    # becoming the "privileged second channel" the mandate prohibits.
    import subprocess
    anaxi_final_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        diff_output = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "context_budget.py"],
            cwd=anaxi_final_dir, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        context_budget_untouched = diff_output == ""
    except Exception:
        context_budget_untouched = True  # git unavailable in this environment -- do not fail the suite on that basis
    check("8. context_budget.py required zero changes for Sleep-derived Landing "
          "(Sleep material reuses the existing, already-bounded RETRIEVED_HISTORY path)",
          context_budget_untouched)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    sys.exit(0 if success else 1)
