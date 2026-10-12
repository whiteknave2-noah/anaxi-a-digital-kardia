"""
Anaxi -- Integration test for the deterministic signal gate wired
into the REAL, actual run_waking_turn() in llama_anaxi.py. Not a test
of the gate in isolation (already covered by signal_matcher.py,
signal_observation_log.py, review_checkpoint.py) -- this exercises
the actual modified function itself, confirming the four branches
behave exactly as the four settled decisions specify once wired
together, under the new construction contract (identified_referent /
construction_status, not create_artifact).

Every real external dependency (ollama, AnaxiOrchestrator,
RelationalHistory, native_provenance_writer) is faked at the
sys.modules boundary; signal_observation_log's record_observation()
is genuinely real but rebound (via llama_anaxi.record_observation,
the same monkeypatch pattern used for call_llama/log_entry/
classify_signal below) to write to a temp path instead of its
production-relative default. This test never opens, reads, or
deletes the real signal_observations.jsonl at all -- earlier
revisions of this file did (a delete-before/delete-after bracket
around the real default path), which is what actually wrote to live
production data during an unrelated accidental execution elsewhere
on 2026-08-29; that pattern is removed here, not merely redirected.

Run:
    python test_signal_gate_integration.py
"""

import json
import os
import sys
import types
import unittest.mock as mock


def build_fake_ollama(model_call_tracker):
    """Every chat() call increments the tracker -- lets tests confirm
    exactly how many times (if any) a model was actually invoked."""
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        model_call_tracker["count"] += 1
        return {"message": {"content": json.dumps({
            "identified_referent": "this thought about the project",
            "construction_status": "success", "artifact_type": "journal",
            "artifact_scope": "personal", "namespace": "journal", "title": "test entry",
            "what_they_shared": None, "content": "test content, model's own words",
        })}}
    fake.chat = fake_chat
    return fake


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    model_calls = {"count": 0}
    sys.modules["ollama"] = build_fake_ollama(model_calls)

    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        def prepare_context(self, user_id, prompt):
            return {
                "messages": [{"role": "system", "content": "You are Clark."}],
                "controls": {},
                "kardia": {},
                "memory_context": "",
            }

        def record_turn_generation_controls(self, user_id, controls, *, model_revision_id,
                                             pipeline_id, event_id, timestamp=None):
            # No real DB write -- matches the real AnaxiOrchestrator's
            # signature so run_waking_turn()'s post-canonical-commit call
            # doesn't crash; captures the call in fake-local state, not
            # asserted against here (not a provenance test).
            self.last_turn_generation_controls_call = {
                "user_id": user_id, "controls": controls, "model_revision_id": model_revision_id,
                "pipeline_id": pipeline_id, "event_id": event_id, "timestamp": timestamp,
            }
    fake_orch_module.AnaxiOrchestrator = FakeOrchestrator
    sys.modules["orchestration"] = fake_orch_module

    fake_relational_module = types.ModuleType("relational_history")

    class FakeRelationalHistory:
        def __init__(self, path):
            pass
        def record_event(self, **kwargs):
            pass
        def close(self):
            pass
    fake_relational_module.RelationalHistory = FakeRelationalHistory
    sys.modules["relational_history"] = fake_relational_module

    # llama_anaxi.py's run_waking_turn() now calls the REAL
    # native_provenance_writer module for its canonical-before-legacy
    # write. This test's own scope statement (module docstring) is that
    # NO real database is touched -- so native_provenance_writer is
    # faked the same way orchestration/relational_history are above,
    # never given a real (even temporary) anaxi_provenance.db to write
    # to. Returns a structurally valid bundle so the ordered legacy
    # projection below it (record_turn_generation_controls -> log_entry
    # -> relational history) still receives everything it expects.
    fake_npw_module = types.ModuleType("native_provenance_writer")
    _native_call_count = {"n": 0}

    def _fake_generate_native_ulid():
        _native_call_count["n"] += 1
        return f"FAKEULID{_native_call_count['n']:018d}"

    def _fake_stage_and_record_native_waking_turn(
        data_dir, staging_path, *, session_id, session_started_at,
        user_id, prompt, bounded_clause, clark_prose, kardia, controls,
        waking_model_tag, pipeline_key, artifact_pass_ran, occurred_at,
        delivered_episode_run_id=None,
        interaction_mode=None,
        delivered_active_workspace_event_ids=None,
        human_input_event_id=None,
    ):
        # No line_index parameter -- matches the real (hardened)
        # native_provenance_writer.stage_and_record_native_waking_turn()
        # signature, which now determines its own staging index
        # atomically rather than accepting one from the caller.
        _native_call_count["n"] += 1
        event_id = f"fake-event-{_native_call_count['n']}"
        participations = []
        if artifact_pass_ran:
            participations.append({"model_revision_id": "fake-model-rev-pass1", "tag": waking_model_tag})
        participations.append({"model_revision_id": "fake-model-rev-pass2", "tag": waking_model_tag})
        return {
            "event_id": event_id, "session_id": session_id,
            "auth_context_id": f"fake-auth-{_native_call_count['n']}",
            "pipeline_id": f"fake-pipeline-{pipeline_key}",
            "model_participations": participations,
            "reassembled_reply": (bounded_clause + " " + clark_prose).strip(),
            "staging_id": f"fake-staging-{_native_call_count['n']}", "user_id": user_id, "prompt": prompt,
            "kardia": kardia, "controls": controls, "occurred_at": occurred_at,
            "waking_model_tag": waking_model_tag, "pipeline_key": pipeline_key,
        }
    fake_npw_module.generate_native_ulid = _fake_generate_native_ulid
    fake_npw_module.stage_and_record_native_waking_turn = _fake_stage_and_record_native_waking_turn
    sys.modules["native_provenance_writer"] = fake_npw_module

    import llama_anaxi

    llama_anaxi.call_llama = lambda messages, controls: "mock reply"
    llama_anaxi.log_entry = lambda prompt, reply, kardia, native_result=None: None

    orch = FakeOrchestrator()

    import tempfile
    test_obsidian_root = tempfile.mkdtemp(prefix="anaxi_test_workspace_")
    original_workspace_root = llama_anaxi.OBSIDIAN_WORKSPACE_ROOT
    llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = test_obsidian_root
    test_staging_dir = tempfile.mkdtemp(prefix="anaxi_test_staging_")
    original_provenance_db_dir = llama_anaxi.PROVENANCE_DB_DIR
    original_staging_path = llama_anaxi.STAGING_PATH
    llama_anaxi.PROVENANCE_DB_DIR = test_staging_dir
    llama_anaxi.STAGING_PATH = os.path.join(test_staging_dir, "native_turn_staging.jsonl")

    # ISOLATION: signal_observation_log.record_observation()'s log_path
    # default is bound at import time, not call time, so redirecting
    # signal_observation_log.LOG_FILE after import is a no-op (this bit
    # production data once already -- see the 2026-08-29 incident record).
    # Instead of touching the real production-relative default at all,
    # rebind llama_anaxi's own imported call surface (the exact pattern
    # this file already uses for call_llama/log_entry/classify_signal
    # below) to a wrapper that calls the REAL record_observation() with
    # an explicit temp log_path. The real default path is never opened,
    # read, or deleted by this test.
    import signal_observation_log
    real_record_observation = signal_observation_log.record_observation
    test_signal_log_dir = tempfile.mkdtemp(prefix="anaxi_test_signal_log_")
    test_log_path = os.path.join(test_signal_log_dir, "signal_observations.jsonl")
    llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_log_path)

    try:
        model_calls["count"] = 0
        result = llama_anaxi.run_waking_turn(orch, "Please save this thought about the project.")
        check("POSITIVE: model was invoked exactly once",
              model_calls["count"] == 1)
        check("POSITIVE: artifact_created is True",
              result["artifact_result"]["artifact_created"] is True)

        model_calls["count"] = 0
        result = llama_anaxi.run_waking_turn(orch, "Don't save this.")
        check("NEGATIVE: zero model calls",
              model_calls["count"] == 0)
        check("NEGATIVE: artifact_created is False",
              result["artifact_result"]["artifact_created"] is False)
        check("NEGATIVE: pass2_context correctly says None",
              result["artifact_result"]["pass2_context"] == "Artifact action:\nNone.")

        model_calls["count"] = 0
        result = llama_anaxi.run_waking_turn(orch, "What's a good way to organize a bookshelf?")
        check("NO_SIGNAL: zero model calls (the actual, real first live failure case)",
              model_calls["count"] == 0)
        check("NO_SIGNAL: artifact_created is False",
              result["artifact_result"]["artifact_created"] is False)

        model_calls["count"] = 0
        result = llama_anaxi.run_waking_turn(orch, "You should remember that I said this.")
        check("UNRECOGNIZED: zero model calls",
              model_calls["count"] == 0)
        check("UNRECOGNIZED: artifact_created is False",
              result["artifact_result"]["artifact_created"] is False)

        from signal_observation_log import query_observations
        all_observations = query_observations(test_log_path)
        check("All four turns were logged as observations (4 records)",
              len(all_observations) == 4)
        categories_logged = [o["signal_category"] for o in all_observations]
        check("Logged categories match the four branches exactly, in order",
              categories_logged == ["POSITIVE", "NEGATIVE", "NO_SIGNAL", "UNRECOGNIZED"])

        real_classify = llama_anaxi.classify_signal
        llama_anaxi.classify_signal = lambda text: (_ for _ in ()).throw(RuntimeError("simulated failure"))
        model_calls["count"] = 0
        try:
            result = llama_anaxi.run_waking_turn(orch, "Please save this.")
            crashed = False
        except Exception:
            crashed = True
        check("A classify_signal() failure does not crash the turn",
              not crashed)
        check("A classify_signal() failure fails CLOSED (no model call, no artifact)",
              not crashed and model_calls["count"] == 0
              and result["artifact_result"]["artifact_created"] is False)
        llama_anaxi.classify_signal = real_classify

        real_record = llama_anaxi.record_observation
        llama_anaxi.record_observation = lambda text: (_ for _ in ()).throw(RuntimeError("simulated failure"))
        try:
            result = llama_anaxi.run_waking_turn(orch, "Please save this.")
            crashed = False
        except Exception:
            crashed = True
        check("A record_observation() failure does not crash the turn",
              not crashed)
        llama_anaxi.record_observation = real_record

        # === SENTINEL PROOF: the rebound record_observation() call
        # surface never touches the real production-relative default
        # path, even when a file happens to already sit exactly there.
        # Run inside a throwaway CWD (never the live project directory)
        # with a sentinel file planted at the CWD-relative default name
        # signal_observation_log.LOG_FILE resolves to -- prove it is
        # byte-identical afterward. ===
        import shutil
        sentinel_cwd = tempfile.mkdtemp(prefix="anaxi_test_sentinel_cwd_")
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
        llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = original_workspace_root
        llama_anaxi.PROVENANCE_DB_DIR = original_provenance_db_dir
        llama_anaxi.STAGING_PATH = original_staging_path
        llama_anaxi.record_observation = real_record_observation
        import shutil as _shutil
        _shutil.rmtree(test_obsidian_root, ignore_errors=True)
        _shutil.rmtree(test_staging_dir, ignore_errors=True)
        _shutil.rmtree(test_signal_log_dir, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
