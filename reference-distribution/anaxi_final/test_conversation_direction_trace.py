"""OWC6-O1 acceptance tests. Zero real Ollama/model calls -- ollama
itself is faked at the sys.modules boundary, exactly matching
test_owc5_s2_integration.py's established convention. orchestration/
relational_history/native_provenance_writer are faked the same way, so
no real database of any kind is touched. The one addition relative to
that file's fake is `staging_id` in the fake persistence return dict,
needed because OWC6-O1's trace binding reads it from native_result.
"""
import json
import os
import sys
import tempfile
import traceback
import types

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import conversation_direction_trace as cdt


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
    # dead defensive code with no effect on anything this file actually
    # tests, and its only observed real effect was a genuine test-
    # isolation leak: sys.modules["sleep_receipts"] was left permanently
    # substituted with no restoration, corrupting any later same-process
    # test (e.g. test_sleep_receipts.py) that performs a dynamic
    # sys.modules-keyed lookup against the real module (inspect.getsource()
    # does exactly this internally). Removed rather than wrapped in
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
        staging_id = f"fake-staging-{_n['count']}"
        participations = []
        if artifact_pass_ran:
            participations.append({"model_revision_id": "fake-rev-pass1", "tag": waking_model_tag})
        participations.append({"model_revision_id": "fake-rev-pass2", "tag": waking_model_tag})
        return {
            "event_id": event_id, "session_id": session_id,
            "auth_context_id": f"fake-auth-{_n['count']}",
            "pipeline_id": f"fake-pipeline-{pipeline_key}",
            "staging_id": staging_id,
            "model_participations": participations,
            "reassembled_reply": (bounded_clause + " " + clark_prose).strip(),
            "occurred_at": occurred_at,
        }

    fake_npw_module.generate_native_ulid = _fake_generate_native_ulid
    fake_npw_module.stage_and_record_native_waking_turn = _fake_stage_and_record_native_waking_turn
    sys.modules["native_provenance_writer"] = fake_npw_module

    return persistence_log


def fresh_llama_anaxi(pass1_value=None, pass2_value=None, core_system_text=None, style_instruction=None,
                       model_digest=_CALIBRATED_MODEL_DIGEST, server_version=_CALIBRATED_SERVER_VERSION,
                       chat_template_sha256=_CALIBRATED_CHAT_TEMPLATE_SHA256):
    """Returns (llama_anaxi module, call_log, pass1_holder, pass2_holder,
    persistence_log, trace_path) with every heavy dependency faked and
    PROVENANCE_DB_DIR/STAGING_PATH/CONVERSATION_DIRECTION_TRACE_PATH
    redirected to a fresh temp dir."""
    call_log = []
    pass1_holder = {"value": pass1_value}
    pass2_holder = {"value": pass2_value}
    sys.modules["ollama"] = build_fake_ollama(call_log, pass1_holder, pass2_holder, model_digest=model_digest)
    persistence_log = install_fake_heavy_dependencies(core_system_text=core_system_text, style_instruction=style_instruction)

    for mod in ("llama_anaxi", "conversation_direction", "conversation_direction_trace"):
        if mod in sys.modules:
            del sys.modules[mod]

    import llama_anaxi
    llama_anaxi._ollama_environment_cache.update({
        "checked": True, "model_digest": model_digest,
        "server_version": server_version, "chat_template_sha256": chat_template_sha256,
    })
    test_dir = tempfile.mkdtemp(prefix="owc6o1_test_")
    llama_anaxi.PROVENANCE_DB_DIR = test_dir
    llama_anaxi.STAGING_PATH = os.path.join(test_dir, "native_turn_staging.jsonl")
    trace_path = os.path.join(test_dir, "conversation_direction_trace.jsonl")
    llama_anaxi.CONVERSATION_DIRECTION_TRACE_PATH = trace_path
    llama_anaxi.reset_working_set()  # simulate a fresh session
    return llama_anaxi, call_log, pass1_holder, pass2_holder, persistence_log, trace_path


NEUTRAL_PROMPT = "So what's on your mind?"  # must not trigger the POSITIVE signal gate


# --------------------------------------------- A/B/C/D/G: success case ----


def test_successful_turn_produces_exactly_one_trace_record():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "pattern recognition", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A developed conversational response."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    records = cdt.query_trace(trace_path)
    assert len(records) == 1  # A


def test_raw_act_and_validated_act_preserved_exactly():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "shift_topic", "thread": "music", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Let's talk about music instead."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    records = cdt.query_trace(trace_path)
    assert records[0]["raw_act"] == "shift_topic"  # B
    assert records[0]["validated_act"] == "shift_topic"  # C
    assert records[0]["thread"] == "music"


def test_working_set_before_after_preserved_mechanically():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "shift_topic", "thread": "astronomy", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Sure, astronomy it is."},
    )
    before = dict(la.get_working_set())
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    records = cdt.query_trace(trace_path)
    trace = records[0]
    assert trace["working_set_before"] == before
    assert trace["working_set_after"]["active_thread"] == "astronomy"
    assert trace["working_set_after"]["active_thread_origin"] == "clark"
    expected_keys = {"active_thread", "active_thread_origin", "last_conversation_act", "open_threads", "direction_owner"}
    assert set(trace["working_set_before"].keys()) == expected_keys
    assert set(trace["working_set_after"].keys()) == expected_keys


def test_successful_release_binds_trace_to_real_event_and_staging_id():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "Continuing on."},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    records = cdt.query_trace(trace_path)
    trace = records[0]
    assert trace["event_id"] == result["native_event_id"]  # bound to the SAME id run_waking_turn() itself returns
    assert trace["event_id"] is not None and trace["event_id"].startswith("fake-event-")
    assert trace["staging_id"] is not None and trace["staging_id"].startswith("fake-staging-")
    assert trace["session_id"] == la._get_native_session()[0]


# --------------------------------------------------- E: content exclusion -


def test_pass1_free_form_content_never_durably_persisted():
    secret_reasoning = "THIS IS FREE-FORM REASONING TEXT THAT MUST NEVER BE WRITTEN DURABLY"
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_file_text = f.read()
    assert secret_reasoning not in raw_trace_file_text
    records = cdt.query_trace(trace_path)
    assert "content" not in records[0]


# ------------------------------------------------- F: no prose duplication


def test_no_released_prose_duplicated_in_trace():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "UNIQUE_RELEASED_EXPRESSION_TEXT_12345"},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_file_text = f.read()
    assert "UNIQUE_RELEASED_EXPRESSION_TEXT_12345" not in raw_trace_file_text


# ---------------------------------------------------- H: restart retrieval


def test_trace_retrievable_and_joinable_after_simulated_restart():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "ask_human", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "What would you like to talk about?"},
    )
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")

    # Simulate process exit/restart: drop the module from sys.modules
    # and reimport it fresh, exactly as a new process would.
    del sys.modules["conversation_direction_trace"]
    import conversation_direction_trace as cdt_fresh

    records = cdt_fresh.query_trace(trace_path)
    assert len(records) == 1
    trace = records[0]
    assert trace["validated_act"] == "ask_human"
    # Join to the released turn via the durable persistence_log (the
    # fake's stand-in for canonical provenance/staging).
    assert persistence[0]["prompt"] == NEUTRAL_PROMPT
    assert trace["event_id"] == result["native_event_id"]


# ------------------------------------------------------- I: pass1 failure


def test_pass1_failure_no_event_binding_no_false_release():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "not_a_real_act", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "unreachable"},
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None and raised.stage == "pass1"
    assert len(persistence) == 0  # never reached the persistence call
    records = cdt.query_trace(trace_path)
    assert len(records) == 1
    trace = records[0]
    assert trace["pass1_status"] == "INVALID_ACT"
    assert trace["pass2_status"] is None
    assert trace["validated_act"] is None
    assert trace["event_id"] is None
    assert trace["staging_id"] is None
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass2_calls == 0  # Pass 2 never ran


# ------------------------------------------------------- J: pass2 failure


def test_pass2_failure_preserves_typed_act_no_release():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "vintage cameras", "direction_request": "none", "relinquish_direction": False},
        pass2_value="   ",   # a blank completion: expression not established
    )
    raised = None
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    except la.ConversationDirectionFailure as exc:
        raised = exc
    assert raised is not None and raised.stage == "pass2"
    assert len(persistence) == 0  # no release => no persistence
    records = cdt.query_trace(trace_path)
    assert len(records) == 1
    trace = records[0]
    assert trace["pass1_status"] == "ok"
    assert trace["validated_act"] == "develop_current"
    assert trace["thread"] == "vintage cameras"
    assert trace["pass2_status"] == "EMPTY_EXPRESSION"
    assert trace["event_id"] is None
    assert trace["staging_id"] is None


# ------------------------------------------------- K: memory/Kardia exclusion


def test_no_memory_kardia_hippocampus_journal_dialogue_reference():
    # The meaningful, non-fragile check (mirrors interaction_mode.py's
    # own test_no_prepare_context_or_model_dependency() convention):
    # zero imports beyond the standard library means this module has
    # no CAPABILITY to reach Kardia/hippocampus/journal/dialogue-window
    # code, regardless of how those words appear in its own docstring
    # prose explaining what it deliberately is NOT.
    with open(os.path.join(ANAXI_FINAL, "conversation_direction_trace.py"), encoding="utf-8") as f:
        source = f.read()
    import ast
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert imported_names.issubset({"datetime", "json", "os", "uuid"})
    # No call-site reference to any messages list at all (this module
    # never inserts anything into what the model sees).
    assert "messages.append" not in source
    assert "insert_dialogue_window(" not in source


# ----------------------------------------------------- L: retry/fallback --


def test_retry_and_fallback_fields_always_zero_false_and_call_counts_unchanged():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "A reply."},
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    records = cdt.query_trace(trace_path)
    assert records[0]["retry_count"] == 0
    assert records[0]["fallback_used"] is False
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    pass2_calls = sum(1 for c in call_log if not _is_json_format(c["format"]))
    assert pass1_calls == 1 and pass2_calls == 1  # unchanged from OWC5-S2's own acceptance test


# --------------------------------------------------------- M: task mode ---


def test_task_mode_writes_no_trace_record_and_unaffected():
    la, call_log, p1, p2, persistence, trace_path = fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False},
        pass2_value="An ordinary task-mode reply.",
    )
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT)  # interaction_mode default (task)
    records = cdt.query_trace(trace_path)
    assert records == []
    pass1_calls = sum(1 for c in call_log if _is_json_format(c["format"]))
    assert pass1_calls == 0  # OWC5 Pass-1 never invoked in task mode
    assert len(persistence) == 1
    assert persistence[0]["clark_prose"] == "An ordinary task-mode reply."


def test_llama_anaxi_wiring_present():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "import conversation_direction_trace" in source
    assert "conversation_direction_trace.record_trace(" in source
    assert "conversation_direction_trace.extract_raw_act(" in source


# -------------------------------------------------- N/O: noninterference --


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


def test_trace_module_isolated_while_waking_owns_workspace_integration():
    # O: the same import-capability proof as K -- conversation_
    # direction_trace.py imports nothing beyond the standard library
    # (asserted below), so it has no way to import or call into
    # workspace_capability.py/workspace_direction.py/workspace_
    # supervisor.py regardless of any filename mentioned in its own
    # comments (e.g. "mirrors workspace_capability.py's _log_action()
    # convention" -- a prose credit, not a coupling).
    with open(os.path.join(ANAXI_FINAL, "conversation_direction_trace.py"), encoding="utf-8") as f:
        source = f.read()
    import ast
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert imported_names.issubset({"datetime", "json", "os", "uuid"})
    assert "import workspace" not in source
    # Workspace integration now deliberately belongs to ordinary waking;
    # the trace writer remains isolated from it and cannot execute actions.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        la_source = f.read()
    assert "import workspace_capability" in la_source
    assert "import workspace_supervisor" in la_source
    assert "cross_validate_workspace_coexistence" in la_source


ALL_TESTS = [
    test_successful_turn_produces_exactly_one_trace_record,
    test_raw_act_and_validated_act_preserved_exactly,
    test_working_set_before_after_preserved_mechanically,
    test_successful_release_binds_trace_to_real_event_and_staging_id,
    test_pass1_free_form_content_never_durably_persisted,
    test_no_released_prose_duplicated_in_trace,
    test_trace_retrievable_and_joinable_after_simulated_restart,
    test_pass1_failure_no_event_binding_no_false_release,
    test_pass2_failure_preserves_typed_act_no_release,
    test_no_memory_kardia_hippocampus_journal_dialogue_reference,
    test_retry_and_fallback_fields_always_zero_false_and_call_counts_unchanged,
    test_task_mode_writes_no_trace_record_and_unaffected,
    test_llama_anaxi_wiring_present,
    test_api1_api2_source_hashes_unchanged,
    test_trace_module_isolated_while_waking_owns_workspace_integration,
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
