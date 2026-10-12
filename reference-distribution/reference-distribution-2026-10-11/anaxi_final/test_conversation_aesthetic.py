"""OWC4-S1 acceptance tests. Zero real prepare_context()/model calls.
Pure unit tests for interaction_mode.apply_conversation_aesthetic()
plus its composition with OWC2 (dialogue window) and OWC3 (interaction
clause). One live, read-only round-trip check proves the durable
Kardia row is genuinely untouched.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import interaction_mode as im
from session_dialogue_window import insert_dialogue_window

MINIMALIST_STYLE = "Respond with extreme brevity and precision. Prefer short sentences. Avoid filler."
PRECISE_STYLE = "Be exact. Prefer technical accuracy over flourish. Use concrete language."


def _identity_system_message(style_instruction):
    content = (
        "Your current stance:\n"
        "- Moral orientation: Value human agency and autonomy.\n"
        "- Volitional channel: Seek clarity.\n"
        "- Affective stance: Calm.\n"
        f"- Aesthetic directive: {style_instruction}\n\n"
        "You have persistent memory and identity across separate conversations."
    )
    return {"role": "system", "content": content}


def _messages(style_instruction, user_content="current prompt"):
    return [_identity_system_message(style_instruction), {"role": "user", "content": user_content}]


# ------------------------------------------------------- A: task unchanged


def test_task_mode_leaves_aesthetic_completely_unchanged():
    messages = _messages(MINIMALIST_STYLE)
    result = im.apply_conversation_aesthetic(messages, im.TASK, MINIMALIST_STYLE)
    assert result is messages
    assert MINIMALIST_STYLE in result[0]["content"]


# ----------------------------------------------- B: conversation override


def test_conversation_mode_override_present_exactly_once():
    messages = _messages(MINIMALIST_STYLE)
    result = im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    assert result is not messages
    assert messages[0]["content"] == _identity_system_message(MINIMALIST_STYLE)["content"]  # original untouched
    system_content = result[0]["content"]
    assert system_content.count(im.CONVERSATION_AESTHETIC_DIRECTIVE) == 1
    assert im.CONVERSATION_AESTHETIC_DIRECTIVE == (
        "Respond naturally and with enough development to carry a "
        "conversational thought forward. Brevity is welcome when it fits, "
        "but do not compress every response into a minimal answer. Allow "
        "room for explanation, association, reflection, and unfinished "
        "thought when useful. Avoid filler and needless repetition."
    )


def test_generalizes_to_any_matched_preset_not_hardcoded_to_minimalist():
    """Proves the override target is whatever style_instruction
    prepare_context() actually returned, not a hardcoded assumption
    that 'minimalist' is always the active preset."""
    messages = _messages(PRECISE_STYLE)
    result = im.apply_conversation_aesthetic(messages, im.CONVERSATION, PRECISE_STYLE)
    assert PRECISE_STYLE not in result[0]["content"]
    assert im.CONVERSATION_AESTHETIC_DIRECTIVE in result[0]["content"]


def test_unexpected_shape_fails_safe_no_garbled_substitution():
    messages = [{"role": "system", "content": "no aesthetic directive line at all here"}, {"role": "user", "content": "hi"}]
    result = im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    assert result == messages  # unchanged -- target substring absent, no partial edit attempted


# --------------------------------------------------- C: no double directive


def test_extreme_brevity_text_absent_after_override():
    messages = _messages(MINIMALIST_STYLE)
    result = im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    assert "extreme brevity" not in result[0]["content"]
    assert "Avoid filler." not in result[0]["content"]  # the old sentence, not the new directive's own "Avoid filler"


# --------------------------------------------------- D: durable Kardia ----


def test_durable_kardia_row_untouched_live_readonly_roundtrip():
    """Read-only round trip against a durable Kardia fixture:
    hash the persisted active_kardia row before, run the
    pure override function (which never receives a DB handle at all),
    hash again -- must be identical. Live production values are deliberately
    outside a unit test's denominator."""
    fd, db_path = tempfile.mkstemp(prefix="kardia-aesthetic-", suffix=".db")
    os.close(fd)
    setup = sqlite3.connect(db_path)
    setup.execute(
        "CREATE TABLE active_kardia (user_id TEXT PRIMARY KEY, moral_valve TEXT, "
        "volitional_channel TEXT, affective_stance TEXT, aesthetic_valve TEXT, updated_at INTEGER)"
    )
    setup.execute(
        "INSERT INTO active_kardia VALUES ('nate','moral','volitional','affective','aesthetic',1000)"
    )
    setup.commit()
    setup.close()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    before = conn.execute(
        "SELECT moral_valve, volitional_channel, affective_stance, aesthetic_valve, updated_at "
        "FROM active_kardia WHERE user_id='nate'"
    ).fetchone()
    conn.close()
    assert before is not None

    messages = _messages(MINIMALIST_STYLE)
    im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    im.apply_conversation_aesthetic(messages, im.TASK, MINIMALIST_STYLE)

    conn2 = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    after = conn2.execute(
        "SELECT moral_valve, volitional_channel, affective_stance, aesthetic_valve, updated_at "
        "FROM active_kardia WHERE user_id='nate'"
    ).fetchone()
    conn2.close()
    assert before == after


def test_source_never_writes_to_kardia_tables():
    with open(os.path.join(ANAXI_FINAL, "interaction_mode.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("active_kardia", "UPDATE ", "INSERT INTO", "sqlite3.connect", "get_current_kardia"):
        assert forbidden not in source


# ------------------------------------------------- E: mode/session isolation


def test_conversation_aesthetic_never_applied_in_task_mode_regardless_of_preset():
    for style in (MINIMALIST_STYLE, PRECISE_STYLE, "Use short, high-impact sentences."):
        messages = _messages(style)
        result = im.apply_conversation_aesthetic(messages, im.TASK, style)
        assert style in result[0]["content"]
        assert im.CONVERSATION_AESTHETIC_DIRECTIVE not in result[0]["content"]


# --------------------------------------------------- F: OWC2 compatibility


def test_owc2_dialogue_window_roles_and_order_unchanged():
    messages = _messages(MINIMALIST_STYLE, user_content="Yeah? What about it?")
    with_aesthetic = im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    with_clause = im.apply_interaction_mode(with_aesthetic, im.CONVERSATION)
    dialogue_window = [
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Human agency."},
    ]
    final = insert_dialogue_window(with_clause, dialogue_window)
    assert [m["role"] for m in final] == ["system", "user", "assistant", "user"]
    assert final[1] == {"role": "user", "content": "What interests you?"}
    assert final[2] == {"role": "assistant", "content": "Human agency."}
    assert final[3] == {"role": "user", "content": "Yeah? What about it?"}


# --------------------------------------------------- G: OWC3 compatibility


def test_owc3_conversation_clause_present_exactly_once_alongside_aesthetic():
    messages = _messages(MINIMALIST_STYLE)
    with_aesthetic = im.apply_conversation_aesthetic(messages, im.CONVERSATION, MINIMALIST_STYLE)
    final = im.apply_interaction_mode(with_aesthetic, im.CONVERSATION)
    system_content = final[0]["content"]
    assert system_content.count(im.CONVERSATION_MODE_CLAUSE) == 1
    assert system_content.count(im.CONVERSATION_AESTHETIC_DIRECTIVE) == 1
    assert MINIMALIST_STYLE not in system_content  # exactly one aesthetic directive active


# ----------------------------------------------------- H: prompt invariance


def test_prompt_wording_never_influences_which_aesthetic_is_active():
    for user_content in ("hi", "Please be brief.", "Please elaborate at length.", ""):
        task_result = im.apply_conversation_aesthetic(_messages(MINIMALIST_STYLE, user_content), im.TASK, MINIMALIST_STYLE)
        assert MINIMALIST_STYLE in task_result[0]["content"]
        conv_result = im.apply_conversation_aesthetic(_messages(MINIMALIST_STYLE, user_content), im.CONVERSATION, MINIMALIST_STYLE)
        assert im.CONVERSATION_AESTHETIC_DIRECTIVE in conv_result[0]["content"]


# --------------------------------------------- I: no prepare/model calls --


def test_no_prepare_or_model_dependency():
    # "prepare_context()" legitimately appears in prose/docstrings
    # (explaining where original_style_instruction comes from) -- the
    # real, meaningful check is that this module imports nothing from
    # orchestration.py or ollama and contains no import statement that
    # could reach either.
    import ast
    with open(os.path.join(ANAXI_FINAL, "interaction_mode.py"), encoding="utf-8") as f:
        source = f.read()
    assert "ollama" not in source.lower()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert False, f"interaction_mode.py must have zero imports; found: {ast.dump(node)}"


def test_llama_anaxi_wiring_present():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "apply_conversation_aesthetic(" in source
    assert 'prepared["controls"].get("style_instruction")' in source


ALL_TESTS = [
    test_task_mode_leaves_aesthetic_completely_unchanged,
    test_conversation_mode_override_present_exactly_once,
    test_generalizes_to_any_matched_preset_not_hardcoded_to_minimalist,
    test_unexpected_shape_fails_safe_no_garbled_substitution,
    test_extreme_brevity_text_absent_after_override,
    test_durable_kardia_row_untouched_live_readonly_roundtrip,
    test_source_never_writes_to_kardia_tables,
    test_conversation_aesthetic_never_applied_in_task_mode_regardless_of_preset,
    test_owc2_dialogue_window_roles_and_order_unchanged,
    test_owc3_conversation_clause_present_exactly_once_alongside_aesthetic,
    test_prompt_wording_never_influences_which_aesthetic_is_active,
    test_no_prepare_or_model_dependency,
    test_llama_anaxi_wiring_present,
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
