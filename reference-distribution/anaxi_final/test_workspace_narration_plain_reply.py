"""Workspace narration uses the SAME shared expression seam as ordinary Pass 2: one plain chat
completion whose text IS Clark's reply -- no JSON envelope requested, no voluntary protocol marker,
no same-occasion retry.

Owner law protected: after a workspace action has already run, a lawful turn must not be lost to a
protocol string the model forgot (the 2026-09-22 live blocker, H 01M33MG39FBZEFSK0JQMNQBW6S, died
MISSING_EXPRESSION_ENVELOPE with real usable narration in hand), and a blank completion is a
mechanical failure -- never Clark's silence and never silently retried into a second generation.
"""
import sqlite3

import pytest

import conversation_direction as cd
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message

REOPEN_ACTION = {"resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": ""}


def _script(h, texts):
    inner = h.la.ollama.chat
    remaining = list(texts)
    calls = []

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        is_probe = bool(options and options.get("num_predict") == 1)
        if format is None and not is_probe and remaining:
            calls.append([dict(m) for m in messages])
            return {"message": {"content": remaining.pop(0)}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 20}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    h.free_text_calls = calls
    return h


def _run(h):
    return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES),
                                interaction_mode="conversation")


def _rows(h, sql):
    conn = sqlite3.connect(f"file:{h.db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_plain_narration_is_the_reply_first_attempt_nothing_typed_back(monkeypatch, tmp_path):
    h = _script(_build(monkeypatch, tmp_path, real_writer=True, action=REOPEN_ACTION),
                ["We left off discussing the persistence model."])
    result = _run(h)
    assert result["reply"] == "We left off discussing the persistence model."
    assert len(h.free_text_calls) == 1
    for message in h.free_text_calls[0]:
        assert cd.PASS2_COMPLETION_MARKER not in message["content"]
        assert cd.PASS2_EXPRESSION_MARKER not in message["content"]
    assert [r[0] for r in _rows(h, "SELECT component_text FROM event_components "
                                   "WHERE component_kind='conversational_prose'")] == [result["reply"]]
    assert _rows(h, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'")[0][0] == 1


def test_a_blank_narration_fails_closed_once_and_is_never_retried(monkeypatch, tmp_path):
    h = _script(_build(monkeypatch, tmp_path, real_writer=True, action=REOPEN_ACTION), ["   \n", "unreachable"])
    with pytest.raises(h.la.ConversationDirectionFailure) as caught:
        _run(h)
    assert "EMPTY_EXPRESSION" in str(caught.value)
    assert len(h.free_text_calls) == 1
    assert _rows(h, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'")[0][0] == 0


def test_a_parenthetical_narration_survives_byte_exact(monkeypatch, tmp_path):
    legit = "(He said: I'm leaving.)"
    h = _script(_build(monkeypatch, tmp_path, real_writer=True, action=REOPEN_ACTION), [legit])
    assert _run(h)["reply"] == legit


def test_an_exact_expression_object_is_still_read_structurally(monkeypatch, tmp_path):
    h = _script(_build(monkeypatch, tmp_path, real_writer=True, action=REOPEN_ACTION),
                ['{"expression": "Structured narration."}'])
    assert _run(h)["reply"] == "Structured narration."
