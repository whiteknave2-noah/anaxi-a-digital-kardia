"""OWC6-G1 acceptance tests. Zero real Ollama/model calls, zero real
Gradio/webview servers started (gr.ChatInterface(...)/webview import
are safe to construct/import without side effects -- only .launch()/
.start(), both guarded behind `if __name__ == "__main__":`, would
start anything, and neither is ever called here). ollama/orchestration/
relational_history/sleep_receipts/native_provenance_writer are faked
at the sys.modules boundary, exactly matching test_owc5_s2_integration.py's
/ test_conversation_direction_trace.py's established convention.

Mode resolution happens at MODULE IMPORT TIME (llama_gui.py reads
sys.argv[1:] at its top level), so each test that needs a different
launch mode sets sys.argv, drops the relevant modules from
sys.modules, and reimports fresh.
"""
import json
import os
import sys
import tempfile
import traceback
import types

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)


def _is_json_format(fmt):
    """OWC9-P3A: a call is "Pass-1-shaped" if format is either the
    loose "json" string or a JSON Schema dict shaped like PASS1_SCHEMA
    (structured_schema=PASS1_SCHEMA, ordinary waking Pass-1).

    Ordinary waking Pass 2 is unstructured (format=None); the dict check
    remains narrow for unrelated structured-output callers."""
    if fmt == "json":
        return True
    if isinstance(fmt, dict):
        return "act" in fmt.get("properties", {})
    return False


# OWC9-P3B: the EXACT synthetic Kardia this gate's own calibration
# assay used (kept as a plain literal here, matching llama_anaxi.
# PASS1_SCAFFOLD_CALIBRATION_REFERENCE_KARDIA by convention rather than
# import -- see test_owc5_s2_integration.py's own identical note on
# why importing llama_anaxi this early is unsafe).
_CALIBRATION_REFERENCE_KARDIA = {
    "moral_valve": "Prioritize honesty and the long-term wellbeing of the person I'm speaking with.",
    "volitional_channel": "Curious, deliberate, willing to take initiative when it serves the conversation.",
    "affective_stance": "Warm, steady, genuinely engaged.",
    "aesthetic_valve": "warm, precise",
}


def _calibration_identity_preamble_and_style():
    from linguistic_pipeline import build_generation_controls
    controls = build_generation_controls(_CALIBRATION_REFERENCE_KARDIA)
    return controls["identity_preamble"], controls["style_instruction"]


_CALIBRATED_MODEL_DIGEST = "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb"
_CALIBRATED_SERVER_VERSION = "0.34.0"  # Windows->macOS migration recalibration (2026-09-11): matches Pass-1/Pass-2's own re-earned calibration.
_CALIBRATED_CHAT_TEMPLATE_SHA256 = "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315"


def build_fake_ollama(call_log, pass1_value_holder, pass2_value_holder, model_digest=_CALIBRATED_MODEL_DIGEST):
    """OWC9-P3B: also fakes ollama.list() -- see test_owc5_s2_integration.py's
    identical note on why this is required for the Pass-1 scaffold
    calibration's fingerprint check to ever match in tests."""
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        import sleep_decision_test_support as _sdts
        if _sdts.is_sleep_decision(format):
            return _sdts.sleep_decision_response()
        call_log.append({"model": model, "format": format, "messages": [dict(m) for m in messages], "think": think, "options": options})
        if _is_json_format(format):
            value = pass1_value_holder["value"]
        else:
            value = pass2_value_holder["value"]
        if format is None and isinstance(value, dict) and set(value) == {"expression"}:
            content = value["expression"]
        else:
            content = value if isinstance(value, str) else json.dumps(value)
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


def install_fake_heavy_dependencies(core_system_text=None, style_instruction=None):
    """OWC9-P3B: core_system_text/style_instruction default to this
    gate's own calibration-matching identity_preamble/style_instruction
    -- see test_owc5_s2_integration.py's identical note for why this is
    required for ordinary CONVERSATION-mode Pass-1 turns to fit at all."""
    default_core_system_text, default_style_instruction = _calibration_identity_preamble_and_style()
    resolved_core_system_text = core_system_text if core_system_text is not None else default_core_system_text
    resolved_style_instruction = style_instruction if style_instruction is not None else default_style_instruction
    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        def __init__(self, *a, **k):
            pass

        def prepare_context(self, user_id, prompt):
            return {
                "messages": [
                    {"role": "system", "content": "Your current stance:\n- Aesthetic directive: Respond with extreme brevity and precision. Prefer short sentences. Avoid filler."},
                    {"role": "user", "content": prompt},
                ],
                "controls": {"temperature": 0.4, "top_p": 0.85, "style_instruction": resolved_style_instruction},
                "kardia": {},
                "memory_context": "",
                "core_system_text": resolved_core_system_text,
            }

        def record_turn_generation_controls(self, user_id, controls, *, model_revision_id, pipeline_id, event_id, timestamp=None):
            pass

        def close(self):
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
        persistence_log.append({"prompt": prompt, "clark_prose": clark_prose, "session_id": session_id})
        participations = []
        if artifact_pass_ran:
            participations.append({"model_revision_id": "fake-rev-pass1", "tag": waking_model_tag})
        participations.append({"model_revision_id": "fake-rev-pass2", "tag": waking_model_tag})
        return {
            "event_id": f"fake-event-{_n['count']}", "session_id": session_id,
            "auth_context_id": f"fake-auth-{_n['count']}",
            "pipeline_id": f"fake-pipeline-{pipeline_key}",
            "staging_id": f"fake-staging-{_n['count']}",
            "model_participations": participations,
            "reassembled_reply": (bounded_clause + " " + clark_prose).strip(),
            "occurred_at": occurred_at,
        }

    fake_npw_module.generate_native_ulid = _fake_generate_native_ulid
    fake_npw_module.stage_and_record_native_waking_turn = _fake_stage_and_record_native_waking_turn
    sys.modules["native_provenance_writer"] = fake_npw_module

    return persistence_log


PRODUCTION_TRACE_PATH = os.path.join(ANAXI_FINAL, "conversation_direction_trace.jsonl")
PRODUCTION_STAGING_PATH = os.path.join(ANAXI_FINAL, "native_turn_staging.jsonl")


def fresh_gui(argv, pass1_value=None, pass2_value=None, desktop=False, core_system_text=None, style_instruction=None,
              model_digest=_CALIBRATED_MODEL_DIGEST, server_version=_CALIBRATED_SERVER_VERSION,
              chat_template_sha256=_CALIBRATED_CHAT_TEMPLATE_SHA256):
    """Sets sys.argv, drops the relevant modules from sys.modules, and
    imports llama_gui.py (or llama_desktop.py) fresh -- mode is
    resolved at import time from the given argv. Redirects
    PROVENANCE_DB_DIR/STAGING_PATH/CONVERSATION_DIRECTION_TRACE_PATH/
    (llama_gui's own) DIRECTION_CONTROL_TRACE_PATH to a fresh temp dir
    immediately after import, before any respond()/button-click call --
    never leaves them pointed at the production-relative defaults
    (spec section 10/16/R). llama_gui is always imported (even when
    desktop=True) so its DIRECTION_CONTROL_TRACE_PATH can be
    redirected -- llama_desktop.py reuses llama_gui's own `demo`
    object/button handlers directly, so llama_gui's module state is
    what actually matters regardless of which surface is under test."""
    call_log = []
    pass1_holder = {"value": pass1_value}
    pass2_holder = {"value": pass2_value}
    sys.modules["ollama"] = build_fake_ollama(call_log, pass1_holder, pass2_holder, model_digest=model_digest)
    persistence_log = install_fake_heavy_dependencies(core_system_text=core_system_text, style_instruction=style_instruction)

    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: None
    fake_webview.start = lambda *a, **k: None
    sys.modules.setdefault("webview", fake_webview)

    for mod in ("llama_gui", "llama_desktop", "llama_anaxi", "conversation_direction",
                "conversation_direction_trace", "direction_control",
                "interaction_mode", "session_dialogue_window",
                "workspace_roaming", "workspace_supervisor", "workspace_direction", "workspace_capability",
                "workspace_private"):
        if mod in sys.modules:
            del sys.modules[mod]

    old_argv = sys.argv
    sys.argv = list(argv)
    try:
        import llama_anaxi
        llama_anaxi._ollama_environment_cache.update({
            "checked": True, "model_digest": model_digest,
            "server_version": server_version, "chat_template_sha256": chat_template_sha256,
        })
        test_dir = tempfile.mkdtemp(prefix="owc6g1_test_")
        # Redirected BEFORE llama_gui is imported would be pointless --
        # llama_gui doesn't touch these paths at import time, only
        # inside respond()/run_waking_turn(), which read llama_anaxi's
        # module globals dynamically at call time, so redirecting here
        # (import llama_anaxi already happened; llama_gui import is
        # next) is timed correctly and BEFORE any respond() call below.
        llama_anaxi.PROVENANCE_DB_DIR = test_dir
        llama_anaxi.STAGING_PATH = os.path.join(test_dir, "native_turn_staging.jsonl")
        llama_anaxi.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(test_dir, "conversation_direction_trace.jsonl")
        llama_anaxi.reset_working_set()

        import llama_gui
        direction_control_trace_path = os.path.join(test_dir, "direction_control_trace.jsonl")
        llama_gui.DIRECTION_CONTROL_TRACE_PATH = direction_control_trace_path
        llama_gui.WORKSPACE_ROAMING_TRACE_PATH = os.path.join(test_dir, "workspace_roaming_trace.jsonl")
        # RECURRENT TURN-ACCUMULATION UI FAILURE gate: same convention --
        # never left pointed at the production-relative default during a
        # test run (a real gap this fix closes: any earlier test run
        # that reached respond() wrote real-looking diagnostic records
        # into the production anaxi_final/logs/ directory).
        llama_gui.UI_TURN_DIAGNOSTICS_LOG_PATH = os.path.join(test_dir, "ui_turn_diagnostics.jsonl")

        if desktop:
            import llama_desktop as mod_under_test
        else:
            mod_under_test = llama_gui
    finally:
        sys.argv = old_argv

    return (
        mod_under_test, llama_anaxi, call_log, pass1_holder, pass2_holder, persistence_log,
        llama_anaxi.CONVERSATION_DIRECTION_TRACE_PATH, test_dir, direction_control_trace_path,
    )


def setup_canonical_human_actor(test_dir, human_actor_id="human-actor-test-canonical"):
    """Builds a real, schema-only provenance DB at test_dir (matching
    what direction_control.resolve_canonical_human_actor_id() reads)
    with exactly one actor_type='human_person' row, PLUS the canonical
    Clark actor row (stable_key='clark', matching provenance_schema.
    derive_stable_id("actor", "clark")) -- WSP2-S1's roaming handlers
    resolve both. WSP2-P3-P1: ALSO seeds the one pipelines row and the
    host-system actor row workspace_episode_provenance.py's own
    canonical-preflight check requires -- this is now a general-
    purpose "give me a working canonical DB" helper, not human/Clark-
    actor-specific only; every existing caller is unaffected by the
    extra rows (none of them assert the DB's row set is exhaustive).
    Returns the human actor id used."""
    import provenance_schema
    import workspace_episode_provenance as wep
    from llama_anaxi import PIPELINE_KEY
    db_path = os.path.join(test_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    clark_actor_id = provenance_schema.derive_stable_id("actor", "clark")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
        (clark_actor_id, 1000),
    )
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, ?)",
        (human_actor_id, human_actor_id, 1000),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', ?)",
        (host_actor_id, 1000),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')",
        (PIPELINE_KEY,),
    )
    conn.commit()
    conn.close()
    return human_actor_id


NEUTRAL_MESSAGE = "So what's on your mind?"


# ---------------------------------------------------- A/B: GUI launch mode


def test_gui_default_launch_resolves_task():
    gui, la, *_ = fresh_gui(["llama_gui.py"])
    assert gui.LAUNCH_MODE == la.TASK_MODE
    assert gui.INTERACTION_MODE_LABEL == "TASK"


def test_gui_conversation_flag_resolves_conversation():
    gui, la, *_ = fresh_gui(["llama_gui.py", "--conversation"])
    assert gui.LAUNCH_MODE == la.CONVERSATION_MODE
    assert gui.INTERACTION_MODE_LABEL == "CONVERSATION"


def test_gui_rejects_unknown_extra_argument():
    raised = None
    try:
        fresh_gui(["llama_gui.py", "--something-else"])
    except SystemExit as exc:
        raised = exc
    assert raised is not None


# -------------------------------------------------- C/D: desktop launch mode


def test_desktop_default_resolves_task():
    desktop, la, *_ = fresh_gui(["llama_desktop.py"], desktop=True)
    assert desktop.LAUNCH_MODE == la.TASK_MODE
    assert desktop.INTERACTION_MODE_LABEL == "TASK"


def test_desktop_conversation_flag_resolves_conversation():
    desktop, la, *_ = fresh_gui(["llama_desktop.py", "--conversation"], desktop=True)
    assert desktop.LAUNCH_MODE == la.CONVERSATION_MODE
    assert desktop.INTERACTION_MODE_LABEL == "CONVERSATION"
    import llama_gui
    assert desktop.LAUNCH_MODE is llama_gui.LAUNCH_MODE  # single source of truth, not a second parser


# --------------------------------------------------- E: explicit per-turn


def test_respond_passes_explicit_mode_every_turn():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    # Simulate _interaction_mode_state having been left in a DIFFERENT
    # state by some earlier, unrelated call in this process -- proves
    # respond() does not rely on session stickiness (spec section 3).
    la._interaction_mode_state["mode"] = la.TASK_MODE
    # OWC7-P2: respond() now always returns (reply, status_panel_value)
    # -- required by ChatInterface's additional_outputs contract, see
    # llama_gui.py's own respond()/CONTROL_FAILURE_* comments. The
    # second element is asserted separately, in the OWC7-P2 section
    # below, where it actually varies.
    reply, _status = gui.respond(NEUTRAL_MESSAGE, [])
    assert reply == "A reply."
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 1  # conversation pathway ran despite stale task-mode session state


# --------------------------------------------------------- F: persistence


def test_persistent_session_across_multiple_turns():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "First reply."},
    )
    gui.respond("First message.", [])
    session_after_first = la._get_native_session()[0]

    p2["value"] = {"expression": "Second reply."}
    gui.respond("Second message.", [])
    session_after_second = la._get_native_session()[0]

    assert session_after_first == session_after_second  # same process, same session (OWC2 continuity intact)
    assert len(persistence) == 2
    assert persistence[0]["session_id"] == persistence[1]["session_id"]


# --------------------------------------------------- G/H: typed-path reach


def test_conversation_gui_reaches_typed_pass1_pass2():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "shift_topic", "thread": "birds", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Let's talk about birds."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1 and pass2_calls == 1
    ws = la.get_working_set()
    assert ws["active_thread"] == "birds"  # apply_conversation_act() ran -- typed pathway genuinely executed


def test_task_gui_bypasses_typed_pass1_pass2():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py"],  # no --conversation
        pass1_value={"act": "shift_topic", "thread": "unreachable", "direction_request": "none", "relinquish_direction": False},
        pass2_value="An ordinary task-mode reply.",
    )
    reply, _status = gui.respond(NEUTRAL_MESSAGE, [])
    assert reply == "An ordinary task-mode reply."
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 0  # OWC5 Pass 1 never invoked
    ws = la.get_working_set()
    assert ws["active_thread"] is None  # never touched


# --------------------------------------------------- I/J: trace emission


def test_conversation_gui_emits_exactly_one_durable_trace():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    import conversation_direction_trace as cdt
    records = cdt.query_trace(trace_path)
    assert len(records) == 1
    assert records[0]["validated_act"] == "develop_current"
    assert records[0]["event_id"] is not None


def test_task_gui_emits_zero_trace_records():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="An ordinary task-mode reply.",
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    import conversation_direction_trace as cdt
    records = cdt.query_trace(trace_path)
    assert records == []


# ------------------------------------------------------- K: indicator match


def test_mode_indicator_matches_execution_mode_conversation():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    assert gui.INTERACTION_MODE_LABEL == "CONVERSATION"
    gui.respond(NEUTRAL_MESSAGE, [])
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 1  # execution matches the displayed label


def test_mode_indicator_matches_execution_mode_task():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="An ordinary reply.",
    )
    assert gui.INTERACTION_MODE_LABEL == "TASK"
    gui.respond(NEUTRAL_MESSAGE, [])
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 0  # execution matches the displayed label


def test_indicator_derived_from_launch_not_model_or_prose():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="Let's have a real conversation about deep things.",
    )
    # Conversational-sounding user/model text must not flip a TASK
    # launch's indicator or behavior.
    gui.respond("Let's just talk, no task at all, I promise.", [])
    assert gui.INTERACTION_MODE_LABEL == "TASK"
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 0


# ------------------------------------------------------- L: dialogue window


def test_dialogue_window_present_on_second_conversation_turn():
    # OWC2's own dialogue-window recovery logic already has its own
    # dedicated, thorough, unaffected 13-test suite
    # (test_session_dialogue_window.py) -- not re-proven here. What's
    # specific to THIS gate is whether the GUI wiring feeds
    # build_session_dialogue_window() the SAME session_id across
    # successive turns in one process, which is the one fact that
    # would silently break OWC2 continuity if the GUI's mode/session
    # plumbing were wrong. Verified via a spy wrapper around the real
    # function (not a behavior change -- restored immediately after).
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "First reply."},
    )
    # run_waking_turn() only calls build_session_dialogue_window() at
    # all when a real anaxi_provenance.db file exists at
    # PROVENANCE_DB_DIR (persistence itself is fully faked -- no such
    # file gets created otherwise) -- create a real, schema-only,
    # zero-row one so that code path is genuinely exercised.
    import provenance_schema
    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    provenance_schema.create_provenance_db(db_path).close()

    import session_dialogue_window as sdw
    seen_session_ids = []
    real_build = sdw.build_session_dialogue_window

    def spy_build(conn, session_id, staging_path, max_turns=8):
        seen_session_ids.append(session_id)
        return real_build(conn, session_id, staging_path, max_turns=max_turns)

    la.build_session_dialogue_window = spy_build
    try:
        gui.respond("First message.", [])
        p2["value"] = {"expression": "Second reply."}
        gui.respond("Second message.", [])
    finally:
        la.build_session_dialogue_window = real_build

    assert len(seen_session_ids) == 2
    assert seen_session_ids[0] == seen_session_ids[1] == la._get_native_session()[0]


# ------------------------------------------------- M/N/O/P/Q: noninterference


def test_owc6_p1_clause_unchanged():
    import interaction_mode as im
    clause = im.CONVERSATION_MODE_CLAUSE
    assert "No task is pending or implied by this mode. You do not need to search for a service request." in clause
    assert "return conversational direction to the human" not in clause
    assert "hand the next choice back to the human" not in clause


def test_task_mode_behavior_unchanged_no_owc5_wiring():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py"],
        pass1_value=None,
        pass2_value="An ordinary task-mode reply, unchanged shape.",
    )
    reply, _status = gui.respond(NEUTRAL_MESSAGE, [])
    assert reply == "An ordinary task-mode reply, unchanged shape."
    assert len(persistence) == 1


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


def test_workspace_paths_unreferenced_in_gui_files():
    # OWC6-G1 established zero workspace references here; WSP2-S1
    # deliberately, narrowly introduces exactly two (workspace_
    # capability for WorkspacePaths.production_defaults(), workspace_
    # supervisor for resolve_canonical_actor_id()) to wire the roaming
    # control surface -- workspace_direction itself is NOT imported
    # here (it's used inside workspace_roaming.py, not llama_gui.py
    # directly), and llama_desktop.py still references none of these
    # (it only imports demo/handlers from llama_gui).
    with open(os.path.join(ANAXI_FINAL, "llama_gui.py"), encoding="utf-8") as f:
        gui_source = f.read()
    assert "import workspace_supervisor" in gui_source
    assert "import workspace_capability" in gui_source
    assert "workspace_direction" not in gui_source
    with open(os.path.join(ANAXI_FINAL, "llama_desktop.py"), encoding="utf-8") as f:
        desktop_source = f.read()
    assert "workspace_supervisor" not in desktop_source
    assert "workspace_direction" not in desktop_source
    assert "workspace_capability" not in desktop_source


# --------------------------------------------------------------- R: isolation


def test_no_test_writes_to_production_paths():
    prod_trace_before = os.path.exists(PRODUCTION_TRACE_PATH)
    prod_staging_before = (
        os.path.getsize(PRODUCTION_STAGING_PATH) if os.path.exists(PRODUCTION_STAGING_PATH) else None
    )
    prod_ui_diagnostics_path = os.path.join(ANAXI_FINAL, "logs", "ui_turn_diagnostics.jsonl")
    prod_ui_diagnostics_before = os.path.exists(prod_ui_diagnostics_path)
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    assert trace_path != PRODUCTION_TRACE_PATH
    assert la.STAGING_PATH != PRODUCTION_STAGING_PATH
    assert gui.UI_TURN_DIAGNOSTICS_LOG_PATH != prod_ui_diagnostics_path
    gui.respond(NEUTRAL_MESSAGE, [])
    assert os.path.exists(PRODUCTION_TRACE_PATH) == prod_trace_before  # unchanged (created or not, same as before)
    if prod_staging_before is not None:
        assert os.path.getsize(PRODUCTION_STAGING_PATH) == prod_staging_before
    # RECURRENT TURN-ACCUMULATION UI FAILURE gate: a real gap this
    # closes -- an earlier test run, before UI_TURN_DIAGNOSTICS_LOG_PATH
    # was redirected in fresh_gui() above, wrote real-looking diagnostic
    # records straight into this production path.
    assert os.path.exists(prod_ui_diagnostics_path) == prod_ui_diagnostics_before
    # And the redirected test log DID actually receive the turn's record
    # -- proving the instrumentation itself fired, not merely that it
    # avoided production.
    assert os.path.exists(gui.UI_TURN_DIAGNOSTICS_LOG_PATH)
    with open(gui.UI_TURN_DIAGNOSTICS_LOG_PATH, encoding="utf-8") as f:
        diag_lines = [l for l in f if l.strip()]
    assert len(diag_lines) == 1


# ================================================ OWC7-S2: direction control


def test_fresh_process_status_displays_unknown():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"


def test_interface_load_alone_does_not_alter_owner():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    before = dict(la.get_working_set())
    gui.refresh_conversation_lead()  # simulates demo.load()'s handler firing
    gui.refresh_conversation_lead()  # simulates a reconnect firing it again
    after = dict(la.get_working_set())
    assert before == after
    assert after["direction_owner"] == "unknown"


def test_give_direction_to_clark_full_flow():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    human_id = setup_canonical_human_actor(test_dir)
    status = gui.give_direction_to_clark()
    assert status == "\U0001f7e2 **Conversation lead: Clark**"
    assert la.get_working_set()["direction_owner"] == "clark"
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert len(records) == 1
    assert records[0]["status"] == "ok"
    assert records[0]["control_act"] == "give_direction_to_clark"
    assert records[0]["direction_owner_before"] == "unknown"
    assert records[0]["direction_owner_after"] == "clark"
    assert records[0]["source_actor_id"] == human_id
    assert len(call_log) == 0  # zero model calls


def test_take_direction_full_flow():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    human_id = setup_canonical_human_actor(test_dir)
    status = gui.take_direction()
    assert status == "\U0001f535 **Conversation lead: You**"
    assert la.get_working_set()["direction_owner"] == "human"
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert len(records) == 1
    assert records[0]["control_act"] == "take_direction"
    assert records[0]["direction_owner_after"] == "human"
    assert records[0]["source_actor_id"] == human_id
    assert len(call_log) == 0


def test_repeated_explicit_control_idempotent_and_recorded_each_time():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    gui.take_direction()
    status2 = gui.take_direction()  # human -> human again
    assert status2 == "\U0001f535 **Conversation lead: You**"
    assert la.get_working_set()["direction_owner"] == "human"
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert len(records) == 2  # both explicit clicks recorded, even though idempotent
    assert all(r["status"] == "ok" for r in records)


def test_unresolved_human_actor_fails_closed():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    # deliberately do NOT call setup_canonical_human_actor -- no
    # anaxi_provenance.db exists at all in this fresh temp dir.
    before = dict(la.get_working_set())
    status = gui.give_direction_to_clark()
    assert "control failed" in status
    assert la.get_working_set() == before  # unchanged
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert len(records) == 1
    assert records[0]["status"] == "RESOLUTION_FAILED"
    assert records[0]["source_actor_id"] is None
    assert records[0]["direction_owner_after"] is None


def test_ambiguous_human_actor_registry_fails_closed():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    import provenance_schema
    db_path = os.path.join(test_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)", ("human-a", "human-a"))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)", ("human-b", "human-b"))
    conn.commit()
    conn.close()
    before = dict(la.get_working_set())
    status = gui.take_direction()
    assert "control failed" in status
    assert la.get_working_set() == before


def test_shared_backend_desktop_and_browser_same_owner():
    # Both surfaces reuse the exact same llama_gui module state/demo
    # object -- proven directly: llama_desktop imports demo/handlers
    # from llama_gui, never redefining them.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_desktop.py", "--conversation"], desktop=True,
    )
    setup_canonical_human_actor(test_dir)
    import llama_gui as underlying_gui
    underlying_gui.give_direction_to_clark()
    assert la.get_working_set()["direction_owner"] == "clark"
    # The desktop-imported demo IS the browser demo -- same Blocks object.
    assert gui.demo is underlying_gui.demo


def test_reconnect_render_cannot_mutate_owner():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    gui.give_direction_to_clark()
    owner_after_control = la.get_working_set()["direction_owner"]
    for _ in range(3):
        gui.refresh_conversation_lead()  # repeated simulated reconnects
    assert la.get_working_set()["direction_owner"] == owner_after_control == "clark"


def test_next_pass2_interface_sees_owner_after_give_to_clark():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    setup_canonical_human_actor(test_dir)
    gui.give_direction_to_clark()

    import conversation_direction as cd
    seen = {}
    real_build_pass2 = cd.build_pass2_interface

    def spy_build_pass2(*args, **kwargs):
        iface = real_build_pass2(*args, **kwargs)
        seen["direction_resolution"] = iface["direction_resolution"]
        return iface

    la.build_pass2_interface = spy_build_pass2
    try:
        gui.respond(NEUTRAL_MESSAGE, [])
    finally:
        la.build_pass2_interface = real_build_pass2

    assert seen["direction_resolution"]["direction_owner_after"] == "clark"


def test_next_pass2_interface_sees_owner_after_take_direction():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "ask_human", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    setup_canonical_human_actor(test_dir)
    gui.take_direction()

    import conversation_direction as cd
    seen = {}
    real_build_pass2 = cd.build_pass2_interface

    def spy_build_pass2(*args, **kwargs):
        iface = real_build_pass2(*args, **kwargs)
        seen["direction_resolution"] = iface["direction_resolution"]
        return iface

    la.build_pass2_interface = spy_build_pass2
    try:
        gui.respond(NEUTRAL_MESSAGE, [])
    finally:
        la.build_pass2_interface = real_build_pass2

    assert seen["direction_resolution"]["direction_owner_after"] == "human"


def test_trace_correct_values_and_session_id():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    human_id = setup_canonical_human_actor(test_dir)
    gui.give_direction_to_clark()
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert records[0]["session_id"] == la.get_current_session_id()
    assert records[0]["source_actor_id"] == human_id
    assert records[0]["direction_owner_before"] == "unknown"
    assert records[0]["direction_owner_after"] == "clark"


def test_control_trace_never_referenced_by_dialogue_kardia_hippocampus_sources():
    for fn in ("direction_control.py",):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        import ast
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
        # Scope validation is an authority dependency, not a dialogue,
        # Kardia, or retrieval dependency.
        assert imported_names.issubset({"datetime", "family_membership", "json", "os", "uuid"})
    # The meaningful check is MODULE reference (import), not the bare
    # substring "direction_control" -- conversation_direction.py's own
    # unrelated identifiers (cross_validate_direction_control,
    # apply_human_direction_control, etc.) legitimately contain that
    # substring without importing the direction_control module at all.
    with open(os.path.join(ANAXI_FINAL, "session_dialogue_window.py"), encoding="utf-8") as f:
        sdw_source = f.read()
    assert "import direction_control" not in sdw_source
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        cd_source = f.read()
    assert "import direction_control" not in cd_source


def direction_control_module():
    import direction_control
    return direction_control


# ============================================ WSP2-S1: workspace roaming --
# Deep coverage of the roaming worker's own internal behavior (act/wait/
# stop, attribution, failure handling, waking-substrate-only, memory
# exclusion, shutdown) lives in test_workspace_roaming.py. These tests
# prove only the GUI WIRING is correct: canonical resolution -> the
# right workspace_roaming calls -> the right state/UI results.


def roaming_module():
    import workspace_roaming
    return workspace_roaming


def private_module():
    import workspace_private
    return workspace_private


# ==================================== WSP3-P1: private-pathway GUI wiring --


def test_resume_background_activity_wires_production_private_paths():
    # Proves the WIRING only -- never starts a real worker thread/model
    # call, and never touches the real production workspace/private/
    # directory. Deep private-pathway behavior itself is already
    # covered by test_workspace_private.py and test_workspace_roaming.py.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wp = private_module()
    wr.reset_roaming_state()
    human_id = setup_canonical_human_actor(test_dir)

    captured = {}
    real_start = wr.start_roaming_worker

    def fake_start(**kwargs):
        captured.update(kwargs)
        return False  # deliberately starts nothing real

    wr.start_roaming_worker = fake_start
    try:
        gui.resume_background_activity()
    finally:
        wr.start_roaming_worker = real_start

    assert "private_paths" in captured
    assert captured["private_paths"] is not None
    assert captured["private_paths"].root == wp.PrivatePaths.production_defaults().root
    # (the literal live path is asserted with the test redirect removed in
    # test_workspace_private.py; under pytest production_defaults() is redirected)
    import runtime_roots
    assert captured["private_paths"].root == os.path.join(runtime_roots.workspace_root(), "private")


def test_gui_status_text_unaffected_by_private_wiring():
    # The GUI remains observationally unchanged with respect to private
    # material (spec section 2/8) -- background-activity lifecycle
    # labels only, nothing private-specific added.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    assert gui.refresh_roaming_status() == "**Background Space activity: Idle**"
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_HUMAN
    assert gui.refresh_roaming_status() == "**Background Space activity: Paused**"


# ============================================= WSP2-P5-P1: Clark self-resume


def test_p5p1_respond_applies_clark_self_resume_when_durably_recorded():
    # End-to-end through the real respond() path: Clark's own typed
    # resume_own_pause choice, durably recorded by run_waking_turn()
    # against the real (schema-seeded) test provenance DB, is then
    # APPLIED by respond()'s own hook -- clearing the live process-
    # local PAUSED_BY_CLARK state, exactly like release_wait_for_human_
    # and_maybe_resume()'s own established hook does for its case.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={
            "act": "develop_current", "thread": "", "direction_request": "none",
            "relinquish_direction": False, "background_activity_request": "resume_own_pause",
        },
        pass2_value={"expression": "Good to be back."},
    )
    setup_canonical_human_actor(test_dir)  # seeds the pipeline/clark actor rows the durable write needs
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK

    reply, _status = gui.respond(NEUTRAL_MESSAGE, [])

    assert reply == "Good to be back."
    assert wr.get_roaming_state()["background_activity_state"] != wr.BACKGROUND_STATE_PAUSED_BY_CLARK


def test_p5p1_respond_leaves_paused_by_human_untouched_even_with_resume_own_pause():
    # Section 8/10: a human-owned pause is never overridden by Clark's
    # own resume_own_pause, even end-to-end through respond().
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={
            "act": "develop_current", "thread": "", "direction_request": "none",
            "relinquish_direction": False, "background_activity_request": "resume_own_pause",
        },
        pass2_value={"expression": "Just chatting."},
    )
    setup_canonical_human_actor(test_dir)
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_HUMAN

    gui.respond(NEUTRAL_MESSAGE, [])

    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_HUMAN


# ============================================= WSP2-P5-P2: fail-closed reconstruction


def test_p5p2_human_resume_refuses_to_start_while_authority_still_unknown():
    # Section 8/19B: human Resume must not silently bypass an
    # unresolved lifecycle-authority uncertainty -- if canonical truth
    # is STILL unreadable on the one safe re-read, background activity
    # must not start, and the state must remain uncertain.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    wr = roaming_module()
    import workspace_episode_provenance as wep
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN

    real_find = wep.find_latest_background_lifecycle_control

    def _still_unreadable(data_dir):
        raise RuntimeError("simulated still-unreadable canonical store")

    wep.find_latest_background_lifecycle_control = _still_unreadable
    try:
        status = gui.resume_background_activity()
    finally:
        wep.find_latest_background_lifecycle_control = real_find

    assert "could not resume" in status
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    assert wr.is_worker_running() is False


def test_p5p2_human_resume_recovers_once_canonical_truth_becomes_readable():
    # Section 7/8: the one safe re-read succeeding lets human Resume
    # proceed normally -- if the ledger turns out to have no real
    # pause on record, background activity becomes available again.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN

    status = gui.resume_background_activity()

    assert "could not resume" not in status
    assert wr.get_roaming_state()["background_activity_state"] != wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN


def test_p5p2_status_text_shows_recovery_needed():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    status = gui.refresh_roaming_status()
    assert status == "**Background Space activity: Recovery needed**"
    # Never a fabricated Clark/human decision label.
    assert "Paused" not in status


# ========================================== WSP2-P5-P2a: canonical path anchor


def test_p5p2a_provenance_db_dir_anchored_to_source_file_not_cwd():
    # Section 15A/B: llama_anaxi.py's own production default is a
    # deterministic function of this source file's own location --
    # never the launching process's CWD. Proven directly against the
    # exact source text (safe: no fresh heavy import required) rather
    # than the old literal-"." default this gate replaces.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "PROVENANCE_DB_DIR = os.path.dirname(os.path.abspath(__file__))" in source
    assert 'PROVENANCE_DB_DIR = "."' not in source


def test_p5p2a_provenance_db_dir_resolves_to_this_repo_directory():
    # The formula, evaluated: confirms it actually resolves to
    # anaxi_final/ (where llama_anaxi.py itself lives), matching
    # ANAXI_FINAL -- this file's own already-established anchor
    # constant (line 22 above), computed the identical way.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    production_default = os.path.dirname(os.path.abspath(la.__file__))
    assert production_default == ANAXI_FINAL
    assert os.path.isabs(production_default)


def test_p5p2a_recomputing_after_cwd_change_still_matches():
    # Section 15B, exercised (not merely read from source): re-deriving
    # llama_anaxi.py's own formula after actually changing the
    # process's current working directory still resolves to the exact
    # same anchored location -- os.path.abspath() on an already-
    # absolute __file__ never consults os.getcwd() at all.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    before = os.path.dirname(os.path.abspath(la.__file__))
    original_cwd = os.getcwd()
    unrelated_cwd = tempfile.mkdtemp(prefix="p5p2a_recompute_cwd_")
    try:
        os.chdir(unrelated_cwd)
        after = os.path.dirname(os.path.abspath(la.__file__))
    finally:
        os.chdir(original_cwd)
    assert after == before


def test_p5p2a_explicit_test_data_dir_override_still_authoritative():
    # Section 4/15F/15G: the layering this gate must preserve --
    # caller-provided data_dir (here, the test fixture's own temp_dir
    # override, exactly like every existing test in this suite already
    # relies on) remains fully authoritative over the anchored
    # production default; a test process never silently falls back to
    # writing production data merely because the default changed.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    assert la.PROVENANCE_DB_DIR == test_dir
    assert la.PROVENANCE_DB_DIR != ANAXI_FINAL


def test_p5p2a_launch_launcher_shares_launch_production_backend_not_reimplemented():
    # Section 15D/13: llama_launch.py never defines its own copy --
    # calling it from that surface necessarily executes the identical
    # code referencing the identical PROVENANCE_DB_DIR binding llama_
    # gui.py's own direct launch does.
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    assert "launch_production_backend" in source
    assert "def launch_production_backend" not in source


def test_p5p2a_desktop_launcher_shares_launch_production_backend_not_reimplemented():
    # Section 15E/13.
    with open(os.path.join(ANAXI_FINAL, "llama_desktop.py"), encoding="utf-8") as f:
        source = f.read()
    assert "launch_production_backend" in source
    assert "def launch_production_backend" not in source


def test_roaming_fresh_status_idle():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    roaming_module().reset_roaming_state()
    assert gui.refresh_roaming_status() == "**Background Space activity: Idle**"


def test_roaming_load_alone_does_not_enable():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    gui.refresh_roaming_status()
    gui.refresh_roaming_status()
    assert wr.get_roaming_state()["authorized"] is False


def test_resume_background_activity_enables_and_starts_worker():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    human_id = setup_canonical_human_actor(test_dir)
    # The fake ollama returns pass1_holder's queued value for every
    # format="json" call -- the roaming worker's very first call asks
    # for the roaming choice; "stop" ends the run almost immediately so
    # this test doesn't need to wait out a real interval.
    p1["value"] = {"roaming_act": "stop", "wait_minutes": 5}
    status = gui.resume_background_activity()
    # A queued "stop" response resolves near-instantly in the
    # background thread -- by the time this line runs, the lifecycle
    # may have already flipped to PAUSED_BY_CLARK (a legitimate race,
    # not a defect: the worker genuinely ran and genuinely stopped).
    # Either displayed status is correct; the durable trace (checked
    # below) is the authoritative record of what actually happened.
    assert status in (
        "**Background Space activity: Active**",
        "**Background Space activity: Paused**",
    )
    import time
    deadline = time.time() + 2.0
    while time.time() < deadline and wr.is_worker_running():
        time.sleep(0.01)
    records = wr.query_roaming_trace(gui.WORKSPACE_ROAMING_TRACE_PATH)
    assert len(records) >= 1
    assert records[0]["run_status"] == wr.RUN_STATUS_ENABLED  # the resume click itself was recorded
    assert records[-1]["run_status"] == wr.RUN_STATUS_STOPPED_BY_CLARK
    assert wr.get_roaming_state()["last_roaming_act"] == "stop"


def test_resume_background_activity_resolution_failure_stays_off():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    # deliberately do NOT set up a canonical human actor
    status = gui.resume_background_activity()
    assert "could not resume" in status
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.is_worker_running() is False


def test_pause_background_activity_disables_and_stops_worker():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    wr = roaming_module()
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05
    human_id = setup_canonical_human_actor(test_dir)
    p1["value"] = {"roaming_act": "wait", "wait_minutes": 60}
    gui.resume_background_activity()
    assert wr.get_roaming_state()["authorized"] is True
    status = gui.pause_background_activity()
    assert status == "**Background Space activity: Paused**"
    import time
    deadline = time.time() + 2.0
    while time.time() < deadline and wr.is_worker_running():
        time.sleep(0.01)
    assert wr.is_worker_running() is False
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_HUMAN


def test_roaming_shared_backend_desktop_and_browser():
    # llama_desktop.py re-exports only demo/MODEL/LAUNCH_MODE/
    # INTERACTION_MODE_LABEL from llama_gui (matching OWC6-G1's own
    # established pattern) -- the roaming handlers themselves live
    # solely on llama_gui and are reachable through the shared `demo`
    # Blocks object's button callbacks either way, so "shared backend"
    # is proven the same way OWC7-S2's own test proves it: same demo
    # object, same underlying module state.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_desktop.py", "--conversation"], desktop=True,
    )
    wr = roaming_module()
    wr.reset_roaming_state()
    import llama_gui as underlying_gui
    assert gui.demo is underlying_gui.demo
    assert underlying_gui.refresh_roaming_status() == "**Background Space activity: Idle**"
    # State is process-global (proven directly in test_workspace_roaming.py's
    # test_roaming_state_is_single_process_global_not_per_surface). Here,
    # confirm llama_gui.py's own workspace_roaming import IS this same
    # freshly-imported module object, not an independent copy -- so
    # any state llama_gui's button handlers touch is the identical
    # state `wr` (imported fresh by this test) observes.
    assert sys.modules["workspace_roaming"] is wr
    state = wr.get_roaming_state()
    state["last_roaming_act"] = "stop"
    assert wr.get_roaming_state()["last_roaming_act"] == "stop"  # same object, mutation visible


def test_roaming_zero_model_calls_from_button_click_itself():
    # resume_background_activity() itself must make zero model calls --
    # only the worker THREAD it starts does, asynchronously, and that
    # thread's own timing relative to the test is inherently racy (the
    # fake ask_llama_for_json is instant, so a runtime call-count check
    # taken "immediately after" the click can't reliably distinguish
    # "the handler called it synchronously" from "the background thread
    # already ran once by the time we checked"). The robust, non-racy
    # proof is structural: none of _background_activity_kwargs()/
    # _auto_start_background_activity()/pause_background_activity()/
    # resume_background_activity() ever call ask_llama_for_json
    # directly -- it is only ever passed by reference for the worker
    # THREAD to call later.
    with open(os.path.join(ANAXI_FINAL, "llama_gui.py"), encoding="utf-8") as f:
        source = f.read()
    fn_start = source.index("def _background_activity_kwargs")
    fn_end = source.index("with gr.Blocks")
    fn_body = source[fn_start:fn_end]
    assert "ask_llama_for_json(" not in fn_body
    assert '"ask_llama_for_json": ask_llama_for_json' in fn_body  # passed by reference, not called


# ======================================== OWC7-P2: control-failure containment


def _read_diag_records(gui):
    with open(gui.UI_TURN_DIAGNOSTICS_LOG_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_malformed_act_contained_gui_survives_and_flags_diagnostics():
    # Test A (spec section 19): synthetic malformed Pass-1 output,
    # driven all the way through respond() (not just run_waking_turn()
    # directly) -- proves the Gradio-facing callback itself no longer
    # raises for this exact exception family.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="not valid json at all",
        pass2_value={"expression": "unreachable"},
    )
    before_ws = dict(la.get_working_set())
    chat_reply, status_text = gui.respond(NEUTRAL_MESSAGE, [])  # must not raise
    assert chat_reply == gui.CONTROL_FAILURE_CHAT_MARKER
    assert status_text == gui.CONTROL_FAILURE_STATUS_TEXT
    assert not chat_reply.lower().startswith("i ")  # not phrased in Clark's own first-person voice
    assert "generated by the host, not clark" in chat_reply.lower()  # explicitly disclaims Clark's authorship
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0
    assert len(persistence) == 0
    assert la.get_working_set() == before_ws  # direction state unchanged

    records = _read_diag_records(gui)
    assert len(records) == 1
    rec = records[0]
    assert rec["callback_success"] is False
    assert rec["waking_turn_success"] is False
    assert rec["control_failure_contained"] is True
    assert rec["pipeline_stage"] == "pass1"
    assert rec["exception_type"] == "ConversationDirectionFailure"
    assert rec["raw_pass1_output_preview"] == "not valid json at all"
    assert rec["raw_pass1_output_truncated"] is False

    import conversation_direction_trace as cdt
    trace_records = cdt.query_trace(trace_path)
    assert len(trace_records) == 1
    assert trace_records[0]["pass1_status"] == "MALFORMED_ACT"  # underlying cause preserved, not rewritten


def test_unauthorized_relinquish_contained_owner_unchanged_no_preview():
    # Test B (spec section 19).
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": True},
        pass2_value={"expression": "unreachable"},
    )
    import conversation_direction as cd
    before_ws = dict(la.get_working_set())
    assert before_ws["direction_owner"] == cd.DIRECTION_UNKNOWN
    chat_reply, status_text = gui.respond(NEUTRAL_MESSAGE, [])  # must not raise
    assert chat_reply == gui.CONTROL_FAILURE_CHAT_MARKER
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0
    assert la.get_working_set() == before_ws  # owner remains unknown, not silently authorized

    records = _read_diag_records(gui)
    assert records[0]["control_failure_contained"] is True
    assert records[0]["raw_pass1_output_preview"] is None  # act was structurally valid, nothing raw to capture

    import conversation_direction_trace as cdt
    trace_records = cdt.query_trace(trace_path)
    assert trace_records[0]["pass1_status"] == "UNAUTHORIZED_RELINQUISH"


def test_diagnostic_semantic_split_success_vs_contained_failure():
    # Test I (spec section 19): callback_success/waking_turn_success/
    # control_failure_contained must never let a contained failure be
    # mistaken for a completed Clark turn, and must never mislabel an
    # ordinary successful turn either.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A genuine reply."},
    )
    chat_reply, status_text = gui.respond(NEUTRAL_MESSAGE, [])
    assert chat_reply == "A genuine reply."
    assert status_text == gui.CONTROL_FAILURE_STATUS_NEUTRAL

    p1["value"] = "malformed, not json"
    chat_reply2, status_text2 = gui.respond("second message", [])
    assert chat_reply2 == gui.CONTROL_FAILURE_CHAT_MARKER
    assert status_text2 == gui.CONTROL_FAILURE_STATUS_TEXT

    records = _read_diag_records(gui)
    assert len(records) == 2
    assert records[0]["callback_success"] is True
    assert records[0]["waking_turn_success"] is True
    assert records[0]["control_failure_contained"] is False
    assert records[1]["callback_success"] is False
    assert records[1]["waking_turn_success"] is False
    assert records[1]["control_failure_contained"] is True


def test_contained_failure_status_panel_clears_on_next_success():
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="malformed, not json",
        pass2_value={"expression": "unreachable"},
    )
    _chat1, status1 = gui.respond(NEUTRAL_MESSAGE, [])
    assert status1 == gui.CONTROL_FAILURE_STATUS_TEXT

    p1["value"] = {"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False}
    p2["value"] = {"expression": "Back to normal."}
    chat2, status2 = gui.respond("second message", [])
    assert chat2 == "Back to normal."
    assert status2 == gui.CONTROL_FAILURE_STATUS_NEUTRAL  # stale failure text does not linger


def test_no_content_leak_in_contained_failure_diagnostics():
    # Test H (spec section 19).
    secret_prompt = "SECRET-MARKER-nobody-should-see-this-in-a-log"
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="malformed prose, not json, contains no secret",
        pass2_value={"expression": "unreachable"},
    )
    gui.respond(secret_prompt, [])
    with open(gui.UI_TURN_DIAGNOSTICS_LOG_PATH, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_prompt not in raw_text


def test_zero_retry_on_contained_control_failure():
    # Test E (spec section 19), reconfirmed through the full GUI path.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="malformed, not json",
        pass2_value={"expression": "unreachable"},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1
    assert pass2_calls == 0


def test_ui_controls_remain_usable_after_contained_failure():
    # Test K (spec section 19), as far as this Gradio-server-free
    # harness can prove it: after a contained control failure, this
    # SAME process's host-side control-surface functions (button
    # handlers, status refreshers -- the actual Python callables Gradio
    # wires to the page's buttons) still execute normally. A real
    # browser reload/interactivity check is out of scope for this
    # module-level harness (see the final report's smoke-test note).
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="malformed, not json",
        pass2_value={"expression": "unreachable"},
    )
    gui.respond(NEUTRAL_MESSAGE, [])  # contained failure

    # direction/roaming status + control surfaces still callable/working:
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"
    assert gui.refresh_roaming_status() == "**Background Space activity: Idle**"
    setup_canonical_human_actor(test_dir)
    status_after_take = gui.take_direction()
    assert "You" in status_after_take

    # and an ordinary turn immediately afterward completes normally --
    # the backend/session genuinely survived, not just "didn't crash
    # this one call":
    p1["value"] = {"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False}
    p2["value"] = {"expression": "Back online."}
    reply, _status = gui.respond("are you still there", [])
    assert reply == "Back online."


# ============================ OWC7-P2 gate B: family-facing conversation lead


def test_owc7pB_owner_unknown_request_none_amber_open():
    # Test A (spec section B15).
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"


def test_owc7pB_owner_human_request_none_blue_you():
    # Test B.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    ws = dict(la.get_working_set())
    ws["direction_owner"] = la.DIRECTION_HUMAN
    la.set_working_set(ws)
    assert gui.refresh_conversation_lead() == "\U0001f535 **Conversation lead: You**"


def test_owc7pB_owner_clark_request_none_green_clark():
    # Test C.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    ws = dict(la.get_working_set())
    ws["direction_owner"] = la.DIRECTION_CLARK
    la.set_working_set(ws)
    assert gui.refresh_conversation_lead() == "\U0001f7e2 **Conversation lead: Clark**"


def test_owc7pB_owner_unknown_request_clark_separate_green_banner():
    # Test D: owner remains Open/amber; a SEPARATE green request banner
    # appears; "Let Clark lead" remains the exposed action (source
    # check -- the button label literal, spec B4/B7).
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "request_clark", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    assert la.get_working_set()["direction_owner"] == "unknown"  # request alone never changes ownership
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"
    assert gui.refresh_conversation_lead_request() == "\U0001f7e2 Clark is asking to lead."
    with open(os.path.join(ANAXI_FINAL, "llama_gui.py"), encoding="utf-8") as f:
        assert '"Let Clark lead"' in f.read()


def test_owc7pB_owner_unknown_request_human_separate_blue_banner():
    # Test E.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "request_human", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    assert la.get_working_set()["direction_owner"] == "unknown"
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"
    assert gui.refresh_conversation_lead_request() == "\U0001f535 Clark is asking you to lead."
    with open(os.path.join(ANAXI_FINAL, "llama_gui.py"), encoding="utf-8") as f:
        assert '"Take the lead"' in f.read()


def test_owc7pB_let_clark_lead_button_uses_existing_transition():
    # Test F: same _perform_human_direction_control()/give_direction_to_
    # clark() as before this gate -- only the button label changed.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    gui.give_direction_to_clark()
    assert la.get_working_set()["direction_owner"] == "clark"


def test_owc7pB_take_the_lead_button_uses_existing_transition():
    # Test G.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    gui.take_direction()
    assert la.get_working_set()["direction_owner"] == "human"


def test_owc7pB_request_display_never_mutates_ownership():
    # Test H.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "request_clark", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    before = dict(la.get_working_set())
    for _ in range(5):
        gui.refresh_conversation_lead_request()
    assert la.get_working_set() == before


def test_owc7pB_malformed_act_no_lead_request_indicator():
    # Test I.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value="not valid json at all",
        pass2_value={"expression": "unreachable"},
    )
    chat_reply, status_text = gui.respond(NEUTRAL_MESSAGE, [])
    assert status_text == gui.CONTROL_FAILURE_STATUS_TEXT  # Test M: status panel still usable/correct
    assert gui.refresh_conversation_lead_request() == ""
    assert gui.refresh_conversation_lead() == "\U0001f7e0 **Conversation lead: Open**"


def test_owc7pB_unauthorized_relinquish_no_misleading_request_indicator():
    # Test J: direction_request WAS structurally present on the raw
    # act (request_clark, alongside the unauthorized relinquish), but
    # pass1_status != "ok" -- must still show nothing.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "request_clark", "relinquish_direction": True},
        pass2_value={"expression": "unreachable"},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    trace = la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "UNAUTHORIZED_RELINQUISH"
    assert trace["direction_request"] == "request_clark"  # structurally present, raw
    assert gui.refresh_conversation_lead_request() == ""  # but not family-facing-displayed


def test_owc7pB_raw_values_remain_available_in_diagnostics():
    # Test K.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(
        ["llama_gui.py", "--conversation"],
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "request_clark", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    gui.respond(NEUTRAL_MESSAGE, [])
    assert la.get_working_set()["direction_owner"] == "unknown"  # raw mechanical value, unrenamed
    import conversation_direction_trace as cdt
    records = cdt.query_trace(trace_path)
    assert records[0]["direction_request"] == "request_clark"  # raw value, untouched by presentation


def test_owc7pB_roaming_controls_remain_usable():
    # Test L.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    assert gui.refresh_roaming_status() == "**Background Space activity: Idle**"


def test_owc7pB_button_labels_changed_control_acts_unchanged():
    # Test N: label strings changed; the underlying control_act
    # constants/trace semantics did not.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    setup_canonical_human_actor(test_dir)
    gui.give_direction_to_clark()
    records = direction_control_module().query_direction_control_trace(dc_trace_path)
    assert records[0]["control_act"] == "give_direction_to_clark"  # unchanged mechanical constant
    assert gui.direction_control.TAKE_DIRECTION == "take_direction"
    assert gui.direction_control.GIVE_DIRECTION_TO_CLARK == "give_direction_to_clark"


def test_owc7pB_color_not_sole_signal():
    # Test O: every displayed state/request string carries readable
    # plain-language text, not just a color swatch.
    gui, la, call_log, p1, p2, persistence, trace_path, test_dir, dc_trace_path = fresh_gui(["llama_gui.py", "--conversation"])
    for owner, expected_word in ((la.DIRECTION_UNKNOWN, "Open"), (la.DIRECTION_HUMAN, "You"), (la.DIRECTION_CLARK, "Clark")):
        ws = dict(la.get_working_set())
        ws["direction_owner"] = owner
        la.set_working_set(ws)
        text = gui.refresh_conversation_lead()
        assert expected_word in text
        # stripping the leading emoji swatch still leaves comprehensible text
        stripped = text.split(" ", 1)[1] if " " in text else text
        assert "Conversation lead" in stripped


ALL_TESTS = [
    test_gui_default_launch_resolves_task,
    test_gui_conversation_flag_resolves_conversation,
    test_gui_rejects_unknown_extra_argument,
    test_desktop_default_resolves_task,
    test_desktop_conversation_flag_resolves_conversation,
    test_respond_passes_explicit_mode_every_turn,
    test_persistent_session_across_multiple_turns,
    test_conversation_gui_reaches_typed_pass1_pass2,
    test_task_gui_bypasses_typed_pass1_pass2,
    test_conversation_gui_emits_exactly_one_durable_trace,
    test_task_gui_emits_zero_trace_records,
    test_mode_indicator_matches_execution_mode_conversation,
    test_mode_indicator_matches_execution_mode_task,
    test_indicator_derived_from_launch_not_model_or_prose,
    test_dialogue_window_present_on_second_conversation_turn,
    test_owc6_p1_clause_unchanged,
    test_task_mode_behavior_unchanged_no_owc5_wiring,
    test_api1_api2_source_hashes_unchanged,
    test_workspace_paths_unreferenced_in_gui_files,
    test_no_test_writes_to_production_paths,
    # OWC7-S2
    test_fresh_process_status_displays_unknown,
    test_interface_load_alone_does_not_alter_owner,
    test_give_direction_to_clark_full_flow,
    test_take_direction_full_flow,
    test_repeated_explicit_control_idempotent_and_recorded_each_time,
    test_unresolved_human_actor_fails_closed,
    test_ambiguous_human_actor_registry_fails_closed,
    test_shared_backend_desktop_and_browser_same_owner,
    test_reconnect_render_cannot_mutate_owner,
    test_next_pass2_interface_sees_owner_after_give_to_clark,
    test_next_pass2_interface_sees_owner_after_take_direction,
    test_trace_correct_values_and_session_id,
    test_control_trace_never_referenced_by_dialogue_kardia_hippocampus_sources,
    # WSP2-S1 / WSP2-P5
    test_roaming_fresh_status_idle,
    test_roaming_load_alone_does_not_enable,
    test_resume_background_activity_enables_and_starts_worker,
    test_resume_background_activity_resolution_failure_stays_off,
    test_pause_background_activity_disables_and_stops_worker,
    test_roaming_shared_backend_desktop_and_browser,
    test_roaming_zero_model_calls_from_button_click_itself,
    test_resume_background_activity_wires_production_private_paths,
    test_gui_status_text_unaffected_by_private_wiring,
    # WSP2-P5-P1
    test_p5p1_respond_applies_clark_self_resume_when_durably_recorded,
    test_p5p1_respond_leaves_paused_by_human_untouched_even_with_resume_own_pause,
    # WSP2-P5-P2
    test_p5p2_human_resume_refuses_to_start_while_authority_still_unknown,
    test_p5p2_human_resume_recovers_once_canonical_truth_becomes_readable,
    test_p5p2_status_text_shows_recovery_needed,
    # WSP2-P5-P2a
    test_p5p2a_provenance_db_dir_anchored_to_source_file_not_cwd,
    test_p5p2a_provenance_db_dir_resolves_to_this_repo_directory,
    test_p5p2a_recomputing_after_cwd_change_still_matches,
    test_p5p2a_explicit_test_data_dir_override_still_authoritative,
    test_p5p2a_launch_launcher_shares_launch_production_backend_not_reimplemented,
    test_p5p2a_desktop_launcher_shares_launch_production_backend_not_reimplemented,
    # OWC7-P2
    test_malformed_act_contained_gui_survives_and_flags_diagnostics,
    test_unauthorized_relinquish_contained_owner_unchanged_no_preview,
    test_diagnostic_semantic_split_success_vs_contained_failure,
    test_contained_failure_status_panel_clears_on_next_success,
    test_no_content_leak_in_contained_failure_diagnostics,
    test_zero_retry_on_contained_control_failure,
    test_ui_controls_remain_usable_after_contained_failure,
    # OWC7-P2 gate B
    test_owc7pB_owner_unknown_request_none_amber_open,
    test_owc7pB_owner_human_request_none_blue_you,
    test_owc7pB_owner_clark_request_none_green_clark,
    test_owc7pB_owner_unknown_request_clark_separate_green_banner,
    test_owc7pB_owner_unknown_request_human_separate_blue_banner,
    test_owc7pB_let_clark_lead_button_uses_existing_transition,
    test_owc7pB_take_the_lead_button_uses_existing_transition,
    test_owc7pB_request_display_never_mutates_ownership,
    test_owc7pB_malformed_act_no_lead_request_indicator,
    test_owc7pB_unauthorized_relinquish_no_misleading_request_indicator,
    test_owc7pB_raw_values_remain_available_in_diagnostics,
    test_owc7pB_roaming_controls_remain_usable,
    test_owc7pB_button_labels_changed_control_acts_unchanged,
    test_owc7pB_color_not_sole_signal,
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


def test_gui_says_a_reply_that_did_not_finish_generating_is_not_a_control_validation_failure():
    """Live: a workspace turn's vision reply exhausted its window (done_reason=length, zero content). The
    status panel said the conversational-direction control could not be validated -- untrue."""
    gui, *_rest = fresh_gui(['llama_gui.py', '--conversation'])
    import conversation_direction as cd

    for stage, code in (("workspace_pass2", "COMPLETION_LIMIT_REACHED"), ("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY")):
        marker, status = gui.contain_control_failure(cd.ConversationDirectionFailure(stage, code))
        assert "did not finish generating" in status and code in status
        assert "could not be validated" not in status
        assert "not a Clark response" in marker
    _marker, control_status = gui.contain_control_failure(cd.ConversationDirectionFailure("pass1", "MALFORMED_ACT"))
    assert "could not be validated" in control_status                 # genuine control failures keep their text
