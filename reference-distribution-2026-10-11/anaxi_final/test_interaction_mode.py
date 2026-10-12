"""OWC3-S1 acceptance tests. Zero real prepare_context()/model calls.
Pure unit tests for interaction_mode.py plus module-level session-
scoping tests for llama_anaxi.py's _resolve_interaction_mode(). No
live DB touched -- these tests import llama_anaxi (safe, side-effect-
free at import time, already verified by prior gates) but never call
run_waking_turn(), so no staging/canonical write path is exercised at
all here.
"""
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import interaction_mode as im
import llama_anaxi
from session_dialogue_window import insert_dialogue_window


def _system_and_user(system_content="kardia + memory", user_content="current prompt"):
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------- A: default ----


def test_task_mode_leaves_messages_unchanged():
    messages = _system_and_user()
    result = im.apply_interaction_mode(messages, im.TASK)
    assert result is messages  # same object, not even a copy
    assert result == _system_and_user()


# ---------------------------------------------- B: explicit conversation --


def test_conversation_mode_clause_present_exactly_once():
    messages = _system_and_user()
    result = im.apply_interaction_mode(messages, im.CONVERSATION)
    assert result is not messages
    assert messages == _system_and_user()  # original untouched
    system_content = result[0]["content"]
    assert system_content.count(im.CONVERSATION_MODE_CLAUSE) == 1
    assert system_content.startswith("kardia + memory")
    assert result[1] == {"role": "user", "content": "current prompt"}
    # OWC6-P1: updated to the post-patch clause text (anti-handoff
    # language removed -- see test_conversation_mode_clause_anti_
    # handoff_language_removed() below for the dedicated MUST/MUST-NOT
    # regression proving exactly what changed and why).
    assert im.CONVERSATION_MODE_CLAUSE == (
        "Interaction mode: open conversation.\n\n"
        "No task is pending or implied by this mode. You do not need to "
        "search for a service request.\n\n"
        "You may choose and develop a topic, continue a line of thought, "
        "make an observation, follow an association available from the "
        "conversation, disagree, ask a question when genuinely useful, or "
        "allow a thought to remain open.\n\n"
        "If genuine clarification is required to understand what the "
        "human means, ask for it."
    )


def test_conversation_mode_clause_anti_handoff_language_removed():
    """OWC6-P1: mechanical regression proving the production clause
    matches the OWC6-S2/S3 experimentally-validated clause-absent
    wording exactly -- MUST contain the patched sentence, MUST NOT
    contain either removed anti-handoff statement, and the clause must
    be byte-for-byte the patched text (not re-authored from memory)."""
    clause = im.CONVERSATION_MODE_CLAUSE
    assert "No task is pending or implied by this mode. You do not need to search for a service request." in clause
    assert "return conversational direction to the human" not in clause
    assert "hand the next choice back to the human" not in clause
    assert "You do not need to end each response with a question" not in clause
    # Byte-for-byte equal to the exact wording OWC6-S2/S3 validated
    # (constructed independently here, not imported from the
    # scratchpad experiment harness, which is inappropriate for a
    # production test to depend on).
    expected = (
        "Interaction mode: open conversation.\n\n"
        "No task is pending or implied by this mode. You do not need to "
        "search for a service request.\n\n"
        "You may choose and develop a topic, continue a line of thought, "
        "make an observation, follow an association available from the "
        "conversation, disagree, ask a question when genuinely useful, or "
        "allow a thought to remain open.\n\n"
        "If genuine clarification is required to understand what the "
        "human means, ask for it."
    )
    assert clause == expected


def test_conversation_clause_semantics_no_forced_phenotype():
    forbidden = ("always choose the topic", "never ask", "always disagree", "always volunteer")
    lowered = im.CONVERSATION_MODE_CLAUSE.lower()
    for phrase in forbidden:
        assert phrase not in lowered
    assert "you may" in lowered  # permits, does not compel


# ------------------------------------------------- C: no prose inference --


def test_no_prose_inference_task_mode_ignores_conversational_wording():
    messages = _system_and_user(user_content="I don't have a task. Let's just talk. What's on your mind?")
    result = im.apply_interaction_mode(messages, im.TASK)
    assert im.CONVERSATION_MODE_CLAUSE not in result[0]["content"]
    assert result == messages


def test_no_prose_inference_conversation_mode_ignores_task_wording():
    messages = _system_and_user(user_content="Please fix the bug in module X and run the tests.")
    result = im.apply_interaction_mode(messages, im.CONVERSATION)
    assert im.CONVERSATION_MODE_CLAUSE in result[0]["content"]  # clause present regardless of prompt content


def test_invariant_to_prompt_wording_generally():
    for user_content in ("hello", "Let's just talk.", "Please complete this task.", ""):
        task_result = im.apply_interaction_mode(_system_and_user(user_content=user_content), im.TASK)
        assert im.CONVERSATION_MODE_CLAUSE not in task_result[0]["content"]
        conv_result = im.apply_interaction_mode(_system_and_user(user_content=user_content), im.CONVERSATION)
        assert im.CONVERSATION_MODE_CLAUSE in conv_result[0]["content"]


# ------------------------------------------------- D: session isolation --


def test_session_scoped_state_sticky_within_session_resets_across_sessions():
    # Simulates one session: explicit selection is remembered for
    # subsequent implicit (interaction_mode=None) calls within it --
    # this IS session-scoped persistence, not a bug, since one process
    # run == one session (mirrors _get_native_session()).
    llama_anaxi._interaction_mode_state["mode"] = None
    assert llama_anaxi._resolve_interaction_mode(None) == im.TASK
    assert llama_anaxi._resolve_interaction_mode(im.CONVERSATION) == im.CONVERSATION
    assert llama_anaxi._resolve_interaction_mode(None) == im.CONVERSATION  # sticky within this session

    # A fresh session (a real new process would start with fresh module
    # state) never inherits the previous session's mode -- simulated
    # here by resetting the state dict exactly as a new process would
    # naturally have it.
    llama_anaxi._interaction_mode_state["mode"] = None
    assert llama_anaxi._resolve_interaction_mode(None) == im.TASK


def test_invalid_mode_rejected():
    raised = None
    try:
        llama_anaxi._resolve_interaction_mode("reflection")
    except ValueError as exc:
        raised = exc
    assert raised is not None
    raised2 = None
    try:
        im.apply_interaction_mode(_system_and_user(), "reflection")
    except ValueError as exc:
        raised2 = exc
    assert raised2 is not None
    llama_anaxi._interaction_mode_state["mode"] = None  # leave clean for later tests


# -------------------------------------------- E: dialogue-window compat --


def test_dialogue_window_preservation_exact_order_and_roles():
    prepared_messages = _system_and_user(user_content="Yeah? What about it?")
    with_mode = im.apply_interaction_mode(prepared_messages, im.CONVERSATION)
    dialogue_window = [
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Human agency."},
    ]
    final = insert_dialogue_window(with_mode, dialogue_window)
    assert final == [
        {"role": "system", "content": "kardia + memory\n\n" + im.CONVERSATION_MODE_CLAUSE},
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Human agency."},
        {"role": "user", "content": "Yeah? What about it?"},
    ]


# --------------------------------------------- F: current prompt once ----


def test_current_prompt_exactly_once_with_mode_and_window():
    prepared_messages = _system_and_user(user_content="current prompt text")
    with_mode = im.apply_interaction_mode(prepared_messages, im.CONVERSATION)
    dialogue_window = [
        {"role": "user", "content": "prior prompt"},
        {"role": "assistant", "content": "prior reply"},
    ]
    final = insert_dialogue_window(with_mode, dialogue_window)
    occurrences = sum(1 for m in final if m["content"] == "current prompt text")
    assert occurrences == 1
    assert final[-1] == {"role": "user", "content": "current prompt text"}


# --------------------------------------- G/H: no prepare/model dependency


def test_no_prepare_context_or_model_dependency():
    # OWC4-S1 added a docstring reference to "prepare_context()" in
    # prose (explaining a parameter's provenance) -- not a call. The
    # meaningful, non-fragile check is zero imports (so nothing in this
    # module can reach orchestration.py or ollama at all) plus the
    # literal absence of "ollama".
    import ast
    with open(os.path.join(ANAXI_FINAL, "interaction_mode.py"), encoding="utf-8") as f:
        source = f.read()
    assert "ollama" not in source.lower()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom))


# ------------------------------------------------------- misc noninterference


def test_kardia_hippocampus_noninterference():
    with open(os.path.join(ANAXI_FINAL, "interaction_mode.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("active_kardia", "get_current_kardia", "hippocampus_store", "hippocampus_retrieval", "sync_hippocampus"):
        assert forbidden not in source


def test_llama_anaxi_wiring_present_and_task_mode_default_preserved():
    import ast
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "from interaction_mode import" in source
    assert "apply_interaction_mode(" in source
    tree = ast.parse(source)
    run = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_waking_turn"
    )
    assert [arg.arg for arg in run.args.args][:3] == ["orch", "prompt", "interaction_mode"]
    interaction_default = run.args.defaults[0]
    assert isinstance(interaction_default, ast.Constant)
    assert interaction_default.value is None
    assert "--conversation" in source


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


ALL_TESTS = [
    test_task_mode_leaves_messages_unchanged,
    test_conversation_mode_clause_present_exactly_once,
    test_conversation_mode_clause_anti_handoff_language_removed,
    test_conversation_clause_semantics_no_forced_phenotype,
    test_no_prose_inference_task_mode_ignores_conversational_wording,
    test_no_prose_inference_conversation_mode_ignores_task_wording,
    test_invariant_to_prompt_wording_generally,
    test_session_scoped_state_sticky_within_session_resets_across_sessions,
    test_invalid_mode_rejected,
    test_dialogue_window_preservation_exact_order_and_roles,
    test_current_prompt_exactly_once_with_mode_and_window,
    test_no_prepare_context_or_model_dependency,
    test_kardia_hippocampus_noninterference,
    test_llama_anaxi_wiring_present_and_task_mode_default_preserved,
    test_api1_api2_source_hashes_unchanged,
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
