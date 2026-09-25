"""
Anaxi -- Regression suite for the approved Gemma waking-substrate
switch. Proves the configuration seam (llama_anaxi.MODEL) genuinely
drives both model-call sites, proves the legacy "substrate" pipeline
discriminator and the new "waking_model_tag" exact-provenance field
stay genuinely distinct (never conflated), proves REM's unmodified
consolidation filter still picks up new-format waking-turn log
entries, and proves REM's own model configuration is untouched and
independent of the waking substrate.

No real model calls anywhere in this file -- every ollama.chat() call
site is faked. No real database is touched -- orchestration,
relational_history, sleep_receipts, and native_provenance_writer are
faked at the module-import boundary, matching test_signal_gate_integration.py's
established convention, so only the specific functions under test
(call_llama, ask_llama_for_json, log_entry, write_journal_entry,
process_artifact_decision, llama_sleep.load_recent_conversation) run
for real. signal_observation_log's record_observation() is also
genuinely real but rebound (llama_anaxi.record_observation) to write
to a temp path instead of its production-relative default -- this test
never opens, reads, or deletes the real signal_observations.jsonl at
all, proven by a dedicated sentinel-file check.

Run:
    python test_gemma_substrate_switch.py
"""

import json
import os
import shutil
import sys
import tempfile
import types


def build_fake_ollama(call_tracker):
    """call_tracker collects every (model, format) pair passed to
    chat() -- lets tests confirm the exact model tag actually
    forwarded to Ollama, not just that a call happened."""
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        call_tracker.append({"model": model, "format": format})
        if format == "json":
            return {"message": {"content": json.dumps({"ok": True})}}
        return {"message": {"content": "mock prose reply"}}

    fake.chat = fake_chat
    return fake


def install_fake_heavy_dependencies():
    """orchestration/relational_history/sleep_receipts pull in a real
    SQLite DB and (via anaxi_protocol_sqlite) a real sentence-transformers
    embedding model at instantiation time -- neither is needed to test
    the functions this file actually exercises, so both are faked at
    the sys.modules boundary before import, exactly matching
    test_signal_gate_integration.py's established pattern."""
    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        last_turn_generation_controls_kwargs = None

        def prepare_context(self, user_id, prompt):
            return {
                "messages": [{"role": "system", "content": "You are Clark."}],
                "controls": {"temperature": 0.4, "top_p": 0.85},
                "kardia": {},
                "memory_context": "",
            }

        def record_turn_generation_controls(self, user_id, controls, *, model_revision_id,
                                             pipeline_id, event_id, timestamp=None):
            # No real DB write -- matches the real AnaxiOrchestrator's
            # signature so run_waking_turn()'s post-canonical-commit call
            # doesn't crash; captures the call in fake-local state (same
            # class-attribute convention FakeRelationalHistory.last_call_kwargs
            # already uses in this file), not asserted against here
            # (not a provenance test).
            FakeOrchestrator.last_turn_generation_controls_kwargs = {
                "user_id": user_id, "controls": controls, "model_revision_id": model_revision_id,
                "pipeline_id": pipeline_id, "event_id": event_id, "timestamp": timestamp,
            }
    fake_orch_module.AnaxiOrchestrator = FakeOrchestrator
    sys.modules["orchestration"] = fake_orch_module

    fake_relational_module = types.ModuleType("relational_history")

    class FakeRelationalHistory:
        last_call_kwargs = None

        def __init__(self, path):
            pass

        def record_event(self, **kwargs):
            FakeRelationalHistory.last_call_kwargs = kwargs

        def close(self):
            pass
    fake_relational_module.RelationalHistory = FakeRelationalHistory
    sys.modules["relational_history"] = fake_relational_module

    # SLP0: sleep_receipts.py imports only stdlib (json/sqlite3/uuid) --
    # never a heavy dependency -- and neither llama_anaxi.py nor this
    # file's own genuine `import llama_sleep` (below) requires it to be
    # faked. Faking it here was unnecessary and its only observed real
    # effect was a permanent, unrestored sys.modules["sleep_receipts"]
    # substitution that corrupted later same-process tests (e.g.
    # test_sleep_receipts.py's own inspect.getsource() call, which does
    # a dynamic sys.modules-keyed lookup). Removed rather than wrapped
    # in save/restore machinery, since the injection itself was
    # unnecessary -- llama_sleep now genuinely imports the real
    # SleepReceiptStore, exactly as production does.

    # llama_anaxi.py's run_waking_turn() now calls the REAL
    # native_provenance_writer module for its canonical-before-legacy
    # write. Faked here the same way orchestration/relational_history
    # are above, so this file's own "No real database is
    # touched" guarantee (module docstring) still holds.
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

    return FakeOrchestrator, FakeRelationalHistory


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    call_log = []
    sys.modules["ollama"] = build_fake_ollama(call_log)
    FakeOrchestrator, FakeRelationalHistory = install_fake_heavy_dependencies()

    for mod in ("llama_anaxi", "llama_sleep"):
        if mod in sys.modules:
            del sys.modules[mod]

    import llama_anaxi

    test_staging_dir = tempfile.mkdtemp(prefix="anaxi_test_gemma_switch_staging_")
    llama_anaxi.PROVENANCE_DB_DIR = test_staging_dir
    llama_anaxi.STAGING_PATH = os.path.join(test_staging_dir, "native_turn_staging.jsonl")

    # === 1. Configured waking model is Gemma ===
    check("1. Approved configuration: llama_anaxi.MODEL == 'gemma4:e4b'",
          llama_anaxi.MODEL == "gemma4:e4b")

    # === 2/3. Model selection comes from the seam, not a buried literal;
    # both supported tags selectable in isolation, for BOTH call sites ===
    for tag in ("llama3.2:3b", "gemma4:e4b"):
        llama_anaxi.MODEL = tag
        call_log.clear()
        llama_anaxi.call_llama([{"role": "user", "content": "hi"}], {"temperature": 0.4, "top_p": 0.85})
        check(f"2/3. call_llama() forwards the seam's current value ({tag!r}) to ollama.chat, "
              f"not a hardcoded literal",
              len(call_log) == 1 and call_log[0]["model"] == tag and call_log[0]["format"] is None)

        call_log.clear()
        llama_anaxi.ask_llama_for_json([{"role": "user", "content": "hi"}])
        check(f"2/3. ask_llama_for_json() forwards the seam's current value ({tag!r}) to ollama.chat",
              len(call_log) == 1 and call_log[0]["model"] == tag and call_log[0]["format"] == "json")

    llama_anaxi.MODEL = "gemma4:e4b"  # restore approved configuration before continuing

    # === 4/6. log_entry() provenance: legacy field vs exact field, never conflated ===
    tmp_log = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmp_log.close()
    original_log_file = llama_anaxi.LOG_FILE
    llama_anaxi.LOG_FILE = tmp_log.name
    try:
        llama_anaxi.log_entry("test prompt", "test reply", {"aesthetic_valve": "warm"})
        with open(tmp_log.name, encoding="utf-8") as f:
            written = json.loads(f.readline())
    finally:
        llama_anaxi.LOG_FILE = original_log_file
        os.remove(tmp_log.name)

    check("4. log_entry(): legacy pipeline discriminator remains exactly 'llama'",
          written["substrate"] == "llama")
    check("4. log_entry(): exact provenance field 'waking_model_tag' == configured model 'gemma4:e4b'",
          written["waking_model_tag"] == "gemma4:e4b")
    check("4. log_entry(): pre-existing 'model' field also reflects the configured model (auto-updated via the seam)",
          written["model"] == "gemma4:e4b")
    check("6. log_entry(): 'substrate' and 'waking_model_tag' are genuinely distinct values, never conflated",
          written["substrate"] != written["waking_model_tag"])
    check("4/6. No stale Llama attribution: waking_model_tag is NOT the old default 'llama3.2:3b'",
          written["waking_model_tag"] != "llama3.2:3b")

    # === 4/6. Journal frontmatter provenance ===
    import clark_journal
    tmp_workspace = tempfile.mkdtemp(prefix="anaxi_test_gemma_switch_")
    try:
        r = clark_journal.write_journal_entry(
            tmp_workspace, "Journal/gemma-provenance-test.md", "content",
            waking_model_tag="gemma4:e4b",
        )
        frontmatter_text = open(r["path"], encoding="utf-8").read()
        check("4. Journal frontmatter: legacy 'substrate: llama' line still present",
              "substrate: llama\n" in frontmatter_text)
        check("4. Journal frontmatter: new 'waking_model_tag: gemma4:e4b' line present",
              "waking_model_tag: gemma4:e4b\n" in frontmatter_text)
        check("6. Journal frontmatter: the two provenance lines are distinct lines with distinct values",
              "substrate: llama\n" in frontmatter_text
              and "waking_model_tag: gemma4:e4b\n" in frontmatter_text
              and "substrate: gemma4:e4b\n" not in frontmatter_text
              and "waking_model_tag: llama\n" not in frontmatter_text)

        # process_artifact_decision() end-to-end threading of waking_model_tag
        source = "Please save this thought about the switch."
        decision = json.dumps({
            "identified_referent": "this thought about the switch", "construction_status": "success",
            "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
            "title": "switch-test", "what_they_shared": None, "content": "content about the switch",
        })
        r2 = clark_journal.process_artifact_decision(decision, tmp_workspace, source_prompt=source,
                                                       waking_model_tag="gemma4:e4b")
        check("4. process_artifact_decision() threads waking_model_tag through to the written file",
              r2["artifact_created"] is True
              and "waking_model_tag: gemma4:e4b\n" in open(r2["detail"]["path"], encoding="utf-8").read())

        # Backward compatibility: existing callers that don't pass waking_model_tag still work
        r3 = clark_journal.write_journal_entry(tmp_workspace, "Journal/legacy-call.md", "x")
        check("Backward compatibility: write_journal_entry() without waking_model_tag still succeeds "
              "(defaults to 'unknown', doesn't crash existing callers)",
              r3["status"] == "success"
              and "waking_model_tag: unknown\n" in open(r3["path"], encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp_workspace)

    # === 4. Relational-history known gap: substrate stays "llama", no invented field ===
    FakeRelationalHistory.last_call_kwargs = None
    original_call_llama = llama_anaxi.call_llama
    original_log_entry = llama_anaxi.log_entry
    llama_anaxi.call_llama = lambda messages, controls: "mock waking reply"
    llama_anaxi.log_entry = lambda prompt, reply, kardia, native_result=None: None
    test_obsidian_root = tempfile.mkdtemp(prefix="anaxi_test_gemma_switch_workspace_")
    original_workspace_root = llama_anaxi.OBSIDIAN_WORKSPACE_ROOT
    llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = test_obsidian_root

    # ISOLATION: this run_waking_turn() call reaches record_observation()
    # for real. signal_observation_log.record_observation()'s log_path
    # default is bound at import time, not call time, so reassigning
    # signal_observation_log.LOG_FILE is a no-op. Rebind llama_anaxi's own
    # imported call surface (same pattern used in test_signal_gate_integration.py/
    # test_bounded_clause_integration.py/test_native_waking_turn_end_to_end.py)
    # to a wrapper calling the REAL record_observation() with an explicit
    # temp log_path -- the real production-relative default is never
    # opened, read, or deleted by this test.
    import signal_observation_log
    original_record_observation = llama_anaxi.record_observation
    test_signal_log_dir = tempfile.mkdtemp(prefix="anaxi_test_gemma_switch_signal_log_")
    test_signal_log_path = os.path.join(test_signal_log_dir, "signal_observations.jsonl")
    llama_anaxi.record_observation = lambda text: signal_observation_log.record_observation(
        text, log_path=test_signal_log_path)

    try:
        llama_anaxi.run_waking_turn(FakeOrchestrator(), "An ordinary message with no save signal.")
        check("4. Known gap, confirmed live: RelationalHistory.record_event() still receives substrate='llama' "
              "(the legacy substrate label itself was never migrated to a new value -- relational_events "
              "since gained additive model_revision_id/pipeline_id/auth_context_id/event_id columns, "
              "checked separately below, but 'substrate' stays the same legacy literal)",
              FakeRelationalHistory.last_call_kwargs is not None
              and FakeRelationalHistory.last_call_kwargs.get("substrate") == "llama")
        check("6. Known gap: no 'waking_model_tag' kwarg was invented for relational history "
              "(the field genuinely doesn't exist there, not silently aliased)",
              "waking_model_tag" not in FakeRelationalHistory.last_call_kwargs)
        check("Native-path provenance kwargs (model_revision_id/pipeline_id/auth_context_id/event_id) "
              "ARE threaded through to relational_history.record_event(), from the same native bundle "
              "that log_entry() and turn_generation_log received",
              all(k in FakeRelationalHistory.last_call_kwargs
                  for k in ("model_revision_id", "pipeline_id", "auth_context_id", "event_id")))

        # === SENTINEL PROOF: the rebound record_observation() call
        # surface never touches the real production-relative default
        # path, even when a file happens to already sit exactly there.
        # Run inside a throwaway CWD (never the live project directory)
        # with a sentinel file planted at the CWD-relative default name
        # signal_observation_log.LOG_FILE resolves to -- prove it is
        # byte-identical afterward. ===
        sentinel_cwd = tempfile.mkdtemp(prefix="anaxi_test_gemma_switch_sentinel_cwd_")
        sentinel_path = os.path.join(sentinel_cwd, signal_observation_log.LOG_FILE)
        sentinel_content = b'{"pre_existing": "sentinel, must not be touched"}\n'
        with open(sentinel_path, "wb") as f:
            f.write(sentinel_content)
        original_cwd = os.getcwd()
        try:
            os.chdir(sentinel_cwd)
            llama_anaxi.run_waking_turn(FakeOrchestrator(), "Another ordinary message, sentinel proof.")
        finally:
            os.chdir(original_cwd)
        with open(sentinel_path, "rb") as f:
            sentinel_after = f.read()
        check("SENTINEL PROOF: a pre-existing file at the production-relative default path "
              "(signal_observations.jsonl) remains byte-identical after a full run_waking_turn() "
              "call -- the rebound wrapper never opened it",
              sentinel_after == sentinel_content)
        shutil.rmtree(sentinel_cwd, ignore_errors=True)
    finally:
        llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = original_workspace_root
        llama_anaxi.call_llama = original_call_llama
        llama_anaxi.log_entry = original_log_entry
        llama_anaxi.record_observation = original_record_observation
        shutil.rmtree(test_signal_log_dir, ignore_errors=True)
        shutil.rmtree(test_obsidian_root)

    # === 5. REM's unmodified consolidation filter still includes new-format entries ===
    import llama_sleep
    tmp_rem_log = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmp_rem_log.close()
    original_llama_anaxi_log_file = llama_anaxi.LOG_FILE
    llama_anaxi.LOG_FILE = tmp_rem_log.name
    llama_anaxi.MODEL = "gemma4:e4b"
    try:
        llama_anaxi.log_entry("What season do I like best?", "Autumn, as always.", {})
        original_rem_log_file = llama_sleep.LOG_FILE
        llama_sleep.LOG_FILE = tmp_rem_log.name
        try:
            # load_recent_conversation() now requires a provenance_conn
            # (the accepted pipeline-aware matches_legacy_pipeline()
            # matcher) -- isolated, self-contained, deterministic
            # fixture: a fresh in-memory provenance DB, seeded with only
            # the pipeline reference rows the matcher needs, never a
            # real anaxi_provenance.db.
            from provenance_schema import create_provenance_db, derive_stable_id
            from migrate_historical_data import PIPELINE_STRUCTURE
            synthetic_provenance_conn = create_provenance_db(":memory:")
            try:
                for legacy_label, structure in PIPELINE_STRUCTURE.items():
                    pipeline_key = structure["pipeline_key"]
                    synthetic_provenance_conn.execute(
                        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label) "
                        "VALUES (?, ?, ?)",
                        (derive_stable_id("pipeline", pipeline_key), pipeline_key, legacy_label),
                    )
                synthetic_provenance_conn.commit()
                conversation_text = llama_sleep.load_recent_conversation(20, synthetic_provenance_conn)
            finally:
                synthetic_provenance_conn.close()
        finally:
            llama_sleep.LOG_FILE = original_rem_log_file
    finally:
        llama_anaxi.LOG_FILE = original_llama_anaxi_log_file
        os.remove(tmp_rem_log.name)

    check("5. REM's real, unmodified load_recent_conversation() still includes a Gemma-tagged waking entry "
          "(its 'llama' pipeline-discriminator filter remains satisfied, unchanged)",
          "What season do I like best?" in conversation_text and "Autumn, as always." in conversation_text)

    # === 7. REM's own model configuration is unchanged and independent ===
    check("7. llama_sleep.MODEL remains the original Llama tag, untouched by the waking-substrate switch",
          llama_sleep.MODEL == "llama3.2:3b")
    llama_anaxi.MODEL = "some-other-tag-entirely"
    check("7. Changing llama_anaxi.MODEL has zero effect on llama_sleep.MODEL (separate, independent constants)",
          llama_sleep.MODEL == "llama3.2:3b")
    llama_anaxi.MODEL = "gemma4:e4b"  # restore approved configuration

    shutil.rmtree(test_staging_dir, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
