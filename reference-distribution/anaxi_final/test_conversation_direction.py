"""OWC5-S1/OWC7-S1 acceptance tests. Zero real prepare_context()/model
calls -- pure unit tests against fake Pass-1/Pass-2 callables only.
"""
import json
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import conversation_direction as cd

CANONICAL_HUMAN_ACTOR_ID = "human-actor-c8feddc1b4bb"
ARBITRARY_OPAQUE_HUMAN_ID = "human-actor-ffffffffffffffffffffffff-not-canonical"


def counting_callable(return_value_or_fn):
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if callable(return_value_or_fn):
            return return_value_or_fn()
        return return_value_or_fn

    _fn.calls = calls
    return _fn


def ws_with_active_thread(thread="pattern recognition", origin=cd.ORIGIN_CLARK):
    ws = cd.empty_working_set()
    ws["active_thread"] = thread
    ws["active_thread_origin"] = origin
    return ws


def raw_act(act, thread="", direction_request=cd.REQUEST_NONE, relinquish_direction=False):
    return {"act": act, "thread": thread, "direction_request": direction_request, "relinquish_direction": relinquish_direction}


# --------------------------------------------------- A: task noninterference


def test_task_mode_pathway_not_invoked_anywhere():
    """Superseded by OWC5-S2 (llama_anaxi.py now legitimately imports
    and wires this pathway in for conversation mode -- see
    test_owc5_s2_integration.py for the full behavioral proof that
    TASK mode specifically never invokes it). This test now checks the
    narrower, still-true invariant: the wiring exists only inside
    run_waking_turn()'s explicit `if resolved_interaction_mode ==
    CONVERSATION_MODE:` branch, never unconditionally."""
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "from conversation_direction import" in source
    assert "if resolved_interaction_mode == CONVERSATION_MODE:" in source


# --------------------------------------------------------- B: develop_current


def test_develop_current_retains_active_thread():
    ws = ws_with_active_thread()
    act = raw_act(cd.DEVELOP_CURRENT)
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["active_thread"] == "pattern recognition"
    assert new_ws["active_thread_origin"] == cd.ORIGIN_CLARK
    assert new_ws["last_conversation_act"] == cd.DEVELOP_CURRENT
    assert new_ws["open_threads"] == []


# --------------------------------------------------------------- C: shift


def test_shift_topic_establishes_supplied_thread_origin_clark():
    ws = ws_with_active_thread(thread="pattern recognition")
    act = raw_act(cd.SHIFT_TOPIC, thread="moral philosophy")
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["active_thread"] == "moral philosophy"
    assert new_ws["active_thread_origin"] == cd.ORIGIN_CLARK
    assert new_ws["last_conversation_act"] == cd.SHIFT_TOPIC


# ------------------------------------------------------------------ D: ask


def test_ask_human_does_not_automatically_transfer_direction():
    ws = ws_with_active_thread(thread="pattern recognition")
    act = raw_act(cd.ASK_HUMAN)
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["active_thread"] == "pattern recognition"  # unchanged
    assert new_ws["last_conversation_act"] == cd.ASK_HUMAN

    # explicit new thread supplied alongside ask_human IS honored
    act2 = raw_act(cd.ASK_HUMAN, thread="a new angle")
    new_ws2 = cd.apply_conversation_act(ws, act2)
    assert new_ws2["active_thread"] == "a new angle"
    assert new_ws2["active_thread_origin"] == cd.ORIGIN_CLARK


# ---------------------------------------------------------- E: explicit yield


def test_only_yield_direction_records_deliberate_handoff():
    ws = ws_with_active_thread(thread="pattern recognition", origin=cd.ORIGIN_CLARK)
    act = raw_act(cd.YIELD_DIRECTION)
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["last_conversation_act"] == cd.YIELD_DIRECTION
    assert new_ws["active_thread"] == "pattern recognition"  # unchanged, not fabricated as human's
    assert new_ws["active_thread_origin"] == cd.ORIGIN_CLARK  # never invented as human

    # other acts do NOT set last_conversation_act to yield_direction
    for other_act in (cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN, cd.PAUSE_THREAD, cd.CLOSE_THREAD):
        other = cd.apply_conversation_act(ws, raw_act(other_act, thread="x"))
        assert other["last_conversation_act"] != cd.YIELD_DIRECTION


# ------------------------------------------------------------------ F: pause


def test_pause_thread_moves_active_to_open_threads():
    ws = ws_with_active_thread(thread="pattern recognition")
    act = raw_act(cd.PAUSE_THREAD)
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["active_thread"] is None
    assert new_ws["active_thread_origin"] is None
    assert new_ws["open_threads"] == ["pattern recognition"]
    assert new_ws["last_conversation_act"] == cd.PAUSE_THREAD


def test_pause_with_no_active_thread_is_a_noop_on_open_threads():
    ws = cd.empty_working_set()
    new_ws = cd.apply_conversation_act(ws, raw_act(cd.PAUSE_THREAD))
    assert new_ws["open_threads"] == []
    assert new_ws["active_thread"] is None


# ------------------------------------------------------------------ G: close


def test_close_thread_clears_without_appending_to_open_threads():
    ws = ws_with_active_thread(thread="pattern recognition")
    act = raw_act(cd.CLOSE_THREAD)
    new_ws = cd.apply_conversation_act(ws, act)
    assert new_ws["active_thread"] is None
    assert new_ws["active_thread_origin"] is None
    assert new_ws["open_threads"] == []  # distinguishes close from pause
    assert new_ws["last_conversation_act"] == cd.CLOSE_THREAD


# --------------------------------------------------------- H: open-thread bound


def test_open_thread_bound_deterministic_oldest_drop():
    ws = cd.empty_working_set()
    for i in range(5):
        ws["active_thread"] = f"thread-{i}"
        ws = cd.apply_conversation_act(ws, raw_act(cd.PAUSE_THREAD))
        ws["active_thread"] = None  # simulate: nothing active before next pause seeds a new one via direct assignment
    assert len(ws["open_threads"]) == 4
    assert ws["open_threads"] == ["thread-1", "thread-2", "thread-3", "thread-4"]  # thread-0 dropped (oldest)


# ------------------------------------------------------ I: fresh-session isolation


def test_working_set_functions_are_pure_no_cross_call_leakage():
    ws_a = ws_with_active_thread(thread="session A topic")
    ws_b = cd.empty_working_set()
    result_a = cd.apply_conversation_act(ws_a, raw_act(cd.DEVELOP_CURRENT))
    result_b = cd.apply_conversation_act(ws_b, raw_act(cd.DEVELOP_CURRENT))
    assert result_a["active_thread"] == "session A topic"
    assert result_b["active_thread"] is None
    assert ws_a["active_thread"] == "session A topic"  # original input untouched


def test_session_state_holder_reset_gives_fresh_empty_set():
    cd.set_working_set(ws_with_active_thread(thread="leftover from a prior session"))
    assert cd.get_working_set()["active_thread"] == "leftover from a prior session"
    cd.reset_working_set()
    fresh = cd.get_working_set()
    assert fresh == cd.empty_working_set()
    assert fresh["active_thread"] is None
    assert fresh["open_threads"] == []
    assert fresh["direction_owner"] == cd.DIRECTION_UNKNOWN


# --------------------------------------------------- J: dialogue compatibility


def test_owc2_dialogue_window_passed_through_unchanged():
    dialogue_window = [
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Pattern recognition."},
    ]
    iface1 = cd.build_pass1_interface("system text", dialogue_window, "current prompt", cd.empty_working_set())
    assert iface1["dialogue_window"] == dialogue_window
    assert iface1["dialogue_window"] is not dialogue_window  # defensive copy, not aliased

    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_NONE, False)
    iface2 = cd.build_pass2_interface(
        "system text", dialogue_window, "current prompt",
        raw_act(cd.DEVELOP_CURRENT), cd.empty_working_set(), direction_resolution,
    )
    assert iface2["dialogue_window"] == dialogue_window


# --------------------------------------------------- K: aesthetic compatibility


def test_owc4_aesthetic_module_untouched():
    # "_interaction_mode_state" is mentioned in prose (comparing this
    # module's own session-state pattern to llama_anaxi.py's existing
    # one) -- that is not a dependency on interaction_mode.py. The real
    # check is: no import of it, and none of its actual symbols used.
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    assert "from interaction_mode import" not in source
    assert "import interaction_mode" not in source
    assert "CONVERSATION_AESTHETIC_DIRECTIVE" not in source
    assert "apply_conversation_aesthetic" not in source


# --------------------------------------------------- L: zero semantic inference


def test_expression_wording_never_changes_typed_act():
    ws = cd.empty_working_set()
    pass1 = counting_callable(raw_act(cd.DEVELOP_CURRENT))
    # Pass 2's expression contains a question -- host must NOT reinterpret this as ask_human
    pass2 = counting_callable(json.dumps({"expression": "Here's a thought. What do you think?"}))
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn(
        "system", [], "current", ws, pass1, pass2,
    )
    assert failure is None
    assert result["act"] == cd.DEVELOP_CURRENT
    assert new_ws["last_conversation_act"] == cd.DEVELOP_CURRENT

    # Pass 2's expression sounds tentative/yielding -- still not reinterpreted as yield_direction
    pass1b = counting_callable(raw_act(cd.DEVELOP_CURRENT))
    pass2b = counting_callable({"expression": "I suppose it's up to you, really."})
    result2, failure2, new_ws2, _, _ = cd.run_typed_conversation_turn(
        "system", [], "current", ws, pass1b, pass2b,
    )
    assert failure2 is None
    assert result2["act"] == cd.DEVELOP_CURRENT
    assert new_ws2["last_conversation_act"] == cd.DEVELOP_CURRENT


# ------------------------------------------------------- M: malformed Pass 1


def test_malformed_pass1_fails_closed_no_fabricated_act():
    ws = ws_with_active_thread(thread="unchanged topic")
    pass1 = counting_callable({"act": "not_a_real_act", "thread": "x", "direction_request": cd.REQUEST_NONE, "relinquish_direction": False})
    pass2 = counting_callable({"expression": "unreachable"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert result is None
    assert failure == cd.DirectionFailure.INVALID_ACT
    assert new_ws == ws  # completely unchanged
    assert p1 == 1
    assert p2 == 0  # pass2 never invoked
    assert pass2.calls["n"] == 0


def test_malformed_pass1_json_and_extra_fields():
    ws = cd.empty_working_set()
    for bad_raw, expected in (
        ("not json at all {{{", cd.DirectionFailure.MALFORMED_ACT),
        (42, cd.DirectionFailure.MALFORMED_ACT),
        ({**raw_act(cd.DEVELOP_CURRENT), "extra": "leak"}, cd.DirectionFailure.PROTOCOL_LEAKAGE),
        ({"act": cd.DEVELOP_CURRENT, "thread": ""}, cd.DirectionFailure.MALFORMED_ACT),
        ({**raw_act(cd.DEVELOP_CURRENT), "thread": 5}, cd.DirectionFailure.INVALID_THREAD),
        ({**raw_act(cd.DEVELOP_CURRENT), "direction_request": "not_a_real_request"}, cd.DirectionFailure.INVALID_DIRECTION_REQUEST),
        ({**raw_act(cd.DEVELOP_CURRENT), "relinquish_direction": "true"}, cd.DirectionFailure.INVALID_RELINQUISH_TYPE),
        ({**raw_act(cd.DEVELOP_CURRENT), "relinquish_direction": 1}, cd.DirectionFailure.INVALID_RELINQUISH_TYPE),
    ):
        validated, failure = cd.validate_pass1_conversation_act(bad_raw)
        assert validated is None
        assert failure == expected, f"raw={bad_raw!r} expected={expected} got={failure}"


def test_content_field_no_longer_accepted():
    # OWC7-S1: a payload carrying the OLD `content` field instead of
    # the new required fields must fail as malformed (missing required
    # keys), not be silently accepted or ignored.
    validated, failure = cd.validate_pass1_conversation_act(
        {"act": cd.DEVELOP_CURRENT, "thread": "", "content": "leftover free-form text"}
    )
    assert validated is None
    assert failure == cd.DirectionFailure.MALFORMED_ACT


# --------------------------------------------------------- N: Pass-2 failure


def test_pass2_failure_leaves_act_and_state_observable_no_reselection():
    ws = ws_with_active_thread(thread="pattern recognition")
    pass1 = counting_callable(raw_act(cd.SHIFT_TOPIC, thread="new topic"))
    pass2 = counting_callable({"wrong_key": "oops"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert result is None
    assert failure == cd.DirectionFailure.MALFORMED_EXPRESSION
    # the act WAS validly selected and its transition IS observable:
    assert new_ws["active_thread"] == "new topic"
    assert new_ws["active_thread_origin"] == cd.ORIGIN_CLARK
    assert new_ws["last_conversation_act"] == cd.SHIFT_TOPIC
    assert p1 == 1
    assert p2 == 1
    assert pass1.calls["n"] == 1  # no automatic reselection


def test_pass2_empty_expression_rejected():
    ws = cd.empty_working_set()
    pass1 = counting_callable(raw_act(cd.DEVELOP_CURRENT))
    pass2 = counting_callable({"expression": "   "})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert result is None
    assert failure == cd.DirectionFailure.EMPTY_EXPRESSION


# ------------------------------------------------------------- O: no retries


def test_exactly_one_pass1_one_pass2_on_success():
    ws = cd.empty_working_set()
    pass1 = counting_callable(raw_act(cd.ASK_HUMAN))
    pass2 = counting_callable({"expression": "So, what do you think about this?"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert failure is None
    assert pass1.calls["n"] == 1
    assert pass2.calls["n"] == 1
    assert p1 == 1 and p2 == 1
    assert result["act"] == cd.ASK_HUMAN
    assert result["expression"] == "So, what do you think about this?"


# ============================================== OWC7-S1: directional ownership


# ---------------------------------------------------------- P: initialization


def test_fresh_working_set_direction_owner_unknown():
    ws = cd.empty_working_set()
    assert ws["direction_owner"] == cd.DIRECTION_UNKNOWN


# --------------------------------------------------------------- Q: requests


def test_request_alone_never_changes_owner_human_plus_request_clark():
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_HUMAN
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, cd.REQUEST_CLARK, False)
    assert resolution["direction_owner_after"] == cd.DIRECTION_HUMAN  # unchanged


def test_request_alone_never_changes_owner_clark_plus_request_human():
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_HUMAN, False)
    assert resolution["direction_owner_after"] == cd.DIRECTION_CLARK  # unchanged


def test_request_alone_never_changes_owner_when_unknown():
    for request in (cd.REQUEST_NONE, cd.REQUEST_HUMAN, cd.REQUEST_CLARK):
        resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, request, False)
        assert resolution["direction_owner_after"] == cd.DIRECTION_UNKNOWN  # unchanged


def test_request_none_owner_unchanged():
    for owner in (cd.DIRECTION_HUMAN, cd.DIRECTION_CLARK, cd.DIRECTION_UNKNOWN):
        resolution = cd.resolve_clark_direction_control(owner, cd.REQUEST_NONE, False)
        assert resolution["direction_owner_after"] == owner


# ---------------------------------------------------------- R: relinquishment


def test_clark_relinquish_from_clark_resolves_to_unknown_not_human():
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_NONE, True)
    assert resolution["direction_owner_after"] == cd.DIRECTION_UNKNOWN
    assert resolution["direction_owner_after"] != cd.DIRECTION_HUMAN


def test_relinquish_while_human_owns_fails_closed_cross_validation():
    validated = raw_act(cd.DEVELOP_CURRENT, relinquish_direction=True)
    failure = cd.cross_validate_direction_control(validated, cd.DIRECTION_HUMAN)
    assert failure == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH


def test_relinquish_while_unknown_fails_closed_cross_validation():
    validated = raw_act(cd.DEVELOP_CURRENT, relinquish_direction=True)
    failure = cd.cross_validate_direction_control(validated, cd.DIRECTION_UNKNOWN)
    assert failure == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH


def test_relinquish_while_clark_owns_no_cross_validation_failure():
    validated = raw_act(cd.DEVELOP_CURRENT, relinquish_direction=True)
    failure = cd.cross_validate_direction_control(validated, cd.DIRECTION_CLARK)
    assert failure is None


def test_resolve_clark_direction_control_defensively_raises_on_unauthorized_call():
    # Direct-call defensive invariant: even if a caller skipped cross-
    # validation, resolve_clark_direction_control() itself refuses to
    # silently miscompute an unauthorized relinquishment.
    raised = None
    try:
        cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, cd.REQUEST_NONE, True)
    except ValueError as exc:
        raised = exc
    assert raised is not None


# ------------------------------------------------------------ S: human control


def test_authenticated_human_control_sets_clark():
    new_owner, failure = cd.apply_human_direction_control(
        CANONICAL_HUMAN_ACTOR_ID, cd.DIRECTION_UNKNOWN, cd.DIRECTION_CLARK, CANONICAL_HUMAN_ACTOR_ID,
    )
    assert failure is None
    assert new_owner == cd.DIRECTION_CLARK


def test_authenticated_human_control_sets_human():
    new_owner, failure = cd.apply_human_direction_control(
        CANONICAL_HUMAN_ACTOR_ID, cd.DIRECTION_CLARK, cd.DIRECTION_HUMAN, CANONICAL_HUMAN_ACTOR_ID,
    )
    assert failure is None
    assert new_owner == cd.DIRECTION_HUMAN


def test_unauthorized_source_fails_closed():
    for bad_source in (None, "", "clark-actor-not-human", ARBITRARY_OPAQUE_HUMAN_ID, 42, []):
        new_owner, failure = cd.apply_human_direction_control(
            bad_source, cd.DIRECTION_UNKNOWN, cd.DIRECTION_HUMAN, CANONICAL_HUMAN_ACTOR_ID,
        )
        assert new_owner is None
        assert failure == cd.DirectionFailure.UNAUTHORIZED_HUMAN_CONTROL, f"source={bad_source!r}"


def test_invalid_target_fails_closed():
    for bad_target in ("shared", "nonsense", None, 42):
        new_owner, failure = cd.apply_human_direction_control(
            CANONICAL_HUMAN_ACTOR_ID, cd.DIRECTION_UNKNOWN, bad_target, CANONICAL_HUMAN_ACTOR_ID,
        )
        assert new_owner is None
        assert failure == cd.DirectionFailure.INVALID_DIRECTION_TARGET, f"target={bad_target!r}"


def test_human_control_works_regardless_of_arbitrary_opaque_canonical_id():
    # Proves no special-casing of one production ID -- any canonical id
    # works the same mechanical way, matching the project-wide
    # opaque-ID-survives-unchanged discipline.
    new_owner, failure = cd.apply_human_direction_control(
        ARBITRARY_OPAQUE_HUMAN_ID, cd.DIRECTION_UNKNOWN, cd.DIRECTION_HUMAN, ARBITRARY_OPAQUE_HUMAN_ID,
    )
    assert failure is None
    assert new_owner == cd.DIRECTION_HUMAN


def test_clark_model_output_path_never_reaches_human_control_function():
    # Source-level proof: apply_human_direction_control is never called
    # from run_typed_conversation_turn() or resolve_clark_direction_
    # control() -- the Clark-side pathway has no route to it at all.
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    import re
    run_turn_body = source[source.index("def run_typed_conversation_turn"):source.index("# ------------------------------------------------ session-scoped state")]
    assert "apply_human_direction_control" not in run_turn_body
    resolve_clark_body = source[source.index("def resolve_clark_direction_control"):source.index("def apply_human_direction_control")]
    assert "apply_human_direction_control" not in resolve_clark_body


# ---------------------------------------------------- T: preservation invariant


def test_question_containing_expression_cannot_alter_owner():
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_CLARK
    pass1 = counting_callable(raw_act(cd.DEVELOP_CURRENT))
    pass2 = counting_callable({"expression": "What do you think about this? Where should we go?"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert failure is None
    assert new_ws["direction_owner"] == cd.DIRECTION_CLARK  # unchanged by question-shaped expression


def test_answer_containing_expression_cannot_alter_owner():
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_HUMAN
    pass1 = counting_callable(raw_act(cd.ASK_HUMAN))
    pass2 = counting_callable({"expression": "Sure, here is a direct answer to that."})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert failure is None
    assert new_ws["direction_owner"] == cd.DIRECTION_HUMAN  # unchanged


def test_conversation_act_alone_cannot_alter_owner():
    for act in (cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN, cd.YIELD_DIRECTION, cd.PAUSE_THREAD, cd.CLOSE_THREAD):
        ws = cd.empty_working_set()
        ws["direction_owner"] = cd.DIRECTION_HUMAN
        validated = raw_act(act, thread="x" if act in (cd.SHIFT_TOPIC,) else "")
        resolution = cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, cd.REQUEST_NONE, False)
        assert resolution["direction_owner_after"] == cd.DIRECTION_HUMAN, f"act={act}"


def test_active_thread_origin_cannot_alter_owner():
    ws = ws_with_active_thread(thread="pattern recognition", origin=cd.ORIGIN_CLARK)
    ws["direction_owner"] = cd.DIRECTION_HUMAN
    new_ws = cd.apply_conversation_act(ws, raw_act(cd.SHIFT_TOPIC, thread="new topic"))
    assert new_ws["active_thread_origin"] == cd.ORIGIN_CLARK  # topic-origin changes
    assert "direction_owner" not in new_ws or new_ws.get("direction_owner") == cd.DIRECTION_HUMAN  # apply_conversation_act never touches it


def test_direction_request_alone_cannot_alter_owner():
    for request in (cd.REQUEST_NONE, cd.REQUEST_HUMAN, cd.REQUEST_CLARK):
        resolution = cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, request, False)
        assert resolution["direction_owner_after"] == cd.DIRECTION_HUMAN


def test_active_thread_origin_and_direction_owner_fully_independent():
    # Every combination is mechanically legal (OWC7-D1 section 9's
    # explicit example: active_thread_origin=clark, direction_owner=human).
    ws = ws_with_active_thread(thread="pattern recognition", origin=cd.ORIGIN_CLARK)
    ws["direction_owner"] = cd.DIRECTION_HUMAN
    assert ws["active_thread_origin"] == cd.ORIGIN_CLARK
    assert ws["direction_owner"] == cd.DIRECTION_HUMAN  # legal, no contradiction raised anywhere


# --------------------------------------------------------------- U: Pass 2


def test_pass2_receives_committed_direction_owner():
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_CLARK
    pass1 = counting_callable(raw_act(cd.ASK_HUMAN, direction_request=cd.REQUEST_NONE))
    pass2 = counting_callable({"expression": "What would you like to explore next?"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert failure is None
    assert result["direction_owner_before"] == cd.DIRECTION_CLARK
    assert result["direction_owner_after"] == cd.DIRECTION_CLARK

    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_NONE, False)
    pass2_iface = cd.build_pass2_interface("sys", [], "cur", raw_act(cd.ASK_HUMAN), new_ws, direction_resolution)
    assert pass2_iface["direction_resolution"]["direction_owner_after"] == cd.DIRECTION_CLARK


def test_pass2_receives_direction_request():
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_HUMAN, False)
    pass2_iface = cd.build_pass2_interface(
        "sys", [], "cur", raw_act(cd.DEVELOP_CURRENT, direction_request=cd.REQUEST_HUMAN),
        cd.empty_working_set(), direction_resolution,
    )
    assert pass2_iface["direction_resolution"]["direction_request"] == cd.REQUEST_HUMAN
    assert pass2_iface["typed_act"]["direction_request"] == cd.REQUEST_HUMAN


def test_pass2_receives_relinquishment_consequence():
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_NONE, True)
    assert direction_resolution["direction_owner_after"] == cd.DIRECTION_UNKNOWN
    pass2_iface = cd.build_pass2_interface(
        "sys", [], "cur", raw_act(cd.YIELD_DIRECTION, relinquish_direction=True),
        cd.empty_working_set(), direction_resolution,
    )
    assert pass2_iface["direction_resolution"]["relinquish_direction"] is True
    assert pass2_iface["direction_resolution"]["direction_owner_after"] == cd.DIRECTION_UNKNOWN


def test_pass1_free_form_content_absent_from_pass2_input():
    # OWC7-S1: there is no `content` key anywhere in typed_act,
    # direction_resolution, or working_set passed to Pass 2.
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_NONE, False)
    pass2_iface = cd.build_pass2_interface(
        "sys", [], "cur", raw_act(cd.DEVELOP_CURRENT), cd.empty_working_set(), direction_resolution,
    )
    assert "content" not in pass2_iface["typed_act"]
    assert "content" not in pass2_iface["direction_resolution"]
    assert "content" not in pass2_iface["working_set"]


def test_describe_functions_are_host_grounded_not_phenomenological():
    for owner in cd.VALID_DIRECTION_OWNERS:
        text = cd.describe_direction_owner(owner)
        assert isinstance(text, str) and len(text) > 0
        for forbidden in ("desire", "want", "feel", "prefer", "wish"):
            assert forbidden not in text.lower()
    for request in cd.VALID_DIRECTION_REQUESTS:
        text = cd.describe_direction_request(request)
        assert isinstance(text, str) and len(text) > 0
    assert cd.describe_relinquishment(False) is None
    assert isinstance(cd.describe_relinquishment(True), str)


# --------------------------------------------------------------- V: trace/failure


def test_malformed_direction_request_fails_before_pass2():
    ws = cd.empty_working_set()
    pass1 = counting_callable({"act": cd.DEVELOP_CURRENT, "thread": "", "direction_request": "bogus", "relinquish_direction": False})
    pass2 = counting_callable({"expression": "unreachable"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert result is None
    assert failure == cd.DirectionFailure.INVALID_DIRECTION_REQUEST
    assert p2 == 0
    assert pass2.calls["n"] == 0


def test_no_retry_or_fallback_changes_ownership_fields():
    # run_typed_conversation_turn() calls pass1_callable/pass2_callable
    # exactly once each on every path -- proven generically by the
    # existing "exactly one" test above; this test additionally proves
    # a failed turn returns the ORIGINAL working_set object's owner
    # value unchanged (no silent coercion to a default).
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_HUMAN
    pass1 = counting_callable({"act": cd.DEVELOP_CURRENT, "thread": "", "direction_request": "bogus", "relinquish_direction": False})
    pass2 = counting_callable({"expression": "unreachable"})
    result, failure, new_ws, p1, p2 = cd.run_typed_conversation_turn("system", [], "current", ws, pass1, pass2)
    assert new_ws["direction_owner"] == cd.DIRECTION_HUMAN


# ------------------------------------------------------------------ misc --


def test_no_prepare_or_model_dependency():
    # Only stdlib `json` (for parsing raw Pass-1/Pass-2 output) is imported -- no
    # orchestration/ollama/DB/Kardia import. The shared expression seam's validator
    # (validate_plain_reply) needs no other module; outward_expression is imported only by
    # the downstream callers that re-verify an already-extracted expression.
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    assert "ollama" not in source.lower()
    import ast
    tree = ast.parse(source)
    module_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.append(node.module)
    assert sorted(module_names) == ["json"], f"unexpected imports: {module_names}"
    oe_src = open(os.path.join(ANAXI_FINAL, "outward_expression.py"), encoding="utf-8").read()
    assert "import " not in oe_src.replace("Pure; never", "")   # the shared predicate imports nothing


def test_kardia_hippocampus_api2_noninterference():
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in (
        "active_kardia", "get_current_kardia", "hippocampus_store", "hippocampus_retrieval",
        "sync_hippocampus", "api1_control_plane", "api2_control_plane", "api2_supervisor",
        "sqlite3", "protected_decision",
    ):
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


# ============================================ OWC5-P3: multi-intent preservation


def test_pass2_instruction_contains_preservation_invariant():
    text = cd.PASS2_TASK_INSTRUCTION
    assert "distinct, actionable, response-seeking item" in text
    assert "you decide the actual answer, never the host" in text
    assert "rhetorical, quoted, hypothetical, or already-answered" in text
    assert "Do not imply that directional ownership changed" in text  # original invariant unweakened


def test_pass2_interface_carries_invariant_for_pause_thread():
    # Test A (spec section 16): pause_thread + explicit offered choice.
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_HUMAN, False)
    message = "I need to step away. Would you like to roam your workspace while I'm gone?"
    pass2_iface = cd.build_pass2_interface(
        "sys", [], message, raw_act(cd.PAUSE_THREAD, direction_request=cd.REQUEST_HUMAN),
        cd.empty_working_set(), direction_resolution,
    )
    assert pass2_iface["typed_act"]["act"] == cd.PAUSE_THREAD
    assert "distinct, actionable, response-seeking item" in pass2_iface["task_instruction"]
    assert pass2_iface["current_user_message"] == message  # full human message still present, unabridged


def test_pass2_interface_does_not_suppress_human_question_under_ask_human():
    # Test B (spec section 16): ask_human + explicit human question --
    # the contract must not state or imply Clark should discard Alex's
    # own question merely because Clark's own act is ask_human.
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_NONE, False)
    pass2_iface = cd.build_pass2_interface(
        "sys", [], "What's on your mind? Also, could you check the weather for me?",
        raw_act(cd.ASK_HUMAN), cd.empty_working_set(), direction_resolution,
    )
    instruction = pass2_iface["task_instruction"]
    for forbidden in ("discard", "ignore the human", "suppress", "do not answer"):
        assert forbidden not in instruction.lower()
    assert "address it too" in instruction


def test_pass2_interface_carries_invariant_for_develop_current():
    # Test C (spec section 16).
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_NONE, False)
    pass2_iface = cd.build_pass2_interface(
        "sys", [], "Let's keep exploring this. Also, should I bring the book tomorrow?",
        raw_act(cd.DEVELOP_CURRENT), cd.empty_working_set(), direction_resolution,
    )
    assert "distinct, actionable, response-seeking item" in pass2_iface["task_instruction"]


def test_pass2_instruction_excludes_rhetorical_and_quoted_material():
    # Tests D/E (spec section 16): the contract itself must explicitly
    # scope the invariant away from rhetorical/quoted/hypothetical/
    # already-answered material -- this proves the CONTRACT/PROMPT
    # shape, not deterministic live model behavior (spec section 17
    # explicitly forbids claiming a prompt guarantees model behavior).
    text = cd.PASS2_TASK_INSTRUCTION
    assert "rhetorical" in text
    assert "quoted" in text
    assert "hypothetical" in text
    assert "already-answered" in text
    assert "requiring a literal answer" in text


def test_pass2_instruction_identical_across_all_acts():
    # Spec section 7: the same general rule applies to every act --
    # deliberately act-independent, not duplicated/varied per act.
    direction_resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_NONE, False)
    instructions = set()
    for act in (cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN, cd.YIELD_DIRECTION, cd.PAUSE_THREAD, cd.CLOSE_THREAD):
        iface = cd.build_pass2_interface("sys", [], "cur", raw_act(act), cd.empty_working_set(), direction_resolution)
        instructions.add(iface["task_instruction"])
    assert len(instructions) == 1  # identical instruction text regardless of act


def test_no_new_typed_field_added_by_owc5p3():
    # Proof for spec sections 8/9: the raw-act and Pass-2 schemas are
    # completely unchanged BY THIS (OWC5-P3) GATE -- this was a prose-
    # only repair. WSP2-P5-P1, a later, separately-authorized gate,
    # deliberately adds background_activity_request -- see
    # test_p5p1_background_activity_request_is_the_only_new_field below
    # for that gate's own equivalent freeze proof.
    assert cd.RAW_ACT_REQUIRED_FIELDS == {"act", "thread", "direction_request", "relinquish_direction"}
    assert cd.PASS2_ALLOWED_FIELDS == {"expression"}
    for forbidden in ("roaming_request", "workspace_request", "pending_question", "required_answer", "secondary_act", "multi_intent"):
        assert forbidden not in cd.RAW_ACT_ALLOWED_FIELDS
        assert forbidden not in cd.PASS2_ALLOWED_FIELDS


def test_no_host_authored_answer_in_instruction_text():
    # Proof for spec section 10: the instruction never supplies or
    # implies a specific answer -- only that Clark himself decides.
    text = cd.PASS2_TASK_INSTRUCTION
    for forbidden in ("answer yes", "answer no", "enable roaming", "decline roaming", "the answer is"):
        assert forbidden not in text.lower()
    assert "you decide the actual answer, never the host" in text


def test_ownership_semantics_unaffected_by_pass2_instruction_change():
    # Test F (spec section 16): no code path in this gate's change
    # touches direction_owner/direction_request/relinquish_direction.
    for act in (cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN, cd.YIELD_DIRECTION, cd.PAUSE_THREAD, cd.CLOSE_THREAD):
        resolution = cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, cd.REQUEST_NONE, False)
        assert resolution["direction_owner_after"] == cd.DIRECTION_HUMAN, f"act={act}"


# ==================================== OWC7-S2: yield/relinquish disambiguation
# Section 8: prompt-contract tests -- PASS1_TASK_INSTRUCTION itself must
# state the owner-dependent relinquish rule literally, and must not
# describe yield_direction in ownership-surrender language.


def test_owc7s2_yield_direction_described_without_ownership_surrender_language():
    text = cd.PASS1_TASK_INSTRUCTION
    # Isolate the sentence(s) describing the yield_direction ACT itself
    # (not the later, separate relinquish_direction rule block) and
    # confirm that description avoids ownership-surrender wording --
    # this is the exact ambiguity the live forensic identified.
    yield_description_start = text.index("yield_direction means")
    relinquish_rule_start = text.index("relinquish_direction is a separate")
    yield_description = text[yield_description_start:relinquish_rule_start]
    for forbidden in ("give up direction", "giving up direction", "relinquish direction", "surrender"):
        assert forbidden not in yield_description.lower()
    assert "does not, by itself, change direction_owner" in yield_description


def test_owc7s2_direction_owner_literal_values_named():
    text = cd.PASS1_TASK_INSTRUCTION
    assert "direction_owner" in text
    assert '"unknown"' in text
    assert '"human"' in text
    assert '"clark"' in text


def test_owc7s2_owner_clark_relinquish_may_be_true():
    text = cd.PASS1_TASK_INSTRUCTION
    assert 'If direction_owner is "clark"' in text
    idx = text.index('If direction_owner is "clark"')
    assert "relinquish_direction MAY be true" in text[idx: idx + 200]


def test_owc7s2_owner_unknown_relinquish_must_be_false():
    text = cd.PASS1_TASK_INSTRUCTION
    assert 'If direction_owner is "unknown"' in text
    idx = text.index('If direction_owner is "unknown"')
    assert "relinquish_direction MUST be false" in text[idx: idx + 200]


def test_owc7s2_owner_human_relinquish_must_be_false():
    text = cd.PASS1_TASK_INSTRUCTION
    assert 'If direction_owner is "human"' in text
    idx = text.index('If direction_owner is "human"')
    assert "relinquish_direction MUST be false" in text[idx: idx + 200]


def test_owc7s2_direction_request_never_changes_ownership_regardless_of_value():
    text = cd.PASS1_TASK_INSTRUCTION
    assert "A direction_request never itself changes direction_owner" in text
    assert 'whether direction_owner is "unknown", "human", or "clark"' in text


def test_owc7s2_relinquish_not_automatic_from_act_or_request():
    text = cd.PASS1_TASK_INSTRUCTION
    assert "does not follow automatically from choosing yield_direction" in text
    assert "does not follow automatically from setting direction_request to request_human" in text


def test_owc7s2_no_behavioral_command_in_pass1_instruction():
    # Section 7: this repair teaches mechanics only -- it must never
    # tell Clark which act/request/relinquishment choice to prefer.
    text = cd.PASS1_TASK_INSTRUCTION.lower()
    for forbidden in (
        "prefer human direction", "avoid yielding", "continue the thread",
        "remain curious", "you should agree", "you should disagree",
        "take initiative", "you should choose", "the correct choice is",
    ):
        assert forbidden not in text


# Section 9: owner/control matrix -- model-free, using the existing
# cross_validate_direction_control()/resolve_clark_direction_control()/
# apply_conversation_act() validators directly. Items 1-4 reproduce the
# exact typed shape from the live production incident (act=
# yield_direction, direction_request=request_human) rather than a
# generic act, so the matrix is directly traceable to what actually
# failed twice in production.


def test_owc7s2_matrix_1_unknown_yield_request_human_no_relinquish_valid():
    validated = raw_act(cd.YIELD_DIRECTION, direction_request=cd.REQUEST_HUMAN, relinquish_direction=False)
    assert cd.cross_validate_direction_control(validated, cd.DIRECTION_UNKNOWN) is None
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_UNKNOWN, cd.REQUEST_HUMAN, False)
    assert resolution["direction_owner_after"] == cd.DIRECTION_UNKNOWN


def test_owc7s2_matrix_2_unknown_yield_request_human_relinquish_true_rejected():
    # The exact live-production failure shape (turns 10/11).
    validated = raw_act(cd.YIELD_DIRECTION, direction_request=cd.REQUEST_HUMAN, relinquish_direction=True)
    failure = cd.cross_validate_direction_control(validated, cd.DIRECTION_UNKNOWN)
    assert failure == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH


def test_owc7s2_matrix_3_human_owns_relinquish_true_rejected():
    validated = raw_act(cd.YIELD_DIRECTION, direction_request=cd.REQUEST_NONE, relinquish_direction=True)
    failure = cd.cross_validate_direction_control(validated, cd.DIRECTION_HUMAN)
    assert failure == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH


def test_owc7s2_matrix_4_clark_owns_relinquish_true_valid():
    validated = raw_act(cd.YIELD_DIRECTION, direction_request=cd.REQUEST_HUMAN, relinquish_direction=True)
    assert cd.cross_validate_direction_control(validated, cd.DIRECTION_CLARK) is None
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_HUMAN, True)
    assert resolution["direction_owner_after"] == cd.DIRECTION_UNKNOWN  # never "human"


def test_owc7s2_matrix_5_request_human_alone_never_mutates_ownership():
    for owner in (cd.DIRECTION_UNKNOWN, cd.DIRECTION_HUMAN, cd.DIRECTION_CLARK):
        resolution = cd.resolve_clark_direction_control(owner, cd.REQUEST_HUMAN, False)
        assert resolution["direction_owner_after"] == owner


def test_owc7s2_matrix_6_yield_direction_alone_never_mutates_ownership():
    for owner in (cd.DIRECTION_UNKNOWN, cd.DIRECTION_HUMAN, cd.DIRECTION_CLARK):
        ws = cd.empty_working_set()
        ws["direction_owner"] = owner
        updated = cd.apply_conversation_act(
            ws, raw_act(cd.YIELD_DIRECTION, direction_request=cd.REQUEST_HUMAN, relinquish_direction=False),
        )
        # apply_conversation_act() deliberately never touches
        # direction_owner at all -- ownership is resolved only via the
        # separate resolve_clark_direction_control() path (matrix 5).
        assert updated["direction_owner"] == owner


def test_owc7s2_typed_schema_unchanged():
    # Section 2: no schema/enum change BY THIS (OWC7-S2) GATE -- prose-
    # only repair. See test_no_new_typed_field_added_by_owc5p3's own
    # updated comment for why RAW_ACT_ALLOWED_FIELDS itself now differs
    # (WSP2-P5-P1, a later, separately-authorized gate) while the
    # REQUIRED subset this gate actually cared about stays identical.
    # Later reference-harness stabilization deliberately added the one
    # use_workspace bridge act; OWC7's required ownership fields and
    # ownership enums remain unchanged.
    assert cd.ALLOWED_ACTS == {
        cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN,
        cd.YIELD_DIRECTION, cd.PAUSE_THREAD, cd.CLOSE_THREAD,
        cd.USE_WORKSPACE,
    }
    assert cd.RAW_ACT_REQUIRED_FIELDS == {"act", "thread", "direction_request", "relinquish_direction"}
    assert cd.VALID_DIRECTION_OWNERS == {cd.DIRECTION_HUMAN, cd.DIRECTION_CLARK, cd.DIRECTION_UNKNOWN}
    assert cd.VALID_DIRECTION_REQUESTS == {cd.REQUEST_NONE, cd.REQUEST_HUMAN, cd.REQUEST_CLARK}


def test_use_workspace_is_a_typed_no_thread_transition():
    ws = ws_with_active_thread(thread="existing thread")
    validated, failure = cd.validate_pass1_conversation_act(raw_act(cd.USE_WORKSPACE))
    assert failure is None
    updated = cd.apply_conversation_act(ws, validated)
    assert updated["last_conversation_act"] == cd.USE_WORKSPACE
    assert updated["active_thread"] == "existing thread"
    assert updated["active_thread_origin"] == cd.ORIGIN_CLARK


# ==================================== WSP2-P5-P1: background_activity_request


def test_p5p1_field_defaults_to_none_when_absent():
    # Section 6: additive/optional -- every pre-existing caller that
    # never supplies this field keeps validating exactly as before.
    validated, failure = cd.validate_pass1_conversation_act(raw_act(cd.DEVELOP_CURRENT))
    assert failure is None
    assert validated["background_activity_request"] == cd.BACKGROUND_ACTIVITY_REQUEST_NONE


def test_p5p1_field_accepts_resume_own_pause():
    validated, failure = cd.validate_pass1_conversation_act(
        {**raw_act(cd.DEVELOP_CURRENT), "background_activity_request": cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE}
    )
    assert failure is None
    assert validated["background_activity_request"] == cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE


def test_p5p1_field_rejects_unrecognized_value():
    validated, failure = cd.validate_pass1_conversation_act(
        {**raw_act(cd.DEVELOP_CURRENT), "background_activity_request": "start_again"}
    )
    assert validated is None
    assert failure == cd.DirectionFailure.INVALID_BACKGROUND_ACTIVITY_REQUEST


def test_p5p1_field_independent_of_act_and_direction_fields():
    # Section 7: independent of conversation act, direction_owner,
    # direction_request, relinquish_direction, active_thread, topic
    # choice -- proven directly: every combination of act/direction
    # fields validates the SAME regardless of this field's value, and
    # vice versa.
    for act in (cd.DEVELOP_CURRENT, cd.SHIFT_TOPIC, cd.ASK_HUMAN, cd.YIELD_DIRECTION, cd.PAUSE_THREAD, cd.CLOSE_THREAD):
        for bg_request in (cd.BACKGROUND_ACTIVITY_REQUEST_NONE, cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE):
            validated, failure = cd.validate_pass1_conversation_act(
                {**raw_act(act), "background_activity_request": bg_request}
            )
            assert failure is None
            assert validated["act"] == act
            assert validated["background_activity_request"] == bg_request


def test_p5p1_field_does_not_affect_direction_ownership_resolution():
    # Section 7/21E: direction_owner resolution reads only act/
    # direction_request/relinquish_direction -- resume_own_pause must
    # never be treated as taking direction, relinquishing it, or
    # requesting it.
    for bg_request in (cd.BACKGROUND_ACTIVITY_REQUEST_NONE, cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE):
        raw = {**raw_act(cd.DEVELOP_CURRENT, direction_request=cd.REQUEST_NONE, relinquish_direction=False),
               "background_activity_request": bg_request}
        validated, failure = cd.validate_pass1_conversation_act(raw)
        assert failure is None
        resolution = cd.resolve_clark_direction_control(cd.DIRECTION_HUMAN, validated["direction_request"], validated["relinquish_direction"])
        assert resolution["direction_owner_after"] == cd.DIRECTION_HUMAN  # unchanged either way


def test_p5p1_ordinary_prose_has_no_pathway_to_this_field():
    # Section 20B: Pass-2 (the ONLY place ordinary Clark prose is ever
    # produced/validated) has no such field at all -- structurally
    # impossible for free-form text to carry or trigger a self-resume.
    assert "background_activity_request" not in cd.PASS2_ALLOWED_FIELDS
    validated2, failure2 = cd.validate_pass2_expression({"expression": "I would like to resume background activity now."})
    assert failure2 is None
    assert set(validated2.keys()) == {"expression"}  # prose text only -- no control field exists to smuggle this into


def test_p5p1_instruction_text_names_the_field_explicitly():
    # Section 6: the model must select an explicit typed act -- the
    # instruction names the field and its two legal values directly,
    # rather than relying on inference.
    text = cd.PASS1_TASK_INSTRUCTION
    assert "background_activity_request" in text
    assert "resume_own_pause" in text


ALL_TESTS = [
    test_task_mode_pathway_not_invoked_anywhere,
    test_develop_current_retains_active_thread,
    test_shift_topic_establishes_supplied_thread_origin_clark,
    test_ask_human_does_not_automatically_transfer_direction,
    test_only_yield_direction_records_deliberate_handoff,
    test_pause_thread_moves_active_to_open_threads,
    test_pause_with_no_active_thread_is_a_noop_on_open_threads,
    test_close_thread_clears_without_appending_to_open_threads,
    test_open_thread_bound_deterministic_oldest_drop,
    test_working_set_functions_are_pure_no_cross_call_leakage,
    test_session_state_holder_reset_gives_fresh_empty_set,
    test_owc2_dialogue_window_passed_through_unchanged,
    test_owc4_aesthetic_module_untouched,
    test_expression_wording_never_changes_typed_act,
    test_malformed_pass1_fails_closed_no_fabricated_act,
    test_malformed_pass1_json_and_extra_fields,
    test_content_field_no_longer_accepted,
    test_pass2_failure_leaves_act_and_state_observable_no_reselection,
    test_pass2_empty_expression_rejected,
    test_exactly_one_pass1_one_pass2_on_success,
    # OWC7-S1
    test_fresh_working_set_direction_owner_unknown,
    test_request_alone_never_changes_owner_human_plus_request_clark,
    test_request_alone_never_changes_owner_clark_plus_request_human,
    test_request_alone_never_changes_owner_when_unknown,
    test_request_none_owner_unchanged,
    test_clark_relinquish_from_clark_resolves_to_unknown_not_human,
    test_relinquish_while_human_owns_fails_closed_cross_validation,
    test_relinquish_while_unknown_fails_closed_cross_validation,
    test_relinquish_while_clark_owns_no_cross_validation_failure,
    test_resolve_clark_direction_control_defensively_raises_on_unauthorized_call,
    test_authenticated_human_control_sets_clark,
    test_authenticated_human_control_sets_human,
    test_unauthorized_source_fails_closed,
    test_invalid_target_fails_closed,
    test_human_control_works_regardless_of_arbitrary_opaque_canonical_id,
    test_clark_model_output_path_never_reaches_human_control_function,
    test_question_containing_expression_cannot_alter_owner,
    test_answer_containing_expression_cannot_alter_owner,
    test_conversation_act_alone_cannot_alter_owner,
    test_active_thread_origin_cannot_alter_owner,
    test_direction_request_alone_cannot_alter_owner,
    test_active_thread_origin_and_direction_owner_fully_independent,
    test_pass2_receives_committed_direction_owner,
    test_pass2_receives_direction_request,
    test_pass2_receives_relinquishment_consequence,
    test_pass1_free_form_content_absent_from_pass2_input,
    test_describe_functions_are_host_grounded_not_phenomenological,
    test_malformed_direction_request_fails_before_pass2,
    test_no_retry_or_fallback_changes_ownership_fields,
    test_no_prepare_or_model_dependency,
    test_kardia_hippocampus_api2_noninterference,
    test_api1_api2_source_hashes_unchanged,
    # OWC5-P3
    test_pass2_instruction_contains_preservation_invariant,
    test_pass2_interface_carries_invariant_for_pause_thread,
    test_pass2_interface_does_not_suppress_human_question_under_ask_human,
    test_pass2_interface_carries_invariant_for_develop_current,
    test_pass2_instruction_excludes_rhetorical_and_quoted_material,
    test_pass2_instruction_identical_across_all_acts,
    test_no_new_typed_field_added_by_owc5p3,
    test_no_host_authored_answer_in_instruction_text,
    test_ownership_semantics_unaffected_by_pass2_instruction_change,
    # OWC7-S2
    test_owc7s2_yield_direction_described_without_ownership_surrender_language,
    test_owc7s2_direction_owner_literal_values_named,
    test_owc7s2_owner_clark_relinquish_may_be_true,
    test_owc7s2_owner_unknown_relinquish_must_be_false,
    test_owc7s2_owner_human_relinquish_must_be_false,
    test_owc7s2_direction_request_never_changes_ownership_regardless_of_value,
    test_owc7s2_relinquish_not_automatic_from_act_or_request,
    test_owc7s2_no_behavioral_command_in_pass1_instruction,
    test_owc7s2_matrix_1_unknown_yield_request_human_no_relinquish_valid,
    test_owc7s2_matrix_2_unknown_yield_request_human_relinquish_true_rejected,
    test_owc7s2_matrix_3_human_owns_relinquish_true_rejected,
    test_owc7s2_matrix_4_clark_owns_relinquish_true_valid,
    test_owc7s2_matrix_5_request_human_alone_never_mutates_ownership,
    test_owc7s2_matrix_6_yield_direction_alone_never_mutates_ownership,
    test_owc7s2_typed_schema_unchanged,
    # WSP2-P5-P1
    test_p5p1_field_defaults_to_none_when_absent,
    test_p5p1_field_accepts_resume_own_pause,
    test_p5p1_field_rejects_unrecognized_value,
    test_p5p1_field_independent_of_act_and_direction_fields,
    test_p5p1_field_does_not_affect_direction_ownership_resolution,
    test_p5p1_ordinary_prose_has_no_pathway_to_this_field,
    test_p5p1_instruction_text_names_the_field_explicitly,
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
