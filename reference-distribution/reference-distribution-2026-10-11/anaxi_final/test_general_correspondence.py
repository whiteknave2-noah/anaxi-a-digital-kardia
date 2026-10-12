"""General correspondence (Step 2) through the real waking seam: an unanticipated correspondent on an
owner-opened surface, Clark-controlled standing, scoped continuity, reply / follow-up / independent
initiation, revocation, owner safety boundaries, isolation, restart.  Real run_waking_turn, canonical
writer and ledgers; only the model and the Discord transport are faked.

Countermodels named per claim (a green here must not be satisfiable by a wrong system):
  * "unknown source converses" would also pass if the host silently mapped the source to a principal
    -> asserted: no human H exists, no principal component, X scope NULL, correspondent ref recorded.
  * "standing grants continuity" would also pass if continuity were global -> asserted with a SECOND
    correspondent and an owner turn that both must NOT see it.
  * "Clark controls standing" would also pass if any caller could write the ledger -> asserted the DB
    rejects a non-Clark requester and a turn that never carried the typed choice.
  * "initiation requires standing" would also pass if nothing were ever sent -> asserted the lawful case
    posts exactly once AND the unlawful cases post nothing and are disclosed.
"""
import json
import sqlite3
import time

import pytest

import correspondence_surfaces as cs
import discord_correspondence as dc
import family_membership as fm
import human_session_binding as hsb
from test_caret_correspondent_binding import _enter_family_mode, _from, _snow
from test_caret_inbound_seam import fixture_snowflake
from test_caret_wake_service import ROUTE, _rows, _serve, _service, _setup

COLE = "900000000000000001"
ROBO = "900000000000000002"


def _open_surface(s, *, initiation=False):
    cs.set_surface_policy(s.data_dir, destination_id=s.destination_id, allow_unknown_sources=True,
                          permit_independent_initiation=initiation, requester_actor_id=s.h.actor_id,
                          occurred_at=int(time.time()), destination_authorized=True)


def _world(monkeypatch, tmp_path, *, initiation=False):
    s = _setup(monkeypatch, tmp_path)
    _enter_family_mode(s)
    _open_surface(s, initiation=initiation)
    return s, _service(s, tmp_path)


def _occasion(s, svc, author, name, words, mid_offset, **pass1):
    s.pass1_script.append(dict(pass1))
    _serve(s, _from(author, name, content=words, mid=fixture_snowflake(mid_offset)))
    return svc.step()[0]


def _last_turn(s):
    turn = _rows(s, "SELECT event_id FROM events WHERE event_type='waking_turn' ORDER BY rowid DESC LIMIT 1")[0][0]
    return turn, dict(_rows(s, "SELECT component_kind, component_text FROM event_components WHERE event_id=?", turn))


def _all_text(prompts):
    return "\n".join(m["content"] for p in prompts for m in p)


def _owner_private_turn(monkeypatch, s, text, **pass1):
    la = s.h.la
    alt, started = "sess-owner-private-gc", 100
    conn = sqlite3.connect(s.h.db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    auth = hsb.bind_session_to_registered_human(
        conn, session_id=alt, session_started_at=started, pipeline_key=la.PIPELINE_KEY,
        actor_id=s.h.actor_id, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE)
    conn.close()
    original = la._get_native_session
    monkeypatch.setattr(la, "_get_native_session", lambda: (alt, started))
    try:
        s.pass1_script.append(dict(pass1))
        _serve(s)
        return la.run_waking_turn(la.AnaxiOrchestrator(), text, interaction_mode="conversation",
                                  human_input_authority=auth)
    finally:
        monkeypatch.setattr(la, "_get_native_session", original)


# ------------------------------------------------------------------ surfaces are doors, owner-controlled

def test_a_closed_surface_holds_an_unknown_source_exactly_as_before(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _enter_family_mode(s)
    svc = _service(s, tmp_path)
    assert _occasion(s, svc, COLE, "cole", "CLOSED-SURFACE-MARKER", 15100) == "idle"
    (held,) = dc.held_inbound(s.data_dir)
    assert held["reason"] == "unmapped" and "CLOSED-SURFACE-MARKER" not in _all_text(s.pass1_prompts)


def test_opening_a_surface_needs_family_scope_and_the_owner(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    with pytest.raises(cs.CorrespondenceError):          # legacy mode: nothing would isolate them
        _open_surface(s)
    _enter_family_mode(s)
    with pytest.raises(cs.CorrespondenceError):          # not the owner
        cs.set_surface_policy(s.data_dir, destination_id=s.destination_id, allow_unknown_sources=True,
                              permit_independent_initiation=False, requester_actor_id="actor-someone-else",
                              occurred_at=1, destination_authorized=True)


def test_opening_is_prospective(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    _enter_family_mode(s)
    authorized_at = dc.resolve_destination(s.data_dir, s.destination_id)["authorized_at"]
    earlier = _snow(authorized_at + 50)                  # after the door existed, before it was opened
    with monkeypatch.context() as m:
        m.setattr(cs.time, "time", lambda: authorized_at + 100)
        _open_surface(s)
    svc = _service(s, tmp_path)
    _serve(s, _from(COLE, "cole", content="BEFORE-OPENING-MARKER", mid=earlier))
    assert svc.step()[0] == "idle"
    (held,) = dc.held_inbound(s.data_dir)
    assert held["reason"] == dc.HELD_NOT_ADMITTED


# ------------------------------------------------------------------ an unanticipated correspondent

def test_an_unknown_source_converses_without_becoming_a_principal(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    assert _occasion(s, svc, COLE, "cole", "Hi Clark, I'm Cole.", 15100) == "delivered"
    pass1 = _all_text(s.pass1_prompts[-1:])
    assert f"stable Discord user id {COLE}" in pass1 and "unverified" in pass1 and "current standing: NONE" in pass1
    assert "correspondent_standing_request: establish_standing" in pass1
    turn, comps = _last_turn(s)
    assert comps["correspondent_source_ref"] == f"discord_user:{COLE}"
    assert "caret_correspondent_principal" not in comps and "human_input_event_id" not in comps
    assert _rows(s, "SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'")[0][0] == 0
    assert _rows(s, "SELECT visibility_scope FROM events WHERE event_id=?", turn)[0][0] is None
    assert _rows(s, "SELECT COUNT(*) FROM event_components WHERE component_kind='discord_inbound_delivered'")[0][0] == 1


def test_without_standing_each_encounter_stands_alone(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    _occasion(s, svc, COLE, "cole", "FIRST-WORDS-MARKER", 15100)
    s.pass2_text = "SECOND-REPLY"
    _occasion(s, svc, COLE, "cole", "Remember me?", 15200)
    assert "FIRST-WORDS-MARKER" not in _all_text(s.pass2_prompts[-1:])


def test_clarks_standing_choice_binds_continuity_with_that_source_only(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    s.pass2_text = "Nice to meet you, Cole."
    assert _occasion(s, svc, COLE, "cole", "COLE-ESTABLISHING-WORDS", 15100,
                     correspondent_standing_request="establish_standing") == "delivered"
    turn, comps = _last_turn(s)
    assert comps["correspondent_standing_request"] == f"establish:discord_user:{COLE}"
    conn = sqlite3.connect(s.h.db_path)
    try:
        state = cs.standing(conn, "discord_user", COLE)
    finally:
        conn.close()
    assert state["state"] == cs.STANDING_ESTABLISHED and state["history"][-1]["waking_turn_event_id"] == turn
    # the next occasion from Cole carries the establishing exchange as continuity
    _occasion(s, svc, COLE, "cole", "What did I say before?", 15200)
    window = s.pass2_prompts[-1]
    assert {"role": "user", "content": "COLE-ESTABLISHING-WORDS"} in window
    assert {"role": "assistant", "content": "Nice to meet you, Cole."} in window
    assert "current standing: ESTABLISHED" in _all_text(s.pass1_prompts[-1:])
    # another correspondent never sees it ...
    _occasion(s, svc, ROBO, "cole", "I am someone else with the same display name.", 15300)
    assert "COLE-ESTABLISHING-WORDS" not in _all_text(s.pass1_prompts[-1:] + s.pass2_prompts[-1:])
    # ... and neither does the owner
    _owner_private_turn(monkeypatch, s, "What have you been up to?")
    assert "COLE-ESTABLISHING-WORDS" not in _all_text(s.pass1_prompts[-1:] + s.pass2_prompts[-1:])


def test_the_standing_ledger_is_clark_only_and_bound_to_his_typed_choice(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    _occasion(s, svc, COLE, "cole", "hello", 15100)                 # a turn WITHOUT the typed choice
    turn, _ = _last_turn(s)
    with pytest.raises(sqlite3.IntegrityError):
        cs.record_standing_act(s.data_dir, kind="discord_user", value=COLE, action="establish",
                               waking_turn_event_id=turn, occurred_at=int(time.time()))
    conn = sqlite3.connect(s.h.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO correspondent_standing_events VALUES ('x','discord_user',?, 'establish', "
                         "'actor-owner', ?, 1, 1)", (COLE, turn))
    finally:
        conn.close()


def test_owner_identity_binding_informs_but_never_grants(monkeypatch, tmp_path):
    """Identity (Step 1) and standing are separate axes: the owner may later record who a source is;
    that re-contextualizes but grants no standing, principal role or authority."""
    import canonical_person
    s, svc = _world(monkeypatch, tmp_path)
    conn = sqlite3.connect(s.h.db_path)
    principals_before = fm.active_family_principals(conn)
    actors_before = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
    conn.close()
    person = canonical_person.create_person(s.data_dir, label="Cole", entity_kind="human",
                                            requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    canonical_person.bind_identifier(s.data_dir, person_id=person["person_id"], identifier_kind="discord_author_id",
                                     identifier_value=COLE, requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    assert _occasion(s, svc, COLE, "cole", "hello", 15100) == "delivered"
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert cs.standing(conn, "discord_user", COLE)["state"] == cs.STANDING_NONE      # identity != standing
        assert fm.active_family_principals(conn) == principals_before                      # identity != authority
        assert conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == actors_before  # never an ANAXI actor
    finally:
        conn.close()
    _turn, comps = _last_turn(s)
    assert "caret_correspondent_principal" not in comps
    shown = _all_text(s.pass1_prompts[-1:])
    assert 'recorded this Discord user id as canonical person "Cole"' in shown and "grants no authority" in shown


# ------------------------------------------------------------------ reply / follow-up / initiation

def test_reply_and_null_on_a_source_occasion(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    s.pass2_text = "Hello Cole."
    assert _occasion(s, svc, COLE, "cole", "hi", 15100, **ROUTE) == "delivered"
    assert [json.loads(p[2])["content"] for p in s.fake.posts] == ["Hello Cole."]
    assert _occasion(s, svc, COLE, "cole", "you there?", 15200, reply_request="no_reply") == "delivered"
    assert len(s.fake.posts) == 1                                    # Clark's own silence: nothing sent


def test_follow_up_after_silence_is_a_reply_on_a_later_occasion_and_needs_no_standing(monkeypatch, tmp_path):
    """Design case B: Clark stays silent, the source writes again, Clark answers then.  Same authority as a
    reply (the later occasion), not initiation: no standing, no surface initiation grant."""
    s, svc = _world(monkeypatch, tmp_path, initiation=False)
    assert _occasion(s, svc, COLE, "cole", "first", 15100, reply_request="no_reply") == "delivered"
    assert s.fake.posts == []
    s.pass2_text = "Sorry for the silence, Cole."
    assert _occasion(s, svc, COLE, "cole", "still around?", 15200, **ROUTE) == "delivered"
    assert [json.loads(p[2])["content"] for p in s.fake.posts] == ["Sorry for the silence, Cole."]
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert cs.standing(conn, "discord_user", COLE)["state"] == cs.STANDING_NONE
    finally:
        conn.close()


def test_independent_initiation_requires_standing_and_owner_policy(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=True)
    _occasion(s, svc, COLE, "cole", "hi", 15100)                    # encountered, no standing yet
    send = dict(discord_correspondence_request="send_message", discord_destination_id=s.destination_id,
                discord_correspondent_ref=f"discord_user:{COLE}", discord_message_text="Thinking of you, Cole.")
    result = _owner_private_turn(monkeypatch, s, "Anything on your mind?", **send)
    assert s.fake.posts == [] and "no correspondent standing" in result["correspondence"]["notes"][0]
    assert "no correspondent standing" in _all_text(s.pass2_prompts[-1:])          # disclosed to Clark
    _occasion(s, svc, COLE, "cole", "let's keep talking", 15200, correspondent_standing_request="establish_standing")
    menu = _all_text(s.pass1_prompts[-1:])
    _owner_private_turn(monkeypatch, s, "Anything on your mind?", **send)
    assert f"discord_user:{COLE}" in _all_text(s.pass1_prompts[-1:])               # listed to him by stable id
    assert [json.loads(p[2])["content"] for p in s.fake.posts] == ["Thinking of you, Cole."]
    turn, comps = _last_turn(s)
    assert comps["discord_correspondent_ref"] == f"discord_user:{COLE}"
    # revocation ends prospective initiation
    _occasion(s, svc, COLE, "cole", "bye", 15300, correspondent_standing_request="revoke_standing")
    result = _owner_private_turn(monkeypatch, s, "Anything else?", **send)
    assert len(s.fake.posts) == 1 and result["correspondence"]["notes"]
    assert menu  # (the occasion menu existed)


def test_initiation_is_refused_where_the_owner_did_not_permit_it(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=False)
    _occasion(s, svc, COLE, "cole", "hi", 15100, correspondent_standing_request="establish_standing")
    result = _owner_private_turn(monkeypatch, s, "hm", discord_correspondence_request="send_message",
                                 discord_destination_id=s.destination_id,
                                 discord_correspondent_ref=f"discord_user:{COLE}", discord_message_text="hey")
    assert s.fake.posts == [] and "does not permit" in result["correspondence"]["notes"][0]


def test_an_unaddressed_post_to_an_open_surface_is_refused(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=True)
    result = _owner_private_turn(monkeypatch, s, "post something", discord_correspondence_request="send_message",
                                 discord_destination_id=s.destination_id, discord_message_text="Hello room.")
    assert s.fake.posts == [] and "must be addressed" in result["correspondence"]["notes"][0]


def test_authority_is_rechecked_at_the_side_effect_boundary(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=True)
    _occasion(s, svc, COLE, "cole", "hi", 15100, correspondent_standing_request="establish_standing")
    send = dict(discord_correspondence_request="send_message", discord_destination_id=s.destination_id,
                discord_correspondent_ref=f"discord_user:{COLE}", discord_message_text="late")
    real = dc.dispatch_outbound
    monkeypatch.setattr(s.h.la.discord_correspondence, "dispatch_outbound", lambda *a, **k: {"status": "deferred"})
    _owner_private_turn(monkeypatch, s, "hm", **send)
    turn, _ = _last_turn(s)
    cs.set_source_block(s.data_dir, kind="discord_user", value=COLE, blocked=True,
                        requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    assert real(s.data_dir, waking_turn_event_id=turn)["detail"] == "source_blocked"
    assert s.fake.posts == []


# ------------------------------------------------------------------ owner safety boundary, restart

def test_an_owner_block_holds_the_source_and_never_touches_standing(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    _occasion(s, svc, COLE, "cole", "hi", 15100, correspondent_standing_request="establish_standing")
    cs.set_source_block(s.data_dir, kind="discord_user", value=COLE, blocked=True,
                        requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    assert _occasion(s, svc, COLE, "cole", "BLOCKED-WORDS-MARKER", 15200) == "idle"
    assert dc.held_inbound(s.data_dir)[-1]["reason"] == dc.HELD_SOURCE_BLOCKED
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert cs.standing(conn, "discord_user", COLE)["state"] == cs.STANDING_ESTABLISHED
    finally:
        conn.close()


def test_standing_and_continuity_survive_a_restart(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path)
    s.pass2_text = "PRE-RESTART-REPLY"
    _occasion(s, svc, COLE, "cole", "remember this", 15100, correspondent_standing_request="establish_standing")
    fresh = _service(s, tmp_path)                                   # a new service (restart)
    _occasion(s, fresh, COLE, "cole", "still there?", 15200)
    assert {"role": "assistant", "content": "PRE-RESTART-REPLY"} in s.pass2_prompts[-1]


def test_retry_exhaustion_is_durable_and_the_owner_can_reopen(monkeypatch, tmp_path):
    s, _svc = _world(monkeypatch, tmp_path)
    boom = lambda pending: (_ for _ in ()).throw(RuntimeError("model down"))   # noqa: E731
    _serve(s, _from(COLE, "cole", content="hi", mid=fixture_snowflake(15100)))
    svc = _service(s, tmp_path, run=boom)
    for _ in range(cs.MAX_OCCASION_ATTEMPTS):
        svc._not_before.clear()
        assert svc.step()[0] == "failed"
    restarted = _service(s, tmp_path, run=boom)
    assert restarted.step()[0] == "idle"                               # never resurrected by a restart
    assert dc.held_inbound(s.data_dir)[-1]["reason"] == dc.HELD_RETRY_EXHAUSTED
    (inbound,) = [r[0] for r in _rows(s, "SELECT event_id FROM events WHERE event_type='discord_inbound_message'")]
    cs.owner_occasion_act(s.data_dir, inbound_event_id=inbound, action="reopen",
                          requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    assert _service(s, tmp_path).step()[0] == "delivered"             # the owner's reopen, not a resurrection


def _stranded_send(monkeypatch, s, svc):
    """An addressed send Clark authored whose delivery never started (dispatch never ran)."""
    _occasion(s, svc, COLE, "cole", "hi", 15100, correspondent_standing_request="establish_standing")
    monkeypatch.setattr(s.h.la.discord_correspondence, "dispatch_outbound", lambda *a, **k: {"status": "deferred"})
    _owner_private_turn(monkeypatch, s, "hm", discord_correspondence_request="send_message",
                        discord_destination_id=s.destination_id, discord_correspondent_ref=f"discord_user:{COLE}",
                        discord_message_text="STRANDED-WORDS")
    turn, _ = _last_turn(s)
    (pending,) = dc.pending_authored_sends(s.data_dir)
    assert pending["send_turn_event_id"] == turn and s.fake.posts == []
    return turn


def test_clark_can_withdraw_an_undelivered_send_as_a_second_authored_act(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=True)
    real = dc.dispatch_outbound
    send_turn = _stranded_send(monkeypatch, s, svc)
    result = _owner_private_turn(monkeypatch, s, "Still want to send that?", withdraw_pending_send_request=send_turn)
    assert send_turn in _all_text(s.pass1_prompts[-1:])                       # he is shown it
    assert result["correspondence"]["withdrawal"]["lifecycle_event_id"]
    monkeypatch.setattr(s.h.la.discord_correspondence, "dispatch_outbound", real)
    for _ in range(3):
        svc.step()
    assert s.fake.posts == [] and dc.pending_authored_sends(s.data_dir) == []
    assert real(s.data_dir, waking_turn_event_id=send_turn)["detail"] == "withdrawn_by_clark"
    # authorship is untouched: the original turn still carries his words
    assert "STRANDED-WORDS" in [r[0] for r in _rows(s, "SELECT component_text FROM event_components WHERE event_id=?", send_turn)]


def test_a_withdrawal_needs_clarks_canonical_choice_and_owner_closure_is_the_owners_act(monkeypatch, tmp_path):
    s, svc = _world(monkeypatch, tmp_path, initiation=True)
    send_turn = _stranded_send(monkeypatch, s, svc)
    with pytest.raises(sqlite3.IntegrityError):                              # no canonical Clark choice
        cs.record_outbound_lifecycle(s.data_dir, send_turn_event_id=send_turn, action="withdrawn_by_clark",
                                     requester_actor_id="clark", occurred_at=1, evidence_event_id=send_turn)
    with pytest.raises(cs.CorrespondenceError):                              # not the owner
        cs.record_outbound_lifecycle(s.data_dir, send_turn_event_id=send_turn, action="closed_by_owner",
                                     requester_actor_id="actor-someone", occurred_at=1)
    cs.record_outbound_lifecycle(s.data_dir, send_turn_event_id=send_turn, action="closed_by_owner",
                                 requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    conn = sqlite3.connect(s.h.db_path)
    try:
        state = cs.outbound_lifecycle(conn, send_turn)
    finally:
        conn.close()
    assert state == {"withdrawn_by_clark": False, "closed_by_owner": True, "host_resume_failures": 0}
    assert dc.pending_authored_sends(s.data_dir) == []


def test_owner_panel_is_content_free_owner_only_and_never_sets_standing(monkeypatch, tmp_path):
    import correspondence_admin as admin
    s, svc = _world(monkeypatch, tmp_path)
    _occasion(s, svc, COLE, "cole", "PANEL-SECRET-WORDS", 15100)
    view = admin.load_view(s.data_dir)
    assert "open to correspondents who are not ANAXI principals" in view["markdown"]
    assert f"discord_user:{COLE}" in view["markdown"] and "standing NONE" in view["markdown"]
    assert "PANEL-SECRET-WORDS" not in view["markdown"]
    assert not hasattr(admin, "set_standing")                       # the owner never sets Clark's standing
    ok, note = admin.set_block(s.data_dir, requester_actor_id="actor-not-owner", discord_user_id=COLE, blocked=True)
    assert not ok
    ok, note = admin.set_block(s.data_dir, requester_actor_id=s.h.actor_id, discord_user_id=COLE, blocked=True)
    assert ok and "BLOCKED" in admin.load_view(s.data_dir)["markdown"]
    ok, _ = admin.set_door(s.data_dir, requester_actor_id=s.h.actor_id, destination_id=s.destination_id,
                           allow_unknown_sources=False, permit_independent_initiation=False)
    assert ok and "mapped ANAXI principals only" in admin.load_view(s.data_dir)["markdown"]
    assert admin.door_policy(s.data_dir, s.destination_id) == (False, False)


def test_the_door_checkboxes_show_the_durable_policy_never_a_stale_default(monkeypatch, tmp_path):
    # Live 2026-09-24: input-only checkboxes reset to unchecked on a page load while the door stayed open;
    # pressing "Set door" then would have closed it. They now read the selected destination's policy.
    import correspondence_admin as admin
    s, _svc = _world(monkeypatch, tmp_path)
    assert admin.door_policy(s.data_dir, s.destination_id) == (True, False)
    ok, _ = admin.set_door(s.data_dir, requester_actor_id=s.h.actor_id, destination_id=s.destination_id,
                           allow_unknown_sources=True, permit_independent_initiation=True)
    assert ok and admin.door_policy(s.data_dir, s.destination_id) == (True, True)
    assert admin.door_policy(s.data_dir, None) == (False, False)
    # ... and the GUI wires every way the selection or the page can change to that durable read.
    import re
    from pathlib import Path
    gui = (Path(__file__).resolve().parent / "llama_gui.py").read_text(encoding="utf-8")
    assert "correspondence_admin.door_policy(PROVENANCE_DB_DIR, destination_id)" in gui
    assert re.search(r"corr_destination\.change\(fn=load_door_policy, inputs=\[corr_destination\],\s*"
                     r"outputs=\[corr_allow_unknown, corr_permit_initiation\]", gui)
    assert gui.count("fn=load_door_policy, inputs=[corr_destination], outputs=[corr_allow_unknown, corr_permit_initiation]") == 2


def test_a_correspondent_never_sees_owner_history_and_standing_widens_no_authority(monkeypatch, tmp_path):
    """Isolation runs both ways, and ESTABLISHED standing is continuity with that source only: it is not a
    principal role, not a visibility grant over anyone else's history, not an actor."""
    s, svc = _world(monkeypatch, tmp_path)
    s.pass2_text = "OWNER-PRIVATE-REPLY-MARKER"
    _owner_private_turn(monkeypatch, s, "OWNER-PRIVATE-WORDS-MARKER")
    owner_turn, _ = _last_turn(s)
    s.pass2_text = "hello"
    _occasion(s, svc, COLE, "cole", "hi", 15100, correspondent_standing_request="establish_standing")
    cole_turn, _ = _last_turn(s)
    _occasion(s, svc, COLE, "cole", "what else do you know?", 15200)
    seen = _all_text(s.pass1_prompts[-2:] + s.pass2_prompts[-2:])
    assert "OWNER-PRIVATE-WORDS-MARKER" not in seen and "OWNER-PRIVATE-REPLY-MARKER" not in seen
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert cs.standing(conn, "discord_user", COLE)["state"] == cs.STANDING_ESTABLISHED
        viewer = dict(viewer_principal_actor_id=f"discord_user:{COLE}", viewer_scope=fm.SCOPE_CORRESPONDENT)
        assert not fm.can_receive_canonical_event(conn, owner_turn, **viewer)
        assert fm.can_receive_canonical_event(conn, cole_turn, **viewer)        # the control: his own is
        assert f"discord_user:{COLE}" not in fm.active_family_principals(conn)
        assert conn.execute("SELECT COUNT(*) FROM actors WHERE actor_id LIKE ?", (f"%{COLE}%",)).fetchone()[0] == 0
    finally:
        conn.close()


def test_the_capability_is_general_across_sources_and_surfaces(monkeypatch, tmp_path):
    """Nothing is keyed to one person or one channel: a second owner-opened destination admits a second
    unknown source, and Clark's reply goes to the surface that produced that occasion."""
    s, svc = _world(monkeypatch, tmp_path)
    other = dc.authorize_destination(
        s.data_dir, destination_kind="channel", discord_snowflake="222222222222222222",
        display_label="Second room", requester_actor_id=s.h.actor_id, occurred_at=1)
    cs.set_surface_policy(s.data_dir, destination_id=other["destination_id"], allow_unknown_sources=True,
                          permit_independent_initiation=False, requester_actor_id=s.h.actor_id,
                          occurred_at=int(time.time()), destination_authorized=True)
    robo = _from(ROBO, "robo", content="SECOND-ROOM-WORDS", mid=fixture_snowflake(15100))
    robo["channel_id"] = "222222222222222222"

    def request(method, url, headers, body, timeout):
        s.fake.requests.append((method, url, body))
        if method == "POST":
            return 200, json.dumps({"id": "555555555555555555", "channel_id": "222222222222222222"}).encode()
        return 200, json.dumps([robo] if "/channels/222222222222222222/" in url else []).encode()

    import discord_correspondence_net as net
    monkeypatch.setattr(net, "_default_request", request)
    s.pass1_script.append(dict(ROUTE))
    s.pass2_text = "Hello, second room."
    assert svc.step()[0] == "delivered"
    assert "SECOND-ROOM-WORDS" in _all_text(s.pass1_prompts[-1:])
    assert len(s.fake.posts) == 1 and "/channels/222222222222222222/messages" in s.fake.posts[0][1]
    turn, comps = _last_turn(s)
    assert comps["correspondent_source_ref"] == f"discord_user:{ROBO}"


def test_the_owner_turn_grammar_admits_an_addressed_send_in_the_order_the_menu_lists_it():
    """Real-model finding: Ollama's schema grammar admits properties only in declared order.  With the ref
    appended after the text, a real addressed send (destination, ref, text -- the menu's order) could not
    carry its words.  Countermodel: any order placing the ref or the standing choice after the text."""
    import conversation_direction as cd
    keys = list(cd.pass1_schema_with_correspondence(correspondents_available=True)["properties"])
    assert (keys.index("discord_correspondence_request") < keys.index("discord_destination_id")
            < keys.index("correspondent_standing_request") < keys.index("discord_correspondent_ref")
            < keys.index("discord_message_text"))
    menu = "\n".join(cd.render_pass1_action_menu_parts(correspondents=[
        {"ref": f"discord_user:{COLE}", "display_name": "cole", "standing": "ESTABLISHED",
         "initiation_destinations": ["d1"]}]))
    assert menu.index("discord_destination_id, discord_correspondent_ref and discord_message_text") > 0
    assert cd.pass1_schema_with_correspondence() is cd.PASS1_SCHEMA         # nothing added when nothing to name


def test_sleep_consolidation_never_carries_a_correspondents_words_across_the_boundary(monkeypatch, tmp_path):
    """Adversarial, cross-law (Step 2 x Sleep x retrieval): a Sleep derivation's visibility is the
    intersection of its sources.  Countermodel: a derivation that merged Cole's and the owner's words
    reaching either of them (or reaching Robo) would be a leak through consolidation."""
    import hippocampus_retrieval as hr
    import sleep_c_schema
    s, svc = _world(monkeypatch, tmp_path)
    s.pass2_text = "OWNER-SIDE"
    _owner_private_turn(monkeypatch, s, "owner words")
    owner_x, _ = _last_turn(s)
    _occasion(s, svc, COLE, "cole", "cole one", 15100, correspondent_standing_request="establish_standing")
    cole_x, _ = _last_turn(s)
    _occasion(s, svc, ROBO, "robo", "robo one", 15200)
    robo_x, _ = _last_turn(s)
    sleep_c_schema.apply_additive_migration(s.h.db_path)
    conn = sqlite3.connect(s.h.db_path)
    conn.execute("INSERT INTO sleep_cycles (cycle_id, window_start_component_id, window_end_component_id, selected_count, "
                 "derivation_count, model_tag, started_at, completed_at) VALUES ('cyc', 0, 0, 1, 1, 't', 1, 2)")
    derivations = {"deriv-cole": [cole_x], "deriv-mixed": [cole_x, owner_x], "deriv-robo": [robo_x],
                   "deriv-owner": [owner_x]}
    for did, sources in derivations.items():
        conn.execute("INSERT INTO sleep_derivations (derivation_id, cycle_id, batch_index, item_index, derived_text, "
                     "derived_text_sha256, model_tag, created_at) VALUES (?, 'cyc', 0, 0, 'd', 'h', 't', 3)", (did,))
        for i, src in enumerate(sources):
            conn.execute("INSERT INTO sleep_derivation_sources (derivation_id, wmu_id, primary_event_id, cite_order, "
                         "source_event_id, source_component_id, segment_id, segment_index) VALUES (?, 'w', ?, ?, ?, ?, 's', 0)",
                         (did, src, i, src, i))
    conn.commit()
    items = [hr.RetrievedMemory(
        item_id=f"item-{d}", event_id=d, occurred_at=3, source_store="sleep_derivations", source_locator=d,
        memory_kind="derived_inference", attribution_status="host_attributed", authentication_status="not_applicable",
        creator_actor_id=None, creator_actor_type=None, pipeline_id=None, session_id=None,
        session_resolution="unresolved", component_kind=None, content="d", content_truncated=False,
        retrieval_method=hr.RETRIEVAL_METHOD, bm25_score=-1.0, query_terms=()) for d in derivations]

    def seen(viewer, scope):
        return {i.event_id for i in fm.permitted_hippocampal_items(
            conn, items, viewer_principal_actor_id=viewer, viewer_scope=scope,
            active_family_principal_ids=fm.active_family_principals(conn))}
    try:
        assert seen(s.h.actor_id, fm.SCOPE_PRINCIPAL_PRIVATE) == {"deriv-owner"}
        assert seen(f"discord_user:{COLE}", fm.SCOPE_CORRESPONDENT) == {"deriv-cole"}
        assert seen(f"discord_user:{ROBO}", fm.SCOPE_CORRESPONDENT) == set()      # no standing: nothing carried
    finally:
        conn.close()


def test_the_owner_can_authorize_and_revoke_a_channel_from_the_panel(monkeypatch, tmp_path):
    """Live acceptance 2026-09-24: no owner control existed to authorize a new Discord channel -- a new
    surface for a new correspondent needed code.  Owner-only, prospective, door closed by default."""
    import correspondence_admin as admin
    s, svc = _world(monkeypatch, tmp_path)
    ok, _ = admin.authorize_channel(s.data_dir, requester_actor_id="actor-not-owner", discord_channel_id="333333333333333333", display_label="Test room")
    assert not ok
    assert not admin.authorize_channel(s.data_dir, requester_actor_id=s.h.actor_id, discord_channel_id="not-digits", display_label="x")[0]
    ok, note = admin.authorize_channel(s.data_dir, requester_actor_id=s.h.actor_id, discord_channel_id="333333333333333333", display_label="Test room")
    assert ok and "door closed" in note
    view = admin.load_view(s.data_dir)
    new = [d for label, d in view["destinations"] if label == "Test room"]
    assert new and "Test room" in view["markdown"]
    assert "Test room (`%s`): mapped ANAXI principals only" % new[0] in view["markdown"]
    ok, _ = admin.revoke_destination(s.data_dir, requester_actor_id=s.h.actor_id, destination_id=new[0])
    assert ok and "Test room" not in [label for label, _d in admin.load_view(s.data_dir)["destinations"]]


def test_the_owner_can_record_who_a_discord_id_is_from_the_panel(monkeypatch, tmp_path):
    """Live acceptance 2026-09-24: Step 1 identity binding had no owner control.  Identity only."""
    import correspondence_admin as admin
    s, svc = _world(monkeypatch, tmp_path)
    assert not admin.record_identity(s.data_dir, requester_actor_id="actor-not-owner", discord_user_id=COLE, name="Cole")[0]
    ok, note = admin.record_identity(s.data_dir, requester_actor_id=s.h.actor_id, discord_user_id=COLE, name="Cole")
    assert ok and "identity only" in note
    assert _occasion(s, svc, COLE, "cole", "hello again", 15100) == "delivered"
    assert 'recorded this Discord user id as canonical person "Cole"' in _all_text(s.pass1_prompts[-1:])
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert cs.standing(conn, "discord_user", COLE)["state"] == cs.STANDING_NONE     # identity != standing
    finally:
        conn.close()
