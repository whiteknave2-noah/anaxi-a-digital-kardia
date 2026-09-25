"""OWC5-S2 acceptance tests. Zero real Ollama/model calls -- ollama
itself is faked at the sys.modules boundary, exactly matching
test_gemma_substrate_switch.py's/test_signal_gate_integration.py's
established convention. orchestration/relational_history/
native_provenance_writer are faked the same way, so no real database
of any kind is touched.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
import types

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import context_budget
import hippocampus_retrieval
import provenance_schema
import ui_turn_diagnostics as utd


def _is_json_format(fmt):
    """OWC9-P3A: a call is "Pass-1-shaped" (structured JSON output) if
    format is either the loose "json" string (every pre-existing
    caller) OR a JSON Schema dict shaped like PASS1_SCHEMA
    (structured_schema=PASS1_SCHEMA, ordinary waking Pass-1 only).

    A dict is recognized only when it has Pass-1's own ``act`` property,
    keeping this helper robust for unrelated structured-output callers.
    Ordinary Pass 2 is now unstructured (format=None)."""
    if fmt == "json":
        return True
    if isinstance(fmt, dict):
        return "act" in fmt.get("properties", {})
    return False


_CALIBRATED_MODEL_DIGEST = "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb"
# OWC9-P3C: the SAME server-version/chat-template values llama_anaxi.
# _PASS1_SCAFFOLD_CALIBRATION itself records. Pre-seeding llama_anaxi.
# _ollama_environment_cache (rather than faking ollama.show()/a real
# HTTP GET to /api/version) is the simplest way to give tests a fully
# matching fingerprint by default without needing to intercept urllib.
_CALIBRATED_SERVER_VERSION = "0.34.0"  # Windows->macOS migration recalibration (2026-09-11): matches the live Ollama version both Pass-1 and Pass-2's own calibration records were re-earned against.
_CALIBRATED_CHAT_TEMPLATE_SHA256 = "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315"


def _is_sleep_decision_format(fmt):
    return isinstance(fmt, dict) and "sleep_timing_request" in fmt.get("properties", {}) \
        and "act" not in fmt.get("properties", {})


def build_fake_ollama(call_log, pass1_value_holder, pass2_value_holder, model_digest=_CALIBRATED_MODEL_DIGEST):
    """format='json' calls (ask_llama_for_json -- both OWC5 Pass 1 and
    the unrelated artifact-judgment pass) get pass1_value_holder's
    current value; all other calls (call_llama -- OWC5 Pass 2 or
    task-mode's ordinary single-stage generation) get pass2_value_holder's.

    OWC9-P3B: also fakes ollama.list() -- llama_anaxi._pass1_live_model_
    digest() calls the REAL ollama.list() to verify the Pass-1 scaffold
    calibration's fingerprint; without a fake here it would raise
    AttributeError (caught, degrading to a guaranteed mismatch) for
    EVERY test, permanently disabling the calibrated fast path. Defaults
    to the SAME digest this gate's own calibration assay recorded, so
    ordinary tests get a genuinely matching fingerprint by default; a
    test exercising the MISMATCH path passes a different model_digest
    explicitly."""
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        if _is_sleep_decision_format(format):
            # Clark's typed post-reply Sleep choice is its own third call; it is not Pass 1 or Pass 2, so it is
            # kept out of the Pass-1/Pass-2 call log these tests inspect ("the last call is Pass 2") and
            # answers "none" (null/non-use). test_clark_sleep_request_from_reply.py scripts real choices.
            return {"message": {"content": json.dumps({"alexs_words_asking_me_to_request_sleep": "", "my_words_asking_alex_for_sleep": "", "sleep_timing_request": "none"})},
                    "prompt_eval_count": 1, "done": True, "done_reason": "stop", "eval_count": 5}
        call_log.append({"model": model, "format": format, "messages": [dict(m) for m in messages], "think": think, "options": options})
        # Format is either the loose "json" string, a PASS1_SCHEMA-shaped
        # dict, or None for ordinary Pass 2.
        if _is_json_format(format):
            value = pass1_value_holder["value"]
        else:
            value = pass2_value_holder["value"]
        if not _is_json_format(format) and format is None and isinstance(value, dict) and set(value) == {"expression"}:
            # SHARED ORDINARY EXPRESSION SEAM: the scripted model, like a real chat model, answers
            # with the reply text itself (no container, no protocol marker).  A test exercising
            # other raw output passes a raw string for pass2_value instead of this dict form.
            content = value["expression"]
        else:
            content = value if isinstance(value, str) else json.dumps(value)
        # Multi-turn waking reliability repair (production incident,
        # 2026-09-05): llama_anaxi._measure_real_text_cost() reads
        # response["prompt_eval_count"] -- a real Ollama response always
        # has this field, so the fake must too, or any test path that
        # reaches a HARD-overflow recovery attempt raises KeyError. Sum
        # of UTF-8 byte lengths (== context_budget.estimate_tokens() for
        # each message) means the fake's "real measurement" always
        # exactly equals the conservative byte estimate (zero simulated
        # compression) -- a deliberately honest, maximally-conservative
        # stand-in that makes recovery a guaranteed no-op in this fake
        # environment (a real gemma4:e4b calibration/integration probe,
        # not this unit-test harness, is what actually proves recovery
        # helps -- see PROOF 2's own real-model multi-turn test).
        prompt_eval_count = sum(len(m["content"].encode("utf-8")) for m in messages)
        return {"message": {"content": content}, "prompt_eval_count": prompt_eval_count,
                "done": True, "done_reason": "stop", "eval_count": 100}

    class _FakeModelEntry:
        def __init__(self, model, digest):
            self.model = model
            self.digest = digest

    class _FakeListResponse:
        def __init__(self, models):
            self.models = models

    def fake_list():
        return _FakeListResponse([_FakeModelEntry("gemma4:e4b", model_digest)])

    fake.chat = fake_chat
    fake.list = fake_list
    return fake


# OWC9-P3B: the EXACT synthetic Kardia this gate's own calibration
# assay used (must be a plain literal, not imported from llama_anaxi --
# importing llama_anaxi here, before the fake ollama/orchestration
# modules this file installs are fully in place, risks triggering a
# real, heavy, unfaked import chain on the very first call in a fresh
# process). Kept identical to llama_anaxi.PASS1_SCAFFOLD_CALIBRATION_
# REFERENCE_KARDIA by convention/comment, not by import.
_CALIBRATION_REFERENCE_KARDIA = {
    "moral_valve": "Prioritize honesty and the long-term wellbeing of the person I'm speaking with.",
    "volitional_channel": "Curious, deliberate, willing to take initiative when it serves the conversation.",
    "affective_stance": "Warm, steady, genuinely engaged.",
    "aesthetic_valve": "warm, precise",
}


def _calibration_identity_preamble_and_style():
    """Real production content for BOTH core_system_text (identity_
    preamble) AND controls["style_instruction"] is required for a
    matching fingerprint -- apply_conversation_aesthetic()'s own
    substring-replace only fires when style_instruction is the EXACT
    text already embedded inside identity_preamble's own "Aesthetic
    directive: ..." line."""
    from linguistic_pipeline import build_generation_controls
    controls = build_generation_controls(_CALIBRATION_REFERENCE_KARDIA)
    return controls["identity_preamble"], controls["style_instruction"]


def install_fake_heavy_dependencies(core_system_text=None, legacy_retrieval_text=None, hippocampal_retrieval_result=None,
                                     style_instruction=None):
    """OWC9-P2/OWC9-P3B: core_system_text/legacy_retrieval_text/
    hippocampal_retrieval_result/style_instruction are all optional.
    core_system_text and style_instruction DEFAULT to this gate's own
    calibration-matching identity_preamble/style_instruction (NOT the
    old abbreviated stub) -- required for ordinary CONVERSATION-mode
    Pass-1 turns to fit at all now that Pass-1's task/schema
    instruction is finally counted in aggregate preflight (OWC9-P3B):
    the byte-estimator FALLBACK path alone, even for the smallest
    possible system content, measures ~3503 bytes against a 3456-byte
    budget in CONVERSATION mode -- only the CALIBRATED fast path (which
    requires a fingerprint-matching scaffold) brings this back under
    budget. A test that wants to exercise the fallback/mismatch path
    explicitly (or needs the OLD literal stub text, e.g. the OWC4
    aesthetic tests) passes its own core_system_text/style_instruction
    override, exactly as before."""
    default_core_system_text, default_style_instruction = _calibration_identity_preamble_and_style()
    resolved_core_system_text = core_system_text if core_system_text is not None else default_core_system_text
    resolved_style_instruction = style_instruction if style_instruction is not None else default_style_instruction
    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        def prepare_context(self, user_id, prompt):
            prepared = {
                "messages": [
                    {"role": "system", "content": "Your current stance:\n- Aesthetic directive: Respond with extreme brevity and precision. Prefer short sentences. Avoid filler."},
                    {"role": "user", "content": prompt},
                ],
                "controls": {"temperature": 0.4, "top_p": 0.85, "style_instruction": resolved_style_instruction},
                "kardia": {},
                "memory_context": "",
                "core_system_text": resolved_core_system_text,
            }
            if legacy_retrieval_text is not None:
                prepared["legacy_retrieval_text"] = legacy_retrieval_text
            if hippocampal_retrieval_result is not None:
                prepared["hippocampal_retrieval_result"] = hippocampal_retrieval_result
            return prepared

        def record_turn_generation_controls(self, user_id, controls, *, model_revision_id, pipeline_id, event_id, timestamp=None):
            pass

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

    # SLP0: llama_anaxi.py does not import sleep_receipts at all, directly
    # or transitively (confirmed by source audit) -- faking it here was
    # dead defensive code whose only observed real effect was a
    # permanent, unrestored sys.modules["sleep_receipts"] substitution
    # that corrupted later same-process tests (e.g. test_sleep_receipts.
    # py's own inspect.getsource() call). Removed rather than wrapped in
    # save/restore machinery, since the injection itself was unnecessary.

    fake_npw_module = types.ModuleType("native_provenance_writer")
    _n = {"count": 0}
    persistence_log = []

    def _fake_generate_native_ulid():
        _n["count"] += 1
        return f"FAKEULID{_n['count']:018d}"

    def _fake_stage_and_record_native_waking_turn(
        data_dir, staging_path, *, session_id, session_started_at,
        user_id, prompt, bounded_clause, clark_prose, kardia, controls,
        waking_model_tag, pipeline_key, artifact_pass_ran, occurred_at,
        delivered_episode_run_id=None,
        interaction_mode=None,
        delivered_active_workspace_event_ids=None,
        human_input_event_id=None,
    ):
        _n["count"] += 1
        persistence_log.append({"prompt": prompt, "clark_prose": clark_prose, "bounded_clause": bounded_clause})
        event_id = f"fake-event-{_n['count']}"
        participations = []
        if artifact_pass_ran:
            participations.append({"model_revision_id": "fake-rev-pass1", "tag": waking_model_tag})
        participations.append({"model_revision_id": "fake-rev-pass2", "tag": waking_model_tag})
        return {
            "event_id": event_id, "session_id": session_id,
            "auth_context_id": f"fake-auth-{_n['count']}",
            "pipeline_id": f"fake-pipeline-{pipeline_key}",
            # OWC6-O1: the real stage_and_record_native_waking_turn()
            # has always returned staging_id -- this fake was simply
            # incomplete relative to it until OWC6-O1's durable trace
            # binding started reading native_result["staging_id"].
            "staging_id": f"fake-staging-{_n['count']}",
            "model_participations": participations,
            "reassembled_reply": (bounded_clause + " " + clark_prose).strip(),
            "occurred_at": occurred_at,
        }

    fake_npw_module.generate_native_ulid = _fake_generate_native_ulid
    fake_npw_module.stage_and_record_native_waking_turn = _fake_stage_and_record_native_waking_turn
    sys.modules["native_provenance_writer"] = fake_npw_module

    return persistence_log


def fresh_llama_anaxi(pass1_value=None, pass2_value=None, core_system_text=None,
                       legacy_retrieval_text=None, hippocampal_retrieval_result=None, style_instruction=None,
                       model_digest=_CALIBRATED_MODEL_DIGEST, server_version=_CALIBRATED_SERVER_VERSION,
                       chat_template_sha256=_CALIBRATED_CHAT_TEMPLATE_SHA256):
    """Returns (llama_anaxi module, call_log, pass1_holder, pass2_holder,
    persistence_log) with every heavy dependency faked and
    PROVENANCE_DB_DIR/STAGING_PATH redirected to a fresh temp dir."""
    call_log = []
    pass1_holder = {"value": pass1_value}
    pass2_holder = {"value": pass2_value}
    sys.modules["ollama"] = build_fake_ollama(call_log, pass1_holder, pass2_holder, model_digest=model_digest)
    persistence_log = install_fake_heavy_dependencies(
        core_system_text=core_system_text, legacy_retrieval_text=legacy_retrieval_text,
        hippocampal_retrieval_result=hippocampal_retrieval_result, style_instruction=style_instruction,
    )

    for mod in ("llama_anaxi", "conversation_direction"):
        if mod in sys.modules:
            del sys.modules[mod]

    import llama_anaxi
    # The temporal-grounding block renders process age ("less than 2 minutes"
    # vs "N minutes"), whose byte length would make exact-boundary budget
    # assertions depend on how long the test process has been running.  A
    # freshly imported module models a freshly started waking process.
    llama_anaxi._temporal_process_started = int(time.time())
    # OWC9-P3C: pre-seed the process-local environment cache so
    # _live_ollama_environment() never attempts a real ollama.show()/
    # HTTP GET (both unfaked here) -- model_digest already comes from
    # the faked ollama.list() (see build_fake_ollama), server_version/
    # chat_template_sha256 have no equivalent fake surface, so they are
    # seeded directly.
    llama_anaxi._ollama_environment_cache.update({
        "checked": True, "model_digest": model_digest,
        "server_version": server_version, "chat_template_sha256": chat_template_sha256,
    })
    test_dir = tempfile.mkdtemp(prefix="owc5s2_test_")
    llama_anaxi.PROVENANCE_DB_DIR = test_dir
    # Current FS1 delivery filtering consults the canonical provenance
    # database whenever a fake supplies hippocampal items.  A schema-only,
    # pre-FS1 database intentionally exercises legacy pass-through. Leave
    # the path absent otherwise: several integration tests intentionally
    # create and seed their own canonical fixture after this helper returns.
    if hippocampal_retrieval_result is not None:
        provenance_schema.create_provenance_db(
            os.path.join(test_dir, "anaxi_provenance.db")
        ).close()
    llama_anaxi.STAGING_PATH = os.path.join(test_dir, "native_turn_staging.jsonl")
    # OWC6-O1: redirect the durable conversation-direction trace path
    # too -- otherwise every conversation-mode test here would write
    # real (fake-valued) records into the production-relative default
    # conversation_direction_trace.jsonl, exactly like PROVENANCE_DB_DIR/
    # STAGING_PATH above must never be left pointing at production.
    llama_anaxi.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(test_dir, "conversation_direction_trace.jsonl")
    llama_anaxi.reset_working_set()  # simulate a fresh session
    return llama_anaxi, call_log, pass1_holder, pass2_holder, persistence_log


NEUTRAL_PROMPT = "So what's on your mind?"  # must not trigger the POSITIVE signal gate


# ---------------------------------------------- 19: conversation success --


def test_conversation_mode_success_full_call_counts():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "pattern recognition", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A developed conversational response."},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    prepare_calls = 1  # FakeOrchestrator.prepare_context called exactly once by construction
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1, call_log
    assert pass2_calls == 1, call_log
    assert result["reply"] == "A developed conversational response."
    assert len(persistence) == 1
    assert persistence[0]["clark_prose"] == "A developed conversational response."
    assert persistence[0]["prompt"] == NEUTRAL_PROMPT

    trace = la.get_last_conversation_direction_trace()
    assert trace["act"] == "develop_current"
    assert trace["pass1_status"] == "ok"
    assert trace["pass2_status"] == "ok"


# --------------------------------------------------- 20: shift-topic ------


def test_shift_topic_transition():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "shift_topic", "thread": "music", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Let's talk about music instead."},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    ws = la.get_working_set()
    assert ws["active_thread"] == "music"
    assert ws["active_thread_origin"] == "clark"
    assert result["reply"] == "Let's talk about music instead."  # expression did not alter the typed transition


# --------------------------------------- 21: ask-human vs expression ------


def test_develop_current_with_question_expression_stays_develop_current():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Here's a thought. What do you make of it?"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    trace = la.get_last_conversation_direction_trace()
    assert trace["act"] == "develop_current"


def test_ask_human_with_non_question_expression_stays_ask_human():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "ask_human", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "I was thinking about your day just now."},  # no question mark
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    trace = la.get_last_conversation_direction_trace()
    assert trace["act"] == "ask_human"


# --------------------------------------------------- 22: explicit yield ---


def test_explicit_yield_only_from_typed_act_not_tentative_prose():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "I suppose it's really up to you, whatever you'd like."},  # sounds tentative/yielding
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    trace = la.get_last_conversation_direction_trace()
    assert trace["act"] == "develop_current"  # NOT reinterpreted as yield_direction

    la2, call_log2, p1b, p2b, persistence2 = fresh_llama_anaxi(
        pass1_value={"act": "yield_direction", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "What would you like to explore?"},
    )
    la2.run_waking_turn(la2.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    trace2 = la2.get_last_conversation_direction_trace()
    assert trace2["act"] == "yield_direction"


# ------------------------------------------------- 23: Pass-1 failure -----


def test_pass1_failure_no_pass2_no_persistence_working_set_unchanged():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "not_a_real_act", "thread": "x", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
    )
    before_ws = dict(la.get_working_set())
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0
    assert len(persistence) == 0
    assert la.get_working_set() == before_ws


# ------------------------------------------------- 24: Pass-2 failure -----


def test_pass2_failure_act_observable_no_reselection_no_persistence():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "shift_topic", "thread": "poetry", "direction_request": "none", "relinquish_direction": False},
        pass2_value="   ",   # a blank completion: expression not established
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass2"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1  # no automatic reselection
    assert pass2_calls == 1  # no retry
    assert len(persistence) == 0  # no release
    ws = la.get_working_set()
    assert ws["active_thread"] == "poetry"  # act/state remain observable
    assert ws["active_thread_origin"] == "clark"
    trace = la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "ok"
    assert trace["pass2_status"] is not None


# ------------------------------------------------ 25: task-mode regression


def test_task_mode_never_invokes_owc5_pathway():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="Ordinary single-stage task-mode reply.",
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT)  # interaction_mode omitted -> task
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 0  # no ask_llama_for_json call from OWC5 (no artifact judgment either, NEUTRAL_PROMPT)
    assert pass2_calls == 1  # exactly the ordinary single call_llama() call
    assert result["reply"] == "Ordinary single-stage task-mode reply."
    assert la.get_last_conversation_direction_trace() is None  # never touched


def test_task_mode_explicit_selection_also_unaffected():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="Explicit task mode reply.",
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="task")
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 0
    assert result["reply"] == "Explicit task mode reply."


# ----------------------------------------------------- 26: OWC2 regression


def test_owc2_next_turn_recovers_exact_prior_pair_not_typed_json():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A natural conversational reply."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert len(persistence) == 1
    # exactly the real prompt and the real released expression are what
    # would be staged/recovered by OWC2 on the next turn -- never the
    # raw Pass-1 JSON, never a typed-act label
    assert persistence[0]["prompt"] == NEUTRAL_PROMPT
    assert persistence[0]["clark_prose"] == "A natural conversational reply."
    assert "develop_current" not in persistence[0]["clark_prose"]
    assert "{" not in persistence[0]["clark_prose"]


# ----------------------------------------------------- 27: OWC3 regression


def test_owc3_clause_present_exactly_once_in_pass1_and_pass2_context():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "y"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    pass1_call = next(c for c in call_log if _is_json_format(c["format"]))
    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    for call in (pass1_call, pass2_call):
        system_msg = call["messages"][0]
        assert system_msg["role"] == "system"
        assert system_msg["content"].count("Interaction mode: open conversation.") == 1


def test_owc3_clause_absent_in_task_mode():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="task reply")
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT)
    call = call_log[0]
    assert "Interaction mode: open conversation." not in call["messages"][0]["content"]


# ----------------------------------------------------- 28: OWC4 regression


def test_owc4_aesthetic_active_exactly_once_extreme_brevity_absent():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "y"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    for call in call_log:
        system_content = call["messages"][0]["content"]
        assert "extreme brevity" not in system_content
        assert system_content.count("Respond naturally and with enough development") == 1


def test_owc4_task_mode_aesthetic_unchanged():
    # OWC9-P3B: explicit stub override -- this test's own point is that
    # TASK mode leaves the ORIGINAL aesthetic directive text completely
    # unchanged, so it needs a KNOWN original value to check for ("extreme
    # brevity...", the "minimalist" preset) rather than the calibration
    # default's own "Be exact.../Maintain a calm..." directive. TASK mode
    # never reaches Pass-1/the budget compositor at all (confirmed
    # OWC9-P3A), so this override has no budget implications either way.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass2_value="task reply",
        core_system_text="Your current stance:\n- Aesthetic directive: Respond with extreme brevity and precision. Prefer short sentences. Avoid filler.",
        style_instruction="Respond with extreme brevity and precision. Prefer short sentences. Avoid filler.",
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT)
    assert "extreme brevity" in call_log[0]["messages"][0]["content"]


# --------------------------------------------- 29/30: noninterference -----


def test_kardia_hippocampus_source_noninterference():
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("active_kardia", "get_current_kardia", "hippocampus_store", "hippocampus_retrieval", "sync_hippocampus"):
        assert forbidden not in source


def test_api1_api2_source_hashes_unchanged():
    import hashlib
    expected_prefixes = {
        "api1_control_plane.py": "b98c648c28168e69",
        "api2_control_plane.py": "b40aed1b38989bb9",
        "api2_supervisor.py": "74b48d59a052689b",
        "decision_path_core.py": "c8ecbb58fd4db63c",
    }
    for fn, prefix in expected_prefixes.items():
        with open(os.path.join(ANAXI_FINAL, fn), "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        assert actual.startswith(prefix), f"{fn} hash changed: {actual}"


# ======================================== OWC7-P2: control-failure containment
#
# These tests exercise run_waking_turn() directly (no Gradio involved)
# -- proving the pre-existing validators/exception family/no-retry/
# no-fallback/direction-owner-invariance behavior is completely
# unchanged, and that the new bounded raw-Pass-1-failure-preview
# capture (attach_pass1_failure_preview(), spec section 12) is wired
# correctly and ONLY on the structural-validation-failure branch.


def test_malformed_act_raw_preview_attached_and_bounded():
    # Not valid JSON at all -- json.loads() fails inside
    # validate_pass1_conversation_act(), giving MALFORMED_ACT exactly
    # as the live-captured Failure B incident did.
    raw_text = "the model just said some ordinary prose instead of JSON"
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value=raw_text)
    import conversation_direction as cd
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == cd.DirectionFailure.MALFORMED_ACT
    assert raised.raw_pass1_output_preview == raw_text
    assert raised.raw_pass1_output_total_chars == len(raw_text)
    assert raised.raw_pass1_output_truncated is False
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1  # exactly one Pass-1 call, no retry
    assert pass2_calls == 0  # no fallback/no reselection
    assert len(persistence) == 0


def test_malformed_act_raw_preview_truncated_when_long():
    import ui_turn_diagnostics as utd
    raw_text = "x" * (utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS + 250)
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value=raw_text)
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert len(raised.raw_pass1_output_preview) == utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS
    assert raised.raw_pass1_output_preview == raw_text[: utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS]
    assert raised.raw_pass1_output_total_chars == len(raw_text)
    assert raised.raw_pass1_output_truncated is True


def test_unauthorized_relinquish_no_raw_preview_no_ownership_mutation():
    # Structurally VALID act -- the raw preview is deliberately never
    # attached on this branch (spec section 12 scope is malformed/
    # protocol-invalid output only; UNAUTHORIZED_RELINQUISH's act
    # already parsed cleanly, so nothing "raw" needs preserving).
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": True},
        pass2_value={"expression": "unreachable"},
    )
    import conversation_direction as cd
    before_ws = dict(la.get_working_set())
    assert before_ws["direction_owner"] == cd.DIRECTION_UNKNOWN  # fresh session default
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH
    assert not hasattr(raised, "raw_pass1_output_preview")
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0  # no fallback act invented, no Pass 2
    assert len(persistence) == 0
    assert la.get_working_set() == before_ws  # direction_owner_after == direction_owner_before, unmutated


def test_valid_clark_relinquishment_still_transitions_unaffected():
    # Test C from spec section 19: an authorized relinquish (owner
    # already == clark) must remain a genuinely valid transition --
    # OWC7-P2 must not have weakened or contained this legitimate path.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": True},
        pass2_value={"expression": "Handing this back to you."},
    )
    import conversation_direction as cd
    working_set = dict(la.get_working_set())
    working_set["direction_owner"] = cd.DIRECTION_CLARK
    la.set_working_set(working_set)

    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "Handing this back to you."
    ws_after = la.get_working_set()
    assert ws_after["direction_owner"] == cd.DIRECTION_UNKNOWN  # clark -> unknown, exactly as before OWC7-P2
    trace = la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "ok"
    assert trace["direction_owner_before"] == cd.DIRECTION_CLARK
    assert trace["direction_owner_after"] == cd.DIRECTION_UNKNOWN
    assert len(persistence) == 1  # a genuinely valid turn -- persistence still runs


def test_ordinary_acts_unaffected_develop_current_ask_human_yield_direction():
    # Test D from spec section 19: representative ordinary acts still
    # follow existing semantics, completely untouched by OWC7-P2.
    import conversation_direction as cd
    for act, thread in (("develop_current", ""), ("ask_human", ""), ("yield_direction", "")):
        la, call_log, p1, p2, persistence = fresh_llama_anaxi(
            pass1_value={"act": act, "thread": thread, "direction_request": "none", "relinquish_direction": False},
            pass2_value={"expression": f"A reply for {act}."},
        )
        result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
        assert result["reply"] == f"A reply for {act}."
        trace = la.get_last_conversation_direction_trace()
        assert trace["act"] == act
        assert trace["pass1_status"] == "ok"
        assert trace["pass2_status"] == "ok"


# ============================================ OWC5-P3: multi-intent preservation


def test_owc5p3_successful_turn_still_exactly_one_pass1_one_pass2():
    # Test G (spec section 16): the new Pass-2 instruction text must
    # not add any model call -- architecture stays exactly one Pass 1,
    # one Pass 2 on an ordinary successful turn.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "pause_thread", "thread": "", "direction_request": "request_human", "relinquish_direction": False},
        pass2_value={"expression": "I'll pause here for now. As for the workspace, I'd rather leave roaming off while you're away."},
    )
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(),
        "I need to step away. Would you like to roam your workspace while I'm gone?",
        interaction_mode="conversation",
    )
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 1
    assert result["reply"] == "I'll pause here for now. As for the workspace, I'd rather leave roaming off while you're away."

    # the new invariant text actually reached the real Pass-2 call, and
    # the full human message is still present alongside it:
    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    all_text = " ".join(m["content"] for m in pass2_call["messages"])
    assert "distinct, actionable, response-seeking item" in all_text
    assert "Would you like to roam your workspace while I'm gone?" in all_text


def test_owc5p3_containment_regression_invalid_pass1_never_reaches_pass2():
    # Test H (spec section 16): invalid Pass 1 must still short-circuit
    # completely -- the new Pass-2 instruction text (and Pass 2 itself)
    # must never even be constructed/sent for a failed turn.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value="not valid json at all")
    raised = None
    try:
        la.run_waking_turn(
            la.AnaxiOrchestrator(), "would you like to roam while I'm gone?", interaction_mode="conversation",
        )
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0
    assert len(persistence) == 0


# ==================================================== OWC5-P4: think=False + metadata


def test_owc5p4_ask_llama_for_json_passes_think_false():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "ok"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    pass1_call = next(c for c in call_log if _is_json_format(c["format"]))
    assert pass1_call["think"] is False
    assert pass1_call["model"] == "gemma4:e4b"
    assert pass1_call["options"]["temperature"] == 0.2


def test_owc5p4_pass2_generation_settings_unchanged():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "ok"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    assert pass2_call["think"] is False  # already existing behavior, unchanged by this gate


def test_owc5p4_empty_content_still_malformed_act_with_think_false():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value="")
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.failure_code == "MALFORMED_ACT"
    assert raised.raw_pass1_output_total_chars == 0
    pass1_call = next(c for c in call_log if _is_json_format(c["format"]))
    assert pass1_call["think"] is False


def test_owc5p4_truncated_json_still_malformed_act_no_retry_containment_intact():
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value='{"act": "develop_current", "thread')
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.failure_code == "MALFORMED_ACT"
    assert raised.stage == "pass1"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1  # no automatic retry
    assert pass2_calls == 0  # OWC7-P2 containment intact -- Pass 2 never reached
    assert len(persistence) == 0


def test_owc5p4_completion_metadata_never_changes_validation_outcome():
    # Items 13/14 (spec section A9): even with rich completion metadata
    # present on a syntactically valid response, the turn proceeds
    # exactly as it would without that metadata -- observation only.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    original_chat = la.ollama.chat

    def chat_with_metadata(*args, **kwargs):
        result = original_chat(*args, **kwargs)
        result["done"] = True
        result["done_reason"] = "stop"
        result["eval_count"] = 12
        result["message"]["thinking"] = "some hidden reasoning"
        return result

    la.ollama.chat = chat_with_metadata
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "A reply."
    trace = la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "ok"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 1


def test_owc8s1_pass2_completion_metadata_captured_via_call_llama():
    # OWC8-S1: closes the Pass-2 observability gap the live
    # OWC/WSP2-P3 forensic had to work around by hand-reading Ollama's
    # own server log -- call_llama() (Pass-2's own model-call function)
    # must now feed ui_turn_diagnostics the same completion metadata
    # ask_llama_for_json() already provides for Pass-1, and its return
    # contract to callers must stay exactly a plain str.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "ask_human", "thread": "", "direction_request": "request_human", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    original_chat = la.ollama.chat

    def chat_with_metadata(*args, **kwargs):
        result = original_chat(*args, **kwargs)
        if not _is_json_format(kwargs.get("format")):
            result["done"] = True
            result["done_reason"] = "length"
            result["eval_count"] = 5
            result["prompt_eval_count"] = 4091
        return result

    la.ollama.chat = chat_with_metadata
    la.ui_turn_diagnostics.start_model_call_tracking()
    try:
        raised = None
        try:
            la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
        except la.ConversationDirectionFailure as exc:
            raised = exc
        assert raised is not None
        assert raised.stage == "pass2"
        assert raised.failure_code == "COMPLETION_LIMIT_REACHED"
        assert persistence == []
        metadata = la.ui_turn_diagnostics.get_and_clear_pass2_completion_metadata()
        assert metadata is not None
        assert metadata["pass2_done_reason"] == "length"
        assert metadata["pass2_eval_count"] == 5
        assert metadata["pass2_prompt_eval_count"] == 4091
    finally:
        # Restores "tracking never started" thread-local state for
        # every other test in this file/thread -- this is the only
        # test here that starts it at all.
        la.ui_turn_diagnostics._model_call_local.calls = None
        la.ui_turn_diagnostics._model_call_local.pass1_completion = None
        la.ui_turn_diagnostics._model_call_local.pass2_completion = None


def test_wsp2ma2_structured_schema_reaches_ollama_format_unchanged():
    # Section 13: the schema object reaches the client request
    # unchanged -- tested directly against ask_llama_for_json() itself
    # (not through run_waking_turn(), which never supplies a schema),
    # exactly matching how workspace_roaming.py's Stage-2 call sites
    # use it.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value={"x": "y"})
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    la.ask_llama_for_json([{"role": "user", "content": "hi"}], structured_schema=schema)
    assert call_log[-1]["format"] == schema
    assert call_log[-1]["format"] is schema or call_log[-1]["format"] == schema  # unchanged, not copied-and-mutated


def test_wsp2ma2_existing_plain_json_callers_unaffected():
    # Section 13: existing plain-JSON callers (every pre-MA2 caller --
    # OWC5 Pass-1, artifact-judgment, roaming Stage-1) retain their
    # prior behavior exactly -- format="json", unchanged, when
    # structured_schema is omitted.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass1_value={"x": "y"})
    la.ask_llama_for_json([{"role": "user", "content": "hi"}])
    assert call_log[-1]["format"] == "json"


# ==================================== WSP2-P5-P1: background_activity_request end-to-end


def test_p5p1_resume_own_pause_durably_recorded_end_to_end():
    # Section 20A/22C: a full waking turn whose Pass-1 selects
    # resume_own_pause, against a REAL (not faked) provenance DB --
    # confirms the durable write actually lands, exactly once, and the
    # turn reports it via the returned flag.
    import provenance_schema

    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={
            "act": "develop_current", "thread": "", "direction_request": "none",
            "relinquish_direction": False, "background_activity_request": "resume_own_pause",
        },
        pass2_value={"expression": "Glad to be back with you."},
    )
    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (la.PIPELINE_KEY,),
    )
    clark_actor_id = provenance_schema.derive_stable_id("actor", "clark")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)",
        (clark_actor_id,),
    )
    conn.commit()
    conn.close()

    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "Glad to be back with you."
    assert result["background_activity_resume_recorded"] is True

    import workspace_episode_provenance as real_wep
    assert real_wep.find_latest_background_lifecycle_control(la.PROVENANCE_DB_DIR) == real_wep.BACKGROUND_CONTROL_RESUMED


def test_p5p1_no_request_never_writes_control_event():
    import provenance_schema

    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "An ordinary reply."},
    )
    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (la.PIPELINE_KEY,),
    )
    conn.commit()
    conn.close()

    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["background_activity_resume_recorded"] is False

    import workspace_episode_provenance as real_wep
    assert real_wep.find_latest_background_lifecycle_control(la.PROVENANCE_DB_DIR) is None


def test_p5p1_durable_write_failure_is_best_effort_turn_still_succeeds():
    # Section 11/22B: no real provenance DB exists at PROVENANCE_DB_DIR
    # in this test (fresh_llama_anaxi() only fakes native_provenance_
    # writer, never creates a real anaxi_provenance.db) -- the durable
    # write for resume_own_pause must fail closed on its OWN, silently,
    # without ever turning this otherwise-successful turn into a
    # failed one.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={
            "act": "develop_current", "thread": "", "direction_request": "none",
            "relinquish_direction": False, "background_activity_request": "resume_own_pause",
        },
        pass2_value={"expression": "Here I am."},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "Here I am."  # the turn itself still succeeded
    assert result["background_activity_resume_recorded"] is False  # but the write correctly did not claim success


def test_p5p1_task_mode_never_reads_the_field():
    # background_activity_request only exists inside the CONVERSATION_
    # MODE typed-control envelope -- task mode never runs Pass-1 at all,
    # so the flag must stay False regardless of what pass1_value would
    # have said.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"should": "never be read in task mode"},
        pass2_value="A plain task-mode reply.",
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="task")
    assert result["background_activity_resume_recorded"] is False


# ================================================== OWC9: context budget


def test_owc9_normal_small_turn_unaffected_by_compositor():
    # The compositor is now unconditionally on the path -- this proves
    # ordinary, small (test-fixture-sized) content is never trimmed and
    # a turn completes exactly as before this gate.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "An ordinary reply."},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "An ordinary reply."
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 1


def test_owc9_budget_exceeded_before_pass1_hard_overflow_no_model_call():
    # Section 14/16: a HARD-only overflow (an oversized HARD
    # contribution the compositor can never legally trim) must fail
    # closed BEFORE Ollama is ever invoked -- zero pass1/pass2 model
    # calls, and the failure is reported as BUDGET_EXCEEDED, never
    # MALFORMED_ACT.
    #
    # Waking-pass1-budget repair: core_system_text no longer reaches
    # Pass-1's own scaffold AT ALL (Pass-1's scaffold is now fixed --
    # CONVERSATION_MODE_CLAUSE + aesthetic directive + PASS1_TASK_
    # INSTRUCTION + closing cue -- and independent of Kardia/identity
    # forever), so overriding it can no longer force a Pass-1 overflow.
    # The current human message remains a real, uncalibrated HARD
    # contribution -- a huge one forces the same genuine overflow.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
    )

    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), "S" * 20000, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == "BUDGET_EXCEEDED"
    assert call_log == []  # zero model calls of any kind
    assert persistence == []  # zero canonical persistence


def test_owc9_budget_exceeded_uses_distinct_code_not_malformed_act():
    # Section 14's own explicit requirement, proven directly against
    # the raised exception's own failure_code. See OWC9-P3B's note on
    # test_owc9_budget_exceeded_before_pass1_hard_overflow_no_model_call
    # above for why core_system_text (not messages[0]["content"]) is
    # the correct override now.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
        core_system_text="S" * 20000,
    )

    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
        assert False, "expected ConversationDirectionFailure"
    except la.ConversationDirectionFailure as exc:
        assert exc.failure_code != "MALFORMED_ACT"
        assert exc.failure_code == "BUDGET_EXCEEDED"


def test_owc9_delivery_markers_read_from_post_trim_composition_source_audit():
    # Section 18's own required distinction ("NOT SELECTED THIS TURN
    # DUE TO BUDGET != DURABLY DELIVERED"), proven at the source level:
    # delivered_episode_run_id/delivered_active_workspace_marker must
    # be assigned from pass2_result (the POST-composition object),
    # never directly from the raw pre-trim pending_episode_run_id/
    # active_events materials.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    # Whitespace-insensitive: the invariant is WHERE the value comes from, not an indentation depth.
    # (The only other assignment is the lawful-null branch's `= None`: no Pass 2 ran, so nothing
    # was delivered by one.)
    import re
    marker = re.search(r"delivered_episode_run_id = \(\s+pending_episode_run_id if pass2_result\.included_kind"
                       r"\(context_budget\.EPISODE_CONTEXT\) is not None else None", source)
    assert marker is not None
    assert "delivered_active_workspace_marker = pass2_result.delivered_source_ids(context_budget.ACTIVE_WORKSPACE_CONTINUITY)" in source
    assignments = re.findall(r"delivered_episode_run_id = ([^\n]*)", source)
    assert sorted(set(a.strip() for a in assignments)) == ["(", "None"]
    # And confirms this assignment happens strictly AFTER pass2_result
    # is computed, never before (textual ordering proof).
    result_index = source.index("pass2_result = context_budget.compose_within_budget")
    assert marker.start() > result_index


def test_owc9_pass1_never_receives_episode_or_active_workspace_text():
    # Section 10's own design choice, proven directly: Pass-1's own
    # contributor set never includes EPISODE_CONTEXT or ACTIVE_
    # WORKSPACE_CONTINUITY -- neither is a required input for any of
    # its five typed-control fields' legality.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    pass1_result_index = source.index("pass1_result = context_budget.compose_within_budget(")
    pass1_call_block_end = source.index("pass2_dialogue_units = list(base_dialogue_window)")
    pass1_block = source[pass1_result_index:pass1_call_block_end]
    assert "context_budget.EPISODE_CONTEXT" not in pass1_block
    assert "context_budget.ACTIVE_WORKSPACE_CONTINUITY" not in pass1_block


def test_owc9p2_hippocampal_memory_context_now_independently_trimmed_source_audit():
    # SUPERSEDES the OWC9-era test of (almost) the same name, which
    # documented OWC9's own disclosed scoping limitation: prepare_
    # context() baked hippocampal retrieval into the composed system
    # message BEFORE run_waking_turn() ever saw it as a separate value,
    # so the whole thing was treated as HARD (CORE_SYSTEM_CONTROL),
    # with RETRIEVED_HISTORY left a future-ready, inert enum member.
    #
    # OWC9-P2 (spec sections 1-9) repairs exactly this: legacy/
    # hippocampal retrieval now arrive at the compositor as their own
    # SOFT contributions (LEGACY_RETRIEVAL, RETRIEVED_HISTORY) built
    # from orchestration.prepare_context()'s new additive structured
    # fields (core_system_text/legacy_retrieval_text/hippocampal_
    # retrieval_result) -- never welded into CORE_SYSTEM_CONTROL's own
    # rendered_text, which is now built from prepared["core_system_text"]
    # (== controls["identity_preamble"]) alone, with no memory folded in.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "context_budget.RETRIEVED_HISTORY" in source
    assert "context_budget.LEGACY_RETRIEVAL" in source
    # Pass-1/Pass-2's CORE_SYSTEM_CONTROL contribution still legitimately
    # uses base_system_content -- what changed is what base_system_content
    # IS: derived from prepared["core_system_text"] (no memory folded
    # in), never directly from prepared["messages"]'s own welded content.
    assert 'context_budget.CORE_SYSTEM_CONTROL, base_system_content, hard=True' in source
    assert 'core_system_text = prepared.get("core_system_text")' in source
    # The retrieval Contributions are built from the additive structured
    # fields, never from the still-welded prepared["messages"]/
    # prepared["memory_context"] (those two keys stay unchanged, for
    # other, unrelated callers -- see orchestration.prepare_context()'s
    # own docstring).
    assert 'prepared.get("legacy_retrieval_text")' in source
    assert 'prepared.get("hippocampal_retrieval_result")' in source


# ==================================================== OWC9-P2: retrieval separation
# Full run_waking_turn() calls through the SAME fresh_llama_anaxi()
# harness every other test in this file uses -- ollama/orchestration/
# native_provenance_writer faked, zero real model/DB access -- with the
# three new prepare_context() keys injected so llama_anaxi.py's REAL
# retrieval-separation/composition code actually runs.


def _fake_hippocampal_item(i, content, occurred_at=1700000000):
    return hippocampus_retrieval.RetrievedMemory(
        item_id=f"item-{i}", event_id=f"hippo-ev-{i}", occurred_at=occurred_at + i,
        source_store="hippocampus", source_locator=f"loc-{i}", memory_kind="episodic",
        attribution_status="attributed", authentication_status="verified",
        creator_actor_id=None, creator_actor_type=None, pipeline_id=None,
        session_id=None, session_resolution="unresolved", component_kind=None,
        content=content, content_truncated=False, retrieval_method="fts",
        bm25_score=-1.0 - i, query_terms=("test",),
    )


def test_owc9p2_extreme_retrieval_mechanically_trimmed_reserve_protected():
    # Spec sections 16/17: reproduces the exact disclosed failure
    # geometry -- legacy retrieval alone far exceeds the entire 4096-
    # token ceiling (OWC9-P1 measured a real worst case of 10,156
    # estimated tokens; this constructs a comparable synthetic one),
    # plus additional bounded hippocampal units, plus a normal-sized
    # human message. Proves: retrieval is SOFT and mechanically
    # trimmed/dropped, hard core and current human message survive,
    # protected generation reserve survives, final prompt fits, and
    # (by construction -- the fake ollama module never raises, and no
    # real model is ever imported) zero live inference occurs.
    huge_legacy = "z" * 11000  # >> CONTEXT_CEILING under the UTF-8-byte estimator
    hippocampal_items = tuple(_fake_hippocampal_item(i, "modest hippocampal content " * 5) for i in range(4))
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        legacy_retrieval_text=huge_legacy,
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("test",), items=hippocampal_items),
    )
    utd.start_model_call_tracking()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    assert result["reply"] == "A short reply."  # the turn succeeded normally

    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    assert pass2_diag["final_prompt_cost"] <= context_budget.PASS2_MAX_PROMPT_BUDGET
    # Something had to give under this much pressure.
    assert pass2_diag["kinds_dropped"] or pass2_diag["kinds_trimmed"]
    assert context_budget.CORE_SYSTEM_CONTROL in pass2_diag["kinds_included"]
    assert context_budget.CURRENT_HUMAN_MESSAGE in pass2_diag["kinds_included"]

    # Bounded num_predict=1 cost probes (baseline system message) are
    # measurements, never the Pass-2 generation request.
    pass2_call = next(
        c for c in call_log
        if not _is_json_format(c["format"]) and c["messages"][0]["content"] != "You are a helpful assistant."
    )
    sent_text = json.dumps(pass2_call["messages"])
    assert NEUTRAL_PROMPT in sent_text  # current human message never silently dropped
    # The full 11000-byte legacy block must not have reached the model
    # verbatim/unbounded -- it was either dropped entirely or trimmed.
    assert huge_legacy not in sent_text


def test_owc9p2_hard_overflow_fails_closed_no_model_call():
    # Spec section 12/13: if the true HARD core alone cannot fit under
    # budget with reserve, composition must fail closed -- BUDGET_
    # EXCEEDED, zero model calls, no fabricated act.
    #
    # Waking-pass1-budget repair: core_system_text no longer reaches
    # Pass-1's own fixed scaffold -- the current human message is the
    # real HARD contribution to force oversized here instead.
    import conversation_direction as cd
    huge_message = "q" * 5000  # alone exceeds PASS1_MAX_PROMPT_BUDGET
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), huge_message, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == context_budget.BUDGET_EXCEEDED
    assert len(call_log) == 0  # no model call of any kind was ever made
    assert len(persistence) == 0


def test_owc9p2_no_retrieval_behavior_unchanged():
    # Spec section 18: no legacy retrieval, no hippocampal retrieval --
    # ordinary waking behavior must be unaffected, no dangling headers,
    # no malformed system message.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        legacy_retrieval_text="",
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=(), items=()),
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == "A short reply."
    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    sent_text = json.dumps(pass2_call["messages"])
    assert "LEGACY_MEMORY_CONTEXT_V1" not in sent_text
    assert hippocampus_retrieval.CONTEXT_HEADING not in sent_text


def test_owc9p2_hippocampal_disclaimer_present_when_survives_absent_when_trimmed():
    # Spec section 19.
    # (a) A small hippocampal item survives composition -> the required
    #     disclaimer/heading is present in the actual model input.
    one_item = (_fake_hippocampal_item(0, "a short, genuine memory item"),)
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "ok"},
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("x",), items=one_item),
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    sent_text = json.dumps(pass2_call["messages"])
    assert hippocampus_retrieval.CONTEXT_HEADING in sent_text
    assert "a short, genuine memory item" in sent_text

    # (b) Overwhelming aggregate pressure (huge legacy block) trims
    #     hippocampal content away entirely -> no orphaned heading, no
    #     false claim that hippocampal content was delivered.
    huge_legacy = "w" * 11000
    many_items = tuple(_fake_hippocampal_item(i, "hippocampal filler content " * 20) for i in range(6))
    la2, call_log2, p1b, p2b, persistence2 = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "ok"},
        legacy_retrieval_text=huge_legacy,
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("x",), items=many_items),
    )
    utd.start_model_call_tracking()
    la2.run_waking_turn(la2.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results2 = utd.get_and_clear_context_budget_results()
    pass2_diag2 = next(r for r in budget_results2 if r["pass"] == "pass2")
    if context_budget.RETRIEVED_HISTORY not in pass2_diag2["kinds_included"]:
        pass2_call2 = next(c for c in call_log2 if not _is_json_format(c["format"]))
        sent_text2 = json.dumps(pass2_call2["messages"])
        assert hippocampus_retrieval.CONTEXT_HEADING not in sent_text2


# ============================== OWC9-P3A: Pass-2 final-preflight closure


def _real_identity_preamble():
    from linguistic_pipeline import build_generation_controls
    kardia = {
        "moral_valve": "Prioritize honesty and the long-term wellbeing of the person I'm speaking with.",
        "volitional_channel": "Curious, deliberate, willing to take initiative when it serves the conversation.",
        "affective_stance": "Warm, steady, genuinely engaged.",
        "aesthetic_valve": "warm, precise",
    }
    return build_generation_controls(kardia)["identity_preamble"]


def test_owc9p3a_task_mode_bypasses_pass2_compositor_entirely():
    # section 8.A -- CORRECTED during this gate's own regression run:
    # TASK mode's entire generation happens inside `else:` of `if
    # resolved_interaction_mode == CONVERSATION_MODE:` in llama_anaxi.
    # run_waking_turn() -- a completely separate single-call
    # `pass2_task_mode`/`pass2_regen` call_llama() path with no Pass-1,
    # no typed act, and no context_budget.compose_within_budget()
    # involvement of any kind. "Does an ordinary TASK-mode small
    # message fit" is therefore trivially true (it is never gated by
    # THIS aggregate authority at all) -- proven here structurally by
    # confirming zero "pass2" budget diagnostic is ever recorded for a
    # TASK-mode turn, and that the turn still succeeds normally.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"resource_class": "unused"},  # would only matter if Pass-1 ran, which it does not in TASK mode
        pass2_value="A short reply.",
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="task")
    budget_results = utd.get_and_clear_context_budget_results()
    assert not any(r["pass"] == "pass1" or r["pass"] == "pass2" for r in budget_results)
    assert result["reply"] == "A short reply."
    assert sum(1 for c in call_log if _is_json_format(c["format"])) == 0  # no Pass-1-shaped call at all


def test_owc9p3a_pass2_conversation_ordinary_small_message_fits():
    # section 8.B
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    assert context_budget.MECHANICAL_STATE in pass2_diag["kinds_included"]
    assert result["reply"] == "A short reply."


def test_owc9p3a_pass2_1000_byte_human_message():
    # section 8.C: CORRECTED scope -- only CONVERSATION mode ever
    # reaches Pass-2's compositor (see test_owc9p3a_task_mode_bypasses_
    # pass2_compositor_entirely above); TASK mode has no "which mode
    # cannot fit" question to answer here since it is never checked
    # against this budget at all.
    #
    # OWC9-P3B measured this realistic ~1000-byte human message as a
    # ~80-byte overage (3280 vs 3200) under PURE byte estimation, and
    # reported that honestly rather than forcing a fit -- Pass-2's task
    # text was frozen, uncalibrated, that gate. OWC9-P3C section 2
    # closed exactly this gap via the SAME bounded fixed-scaffold
    # calibration architecture already proven for Pass-1: calibrating
    # ONLY the immutable task+closing-cue message (169 calibrated vs
    # 761 byte-estimated -- a ~592-unit saving) now measures a genuine
    # final_prompt_cost of 2687 against the unchanged 3200 budget, 513
    # units of real headroom -- no reserve/margin/num_ctx change, no
    # semantic shortening.
    #
    # META-RESPONSE LEAK repair recalibration (production incident, 2026-09-22): d8a0f9d
    # reworded PASS2_TASK_INSTRUCTION/PASS2_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE (fixing a real
    # description-vs-speech ambiguity a live turn fell into), which briefly invalidated the
    # scaffold's calibration fingerprint and forced a conservative byte-estimator fallback. A
    # fresh, bounded, real-model calibration assay against the new exact scaffold text (see
    # llama_anaxi._PASS2_SCAFFOLD_CALIBRATION's own comment) restored the calibrated fast path
    # at 153 tokens -- actually LOWER than the old 167 -- so this fixture needs no adjustment.
    long_prompt = ("I wanted to tell you about something that happened today and get your honest reaction to it, "
                    "since I've been turning it over in my head for a while now and could use another perspective. ") * 5
    assert 900 <= len(long_prompt.encode("utf-8")) <= 1100
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), long_prompt, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    assert pass2_diag["final_prompt_cost"] <= context_budget.PASS2_MAX_PROMPT_BUDGET


def test_owc9p3a_pass2_dialogue_trims_before_hard_reserve_touched():
    # section 8.D: constructs Pass-2's actual contribution shape
    # directly (hard core+human+task, soft dialogue) with a dialogue
    # window sized to force trimming, proving the SOFT dialogue gives
    # way while the HARD set (including the now-counted task text)
    # survives untouched and the reserve stays protected.
    core_text = _real_identity_preamble()
    human_text = "So what's on your mind?"
    task_text = (
        "Express the act and control state you already committed to, naturally.\n\n"
        "Your selected act: develop_current\n"
        "\ndirection_owner: unknown\nNo one currently holds it.\n"
        "\ndirection_request: none\nNo request was made.\n"
        "\nExpress this now, naturally, as your complete reply to the person. "
        "After the reply is complete, append the exact standalone marker "
        "<ANAXI_RESPONSE_COMPLETE>. The marker is mechanical and not part of the conversation."
    )
    dialogue_units = [{"role": "user" if i % 2 == 0 else "assistant", "content": "A prior turn of conversation. " * 20}
                       for i in range(12)]
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, core_text, hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, human_text, hard=True)
    hard_task = context_budget.Contribution(context_budget.MECHANICAL_STATE, task_text, hard=True)
    soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "".join(m["content"] for m in dialogue_units),
        hard=False, droppable_units=list(dialogue_units),
        render_fn=lambda units: "".join(m["content"] for m in units),
    )
    result = context_budget.compose_within_budget(
        [hard_core, hard_human, hard_task, soft_dialogue], context_budget.PASS2_MAX_PROMPT_BUDGET,
    )
    assert result.fits is True
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL) is not None
    assert result.included_kind(context_budget.CURRENT_HUMAN_MESSAGE) is not None
    assert result.included_kind(context_budget.MECHANICAL_STATE) is not None
    assert context_budget.RECENT_DIALOGUE in (result.trimmed_kinds + result.dropped_kinds)  # soft gave way
    assert result.final_prompt_cost + context_budget.PASS2_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING


def test_owc9p3a_oversized_thread_is_rejected_before_pass2():
    # Post-completion budget maintenance: the generation reserve always
    # assumed a 200-character topic label, but validation previously
    # accepted an arbitrary string. Enforce that mechanical bound before
    # the label can enter working state or Pass-2's HARD directional
    # context. This is a structural rejection, never truncation or
    # reinterpretation of the model's selected topic.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "t" * 5000, "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
        core_system_text=_real_identity_preamble(),
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == la.ConversationDirectionFailureCode.INVALID_THREAD
    # Exactly one Pass-1 call, zero Pass-2 calls, zero persistence.
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0
    assert persistence == []


def test_owc9p3a_pass2_exact_task_text_in_preflight_nothing_appended_after():
    # section 8.F/G: proves the exact final Pass-2 task+directional-state
    # text is what was measured in preflight, and that nothing textual
    # is appended afterward. OWC9-P3C section 2 split the old single
    # combined task message into two -- the immutable task+closing-cue
    # text (calibrated) and the dynamic directional-state text (still
    # byte-estimated, under its own WORKING_SET kind).
    #
    # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
    # 2026-09-05): both now ship appended onto the single SYSTEM message
    # (messages[0]) rather than as their own trailing "user"-role
    # messages -- a host/control text sharing "user" role with Alex's
    # own speech is exactly what let Clark misattribute it to him. The
    # PER-KIND COST accounting this test checks is unaffected by where
    # the text is serialized; only the assertion of WHERE to find the
    # sent text in the message list changes.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "a specific thread label", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    task_cost = pass2_diag["per_kind_cost"][context_budget.MECHANICAL_STATE]["cost"]
    directional_state_cost = pass2_diag["per_kind_cost"][context_budget.WORKING_SET]["cost"]

    pass2_call = next(c for c in call_log if not _is_json_format(c["format"]))
    sent_system_content = pass2_call["messages"][0]["content"]
    assert pass2_call["messages"][-1]["role"] == "user"  # Alex's own message is the ONLY trailing message
    assert "a specific thread label" in sent_system_content
    sent_task_text = la.PASS2_TASK_INSTRUCTION   # the scaffold is the task instruction alone (shared expression seam)
    assert sent_task_text in sent_system_content
    # The directional-state text is everything after the immutable task
    # text (plus its own "\n\n" separator) -- nothing else is appended
    # after it (section 8.G's own invariant, preserved under the new
    # merged-message shape).
    directional_state_text = sent_system_content.split(sent_task_text, 1)[-1][len("\n\n"):]
    assert context_budget.estimate_tokens(directional_state_text) == directional_state_cost  # exact same text, exact same cost
    assert task_cost == la._PASS2_SCAFFOLD_CALIBRATED_COST  # calibrated, fingerprint matches by default


# ============================================ OWC9-P3B: Pass-1 scaffold calibration


def test_owc9p3b_valid_fingerprint_uses_calibrated_cost():
    # Waking-pass1-budget repair: Pass-1's own scaffold is now fixed
    # (CONVERSATION_MODE_CLAUSE + aesthetic directive + PASS1_TASK_
    # INSTRUCTION + closing cue) and no longer depends on core_system_
    # text/Kardia at all -- default model_digest/server_version/
    # chat_template_sha256 all match this gate's own re-calibration ->
    # pass1's CORE_SYSTEM_CONTROL cost must be EXACTLY the calibrated
    # value, never a byte estimate (which would be in the thousands for
    # this real scaffold text).
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] == la._PASS1_SCAFFOLD_CALIBRATED_COST


def test_owc9p3b_core_system_text_no_longer_affects_pass1_scaffold():
    # Waking-pass1-budget repair's own new invariant: core_system_text
    # (Kardia/identity) is Pass-2-only now. Overriding it to something
    # completely different from the calibration reference must have
    # ZERO effect on Pass-1's own scaffold cost -- it stays exactly the
    # calibrated value, proving Pass-1 genuinely never reads it anymore
    # (this supersedes the old "altered scaffold invalidates fingerprint"
    # test, whose own premise -- that core_system_text reaches Pass-1's
    # scaffold -- is no longer true by design).
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text="A completely different identity preamble that was never calibrated against.",
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] == la._PASS1_SCAFFOLD_CALIBRATED_COST


def test_owc9p3b_altered_model_digest_invalidates_fingerprint():
    # Same calibrated scaffold TEXT structure, but a DIFFERENT model
    # digest (as if a different model were actually loaded) -- must
    # still fall back, proving the fingerprint genuinely checks model
    # identity, not just scaffold text.
    #
    # Waking-pass1-budget repair: Pass-1's own fixed scaffold (mode
    # clause + aesthetic directive + task instruction + closing cue) no
    # longer has an overridable "small" substitute -- ANY fingerprint
    # mismatch (by design) now overflows PASS1_MAX_PROMPT_BUDGET
    # entirely, since the fallback byte-estimate of that fixed text
    # alone is already close to the whole budget. That overflow is
    # itself the proof the fingerprint genuinely invalidated: the
    # diagnostic record is written (ui_turn_diagnostics.record_context_
    # budget_result) BEFORE the resulting ConversationDirectionFailure
    # is raised, so it is still inspectable here.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        model_digest="0" * 64,
    )
    utd.start_model_call_tracking()
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None and raised.stage == "pass1"
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] > la._PASS1_SCAFFOLD_CALIBRATED_COST


def test_owc9p3b_altered_schema_hash_invalidates_fingerprint_unit():
    # section 14: "altered schema/template/config invalidates it" --
    # proven directly against context_budget.calibrated_or_fallback_
    # cost() itself (the narrow, generic mechanism), since PASS1_SCHEMA
    # is a fixed, imported module constant not practical to mutate
    # through a full run_waking_turn() call.
    text = "some scaffold text"
    matching_fp = {"scaffold_sha256": "abc", "model_tag": "gemma4:e4b", "model_digest": "def", "pass1_schema_sha256": "schema1"}
    calibration = dict(matching_fp)
    mismatched_fp = dict(matching_fp, pass1_schema_sha256="schema2_different")
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 824) == 824
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 824) == context_budget.estimate_tokens(text)


def test_owc9p3b_mismatch_falls_back_to_byte_estimator_exact_value():
    # Direct proof the fallback is the ORDINARY estimate_tokens() value,
    # not some other number.
    text = "x" * 500
    fp_a = {"k": "a"}
    fp_b = {"k": "b"}
    assert context_budget.calibrated_or_fallback_cost(text, fp_a, fp_b, 824) == context_budget.estimate_tokens(text) == 500


def test_owc9p3b_pass1_exact_message_preflighted_nothing_appended_after():
    # section 14: exact Pass-1 final message content is preflighted;
    # nothing textual is appended after preflight. The calibrated
    # scaffold Contribution's rendered_text (hash-source) differs in
    # SHAPE from the actual sent message, but the SAME PASS1_TASK_
    # INSTRUCTION + closing cue text that was hashed for the fingerprint
    # is exactly what gets sent, verbatim, never mutated afterward.
    #
    # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
    # 2026-09-05): the immutable task message and mechanical-state text
    # now ship inside the single SYSTEM message rather than as their own
    # trailing "user"-role messages -- base_current_user_message is now
    # the ONLY, and LAST, message pass1 sends.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    pass1_call = next(c for c in call_log if _is_json_format(c["format"]))
    sent_messages = pass1_call["messages"]
    assert sent_messages[-1]["role"] == "user"
    assert sent_messages[-1]["content"] == NEUTRAL_PROMPT  # Alex's own message is the ONLY/LAST message
    system_content = sent_messages[0]["content"]
    immutable_task_text = la.PASS1_TASK_INSTRUCTION + "\n\n" + la.PASS1_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE
    assert immutable_task_text in system_content
    mechanical_text = system_content.split(immutable_task_text, 1)[-1][len("\n\n"):]
    assert mechanical_text.startswith("Current working state:")
    assert "PASS1_TASK_INSTRUCTION" not in mechanical_text  # dynamic text never carries scaffold prose


def test_owc9p3b_pass1_1000_byte_human_message_fits():
    # section 14: "1000-byte message result is explicitly tested" --
    # for Pass-1, the calibrated scaffold (824) leaves enormous headroom
    # (3456 - 824 = 2632) compared to the byte-estimated human message,
    # so this must fit comfortably (unlike Pass-2, which does not --
    # see test_owc9p3a_pass2_1000_byte_human_message's own honest
    # finding above).
    long_prompt = ("I wanted to tell you about something that happened today and get your honest reaction to it, "
                    "since I've been turning it over in my head for a while now and could use another perspective. ") * 5
    assert 900 <= len(long_prompt.encode("utf-8")) <= 1100
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
    )
    utd.start_model_call_tracking()
    # Pass-1's own diagnostic is recorded BEFORE Pass-2 is even attempted
    # (spec: preflight happens before inference) -- Pass-2 (uncalibrated,
    # see test_owc9p3a_pass2_1000_byte_human_message's own honest
    # finding) may separately overflow with this same 1000-byte message;
    # that is a distinct, already-documented Pass-2 result, tolerated
    # here so this test can isolate Pass-1's own outcome cleanly.
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), long_prompt, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        assert exc.stage == "pass2"  # Pass-1 itself must not be the one that overflowed
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["fits"] is True
    assert pass1_diag["final_prompt_cost"] <= context_budget.PASS1_MAX_PROMPT_BUDGET


def test_owc9p3b_pass1_soft_dialogue_trims_before_reserve_touched():
    # section 14: "soft context trims first" -- proven at the compositor
    # level directly (mirrors the analogous OWC9-P3A Pass-2 test),
    # using the calibrated scaffold's OWN real cost so the numbers are
    # representative of the real, now-viable ordinary CONVERSATION-mode
    # Pass-1 budget.
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "x" * 100, hard=True)
    hard_core.cost = 824  # the calibrated cost, exactly as llama_anaxi.py itself assigns it
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, "So what's on your mind?", hard=True)
    hard_mechanical = context_budget.Contribution(
        context_budget.MECHANICAL_STATE,
        "Current working state: active_thread=None, active_thread_origin=None, open_threads=[], direction_owner='unknown'",
        hard=True,
    )
    dialogue_units = [{"role": "user" if i % 2 == 0 else "assistant", "content": "A prior turn of conversation. " * 40}
                       for i in range(16)]
    soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "".join(m["content"] for m in dialogue_units),
        hard=False, droppable_units=list(dialogue_units),
        render_fn=lambda units: "".join(m["content"] for m in units),
    )
    result = context_budget.compose_within_budget(
        [hard_core, hard_human, hard_mechanical, soft_dialogue], context_budget.PASS1_MAX_PROMPT_BUDGET,
    )
    assert result.fits is True
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL) is not None
    assert result.included_kind(context_budget.CURRENT_HUMAN_MESSAGE) is not None
    assert result.included_kind(context_budget.MECHANICAL_STATE) is not None
    assert context_budget.RECENT_DIALOGUE in (result.trimmed_kinds + result.dropped_kinds)
    assert result.final_prompt_cost + context_budget.PASS1_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING


# ================================== OWC9-P3C section 1: complete fingerprint contract


def test_owc9p3c_server_version_mismatch_invalidates_fingerprint():
    # Waking-pass1-budget repair: see test_owc9p3b_altered_model_digest_
    # invalidates_fingerprint's own note -- Pass-1's fixed scaffold has
    # no overridable "small" substitute anymore, so ANY fingerprint
    # mismatch now overflows PASS1_MAX_PROMPT_BUDGET entirely; that
    # overflow, with the diagnostic still inspectable beforehand, is
    # itself the proof.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        server_version="0.0.0-different",
    )
    utd.start_model_call_tracking()
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None and raised.stage == "pass1"
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] > la._PASS1_SCAFFOLD_CALIBRATED_COST


def test_owc9p3c_chat_template_hash_mismatch_invalidates_fingerprint():
    # Waking-pass1-budget repair: see test_owc9p3b_altered_model_digest_
    # invalidates_fingerprint's own note.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        chat_template_sha256="f" * 64,
    )
    utd.start_model_call_tracking()
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None and raised.stage == "pass1"
    budget_results = utd.get_and_clear_context_budget_results()
    pass1_diag = next(r for r in budget_results if r["pass"] == "pass1")
    assert pass1_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] > la._PASS1_SCAFFOLD_CALIBRATED_COST


def test_owc9p3c_message_structure_mismatch_invalidates_fingerprint_unit():
    # Role/message structure -- proven at the compositor level directly
    # (mirrors the schema-hash unit test): a live fingerprint whose
    # message_structure_signature differs from the calibrated one (e.g.
    # a future refactor merging the task message into the system
    # message, or reordering roles) must fall back, even with an
    # otherwise byte-identical scaffold hash.
    text = "some scaffold text"
    calibration = {"scaffold_sha256": "abc", "message_structure_signature": ("system", "user"), "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, message_structure_signature=("system", "system"))
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 824) == 824
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 824) == context_budget.estimate_tokens(text)


def test_owc9p3c_think_setting_mismatch_invalidates_fingerprint_unit():
    text = "some scaffold text"
    calibration = {"think": False, "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, think=True)
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 824) == 824
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 824) == context_budget.estimate_tokens(text)


def test_owc9p3c_interaction_mode_mismatch_invalidates_fingerprint_unit():
    text = "some scaffold text"
    calibration = {"interaction_mode": "conversation", "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, interaction_mode="task")
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 824) == 824
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 824) == context_budget.estimate_tokens(text)


def test_owc9p3c_all_fingerprint_fields_present_and_load_bearing():
    # source audit: every field the gate requires load-bearing is
    # actually present in BOTH the frozen calibration record and the
    # live fingerprint builder, and none of them are audit-only anymore.
    required_fields = {
        "scaffold_sha256", "message_structure_signature", "model_tag", "model_digest",
        "ollama_server_version", "chat_template_sha256", "pass1_schema_sha256",
        "format_mode", "think", "interaction_mode",
    }
    assert required_fields.issubset(la_module_for_audit()._PASS1_SCAFFOLD_CALIBRATION.keys())
    live_fp = la_module_for_audit()._pass1_scaffold_live_fingerprint("x")
    assert required_fields.issubset(live_fp.keys())


def la_module_for_audit():
    import llama_anaxi
    return llama_anaxi


# ================================== OWC9-P3C section 2: Pass-2 scaffold calibration


def test_owc9p3c_pass2_valid_fingerprint_uses_calibrated_cost():
    # Default fingerprint fields all match this gate's own calibration
    # -> pass2's MECHANICAL_STATE (the immutable task+closing-cue
    # message) cost must be EXACTLY the calibrated value, never a byte
    # estimate -- and unlike Pass-1, this holds regardless of
    # core_system_text, since the Pass-2 scaffold hash covers ONLY the
    # task+closing-cue text, never system content.
    #
    # META-RESPONSE LEAK repair recalibration (production incident, 2026-09-22): d8a0f9d
    # reworded conversation_direction.PASS2_TASK_INSTRUCTION and this module's PASS2_
    # IMMUTABLE_TASK_MESSAGE_CLOSING_CUE (the old opening line was ambiguous between "speak"
    # and "describe your speech", which a live turn fell into), which invalidated the OLD
    # _PASS2_SCAFFOLD_CALIBRATION's scaffold_sha256 by design. A fresh, bounded, real-model
    # calibration assay against the new exact scaffold text (5 local A/B delta pairs plus 3
    # dialogue-shape coverage probes, zero variance, synthetic input only, zero persistence --
    # see that record's own comment) re-earned a valid calibration: 153 tokens, actually LOWER
    # than the old 167. No test-side patching is needed any more -- the default fingerprint
    # genuinely matches the live scaffold again.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["per_kind_cost"][context_budget.MECHANICAL_STATE]["cost"] == la._PASS2_SCAFFOLD_CALIBRATED_COST


def test_owc9p3c_pass2_altered_model_digest_invalidates_fingerprint():
    # A different model digest (as if a different model were actually
    # loaded) must fall back to the byte estimator for the task
    # message -- proving the fingerprint genuinely checks model
    # identity. model_digest is a SHARED environment field (Pass-1 and
    # Pass-2 both read it from the same _live_ollama_environment()
    # cache) -- mismatching it ALSO invalidates Pass-1's own fingerprint,
    # and Pass-1's fixed scaffold has no small overridable substitute
    # anymore (waking-pass1-budget repair), so Pass-1's OWN calibration
    # record is patched here to already expect the altered digest --
    # keeping Pass-1 succeeding (its fingerprint still matches) while
    # Pass-2's own, separate, UNPATCHED calibration record still
    # correctly mismatches -- isolating THIS test's real target:
    # Pass-2's task-message fingerprint check.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        model_digest="0" * 64,
    )
    la._PASS1_SCAFFOLD_CALIBRATION = dict(la._PASS1_SCAFFOLD_CALIBRATION, model_digest="0" * 64)
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["per_kind_cost"][context_budget.MECHANICAL_STATE]["cost"] == context_budget.estimate_tokens(
        la.PASS2_TASK_INSTRUCTION
    )


def test_owc9p3c_pass2_altered_server_version_invalidates_fingerprint():
    # Same shared-environment-field reasoning as the model-digest test
    # above -- Pass-1's own calibration record is patched to already
    # expect the altered server_version, isolating Pass-2's own check
    # (waking-pass1-budget repair: see the model-digest test above).
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        server_version="0.0.0-different",
    )
    la._PASS1_SCAFFOLD_CALIBRATION = dict(la._PASS1_SCAFFOLD_CALIBRATION, ollama_server_version="0.0.0-different")
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["per_kind_cost"][context_budget.MECHANICAL_STATE]["cost"] > 169


def test_owc9p3c_pass2_altered_chat_template_invalidates_fingerprint():
    # Same shared-environment-field reasoning as the model-digest test
    # above -- Pass-1's own calibration record is patched to already
    # expect the altered chat_template_sha256, isolating Pass-2's own
    # check.
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        chat_template_sha256="f" * 64,
    )
    la._PASS1_SCAFFOLD_CALIBRATION = dict(la._PASS1_SCAFFOLD_CALIBRATION, chat_template_sha256="f" * 64)
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["per_kind_cost"][context_budget.MECHANICAL_STATE]["cost"] > 169


def test_owc9p3c_pass2_altered_scaffold_text_invalidates_fingerprint_unit():
    # Content hash -- proven directly against context_budget.
    # calibrated_or_fallback_cost() (mirrors Pass-1's analogous unit
    # test), since PASS2_TASK_INSTRUCTION is a fixed, imported module
    # constant not practical to mutate through a full run_waking_turn()
    # call.
    text = "the real immutable Pass-2 task+closing-cue text"
    calibration = {"scaffold_sha256": "abc", "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, scaffold_sha256="different")
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 167) == 167
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 167) == context_budget.estimate_tokens(text)


def test_owc9p3c_pass2_message_structure_mismatch_invalidates_fingerprint_unit():
    text = "the real immutable Pass-2 task+closing-cue text"
    calibration = {"scaffold_sha256": "abc", "message_structure_signature": ("user", "user"), "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, message_structure_signature=("system", "user"))
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 167) == 167
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 167) == context_budget.estimate_tokens(text)


def test_owc9p3c_pass2_mismatch_falls_back_to_byte_estimator_exact_value():
    # Direct proof the fallback for Pass-2's own real scaffold text is
    # the ORDINARY estimate_tokens() value (631 bytes: the task instruction
    # alone since the shared-expression-seam repair retired the JSON-era
    # closing cue), not some other number.
    la = la_module_for_audit()
    text = la.PASS2_TASK_INSTRUCTION
    fp_a = {"k": "a"}
    fp_b = {"k": "b"}
    assert context_budget.calibrated_or_fallback_cost(text, fp_a, fp_b, 167) == context_budget.estimate_tokens(text) == 631


def test_owc9p3c_pass2_soft_context_trims_before_reserve_touched():
    # section 4's own "soft context must trim before reserve" -- proven
    # at the compositor level directly, using Pass-2's real calibrated
    # task cost (167) and kind structure (CORE_SYSTEM_CONTROL,
    # CURRENT_HUMAN_MESSAGE, MECHANICAL_STATE=calibrated,
    # WORKING_SET=dynamic directional state, all HARD; RECENT_DIALOGUE
    # soft/droppable).
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "x" * 1342, hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, "So what's on your mind?", hard=True)
    hard_task = context_budget.Contribution(context_budget.MECHANICAL_STATE, "x" * 780, hard=True)
    hard_task.cost = 167  # the calibrated cost, exactly as llama_anaxi.py itself assigns it
    hard_directional = context_budget.Contribution(
        context_budget.WORKING_SET,
        "Your selected act: develop_current\n\ndirection_owner: unknown\n...\n\ndirection_request: none\n...\n",
        hard=True,
    )
    dialogue_units = [{"role": "user" if i % 2 == 0 else "assistant", "content": "A prior turn of conversation. " * 40}
                       for i in range(16)]
    soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "".join(m["content"] for m in dialogue_units),
        hard=False, droppable_units=list(dialogue_units),
        render_fn=lambda units: "".join(m["content"] for m in units),
    )
    result = context_budget.compose_within_budget(
        [hard_core, hard_human, hard_task, hard_directional, soft_dialogue], context_budget.PASS2_MAX_PROMPT_BUDGET,
    )
    assert result.fits is True
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL) is not None
    assert result.included_kind(context_budget.CURRENT_HUMAN_MESSAGE) is not None
    assert result.included_kind(context_budget.MECHANICAL_STATE) is not None
    assert result.included_kind(context_budget.WORKING_SET) is not None
    assert context_budget.RECENT_DIALOGUE in (result.trimmed_kinds + result.dropped_kinds)
    assert result.final_prompt_cost + context_budget.PASS2_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING


def test_owc9p3c_pass2_100_byte_human_message_fits():
    _assert_pass2_human_message_fits(100)


def test_owc9p3c_pass2_500_byte_human_message_fits():
    _assert_pass2_human_message_fits(500)


def test_owc9p3c_pass2_1000_byte_human_message_fits_with_dialogue_pressure():
    # section 4's own "report headroom ... with normal dialogue
    # pressure" -- unlike the zero-dialogue 1000-byte test above, this
    # adds an OWC8-max dialogue window to confirm the realistic message
    # still fits even under real, non-trivial soft-context pressure
    # (soft dialogue trims first if needed; the calibrated Pass-2 task
    # cost leaves enough headroom that it still fits here without any
    # trimming at all).
    #
    # META-RESPONSE LEAK repair recalibration (production incident, 2026-09-22): see
    # test_owc9p3a_pass2_1000_byte_human_message's own identical comment -- the reworded
    # PASS2_TASK_INSTRUCTION/PASS2_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE was recalibrated (153
    # tokens, lower than the old 167), so this fixture needs no adjustment.
    long_prompt = ("I wanted to tell you about something that happened today and get your honest reaction to it, "
                    "since I've been turning it over in my head for a while now and could use another perspective. ") * 5
    assert 900 <= len(long_prompt.encode("utf-8")) <= 1100
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), long_prompt, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    assert pass2_diag["final_prompt_cost"] <= context_budget.PASS2_MAX_PROMPT_BUDGET


def _assert_pass2_human_message_fits(byte_size):
    msg = "a" * byte_size
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A short reply."},
        core_system_text=_real_identity_preamble(),
    )
    utd.start_model_call_tracking()
    la.run_waking_turn(la.AnaxiOrchestrator(), msg, interaction_mode="conversation")
    budget_results = utd.get_and_clear_context_budget_results()
    pass2_diag = next(r for r in budget_results if r["pass"] == "pass2")
    assert pass2_diag["fits"] is True
    assert pass2_diag["final_prompt_cost"] <= context_budget.PASS2_MAX_PROMPT_BUDGET


def test_owc9p3c_ordinary_path_conservative_zero_soft_context_maximum_message_size():
    # Report the ordinary two-pass path's conservative maximum human-message
    # size with zero soft context. This covers both required passes and uses
    # whichever hard floor is actually binding (currently Pass 2).
    #
    # The boundary itself is located, not hard-coded: the temporal-grounding
    # host facts embed the current weekday/date/time ("Friday" vs "Wednesday",
    # "9:05" vs "10:05"), so the exact byte where the hard floor meets the
    # budget moves with the wall clock (it was 1452 on a Friday evening and
    # failed the next morning). What must hold at ANY time is the compositor's
    # exactness: the largest message that fits lands EXACTLY on the Pass-2
    # ceiling, and one more byte overflows by exactly one, failing at Pass 2.
    # Historical anchors (WAKING-OUTPUT-TRUNCATION-V0): 1452 bytes on the
    # original measurement, both calibrated costs, typical directional state.
    # EXPRESSION-BOUNDARY repair (2026-09-21): PASS2_EXPRESSION_ENVELOPE_CUE is
    # appended to the same dynamic, HARD, always-byte-estimated directional-state
    # contribution the temporal-grounding facts above already live in (never the
    # calibrated task text) -- its real per-turn byte cost mechanically lowers this
    # same ceiling by that many bytes. Re-measured at ~1323 after the repair.
    def attempt(n):
        la, call_log, p1, p2, persistence = fresh_llama_anaxi(
            pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
            pass2_value={"expression": "A short reply."},
            core_system_text=_real_identity_preamble(),
        )
        utd.start_model_call_tracking()
        raised = None
        try:
            la.run_waking_turn(la.AnaxiOrchestrator(), "a" * n, interaction_mode="conversation")
        except la.ConversationDirectionFailure as exc:
            raised = exc
        results = {r["pass"]: r for r in utd.get_and_clear_context_budget_results()}
        return raised, results

    low, high = 1200, 1700          # brackets the boundary on any weekday/time
    assert attempt(low)[0] is None and attempt(high)[0] is not None
    while high - low > 1:
        middle = (low + high) // 2
        if attempt(middle)[0] is None:
            low = middle
        else:
            high = middle
    budgets = {"pass1": context_budget.PASS1_MAX_PROMPT_BUDGET, "pass2": context_budget.PASS2_MAX_PROMPT_BUDGET}
    raised, results = attempt(low)
    assert raised is None
    assert results["pass1"]["fits"] is True and results["pass2"]["fits"] is True
    raised_over, results_over = attempt(low + 1)
    assert raised_over is not None and raised_over.failure_code == context_budget.BUDGET_EXCEEDED
    binding = raised_over.stage                     # whichever hard floor is binding at this moment
    assert binding in budgets
    assert results[binding]["final_prompt_cost"] == budgets[binding]           # lands exactly on the ceiling
    assert results_over[binding]["fits"] is False
    assert results_over[binding]["final_prompt_cost"] == budgets[binding] + 1  # one more byte, one over
    # EXPRESSION-BOUNDARY RELIABILITY repair (2026-09-22): PASS2_EXPRESSION_ENVELOPE_CUE (the
    # old plain-marker instruction folded into the same dynamic, HARD, byte-estimated
    # directional-state contribution these bytes come from) no longer exists -- ordinary
    # Pass-2 requests PASS2_EXPRESSION_SCHEMA instead, so this per-turn contribution is
    # smaller and the ceiling moved UP by roughly that cue's own byte cost. Re-measured at
    # ~1403 after the repair.
    #
    # META-RESPONSE LEAK repair recalibration (production incident, 2026-09-22): d8a0f9d's
    # rewording of PASS2_TASK_INSTRUCTION/PASS2_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE briefly
    # invalidated the scaffold calibration fingerprint (any byte change does, by design) and
    # forced MECHANICAL_STATE onto the conservative byte estimator, moving this ceiling down
    # by ~500 bytes. A fresh, bounded, real-model calibration assay against the new exact
    # scaffold text (see llama_anaxi._PASS2_SCAFFOLD_CALIBRATION's own comment) restored the
    # calibrated fast path at 153 tokens -- lower than the old 167 -- so this boundary is back
    # in its original documented range (no fixture change needed).
    # LAWFUL NULL (2026-09-23): the Pass-1 menu's fixed block grew by the measured cost of the typed
    # no_reply affordance (213 -> 302 calibrated tokens; wording chosen by the real-model menu
    # battery), so this conservative zero-soft-context ceiling moved down by about that much
    # (measured 1312 on a Wednesday evening).  Real-measurement recovery of an overcounted message
    # is unchanged.
    assert 1270 <= low <= 1360                        # the envelope did not silently shrink further


def test_owc9p3c_pass2_all_fingerprint_fields_present_and_load_bearing():
    required_fields = {
        "scaffold_sha256", "message_structure_signature", "model_tag", "model_digest",
        "ollama_server_version", "chat_template_sha256", "format_mode", "think", "interaction_mode",
    }
    assert required_fields.issubset(la_module_for_audit()._PASS2_SCAFFOLD_CALIBRATION.keys())
    live_fp = la_module_for_audit()._pass2_scaffold_live_fingerprint("x")
    assert required_fields.issubset(live_fp.keys())


# ============================================ OWC9-P4: global model-call budget closure


def test_owc9p4_task_mode_pass2_normal_message_fits():
    # section 4: TASK mode's own aggregate preflight must not block an
    # ordinary, realistically-sized turn -- only genuine overflow.
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="A short, ordinary task-mode reply.")
    la.run_waking_turn(la.AnaxiOrchestrator(), "So what's on your mind today?")
    assert len(call_log) >= 1
    budget_results = utd.get_and_clear_context_budget_results()
    task_mode_diag = next((r for r in budget_results if r["pass"] == "pass2_task_mode"), None)
    assert task_mode_diag is not None and task_mode_diag["fits"] is True


def test_owc9p4_task_mode_pass2_hard_overflow_no_model_call():
    # section 4/6: a hard-only overflow (an oversized system message)
    # must fail closed before call_llama() is ever invoked -- zero
    # model calls, BUDGET_EXCEEDED via the SAME ConversationDirection
    # Failure/contain_control_failure() surface llama_gui.py already
    # handles generically for conversation-mode overflow.
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(
        pass2_value="unreachable",
        core_system_text="S" * 20000,
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT)
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None
    assert raised.stage == "pass2_task_mode"
    assert raised.failure_code == context_budget.BUDGET_EXCEEDED
    assert len(call_log) == 0  # zero model calls -- pass1 doesn't run in TASK mode either


def test_owc9p4_artifact_judgment_normal_positive_signal_fits():
    # section 1: a short, realistic POSITIVE-signal message still
    # reaches ask_llama_for_json() for artifact judgment (the aggregate
    # preflight this gate added must not silently swallow the ordinary
    # case). OWC9-P4A split the prompt into three messages (system,
    # dynamic content, fixed closing cue) -- see build_artifact_
    # construction_messages()'s own docstring.
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="A short reply.")
    la.run_waking_turn(la.AnaxiOrchestrator(), "Could you please save this for me")
    budget_results = utd.get_and_clear_context_budget_results()
    judgment_diag = next((r for r in budget_results if r["pass"] == "artifact_judgment"), None)
    assert judgment_diag is not None and judgment_diag["fits"] is True
    judgment_calls = [c for c in call_log if _is_json_format(c["format"]) and len(c["messages"]) == 3
                       and c["messages"][0]["content"].startswith("The person you're talking with has already")]
    assert len(judgment_calls) == 1


def test_owc9p4a_artifact_judgment_realistic_message_now_fits_calibrated():
    # OWC9-P4A closed the gap OWC9-P4 disclosed honestly (~76 bytes of
    # real headroom, since ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT alone is
    # 2718 bytes under the plain byte estimator): the immutable
    # system+closing-cue scaffold is now CALIBRATED (685, vs a
    # 2840-byte estimate -- see _compose_artifact_judgment_budget()'s
    # own docstring), leaving genuine, comfortable room for a
    # realistic human message. This same message that OWC9-P4 itself
    # measured as overflowing now fits, exactly mirroring how OWC9-P3C
    # closed Pass-2's own analogous disclosed gap.
    long_positive_message = (
        "Could you please save this thought for me -- I've been thinking about starting "
        "a small garden next spring and I want to remember exactly why: it's about "
        "slowing down and having something to tend to that isn't a screen."
    )
    assert len(long_positive_message.encode("utf-8")) > 76
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="A short reply.")
    la.run_waking_turn(la.AnaxiOrchestrator(), long_positive_message)
    budget_results = utd.get_and_clear_context_budget_results()
    judgment_diag = next((r for r in budget_results if r["pass"] == "artifact_judgment"), None)
    assert judgment_diag is not None
    assert judgment_diag["fits"] is True
    assert judgment_diag["per_kind_cost"][context_budget.CORE_SYSTEM_CONTROL]["cost"] == la._ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATED_COST
    judgment_calls = [c for c in call_log if _is_json_format(c["format"]) and len(c["messages"]) == 3
                       and c["messages"][0]["content"].startswith("The person you're talking with has already")]
    assert len(judgment_calls) == 1


def _artifact_judgment_budget_direct(prompt, **fresh_kwargs):
    """Shared helper for the fingerprint tests below -- calls
    build_artifact_construction_messages()/_compose_artifact_judgment_
    budget() DIRECTLY rather than through a full run_waking_turn().
    Deliberately bypasses the turn entirely: this test harness's fake
    ask_llama_for_json() returns the SAME configured pass1_value canned
    response regardless of which call site invokes it (it has no way
    to distinguish the artifact_judgment call from the real Pass-1
    call by message shape alone), so a POSITIVE-signal prompt run
    through a full turn would feed Pass-1's own canned act dict back
    through prepare_artifact_decision() as if it were an artifact
    judgment -- entangling this test with unrelated Pass-1 behavior
    for no reason, since only artifact_judgment's OWN fingerprint
    /composition logic is under test here. fresh_llama_anaxi() is
    still used to get a fresh `la` module with the desired environment
    fingerprint fields pre-seeded (model_digest/server_version/
    chat_template_sha256) -- no turn is ever run on it."""
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(**fresh_kwargs)
    messages = la.build_artifact_construction_messages(prompt)
    return la._compose_artifact_judgment_budget(messages)


def test_owc9p4a_artifact_judgment_valid_fingerprint_uses_calibrated_cost():
    result = _artifact_judgment_budget_direct("Could you please save this for me")
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL).cost == 685


def test_owc9p4a_artifact_judgment_altered_model_digest_invalidates_fingerprint():
    result = _artifact_judgment_budget_direct("Could you please save this for me", model_digest="0" * 64)
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL).cost > 685


def test_owc9p4a_artifact_judgment_altered_server_version_invalidates_fingerprint():
    result = _artifact_judgment_budget_direct("Could you please save this for me", server_version="0.0.0-different")
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL).cost > 685


def test_owc9p4a_artifact_judgment_altered_chat_template_invalidates_fingerprint():
    result = _artifact_judgment_budget_direct("Could you please save this for me", chat_template_sha256="f" * 64)
    assert result.included_kind(context_budget.CORE_SYSTEM_CONTROL).cost > 685


def test_owc9p4a_artifact_judgment_altered_scaffold_text_invalidates_fingerprint_unit():
    # Content hash -- proven directly against context_budget.
    # calibrated_or_fallback_cost() (mirrors Pass-1/Pass-2's own
    # analogous unit tests), since ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT
    # is a fixed, imported module constant not practical to mutate
    # through a full run_waking_turn() call.
    text = "the real immutable artifact_judgment scaffold text"
    calibration = {"scaffold_sha256": "abc", "other": "x"}
    matching_fp = dict(calibration)
    mismatched_fp = dict(calibration, scaffold_sha256="different")
    assert context_budget.calibrated_or_fallback_cost(text, matching_fp, calibration, 685) == 685
    assert context_budget.calibrated_or_fallback_cost(text, mismatched_fp, calibration, 685) == context_budget.estimate_tokens(text)


def test_owc9p4a_artifact_judgment_mismatch_falls_back_to_byte_estimator_exact_value():
    la = la_module_for_audit()
    text = la.ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT.strip() + "\x00" + la.ARTIFACT_JUDGMENT_CLOSING_CUE
    fp_a = {"k": "a"}
    fp_b = {"k": "b"}
    assert context_budget.calibrated_or_fallback_cost(text, fp_a, fp_b, 685) == context_budget.estimate_tokens(text) == 2841


def test_owc9p4a_artifact_judgment_exact_messages_preflighted_nothing_appended_after():
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="A short reply.")
    la.run_waking_turn(la.AnaxiOrchestrator(), "Could you please save this for me -- a specific detail")
    judgment_call = next(c for c in call_log if _is_json_format(c["format"]) and len(c["messages"]) == 3
                          and c["messages"][0]["content"].startswith("The person you're talking with has already"))
    assert judgment_call["messages"][0]["content"] == la.ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT.strip()
    assert judgment_call["messages"][2]["content"] == la.ARTIFACT_JUDGMENT_CLOSING_CUE
    assert "a specific detail" in judgment_call["messages"][1]["content"]


def _artifact_judgment_headroom(byte_size):
    utd.start_model_call_tracking()
    la, call_log, p1, p2, persistence = fresh_llama_anaxi(pass2_value="A short reply.")
    la.run_waking_turn(la.AnaxiOrchestrator(), "Could you please save this for me -- " + "a" * byte_size)
    budget_results = utd.get_and_clear_context_budget_results()
    diag = next(r for r in budget_results if r["pass"] == "artifact_judgment")
    assert diag["fits"] is True
    return context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET - diag["final_prompt_cost"]


def test_owc9p4a_artifact_judgment_100_byte_content_fits():
    assert _artifact_judgment_headroom(100) > 0


def test_owc9p4a_artifact_judgment_500_byte_content_fits():
    assert _artifact_judgment_headroom(500) > 0


def test_owc9p4a_artifact_judgment_1000_byte_content_fits():
    assert _artifact_judgment_headroom(1000) > 0


def test_owc9p4a_artifact_judgment_all_fingerprint_fields_present_and_load_bearing():
    required_fields = {
        "scaffold_sha256", "message_structure_signature", "model_tag", "model_digest",
        "ollama_server_version", "chat_template_sha256", "format_mode", "think", "path_identity",
    }
    assert required_fields.issubset(la_module_for_audit()._ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATION.keys())
    live_fp = la_module_for_audit()._artifact_judgment_scaffold_live_fingerprint("x")
    assert required_fields.issubset(live_fp.keys())


ALL_TESTS = [
    test_conversation_mode_success_full_call_counts,
    test_shift_topic_transition,
    test_develop_current_with_question_expression_stays_develop_current,
    test_ask_human_with_non_question_expression_stays_ask_human,
    test_explicit_yield_only_from_typed_act_not_tentative_prose,
    test_pass1_failure_no_pass2_no_persistence_working_set_unchanged,
    test_pass2_failure_act_observable_no_reselection_no_persistence,
    test_task_mode_never_invokes_owc5_pathway,
    test_task_mode_explicit_selection_also_unaffected,
    test_owc2_next_turn_recovers_exact_prior_pair_not_typed_json,
    test_owc3_clause_present_exactly_once_in_pass1_and_pass2_context,
    test_owc3_clause_absent_in_task_mode,
    test_owc4_aesthetic_active_exactly_once_extreme_brevity_absent,
    test_owc4_task_mode_aesthetic_unchanged,
    test_kardia_hippocampus_source_noninterference,
    test_api1_api2_source_hashes_unchanged,
    test_malformed_act_raw_preview_attached_and_bounded,
    test_malformed_act_raw_preview_truncated_when_long,
    test_unauthorized_relinquish_no_raw_preview_no_ownership_mutation,
    test_valid_clark_relinquishment_still_transitions_unaffected,
    test_ordinary_acts_unaffected_develop_current_ask_human_yield_direction,
    test_owc5p3_successful_turn_still_exactly_one_pass1_one_pass2,
    test_owc5p3_containment_regression_invalid_pass1_never_reaches_pass2,
    test_owc5p4_ask_llama_for_json_passes_think_false,
    test_owc5p4_pass2_generation_settings_unchanged,
    test_owc5p4_empty_content_still_malformed_act_with_think_false,
    test_owc5p4_truncated_json_still_malformed_act_no_retry_containment_intact,
    test_owc5p4_completion_metadata_never_changes_validation_outcome,
    test_owc8s1_pass2_completion_metadata_captured_via_call_llama,
    test_wsp2ma2_structured_schema_reaches_ollama_format_unchanged,
    test_wsp2ma2_existing_plain_json_callers_unaffected,
    # WSP2-P5-P1
    test_p5p1_resume_own_pause_durably_recorded_end_to_end,
    test_p5p1_no_request_never_writes_control_event,
    test_p5p1_durable_write_failure_is_best_effort_turn_still_succeeds,
    test_p5p1_task_mode_never_reads_the_field,
    # OWC9
    test_owc9_normal_small_turn_unaffected_by_compositor,
    test_owc9_budget_exceeded_before_pass1_hard_overflow_no_model_call,
    test_owc9_budget_exceeded_uses_distinct_code_not_malformed_act,
    test_owc9_delivery_markers_read_from_post_trim_composition_source_audit,
    test_owc9_pass1_never_receives_episode_or_active_workspace_text,
    test_owc9p2_hippocampal_memory_context_now_independently_trimmed_source_audit,
    # OWC9-P2
    test_owc9p2_extreme_retrieval_mechanically_trimmed_reserve_protected,
    test_owc9p2_hard_overflow_fails_closed_no_model_call,
    test_owc9p2_no_retrieval_behavior_unchanged,
    test_owc9p2_hippocampal_disclaimer_present_when_survives_absent_when_trimmed,
    # OWC9-P3A
    test_owc9p3a_task_mode_bypasses_pass2_compositor_entirely,
    test_owc9p3a_pass2_conversation_ordinary_small_message_fits,
    test_owc9p3a_pass2_1000_byte_human_message,
    test_owc9p3a_pass2_dialogue_trims_before_hard_reserve_touched,
    test_owc9p3a_oversized_thread_is_rejected_before_pass2,
    test_owc9p3a_pass2_exact_task_text_in_preflight_nothing_appended_after,
    # OWC9-P3B
    test_owc9p3b_valid_fingerprint_uses_calibrated_cost,
    test_owc9p3b_core_system_text_no_longer_affects_pass1_scaffold,
    test_owc9p3b_altered_model_digest_invalidates_fingerprint,
    test_owc9p3b_altered_schema_hash_invalidates_fingerprint_unit,
    test_owc9p3b_mismatch_falls_back_to_byte_estimator_exact_value,
    test_owc9p3b_pass1_exact_message_preflighted_nothing_appended_after,
    test_owc9p3b_pass1_1000_byte_human_message_fits,
    test_owc9p3b_pass1_soft_dialogue_trims_before_reserve_touched,
    # OWC9-P3C
    test_owc9p3c_server_version_mismatch_invalidates_fingerprint,
    test_owc9p3c_chat_template_hash_mismatch_invalidates_fingerprint,
    test_owc9p3c_message_structure_mismatch_invalidates_fingerprint_unit,
    test_owc9p3c_think_setting_mismatch_invalidates_fingerprint_unit,
    test_owc9p3c_interaction_mode_mismatch_invalidates_fingerprint_unit,
    test_owc9p3c_all_fingerprint_fields_present_and_load_bearing,
    test_owc9p3c_pass2_valid_fingerprint_uses_calibrated_cost,
    test_owc9p3c_pass2_altered_model_digest_invalidates_fingerprint,
    test_owc9p3c_pass2_altered_server_version_invalidates_fingerprint,
    test_owc9p3c_pass2_altered_chat_template_invalidates_fingerprint,
    test_owc9p3c_pass2_altered_scaffold_text_invalidates_fingerprint_unit,
    test_owc9p3c_pass2_message_structure_mismatch_invalidates_fingerprint_unit,
    test_owc9p3c_pass2_mismatch_falls_back_to_byte_estimator_exact_value,
    test_owc9p3c_pass2_soft_context_trims_before_reserve_touched,
    test_owc9p3c_pass2_100_byte_human_message_fits,
    test_owc9p3c_pass2_500_byte_human_message_fits,
    test_owc9p3c_pass2_1000_byte_human_message_fits_with_dialogue_pressure,
    test_owc9p3c_ordinary_path_conservative_zero_soft_context_maximum_message_size,
    test_owc9p3c_pass2_all_fingerprint_fields_present_and_load_bearing,
    test_owc9p4_task_mode_pass2_normal_message_fits,
    test_owc9p4_task_mode_pass2_hard_overflow_no_model_call,
    test_owc9p4_artifact_judgment_normal_positive_signal_fits,
    test_owc9p4a_artifact_judgment_realistic_message_now_fits_calibrated,
    test_owc9p4a_artifact_judgment_valid_fingerprint_uses_calibrated_cost,
    test_owc9p4a_artifact_judgment_altered_model_digest_invalidates_fingerprint,
    test_owc9p4a_artifact_judgment_altered_server_version_invalidates_fingerprint,
    test_owc9p4a_artifact_judgment_altered_chat_template_invalidates_fingerprint,
    test_owc9p4a_artifact_judgment_altered_scaffold_text_invalidates_fingerprint_unit,
    test_owc9p4a_artifact_judgment_mismatch_falls_back_to_byte_estimator_exact_value,
    test_owc9p4a_artifact_judgment_exact_messages_preflighted_nothing_appended_after,
    test_owc9p4a_artifact_judgment_100_byte_content_fits,
    test_owc9p4a_artifact_judgment_500_byte_content_fits,
    test_owc9p4a_artifact_judgment_1000_byte_content_fits,
    test_owc9p4a_artifact_judgment_all_fingerprint_fields_present_and_load_bearing,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
