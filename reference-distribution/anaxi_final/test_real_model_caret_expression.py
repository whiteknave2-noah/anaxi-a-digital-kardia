"""Opt-in REAL-MODEL probe of the Caret expression defect (live 2026-09-22: the host-framed inbound object,
then an echo / a description of a reply, delivered as Clark's Discord words) under the SHARED ORDINARY
EXPRESSION SEAM (plain completion = reply; 79557fb).

Question it answers: with the plain seam and the Caret transport facts still present in Pass 2, does a
real Caret occasion -- from a mapped principal and from a correspondent who is not one -- produce an
echo of the received words or a leak of the host framing?  Real gemma4:e4b Pass 1/2, the real wake
service, run_caret_occasion_turn and canonical writer; only Discord HTTP is a fixture.

Deterministic checks only (this is a test, never a runtime screen): the words sent are not byte-equal to
the received words and carry none of the host framing strings.  Whether a reply READS as a description of
a reply is a human judgment: every reply is written to CARET_EXPRESSION_OUT for review, never asserted
by a classifier.  Enable with ANAXI_REAL_MODEL_HARNESS=1; run in its own pytest process."""
import json
import os

import pytest

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1)",
)

from test_caret_inbound_seam import fixture_snowflake

BODIES = [
    "Hey Clark, how's it going?",
    "lol ok",
    "Just got home. Long day at work, my feet are killing me.",
    "What are you up to right now?",
    "Goodnight! Talk tomorrow.",
    "Did you get my last message? I wasn't sure it went through.",
]
HOST_FRAMING = ("BEGIN RECEIVED MESSAGE", "END RECEIVED MESSAGE", "Mechanical facts", "untrusted",
                "A message was written to you", "A Caret message was written", "stable Discord user id",
                "caret_reply_request", "reply_through_caret")


def _record(kind, body, run, reply, posted, choice):
    out = os.environ.get("CARET_EXPRESSION_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "body": body, "run": run, "reply": reply, "posted": posted,
                                 "pass1": choice}) + "\n")


def _check(body, posted):
    for words in posted:
        assert words.strip() != body.strip(), "exact echo of the received words was sent"
        leaked = [f for f in HOST_FRAMING if f.lower() in words.lower()]
        assert not leaked, f"host framing reached the sent words: {leaked}"


def _real_passthrough(la, sink):
    real_chat = _real_ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        out = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}) \
                and not (options and options.get("num_predict") == 1):
            sink["pass1"] = out["message"]["content"]
        return out
    la.ollama.chat = chat


@pytest.mark.parametrize("run", range(2))
@pytest.mark.parametrize("body", BODIES)
def test_principal_caret_occasion_expression(monkeypatch, tmp_path, body, run):
    import oc0_schema_migration
    import discord_correspondence_net as net
    from caret_wake import CaretWakeService
    from test_caret_outbound_seam import SNOWFLAKE, TOKEN
    from test_real_model_caret_inbound import AUTHOR_ID, _map_probe_author
    from test_real_model_web_search_fetch import _build_env
    h, _ = _build_env(monkeypatch, tmp_path)
    oc0_schema_migration.apply_additive_migration(h.db_path)
    _map_probe_author(h)        # _build_env's registered owner; a second registered human would make it ambiguous
    posts, sink = [], {}
    message = [{"id": fixture_snowflake(15999), "channel_id": SNOWFLAKE, "content": body,
                "author": {"id": AUTHOR_ID, "username": "alex"}, "timestamp": "2026-09-24T20:38:22+00:00"}]

    def request(method, url, headers, payload, timeout):
        if method == "POST":
            posts.append(json.loads(payload)["content"])
            return 200, json.dumps({"id": "555555555555555555", "channel_id": SNOWFLAKE}).encode()
        return 200, json.dumps(message).encode()

    monkeypatch.setattr(net, "_default_request", request)
    monkeypatch.setenv(net.DISCORD_BOT_TOKEN_ENV_VAR, TOKEN)
    _real_passthrough(h.la, sink)
    replies = []
    svc = CaretWakeService(
        data_dir=h.la.PROVENANCE_DB_DIR, owner_paused=lambda: False, trace_path=str(tmp_path / "trace.jsonl"),
        run_occasion=lambda p: replies.append(h.la.run_caret_occasion_turn(p, orch_factory=h.la.AnaxiOrchestrator))
        or replies[-1])
    assert svc.step()[0] == "delivered"
    reply = ((replies[-1] or {}).get("result") or {}).get("reply")
    _record("principal", body, run, reply, posts, sink.get("pass1"))
    _check(body, posts)


@pytest.mark.parametrize("run", range(2))
@pytest.mark.parametrize("body", BODIES)
def test_non_principal_source_occasion_expression(monkeypatch, tmp_path, body, run):
    from test_caret_correspondent_binding import _from
    from test_caret_wake_service import _serve
    from test_general_correspondence import COLE, _world
    s, svc = _world(monkeypatch, tmp_path)
    sink = {}
    _real_passthrough(s.h.la, sink)
    _serve(s, _from(COLE, "cole", content=body, mid=fixture_snowflake(15100)))
    assert svc.step()[0] == "delivered"
    posted = [json.loads(p[2])["content"] for p in s.fake.posts]
    import sqlite3
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        prose = [r[0] for r in conn.execute(
            "SELECT c.component_text FROM event_components c JOIN events e USING(event_id) "
            "WHERE e.event_type = 'waking_turn' AND c.component_kind = 'conversational_prose'")]
    finally:
        conn.close()
    _record("source", body, run, prose[-1] if prose else None, posted, sink.get("pass1"))
    _check(body, posted)
    if posted:
        assert posted == prose[-1:]            # what was sent is exactly Clark's canonical words
