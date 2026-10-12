"""Multipart Discord transport of ONE canonical Caret reply (owner decision: transport repair).

Live defect: a 2410-character reply that Clark chose to send (reply_through_caret) was never dispatched
because Discord's hard per-message limit is 2000.  The transport now segments one canonical reply into
1..3 ordered physical messages (cap 6000), with a persisted plan and a per-part ledger.

Pure segmentation tests, then the real run_waking_turn / canonical writer / dispatcher with the fake
Discord transport (no real Discord, no real model).
"""
import json
import sqlite3
import ssl
import urllib.error

import pytest

import caret_owner_status as cos
import discord_correspondence as dc
import discord_correspondence_registry as dcr
import discord_transport_split as split
import native_provenance_writer
from test_caret_inbound_seam import _remote
from test_caret_outbound_seam import SNOWFLAKE, _send
from test_caret_wake_service import (
    ROUTE, _rows, _service, _serve, _setup, _trace, _waking_turns, _delivered,
)


# ------------------------------------------------------------------ pure segmentation

def _prose(n, paragraphs=True):
    """Exactly n characters, no leading/trailing whitespace, with paragraph breaks."""
    sentence = "Clark writes a considered sentence about small regular things. "
    para = (sentence * 6).strip() + "\n\n"
    out = ""
    while len(out) < n + 50:
        out += para if paragraphs else sentence
    return out[:n].strip() + "x" * (n - len(out[:n].strip()))


@pytest.mark.parametrize("n,parts", [(1, 1), (2000, 1), (2001, 2), (4000, 2), (4001, 3), (6000, 3)])
def test_part_counts_follow_the_owner_table_and_reproduce_the_text_exactly(n, parts):
    text = _prose(n)
    assert len(text) == n
    got = split.segment_reply(text)
    assert len(got) == parts and "".join(got) == text
    assert all(1 <= len(p) <= 2000 and p.strip() for p in got)


def test_over_six_thousand_is_refused_never_truncated():
    with pytest.raises(split.TransportSegmentationError):
        split.segment_reply("y" * 6001)


def test_paragraph_boundary_is_preferred_and_the_text_is_untouched():
    a = "First paragraph. " * 100                       # ~1700 chars
    text = a.strip() + "\n\n" + "Second paragraph runs on. " * 60
    text = text.strip()
    assert len(text) > 2000
    parts = split.segment_reply(text)
    assert parts[0].endswith("\n\n") and "".join(parts) == text


def test_newline_then_whitespace_are_preferred_over_a_hard_slice():
    text = ("word " * 350 + "\n" + "word " * 300).strip()      # a single newline inside the feasible window
    p = split.segment_reply(text)
    assert p[0].endswith("\n") and "".join(p) == text
    text = ("word " * 900).strip()
    p = split.segment_reply(text)
    assert p[0].endswith(" ") and "".join(p) == text


def test_no_friendly_boundary_hard_slices_deterministically_and_exactly():
    text = "x" * 4500
    a, b = split.segment_reply(text), split.segment_reply(text)
    assert a == b and [len(p) for p in a] == [2000, 2000, 500] and "".join(a) == text


def test_unicode_is_preserved_codepoint_for_codepoint():
    text = ("naïve café — “quoted” 日本語 😀 é " * 300).strip()
    text = text[:5000]
    p = split.segment_reply(text)
    assert "".join(p) == text and all(len(x) <= 2000 for x in p)


def test_every_remaining_part_stays_feasible_when_boundaries_are_early():
    text = ("tiny para.\n\n" * 480 + "z" * 200)[:5990].strip()     # early paragraph breaks everywhere
    p = split.segment_reply(text)
    assert len(p) == 3 and "".join(p) == text


def test_a_whitespace_only_part_is_refused_not_sent():
    text = "a" + " " * 4100 + "b"
    with pytest.raises(split.TransportSegmentationError):
        split.segment_reply(text)


def test_the_writer_cap_is_pinned_to_the_transport_cap():
    assert native_provenance_writer._MAX_CARET_REPLY_TEXT_LENGTH == dc.MAX_CARET_REPLY_TEXT_LENGTH == 6000
    assert dc.MAX_TRANSPORT_PART_CHARS == 2000 and dc.MAX_TRANSPORT_PARTS == 3


# ------------------------------------------------------------------ pipeline harness

class Crash(BaseException):
    """Process death (not an Exception, so nothing in the code under test can swallow it)."""


def _wire(s, monkeypatch, *, messages=None, post=None):
    """Fake Discord: GET returns `messages`; every POST gets a distinct confirmed id unless `post` decides."""
    import discord_correspondence_net as net
    state = {"posts": [], "ids": [], "gets": 0}

    def request(method, url, headers, body, timeout):
        if method == "GET":
            state["gets"] += 1
            return 200, json.dumps(list(messages if messages is not None else [_remote()])).encode()
        payload = json.loads(body)
        state["posts"].append((url, payload["content"]))
        if post is not None:
            outcome = post(len(state["posts"]))
            if outcome is not None:
                return outcome
        mid = f"55500000000000{len(state['posts']):04d}"
        state["ids"].append(mid)
        return 200, json.dumps({"id": mid, "channel_id": SNOWFLAKE}).encode()

    monkeypatch.setattr(net, "_default_request", request)
    return state


def _canonical_turn(s, monkeypatch, tmp_path, prose, *, dispatch=True, **wire):
    """Real occasion turn choosing the route with `prose` as Pass 2; optionally suppress the in-turn
    dispatch so a test can drive/crash the dispatcher itself."""
    state = _wire(s, monkeypatch, **wire)
    s.pass2_text = prose
    s.pass1_script.append(dict(ROUTE))
    real = dc.dispatch_outbound
    if not dispatch:
        monkeypatch.setattr(dc, "dispatch_outbound", lambda *a, **k: {"status": "not_recorded"})
    svc = _service(s, tmp_path)
    svc.step()
    monkeypatch.setattr(dc, "dispatch_outbound", real)
    return svc, state


def _turn_id(s):
    return _rows(s, "SELECT event_id FROM events WHERE event_type = 'waking_turn' ORDER BY rowid DESC")[0][0]


def _bodies(state):
    return [b for _u, b in state["posts"]]


def _status(s, tmp_path):
    return cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))[0]


def _part_rows(s):
    return _rows(s, "SELECT part_index, status, discord_message_id, detail FROM discord_outbound_part_attempts "
                    "ORDER BY part_index, attempt_sequence")


def _dispatch(s, state=None, **kw):
    return dc.dispatch_outbound(s.data_dir, waking_turn_event_id=_turn_id(s), **kw)


# ------------------------------------------------------------------ 1-3: counts through the real pipeline

@pytest.mark.parametrize("n,parts", [(1, 1), (2000, 1), (2001, 2), (4000, 2), (4001, 3), (6000, 3)])
def test_reply_length_maps_to_the_specified_number_of_ordered_dispatches(monkeypatch, tmp_path, n, parts):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(n)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    assert len(state["posts"]) == parts
    assert "".join(_bodies(state)) == prose                                    # exact, in order
    canonical = _rows(s, "SELECT c.component_text FROM event_components c JOIN events e USING(event_id) "
                         "WHERE e.event_type='waking_turn' AND c.component_kind='conversational_prose'")
    assert [c[0] for c in canonical] == [prose]
    assert all(u.endswith(f"/channels/{SNOWFLAKE}/messages") for u, _ in state["posts"])
    assert _status(s, tmp_path)["code"] == (cos.REPLY_SENT if parts == 1 else cos.MP_SENT)


def test_no_host_text_is_inserted_into_any_part(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(4500)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    joined = "".join(_bodies(state))
    assert joined == prose and "(1/" not in joined and "continued" not in joined.lower().replace(prose.lower(), "")


def test_over_six_thousand_sends_nothing_and_the_owner_state_is_truthful(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(6001))
    assert state["posts"] == [] and _waking_turns(s) == 1 and _delivered(s) == 1
    assert _rows(s, "SELECT COUNT(*) FROM events WHERE event_type='clark_outward_act'")[0][0] == 0
    assert _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0] == 0
    st = _status(s, tmp_path)
    assert st["code"] == cos.REPLY_TRANSPORT_CAP_EXCEEDED and "6000-char transport cap; nothing sent" in st["summary"]
    assert any(t.get("caret_reply_reason") == "reply_body_exceeds_transport_cap" for t in _trace(tmp_path))


def test_hard_split_through_the_pipeline_is_exact(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = "q" * 4500
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    assert [len(b) for b in _bodies(state)] == [2000, 2000, 500] and "".join(_bodies(state)) == prose


# ------------------------------------------------------------------ canonical shape / plan

def test_one_canonical_act_and_one_authored_reply_stay_authoritative(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(4500)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    acts = _rows(s, "SELECT event_id FROM events WHERE event_type='clark_outward_act'")
    assert len(acts) == 1
    content = _rows(s, "SELECT component_text FROM event_components WHERE event_id=? AND sequence=0", acts[0][0])
    assert content == [(prose,)]
    plan = _rows(s, "SELECT outward_event_id, part_index, part_count, char_start, char_end FROM discord_outbound_parts "
                    "ORDER BY part_index")
    assert [p[0] for p in plan] == [acts[0][0]] * 3 and [p[2] for p in plan] == [3, 3, 3]
    assert plan[0][3] == 0 and plan[-1][4] == len(prose)
    assert _rows(s, "SELECT COUNT(*) FROM outward_projection_attempts")[0][0] == 0   # parts live in their own ledger
    assert _rows(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'")[0][0] == 1


def test_the_plan_is_persisted_and_the_first_part_is_receipted_before_the_first_network_call(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    seen = {}

    def post(n):
        if n == 1:
            seen["plan"] = _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0]
            seen["attempted"] = _part_rows(s)
        return None
    _canonical_turn(s, monkeypatch, tmp_path, _prose(4500), post=post)
    assert seen["plan"] == 3 and [(r[0], r[1]) for r in seen["attempted"]] == [(1, "attempted")]


def test_persisted_plan_and_ledgers_are_append_only(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _canonical_turn(s, monkeypatch, tmp_path, _prose(2500))
    conn = sqlite3.connect(s.h.db_path)
    for sql in ("UPDATE discord_outbound_parts SET char_end = 1", "DELETE FROM discord_outbound_parts",
                "UPDATE discord_outbound_part_attempts SET status='failed'",
                "DELETE FROM discord_outbound_part_attempts"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.close()


def test_replaying_a_completed_reply_never_resends_any_part(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(4500))
    before = list(state["posts"])
    for _ in range(3):
        assert _dispatch(s)["status"] == dcr.DISPATCH_CONFIRMED_SENT
    assert dc.resume_multipart_dispatch(s.data_dir) == []
    assert state["posts"] == before and len(_part_rows(s)) == 6                 # 3 x (attempted, succeeded)


# ------------------------------------------------------------------ crash / resume

def _crash_before_attempt(monkeypatch, part_index):
    real = dc._record_part_attempt

    def hook(data_dir, outward, index, status, **kw):
        if index == part_index and status == "attempted":
            raise Crash()
        return real(data_dir, outward, index, status, **kw)
    monkeypatch.setattr(dc, "_record_part_attempt", hook)
    return real


def test_crash_after_the_first_confirmed_part_resumes_at_the_second_with_no_duplicate(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(4500)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose, dispatch=False)
    real = _crash_before_attempt(monkeypatch, 2)
    with pytest.raises(Crash):
        _dispatch(s)
    assert len(state["posts"]) == 1 and _status(s, tmp_path)["code"] == cos.MP_PARTIAL
    monkeypatch.setattr(dc, "_record_part_attempt", real)               # "restart"
    results = dc.resume_multipart_dispatch(s.data_dir)
    assert [r["status"] for r in results] == [dcr.DISPATCH_CONFIRMED_SENT]
    assert "".join(_bodies(state)) == prose and len(state["posts"]) == 3
    assert _bodies(state)[0] == split.segment_reply(prose)[0]           # part 1 was sent exactly once


def test_crash_after_the_second_of_three_leaves_only_the_third(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(5500)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose, dispatch=False)
    real = _crash_before_attempt(monkeypatch, 3)
    with pytest.raises(Crash):
        _dispatch(s)
    assert len(state["posts"]) == 2
    monkeypatch.setattr(dc, "_record_part_attempt", real)
    dc.resume_multipart_dispatch(s.data_dir)
    assert len(state["posts"]) == 3 and "".join(_bodies(state)) == prose
    assert _bodies(state) == split.segment_reply(prose)                        # each part exactly once, in order


def test_the_wake_service_itself_resumes_an_interrupted_reply_on_its_next_tick(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(4500)
    svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose, dispatch=False)
    real = _crash_before_attempt(monkeypatch, 2)
    with pytest.raises(Crash):
        _dispatch(s)
    monkeypatch.setattr(dc, "_record_part_attempt", real)
    assert svc.step()[0] == "idle"
    assert "".join(_bodies(state)) == prose and len(state["posts"]) == 3
    assert any(t["event"] == "multipart_resumed" for t in _trace(tmp_path))
    assert svc.step()[0] == "idle" and len(state["posts"]) == 3


def test_a_crash_mid_part_is_outcome_not_established_and_blocks_later_parts(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(5500), dispatch=False)
    import discord_correspondence_net as net
    inner = net._default_request

    def dying(method, url, headers, body, timeout):
        if method == "POST" and len(state["posts"]) == 1:
            raise Crash()                                 # the 2nd POST's fate is unknown
        return inner(method, url, headers, body, timeout)
    monkeypatch.setattr(net, "_default_request", dying)
    with pytest.raises(Crash):
        _dispatch(s)
    monkeypatch.setattr(net, "_default_request", inner)
    posts_before = len(state["posts"])
    assert dc.resume_multipart_dispatch(s.data_dir)[0]["status"] == dcr.DISPATCH_OUTCOME_NOT_ESTABLISHED
    assert len(state["posts"]) == posts_before == 1                     # no blind duplicate, no overtake
    st = _status(s, tmp_path)
    assert st["code"] == cos.MP_OUTCOME_NOT_ESTABLISHED and "OUTCOME_NOT_ESTABLISHED" in st["summary"]
    assert "part 2" in st["summary"] and "1/3 sent" in st["summary"]
    for _ in range(2):
        dc.resume_multipart_dispatch(s.data_dir)
        _dispatch(s)
    assert len(state["posts"]) == 1


def test_an_ambiguous_transport_result_on_part_two_stops_part_three(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    boom = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    def post(n):
        if n == 2:
            raise boom
        return None
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(5500), post=post)
    assert len(state["posts"]) == 2
    assert [(r[0], r[1]) for r in _part_rows(s)] == [(1, "attempted"), (1, "succeeded"), (2, "attempted"),
                                                     (2, "not_established")]
    assert _status(s, tmp_path)["code"] == cos.MP_OUTCOME_NOT_ESTABLISHED
    assert dc.resume_multipart_dispatch(s.data_dir) == [] and len(state["posts"]) == 2


def test_a_definite_rejection_of_a_later_part_is_partial_not_sent_and_not_success(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(5500),
                                  post=lambda n: (403, b"{}") if n == 2 else None)
    st = _status(s, tmp_path)
    assert st["code"] == cos.MP_PARTIAL and "1/3 sent, part 2 failed" in st["summary"]
    assert len(state["posts"]) == 2
    assert dc.resume_multipart_dispatch(s.data_dir) == [] and len(state["posts"]) == 2   # never auto-retried


def test_a_definite_rejection_of_the_first_part_is_a_plain_multipart_failure(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(4500), post=lambda n: (403, b"{}"))
    st = _status(s, tmp_path)
    assert st["code"] == cos.MP_FAILED and "0/3 sent" in st["summary"] and len(state["posts"]) == 1


# ------------------------------------------------------------------ authorization

def test_revocation_between_parts_stops_fail_closed_and_keeps_the_confirmed_part(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(4500), dispatch=False)
    real = dc._record_part_attempt

    def hook(data_dir, outward, index, status, **kw):
        out = real(data_dir, outward, index, status, **kw)
        if index == 1 and status == "succeeded":          # owner revokes right after part 1 is confirmed
            dc._record_registry_change(
                data_dir, destination_id=s.destination_id, destination_kind=None, discord_snowflake=None,
                display_label=None, action=dcr.REGISTRY_ACTION_REVOKE, requester_actor_id=s.h.actor_id,
                occurred_at=2, event_id=None)
        return out
    monkeypatch.setattr(dc, "_record_part_attempt", hook)
    result = _dispatch(s)
    assert len(state["posts"]) == 1                                         # nothing redirected, nothing more sent
    assert result["confirmed_parts"] == 1 and result["status"] == dcr.DISPATCH_MULTIPART_PARTIALLY_SENT
    rows = _part_rows(s)
    assert (1, "succeeded") in [(r[0], r[1]) for r in rows]
    assert (2, "failed", None, "not_authorized_at_dispatch") in rows
    assert "1/3 sent, part 2 failed" in _status(s, tmp_path)["summary"]


def test_every_part_binds_the_original_inbound_destination(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(5500))
    assert len(state["posts"]) == 3
    assert {u for u, _ in state["posts"]} == {f"https://discord.com/api/v10/channels/{SNOWFLAKE}/messages"} or \
        all(u.endswith(f"/channels/{SNOWFLAKE}/messages") for u, _ in state["posts"])
    ref = _rows(s, "SELECT c.component_text FROM event_components c JOIN events e USING(event_id) "
                   "WHERE e.event_type='clark_outward_act' AND c.component_kind='outward_recipient_reference'")
    assert ref == [(f"{dcr.RECIPIENT_REFERENCE_PREFIX}{s.destination_id}",)]


# ------------------------------------------------------------------ self-echo

def test_every_confirmed_part_id_is_suppressed_as_self_echo_and_never_wakes_clark(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(5500)
    svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    assert len(state["ids"]) == 3
    echoes = [{"id": mid, "channel_id": SNOWFLAKE, "content": body, "author": {"id": "9", "username": "clark-bot"},
               "timestamp": "2026-09-21T20:40:00+00:00"} for mid, body in zip(state["ids"], _bodies(state))]
    _wire(s, monkeypatch, messages=echoes)
    assert svc.step()[0] == "idle"
    assert _waking_turns(s) == 1 and len(dc.next_pending_inbound(s.data_dir)) == 0
    conn = sqlite3.connect(s.h.db_path)
    for mid in state["ids"]:
        assert dc._is_confirmed_outbound_message(conn, s.destination_id, mid)
    conn.close()


# ------------------------------------------------------------------ unchanged behavior

def test_no_route_long_prose_stays_local_and_sends_nothing(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    state = _wire(s, monkeypatch)
    s.pass2_text = _prose(4500)
    _service(s, tmp_path).step()
    assert state["posts"] == [] and _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0] == 0
    assert _rows(s, "SELECT COUNT(*) FROM events WHERE event_type='clark_outward_act'")[0][0] == 0


def test_pass2_failure_after_the_route_sends_nothing_even_when_a_long_body_was_expected(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    state = _wire(s, monkeypatch)
    s.pass2_text = ""
    s.pass1_script.append(dict(ROUTE))
    assert _service(s, tmp_path).step()[0] == "failed"
    assert state["posts"] == [] and _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0] == 0


def test_the_general_send_message_action_keeps_its_2000_cap_and_single_message_law(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    state = _wire(s, monkeypatch)
    try:
        s.turn("send it", **_send(s, text="w" * 2001))     # refused before any dispatch (writer/validator cap)
    except Exception:
        pass
    assert state["posts"] == [] and _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0] == 0
    s.turn("send a normal one", **_send(s, text="short and exact"))
    assert [b for _u, b in state["posts"]] == ["short and exact"]
    assert _rows(s, "SELECT COUNT(*) FROM outward_projection_attempts")[0][0] == 2


def test_a_2000_char_reply_is_the_unchanged_single_message_path(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(2000)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose)
    assert _bodies(state) == [prose]
    assert _rows(s, "SELECT COUNT(*) FROM discord_outbound_parts")[0][0] == 0
    assert [r[0] for r in _rows(s, "SELECT status FROM outward_projection_attempts ORDER BY rowid")] == [
        "attempted", "succeeded"]


# ------------------------------------------------------------------ owner status

def test_multipart_pending_is_shown_once_the_plan_exists_and_nothing_is_sent(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    prose = _prose(4500)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, prose, dispatch=False)
    real = _crash_before_attempt(monkeypatch, 1)
    with pytest.raises(Crash):
        _dispatch(s)
    monkeypatch.setattr(dc, "_record_part_attempt", real)
    st = _status(s, tmp_path)
    assert st["code"] == cos.MP_PENDING and "multipart pending" in st["summary"] and state["posts"] == []


def test_status_refresh_is_read_only_and_performs_no_network_action(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, _prose(4500),
                                  post=lambda n: (403, b"{}") if n == 2 else None)
    before = (len(state["posts"]), state["gets"], _part_rows(s), _waking_turns(s))
    for _ in range(5):
        label, body = cos.render_owner_status(cos.caret_owner_status(
            s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl")))
    assert (len(state["posts"]), state["gets"], _part_rows(s), _waking_turns(s)) == before
    assert _prose(10)[:10] not in label + body                                   # no message content in telemetry


def test_prior_outbound_disclosure_is_truthful_about_partial_multipart_delivery(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _canonical_turn(s, monkeypatch, tmp_path, _prose(4500), post=lambda n: (403, b"{}") if n == 2 else None)
    text = dc.describe_prior_outbound(dc.undisclosed_prior_outbound(s.data_dir))
    assert "3 consecutive Discord messages" in text and "only 1 of 3 were confirmed" in text
    assert "NOT delivered" in text and "sent successfully" not in text


def test_readers_tolerate_a_database_that_predates_the_multipart_tables(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _svc, state = _canonical_turn(s, monkeypatch, tmp_path, "A short reply.")
    conn = sqlite3.connect(s.h.db_path)
    conn.execute("DROP TABLE discord_outbound_part_attempts")
    conn.execute("DROP TABLE discord_outbound_parts")
    conn.commit()
    conn.close()
    assert _status(s, tmp_path)["code"] == cos.REPLY_SENT
    outward = dc.derive_stable_id("discord_outward_act", _turn_id(s))
    assert dc.outbound_dispatch_state(s.data_dir, outward) == dcr.DISPATCH_CONFIRMED_SENT
    assert dc.multipart_summary(s.data_dir, outward) is None
