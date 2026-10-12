"""
Anaxi -- Integration test for the bounded-clause implementation,
exercising the REAL run_waking_turn(), not just bounded_clause.py in
isolation (already covered) or the pre-existing suites (which never
touch this new code path at all -- confirmed directly: neither
test_clark_journal.py nor test_signal_gate_integration.py asserts on
the final reply string's content or structure).

Tests all six operation_status states reachable through the real
function, confirming:
  - the final reply genuinely starts with the exact approved clause
  - stale_schema is genuinely distinguishable from
    insufficient_information (the specific targeted fix)
  - success uses the real, actual title from construction, not a
    placeholder
  - Clark's system message genuinely receives the new "already been
    told" instruction text, not just that the final string happens
    to look right

Every real external dependency mocked, matching the established
pattern from test_signal_gate_integration.py -- including
OBSIDIAN_WORKSPACE_ROOT this time, per the isolation bug found and
fixed earlier this session.

ISOLATION HISTORY (kept for the lesson, not the fix): the original
run_case() went through the real record_observation(prompt) call with
no isolation for signal_observation_log.LOG_FILE at all. A first
attempted fix -- reassigning signal_observation_log.LOG_FILE to a temp
path -- looked reasonable but was actually a no-op: record_observation's
log_path parameter defaults to LOG_FILE, and Python binds a function's
default argument value ONCE, at module-import time, not at each call.
Since llama_anaxi.py's call site never passes log_path explicitly
(record_observation(prompt)), the reassignment never took effect, and
running the test for real DID write real, confirmed-synthetic lines
into the actual signal_observations.jsonl in the working directory. A
second fix bracketed the real default path with delete-before/
delete-after instead -- which is exactly the pattern that later wrote
to live production data for real, during an unrelated accidental
execution elsewhere on 2026-08-29. Neither approach is used anymore.
The actual fix: rebind llama_anaxi's own imported call surface
(llama_anaxi.record_observation, inside run_case() since llama_anaxi
is re-imported fresh every call) to a wrapper that calls the REAL
record_observation() with an explicit temp log_path -- the real
default is never opened, read, or deleted by this test at all, proven
by a dedicated sentinel-file check near the end of run_test_suite().

Run:
    python test_bounded_clause_integration.py
"""

import json
import os
import sys
import tempfile
import types

_THINK_UNSET = object()


def build_fake_ollama(construction_response, prose_response, model_calls, think_calls=None):
    """model_calls tracks total ollama.chat() invocations; since
    capturing_call_llama() (below) calls through to the REAL
    call_llama() rather than replacing it, Pass 2's prose generation
    genuinely invokes ollama.chat() too -- Clark's prose is produced
    regardless of gate outcome, by design. construction_calls tracks
    ONLY the format="json" construction-pass calls specifically, so
    "no construction was invoked" can still be checked precisely,
    without conflating it with the always-present prose call.

    think_calls, if given, records the exact `think` value (or the
    _THINK_UNSET sentinel if the caller never passed one at all --
    distinct from an explicit None/False) for EVERY call, tagged
    pass1/pass2 by format, so a test can assert pass-2 explicitly
    passes think=False while pass-1 (ask_llama_for_json) leaves it
    unset, not merely defaulted to some fixed value on this fake."""
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=_THINK_UNSET):
        model_calls["count"] += 1
        if think_calls is not None:
            think_calls.append({"format": format, "think": think})
        if format == "json":
            model_calls["construction_count"] += 1
            return {"message": {"content": json.dumps(construction_response)}}
        return {"message": {"content": prose_response}}
    fake.chat = fake_chat
    return fake


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    model_calls = {"count": 0, "construction_count": 0}

    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        def prepare_context(self, user_id, prompt):
            return {
                "messages": [{"role": "system", "content": "You are Clark."}],
                "controls": {"temperature": 0.4, "top_p": 0.85}, "kardia": {}, "memory_context": "",
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
        def __init__(self, path): pass
        def record_event(self, **kwargs): pass
        def close(self): pass
    fake_relational_module.RelationalHistory = FakeRelationalHistory
    sys.modules["relational_history"] = fake_relational_module

    # llama_anaxi.py's run_waking_turn() now calls the REAL
    # native_provenance_writer module for its canonical-before-legacy
    # write. Faked here the same way orchestration/relational_history
    # are above, so this test never touches a real (even temporary)
    # anaxi_provenance.db -- consistent with "Every real external
    # dependency mocked" (module docstring).
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

    test_workspace = tempfile.mkdtemp(prefix="anaxi_bounded_clause_test_")
    test_staging_dir = tempfile.mkdtemp(prefix="anaxi_bounded_clause_staging_")

    # ISOLATION -- see module docstring. record_observation()'s log_path
    # default is bound at import time, not call time, so reassigning
    # signal_observation_log.LOG_FILE after import is a no-op. Instead
    # of touching the real production-relative default at all (an
    # earlier revision of this file bracketed it with delete-before/
    # delete-after, which is what actually wrote to live production data
    # during an unrelated accidental execution elsewhere on 2026-08-29),
    # rebind llama_anaxi's own imported call surface to a wrapper that
    # calls the REAL record_observation() with an explicit temp
    # log_path. Because run_case() re-imports llama_anaxi fresh on every
    # call (del sys.modules + import), the rebind must happen INSIDE
    # run_case() too, every time, not once outside the loop.
    import signal_observation_log
    real_record_observation = signal_observation_log.record_observation
    test_signal_log_dir = tempfile.mkdtemp(prefix="anaxi_bounded_clause_signal_log_")
    test_log_path = os.path.join(test_signal_log_dir, "signal_observations.jsonl")

    orch = FakeOrchestrator()
    last_system_content = {}
    last_think_calls = []

    def run_case(prompt, construction_response, prose_response):
        last_think_calls.clear()
        sys.modules["ollama"] = build_fake_ollama(
            construction_response, prose_response, model_calls, think_calls=last_think_calls
        )
        if "llama_anaxi" in sys.modules:
            del sys.modules["llama_anaxi"]
        import llama_anaxi
        llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = test_workspace
        llama_anaxi.PROVENANCE_DB_DIR = test_staging_dir
        llama_anaxi.STAGING_PATH = os.path.join(test_staging_dir, "native_turn_staging.jsonl")
        llama_anaxi.log_entry = lambda prompt, reply, kardia, native_result=None: None
        llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_log_path)

        real_call_llama = llama_anaxi.call_llama
        def capturing_call_llama(messages, controls):
            last_system_content["text"] = messages[0]["content"]
            return real_call_llama(messages, controls)
        llama_anaxi.call_llama = capturing_call_llama

        return llama_anaxi.run_waking_turn(orch, prompt)

    try:
        # === Case 1: NEGATIVE gate -> not_authorized -- now SILENT (bounded-
        # clause UX correction). The routine internal control-plane fact is
        # not announced; the reply is Clark's ordinary prose alone. ===
        model_calls["count"] = 0
        model_calls["construction_count"] = 0
        last_system_content.pop("text", None)
        result = run_case("Don't save this.", {}, "That's an interesting thought though.")
        check("NEGATIVE/not_authorized: reply is Clark's prose ALONE, no bounded-clause prefix "
              "of any kind",
              result["reply"] == "That's an interesting thought though.")
        check("NEGATIVE/not_authorized: the literal old/new clause text does not appear anywhere "
              "in the displayed reply",
              "No new long-term memory entry was created from that." not in result["reply"]
              and "Nothing was saved from that." not in result["reply"])
        check("NEGATIVE/not_authorized: zero CONSTRUCTION calls (gate rejects before any "
              "construction call; Pass 2's prose call still genuinely happens, by design, and "
              "is not counted here)",
              model_calls["construction_count"] == 0)
        check("NEGATIVE/not_authorized: Gemma's system message is NOT told any clause was "
              "already spoken (nothing was actually shown to suppress-repeat)",
              "already been told to the person you're talking with" not in last_system_content.get("text", ""))
        check("NEGATIVE/not_authorized: the underlying operation_status still mechanically "
              "equals 'not_authorized' -- silencing the UX did not silence the decision",
              result["operation_status"] == "not_authorized")
        check("NEGATIVE/not_authorized: artifact authorization semantics are unchanged -- no "
              "artifact was created merely because the clause is suppressed",
              result["artifact_result"]["artifact_created"] is False
              and result["artifact_result"]["detail"]["status"] == "no_artifact_requested")
        check("PASS-2 THINK: the single call this case makes is the pass-2 prose call, and it "
              "explicitly passes think=False",
              last_think_calls == [{"format": None, "think": False}])

        # === Case 2: insufficient_information -- exercises BOTH a real pass-1
        # construction call (format='json') and the pass-2 prose call, so this
        # is where pass-1-vs-pass-2 think behavior is genuinely distinguished,
        # not just asserted from a case with only one call. ===
        model_calls["count"] = 0
        result = run_case("Please save this thought about something.",
                           {"identified_referent": None, "construction_status": "insufficient_information",
                            "artifact_type": None, "artifact_scope": None, "namespace": None,
                            "title": None, "what_they_shared": None, "content": None},
                           "I wasn't sure what to hold onto there.")
        # OWC5-P4 (Gate A): ask_llama_for_json() now explicitly sets
        # think=False unconditionally -- a mechanically-proven fix for
        # the 4096-token context-starvation failure this exact call
        # shares with OWC5's own Pass-1 (both go through this same
        # function). This assertion is updated to match that
        # intentional, already-shipped, separately-tested change --
        # it previously asserted the OLD (pre-Gate-A) behavior.
        check("PASS-1 THINK: the construction (format='json') call explicitly passes think=False "
              "(OWC5-P4) -- the same fix already proven for OWC5 Pass-1, since both share "
              "ask_llama_for_json()",
              any(c["format"] == "json" and c["think"] is False for c in last_think_calls))
        check("PASS-2 THINK: the prose call in the SAME turn explicitly passes think=False",
              any(c["format"] is None and c["think"] is False for c in last_think_calls))
        check("insufficient_information: reply starts with the exact clause",
              result["reply"].startswith("There wasn't enough information to identify what to save."))

        # === Case 3: failed (a real _failed() trigger -- invalid artifact_type) ===
        model_calls["count"] = 0
        result = run_case("Please save this poem I wrote.",
                           {"identified_referent": "this poem I wrote", "construction_status": "success",
                            "artifact_type": "poem", "artifact_scope": "personal", "namespace": "journal",
                            "title": "x", "what_they_shared": None, "content": "x"},
                           "That's a lovely line.")
        check("failed: reply starts with the exact clause",
              result["reply"].startswith("That wasn't saved."))

        # === Case 4: stale_schema -- THE targeted fix ===
        model_calls["count"] = 0
        result = run_case("Please save this.",
                           {"create_artifact": True, "reason": "old shape", "artifact_type": "journal",
                            "artifact_scope": "personal", "namespace": "journal", "title": "x", "content": "x"},
                           "Let's talk about something else.")
        check("stale_schema: reply starts with the stale_schema-specific clause",
              result["reply"].startswith("A new long-term memory entry could not be created from that."))
        check("stale_schema: detail.status is GENUINELY 'stale_schema', not silently 'insufficient_information'",
              result["artifact_result"]["detail"]["status"] == "stale_schema")

        # === Case 4b: not_authorized and stale_schema are no longer textually
        # identical -- the ambiguity a bare "reply starts with the SAME clause
        # text as not_authorized" comment used to paper over, prior to the
        # bounded-clause semantic hardening. ===
        from bounded_clause import render_bounded_clause
        check("not_authorized and stale_schema clauses are now textually distinct",
              render_bounded_clause("not_authorized") != render_bounded_clause("stale_schema"))

        # === Case 5: genuine success, real title ===
        model_calls["count"] = 0
        result = run_case("Please save this thought: simplicity matters.",
                           {"identified_referent": "simplicity matters", "construction_status": "success",
                            "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
                            "title": "A Note on Simplicity", "what_they_shared": None,
                            "content": "Simplicity really does matter."},
                           "I liked that one.")
        check("success: reply starts with the clause using the REAL title from construction",
              result["reply"].startswith("Saved: A Note on Simplicity."))
        check("success: Clark's own prose is genuinely appended after the clause",
              result["reply"] == "Saved: A Note on Simplicity. I liked that one.")

        # === Case 6: confirm the actual prompt wiring, not just the final string shape ===
        check("Clark's system message genuinely contains the 'already been told' instruction",
              "already been told to the person you're talking with" in last_system_content.get("text", ""))
        check("Clark's system message genuinely contains the real clause text verbatim",
              "Saved: A Note on Simplicity." in last_system_content.get("text", ""))

        # === Case 7: bounded-clause repetition (the demonstrated first-native-
        # turn failure mode) drives the SAME regeneration/suppression seam as
        # the three frozen constructions, through the real run_waking_turn().
        # Uses insufficient_information (a status that STILL shows a visible
        # clause post-silencing -- not_authorized no longer has one to
        # repeat, see Case 1) so this genuinely exercises the screen.
        # build_fake_ollama() returns the identical prose_response on every
        # prose call, so the regen attempt repeats it too -- exercising the
        # suppress_matched_sentences() fallback path, not just first-try
        # acceptance. ===
        model_calls["count"] = 0
        model_calls["construction_count"] = 0
        repeating_prose = ("There wasn't enough information to identify what to save. "
                            "It is genuinely good to hear from you.")
        result = run_case("Please save this thought about something.",
                           {"identified_referent": None, "construction_status": "insufficient_information",
                            "artifact_type": None, "artifact_scope": None, "namespace": None,
                            "title": None, "what_they_shared": None, "content": None},
                           repeating_prose)
        check("BOUNDED-CLAUSE-REPEAT: regeneration was invoked (1 construction call + 2 prose calls)",
              model_calls["construction_count"] == 1 and model_calls["count"] == 3)
        check("BOUNDED-CLAUSE-REPEAT: assembled reply contains the clause exactly ONCE, never doubled",
              result["reply"].count("There wasn't enough information to identify what to save.") == 1)
        check("BOUNDED-CLAUSE-REPEAT: reply matches the exact non-doubled expected string",
              result["reply"] == "There wasn't enough information to identify what to save. "
                                  "It is genuinely good to hear from you.")
        prose_think_calls = [c["think"] for c in last_think_calls if c["format"] is None]
        check("PASS-2 THINK: BOTH the initial pass-2 call AND its regeneration/retry explicitly "
              "pass think=False -- the retry is semantically another pass-2 conversational "
              "generation, not a different call site that could fall back to the default",
              prose_think_calls == [False, False])

        # === Case 8: not_authorized has NO clause to screen against -- the
        # repetition check is a structural no-op here, never invoked as an
        # error, never blocking ordinary prose that happens to contain
        # similar words. ===
        model_calls["count"] = 0
        model_calls["construction_count"] = 0
        result = run_case("Don't save this.", {},
                           "Nothing was saved from that, but it's still good to talk.")
        check("not_authorized: prose that happens to contain clause-like words is NOT screened "
              "or regenerated (there is no clause to compare it against)",
              model_calls["count"] == 1)
        check("not_authorized: that prose passes through completely unmodified",
              result["reply"] == "Nothing was saved from that, but it's still good to talk.")

        # === SENTINEL PROOF: the rebound record_observation() call
        # surface never touches the real production-relative default
        # path, even when a file happens to already sit exactly there.
        # Run inside a throwaway CWD (never the live project directory)
        # with a sentinel file planted at the CWD-relative default name
        # signal_observation_log.LOG_FILE resolves to -- prove it is
        # byte-identical afterward. ===
        import shutil
        sentinel_cwd = tempfile.mkdtemp(prefix="anaxi_bounded_clause_sentinel_cwd_")
        sentinel_path = os.path.join(sentinel_cwd, signal_observation_log.LOG_FILE)
        sentinel_content = b'{"pre_existing": "sentinel, must not be touched"}\n'
        with open(sentinel_path, "wb") as f:
            f.write(sentinel_content)
        original_cwd = os.getcwd()
        try:
            os.chdir(sentinel_cwd)
            run_case("Don't save this.", {}, "sentinel-proof prose reply")
        finally:
            os.chdir(original_cwd)
        with open(sentinel_path, "rb") as f:
            sentinel_after = f.read()
        check("SENTINEL PROOF: a pre-existing file at the production-relative default path "
              "(signal_observations.jsonl) remains byte-identical after a full run_case() turn "
              "-- the rebound wrapper never opened it",
              sentinel_after == sentinel_content)
        shutil.rmtree(sentinel_cwd, ignore_errors=True)
    finally:
        import shutil
        shutil.rmtree(test_workspace, ignore_errors=True)
        shutil.rmtree(test_staging_dir, ignore_errors=True)
        shutil.rmtree(test_signal_log_dir, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
