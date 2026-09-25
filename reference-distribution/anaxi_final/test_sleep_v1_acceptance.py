"""
ANAXI SLP1-E -- final dormant Sleep/REM v1 acceptance.

Proves the CLOSED A/B/C/D stages work together as ONE dormant pipeline,
and that no obsolete legacy path can bypass it. Does not re-prove every
internal invariant A/B/C/D already established individually -- see each
stage's own test suite for that.

Every test uses temporary, synthetic canonical data. No real model call
anywhere in this file (sleep_selection._selection_chat and
sleep_transformation._transformation_chat are monkeypatched to
deterministic fakes). No production database is opened. No Clark wake,
no scheduler run, no genuine Sleep cycle against real data.

Run:
    python test_sleep_v1_acceptance.py
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
import anaxi_sleep
import budget_compliance_registry as registry
import hippocampus_retrieval as hr
import hippocampus_store as hs
import llama_sleep
import sleep_c_schema as schema
import sleep_cycle as cycle_mod
import sleep_evidence as se
import sleep_lease as lease_mod
import sleep_selection
import sleep_transformation as st
import sleep_watermark as watermark_mod
import test_owc9p4_bypass_guard as bypass_guard
import waking_material_unit as wmu_mod

TEST_ROOT = tempfile.mkdtemp(prefix="slp1e_acceptance_")


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


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at, event_type="waking_turn"):
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


def fresh_prov_conn(name):
    """A real canonical provenance DB (via the real create_provenance_db()),
    plus SLP1-C's additive Sleep schema -- exactly the anchored-DB shape
    the authoritative v1 route expects. Lawful synthetic waking material
    only; no production data anywhere."""
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
    conn.execute("PRAGMA foreign_keys = OFF;")  # sleep_c_schema tables reference sleep_cycles, not seeded above
    schema.ensure_schema(conn)
    conn.commit()
    conn.isolation_level = None
    return conn, prov_path, {"pipeline_id": pipeline_id, "host_id": host_id, "clark_id": clark_id}


def add_eligible_turn(conn, ids, event_id, text, sequence=1, occurred_at=100):
    _insert_event(conn, event_id, ids["pipeline_id"], "ac-unk", occurred_at)
    return _insert_component(conn, event_id, sequence, ids["clark_id"], "conversational_prose", text)


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


def build_minimal_hippocampus_sources(tmp_dir, prov_path):
    rel_path = os.path.join(tmp_dir, "anaxi_relational_llama.db")
    jsonl_path = os.path.join(tmp_dir, "anaxi_log.jsonl")
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
    return hs.HippocampusPaths(
        provenance_db_path=prov_path, relational_db_path=rel_path,
        historical_jsonl_path=jsonl_path, hippocampus_db_path=os.path.join(tmp_dir, "anaxi_hippocampus.db"),
    )


def selection_selects_all(messages):
    offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
    return json.dumps({"selected_wmu_ids": offered})


def selection_selects_none(messages):
    return json.dumps({"selected_wmu_ids": []})


def transformation_returns_none(messages):
    return json.dumps({"derivations": []})


# =============================================================== scenarios =


def test_full_positive_pipeline(check):
    """Section 6: the complete synthetic A -> B -> C -> D chain, using
    the REAL production modules throughout, with only the two model
    calls substituted deterministically."""
    conn, prov_path, ids = fresh_prov_conn("full_pipeline")
    tmp_dir = os.path.dirname(prov_path)
    text_a = "The morning garden watering routine happened again today."
    text_b = "I may be becoming more attentive to my own morning routines."  # self-referential
    comp_a = add_eligible_turn(conn, ids, "evt-a", text_a, occurred_at=100)
    comp_b = add_eligible_turn(conn, ids, "evt-b", text_b, occurred_at=200)
    conn.commit()

    derived_text = "A recurring attentiveness to morning routines seems to be emerging."

    def transformation_derives_one(messages):
        offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
        return json.dumps({"derivations": [{"source_wmu_ids": offered, "derived_text": derived_text}]})

    orig_sel = install_selection_chat(selection_selects_all)
    orig_tr = install_transformation_chat(transformation_derives_one)
    try:
        result = cycle_mod.run_sleep_cycle(conn, owner_id="owner-e1", now=1000)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)

    check("full pipeline: cycle completes with the expected selected/derivation counts",
          result.status == "completed" and result.selected_count == 2 and result.derivation_count == 1)

    row = conn.execute(
        "SELECT derived_text FROM sleep_derivations WHERE cycle_id = ?", (result.cycle_id,)
    ).fetchone()
    check("full pipeline: exact derived text committed canonically", row[0] == derived_text)

    source_component_ids = {
        r[0] for r in conn.execute(
            "SELECT source_component_id FROM sleep_derivation_sources "
            "WHERE derivation_id = (SELECT derivation_id FROM sleep_derivations WHERE cycle_id = ?)",
            (result.cycle_id,),
        ).fetchall()
    }
    check("full pipeline: source links point to the actual lawful selection-bearing components, nothing else",
          source_component_ids == {comp_a, comp_b})

    watermark = watermark_mod.read_watermark_from_conn(conn)
    check("full pipeline: watermark agrees with cycle completion",
          watermark["last_processed_component_id"] == result.window_end_component_id
          and watermark["last_successful_cycle_id"] == result.cycle_id)

    lease_row = conn.execute("SELECT 1 FROM sleep_v1_lease WHERE id = 1").fetchone()
    check("full pipeline: lease released after successful completion", lease_row is None)

    # D: Landing -- rebuild the hippocampus from this same canonical DB.
    paths = build_minimal_hippocampus_sources(tmp_dir, prov_path)
    hs.rebuild_hippocampus(paths.hippocampus_db_path, paths)
    retrieval = hr.retrieve_hippocampal_context(paths.hippocampus_db_path, "morning routine attentiveness")
    sleep_items = [i for i in retrieval.items if i.source_store == "sleep_derivations"]
    check("full pipeline: the canonical derivation is indexed and retrievable via a relevant waking query",
          len(sleep_items) == 1 and sleep_items[0].content == derived_text)

    rendered = hr.render_hippocampal_context(retrieval)
    check("full pipeline: waking rendering carries the neutral Sleep-derived label and exact text",
          hr.SLEEP_DERIVED_LABEL in rendered and derived_text in rendered)
    check("full pipeline: waking rendering never reopens the original WMU source text",
          text_a not in rendered.split(hr.SLEEP_DERIVED_LABEL)[-1].split("\n")[1]
          if hr.SLEEP_DERIVED_LABEL in rendered else False)
    check("full pipeline: rendering carries no truth/belief/identity language",
          not any(w in rendered.lower() for w in ("belief", "truth", "important", "identity", "revelation")))

    # No identity/Kardia state exists anywhere this pipeline could have
    # touched -- there is no active_kardia table/file this codebase's
    # Sleep-v1 modules could have written to at all (structural, see
    # test_identity_kardia_owc9_and_anti_recursion below for the
    # importable-surface proof); the self-referential derivation above
    # persisted as an ordinary derivation row, nothing more.


def test_zero_content_paths(check):
    """Section 7: B returns zero selected WMUs, and separately, C
    returns zero derivations. Both must complete successfully, advance
    the watermark, and fabricate nothing."""
    # Case A: Selection selects nothing.
    conn_a, _, ids_a = fresh_prov_conn("zero_selection")
    add_eligible_turn(conn_a, ids_a, "evt-a", "Some ordinary waking sentence.", occurred_at=100)
    conn_a.commit()
    orig_sel = install_selection_chat(selection_selects_none)
    orig_tr = install_transformation_chat(lambda m: (_ for _ in ()).throw(AssertionError("Transformation must not be called")))
    try:
        result_a = cycle_mod.run_sleep_cycle(conn_a, owner_id="owner-za", now=1000)
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)
    check("zero paths: B-zero completes successfully with no fabricated derivation",
          result_a.status == "completed" and result_a.selected_count == 0 and result_a.derivation_count == 0)
    check("zero paths: B-zero still advances the watermark through the processed window",
          watermark_mod.read_watermark_from_conn(conn_a)["last_processed_component_id"] == result_a.window_end_component_id)

    # Case B: Selection selects something, Transformation derives nothing.
    conn_b, _, ids_b = fresh_prov_conn("zero_transformation")
    add_eligible_turn(conn_b, ids_b, "evt-b", "Another ordinary waking sentence.", occurred_at=100)
    conn_b.commit()
    orig_sel2 = install_selection_chat(selection_selects_all)
    orig_tr2 = install_transformation_chat(transformation_returns_none)
    try:
        result_b = cycle_mod.run_sleep_cycle(conn_b, owner_id="owner-zb", now=1000)
    finally:
        restore_selection_chat(orig_sel2)
        restore_transformation_chat(orig_tr2)
    check("zero paths: C-zero completes successfully with a real selected_count but zero derivations",
          result_b.status == "completed" and result_b.selected_count == 1 and result_b.derivation_count == 0)
    check("zero paths: C-zero still advances the watermark",
          watermark_mod.read_watermark_from_conn(conn_b)["last_processed_component_id"] == result_b.window_end_component_id)


def test_stale_owner_fencing_at_pipeline_level(check):
    """Section 8: owner A begins a cycle; owner B steals the lease
    (generation increments) mid-cycle, during A's own Selection model
    call; A's subsequent pre-Transformation lease renewal must then
    fail, the whole cycle must fail with zero canonical trace, A cannot
    clear B's lease, and B can independently complete."""
    conn, _, ids = fresh_prov_conn("fencing")
    add_eligible_turn(conn, ids, "evt-a", "Waking material for the fencing scenario.", occurred_at=100)
    conn.commit()

    stolen = {}

    def selection_chat_that_steals_lease(messages):
        offered = re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
        handle = lease_mod.acquire_lease(conn, owner_id="owner-thief", now=2000)
        assert handle is not None, "test setup: the thief must successfully steal the lease"
        stolen["handle"] = handle
        return json.dumps({"selected_wmu_ids": offered})

    orig_sel = install_selection_chat(selection_chat_that_steals_lease)
    orig_tr = install_transformation_chat(lambda m: (_ for _ in ()).throw(AssertionError("Transformation must not be reached")))
    raised = False
    try:
        cycle_mod.run_sleep_cycle(conn, owner_id="owner-a", now=1000, lease_duration_seconds=1)
    except cycle_mod.SleepCycleFailure:
        raised = True
    finally:
        restore_selection_chat(orig_sel)
        restore_transformation_chat(orig_tr)

    check("fencing: A's cycle fails once its lease is stolen mid-Selection", raised)
    check("fencing: no cycle was ever committed by the fenced-out owner",
          conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0] == 0)
    check("fencing: watermark remains untouched", watermark_mod.read_watermark_from_conn(conn) == watermark_mod.INITIAL_WATERMARK_STATE)
    lease_row = conn.execute("SELECT owner_id, generation FROM sleep_v1_lease WHERE id = 1").fetchone()
    check("fencing: A's failed release attempt could not clear B's lease -- B's lease is still intact",
          lease_row == ("owner-thief", stolen["handle"].generation))

    completed_cycle_id = cycle_mod._commit_successful_cycle(
        conn, owner_id="owner-thief", generation=stolen["handle"].generation, now=2000,
        window_start_component_id=0, window_end_component_id=1, selected_count=0, derivations=[], id_to_wmu={},
    )
    check("fencing: the newer owner can independently complete a cycle",
          conn.execute("SELECT COUNT(*) FROM sleep_cycles WHERE cycle_id = ?", (completed_cycle_id,)).fetchone()[0] == 1)


def test_private_boundary_and_no_source_reopening(check):
    """Section 9: the authoritative v1 path has no private-workspace
    source or retrieval side door anywhere in its own source."""
    v1_modules = [
        "sleep_evidence.py", "waking_material_unit.py", "sleep_selection.py",
        "sleep_transformation.py", "sleep_cycle.py", "sleep_lease.py",
        "sleep_watermark.py", "sleep_c_schema.py", "hippocampus_store.py",
        "hippocampus_retrieval.py",
    ]
    forbidden_imports = ["import workspace_private", "from workspace_private"]
    violations = []
    for fname in v1_modules:
        with open(os.path.join(ANAXI_FINAL, fname), "r", encoding="utf-8") as f:
            src = f.read()
        for token in forbidden_imports:
            if token in src:
                violations.append((fname, token))
    check(f"private boundary: no Sleep-v1 module imports the private-workspace reader -- "
          f"violations: {violations!r}", violations == [])


def test_legacy_disposition_and_one_authoritative_entrypoint(check):
    """Sections 2-5: exactly one authoritative Sleep v1 route, both
    legacy semantic Sleep pipelines are unreachable via their own CLIs,
    the shared JSON transport wrapper remains usable, and the
    authoritative route always resolves the anchored canonical DB, never
    a CWD-relative path."""
    # anaxi_sleep.py (Claude substrate) -- already disabled at main()'s
    # very first line (SLP1-D1B, pre-existing); cheap functional re-check.
    old_argv = sys.argv
    sys.argv = ["anaxi_sleep.py"]
    try:
        try:
            anaxi_sleep.main()
            exit_code = None
        except SystemExit as e:
            exit_code = e.code
    finally:
        sys.argv = old_argv
    check("legacy: anaxi_sleep.py's CLI fails closed", exit_code == 1)

    # llama_sleep.py (local-Llama substrate) -- SLP1-E's own new guard.
    class _PoisonOrchestratorConstructed(Exception):
        pass

    def _poison_orchestrator(*args, **kwargs):
        raise _PoisonOrchestratorConstructed("AnaxiOrchestrator must never be constructed by the disabled CLI")

    old_argv2 = sys.argv
    old_orch = llama_sleep.AnaxiOrchestrator
    sys.argv = ["llama_sleep.py"]
    llama_sleep.AnaxiOrchestrator = _poison_orchestrator
    exit_code2 = None
    orchestrator_reached = False
    try:
        try:
            llama_sleep.main()
        except SystemExit as e:
            exit_code2 = e.code
        except _PoisonOrchestratorConstructed:
            orchestrator_reached = True
    finally:
        sys.argv = old_argv2
        llama_sleep.AnaxiOrchestrator = old_orch
    check("legacy: llama_sleep.py's CLI fails closed without ever constructing AnaxiOrchestrator",
          exit_code2 == 1 and not orchestrator_reached)

    # The actual semantic entrypoint itself (run_sleep()), not merely
    # its CLI wrapper, must independently fail closed -- disabling only
    # main() would leave run_sleep() directly callable by any Python
    # caller, which is the exact gap this scenario now closes.
    def _poison_ask_llama_for_json(messages):
        raise AssertionError("ask_llama_for_json must never be called -- run_sleep() is disabled")

    old_ask = llama_sleep.ask_llama_for_json
    llama_sleep.ask_llama_for_json = _poison_ask_llama_for_json
    run_sleep_raised = False
    try:
        llama_sleep.run_sleep(orch=None, receipts=None)  # must fail before touching either argument
    except RuntimeError as e:
        run_sleep_raised = "LEGACY_SLEEP_DISABLED" in str(e)
    finally:
        llama_sleep.ask_llama_for_json = old_ask
    check("legacy: llama_sleep.run_sleep() itself (not just main()) fails closed before any legacy "
          "semantic work, receipt write, or model call",
          run_sleep_raised)

    check("legacy: llama_sleep.run_sleep/list_proposals/resolve/reconsider remain defined on disk "
          "(unreachable via CLI, not deleted)",
          all(callable(getattr(llama_sleep, n)) for n in ("run_sleep", "list_proposals", "resolve", "reconsider")))

    check("legacy: the approved JSON transport wrapper remains usable and unrelated to the disabled CLI",
          callable(llama_sleep.ask_llama_for_json) and llama_sleep.MODEL == "llama3.2:3b")

    # One authoritative v1 route, anchored (never CWD-relative).
    check("authoritative route: sleep_cycle.run_sleep_cycle_with_production_defaults exists and is callable",
          callable(cycle_mod.run_sleep_cycle_with_production_defaults))
    resolved_path = schema.SleepCPaths.production_defaults().provenance_db_path
    check("authoritative route: the resolved canonical DB path is absolute and anchored to this file's own "
          "directory, never the current working directory",
          os.path.isabs(resolved_path) and os.path.dirname(resolved_path) == ANAXI_FINAL)

    with open(os.path.join(ANAXI_FINAL, "sleep_cycle.py"), encoding="utf-8") as f:
        sleep_cycle_source = f.read()
    check("authoritative route: sleep_cycle.py itself never hardcodes a bare relative provenance-db filename",
          '"anaxi_provenance.db"' not in sleep_cycle_source)

    # SLP2 intentionally activated the formerly-dormant authoritative
    # route through explicit owner-operated surfaces.  Preserve the
    # original anti-bypass intent by checking the exact production
    # caller set rather than asserting the superseded dormant state.
    production_callers = {
        f for f in os.listdir(ANAXI_FINAL)
        if f.endswith(".py") and "test" not in f.lower() and f != "sleep_cycle.py"
        and "run_sleep_cycle_with_production_defaults" in open(
            os.path.join(ANAXI_FINAL, f), encoding="utf-8"
        ).read()
    }
    check("authoritative route: SLP2 exposes exactly the CLI and ordinary owner GUI callers",
          production_callers == {"slp2_cli.py", "llama_gui.py"})


def test_identity_kardia_owc9_and_anti_recursion_boundaries(check):
    """Sections 10-12: no Sleep-v1 module can reach identity/Kardia
    installation; sleep_selection/sleep_transformation remain the only
    new OWC9-budgeted inference pathways with no unbudgeted side door;
    Sleep-derived content is still ineligible as fresh A waking material."""
    v1_modules = [
        "sleep_evidence.py", "waking_material_unit.py", "sleep_selection.py",
        "sleep_transformation.py", "sleep_cycle.py", "sleep_lease.py",
        "sleep_watermark.py", "sleep_c_schema.py", "hippocampus_store.py",
        "hippocampus_retrieval.py",
    ]
    forbidden = ["govern_identity_revision", "active_kardia", "mind_graph", "ConstitutionalMind"]
    violations = []
    for fname in v1_modules:
        with open(os.path.join(ANAXI_FINAL, fname), "r", encoding="utf-8") as f:
            src = f.read().lower()
        for token in forbidden:
            if token.lower() in src:
                violations.append((fname, token))
    check(f"identity: no Sleep-v1 module references identity governance, Kardia, or mind-graph -- "
          f"violations: {violations!r}", violations == [])

    # Direct ollama.chat() calls: reuse the bypass guard's own real AST
    # scanner (find_chat_calls), never a raw substring match -- a raw
    # substring match would false-positive on this very module's own
    # explanatory comments/docstrings ("no new physical ollama.chat()
    # call site").
    chat_call_violations = []
    for fname in v1_modules:
        full_path = os.path.join(ANAXI_FINAL, fname)
        found = bypass_guard._find_chat_calls(full_path)
        if found:
            chat_call_violations.append((fname, found))
    check(f"OWC9: no Sleep-v1 module contains a direct ollama.chat() call (AST-verified) -- "
          f"violations: {chat_call_violations!r}", chat_call_violations == [])

    # OWC9 bypass proof -- run once, reusing the existing focused guard.
    ollama_call_sites = registry.WRAPPER_CALL_SITES
    check("OWC9: sleep_selection is registered and admitted through the approved wrapper",
          any(r["path_id"] == "sleep_selection" and r["wrapper_call_site"] in ollama_call_sites for r in registry.ALL_PATHS))
    check("OWC9: sleep_transformation is registered and admitted through the approved wrapper",
          any(r["path_id"] == "sleep_transformation" and r["wrapper_call_site"] in ollama_call_sites for r in registry.ALL_PATHS))
    bypass_guard_ok = True
    try:
        bypass_guard.test_no_unclassified_production_inference_call_sites()
    except AssertionError:
        bypass_guard_ok = False
    check("OWC9: the existing focused bypass guard finds no new/unclassified physical chat() call site "
          "anywhere in production source", bypass_guard_ok)

    # Anti-recursion.
    check("anti-recursion: Sleep-derived content still cannot satisfy A's positive waking-material allowlist",
          not any("sleep_derivation" in str(t) or "sleep_cycle" in str(t) for t in se.ELIGIBLE_SELECTION_TUPLES))
    check("anti-recursion: sleep_evidence.py never queries its own Sleep-output tables as an input source",
          "sleep_derivations" not in open(os.path.join(ANAXI_FINAL, "sleep_evidence.py"), encoding="utf-8").read()
          and "sleep_cycles" not in open(os.path.join(ANAXI_FINAL, "sleep_evidence.py"), encoding="utf-8").read())


ALL_TESTS = [
    test_full_positive_pipeline,
    test_zero_content_paths,
    test_stale_owner_fencing_at_pipeline_level,
    test_private_boundary_and_no_source_reopening,
    test_legacy_disposition_and_one_authoritative_entrypoint,
    test_identity_kardia_owc9_and_anti_recursion_boundaries,
]


def main():
    results = []
    failures = []

    def check(name, cond):
        ok = bool(cond)
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        results.append(ok)

    for t in ALL_TESTS:
        try:
            t(check)
        except Exception:
            results.append(False)
            tb = traceback.format_exc()
            failures.append((t.__name__, tb))
            print(f"FAIL (exception) {t.__name__}:\n{tb}")

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    if failures:
        for name, tb in failures:
            print(f"--- {name} ---\n{tb}")
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
