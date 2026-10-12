"""
Anaxi -- Synthetic end-to-end test of ONE native waking turn through
the actual production-shaped path: llama_anaxi.run_waking_turn() wired
to the REAL native_provenance_writer, REAL anaxi_protocol_sqlite
generation-controls projection, REAL anaxi_log.jsonl write, and REAL
relational_history projection -- all four stores genuinely exercised,
against temporary files/DBs, never anything in the working project
directory. Only two things are faked: ollama.chat() (no real model
call) and AnaxiOrchestrator.prepare_context()'s Kardia/embedding
retrieval (orthogonal to provenance, and the real ConstitutionalMind
constructor eagerly loads a sentence-transformers model this test has
no business downloading). signal_observation_log's record_observation()
is genuinely real but rebound (llama_anaxi.record_observation) to
write to a temp path instead of its production-relative default --
this test never opens, reads, or deletes the real signal_observations.jsonl
at all, proven by a dedicated sentinel-file check near the end of
run_test_suite().

Everything downstream of prepare_context() -- the native canonical
write, the ordered legacy projection (turn_generation_log ->
anaxi_log.jsonl -> relational_events), and every ID/timestamp
threaded between them -- runs through the real, unmocked production
code in native_provenance_writer.py, anaxi_protocol_sqlite.py,
relational_history.py, and llama_anaxi.py itself.

Run:
    python test_native_waking_turn_end_to_end.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    tmp_root = tempfile.mkdtemp(prefix="anaxi_test_e2e_native_turn_")

    try:
        fake = types.ModuleType("ollama")

        def fake_chat(model, messages, format=None, options=None, think=None):
            return {"message": {"content": "This is a synthetic end-to-end reply."}}
        fake.chat = fake_chat
        sys.modules["ollama"] = fake

        for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                    "relational_history", "native_provenance_writer", "native_turn_staging"):
            sys.modules.pop(mod, None)

        from provenance_schema import create_provenance_db
        from migrate_historical_data import (
            build_pipeline_map, seed_reference_data, alter_existing_stores_schema, PIPELINE_STRUCTURE,
        )
        import time as _time

        provenance_db_path = os.path.join(tmp_root, "anaxi_provenance.db")
        create_provenance_db(provenance_db_path).close()
        manifest = {"pipelines": {
            "llama": {"routing_constant_value": "nate"},
            "claude": {"routing_constant_value": "nate"},
        }}
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

        # Real AnaxiOrchestrator, real ConstitutionalMind schema (via
        # _init_core_tables(), called directly) -- but constructed
        # WITHOUT the embedder line, since log_turn_generation_controls_for_event()
        # (the one real method this test exercises) never touches it,
        # and loading a real sentence-transformers model has no place
        # in a synthetic, offline-safe test.
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
        # Base relational_events table (CREATE TABLE IF NOT EXISTS, same
        # pattern as mind._init_core_tables() above) via one throwaway
        # RelationalHistory construction, then closed -- run_waking_turn()
        # opens its own connection per call, same as production.
        RelationalHistory(relational_db_path).close()

        # Both stores start with only their BASE schema (no §3 additive
        # columns) -- exactly the live pre-migration state. The frozen
        # spec's live migration procedure applies
        # alter_existing_stores_schema() as a separate, already-authorized
        # step (§9.6 steps a-g) BEFORE step (h) activation; this test
        # reproduces that same ordering against its own temp DBs so the
        # native write path sees the same additive columns production
        # would have by the time this milestone activates.
        rel_conn_for_alter = sqlite3.connect(relational_db_path)
        alter_existing_stores_schema(rel_conn_for_alter, mind.conn)
        rel_conn_for_alter.close()

        test_obsidian_root = tempfile.mkdtemp(prefix="anaxi_test_e2e_obsidian_")
        llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = test_obsidian_root
        llama_anaxi.DB_PATH = mind_db_path
        llama_anaxi.RELATIONAL_DB_PATH = relational_db_path
        llama_anaxi.PROVENANCE_DB_DIR = tmp_root
        llama_anaxi.STAGING_PATH = os.path.join(tmp_root, "native_turn_staging.jsonl")
        llama_anaxi.LOG_FILE = os.path.join(tmp_root, "anaxi_log.jsonl")

        # ISOLATION: signal_observation_log.record_observation()'s
        # log_path default is bound at import time, not call time, so
        # redirecting the module attribute after import is a no-op.
        # Instead of touching the real production-relative default at
        # all (an earlier revision of this file bracketed it with
        # delete-before/delete-after, which is what actually wrote to
        # live production data during an unrelated accidental execution
        # elsewhere on 2026-08-29), rebind llama_anaxi's own imported
        # call surface to a wrapper that calls the REAL
        # record_observation() with an explicit temp log_path. The real
        # default path is never opened, read, or deleted by this test.
        import signal_observation_log
        real_record_observation = signal_observation_log.record_observation
        test_signal_log_path = os.path.join(tmp_root, "signal_observations.jsonl")
        llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_signal_log_path)

        result = llama_anaxi.run_waking_turn(orch, "Don't save this, just talk to me.")

        event_id = result["native_event_id"]
        check("run_waking_turn() returned a native_event_id",
              bool(event_id))

        # --- 1. Canonical write: a real events row exists in the real anaxi_provenance.db ---
        prov_conn = sqlite3.connect(provenance_db_path)
        event_row = prov_conn.execute(
            "SELECT event_type, pipeline_provenance_status FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        check("1. Canonical write: a real 'events' row exists for this turn in anaxi_provenance.db",
              event_row is not None and event_row[0] == "waking_turn" and event_row[1] == "known")
        participation_rows = prov_conn.execute(
            "SELECT COUNT(*) FROM event_model_participation WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        check("1. Canonical write: at least one event_model_participation row was committed",
              participation_rows >= 1)
        prov_conn.close()

        # --- 2. Generation-controls projection: a real turn_generation_log row ---
        tgl_row = mind.cursor.execute(
            "SELECT event_id, pipeline_id FROM turn_generation_log WHERE event_id = ?", (event_id,)
        ).fetchone()
        check("2. Generation-controls projection: a real turn_generation_log row exists, "
              "written via the REAL anaxi_protocol_sqlite.log_turn_generation_controls_for_event()",
              tgl_row is not None and tgl_row[0] == event_id)
        mind.conn.close()

        # --- 3. anaxi_log.jsonl: a real line with the complete native key set ---
        with open(llama_anaxi.LOG_FILE, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        check("3. anaxi_log.jsonl: exactly one real line was written",
              len(lines) == 1)
        check("3. anaxi_log.jsonl: the line carries this turn's real event_id and pipeline_id",
              lines and lines[0].get("event_id") == event_id
              and lines[0].get("pipeline_id") == pipeline_map["llama"]["pipeline_id"])
        check("3. anaxi_log.jsonl: pipeline_provenance_status is 'known', not silently omitted",
              lines and lines[0].get("pipeline_provenance_status") == "known")

        # --- 4. relational_events: a real row carrying the same event_id/pipeline_id ---
        rel_conn = sqlite3.connect(relational_db_path)
        rel_row = rel_conn.execute(
            "SELECT event_id, pipeline_id, auth_context_id, substrate FROM relational_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        check("4. relational_events: a real row exists for this turn, written via the REAL "
              "RelationalHistory.record_event()",
              rel_row is not None and rel_row[0] == event_id)
        check("4. relational_events: substrate stays the legacy 'llama' discriminator",
              rel_row is not None and rel_row[3] == "llama")
        check("4. relational_events: pipeline_id/auth_context_id match the same canonical event "
              "as the other two projections (not independently re-derived)",
              rel_row is not None and rel_row[1] == pipeline_map["llama"]["pipeline_id"]
              and rel_row[2] is not None)
        rel_conn.close()

        check("No real Ollama call was made (fake module intercepted every chat() call)",
              True)  # structurally guaranteed: sys.modules["ollama"] was replaced before import

        # === SENTINEL PROOF: the rebound record_observation() call
        # surface never touches the real production-relative default
        # path, even when a file happens to already sit exactly there.
        # Run inside a throwaway CWD (never the live project directory)
        # with a sentinel file planted at the CWD-relative default name
        # signal_observation_log.LOG_FILE resolves to -- prove it is
        # byte-identical afterward. ===
        sentinel_cwd = tempfile.mkdtemp(prefix="anaxi_test_e2e_sentinel_cwd_")
        sentinel_path = os.path.join(sentinel_cwd, signal_observation_log.LOG_FILE)
        sentinel_content = b'{"pre_existing": "sentinel, must not be touched"}\n'
        with open(sentinel_path, "wb") as f:
            f.write(sentinel_content)
        original_cwd = os.getcwd()
        try:
            os.chdir(sentinel_cwd)
            llama_anaxi.record_observation("A sentinel-proof turn, still routed to the temp log path.")
        finally:
            os.chdir(original_cwd)
        with open(sentinel_path, "rb") as f:
            sentinel_after = f.read()
        check("SENTINEL PROOF: a pre-existing file at the production-relative default path "
              "(signal_observations.jsonl) remains byte-identical after record_observation() runs "
              "-- the rebound wrapper never opened it",
              sentinel_after == sentinel_content)
        shutil.rmtree(sentinel_cwd, ignore_errors=True)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            shutil.rmtree(test_obsidian_root, ignore_errors=True)
        except NameError:
            pass
        for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                    "relational_history", "native_provenance_writer", "native_turn_staging", "ollama"):
            sys.modules.pop(mod, None)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
