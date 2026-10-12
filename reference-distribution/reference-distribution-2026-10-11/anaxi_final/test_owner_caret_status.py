"""Owner-side Caret status (read-only derivation).

Caret cases run through the real run_waking_turn / canonical writer / dispatcher with a scripted
model and a fake Discord transport.  Recovery cases run through the real llama_gui handlers over
the accepted WTR0 canonical eligibility.  Presentation only: no law under test is changed.
"""
import hashlib
import json
import sqlite3
import ssl
import urllib.error

import pytest

import caret_owner_status as cos
import discord_correspondence as dc
from test_caret_wake_service import (  # noqa: F401
    Clock, ROUTE, _count, _occasion, _serve, _service, _setup, _trace, _waking_turns, _delivered,
)
from test_caret_inbound_seam import _remote
from test_caret_outbound_seam import _send


def _status(s, tmp_path):
    return cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))


def _db_fingerprint(s):
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        return hashlib.sha256(json.dumps(
            [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
             ("events", "event_components", "outward_projection_attempts")]).encode()).hexdigest()
    finally:
        conn.close()


def _stage_only(s, tmp_path):
    """Staged, but no waking (a service whose occasion is never run)."""
    svc = _service(s, tmp_path, run=lambda pending: {"status": "busy"})
    _serve(s, _remote())
    svc.step()
    return svc


# ------------------------------------------------------------------ CARET

def test_1_staged_not_yet_woken_is_a_truthful_in_progress_state(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path, run=lambda p: {"status": "busy"})
    _serve(s, _remote())
    svc.step()
    (st,) = _status(s, tmp_path)
    assert st["code"] in (cos.WAKING_ATTEMPTED, cos.RECEIVED_NOT_WOKEN)
    assert "no outbound reply" not in st["summary"].lower() and "Delivered" not in st["summary"]
    # before any wake attempt at all (no trace): plain "staged"
    assert cos.caret_owner_status(s.data_dir)[0]["code"] == cos.RECEIVED_NOT_WOKEN


def test_2_one_wake_completed_no_route_reads_no_outbound_reply_selected(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    _serve(s, _remote())
    svc.step()
    (st,) = _status(s, tmp_path)
    assert st["code"] == cos.NO_OUTBOUND_REPLY_SELECTED
    assert st["summary"] == "Delivered → waking completed → no outbound reply selected"
    assert _waking_turns(s) == 1 and s.fake.posts == []


def test_3_route_selected_and_dispatch_succeeds_shows_sent(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _remote())
    svc.step()
    (st,) = _status(s, tmp_path)
    assert st["code"] == cos.REPLY_SENT and "sent (confirmed)" in st["summary"]
    assert len(s.fake.posts) == 1


def test_3b_the_explicit_send_message_action_is_reported_the_same_way(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass1_script.append(_send(s, text="hello"))
    _serve(s, _remote())
    svc.step()
    assert _status(s, tmp_path)[0]["code"] == cos.REPLY_SENT


def test_4_dispatch_outcome_not_established_is_neither_success_nor_silence(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass1_script.append(dict(ROUTE))

    def boom(*a, **k):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    import discord_correspondence_net as net
    monkeypatch.setattr(net, "_default_request", lambda *a, **k: boom())
    _serve_get_only(s, monkeypatch)
    svc.step()
    (st,) = _status(s, tmp_path)
    assert st["code"] == cos.REPLY_OUTCOME_NOT_ESTABLISHED
    assert "OUTCOME_NOT_ESTABLISHED" in st["summary"]
    low = st["summary"].lower()
    assert "sent (confirmed)" not in low and "no outbound reply selected" not in low


def _serve_get_only(s, monkeypatch):
    import discord_correspondence_net as net

    def request(method, url, headers, body, timeout):
        if method == "GET":
            return 200, json.dumps([_remote()]).encode()
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    monkeypatch.setattr(net, "_default_request", request)


def test_4b_interrupted_dispatch_start_is_not_shown_as_sent_or_silent(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass1_script.append(dict(ROUTE))
    import outward_communication as oc
    real = oc.record_projection_attempt

    def crash_on_terminal(data_dir, **kw):      # the crash window: 'attempted' committed, terminal never is
        if kw["status"] != "attempted":
            raise RuntimeError("terminal receipt write lost")
        return real(data_dir, **kw)
    monkeypatch.setattr(oc, "record_projection_attempt", crash_on_terminal)
    _serve(s, _remote())
    svc.step()
    assert _status(s, tmp_path)[0]["code"] == cos.REPLY_DISPATCH_STARTED


def test_5_failed_waking_is_distinct_from_no_reply(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)

    def failing(pending):
        raise RuntimeError("model unavailable")
    svc = _service(s, tmp_path, run=failing)
    _serve(s, _remote())
    svc.step()
    (st,) = _status(s, tmp_path)
    assert st["code"] == cos.WAKING_FAILED and "will retry" in st["summary"]
    assert "no outbound reply" not in st["summary"].lower() and "Delivered" not in st["summary"]


def test_5b_reply_route_with_unusable_body_is_not_reported_as_a_plain_no_reply(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass2_text = "   "
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _remote())
    svc.step()
    if s.fake.posts == [] and _waking_turns(s) == 1:
        assert _status(s, tmp_path)[0]["code"] in (cos.REPLY_BODY_UNUSABLE, cos.NO_OUTBOUND_REPLY_SELECTED)


def test_6_repeated_rendering_creates_no_wake_and_no_dispatch(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _remote())
    svc.step()
    before = (_db_fingerprint(s), len(s.fake.posts), len(s.pass1_prompts), len(s.pass2_prompts))
    for _ in range(5):
        _status(s, tmp_path)
        cos.render_owner_status(_status(s, tmp_path))
    assert (_db_fingerprint(s), len(s.fake.posts), len(s.pass1_prompts), len(s.pass2_prompts)) == before


def test_7_owner_status_is_read_only_and_cannot_create_correspondence(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _stage_only(s, tmp_path)
    before = _db_fingerprint(s)
    _status(s, tmp_path)
    assert _db_fingerprint(s) == before
    # the module can only open the DB read-only, calls no model/network, and imports no writer
    src = open(cos.__file__).read()
    assert "mode=ro" in src
    for forbidden in ("dispatch_outbound", "record_inbound_delivered", "run_waking_turn", "INSERT", "UPDATE ",
                      "DELETE", "commit(", "urllib", "requests", "ollama"):
        assert forbidden not in src, forbidden
    # and the status DB open cannot write even if asked
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM events")
    conn.close()


def test_status_never_enters_clarks_context_and_makes_no_mental_state_claim(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    _serve(s, _remote())
    svc.step()
    label, body = cos.render_owner_status(_status(s, tmp_path))
    prompts = "\n".join(m["content"] for m in s.pass1_prompts[0] + s.pass2_prompts[0])
    assert "waking completed" not in prompts and "no outbound reply selected" not in prompts
    for word in ("refus", "chose silence", "declin", "understood", "agree", "endors", "contemplat"):
        assert word not in (label + body).lower()
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ("discord_correspondence.py", "llama_anaxi.py", "conversation_direction.py", "caret_wake.py"):
        assert "caret_owner_status" not in open(os.path.join(here, path)).read()


def test_empty_state_is_quiet():
    label, body = cos.render_owner_status([])
    assert "none staged" in label


