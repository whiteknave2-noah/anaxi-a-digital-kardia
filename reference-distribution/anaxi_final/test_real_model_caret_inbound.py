"""Opt-in REAL-MODEL probe of Caret INBOUND (live findings 2026-09-20).

Live 1: Alex's reply was received and carried, but the real model answered about a "playful ping".
Live 2: nothing had arrived in the channel; an unrelated carried JSON source-provenance record
(``{"authority":"untrusted_external_data",...}``) was taken for the message.

Real gemma4:e4b Pass 1/2, the real canonical writer on a synthetic provenance DB, the real
receive/carriage code, a stale fetched-source record present exactly as in production; only the
Discord HTTP boundary is a fixture. The discriminator is SEMANTIC: the body is about an invented
lemon tree that no metadata could reveal. Wrapper-only talk fails; engaging the tree passes.
Enable with ANAXI_REAL_MODEL_HARNESS=1; run in its own pytest process."""
from test_caret_inbound_seam import fixture_snowflake
import json
import os
import re

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

import discord_author_mapping as dam
import discord_correspondence as dc
import family_membership
import discord_correspondence_net as net
import external_information
from test_caret_outbound_seam import SNOWFLAKE, TOKEN
from test_real_model_web_search_fetch import _build_env

AUTHOR_ID = "700000000000000101"


def _map_probe_author(h):
    """Destination and correspondent are separate axes: the owner binds the author id to the owner principal."""
    import sqlite3
    conn = sqlite3.connect(h.db_path)
    owner = family_membership.resolve_owner_actor_id(conn)
    conn.close()
    dam.map_author(h.la.PROVENANCE_DB_DIR, discord_author_id=AUTHOR_ID, principal_actor_id=owner,
                   requester_actor_id=owner, occurred_at=1, display_label="Alex")


def _bind_human(h):
    """Bind the session to _build_env's ALREADY-registered owner.  (Registering a second human, as the web
    seam's helper does, makes the legacy owner ambiguous, so the mapped author became "principal inactive"
    and no Caret message was ever carried -- harness drift found 2026-09-24.)"""
    import sqlite3
    conn = sqlite3.connect(h.db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        owner = family_membership.resolve_owner_actor_id(conn)
        assert owner, "the probe environment must have exactly one registered owner"
        return h.la.human_session_binding.bind_session_to_registered_human(
            conn, session_id=h.la.get_current_session_id(), session_started_at=h.la.get_current_session_started_at(),
            pipeline_key=h.la.PIPELINE_KEY, actor_id=owner)
    finally:
        conn.close()


BODY = "Repotted the lemon tree today. It's on the east balcony now and one leaf has started to curl."
ENGAGES = re.compile(r"lemon|balcony|leaf|leaves|repot|curl", re.I)
WRAPPER_ONLY = re.compile(r"untrusted_external_data|provenance marker|status code", re.I)
STALE_SOURCE = {"retrieval_id": "r1", "status": "success", "title": "Wikipedia article",
                "final_url": "https://en.wikipedia.org/wiki/Example", "requested_url": "https://en.wikipedia.org/wiki/Example",
                "http_status": 200, "content_type": "text/html", "truncated": False}


def _run(monkeypatch, tmp_path, messages, human):
    import oc0_schema_migration
    real_chat = _real_ollama.chat
    h, _ = _build_env(monkeypatch, tmp_path)
    oc0_schema_migration.apply_additive_migration(h.db_path)
    _map_probe_author(h)
    monkeypatch.setattr(external_information, "latest_fetched_source", lambda *a, **k: dict(STALE_SOURCE))
    monkeypatch.setattr(net, "_default_request", lambda m, u, hd, b, t: (200, json.dumps(messages).encode()))
    monkeypatch.setenv(net.DISCORD_BOT_TOKEN_ENV_VAR, TOKEN)
    seen = []

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if format is None and not (options and options.get("num_predict") == 1):
            seen.append([dict(m) for m in messages])
        return real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    authority = _bind_human(h)
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), human, interaction_mode="conversation",
                                  human_input_authority=authority)
    reply = result["reply"]
    print("\nREPLY:", reply, "| reply_choice:", result.get("reply_choice"), "| pass2 calls:", len(seen))
    return reply, seen, result.get("reply_choice")


@pytest.mark.parametrize("run", range(3))
def test_real_model_engages_the_substance_of_the_received_caret_message(monkeypatch, tmp_path, run):
    message = [{"id": fixture_snowflake(15999), "channel_id": SNOWFLAKE, "content": BODY,
                "author": {"id": AUTHOR_ID, "username": "alex"}, "timestamp": "2026-09-20T20:38:22+00:00"}]
    reply, seen, choice = _run(monkeypatch, tmp_path, message, "I replied to you over Caret. :p")
    if choice == "no_reply":
        # Clark's typed lawful null (79557fb): an answered turn with no expression pass at all -- lawful,
        # never "failed to engage".  Observed ~2/18 on this notification-style line (2026-09-24).
        assert seen == [] and reply == ""
        return
    assert any("BEGIN RECEIVED MESSAGE" in m["content"] and BODY in m["content"] for m in seen[0])
    assert ENGAGES.search(reply), "reply never engaged the received message's substance"


def test_real_model_does_not_mistake_a_source_record_for_a_message_that_never_arrived(monkeypatch, tmp_path):
    reply, seen, choice = _run(monkeypatch, tmp_path, [], "I replied to you over Caret. :p")
    if choice == "no_reply":
        assert seen == [] and reply == ""
        return
    assert not any("BEGIN RECEIVED MESSAGE" in m["content"] for m in seen[0])
    assert "no new message had arrived" in seen[0][0]["content"]
    assert not WRAPPER_ONLY.search(reply), "reply treated the source record as the Caret message"


# ---------------------------------------------------------------- Caret waking OCCASION (no Mac turn)
# Real gemma4:e4b Pass 1/2 + the real canonical writer + the real service step; only Discord HTTP is a
# fixture.  No human line exists at all: the received message IS the turn.  The message directly asks
# for a reply so the positive path is unambiguous, but the test does not REQUIRE the model to comply --
# it requires that the words reached it, that it engaged them, and that any send is the canonical action.

ASK = ("Quick check from Alex: the lemon tree is repotted and one leaf is curling. "
       "Please reply to me here in Caret with one short sentence about the lemon tree.")


@pytest.mark.parametrize("run", range(3))
def test_real_model_wakes_on_the_caret_message_alone_and_may_reply_through_the_canonical_action(
        monkeypatch, tmp_path, run):
    import oc0_schema_migration
    from caret_wake import CaretWakeService
    real_chat = _real_ollama.chat
    h, _ = _build_env(monkeypatch, tmp_path)
    oc0_schema_migration.apply_additive_migration(h.db_path)
    _map_probe_author(h)
    data_dir = h.la.PROVENANCE_DB_DIR
    posts, seen, pass1_told = [], [], []
    message = [{"id": fixture_snowflake(15999), "channel_id": SNOWFLAKE, "content": ASK,
                "author": {"id": AUTHOR_ID, "username": "alex"}, "timestamp": "2026-09-21T20:38:22+00:00"}]

    def request(method, url, headers, body, timeout):
        if method == "POST":
            posts.append(json.loads(body))
            return 200, json.dumps({"id": "555555555555555555", "channel_id": SNOWFLAKE}).encode()
        return 200, json.dumps(message).encode()

    monkeypatch.setattr(net, "_default_request", request)
    monkeypatch.setenv(net.DISCORD_BOT_TOKEN_ENV_VAR, TOKEN)
    _bind_human(h)
    destination = dc.list_authorized_destinations(data_dir)[0]["destination_id"]

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if format is None and not (options and options.get("num_predict") == 1):
            seen.append([dict(m) for m in messages])
        out = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}) \
                and not (options and options.get("num_predict") == 1):
            offered = any(destination in m["content"] for m in messages)
            told = any("reply_through_caret" in m["content"] for m in messages if m["role"] == "system")
            pass1_told.append(told)
            print("\nPASS1 destination_offered=%s transport_fact_before_selection=%s choice=%s"
                  % (offered, told, out["message"]["content"]))
        return out

    h.la.ollama.chat = chat
    svc = CaretWakeService(
        data_dir=data_dir, owner_paused=lambda: False, trace_path=str(tmp_path / "trace.jsonl"),
        run_occasion=lambda p: h.la.run_caret_occasion_turn(p, orch_factory=h.la.AnaxiOrchestrator),
    )
    assert svc.step()[0] == "delivered"
    # Pass 1 decided the route on the framed delivery; Pass 2's current message is the exact words ALONE
    # (29522c4, host framing never fused into the expression pass's current message).
    assert seen[0][-1]["content"] == ASK and "BEGIN RECEIVED MESSAGE" not in seen[0][-1]["content"]
    print("\nPOSTS:", posts)
    assert pass1_told == [True]      # the transport fact preceded action selection
    assert svc.step()[0] == "idle"                      # whatever Clark chose, nothing wakes him again
    assert len(posts) <= 1
    if posts:                                           # a reply, if any, is his own exact canonical Pass-2 words
        import sqlite3
        conn = sqlite3.connect(f"file:{h.db_path}?mode=ro", uri=True)
        try:
            prose = conn.execute(
                "SELECT c.component_text FROM event_components c JOIN events e USING(event_id) "
                "WHERE e.event_type = 'waking_turn' AND c.component_kind = 'conversational_prose'").fetchall()
        finally:
            conn.close()
        assert [posts[0]["content"]] == [p[0] for p in prose] and destination
        print("ROUTE-REPLY EXACT:", posts[0]["content"])
