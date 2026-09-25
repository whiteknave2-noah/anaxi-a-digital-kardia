"""
Anaxi -- SLP1-A4c synthetic end-to-end proof that the human-waking-
authority ordering law actually holds through the REAL production call
path: llama_anaxi.run_waking_turn() wired to the REAL
native_provenance_writer, REAL human_session_binding, REAL
hir1_registration (for a synthetic/temp registered human -- never
production), all against temporary files/DBs, never anything in the
working project directory.

Only ollama.chat() is faked (no real model call) -- exactly the same
isolation technique test_native_waking_turn_end_to_end.py already
established and this file directly reuses. The fake records every
invocation and checks, AT THE MOMENT OF EACH CALL, whether the
canonical human_waking_input event H already exists in the real
provenance DB -- this is the actual proof that H commits before the
model is ever invoked, not merely an assertion about final state.

Four scenarios:
  A. Valid authority -> H commits before model call, X links to H.
  B. No authority -> today's unbound behavior, unchanged.
  C. Invalid authority (wrong session) -> falls back to unbound, no error.
  D. Valid authority but a forced H-write failure -> the whole call
     aborts BEFORE any model call (call count stays 0), and no X is
     ever written either.

Run:
    python test_human_waking_authority_end_to_end.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types


def _fresh_env(tmp_root):
    """Sets up one fresh, isolated environment: real canonical schema,
    real pipeline/reference-data seeding, a real synthetic registered
    human (via the REAL hir1_registration writer), and a freshly
    reloaded llama_anaxi module with every path constant redirected to
    this tmp_root. Mirrors test_native_waking_turn_end_to_end.py's own
    established isolation technique exactly."""
    fake = types.ModuleType("ollama")
    model_call_log = []

    provenance_db_path = os.path.join(tmp_root, "anaxi_provenance.db")

    def fake_chat(model, messages, format=None, options=None, think=None):
        conn = sqlite3.connect(provenance_db_path)
        h_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'"
        ).fetchone()[0]
        conn.close()
        model_call_log.append({"h_exists_at_call_time": h_count > 0})
        return {"message": {"content": "This is a synthetic end-to-end reply."}}

    fake.chat = fake_chat
    sys.modules["ollama"] = fake

    for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                "relational_history", "native_provenance_writer", "native_turn_staging",
                "human_session_binding"):
        sys.modules.pop(mod, None)

    from provenance_schema import create_provenance_db, derive_stable_id
    from migrate_historical_data import build_pipeline_map, seed_reference_data
    import hir1_schema_migration
    from hir1_registration import register_canonical_human, HOST_ACTOR_ID

    create_provenance_db(provenance_db_path).close()
    manifest = {"pipelines": {
        "llama": {"routing_constant_value": "nate"},
        "claude": {"routing_constant_value": "nate"},
    }}
    pipeline_map = build_pipeline_map(manifest)
    seed_conn = sqlite3.connect(provenance_db_path)
    seed_conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    seed_reference_data(seed_conn, pipeline_map, now)
    seed_conn.close()

    hir1_schema_migration.apply_additive_migration(provenance_db_path)

    # A real, SYNTHETIC registered human actor via the REAL production
    # writer -- never production data. seed_reference_data() already
    # seeded the clark_agent and host_system actors (including
    # HOST_ACTOR_ID, which hir1_registration's own event_requesters
    # write depends on).
    reg_conn = sqlite3.connect(provenance_db_path)
    reg_conn.row_factory = sqlite3.Row
    reg_conn.execute("PRAGMA foreign_keys = ON;")
    reg_result, reg_failure = register_canonical_human(reg_conn, {
        "registration_request_id": "e2e-req-1",
        "aab_actor_id": "actor-e2e-synthetic-human",
        "display_label": "Synthetic E2E Human",
        "source": "local_operator_provisioning",
    })
    reg_conn.close()
    assert reg_failure is None, reg_failure
    registered_actor_id = reg_result["actor_id"]

    import llama_anaxi
    import orchestration
    import anaxi_protocol_sqlite
    import human_session_binding
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

    from migrate_historical_data import alter_existing_stores_schema
    rel_conn_for_alter = sqlite3.connect(relational_db_path)
    alter_existing_stores_schema(rel_conn_for_alter, mind.conn)
    rel_conn_for_alter.close()

    test_obsidian_root = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_obsidian_")
    llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = test_obsidian_root
    llama_anaxi.DB_PATH = mind_db_path
    llama_anaxi.RELATIONAL_DB_PATH = relational_db_path
    llama_anaxi.PROVENANCE_DB_DIR = tmp_root
    llama_anaxi.STAGING_PATH = os.path.join(tmp_root, "native_turn_staging.jsonl")
    llama_anaxi.LOG_FILE = os.path.join(tmp_root, "anaxi_log.jsonl")

    import signal_observation_log
    real_record_observation = signal_observation_log.record_observation
    test_signal_log_path = os.path.join(tmp_root, "signal_observations.jsonl")
    llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_signal_log_path)

    return {
        "llama_anaxi": llama_anaxi, "orch": orch, "mind": mind,
        "provenance_db_path": provenance_db_path, "model_call_log": model_call_log,
        "registered_actor_id": registered_actor_id, "human_session_binding": human_session_binding,
        "test_obsidian_root": test_obsidian_root,
    }


def _teardown_env(env):
    try:
        env["mind"].conn.close()
    except Exception:
        pass
    shutil.rmtree(env["test_obsidian_root"], ignore_errors=True)
    for mod in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                "relational_history", "native_provenance_writer", "native_turn_staging",
                "human_session_binding", "ollama"):
        sys.modules.pop(mod, None)


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    # ================================================== Scenario A: valid authority
    tmp_root_a = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_a_")
    try:
        env = _fresh_env(tmp_root_a)
        llama_anaxi = env["llama_anaxi"]
        session_id = llama_anaxi.get_current_session_id()
        session_started_at = llama_anaxi.get_current_session_started_at()

        bind_conn = sqlite3.connect(env["provenance_db_path"])
        bind_conn.execute("PRAGMA foreign_keys = ON;")
        authority = env["human_session_binding"].bind_session_to_registered_human(
            bind_conn, session_id=session_id, session_started_at=session_started_at,
            pipeline_key=llama_anaxi.PIPELINE_KEY, actor_id=env["registered_actor_id"],
        )
        bind_conn.close()

        result = llama_anaxi.run_waking_turn(
            env["orch"], "Don't save this, just talk to me.", human_input_authority=authority,
        )
        x_event_id = result["native_event_id"]

        check("A. run_waking_turn() returned a native_event_id", bool(x_event_id))
        check("A. at least one model call occurred", len(env["model_call_log"]) >= 1)
        check("A. H existed in canonical provenance at EVERY model call this turn",
              all(c["h_exists_at_call_time"] for c in env["model_call_log"]))

        conn = sqlite3.connect(env["provenance_db_path"])
        h_rows = conn.execute(
            "SELECT e.event_id, c.component_text, c.creator_actor_id, c.authorship_resolution "
            "FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'human_waking_input'"
        ).fetchall()
        check("A. exactly one canonical human_waking_input event H exists", len(h_rows) == 1)
        h_event_id, h_text, h_creator, h_authorship = h_rows[0]
        check("A. H's component_text is EXACTLY the submitted message, verbatim",
              h_text == "Don't save this, just talk to me.")
        check("A. H's creator_actor_id is the registered human actor",
              h_creator == env["registered_actor_id"])
        check("A. H's authorship_resolution is 'resolved'", h_authorship == "resolved")

        link_row = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = 'human_input_event_id'",
            (x_event_id,),
        ).fetchone()
        check("A. Clark's waking_turn X carries a human_input_event_id component naming H",
              link_row is not None and link_row[0] == h_event_id)
        conn.close()
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_a, ignore_errors=True)

    # ================================================== Scenario B: no authority
    # SLP1-A4c checkpoint correction: the session IS genuinely bound to
    # the registered human P before this call -- proving the load-
    # bearing invariant precisely (not merely "nothing was bound
    # anywhere"): SESSION BINDING ALONE IS NOT PER-CALL PROOF. A direct
    # call passing human_input_authority=None must NOT inherit P
    # merely because this process's session happens to be bound.
    tmp_root_b = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_b_")
    try:
        env = _fresh_env(tmp_root_b)
        llama_anaxi = env["llama_anaxi"]
        session_id = llama_anaxi.get_current_session_id()
        session_started_at = llama_anaxi.get_current_session_started_at()
        bind_conn = sqlite3.connect(env["provenance_db_path"])
        bind_conn.execute("PRAGMA foreign_keys = ON;")
        env["human_session_binding"].bind_session_to_registered_human(
            bind_conn, session_id=session_id, session_started_at=session_started_at,
            pipeline_key=llama_anaxi.PIPELINE_KEY, actor_id=env["registered_actor_id"],
        )
        bind_conn.close()

        result = llama_anaxi.run_waking_turn(env["orch"], "Just an ordinary unbound turn.")
        x_event_id = result["native_event_id"]

        check("B. session S IS genuinely bound to registered human P before this call",
              env["human_session_binding"].resolve_bound_human_for_session(
                  sqlite3.connect(env["provenance_db_path"]), session_id,
              ) is not None)
        check("B. model call occurred normally despite human_input_authority=None", len(env["model_call_log"]) >= 1)
        conn = sqlite3.connect(env["provenance_db_path"])
        h_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'").fetchone()[0]
        check("B. zero human_waking_input events were written despite the bound session", h_count == 0)
        link_count = conn.execute(
            "SELECT COUNT(*) FROM event_components WHERE event_id = ? AND component_kind = 'human_input_event_id'",
            (x_event_id,),
        ).fetchone()[0]
        check("B. Clark's waking_turn X carries no human_input_event_id component", link_count == 0)
        conn.close()
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_b, ignore_errors=True)

    # ============================================ Scenario C: invalid authority
    tmp_root_c = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_c_")
    try:
        env = _fresh_env(tmp_root_c)
        llama_anaxi = env["llama_anaxi"]
        session_id = llama_anaxi.get_current_session_id()
        session_started_at = llama_anaxi.get_current_session_started_at()

        bind_conn = sqlite3.connect(env["provenance_db_path"])
        bind_conn.execute("PRAGMA foreign_keys = ON;")
        real_authority = env["human_session_binding"].bind_session_to_registered_human(
            bind_conn, session_id=session_id, session_started_at=session_started_at,
            pipeline_key=llama_anaxi.PIPELINE_KEY, actor_id=env["registered_actor_id"],
        )
        bind_conn.close()
        forged_authority = env["human_session_binding"].HumanInputAuthority(
            session_id="a-completely-different-session-id",
            auth_context_id=real_authority.auth_context_id,
            authenticated_actor_id=real_authority.authenticated_actor_id,
        )

        result = llama_anaxi.run_waking_turn(
            env["orch"], "This authority names the wrong session.", human_input_authority=forged_authority,
        )
        check("C. the turn still succeeded (fell back to unbound, no error)", bool(result["reply"]))
        check("C. model call occurred despite the invalid authority", len(env["model_call_log"]) >= 1)
        conn = sqlite3.connect(env["provenance_db_path"])
        h_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'").fetchone()[0]
        check("C. zero human_waking_input events were written for an invalid authority", h_count == 0)
        conn.close()
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_c, ignore_errors=True)

    # ======================================= Scenario D: forced H-write failure
    tmp_root_d = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_d_")
    try:
        env = _fresh_env(tmp_root_d)
        llama_anaxi = env["llama_anaxi"]
        session_id = llama_anaxi.get_current_session_id()
        session_started_at = llama_anaxi.get_current_session_started_at()

        bind_conn = sqlite3.connect(env["provenance_db_path"])
        bind_conn.execute("PRAGMA foreign_keys = ON;")
        authority = env["human_session_binding"].bind_session_to_registered_human(
            bind_conn, session_id=session_id, session_started_at=session_started_at,
            pipeline_key=llama_anaxi.PIPELINE_KEY, actor_id=env["registered_actor_id"],
        )
        bind_conn.close()

        def _forced_failure(*args, **kwargs):
            raise env["human_session_binding"].HumanWakingInputWriteError("forced test failure")
        original_writer = env["human_session_binding"].record_human_waking_input
        env["human_session_binding"].record_human_waking_input = _forced_failure

        raised = False
        try:
            llama_anaxi.run_waking_turn(
                env["orch"], "This H write is forced to fail.", human_input_authority=authority,
            )
        except env["human_session_binding"].HumanWakingInputWriteError:
            raised = True
        finally:
            env["human_session_binding"].record_human_waking_input = original_writer

        check("D. run_waking_turn() raised when a validated authority's H-write failed", raised)
        check("D. ZERO model calls occurred -- aborted before any inference", len(env["model_call_log"]) == 0)
        conn = sqlite3.connect(env["provenance_db_path"])
        x_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'waking_turn'").fetchone()[0]
        check("D. no Clark waking_turn X was ever written either", x_count == 0)
        conn.close()
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_d, ignore_errors=True)

    # ============================ Scenario G: H commits, model then fails
    # SLP1-A4c checkpoint requirement: distinct from Scenario D (H's own
    # WRITE fails). Here H commits genuinely and successfully; the
    # SUBSEQUENT model call (Clark's own generation) raises. H must
    # remain real and canonical -- "human P addressed Clark; Clark did
    # not successfully answer" -- and no waking_turn X may exist.
    tmp_root_g = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_g_")
    try:
        env = _fresh_env(tmp_root_g)
        llama_anaxi = env["llama_anaxi"]
        session_id = llama_anaxi.get_current_session_id()
        session_started_at = llama_anaxi.get_current_session_started_at()

        bind_conn = sqlite3.connect(env["provenance_db_path"])
        bind_conn.execute("PRAGMA foreign_keys = ON;")
        authority = env["human_session_binding"].bind_session_to_registered_human(
            bind_conn, session_id=session_id, session_started_at=session_started_at,
            pipeline_key=llama_anaxi.PIPELINE_KEY, actor_id=env["registered_actor_id"],
        )
        bind_conn.close()

        def _fake_chat_raises(model, messages, format=None, options=None, think=None):
            raise RuntimeError("simulated model-call failure, strictly after H already committed")
        sys.modules["ollama"].chat = _fake_chat_raises

        raised = False
        try:
            llama_anaxi.run_waking_turn(
                env["orch"], "H will commit, then the model call itself will fail.",
                human_input_authority=authority,
            )
        except RuntimeError:
            raised = True

        check("G. run_waking_turn() raised when the model call itself failed", raised)
        conn = sqlite3.connect(env["provenance_db_path"])
        h_rows = conn.execute(
            "SELECT component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'human_waking_input'"
        ).fetchall()
        check("G. H still exists, real and canonical, despite Clark's subsequent generation failing",
              len(h_rows) == 1 and h_rows[0][0] == "H will commit, then the model call itself will fail.")
        x_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'waking_turn'").fetchone()[0]
        check("G. no Clark waking_turn X exists -- the failed generation never reached canonical commit",
              x_count == 0)
        conn.close()
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_g, ignore_errors=True)

    # ========================== Scenario F: live ingress via llama_gui.respond()
    # SLP1-A4c checkpoint requirement: proves the REAL human-facing
    # entrypoint (llama_gui.respond(), the actual function Gradio
    # calls) -- not just run_waking_turn() called directly -- threads a
    # genuine, explicitly-configured HumanInputAuthority into the
    # exact same ordering law. ANAXI_BOUND_HUMAN_ACTOR_ID is set BEFORE
    # llama_gui is imported, exactly as a real production launch would
    # configure it; llama_gui's own module-level binding code (which
    # runs at import time) performs the real, one-time session-start
    # binding against the same temp provenance DB.
    tmp_root_f = tempfile.mkdtemp(prefix="anaxi_test_e2e_hwi_f_")
    try:
        env = _fresh_env(tmp_root_f)
        llama_anaxi = env["llama_anaxi"]

        os.environ["ANAXI_BOUND_HUMAN_ACTOR_ID"] = env["registered_actor_id"]
        for mod in ("llama_gui",):
            sys.modules.pop(mod, None)
        try:
            import llama_gui
            check("F. importing llama_gui with an explicit configured binding "
                  "produces a real BOUND_HUMAN_AUTHORITY (not None)",
                  llama_gui.BOUND_HUMAN_AUTHORITY is not None)
            check("F. BOUND_HUMAN_AUTHORITY names the exact configured registered actor",
                  llama_gui.BOUND_HUMAN_AUTHORITY is not None
                  and llama_gui.BOUND_HUMAN_AUTHORITY.authenticated_actor_id == env["registered_actor_id"])

            llama_gui.DB_PATH = llama_anaxi.DB_PATH
            llama_gui.UI_TURN_DIAGNOSTICS_LOG_PATH = os.path.join(tmp_root_f, "ui_turn_diagnostics.jsonl")
            llama_gui.DIRECTION_CONTROL_TRACE_PATH = os.path.join(tmp_root_f, "direction_control_trace.jsonl")
            llama_gui.WORKSPACE_ROAMING_TRACE_PATH = os.path.join(tmp_root_f, "workspace_roaming_trace.jsonl")

            # respond() constructs its OWN real AnaxiOrchestrator(DB_PATH)
            # internally -- the real ConstitutionalMind constructor
            # eagerly loads a sentence-transformers embedder, exactly
            # the heavy, network-touching path test_native_waking_turn_
            # end_to_end.py already avoids (there, by bypassing __init__
            # via __new__ on a manually-built instance; here, since
            # respond() controls construction itself, by patching the
            # CLASS methods instead -- same isolation goal, only
            # reachable at a different seam).
            import orchestration
            original_init = orchestration.AnaxiOrchestrator.__init__
            original_prepare_context = orchestration.AnaxiOrchestrator.prepare_context
            original_close = orchestration.AnaxiOrchestrator.close
            original_record_controls = orchestration.AnaxiOrchestrator.record_turn_generation_controls
            orchestration.AnaxiOrchestrator.__init__ = lambda self, *a, **k: None
            orchestration.AnaxiOrchestrator.prepare_context = lambda self, user_id, prompt: {
                "messages": [{"role": "system", "content": "You are Clark."}],
                "controls": {"temperature": 0.4, "top_p": 0.85},
                "kardia": {"aesthetic_valve": "warm"},
                "memory_context": "",
            }
            orchestration.AnaxiOrchestrator.close = lambda self: None
            orchestration.AnaxiOrchestrator.record_turn_generation_controls = lambda self, *a, **k: None
            try:
                reply = llama_gui.respond("A message through the real human-facing respond() path.", [])
            finally:
                orchestration.AnaxiOrchestrator.__init__ = original_init
                orchestration.AnaxiOrchestrator.prepare_context = original_prepare_context
                orchestration.AnaxiOrchestrator.close = original_close
                orchestration.AnaxiOrchestrator.record_turn_generation_controls = original_record_controls
            check("F. respond() completed and returned Clark's reply", bool(reply))

            conn = sqlite3.connect(env["provenance_db_path"])
            h_rows = conn.execute(
                "SELECT c.component_text, c.creator_actor_id FROM events e "
                "JOIN event_components c ON c.event_id = e.event_id "
                "WHERE e.event_type = 'human_waking_input'"
            ).fetchall()
            check("F. the REAL respond() path committed a canonical human_waking_input event H",
                  len(h_rows) == 1)
            check("F. H is attributed to the configured registered human actor, verbatim text",
                  h_rows and h_rows[0][0] == "A message through the real human-facing respond() path."
                  and h_rows[0][1] == env["registered_actor_id"])
            conn.close()
        finally:
            del os.environ["ANAXI_BOUND_HUMAN_ACTOR_ID"]
            sys.modules.pop("llama_gui", None)
    finally:
        _teardown_env(env)
        shutil.rmtree(tmp_root_f, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    sys.exit(0 if run_test_suite() else 1)
