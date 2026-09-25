"""Waking-seam regression for the Caret / private Discord OUTBOUND live failure (2026-09-20).

Live evidence: Clark chose a real structured send to the authorized destination, the host
committed the canonical outward act and an 'attempted' receipt and made the one POST -- which died
with ``URLError`` (certificate verification: a python.org macOS build has no CA bundle) and was
recorded ``not_established``. Nothing reached Discord, and Clark, whose reply had been composed
before dispatch, never learned that.

Through the real run_waking_turn, the real canonical writer and the real dispatch code, with a
SCRIPTED model and only the Discord HTTP boundary replaced by a deterministic fake:

* Pass 1 sees the authorized destination id + label; the send binds exactly to it;
* exactly one POST with Clark's exact words; durable attempted -> terminal receipt;
* the next waking turn's Pass 2 is told the mechanical outcome (confirmed / not established /
  not sent), once, and nothing is resent;
* no action -> no send, no outcome claim;
* the default transport verifies TLS with the pinned CA bundle.
"""
import json
import os
import sqlite3
import ssl
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord_author_mapping as dam
import discord_correspondence as dc
import discord_correspondence_net as net
from test_web_waking_seam import Seam

SNOWFLAKE = "123456789012345678"
AUTHOR_ID = "700000000000000101"   # the Discord author id the seam fixtures use for the owner
TOKEN = "synthetic-token-not-real"


class Fake:
    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder or (lambda: (200, json.dumps(
            {"id": "555555555555555555", "channel_id": SNOWFLAKE}).encode()))

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, body))
        return self.responder()

    @property
    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def _setup(monkeypatch, tmp_path, responder=None):
    s = Seam(monkeypatch, tmp_path)
    import oc0_schema_migration
    oc0_schema_migration.apply_additive_migration(s.h.db_path)
    data_dir = s.h.la.PROVENANCE_DB_DIR
    fake = Fake(responder)
    monkeypatch.setattr(net, "_default_request", fake)
    monkeypatch.setenv(net.DISCORD_BOT_TOKEN_ENV_VAR, TOKEN)
    record = dc.authorize_destination(
        data_dir, destination_kind="channel", discord_snowflake=SNOWFLAKE,
        display_label="Caret private family channel", requester_actor_id=s.h.actor_id,
        occurred_at=1,
    )
    s.destination_id = record["destination_id"]
    # The DESTINATION is authorized above; the CORRESPONDENT is a separate axis: the owner binds the
    # Discord author id used by these seam fixtures to the (sole, legacy) owner principal.
    dam.map_author(data_dir, discord_author_id=AUTHOR_ID, principal_actor_id=s.h.actor_id,
                   requester_actor_id=s.h.actor_id, occurred_at=1, display_label="Alex")
    s.fake = fake
    s.data_dir = data_dir
    return s


def _send(s, text="Hello Alex, this one is mine.", destination=None):
    return dict(discord_correspondence_request="send_message",
                discord_destination_id=destination or s.destination_id, discord_message_text=text)


def _receipts(s):
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT status FROM outward_projection_attempts ORDER BY rowid")]
    finally:
        conn.close()


def _pass2_system(s, index=-1):
    return s.pass2_prompts[index][0]["content"]


def test_destination_is_visible_and_a_natural_send_is_one_exact_post(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.turn("Caret is a private channel to me. Send me whatever you like.", **_send(s))
    visible = "\n".join(m["content"] for m in s.pass1_prompts[0])
    assert s.destination_id in visible and "Caret private family channel" in visible
    assert len(s.fake.posts) == 1
    assert json.loads(s.fake.posts[0][2]) == {"content": "Hello Alex, this one is mine."}
    assert s.fake.posts[0][1].endswith(f"/channels/{SNOWFLAKE}/messages")
    assert _receipts(s) == ["attempted", "succeeded"]
    # The reply that chose the send is truthfully told it had not been sent yet.
    assert "has not been sent yet" in _pass2_system(s)


def test_next_turn_pass2_is_told_the_confirmed_outcome_once(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.turn("send it", **_send(s))
    s.turn("did it arrive?")
    assert "Discord confirmed it was posted (message id 555555555555555555). Whether anyone has read it is not known." in _pass2_system(s)
    s.turn("and now?")
    assert "previous Discord send" not in _pass2_system(s)
    assert len(s.fake.posts) == 1


def test_tls_failure_is_recorded_not_established_and_disclosed_never_resent(monkeypatch, tmp_path):
    def boom():
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    s = _setup(monkeypatch, tmp_path, boom)
    s.turn("send it", **_send(s))
    assert _receipts(s) == ["attempted", "not_established"]
    s.turn("did it arrive?")
    text = _pass2_system(s)
    assert "outcome could not be established" in text and "not sent again" in text
    assert "you do not know whether it arrived" in text
    assert "confirmed" not in text
    assert len(s.fake.posts) == 1
    s.turn("try to be sure")
    assert len(s.fake.posts) == 1 and _receipts(s) == ["attempted", "not_established"]


def test_definitive_rejection_is_disclosed_as_not_delivered(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path, lambda: (403, b"{}"))
    s.turn("send it", **_send(s))
    s.turn("well?")
    assert "NOT delivered" in _pass2_system(s) and len(s.fake.posts) == 1


def test_forged_destination_sends_nothing_and_is_disclosed_as_not_sent(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.turn("send it", **_send(s, destination="discord_destination-forged"))
    assert s.fake.posts == []
    s.turn("well?")
    assert "NOT sent" in _pass2_system(s)


def test_no_action_means_no_send_and_no_outcome_claim(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.turn("Just chatting.")
    s.turn("Still chatting.")
    assert s.fake.posts == [] and _receipts(s) == []
    assert "Discord send" not in _pass2_system(s)


def test_authorization_revoked_before_dispatch_never_posts(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    dc.revoke_destination(s.data_dir, destination_id=s.destination_id,
                          requester_actor_id=s.h.actor_id, occurred_at=2)
    s.turn("send it", **_send(s))
    assert s.fake.posts == []


def test_default_transport_verifies_tls_with_the_bundled_ca_store(monkeypatch):
    captured = []

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError("stop")

    monkeypatch.setattr(net.urllib.request, "build_opener", lambda *h: captured.extend(h) or Opener())
    with pytest.raises(urllib.error.URLError):
        net._default_request("GET", f"{net.DISCORD_API_BASE}/x", {}, None, 1)
    https = [h for h in captured if isinstance(h, net.urllib.request.HTTPSHandler)]
    assert len(https) == 1
    ctx = https[0]._context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert any(isinstance(h, net._NoRedirectHandler) for h in captured)

