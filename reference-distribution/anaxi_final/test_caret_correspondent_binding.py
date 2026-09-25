"""Caret correctness repair: correspondent principal binding + unsent-prose projection fix.

Through the real run_waking_turn / canonical writer / receive + carriage code with a scripted model
and a fake Discord transport (the shared Caret wake-service fixtures).  No real Discord, no real
model, no production state.

Law under test
  * DESTINATION and CORRESPONDENT are separate authority axes: a valid channel alone never identifies
    who wrote a message.  A stable Discord author id is bound, by an owner act, to an EXISTING
    canonical principal, revalidated (mapping + Family active membership) at every delivery.
  * Unmapped / revoked / stale / ambiguous authors never wake Clark and their words never reach him.
  * The ordinary owner conversation never projects a Caret occasion turn (generated locally !=
    communicated), and never shows an orphan Clark reply.
  * New turns durably distinguish: no route / route chosen but body unusable / route chosen and sent.
  * In family mode a mapped principal's Caret occasion is a FAMILY_SHARED session of THAT principal, bound
    through the accepted FS1 machinery (own scoped session + canonical H + frozen scope): FAMILY_SHARED
    continuity with individual attribution, never anything principal-private.
  * A mapping is PROSPECTIVE: a message that arrived while its author was unmapped is unauthorized at arrival
    forever; the later-established identity is available only as an explicitly later-dated read-time fact.
"""
import ast
import json
import os
import sqlite3

import pytest

import caret_correspondent_admin as cca
import caret_owner_status as cos
import conversation_projection as cp
import discord_author_mapping as dam
import discord_correspondence as dc
import discord_correspondence_net as net
import family_membership as fm
import human_session_binding as hsb
from hir1_registration import register_canonical_human, HOST_ACTOR_ID
from test_caret_inbound_seam import fixture_snowflake, _remote, WORDS
from test_caret_outbound_seam import AUTHOR_ID, SNOWFLAKE
from test_caret_wake_service import (
    ROUTE, _count, _delivered, _occasion, _rows, _serve, _service, _setup, _trace, _waking_turns,
)

MEMBER_AUTHOR = "777000000000000001"
STRANGER_AUTHOR = "888000000000000002"


def _enter_family_mode(s):
    """Owner = the fixture's registered human; one enrolled family member ('Wife')."""
    conn = sqlite3.connect(s.h.db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    res, failure = register_canonical_human(
        conn, {"registration_request_id": "req-member-caret", "aab_actor_id": "actor-member-caret",
               "display_label": "Wife", "source": "local_operator_provisioning"})
    assert failure is None, failure
    conn.row_factory = None
    conn.commit()
    member = res["actor_id"]
    fm.designate_owner(conn, principal_actor_id=s.h.actor_id, display_label="Alex",
                       requester_actor_id=HOST_ACTOR_ID)
    fm.enroll_family_member(conn, principal_actor_id=member, display_label="Wife",
                            requester_actor_id=s.h.actor_id)
    conn.commit()
    conn.close()
    return member


def _map_member(s, member, author=MEMBER_AUTHOR, label=None):
    return dam.map_author(s.data_dir, discord_author_id=author, principal_actor_id=member,
                          requester_actor_id=s.h.actor_id, occurred_at=1, display_label=label)


def _from(author, username, content=WORDS, mid=fixture_snowflake(15999)):
    m = _remote(mid=mid, content=content)
    m["author"] = {"id": author, "username": username}
    return m


def _projected(s, **kw):
    return cp.project_waking_conversation(s.h.db_path, include_event_metadata=True, **kw)


def _turn_components(s, index=0):
    turn = _rows(s, "SELECT event_id FROM events WHERE event_type='waking_turn' ORDER BY rowid")[index][0]
    return turn, dict(_rows(
        s, "SELECT component_kind, component_text FROM event_components WHERE event_id = ?", turn))


def _all_prompt_text(s):
    return "\n".join(m["content"] for p in s.pass1_prompts + s.pass2_prompts for m in p)


def _last_prompt(s):
    return s.pass2_prompts[-1][-1]["content"]


def _last_pass2_text(s):
    """HOST-FRAMING LEAK repair: the mechanical provenance facts (sender identity, channel,
    authorization) now ride in Pass 2's host/system framing, separately from the exact words
    that are the current human message (see _last_prompt) -- both still reach Pass 2, just
    never fused into one model-visible object. Callers that only need the facts to be
    SOMEWHERE model-visible in the same turn's Pass 2 use this; callers checking the exact
    current-human-message content use _last_prompt."""
    return "\n".join(m["content"] for m in s.pass2_prompts[-1])


# ===================================================================== projection (defect 1)

def test_1_a_caret_turn_with_no_reply_is_never_projected_into_the_ordinary_transcript(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = "LOCAL-PROSE-NO-REPLY-MARKER"
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    _turn, comps = _turn_components(s)
    assert comps["conversational_prose"] == "LOCAL-PROSE-NO-REPLY-MARKER"      # canonical record intact
    for kw in ({}, {"viewer_principal_actor_id": s.h.actor_id, "viewer_visibility_scope": None}):
        rows = _projected(s, **kw)
        assert "LOCAL-PROSE-NO-REPLY-MARKER" not in json.dumps(rows)
        assert rows == []          # no orphan Clark turn, no inbound turn, nothing invented


def test_2_route_chosen_but_body_unusable_or_blocked_never_exposes_local_prose(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = "U" * 6001          # beyond the transport cap: nothing is sent
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    assert s.fake.posts == [] and _waking_turns(s) == 1
    assert "UUUUUUUUUU" not in json.dumps(_projected(s))
    assert _projected(s) == []


def test_3_a_sent_caret_reply_is_not_shown_as_an_orphan_assistant_turn(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = "SENT-REPLY-MARKER words for the correspondent."
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    assert len(s.fake.posts) == 1
    assert _projected(s) == []


def test_4_the_ordinary_mac_transcript_is_unchanged_including_a_mac_turn_that_carried_a_caret_message(
        monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.pass2_text = "Ordinary Mac reply."
    s.turn("First Mac message.")
    _serve(s, _remote())
    s.turn("Second Mac message, which also carries the owner's Caret words.")
    assert _rows(s, "SELECT COUNT(*) FROM event_components WHERE component_kind='discord_inbound_carriage'")[0][0] == 1
    rows = _projected(s, viewer_principal_actor_id=s.h.actor_id, viewer_visibility_scope=None)
    # (both turns fall inside one clock second in a test, so compare content, not tie order)
    assert sorted((r["role"], r["content"]) for r in rows) == sorted([
        ("user", "First Mac message."), ("assistant", "Ordinary Mac reply."),
        ("user", "Second Mac message, which also carries the owner's Caret words."),
        ("assistant", "Ordinary Mac reply."),
    ])


def test_5_historical_ordinary_h_events_remain_visible_alongside_caret_turns(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    s.turn("Historical human message one.")
    s.turn("Historical human message two.")
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    users = [r["content"] for r in _projected(s) if r["role"] == "user"]
    assert users == ["Historical human message one.", "Historical human message two."]
    assert len(_projected(s)) == 4        # two Mac H/X exchanges; the Caret turn adds nothing


# ===================================================================== author -> principal (defect 2)

def test_6_an_unmapped_author_in_an_authorized_channel_never_wakes_clark(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="STRANGER-WORDS-MARKER"))
    assert svc.step()[0] == "idle"
    assert _waking_turns(s) == 0 and _delivered(s) == 0 and s.fake.posts == []
    assert s.pass1_prompts == [] and s.pass2_prompts == []
    # durably staged (troubleshooting possible) yet visibly held, content-free
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='discord_inbound_message'") == 1
    (held,) = dc.held_inbound(s.data_dir)
    assert held["discord_author_id"] == STRANGER_AUTHOR and held["reason"] == "unmapped"
    assert "STRANGER-WORDS-MARKER" not in json.dumps(held)
    assert any(t["event"] == "held_author_not_deliverable" and t["reason"] == "unmapped" for t in _trace(tmp_path))
    assert "STRANGER-WORDS-MARKER" not in json.dumps(_trace(tmp_path))
    (status,) = cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    assert status["code"] == cos.AUTHOR_NOT_DELIVERABLE
    assert "not delivered" in status["summary"] and STRANGER_AUTHOR in status["summary"]
    assert "STRANGER-WORDS-MARKER" not in status["summary"]
    # the owner can learn the id from the held record without the message ever reaching Clark
    view = cca.load_view(s.data_dir)
    assert STRANGER_AUTHOR in view["markdown"] and "STRANGER-WORDS-MARKER" not in view["markdown"]
    # ... and nothing is created automatically
    assert dam.list_mappings(s.data_dir) == [
        {"discord_author_id": AUTHOR_ID, "status": "mapped", "principal_actor_id": s.h.actor_id,
         "display_label": "Alex", "discord_username_snapshot": None}]


def test_7_a_mapped_owner_principal_wakes_with_canonical_attribution(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    # the exact words are Pass 2's current human message, and nothing else
    assert _last_prompt(s) == WORDS
    text = _last_pass2_text(s)
    assert "written to you by Alex (the ANAXI owner)" not in text   # never fused with the words as one object
    assert "the sender is identified by ANAXI as Alex (the ANAXI owner)" in text
    assert "owner bound Discord user id 700000000000000101" in text
    assert "display metadata only and proves nothing" in text
    _turn, comps = _turn_components(s)
    assert comps["caret_correspondent_principal"] == s.h.actor_id


def test_8_a_mapped_family_principal_wakes_with_that_principals_attribution(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    # the owner's own Discord binding is still valid (owner is an active family principal)
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Hi Clark, it's me."))
    assert _service(s, tmp_path).step()[0] == "delivered"
    assert _last_prompt(s) == "Hi Clark, it's me."
    text = _last_pass2_text(s)
    assert "the sender is identified by ANAXI as Wife (an enrolled ANAXI family member)" in text
    assert "the ANAXI owner" not in text            # a family principal is never presented as the owner
    _turn, comps = _turn_components(s)
    assert comps["caret_correspondent_principal"] == member != s.h.actor_id
    assert _projected(s) == []


def test_9_a_username_change_for_the_same_discord_id_keeps_the_canonical_identity(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path)
    _serve(s, _from(AUTHOR_ID, "old_handle", mid=fixture_snowflake(15001)))
    assert svc.step()[0] == "delivered"
    _serve(s, _from(AUTHOR_ID, "totally_new_handle", mid=fixture_snowflake(15002)))
    assert svc.step()[0] == "delivered"
    for p in s.pass2_prompts:
        assert "the sender is identified by ANAXI as Alex (the ANAXI owner)" in "\n".join(m["content"] for m in p)
    assert "old_handle" in "\n".join(m["content"] for m in s.pass2_prompts[0])
    assert "totally_new_handle" in "\n".join(m["content"] for m in s.pass2_prompts[1])
    principals = [_turn_components(s, i)[1]["caret_correspondent_principal"] for i in (0, 1)]
    assert principals == [s.h.actor_id, s.h.actor_id]
    # and a username can never make an author resolve: only the stable id does
    _serve(s, _from("999000000000000003", "old_handle", mid=fixture_snowflake(15003)))
    assert svc.step()[0] == "idle" and _waking_turns(s) == 2


def test_10_a_deactivated_family_principal_fails_closed_before_waking_clark(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle"))
    svc = _service(s, tmp_path, run=lambda p: {"status": "busy"})
    svc.step()                                        # staged, occasion not run
    (stale,) = dc.next_pending_inbound(s.data_dir)
    conn = sqlite3.connect(s.h.db_path)
    fm.deactivate_family_member(conn, principal_actor_id=member, requester_actor_id=s.h.actor_id)
    conn.commit(); conn.close()
    assert dc.next_pending_inbound(s.data_dir) == []
    assert dc.held_inbound(s.data_dir)[0]["reason"] == dam.STATUS_PRINCIPAL_INACTIVE
    # a stale, previously-valid pending record is re-derived and refused at the turn boundary
    assert s.h.la.run_caret_occasion_turn(stale, orch_factory=s.h.la.AnaxiOrchestrator) == {"status": "not_pending"}
    with pytest.raises(s.h.la.CaretOccasionUnavailable):
        s.h.la.run_waking_turn.__wrapped__(
            s.h.la.AnaxiOrchestrator(), "", interaction_mode="conversation", caret_occasion=stale)
    assert _waking_turns(s) == 0 and _delivered(s) == 0 and s.pass1_prompts == []


def test_11_a_revoked_mapping_fails_closed(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    svc = _service(s, tmp_path, run=lambda p: {"status": "busy"})
    _serve(s, _remote())
    svc.step()
    (stale,) = dc.next_pending_inbound(s.data_dir)
    dam.revoke_author(s.data_dir, discord_author_id=AUTHOR_ID, requester_actor_id=s.h.actor_id, occurred_at=5)
    assert dc.next_pending_inbound(s.data_dir) == []
    assert dc.held_inbound(s.data_dir)[0]["reason"] == dam.STATUS_REVOKED
    with pytest.raises(s.h.la.CaretOccasionUnavailable):
        s.h.la.run_waking_turn.__wrapped__(
            s.h.la.AnaxiOrchestrator(), "", interaction_mode="conversation", caret_occasion=stale)
    assert _service(s, tmp_path).step()[0] == "idle" and _waking_turns(s) == 0
    # the canonical writer independently refuses to carry an author that no longer resolves
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        with pytest.raises(dc.DiscordCorrespondenceError):
            dc.verify_inbound_event_for_carriage(
                conn, stale["event_id"], waking_occurred_at=2**40,
                expected_principal_actor_id=s.h.actor_id)
    finally:
        conn.close()


def test_12_a_discord_id_never_resolves_ambiguously_to_multiple_principals(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    with pytest.raises(dam.AuthorMappingError):      # already actively mapped: revoke first
        dam.map_author(s.data_dir, discord_author_id=AUTHOR_ID, principal_actor_id=member,
                       requester_actor_id=s.h.actor_id, occurred_at=2)
    # even a corrupt ledger holding two active principals fails closed rather than picking one
    conn = sqlite3.connect(s.h.db_path)
    conn.execute(
        "INSERT INTO discord_author_mapping_events (mapping_event_id, discord_author_id, action, "
        "principal_actor_id, principal_display_label, discord_username_snapshot, requester_actor_id, "
        "occurred_at, created_at) VALUES ('ambig1', ?, 'map', ?, 'Wife', NULL, ?, 3, 3)",
        (AUTHOR_ID, member, s.h.actor_id))
    conn.commit()
    assert dam.resolve_author(conn, AUTHOR_ID)["status"] == dam.STATUS_AMBIGUOUS
    conn.close()
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "idle" and _waking_turns(s) == 0
    assert dc.held_inbound(s.data_dir)[0]["reason"] == dam.STATUS_AMBIGUOUS


def test_mapping_administration_is_owner_gated_key_is_the_stable_id_and_targets_existing_principals(
        monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    with pytest.raises(dam.AuthorMappingError):      # a family member cannot administer
        dam.map_author(s.data_dir, discord_author_id=MEMBER_AUTHOR, principal_actor_id=member,
                       requester_actor_id=member, occurred_at=2)
    with pytest.raises(dam.AuthorMappingError):      # Clark / anyone else cannot administer
        dam.map_author(s.data_dir, discord_author_id=MEMBER_AUTHOR, principal_actor_id=member,
                       requester_actor_id="actor-clark", occurred_at=2)
    with pytest.raises(dam.AuthorMappingError):      # a username is not a key
        dam.map_author(s.data_dir, discord_author_id="wife_handle", principal_actor_id=member,
                       requester_actor_id=s.h.actor_id, occurred_at=2)
    with pytest.raises(dam.AuthorMappingError):      # no automatic principal creation
        dam.map_author(s.data_dir, discord_author_id=MEMBER_AUTHOR, principal_actor_id="human-never-registered",
                       requester_actor_id=s.h.actor_id, occurred_at=2)
    ok, note = cca.map_author(s.data_dir, requester_actor_id=s.h.actor_id, discord_author_id=MEMBER_AUTHOR,
                              principal_actor_id=member, display_label="Wife")
    assert ok, note
    ok, note = cca.revoke_author(s.data_dir, requester_actor_id=s.h.actor_id, discord_author_id=MEMBER_AUTHOR)
    assert ok, note
    view = cca.load_view(s.data_dir)
    assert f"`{MEMBER_AUTHOR}`" in view["markdown"] and "revoked" in view["markdown"]
    assert dam.resolve_author(sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True), MEMBER_AUTHOR)["status"] == "revoked"
    # append-only: the ledger cannot be rewritten
    conn = sqlite3.connect(s.h.db_path)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM discord_author_mapping_events")
    conn.close()


# ===================================================================== Family / Shared privacy

def _run_private_owner_turn(monkeypatch, s, text, prose):
    """A REAL owner PRINCIPAL_PRIVATE Mac-style waking turn (own scoped session, canonical H/X)."""
    la = s.h.la
    alt, started = "sess-owner-private-alt", 100
    conn = sqlite3.connect(s.h.db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    auth = hsb.bind_session_to_registered_human(
        conn, session_id=alt, session_started_at=started, pipeline_key=la.PIPELINE_KEY,
        actor_id=s.h.actor_id, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE)
    conn.close()
    original = la._get_native_session
    monkeypatch.setattr(la, "_get_native_session", lambda: (alt, started))
    try:
        s.pass2_text = prose
        s.pass1_script.append({})
        _serve(s)
        la.run_waking_turn(la.AnaxiOrchestrator(), text, interaction_mode="conversation",
                           human_input_authority=auth)
    finally:
        monkeypatch.setattr(la, "_get_native_session", original)


def _scopes(s):
    return _rows(s, "SELECT e.event_type, e.visibility_scope, a.authenticated_actor_id, a.session_id, a.auth_state "
                    "FROM events e JOIN auth_contexts a USING(auth_context_id) "
                    "WHERE e.event_type IN ('human_waking_input','waking_turn') ORDER BY e.rowid")


def test_13_a_family_principals_caret_occasion_is_a_lawful_family_shared_session_with_shared_continuity_only(
        monkeypatch, tmp_path):
    """FS1 integration: the shared Caret channel is a FAMILY_SHARED surface.  The occasion binds THAT principal's
    own scoped session, commits a canonical H authored by them, and receives FAMILY_SHARED continuity with
    individual attribution -- and no PRINCIPAL_PRIVATE continuity of anyone."""
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    # Alex's PRIVATE turn (must never reach the shared channel) ...
    _run_private_owner_turn(monkeypatch, s, "ALEX-PRIVATE-QUESTION-MARKER", "ALEX-PRIVATE-PROSE-MARKER")
    # ... and Alex's SHARED Caret exchange (FAMILY_SHARED continuity) through the same channel.
    svc = _service(s, tmp_path)
    s.pass2_text = "ALEX-SHARED-PROSE-MARKER"
    _serve(s, _remote(mid=fixture_snowflake(15100), content="ALEX-SHARED-WORDS-MARKER"))
    assert svc.step()[0] == "delivered"
    p1, p2 = len(s.pass1_prompts), len(s.pass2_prompts)
    s.pass2_text = "A reply to Wife."
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Hi Clark, it's me.", mid=fixture_snowflake(15200)))
    assert svc.step()[0] == "delivered"
    prompts = "\n".join(m["content"] for pr in (s.pass1_prompts[p1:] + s.pass2_prompts[p2:]) for m in pr)
    # FAMILY_SHARED continuity is available, individually attributed to its author principal
    assert "ALEX-SHARED-WORDS-MARKER" in prompts and "ALEX-SHARED-PROSE-MARKER" in prompts
    assert f"[Author principal: {s.h.actor_id}]" in prompts
    # PRINCIPAL_PRIVATE continuity of anyone is excluded
    assert "ALEX-PRIVATE-QUESTION-MARKER" not in prompts and "ALEX-PRIVATE-PROSE-MARKER" not in prompts
    # the canonical private pair exists and is genuinely principal_private (so the exclusion is meaningful)
    private = [r for r in _scopes(s) if r[1] == fm.SCOPE_PRINCIPAL_PRIVATE]
    assert len(private) == 2
    # structurally lawful provenance for the occasion itself: H + X in THAT principal's own scoped session
    wife_rows = [r for r in _scopes(s) if r[2] == member or (r[0] == "waking_turn" and r[3].endswith(member))]
    assert [r[0] for r in wife_rows] == ["human_waking_input", "waking_turn"]
    h_row, x_row = wife_rows
    assert h_row[1] == x_row[1] == fm.SCOPE_FAMILY_SHARED and h_row[4] == "authenticated"
    # DUPLICATE-H-ACROSS-RESTART repair: session_id is now keyed by principal alone (restart-stable),
    # never by the process's own volatile session id -- see llama_anaxi._caret_shared_session.
    assert h_row[3] == x_row[3] == f"caret-shared:{member}"
    _turn, comps = _turn_components(s, 2)     # private turn, owner shared turn, then the wife's
    assert comps["caret_correspondent_principal"] == member
    (h_id,) = [r[0] for r in _rows(
        s, "SELECT h.event_id FROM events h JOIN event_components src ON src.event_id=h.event_id "
           "AND src.component_kind='discord_inbound_source' JOIN event_components c ON c.event_id=h.event_id "
           "AND c.sequence=0 WHERE c.component_text=?", "Hi Clark, it's me.")]
    assert comps["human_input_event_id"] == h_id
    # the pure family law agrees with what was delivered
    assert fm.can_receive_event(member, fm.SCOPE_FAMILY_SHARED, s.h.actor_id, fm.SCOPE_FAMILY_SHARED,
                                frozenset({member, s.h.actor_id})) is True
    assert fm.can_receive_event(member, fm.SCOPE_FAMILY_SHARED, s.h.actor_id, fm.SCOPE_PRINCIPAL_PRIVATE,
                                frozenset({member, s.h.actor_id})) is False
    # and no workspace / general-send action was offered to the shared occasion
    assert "Authorized destinations:" not in "\n".join(m["content"] for m in s.pass1_prompts[p1])
    # the ordinary owner transcript shows neither the Caret words nor the Caret prose
    shown = json.dumps(_projected(s, viewer_principal_actor_id=s.h.actor_id,
                                  viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE))
    assert "ALEX-PRIVATE-QUESTION-MARKER" in shown           # his own Mac history is still there
    assert "Hi Clark" not in shown and "ALEX-SHARED" not in shown and "A reply to Wife." not in shown


def test_2_a_mapped_owner_in_the_shared_channel_gets_shared_semantics_and_no_owner_private_leak(
        monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    _run_private_owner_turn(monkeypatch, s, "OWNER-ONLY-PRIVATE-MARKER", "OWNER-ONLY-PRIVATE-PROSE")
    svc = _service(s, tmp_path)
    s.pass2_text = "WIFE-SHARED-PROSE-MARKER"
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="WIFE-SHARED-WORDS-MARKER", mid=fixture_snowflake(15100)))
    assert svc.step()[0] == "delivered"
    p1, p2 = len(s.pass1_prompts), len(s.pass2_prompts)
    _serve(s, _remote(mid=fixture_snowflake(15300), content="Alex writing in the shared channel."))
    assert svc.step()[0] == "delivered"
    prompts = "\n".join(m["content"] for pr in (s.pass1_prompts[p1:] + s.pass2_prompts[p2:]) for m in pr)
    assert "written to you by Alex (the ANAXI owner)" in prompts
    assert "WIFE-SHARED-WORDS-MARKER" in prompts and f"[Author principal: {member}]" in prompts   # shared: yes
    assert "OWNER-ONLY-PRIVATE-MARKER" not in prompts and "OWNER-ONLY-PRIVATE-PROSE" not in prompts   # private: never
    owner_rows = [r for r in _scopes(s) if r[2] == s.h.actor_id and r[3] == f"caret-shared:{s.h.actor_id}"]
    assert [r[1] for r in owner_rows] == [fm.SCOPE_FAMILY_SHARED]            # the owner's own SHARED session
    # the shared occasion did not rebind or disturb the Mac window's own binding
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        bound = hsb.resolve_bound_human_for_session(conn, s.h.la.get_current_session_id())
    finally:
        conn.close()
    assert bound is not None and bound.authenticated_actor_id == s.h.actor_id and bound.visibility_scope is None


def test_a_failed_occasion_retries_on_the_same_h_and_never_writes_a_second_h(monkeypatch, tmp_path):
    from test_caret_wake_service import Clock, RETRY_BACKOFF_SECONDS
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    clock = Clock()
    svc = _service(s, tmp_path, clock=clock)
    s.pass2_text = ""                 # Pass 2 fails after H has committed
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Try me twice.", mid=fixture_snowflake(15400)))
    assert svc.step()[0] == "failed"
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    assert _waking_turns(s) == 0 and _delivered(s) == 0
    clock.t += RETRY_BACKOFF_SECONDS[0] + 1
    s.pass2_text = "Second attempt works."
    assert svc.step()[0] == "delivered"
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    _turn, comps = _turn_components(s)
    assert comps["human_input_event_id"] and _projected(s) == []


def test_a_failed_occasion_retries_on_the_same_h_across_a_process_restart(monkeypatch, tmp_path):
    """DUPLICATE-H-ACROSS-RESTART repair: the same invariant as the sibling test above (never a
    second H for a retry of the same inbound message), proven across a genuine PROCESS boundary --
    a fresh module-level session state, exactly like a real restart produces. Live evidence
    (2026-09-21/22): after a real production restart onto repaired HEAD 20de9b1, Blair's
    still-unanswered "I plan to, thank you" occasion (H committed at 21:52:37) got a SECOND,
    duplicate H minted for the exact same still-pending Discord message when the restarted
    process's Caret poller reached it, because _caret_shared_session() used to key its session_id
    off the CURRENT process's own freshly-generated session id -- which a restart always changes,
    so the retry-reuse lookup could never find the H a prior process had staged. session_id is now
    keyed by principal alone (restart-stable); human_session_binding.bind_session_to_registered_
    human() is already explicitly idempotent for a repeated identical binding, so re-deriving it in
    a fresh process recovers the same auth_context and the SAME reuse lookup that already protects
    in-process retries now protects across restarts too."""
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    s.pass2_text = ""                 # Pass 2 fails after H has committed
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Try me across a restart.", mid=fixture_snowflake(15500)))
    svc = _service(s, tmp_path)
    assert svc.step()[0] == "failed"
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    assert _waking_turns(s) == 0 and _delivered(s) == 0

    # Simulate a genuine process restart: a fresh process re-imports llama_anaxi, whose
    # module-level _native_session_state starts empty again, so the very next call mints a
    # brand-new session_id -- exactly what a real restart does. A fresh CaretWakeService too
    # (its own in-process attempt/backoff counters, exactly like a real restart's fresh process).
    s.h.la._native_session_state["session_id"] = None
    s.h.la._native_session_state["session_started_at"] = None
    svc2 = _service(s, tmp_path)
    s.pass2_text = "Second attempt works, after the restart."

    assert svc2.step()[0] == "delivered"
    # Still exactly one H -- the ORIGINAL one, reused, never a duplicate second H.
    (h_id,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='human_waking_input'")]
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    _turn, comps = _turn_components(s)
    assert comps["human_input_event_id"] == h_id and _projected(s) == []
    assert _waking_turns(s) == 1 and _delivered(s) == 1


def _old_scheme_caret_shared_session(s):
    """An exact model of PRE-f999b15 `_caret_shared_session`: keys session_id by the (volatile)
    process session id, and never searches for an existing H at all (its own lookup was already
    broken the same way -- this fixture only needs to reproduce the SYMPTOM, minting an H under
    the old scheme, not the old lookup's own now-irrelevant bug)."""
    def _old(caret_occasion, principal_actor_id, process_session_id, process_session_started_at):
        conn = sqlite3.connect(f"{s.h.la.PROVENANCE_DB_DIR}/anaxi_provenance.db")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            if not fm.family_feature_used(conn):
                return None, None, process_session_id, process_session_started_at
            old_session_id = f"{process_session_id}:caret-shared:{principal_actor_id}"
            authority = hsb.bind_session_to_registered_human(
                conn, session_id=old_session_id, session_started_at=process_session_started_at,
                pipeline_key=s.h.la.PIPELINE_KEY, actor_id=principal_actor_id,
                visibility_scope=fm.SCOPE_FAMILY_SHARED,
            )
            return authority, None, old_session_id, process_session_started_at
        finally:
            conn.close()
    return _old


def test_a_pre_repair_h_from_the_old_volatile_session_id_scheme_is_still_reused(monkeypatch, tmp_path):
    """Compatibility seam: Caret correspondence already in flight when the session-id scheme
    itself changes must not be orphaned. Live evidence (2026-09-22): a production restart onto
    the FIRST version of the restart-stable-session_id repair still minted a THIRD H for each of
    Alex's two already-in-flight 22:07/22:11 occasions, because their EXISTING H's (committed
    before that repair existed) carried the OLD, process-prefixed session_id scheme -- which the
    new canonical session_id string never equals. The retry-reuse lookup is now keyed on
    discord_inbound_source (this occasion's own canonical, globally-unique Discord inbound event
    id) plus principal + scope, NEVER on any particular session_id string or representation, and
    adopts whatever session the found H already, lawfully lives in -- eliminating this class of
    defect for any past OR future session-id scheme change, not just this one."""
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    s.pass2_text = ""                 # Pass 2 fails after H has committed

    # Simulate: this exact occasion was first attempted by OLD, pre-repair code, which keyed
    # session_id by the (volatile) process session id instead of by principal/source.
    real_caret_shared_session = s.h.la._caret_shared_session
    monkeypatch.setattr(s.h.la, "_caret_shared_session", _old_scheme_caret_shared_session(s))
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Pre-fix scheme message.", mid=fixture_snowflake(15600)))
    svc = _service(s, tmp_path)
    assert svc.step()[0] == "failed"
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    (old_session_id,) = [r[0] for r in _rows(
        s, "SELECT a.session_id FROM events h JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
           "WHERE h.event_type='human_waking_input'")]
    assert old_session_id != f"caret-shared:{member}"    # genuinely the old, process-prefixed scheme
    (h_id,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='human_waking_input'")]

    # "Deploy the repair" and "restart": restore the repaired lookup, reset the process session state.
    monkeypatch.setattr(s.h.la, "_caret_shared_session", real_caret_shared_session)
    s.h.la._native_session_state["session_id"] = None
    s.h.la._native_session_state["session_started_at"] = None
    svc2 = _service(s, tmp_path)
    s.pass2_text = "Recovered under the repaired scheme."

    assert svc2.step()[0] == "delivered"
    # Still exactly one H -- the ORIGINAL, old-scheme one, reused -- never a second (or third) H.
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    _turn, comps = _turn_components(s)
    assert comps["human_input_event_id"] == h_id and _projected(s) == []
    assert _waking_turns(s) == 1 and _delivered(s) == 1
    # the X's own auth context also lives in that ORIGINAL old-scheme session -- adopted, not migrated
    (turn_session_id,) = [r[0] for r in _rows(
        s, "SELECT a.session_id FROM events x JOIN auth_contexts a ON a.auth_context_id=x.auth_context_id "
           "WHERE x.event_type='waking_turn'")]
    assert turn_session_id == old_session_id


def test_two_historical_duplicate_hs_resolve_deterministically_to_the_earliest_one(monkeypatch, tmp_path):
    """Production already contains, for TWO of Alex's still-pending Caret messages, exactly this
    shape: two historical human_waking_input events for the one exact canonical Discord inbound
    source (an earlier host defect minted both). Models that shape directly -- two pre-existing,
    never-answered H's for the same inbound event, both under the OLD volatile scheme -- and
    proves the repaired lookup deterministically resolves to the EARLIEST one (`ORDER BY h.rowid
    ASC`, and `events` is append-only so rowid IS insertion order -- never an unspecified "first
    matching row"), mints no third H, the eventual canonical X binds to that earliest H, and
    exactly one outward delivery results. A second, textually DIFFERENT inbound message from the
    same principal is never collapsed into this one."""
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    s.pass2_text = ""                 # every attempt fails until the final, deliberately last one

    real_caret_shared_session = s.h.la._caret_shared_session
    monkeypatch.setattr(s.h.la, "_caret_shared_session", _old_scheme_caret_shared_session(s))
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Duplicated by the old defect.", mid=fixture_snowflake(15700)))
    svc_a = _service(s, tmp_path)
    assert svc_a.step()[0] == "failed"
    # A second "restart", still on the old, pre-repair code: mints the SECOND historical duplicate
    # H for the exact same still-pending Discord message -- reproducing production's own shape.
    s.h.la._native_session_state["session_id"] = None
    s.h.la._native_session_state["session_started_at"] = None
    svc_b = _service(s, tmp_path)
    assert svc_b.step()[0] == "failed"
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 2
    (earliest_h, latest_h) = [r[0] for r in _rows(
        s, "SELECT event_id FROM events WHERE event_type='human_waking_input' ORDER BY rowid ASC")]

    # An unrelated, textually different message from the SAME principal, staged but never woken
    # (the duplicated message above is still the oldest undelivered pending item, so FIFO would
    # only ever re-attempt IT next) -- must never be collapsed into either duplicate above.
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="A completely different message.", mid=fixture_snowflake(15800)))
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 2   # unchanged

    # "Deploy the repair" and restart onto it: the duplicated message's retry must resolve
    # deterministically to the EARLIEST of its two historical H's, minting no third.
    monkeypatch.setattr(s.h.la, "_caret_shared_session", real_caret_shared_session)
    s.h.la._native_session_state["session_id"] = None
    s.h.la._native_session_state["session_started_at"] = None
    svc_d = _service(s, tmp_path)
    s.pass2_text = "Finally recovered, deterministically."
    assert svc_d.step()[0] == "delivered"

    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 2  # no third H
    _turn, comps = _turn_components(s, index=0)
    assert comps["human_input_event_id"] == earliest_h        # the EARLIEST duplicate, not the latest
    assert latest_h != earliest_h
    assert _waking_turns(s) == 1 and _delivered(s) == 1        # exactly one delivery for the duplicated message

    # the unrelated different message is still separately, lawfully pending -- never merged in
    remaining_pending = s.h.la.discord_correspondence.next_pending_inbound(
        s.data_dir, limit=s.h.la.discord_correspondence.MAX_PENDING_INBOUND_LIMIT)
    assert len(remaining_pending) == 1
    assert remaining_pending[0]["content"] == "A completely different message."


def test_15_a_shared_channel_is_never_described_as_private(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    everything = _all_prompt_text(s)
    assert "shared correspondence surface" in _last_pass2_text(s)
    assert "read by everyone with access to that channel" in _last_pass2_text(s)
    assert "your private channel" not in everything
    assert "private Discord correspondence" not in everything and "Private Discord correspondence" not in everything


def test_16_the_outbound_reply_retains_exact_inbound_principal_and_destination_provenance(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    s.pass2_text = "A reply to the family member."
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Hello Clark."))
    assert _service(s, tmp_path).step()[0] == "delivered"
    turn, comps = _turn_components(s)
    (inbound_id,) = [r[0] for r in _rows(
        s, "SELECT event_id FROM events WHERE event_type='discord_inbound_message'")]
    inbound_meta = json.loads(_rows(
        s, "SELECT component_text FROM event_components WHERE event_id=? AND component_kind='discord_inbound_metadata'",
        inbound_id)[0][0])
    assert comps["discord_inbound_carriage"] == inbound_id
    assert comps["caret_correspondent_principal"] == member
    assert comps["caret_reply_route_intent"] == "reply_through_caret"
    assert comps["discord_destination_id"] == inbound_meta["destination_id"] == s.destination_id
    assert inbound_meta["author_id"] == MEMBER_AUTHOR
    (outward,) = _rows(s, "SELECT event_id FROM events WHERE event_type='clark_outward_act' AND input_source_ref=?", turn)
    recipient = _rows(
        s, "SELECT component_text FROM event_components WHERE event_id=? AND component_kind='outward_recipient_reference'",
        outward[0])[0][0]
    assert recipient == dc._recipient_reference(s.destination_id)
    assert len(s.fake.posts) == 1 and s.fake.posts[0][1].endswith(f"/channels/{SNOWFLAKE}/messages")
    # the shared destination authority was not widened: the destination set is unchanged
    assert [d["destination_id"] for d in dc.list_authorized_destinations(s.data_dir)] == [s.destination_id]


def test_17_a_multipart_reply_from_a_mapped_principal_remains_exact_and_self_echo_safe(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    body = ("Paragraph one. " * 90).strip() + "\n\n" + ("Paragraph two. " * 90).strip()   # > 2000, < 4000
    s.pass2_text = body
    s.pass1_script.append(dict(ROUTE))
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Tell me a lot."))
    svc = _service(s, tmp_path)
    assert svc.step()[0] == "delivered"
    posts = [json.loads(p[2])["content"] for p in s.fake.requests if p[0] == "POST"]
    assert len(posts) == 2 and "".join(posts) == body
    _turn, comps = _turn_components(s)
    assert comps["caret_correspondent_principal"] == member and comps["discord_message_text"] == body
    # Clark's own confirmed parts echo back through the channel and never wake him (author id irrelevant)
    echo_ids = _rows(s, "SELECT discord_message_id FROM discord_outbound_part_attempts WHERE status='succeeded'")
    assert len(echo_ids) >= 1
    echoes = [{"id": r[0], "channel_id": SNOWFLAKE, "content": "echo", "author": {"id": MEMBER_AUTHOR, "username": "x"},
               "timestamp": "2026-09-21T00:00:00+00:00"} for r in echo_ids if r[0]]
    _serve(s, *echoes)
    before = _waking_turns(s)
    assert svc.step()[0] == "idle" and _waking_turns(s) == before


# ===================================================================== owner UI / status: read-only

def test_18_rendering_the_mapping_ui_and_status_has_no_model_network_or_write_side_effects(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _serve(s, _from(STRANGER_AUTHOR, "stranger"))
    _service(s, tmp_path).step()
    before = [_count(s, f"SELECT COUNT(*) FROM {t}") for t in (
        "events", "event_components", "discord_author_mapping_events", "outward_projection_attempts")]

    def boom(*a, **k):
        raise AssertionError("rendering must not touch the network or a model")

    monkeypatch.setattr(net, "_default_request", boom)
    monkeypatch.setattr(s.h.la.ollama, "chat", boom)
    cca.load_view(s.data_dir)
    dam.list_mappings(s.data_dir)
    dam.assignable_principals(s.data_dir)
    cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    after = [_count(s, f"SELECT COUNT(*) FROM {t}") for t in (
        "events", "event_components", "discord_author_mapping_events", "outward_projection_attempts")]
    assert before == after
    # source-level: the GUI's render path is the read-only view; only the explicit buttons write
    gui = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama_gui.py"), encoding="utf-8").read()
    body = gui[gui.index("def load_caret_correspondents"):gui.index("def map_caret_correspondent")]
    assert "load_view" in body and "map_author" not in body and "ollama" not in body and "net." not in body
    tree = ast.parse(gui)
    writers = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and any(isinstance(c, ast.Attribute) and c.attr in ("map_author", "revoke_author")
                       for c in ast.walk(n))}
    assert writers == {"map_caret_correspondent", "revoke_caret_correspondent"}


# ===================================================================== route intent (new turns)

def test_19_canonical_route_intent_distinguishes_no_route_unusable_and_sent(monkeypatch, tmp_path):
    def run(text, script):
        s = _setup(monkeypatch, tmp_path / text[:1] / str(len(text)))
        s.pass2_text = text
        if script:
            s.pass1_script.append(dict(ROUTE))
        _serve(s, _remote())
        assert _service(s, tmp_path / text[:1] / str(len(text))).step()[0] == "delivered"
        _turn, comps = _turn_components(s)
        st = cos.caret_owner_status(s.data_dir)[0]      # NO trace: the state is canonical
        return s, comps, st

    s0, none, st0 = run("no route chosen", False)
    assert "caret_reply_route_intent" not in none and "discord_correspondence_request" not in none
    assert st0["code"] == cos.NO_OUTBOUND_REPLY_SELECTED

    s1, unusable, st1 = run("U" * 6001, True)
    assert unusable["caret_reply_route_intent"] == "reply_through_caret"
    assert "discord_correspondence_request" not in unusable and s1.fake.posts == []
    assert st1["code"] in (cos.REPLY_BODY_UNUSABLE, cos.REPLY_TRANSPORT_CAP_EXCEEDED)   # chosen, nothing sent

    s2, sent, st2 = run("sent words", True)
    assert sent["caret_reply_route_intent"] == "reply_through_caret"
    assert sent["discord_correspondence_request"] == "reply_through_caret"
    assert st2["code"] == cos.REPLY_SENT and len(s2.fake.posts) == 1


def test_route_intent_is_never_fabricated_for_historical_turns_and_needs_an_attributed_occasion():
    import native_provenance_writer as npw
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_caret_occasion_attribution(None, True, "none", [])
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_caret_occasion_attribution("p", False, "reply_through_caret", ["e"])
    npw._validate_caret_occasion_attribution("p", True, "reply_through_caret", ["e"])


# ===================================================================== wiring pins

def test_a_mac_turn_carries_only_the_owners_own_caret_messages():
    la = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama_anaxi.py"), encoding="utf-8").read()
    assert 'p.get("correspondent") or {}).get("role") == discord_author_mapping.ROLE_OWNER' in la


# ===================================================================== prospective mapping (no backfill)

_DISCORD_EPOCH_MS = 1420070400000


def _snow(unix_seconds, worker=1):
    return str((((unix_seconds * 1000) - _DISCORD_EPOCH_MS) << 22) | worker)


def _destination_authorized_at(s):
    return dc.resolve_destination(s.data_dir, s.destination_id)["authorized_at"]


def _map_at(s, author, principal, host_time, label="Later-bound"):
    """Owner binds `author` at a controlled HOST time (the recorded binding time is the boundary)."""
    from unittest.mock import patch
    with patch.object(dam.time, "time", return_value=host_time):
        return dam.map_author(s.data_dir, discord_author_id=author, principal_actor_id=principal,
                              requester_actor_id=s.h.actor_id, occurred_at=1, display_label=label)


def test_3_a_message_from_a_then_unmapped_author_is_never_woken_on_after_that_author_is_mapped(
        monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    a = _destination_authorized_at(s)
    t1, t2, t3 = a + 100, a + 200, a + 300
    svc = _service(s, tmp_path)
    old = _from(STRANGER_AUTHOR, "stranger", content="ARRIVED-BEFORE-MAPPING-MARKER", mid=_snow(t1))
    _serve(s, old)
    assert svc.step()[0] == "idle"                                   # T1: unauthorized at arrival
    (held,) = dc.held_inbound(s.data_dir)
    assert held["reason"] == dam.STATUS_UNMAPPED
    _map_at(s, STRANGER_AUTHOR, s.h.actor_id, t2)                    # T2: the owner binds the id (to Alex)
    _serve(s, old)
    assert svc.step()[0] == "idle"                                   # T3: still nothing
    assert _waking_turns(s) == 0 and _delivered(s) == 0 and s.pass1_prompts == [] and s.pass2_prompts == []
    assert "ARRIVED-BEFORE-MAPPING-MARKER" not in _all_prompt_text(s)
    # the historical event is neither deleted nor reclassified as authorized: it is still undelivered,
    # and the owner sees it as unauthorized-at-arrival (troubleshooting), never as actionable
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='discord_inbound_message'") == 1
    (held,) = dc.held_inbound(s.data_dir)
    assert held["reason"] == dam.STATUS_BEFORE_MAPPING and "unauthorized at arrival" in held["reason_text"]
    (status,) = cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    assert status["code"] == cos.AUTHOR_NOT_DELIVERABLE and "arrived before its author was mapped" in status["summary"]
    assert dc.next_pending_inbound(s.data_dir) == []


def test_4_the_same_author_sending_after_the_mapping_may_lawfully_wake_clark(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    a = _destination_authorized_at(s)
    t1, t2, t4 = a + 100, a + 200, a + 400
    svc = _service(s, tmp_path)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="BEFORE-MARKER", mid=_snow(t1)))
    assert svc.step()[0] == "idle"
    _map_at(s, STRANGER_AUTHOR, s.h.actor_id, t2)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="AFTER-MARKER", mid=_snow(t4)),
           _from(STRANGER_AUTHOR, "stranger", content="BEFORE-MARKER", mid=_snow(t1)))
    assert svc.step()[0] == "delivered"
    assert _waking_turns(s) == 1
    text = _last_prompt(s)
    assert "AFTER-MARKER" in text and "BEFORE-MARKER" not in _all_prompt_text(s)
    assert svc.step()[0] == "idle"          # the old one is still not eligible on the next tick either
    assert [h["reason"] for h in dc.held_inbound(s.data_dir)] == [dam.STATUS_BEFORE_MAPPING]


def test_5_a_revoked_mapping_fails_closed_for_every_later_message_too(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    a = _destination_authorized_at(s)
    _map_at(s, STRANGER_AUTHOR, s.h.actor_id, a + 50)
    svc = _service(s, tmp_path)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="WHILE-MAPPED", mid=_snow(a + 100)))
    assert svc.step()[0] == "delivered"
    dam.revoke_author(s.data_dir, discord_author_id=STRANGER_AUTHOR, requester_actor_id=s.h.actor_id,
                      occurred_at=9)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="AFTER-REVOKE", mid=_snow(a + 900)))
    assert svc.step()[0] == "idle" and _waking_turns(s) == 1
    assert "AFTER-REVOKE" not in _all_prompt_text(s)
    assert [h["reason"] for h in dc.held_inbound(s.data_dir)] == [dam.STATUS_REVOKED]
    # a re-mapping is prospective again: it does not resurrect what arrived while revoked
    _map_at(s, STRANGER_AUTHOR, s.h.actor_id, a + 1000)
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="AFTER-REVOKE", mid=_snow(a + 900)))
    assert svc.step()[0] == "idle" and _waking_turns(s) == 1


def _legacy_shaped_delivery(s, tmp_path, inbound_id, at):
    """Deliver an inbound event exactly as the pre-binding system did: a committed waking turn carrying it
    (canonical carriage), with NO correspondent-principal component -- the shape of production's history."""
    import native_provenance_writer as npw
    la = s.h.la
    kardia = {"moral_valve": "m", "volitional_channel": "v", "affective_stance": "a", "aesthetic_valve": "e"}
    controls = {"identity_preamble": "i", "raw_kardia": dict(kardia), "style_instruction": "s",
                "temperature": 0.4, "top_p": 0.85}
    result = npw.stage_and_record_native_waking_turn(
        la.PROVENANCE_DB_DIR, str(tmp_path / "legacy_staging.jsonl"),
        session_id="sess-legacy-history", session_started_at=at - 10, user_id="nate", prompt="p",
        bounded_clause="", clark_prose="Old local prose.", kardia=kardia, controls=controls,
        waking_model_tag="m", pipeline_key=la.PIPELINE_KEY, artifact_pass_ran=False, occurred_at=at,
        interaction_mode="conversation", delivered_discord_inbound_event_ids=[inbound_id])
    dc.record_inbound_delivered(la.PROVENANCE_DB_DIR, inbound_event_id=inbound_id,
                                waking_turn_event_id=result["event_id"], occurred_at=at)
    return result["event_id"]


def test_6_historical_reads_keep_the_event_time_fact_and_add_the_later_dated_identity(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    a = _destination_authorized_at(s)
    t1, t2 = a + 100, a + 500
    # (a) historical, delivered while the author was only a Discord id + username (the shape of production's
    #     20 existing events): staged, then carried by a legacy-shaped turn with no correspondent principal.
    legacy_author = "555000000000000005"
    dc.ingest_inbound_messages(
        s.data_dir, destination_id=s.destination_id, occurred_at=t1,
        messages=[_from(legacy_author, "old_handle", content="LEGACY-WORDS", mid=_snow(t1))])
    (legacy_inbound,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='discord_inbound_message'")]
    legacy_turn = _legacy_shaped_delivery(s, tmp_path, legacy_inbound, t1 + 5)
    # (b) a message that was never delivered because its author was unmapped at arrival
    _serve(s, _from(STRANGER_AUTHOR, "stranger", content="NEVER-DELIVERED-WORDS", mid=_snow(t1 + 1)))
    assert _service(s, tmp_path).step()[0] == "idle"
    # before any binding: only the event-time fact
    before = dc.historical_inbound_attribution(s.data_dir, legacy_inbound)
    assert before["event_time"] == {"status": "unverified"} and before["later_binding"] is None
    # the owner later binds both ids (to Alex), at T2
    _map_at(s, legacy_author, s.h.actor_id, t2, label="Alex")
    _map_at(s, STRANGER_AUTHOR, s.h.actor_id, t2, label="Alex")
    snapshot = _rows(s, "SELECT event_id, event_type, occurred_at FROM events ORDER BY rowid")
    comps_before = _rows(s, "SELECT event_id, component_kind, component_text FROM event_components ORDER BY component_id")
    hist = dc.historical_inbound_attribution(s.data_dir, legacy_inbound)
    assert hist["event_time"] == {"status": "unverified"}                    # never rewritten to "known then"
    assert hist["carrying_waking_turn_event_id"] == legacy_turn
    assert hist["discord_author_id"] == legacy_author and hist["discord_username_at_event_time"] == "old_handle"
    later = hist["later_binding"]
    assert later["principal_actor_id"] == s.h.actor_id and later["display_label"] == "Alex"
    assert later["role"] == "owner" and later["binding_recorded_at"] == t2 and later["current_state"] == "mapped"
    assert later["mapping_event_id"]
    text = dc.render_historical_attribution(hist)
    assert "had not bound" in text and "unverified" in text and "Later, at" in text and "Alex" in text
    assert "did not exist when the message arrived" in text
    assert "ANAXI knew" not in text and "was Alex" not in text               # no retroactive claim, no meaning imposed
    undelivered = dc.historical_inbound_attribution(
        s.data_dir, [r[0] for r in _rows(
            s, "SELECT e.event_id FROM events e JOIN event_components c USING(event_id) "
               "WHERE e.event_type='discord_inbound_message' AND c.component_kind='discord_inbound_content' "
               "AND c.component_text='NEVER-DELIVERED-WORDS'")][0])
    assert undelivered["event_time"] == {"status": "not_delivered"} and undelivered["later_binding"]["binding_recorded_at"] == t2
    assert "not delivered to you" in dc.render_historical_attribution(undelivered)
    # reading rewrote nothing, and mapping did not alter any event-time record
    assert _rows(s, "SELECT event_id, event_type, occurred_at FROM events ORDER BY rowid") == snapshot
    assert _rows(s, "SELECT event_id, component_kind, component_text FROM event_components ORDER BY component_id")[:len(comps_before)] == comps_before
    # a later revocation is reported as such, still without touching the event
    dam.revoke_author(s.data_dir, discord_author_id=legacy_author, requester_actor_id=s.h.actor_id, occurred_at=99)
    assert dc.historical_inbound_attribution(s.data_dir, legacy_inbound)["later_binding"]["current_state"] == "revoked"
    # an attributed-at-the-time exchange (new turn) reports that, and adds no "later" identity
    new_author_time = _map_at(s, "444000000000000004", s.h.actor_id, a + 600)
    _serve(s, _from("444000000000000004", "later_user", content="NEW-WORDS", mid=_snow(a + 700)))
    assert _service(s, tmp_path).step()[0] == "delivered"
    (new_inbound,) = [r[0] for r in _rows(
        s, "SELECT e.event_id FROM events e JOIN event_components c USING(event_id) "
           "WHERE e.event_type='discord_inbound_message' AND c.component_kind='discord_inbound_content' "
           "AND c.component_text='NEW-WORDS'")]
    new = dc.historical_inbound_attribution(s.data_dir, new_inbound)
    assert new["event_time"]["status"] == "canonically_attributed" and new["later_binding"] is None
    assert new_author_time["discord_author_id"] == "444000000000000004"


def test_session_started_at_is_restart_stable_not_the_fresh_process_clock(monkeypatch, tmp_path):
    """SESSION-STARTED-AT RESTART-STABILITY repair (production incident, 2026-09-22, H
    01M354AVC7KTK64819V6GF5RFE): reproduces the actual production defect class, not merely an
    approximation of it -- the sibling restart test above happens to run both `.step()` calls
    within the same wall-clock second, so `int(datetime.now(...))` yields the SAME value both
    times and never actually exercises a real mismatch. Here the second (post-"restart")
    process's own session_started_at is forced to a DIFFERENT value than the first, exactly
    like two real launcher runs minutes apart -- the live failure: canonical persistence
    (native_provenance_writer._resolve_or_create_session) verifies started_at exactly against
    the `sessions` row an already-reused session_id was first created with, and a caller that
    keeps using its OWN fresh process-local started_at for a session_id it did NOT originate
    raised MigrationStopCondition on the very first turn that ever reached persistence for a
    restart-stable session_id (nothing before this repair had -- the expression-boundary defect
    this same day masked it until Pass 2 could ever succeed at all)."""
    s = _setup(monkeypatch, tmp_path)
    member = _enter_family_mode(s)
    _map_member(s, member)
    s.pass2_text = ""                 # Pass 2 fails after H has committed -- H exists, no X yet
    _serve(s, _from(MEMBER_AUTHOR, "wife_handle", content="Across a restart, different clock.", mid=fixture_snowflake(99900)))
    svc = _service(s, tmp_path)
    assert svc.step()[0] == "failed"
    first_started_at = s.h.la._native_session_state["session_started_at"]
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1

    # Simulate a genuine restart with a DIFFERENT wall clock reading -- never the same second
    # the first process happened to mint its own (now-irrelevant) session under.
    s.h.la._native_session_state["session_id"] = s.h.la.native_provenance_writer.generate_native_ulid()
    s.h.la._native_session_state["session_started_at"] = first_started_at + 600
    assert s.h.la._native_session_state["session_started_at"] != first_started_at
    svc2 = _service(s, tmp_path)
    s.pass2_text = "Second attempt works, after the restart, on a different clock."

    outcome, _delay = svc2.step()
    assert outcome == "delivered"        # not "failed" with MigrationStopCondition
    (h_id,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='human_waking_input'")]
    assert _count(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'") == 1
    _turn, comps = _turn_components(s)
    assert comps["human_input_event_id"] == h_id and _projected(s) == []
    assert _waking_turns(s) == 1 and _delivered(s) == 1
    # The canonical `sessions` row keeps its TRUE original started_at -- the fresh process's
    # own (later, different) clock reading was never written over it or used to persist.
    (session_id,) = [r[0] for r in _rows(
        s, "SELECT a.session_id FROM events h JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
           "WHERE h.event_type='human_waking_input'")]
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        (stored_started_at,) = conn.execute(
            "SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    assert stored_started_at == first_started_at
