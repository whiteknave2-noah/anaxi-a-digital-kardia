"""The shared ordinary expression seam (Mac turns, Caret occasions): one plain chat completion
whose assistant message IS Clark's reply.

Owner law being protected: Clark's outward words are his literal reply -- never a host-shaped
description of one -- and a mechanical failure to obtain a reply is never passed off as his
silence.  Root cause established 2026-09-23 (see conversation_direction SHARED ORDINARY EXPRESSION
SEAM): the grammar-constrained {"expression": ...} object produced demeanor descriptions in 26/28
production-shaped real-model turns and plain chat produced literal replies in 28/28, so the
structural pins below forbid that container (and the retired voluntary markers) from returning.

Countermodels named per assertion: a seam that still passed a `format` grammar would satisfy
"reply persisted" while reproducing the defect -> pinned structurally; a seam that retried a
blank completion could turn one mechanical failure into a hidden second generation -> exactly one
call pinned; a seam that persisted a blank completion as an empty X would impersonate Clark's
silence -> no X pinned and the failure pinned recovery-eligible.
"""
import hashlib
import sqlite3

import pytest

import conversation_direction as cd
import waking_turn_failure_capture as wtfc
from test_web_waking_seam import Seam
from test_caret_inbound_seam import _remote
from test_caret_wake_service import _setup as _caret_setup, _service as _caret_service, _serve as _caret_serve

RETIRED_PROTOCOL_STRINGS = (cd.PASS2_COMPLETION_MARKER, cd.PASS2_EXPRESSION_MARKER)


def _script_pass2(s, responses):
    """Scripted SEQUENCE of ordinary Pass-2 provider responses (a str is a normal completed stop
    with that content; a dict is the full provider response).  Records every ordinary Pass-2 call
    with its `format` argument."""
    inner = s.h.la.ollama.chat
    remaining = list(responses)
    calls = []

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        is_probe = bool(options and options.get("num_predict") == 1)
        is_pass1 = isinstance(format, dict) and "act" in format.get("properties", {})
        import sleep_decision_test_support as sdts
        if not is_probe and not is_pass1 and not sdts.is_sleep_decision(format) and remaining:
            calls.append({"messages": [dict(m) for m in messages], "format": format, "options": dict(options or {})})
            item = remaining.pop(0)
            if isinstance(item, dict):
                return item
            return {"message": {"content": item}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 20}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    s.h.la.ollama.chat = chat
    s.pass2_calls = calls
    return s


def _count(s, sql):
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _prose(s):
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT component_text FROM event_components WHERE component_kind='conversational_prose' ORDER BY rowid")]
    finally:
        conn.close()


def test_the_ordinary_pass2_call_carries_no_grammar_and_no_protocol_marker(monkeypatch, tmp_path):
    s = _script_pass2(Seam(monkeypatch, tmp_path), ["First reply.", "Second reply."])
    s.turn("Hey there.")
    s.turn("Whatcha been up to, eh?")
    assert len(s.pass2_calls) == 2
    for call in s.pass2_calls:
        assert call["format"] is None, "no grammar/JSON container may shape Clark's reply"
        for message in call["messages"]:
            for sentinel in RETIRED_PROTOCOL_STRINGS:
                assert sentinel not in message["content"]
    # the prior reply reaches the next Pass 2 exactly as persisted
    assert {"role": "assistant", "content": "First reply."} in s.pass2_calls[1]["messages"]


def test_the_reply_is_persisted_verbatim_apart_from_surrounding_whitespace(monkeypatch, tmp_path):
    s = _script_pass2(Seam(monkeypatch, tmp_path), ["\n  Good morning to you too.\n(He said: I'm leaving.)  \n"])
    result = s.turn("Good morning. How are you this fine and glorious day?")
    assert result["reply"] == "Good morning to you too.\n(He said: I'm leaving.)"
    assert _prose(s) == [result["reply"]]
    assert len(s.pass2_calls) == 1


def test_json_looking_text_is_not_parsed_or_reinterpreted_on_the_ordinary_path(monkeypatch, tmp_path):
    raw = '{"expression": "Hi."}'
    s = _script_pass2(Seam(monkeypatch, tmp_path), [raw])
    assert s.turn("So what's on your mind?")["reply"] == raw


def test_an_exact_echo_is_not_screened_or_rewritten_by_the_host(monkeypatch, tmp_path):
    """No content classifier exists at this seam: the host never judges Clark's words (the echo
    defect's cause was the retired container, not an absence of screening)."""
    s = _script_pass2(Seam(monkeypatch, tmp_path), ["Hi."])
    assert s.turn("Hi.")["reply"] == "Hi."


def test_a_blank_completion_is_a_mechanical_failure_never_silence_and_never_retried(monkeypatch, tmp_path):
    s = _script_pass2(Seam(monkeypatch, tmp_path), ["   \n", "unreachable"])
    with pytest.raises(s.h.la.ConversationDirectionFailure) as caught:
        s.turn("So what's on your mind?")
    assert (caught.value.stage, caught.value.failure_code) == ("pass2", "EMPTY_EXPRESSION")
    assert len(s.pass2_calls) == 1
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'") == 0
    assert _count(s, "SELECT COUNT(*) FROM event_components WHERE component_kind='clark_reply_choice'") == 0
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1


@pytest.mark.parametrize("code", ["EMPTY_EXPRESSION", "MALFORMED_EXPRESSION", "INCOMPLETE_EXPRESSION_BOUNDARY",
                                  "COMPLETION_LIMIT_REACHED", "INCOMPLETE_MODEL_COMPLETION"])
def test_expression_not_established_is_classified_recovery_eligible(code):
    exc = cd.ConversationDirectionFailure("pass2", code)
    classification, _basis = wtfc._classify_waking_failure(exc, has_x=False)
    assert classification == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"


def test_a_provider_length_stop_fails_before_validation_or_persistence(monkeypatch, tmp_path):
    s = _script_pass2(Seam(monkeypatch, tmp_path), [
        {"message": {"content": "A reply that was cut"}, "prompt_eval_count": 1, "done": True,
         "done_reason": "length", "eval_count": 768}])
    with pytest.raises(s.h.la.ConversationDirectionFailure) as caught:
        s.turn("Tell me something long.")
    assert caught.value.failure_code == "COMPLETION_LIMIT_REACHED"
    assert _prose(s) == []


def test_a_rejected_generation_never_reaches_canonical_history(monkeypatch, tmp_path):
    s = _script_pass2(Seam(monkeypatch, tmp_path), [
        {"message": {"content": "REJECTED-TEXT-MARKER"}, "prompt_eval_count": 1, "done": False,
         "done_reason": None, "eval_count": 5}])
    with pytest.raises(s.h.la.ConversationDirectionFailure):
        s.turn("So what's on your mind?")
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        blob = "\n".join(r[0] or "" for r in conn.execute("SELECT component_text FROM event_components"))
    finally:
        conn.close()
    assert "REJECTED-TEXT-MARKER" not in blob


def test_the_caret_occasion_path_uses_the_same_seam(monkeypatch, tmp_path):
    s = _caret_setup(monkeypatch, tmp_path)
    _script_pass2(s, ["Good morning to you too, Alex."])
    svc = _caret_service(s, tmp_path)
    _caret_serve(s, _remote())
    outcome, _delay = svc.step()
    assert outcome == "delivered"
    assert len(s.pass2_calls) == 1 and s.pass2_calls[0]["format"] is None
    assert svc._attempts == {}


def test_the_calibrated_scaffold_is_bound_to_the_exact_task_instruction():
    from test_owc5_s2_integration import fresh_llama_anaxi
    la = fresh_llama_anaxi()[0]
    assert la._PASS2_SCAFFOLD_CALIBRATION["scaffold_sha256"] == hashlib.sha256(
        cd.PASS2_TASK_INSTRUCTION.encode("utf-8")).hexdigest()
    assert la._PASS2_SCAFFOLD_CALIBRATION["format_mode"] == "unstructured"
    assert la._PASS2_SCAFFOLD_CALIBRATED_COST == 138
