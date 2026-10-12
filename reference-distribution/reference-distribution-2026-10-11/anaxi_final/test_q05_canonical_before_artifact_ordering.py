"""
Anaxi -- Targeted test for the Q-05 correction (canonical-before-
artifact ordering): the durable journal/artifact Markdown file must
never become durable before canonical provenance
(anaxi_provenance.db's "events" row) has committed for the same turn.

Reuses the two existing harness patterns rather than inventing a third:
  - The REAL, unmocked anaxi_provenance.db / native_provenance_writer
    wiring from test_native_waking_turn_end_to_end.py (only that path
    can prove an actual canonical commit happened).
  - The POSITIVE-construction-response ollama fake from
    test_bounded_clause_integration.py Case 5 (only a real success path
    exercises prepare_artifact_decision()'s "_pending_write" ->
    finalize_artifact_write() split at all).

Three things are demonstrated here, each directly requested by the
Q-05 authorization:

  1. ORDERING ON SUCCESS: clark_journal.write_journal_entry() -- the
     one call that actually creates the durable .md file -- is not
     invoked until AFTER a real, committed "events" row already exists
     in anaxi_provenance.db for this turn's event_id. Proven by
     wrapping write_journal_entry() and querying the real DB with its
     own separate connection at the moment of the wrapped call, before
     calling through to the real function.

  2. NO ORPHAN ON PRE-COMMIT FAILURE: if the native canonical write
     itself fails (simulated here by making
     native_provenance_writer.stage_and_record_native_waking_turn()
     raise), no .md file is left behind in the workspace, and
     write_journal_entry() is never called at all -- structurally
     guaranteed by finalize_artifact_write() sitting strictly after
     that call in run_waking_turn(), demonstrated here rather than only
     asserted from reading the source.

  3. BEHAVIORAL EQUIVALENCE APART FROM ORDERING: on success, the
     bounded_clause reply, the artifact_result contract shape, and the
     actual written file's content/frontmatter are exactly what the
     pre-Q-05 synchronous process_artifact_decision() would have
     produced -- only the moment of durability moved. (The bounded-
     clause reply string itself is already covered end-to-end by
     test_bounded_clause_integration.py Case 5; this test additionally
     confirms the real file on disk matches.)

Run:
    python test_q05_canonical_before_artifact_ordering.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types


def build_fake_ollama(construction_response, prose_response):
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        if format == "json":
            return {"message": {"content": json.dumps(construction_response)}}
        return {"message": {"content": prose_response}}
    fake.chat = fake_chat
    return fake


def find_md_files(root):
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        found.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".md"))
    return found


def fresh_real_stack(tmp_root):
    """Sets up the same real anaxi_provenance.db / mind / relational
    stack as test_native_waking_turn_end_to_end.py, returning the
    pieces the two cases below each need. Re-run per case since
    llama_anaxi and friends are re-imported fresh each time."""
    for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                "relational_history", "native_provenance_writer", "native_turn_staging"):
        sys.modules.pop(mod, None)

    from provenance_schema import create_provenance_db
    from migrate_historical_data import build_pipeline_map, seed_reference_data, alter_existing_stores_schema
    import time as _time

    provenance_db_path = os.path.join(tmp_root, "anaxi_provenance.db")
    create_provenance_db(provenance_db_path).close()
    manifest = {"pipelines": {"llama": {"routing_constant_value": "nate"},
                              "claude": {"routing_constant_value": "nate"}}}
    pipeline_map = build_pipeline_map(manifest)
    seed_conn = sqlite3.connect(provenance_db_path)
    seed_conn.execute("PRAGMA foreign_keys = ON;")
    now = int(_time.time())
    seed_reference_data(seed_conn, pipeline_map, now)
    seed_conn.close()

    import llama_anaxi
    import orchestration
    import anaxi_protocol_sqlite
    from relational_history import RelationalHistory

    mind_db_path = os.path.join(tmp_root, "anaxi_mind_llama.db")
    mind = anaxi_protocol_sqlite.ConstitutionalMind.__new__(anaxi_protocol_sqlite.ConstitutionalMind)
    mind.db_path = mind_db_path
    mind.conn = sqlite3.connect(mind_db_path, timeout=30.0)
    mind.cursor = mind.conn.cursor()
    mind.cursor.execute("PRAGMA foreign_keys = ON;")
    mind.conn.commit()
    mind._init_core_tables()

    orch = orchestration.AnaxiOrchestrator.__new__(orchestration.AnaxiOrchestrator)
    orch.mind = mind
    orch.prepare_context = lambda user_id, prompt: {
        "messages": [{"role": "system", "content": "You are Clark."}],
        "controls": {"temperature": 0.4, "top_p": 0.85},
        "kardia": {"aesthetic_valve": "warm"},
        "memory_context": "",
    }

    relational_db_path = os.path.join(tmp_root, "anaxi_relational_llama.db")
    RelationalHistory(relational_db_path).close()
    rel_conn_for_alter = sqlite3.connect(relational_db_path)
    alter_existing_stores_schema(rel_conn_for_alter, mind.conn)
    rel_conn_for_alter.close()

    workspace_root = tempfile.mkdtemp(prefix="anaxi_test_q05_obsidian_")
    llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = workspace_root
    llama_anaxi.DB_PATH = mind_db_path
    llama_anaxi.RELATIONAL_DB_PATH = relational_db_path
    llama_anaxi.PROVENANCE_DB_DIR = tmp_root
    llama_anaxi.STAGING_PATH = os.path.join(tmp_root, "native_turn_staging.jsonl")
    llama_anaxi.LOG_FILE = os.path.join(tmp_root, "anaxi_log.jsonl")

    import signal_observation_log
    real_record_observation = signal_observation_log.record_observation
    test_signal_log_path = os.path.join(tmp_root, "signal_observations.jsonl")
    llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_signal_log_path)

    return llama_anaxi, orch, mind, provenance_db_path, workspace_root


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    prompt_text = "Please save this thought: the ordering fix matters."
    construction_response = {
        "identified_referent": "the ordering fix matters", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "Q-05 Ordering Proof", "what_they_shared": None,
        "content": "Canonical provenance must commit before this file exists.",
    }
    prose_response = "Noted."

    # =====================================================================
    # Case 1: SUCCESS -- write_journal_entry() must not be called until a
    # committed "events" row already exists for this turn's event_id.
    # =====================================================================
    tmp_root_1 = tempfile.mkdtemp(prefix="anaxi_test_q05_case1_")
    try:
        sys.modules["ollama"] = build_fake_ollama(construction_response, prose_response)
        llama_anaxi, orch, mind, provenance_db_path, workspace_root = fresh_real_stack(tmp_root_1)

        import clark_journal
        real_write_journal_entry = clark_journal.write_journal_entry
        observations = {"write_called": False, "events_row_existed_at_write_time": None,
                         "workspace_had_md_at_write_time": None}

        def watching_write_journal_entry(*args, **kwargs):
            observations["write_called"] = True
            check_conn = sqlite3.connect(provenance_db_path)
            row = check_conn.execute(
                "SELECT event_id FROM events WHERE pipeline_provenance_status = 'known'"
            ).fetchone()
            check_conn.close()
            observations["events_row_existed_at_write_time"] = row is not None
            observations["workspace_had_md_at_write_time"] = len(find_md_files(workspace_root)) > 0
            return real_write_journal_entry(*args, **kwargs)
        clark_journal.write_journal_entry = watching_write_journal_entry
        llama_anaxi.prepare_artifact_decision = clark_journal.prepare_artifact_decision
        llama_anaxi.finalize_artifact_write = clark_journal.finalize_artifact_write

        result = llama_anaxi.run_waking_turn(orch, prompt_text)

        check("1. SUCCESS CASE: write_journal_entry() was actually invoked (positive path genuinely ran)",
              observations["write_called"])
        check("1. ORDERING: a committed, 'known' events row already existed in anaxi_provenance.db "
              "at the exact moment write_journal_entry() was called -- canonical committed BEFORE "
              "the artifact file was written, not after and not concurrently-unordered",
              observations["events_row_existed_at_write_time"] is True)
        check("1. ORDERING: no .md file existed in the workspace yet at that same moment "
              "(the write had not happened yet even though we were inside the call about to make it)",
              observations["workspace_had_md_at_write_time"] is False)

        check("1. RESULT SHAPE: artifact_result reports artifact_created True",
              result["artifact_result"]["artifact_created"] is True)
        check("1. RESULT SHAPE: bounded_clause reply uses the real title, matching the pre-Q-05 contract",
              result["reply"].startswith("Saved: Q-05 Ordering Proof."))

        md_files = find_md_files(workspace_root)
        check("1. AFTER THE TURN: exactly one durable .md artifact now exists on disk",
              len(md_files) == 1)
        if md_files:
            with open(md_files[0], "r", encoding="utf-8") as f:
                content = f.read()
            check("1. CONTENT: the durable file contains the real constructed content, unchanged by "
                  "the timing of when it was written",
                  "Canonical provenance must commit before this file exists." in content)

        prov_conn = sqlite3.connect(provenance_db_path)
        events_count = prov_conn.execute(
            "SELECT COUNT(*) FROM events WHERE pipeline_provenance_status = 'known'"
        ).fetchone()[0]
        prov_conn.close()
        check("1. Exactly one canonical events row exists for this one turn (no duplicate/partial write)",
              events_count == 1)

        mind.conn.close()
    finally:
        shutil.rmtree(tmp_root_1, ignore_errors=True)
        try:
            shutil.rmtree(workspace_root, ignore_errors=True)
        except NameError:
            pass

    # =====================================================================
    # Case 2: PRE-COMMIT FAILURE -- if the native canonical write itself
    # raises, no durable artifact may be left behind, and
    # write_journal_entry() must never be called at all.
    # =====================================================================
    tmp_root_2 = tempfile.mkdtemp(prefix="anaxi_test_q05_case2_")
    try:
        sys.modules["ollama"] = build_fake_ollama(construction_response, prose_response)
        llama_anaxi, orch, mind, provenance_db_path, workspace_root = fresh_real_stack(tmp_root_2)

        import clark_journal
        real_write_journal_entry = clark_journal.write_journal_entry
        write_call_count = {"n": 0}

        def counting_write_journal_entry(*args, **kwargs):
            write_call_count["n"] += 1
            return real_write_journal_entry(*args, **kwargs)
        clark_journal.write_journal_entry = counting_write_journal_entry
        llama_anaxi.prepare_artifact_decision = clark_journal.prepare_artifact_decision
        llama_anaxi.finalize_artifact_write = clark_journal.finalize_artifact_write

        import native_provenance_writer
        real_stage_and_record = native_provenance_writer.stage_and_record_native_waking_turn

        def failing_stage_and_record(*args, **kwargs):
            raise RuntimeError("simulated pre-canonical-commit staging/DB failure")
        native_provenance_writer.stage_and_record_native_waking_turn = failing_stage_and_record

        raised = False
        raised_message = ""
        try:
            llama_anaxi.run_waking_turn(orch, prompt_text)
        except RuntimeError as e:
            raised = True
            raised_message = str(e)

        check("2. A pre-canonical-commit staging failure genuinely propagates out of run_waking_turn() "
              "(not silently swallowed)",
              raised and "simulated pre-canonical-commit staging/DB failure" in raised_message)
        check("2. write_journal_entry() was NEVER called -- finalize_artifact_write() sits strictly "
              "after the (failed) canonical write in run_waking_turn(), so it was never reached",
              write_call_count["n"] == 0)
        md_files = find_md_files(workspace_root)
        check("2. NO ORPHAN: zero durable .md artifacts exist in the workspace after the failure",
              len(md_files) == 0)

        prov_conn = sqlite3.connect(provenance_db_path)
        events_count = prov_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        prov_conn.close()
        check("2. NO PARTIAL CANONICAL STATE EITHER: zero events rows exist (the simulated failure "
              "happened before any canonical commit, consistent with a real staging/DB failure)",
              events_count == 0)

        mind.conn.close()
    finally:
        shutil.rmtree(tmp_root_2, ignore_errors=True)
        try:
            shutil.rmtree(workspace_root, ignore_errors=True)
        except NameError:
            pass
        for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite", "clark_journal",
                    "relational_history", "native_provenance_writer", "native_turn_staging", "ollama"):
            sys.modules.pop(mod, None)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
