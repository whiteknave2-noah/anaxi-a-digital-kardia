"""CPI0: canonical person identity & relational provenance.

Synthetic fixtures only (real create_provenance_db, real hir1 registration, real family_membership, real
hippocampus store/retrieval).  No model, no network, no production state.

Law under test
  IDENTITY IS CANONICAL / PROVENANCE IS DURABLE / INTERIORS ARE NOT / RELATIONSHIP MEANING BELONGS TO CLARK.
  Person identity grants nothing; utterances stay dated, attributed and framed (never timeless biography);
  a standing request is prospective only when explicitly recorded as such and only its own speaker can end
  it; later-established identity re-contextualizes an event without rewriting it; nothing here models what
  a person is, feels or means.
"""
import hashlib
import json
import os
import sqlite3
import time

import pytest

import canonical_person as cp
import cpi_schema_migration
import dc0_schema_migration
import discord_author_mapping as dam
import discord_correspondence as dc
import discord_correspondence_registry as dcr
import family_membership as fm
import fs1_schema_migration
import hippocampus_retrieval as hr
import hippocampus_store as hs
import hir1_schema_migration
import human_session_binding as hsb
import sleep_transformation
from hir1_registration import HOST_ACTOR_ID, register_canonical_human
from provenance_schema import create_provenance_db

COLE_DISCORD = "555000000000000001"
ALEX_DISCORD = "555000000000000002"
RAINE_DISCORD = "555000000000000003"


class Fx:
    pass


def _register(conn, key, label):
    conn.row_factory = sqlite3.Row
    res, failure = register_canonical_human(conn, {
        "registration_request_id": "req-" + key, "aab_actor_id": "actor-" + key,
        "display_label": label, "source": "local_operator_provisioning"})
    assert failure is None, failure
    conn.row_factory = None
    conn.commit()
    return res["actor_id"]


@pytest.fixture
def fx(tmp_path):
    f = Fx()
    f.dir = str(tmp_path)
    f.path = os.path.join(f.dir, "anaxi_provenance.db")
    create_provenance_db(f.path).close()
    hir1_schema_migration.apply_additive_migration(f.path)
    fs1_schema_migration.apply_additive_migration(f.path)
    dc0_schema_migration.apply_additive_migration(f.path)
    conn = sqlite3.connect(f.path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                 "VALUES ('pipe-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')")
    now = int(time.time())
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (HOST_ACTOR_ID, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')",
                 (HOST_ACTOR_ID,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES ('actor-clark', 'clark_agent', 'clark', ?)", (now,))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES ('actor-clark', 'clark')")
    conn.commit()
    f.alex = _register(conn, "alex", "Alex")
    f.wife = _register(conn, "wife", "Wife")
    conn.close()
    f.conn = sqlite3.connect(f.path)
    f.conn.execute("PRAGMA foreign_keys = ON;")
    f.seq = 0
    fm.designate_owner(f.conn, principal_actor_id=f.alex, display_label="Alex", requester_actor_id=HOST_ACTOR_ID)
    f.conn.commit()
    return f


def _family(fx):
    fm.enroll_family_member(fx.conn, principal_actor_id=fx.wife, display_label="Wife", requester_actor_id=fx.alex)
    fx.conn.commit()


def _event(fx, event_type, t, components, scope=None, auth=None):
    fx.seq += 1
    eid = f"evt-{fx.seq}"
    fx.conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, occurred_at, "
        "record_created_at, visibility_scope, auth_context_id) VALUES (?, ?, 'pipe-1', 'known', ?, ?, ?, ?)",
        (eid, event_type, t, t, scope, auth))
    ids = []
    for seq, (kind, creator, text) in enumerate(components):
        cur = fx.conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256) VALUES (?, ?, ?, ?, 'resolved', ?, ?)",
            (eid, seq, creator, kind, text, hashlib.sha256(text.encode()).hexdigest()))
        ids.append(cur.lastrowid)
    fx.conn.commit()
    return eid, ids


def _said(fx, actor, text, t, scope=fm.SCOPE_PRINCIPAL_PRIVATE):
    """A canonical H event authored by `actor` inside that principal's own bound, scoped session (real FS1
    binding), so the existing delivery law governs who may ever receive it."""
    if actor not in getattr(fx, "sessions", {}):
        fx.sessions = getattr(fx, "sessions", {})
    key = (actor, scope)
    if key not in fx.sessions:
        fx.sessions[key] = hsb.bind_session_to_registered_human(
            fx.conn, session_id=f"s-{actor}-{scope}", session_started_at=1,
            pipeline_key="anaxi_orchestration_lineage_a", actor_id=actor, visibility_scope=scope).auth_context_id
        fx.conn.commit()
    return _event(fx, "human_waking_input", t, [(cp.HUMAN_INPUT_COMPONENT_KIND, actor, text)],
                  scope=scope, auth=fx.sessions[key])


def _person(fx, label="Cole", kind="human", actor=None):
    return cp.create_person(fx.dir, label=label, entity_kind=kind, requester_actor_id=fx.alex,
                            occurred_at=10, linked_actor_id=actor)["person_id"]


def _ro(fx):
    return sqlite3.connect(f"file:{fx.path}?mode=ro", uri=True)


# ============================================================ 1-8 identity, separate from authority


def test_1_owner_gets_canonical_person_without_changing_owner_authority(fx):
    before = fm.resolve_owner_actor_id(fx.conn)
    made = cp.establish_principal_person(fx.dir, principal_actor_id=fx.alex, requester_actor_id=fx.alex,
                                         occurred_at=10)
    assert made["created"] is True
    assert cp.establish_principal_person(fx.dir, principal_actor_id=fx.alex, requester_actor_id=fx.alex,
                                         occurred_at=11) == {"person_id": made["person_id"], "created": False}
    conn = _ro(fx)
    assert cp.person_for_actor(conn, fx.alex)["person_id"] == made["person_id"]
    assert fm.resolve_owner_actor_id(conn) == before == fx.alex
    assert dam.principal_role(conn, fx.alex) == dam.ROLE_OWNER


def test_2_family_principal_person_leaves_family_law_unchanged(fx):
    _family(fx)
    active_before = set(fm.active_family_principals(fx.conn))
    scoped = fm.can_receive_event(fx.wife, fm.SCOPE_PRINCIPAL_PRIVATE, fx.alex, fm.SCOPE_PRINCIPAL_PRIVATE,
                                  active_family_principal_ids=frozenset(active_before))
    cp.establish_principal_person(fx.dir, principal_actor_id=fx.wife, requester_actor_id=fx.alex, occurred_at=10)
    conn = _ro(fx)
    assert set(fm.active_family_principals(conn)) == active_before
    assert fm.can_receive_event(fx.wife, fm.SCOPE_PRINCIPAL_PRIVATE, fx.alex, fm.SCOPE_PRINCIPAL_PRIVATE,
                                active_family_principal_ids=frozenset(active_before)) is scoped is False
    assert dam.principal_role(conn, fx.wife) == dam.ROLE_FAMILY_MEMBER


def test_3_external_person_exists_with_no_owner_or_family_authority(fx):
    _family(fx)
    cole = _person(fx, "Cole")
    conn = _ro(fx)
    rec = cp.person_record(conn, cole)
    assert rec["linked_actor_id"] is None and rec["entity_kind"] == "human"
    assert cole not in fm.active_family_principals(conn)
    assert conn.execute("SELECT COUNT(*) FROM actors WHERE actor_id = ?", (cole,)).fetchone()[0] == 0


def test_4_digital_correspondent_is_not_human_or_family(fx):
    _family(fx)
    ai = _person(fx, "Raine", kind="ai_digital")
    assert cp.person_record(_ro(fx), ai)["entity_kind"] == "ai_digital"
    with pytest.raises(cp.CanonicalPersonError):
        cp.create_person(fx.dir, label="Bot", entity_kind="ai_digital", requester_actor_id=fx.alex, occurred_at=1,
                         linked_actor_id=fx.wife)
    with pytest.raises(sqlite3.IntegrityError):    # schema-level: only a human may be linked to an actor
        fx.conn.execute("INSERT INTO canonical_persons (person_id, entity_kind, display_label, linked_actor_id, "
                        "requester_actor_id, occurred_at, created_at) VALUES ('p-x','ai_digital','X',?, 'r', 1, 1)",
                        (fx.wife,))


def test_5_stable_identifier_maps_unambiguously_to_one_person(fx):
    cole = _person(fx)
    cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=20)
    got = cp.resolve_identifier(_ro(fx), "discord_author_id", COLE_DISCORD)
    assert got["status"] == cp.IDENTITY_ESTABLISHED and got["person_id"] == cole
    other = _person(fx, "Other")
    with pytest.raises(cp.CanonicalPersonError):
        cp.bind_identifier(fx.dir, person_id=other, identifier_kind="discord_author_id",
                           identifier_value=COLE_DISCORD, requester_actor_id=fx.alex, occurred_at=21)
    with pytest.raises(cp.CanonicalPersonError):   # a username / non-snowflake is never an identifier
        cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id",
                           identifier_value="cole_the_user", requester_actor_id=fx.alex, occurred_at=21)
    with pytest.raises(cp.CanonicalPersonError):   # owner-only
        cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id",
                           identifier_value=RAINE_DISCORD, requester_actor_id=fx.wife, occurred_at=21)


def test_6_username_change_never_changes_identity(fx):
    cole = _person(fx)
    cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=20, username_snapshot="cole_old")
    # a username is stored only as non-authoritative metadata and is never a lookup key
    assert cp.resolve_identifier(_ro(fx), "discord_author_id", "cole_old")["status"] == cp.IDENTITY_UNESTABLISHED
    assert cp.resolve_discord_author_person(_ro(fx), COLE_DISCORD)["person_id"] == cole


def test_7_conflicting_active_bindings_fail_closed(fx):
    a, b = _person(fx, "A"), _person(fx, "B")
    cp.bind_identifier(fx.dir, person_id=a, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=20)
    # forge the impossible state at the ledger level; the reader must still fail closed
    fx.conn.execute("INSERT INTO canonical_person_identifier_events (binding_event_id, person_id, identifier_kind, "
                    "identifier_value, action, requester_actor_id, occurred_at, created_at) "
                    "VALUES ('forged', ?, 'discord_author_id', ?, 'bind', ?, 30, 30)", (b, COLE_DISCORD, fx.alex))
    fx.conn.commit()
    assert cp.resolve_identifier(_ro(fx), "discord_author_id", COLE_DISCORD)["status"] == cp.IDENTITY_AMBIGUOUS
    assert cp.resolve_discord_author_person(_ro(fx), COLE_DISCORD)["status"] == cp.IDENTITY_AMBIGUOUS
    # explicit binding vs a Caret principal mapping naming a different person: also ambiguous
    alex_person = cp.establish_principal_person(fx.dir, principal_actor_id=fx.alex, requester_actor_id=fx.alex,
                                                occurred_at=10)["person_id"]
    dam.map_author(fx.dir, discord_author_id=ALEX_DISCORD, principal_actor_id=fx.alex,
                   requester_actor_id=fx.alex, occurred_at=40)
    fx.conn.execute("INSERT INTO canonical_person_identifier_events (binding_event_id, person_id, identifier_kind, "
                    "identifier_value, action, requester_actor_id, occurred_at, created_at) "
                    "VALUES ('forged2', ?, 'discord_author_id', ?, 'bind', ?, 50, 50)", (a, ALEX_DISCORD, fx.alex))
    fx.conn.commit()
    assert alex_person != a
    assert cp.resolve_discord_author_person(_ro(fx), ALEX_DISCORD)["status"] == cp.IDENTITY_AMBIGUOUS
    with pytest.raises(cp.CanonicalPersonError):    # and the writer refuses to create such a conflict
        cp.bind_identifier(fx.dir, person_id=a, identifier_kind="discord_author_id",
                           identifier_value=ALEX_DISCORD, requester_actor_id=fx.alex, occurred_at=60)


def test_8_and_30_identity_grants_no_capability_visibility_or_authorization(fx):
    _family(fx)
    cole = _person(fx)
    cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=20)
    conn = _ro(fx)
    # Caret correspondence authorization is untouched: a bound canonical person is still an UNMAPPED author
    assert dam.resolve_author(conn, COLE_DISCORD)["status"] == dam.STATUS_UNMAPPED
    assert dam.correspondent_for_delivery(conn, COLE_DISCORD, "555000000000009999") is None
    assert dam.principal_is_currently_valid(conn, cole) is False
    assert cole not in fm.active_family_principals(conn)
    # the mapping ledger cannot even be written for a person (only an existing principal)
    with pytest.raises(dam.AuthorMappingError):
        dam.map_author(fx.dir, discord_author_id=COLE_DISCORD, principal_actor_id=cole,
                       requester_actor_id=fx.alex, occurred_at=30)
    # visibility of an ordinary canonical event to a scoped session is identical with and without persons
    eid, _ = _said(fx, fx.alex, "hello", 100)
    args = dict(viewer_principal_actor_id=fx.wife, viewer_scope=fm.SCOPE_FAMILY_SHARED,
                active_family_principal_ids=frozenset(fm.active_family_principals(conn)))
    assert fm.can_receive_canonical_event(conn, eid, **args) is False


def test_identity_ledgers_are_append_only_and_carry_no_relationship_field(fx):
    cole = _person(fx)
    with pytest.raises(sqlite3.DatabaseError):
        fx.conn.execute("UPDATE canonical_persons SET display_label='X' WHERE person_id=?", (cole,))
    with pytest.raises(sqlite3.DatabaseError):
        fx.conn.execute("DELETE FROM canonical_persons WHERE person_id=?", (cole,))
    forbidden = ("trust", "close", "importance", "score", "rank", "affection", "friend", "relationship", "mood",
                 "personality", "emotion", "patience", "health", "burden", "role")
    for table in ("canonical_persons", "canonical_person_identifier_events", "canonical_person_statement_records"):
        cols = [r[1] for r in fx.conn.execute(f"PRAGMA table_info({table})")]
        for c in cols:
            assert not any(w in c for w in forbidden), (table, c)
    ddl = cpi_schema_migration.NEW_TABLES_DDL.lower()          # the DDL itself (not the docstring's negations)
    for w in ("trust", "closeness", "importance", "affection", "friend", "patience", "depletion", "score"):
        assert w not in ddl
    assert cp.STATEMENT_CLASSES == ("self_report", "standing_request", "third_party_report",
                                    "clark_reflection", "correction")


# ============================================================ 9-19 utterance provenance and framing


def _bound_wife(fx):
    _family(fx)
    return cp.establish_principal_person(fx.dir, principal_actor_id=fx.wife, requester_actor_id=fx.alex,
                                         occurred_at=10)["person_id"]


def test_9_utterance_keeps_speaker_source_time_and_default_frame(fx):
    wife = _bound_wife(fx)
    eid, (cid,) = _said(fx, fx.wife, "I was at the market today", 500)
    # family is used, so an unbound viewer sees nothing (existing FS1 law) -- use the owner-private view
    conn = _ro(fx)
    view = dict(viewer_principal_actor_id=fx.alex, viewer_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
                active_family_principal_ids=frozenset(fm.active_family_principals(conn)))
    # NULL-scope legacy event: fail-closed for everyone but its own author's private session
    assert cp.person_history(conn, wife, **view)["entries"] == []
    own = dict(view, viewer_principal_actor_id=fx.wife)
    entries = cp.person_history(conn, wife, **own)["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert (e["class"], e["speaker"], e["effective_at"], e["source_event_id"], e["quoted_text"]) == (
        "historical_utterance", "Wife", 500, eid, "I was at the market today")
    assert "not established as true of them now" in e["frame_note"]


def _pair(fx):
    """Wife (family principal, linked person) and external Cole; wife-private view."""
    wife = _bound_wife(fx)
    cole = _person(fx, "Cole")
    view = dict(viewer_principal_actor_id=fx.wife, viewer_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
                active_family_principal_ids=frozenset(fm.active_family_principals(fx.conn)))
    return wife, cole, view


def _rec(fx, **kw):
    base = dict(requester_actor_id=fx.alex, occurred_at=900)
    base.update(kw)
    return cp.record_statement(fx.dir, **base)["record_id"]


def test_10_self_report_is_temporally_situated_not_biography(fx):
    wife, _z, view = _pair(fx)
    eid, (cid,) = _said(fx, fx.wife, "When I'm quiet, I'm usually thinking.", 600)
    _rec(fx, action="record", statement_class="self_report", source_component_id=cid,
         quoted_text="When I'm quiet, I'm usually thinking.", speaker_person_id=wife)
    conn = _ro(fx)
    e = cp.person_history(conn, wife, **view)["entries"][0]
    assert e["class"] == "self_report" and e["effective_at"] == 600
    assert "whether it applies now is unknown" in e["frame_note"]
    assert "state" not in e                              # no standing/active status is ever attached
    # the durable representation contains no timeless present-tense statement about her
    rows = conn.execute("SELECT quoted_text, scope_text FROM canonical_person_statement_records").fetchall()
    assert rows == [("When I'm quiet, I'm usually thinking.", None)]


def test_11_third_party_report_stays_attributed_to_the_reporter(fx):
    wife, cole, view = _pair(fx)
    eid, (cid,) = _said(fx, fx.wife, "Cole hates crowds.", 600)
    _rec(fx, action="record", statement_class="third_party_report", source_component_id=cid,
         quoted_text="Cole hates crowds.", speaker_person_id=wife, referent_person_id=cole)
    e = cp.person_history(_ro(fx), wife, **view)["entries"][0]
    assert (e["class"], e["speaker"], e["about"]) == ("third_party_report", "Wife", "Cole")
    # Cole's own history never receives it as his statement; it appears only as a report ABOUT him
    zview = cp.person_history(_ro(fx), cole, **view)["entries"]
    assert [x["speaker"] for x in zview] == ["Wife"] and zview[0]["class"] == "third_party_report"
    with pytest.raises(cp.CanonicalPersonError):   # wrong speaker refused
        _rec(fx, action="record", statement_class="self_report", source_component_id=cid,
             quoted_text="Cole hates crowds.", speaker_person_id=cole)
    with pytest.raises(cp.CanonicalPersonError):   # a report about oneself is not third-party
        _rec(fx, action="record", statement_class="third_party_report", source_component_id=cid,
             quoted_text="Cole hates", speaker_person_id=wife, referent_person_id=wife)


def test_12_13_standing_request_is_prospective_only_when_recorded_and_can_be_revised_or_withdrawn(fx):
    wife, cole, view = _pair(fx)
    _e1, (c1,) = _said(fx, fx.wife, "Please ask before sharing my writing.", 600)
    with pytest.raises(cp.CanonicalPersonError):     # no stated scope => no prospective force
        _rec(fx, action="record", statement_class="standing_request", source_component_id=c1,
             quoted_text="Please ask before sharing my writing.", speaker_person_id=wife)
    with pytest.raises(cp.CanonicalPersonError):     # a quote that is not in the utterance
        _rec(fx, action="record", statement_class="standing_request", source_component_id=c1,
             quoted_text="Always share my writing.", speaker_person_id=wife, scope_text="writing")
    r1 = _rec(fx, action="record", statement_class="standing_request", source_component_id=c1,
              quoted_text="Please ask before sharing my writing.", speaker_person_id=wife, scope_text="sharing her writing")

    def state():
        return {e["record_action"]: e["state"] for e in cp.person_history(_ro(fx), wife, **view)["entries"]
                if e["class"] == "standing_request"}

    assert state() == {"record": "active"}
    # a third party (or Cole's own words) can never end her request
    _e2, (c2,) = _said(fx, fx.alex, "She said never mind.", 650)
    with pytest.raises(cp.CanonicalPersonError):
        _rec(fx, action="withdraw", statement_class="standing_request", source_component_id=c2,
             quoted_text="She said never mind.", speaker_person_id=wife, references_record_id=r1)
    # an earlier / same-time utterance cannot revise it
    _e3, (c3,) = _said(fx, fx.wife, "Ask me first for anything I write.", 600)
    with pytest.raises(cp.CanonicalPersonError):
        _rec(fx, action="revise", statement_class="standing_request", source_component_id=c3,
             quoted_text="Ask me first for anything I write.", speaker_person_id=wife, scope_text="all writing",
             references_record_id=r1)
    _e4, (c4,) = _said(fx, fx.wife, "Ask me first for anything I write, not just sharing.", 700)
    r2 = _rec(fx, action="revise", statement_class="standing_request", source_component_id=c4,
              quoted_text="Ask me first for anything I write", speaker_person_id=wife, scope_text="any use of her writing",
              references_record_id=r1)
    states = {e["quoted_text"]: e["state"] for e in cp.person_history(_ro(fx), wife, **view)["entries"]
              if e["class"] == "standing_request"}
    assert states == {"Please ask before sharing my writing.": "superseded",
                      "Ask me first for anything I write": "active"}
    _e5, (c5,) = _said(fx, fx.wife, "You don't need to ask me about my writing any more.", 800)
    _rec(fx, action="withdraw", statement_class="standing_request", source_component_id=c5,
         quoted_text="You don't need to ask me about my writing any more.", speaker_person_id=wife,
         references_record_id=r2)
    hist = cp.person_history(_ro(fx), wife, **view)["entries"]
    by_action = {(e["record_action"]): e["state"] for e in hist if e["class"] == "standing_request"}
    assert by_action["withdraw"] == "withdrawn" and by_action["record"] == "superseded" and by_action["revise"] == "superseded"
    assert len([e for e in hist if e["class"] == "standing_request"]) == 3   # full history preserved
    # other classes are never 'revised'; they coexist
    with pytest.raises(cp.CanonicalPersonError):
        _rec(fx, action="revise", statement_class="self_report", source_component_id=c5,
             quoted_text="You don't need", speaker_person_id=wife, references_record_id=r1)


def test_14_clark_reflection_stays_clark_authored_with_named_referent(fx):
    wife, cole, view = _pair(fx)
    eid, (cid,) = _event(fx, "waking_turn", 700, [("conversational_prose", "actor-clark",
                                                    "I wondered whether Cole withdraws when overwhelmed.")])
    _rec(fx, action="record", statement_class="clark_reflection", source_component_id=cid,
         quoted_text="I wondered whether Cole withdraws when overwhelmed.", referent_person_id=cole)
    with pytest.raises(cp.CanonicalPersonError):     # a person's statement cannot be made from Clark's words
        _rec(fx, action="record", statement_class="self_report", source_component_id=cid,
             quoted_text="I wondered", speaker_person_id=cole)
    with pytest.raises(cp.CanonicalPersonError):     # a reflection must be Clark-authored
        w_eid, (w_cid,) = _said(fx, fx.wife, "Cole withdraws.", 710)
        _rec(fx, action="record", statement_class="clark_reflection", source_component_id=w_cid,
             quoted_text="Cole withdraws.", referent_person_id=cole)
    row = _ro(fx).execute("SELECT speaker_person_id, speaker_basis, referent_person_id "
                          "FROM canonical_person_statement_records").fetchone()
    assert row == (None, "clark_actor", cole)
    assert cp.FRAME_TEXT["clark_reflection"].startswith("Clark's own interpretation")


def test_16_17_correction_is_attributable_and_surfaces_without_becoming_interior_fact(fx):
    wife, cole, view = _pair(fx)
    q, _ = _event(fx, "waking_turn", 800, [("conversational_prose", "actor-clark", "Was the quiet you had about Y?")])
    a, (ca,) = _said(fx, fx.wife, "No, that's not what was happening.", 810)
    _rec(fx, action="record", statement_class="correction", source_component_id=ca,
         quoted_text="No, that's not what was happening.", speaker_person_id=wife, prompted_by_event_id=q)
    with pytest.raises(cp.CanonicalPersonError):       # must name Clark's actual question event
        _rec(fx, action="record", statement_class="correction", source_component_id=ca,
             quoted_text="No, that's not", speaker_person_id=wife, prompted_by_event_id="evt-missing")
    e = cp.person_history(_ro(fx), wife, **view)["entries"][0]
    assert (e["class"], e["speaker"], e["source_event_id"], e["in_answer_to_event_id"]) == ("correction", "Wife", a, q)
    assert "says nothing about them beyond what they stated" in e["frame_note"]
    text = cp.render_person_history(cp.person_history(_ro(fx), wife, **view))
    assert "not a description of them" in text and "t=810" in text


def test_19_contradictory_statements_coexist_with_their_own_dates(fx):
    wife, _z, view = _pair(fx)
    _e1, (c1,) = _said(fx, fx.wife, "I love crowded rooms.", 100)
    _e2, (c2,) = _said(fx, fx.wife, "I can't stand crowded rooms.", 900)
    for c, q in ((c1, "I love crowded rooms."), (c2, "I can't stand crowded rooms.")):
        _rec(fx, action="record", statement_class="self_report", source_component_id=c, quoted_text=q,
             speaker_person_id=wife)
    hist = cp.person_history(_ro(fx), wife, **view)["entries"]
    assert [(e["effective_at"], e["quoted_text"]) for e in hist] == [
        (100, "I love crowded rooms."), (900, "I can't stand crowded rooms.")]
    assert all(e["class"] == "self_report" and "state" not in e for e in hist)


def test_person_identity_never_widens_visibility_of_history(fx):
    wife, cole, _v = _pair(fx)
    eid, (cid,) = _said(fx, fx.alex, "private words about Wife", 500)
    cp.establish_principal_person(fx.dir, principal_actor_id=fx.alex, requester_actor_id=fx.alex, occurred_at=11)
    alex_p = cp.person_for_actor(_ro(fx), fx.alex)["person_id"]
    fam = frozenset(fm.active_family_principals(fx.conn))
    wife_view = cp.person_history(_ro(fx), alex_p, viewer_principal_actor_id=fx.wife,
                                  viewer_scope=fm.SCOPE_PRINCIPAL_PRIVATE, active_family_principal_ids=fam)
    assert wife_view["entries"] == []                       # Wife's session never receives Alex's history
    alex_view = cp.person_history(_ro(fx), alex_p, viewer_principal_actor_id=fx.alex,
                                  viewer_scope=fm.SCOPE_PRINCIPAL_PRIVATE, active_family_principal_ids=fam)
    assert [e["quoted_text"] for e in alex_view["entries"]] == ["private words about Wife"]


# ============================================================ 15, 18, 21, 22, 24, 27 retrieval at use


def _hippocampus(fx, human_text, event_id, sleep_text=None):
    """Real Slice-A store: relational human-expression item for a turn event (+ optional Sleep derivation)."""
    rel_path = os.path.join(fx.dir, "anaxi_relational_llama.db")
    rel = sqlite3.connect(rel_path)
    rel.execute("""CREATE TABLE relational_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
        created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
        agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
        status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT)""")
    rel.execute("INSERT INTO relational_events (user_id, substrate, created_at, external_observation, event_id) "
                "VALUES ('u','llama',1,?,?)", (human_text, event_id))
    rel.commit()
    rel.close()
    jsonl = os.path.join(fx.dir, "anaxi_log.jsonl")
    open(jsonl, "w").close()
    paths = hs.HippocampusPaths(fx.path, rel_path, jsonl, os.path.join(fx.dir, "anaxi_hippocampus.db"))
    hs.create_hippocampus_db(paths.hippocampus_db_path).close()
    hs.sync_hippocampus(paths.hippocampus_db_path, paths)
    return paths


def _turn_for(fx, h_eid, t):
    return _event(fx, "waking_turn", t, [("human_input_event_id", "actor-clark", h_eid),
                                         ("conversational_prose", "actor-clark", "ok")])[0]


def test_15_18_21_22_27_retrieval_frames_each_class_and_ordinary_retrieval_is_unchanged(fx):
    wife, cole, view = _pair(fx)
    h_eid, (h_cid,) = _said(fx, fx.wife, "Quiet crowds bother me at the market", 600)
    turn = _turn_for(fx, h_eid, 601)
    paths = _hippocampus(fx, "Quiet crowds bother me at the market", turn)
    conn = _ro(fx)

    def retrieve(annotations):
        result = hr.retrieve_hippocampal_context(paths.hippocampus_db_path, "crowds market")
        items = [i for i in result.items]
        return result, items, cp.annotate_retrieved_items(conn, items) if annotations else None

    result, items, notes = retrieve(True)
    assert len(items) == 1
    # 27: with no annotations the rendering is byte-identical to the pre-CPI renderer
    baseline = hr.render_hippocampal_context(result)
    assert baseline == hr.render_hippocampal_context(result, person_annotations={})
    assert "person_provenance" not in baseline and hr.PERSON_PROVENANCE_STATEMENT not in baseline
    # default frame: a historical utterance by the canonical person, never a fact about her
    note = notes[items[0].item_id]
    assert note["speaker"]["label"] == "Wife" and note["frame"] == "historical_utterance"
    assert "records" not in note
    rendered = hr.render_hippocampal_context(result, person_annotations=notes)
    assert hr.PERSON_PROVENANCE_STATEMENT in rendered and '"person_provenance"' in rendered
    # a recorded self-report and a standing request surface distinctly, each with its own frame
    _rec(fx, action="record", statement_class="self_report", source_component_id=h_cid,
         quoted_text="Quiet crowds bother me", speaker_person_id=wife)
    _rec(fx, action="record", statement_class="standing_request", source_component_id=h_cid,
         quoted_text="at the market", speaker_person_id=wife, scope_text="markets")
    conn = _ro(fx)
    for _ in range(3):                                       # 18: repeated retrieval promotes nothing
        result, items, notes = retrieve(True)
    recs = notes[items[0].item_id]["records"]
    assert [r["class"] for r in recs] == ["self_report", "standing_request"]
    assert recs[0]["frame_note"] != recs[1]["frame_note"] and "state" not in recs[0]
    assert recs[1]["state"] == "active" and recs[1]["stated_scope"] == "markets"
    assert conn.execute("SELECT COUNT(*) FROM canonical_person_statement_records").fetchone()[0] == 2
    # 22: the frames are never collapsed into a generic fact line
    line = [l for l in hr.render_hippocampal_context(result, person_annotations=notes).splitlines()
            if '"person_provenance"' in l][0]
    payload = json.loads(line)["person_provenance"]
    assert payload["frame"] == "historical_utterance" and len(payload["records"]) == 2


def test_15_24_sleep_derivation_naming_a_person_is_never_framed_as_that_persons_word_or_fact(fx):
    wife, cole, view = _pair(fx)
    fx.conn.execute("CREATE TABLE sleep_cycles (cycle_id TEXT PRIMARY KEY)")
    text = "Wife deflects through warmth when afraid."
    fx.conn.execute("CREATE TABLE sleep_derivations (derivation_id TEXT PRIMARY KEY, cycle_id TEXT, batch_index INT, "
                    "item_index INT, derived_text TEXT, derived_text_sha256 TEXT, created_at INT)")
    fx.conn.execute("INSERT INTO sleep_cycles VALUES ('c1')")
    fx.conn.execute("INSERT INTO sleep_derivations VALUES ('deriv-1','c1',0,0,?,?,5)",
                    (text, hashlib.sha256(text.encode()).hexdigest()))
    fx.conn.commit()
    paths = _hippocampus(fx, "unrelated", "evt-none")
    result = hr.retrieve_hippocampal_context(paths.hippocampus_db_path, "Wife deflects warmth afraid")
    items = [i for i in result.items if i.source_store == "sleep_derivations"]
    assert len(items) == 1 and items[0].memory_kind == "derived_inference"
    assert items[0].attribution_status == "unknown" and items[0].creator_actor_id is None
    notes = cp.annotate_retrieved_items(_ro(fx), items)
    assert notes == {}                                       # no speaker, no frame that could lend it authority
    out = hr.render_hippocampal_context(result, person_annotations=notes)
    assert hr.SLEEP_DERIVED_LABEL in out                     # existing Sleep provenance law intact
    assert "person_provenance" not in out


def test_24_sleep_prompt_forbids_person_specific_character_reads():
    assert "Do not describe what any particular person is like" in sleep_transformation._NEUTRAL_INSTRUCTIONS
    assert "Contradictory derivations may coexist" in sleep_transformation._NEUTRAL_INSTRUCTIONS
    assert "Do not install beliefs, values, commitments, personality, or identity" in \
        sleep_transformation._NEUTRAL_INSTRUCTIONS


# ============================================================ 20 historical re-contextualization


def _inbound(fx, author_id, message_id, t):
    meta = json.dumps({"author_id": author_id, "author_name": "someone", "discord_message_id": message_id,
                       "remote_timestamp": "x", "destination_id": "d", "destination_kind": "channel"})
    return _event(fx, dcr.DISCORD_INBOUND_MESSAGE_EVENT_TYPE, t,
                  [(dcr.DISCORD_INBOUND_METADATA_COMPONENT_KIND, HOST_ACTOR_ID, meta),
                   (dcr.DISCORD_INBOUND_CONTENT_COMPONENT_KIND, HOST_ACTOR_ID, "I am the one who wrote this")])


def _snowflake(seconds):
    return str((int(seconds * 1000) - 1420070400000) << 22 | 1)


def test_20_unverified_then_established_later_without_rewriting_the_event(fx, monkeypatch):
    cole = _person(fx, "Cole")
    mid = _snowflake(time.time() - 3600)
    eid, (m, c) = _inbound(fx, COLE_DISCORD, mid, int(time.time()) - 3600)
    before = dc.historical_inbound_attribution(fx.dir, eid)
    assert before["event_time"]["status"] == "not_delivered" and before["later_person_identity"] is None
    rows_before = fx.conn.execute("SELECT component_text FROM event_components WHERE event_id=?", (eid,)).fetchall()
    cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=int(time.time()))
    after = dc.historical_inbound_attribution(fx.dir, eid)
    assert after["event_time"] == before["event_time"]        # the event-time fact is untouched
    later = after["later_person_identity"]
    assert later["person_id"] == cole and later["display_label"] == "Cole" and later["current_state"] == "established"
    assert later["binding_recorded_at"] > after["arrived_at"]
    text = dc.render_historical_attribution(after)
    assert "unverified" in text and "established as canonical person Cole" in text and "does not change" in text
    assert fx.conn.execute("SELECT component_text FROM event_components WHERE event_id=?", (eid,)).fetchall() == rows_before
    # a binding recorded BEFORE the message never claims later knowledge
    early = _snowflake(time.time() + 3600)
    eid2, _ = _inbound(fx, COLE_DISCORD, early, int(time.time()) + 3600)
    assert dc.historical_inbound_attribution(fx.dir, eid2)["later_person_identity"] is None
    # revoking is dated and shown as the current state
    cp.revoke_identifier(fx.dir, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                         requester_actor_id=fx.alex, occurred_at=int(time.time()))
    assert dc.historical_inbound_attribution(fx.dir, eid)["later_person_identity"]["current_state"] == "revoked"


def test_20b_caret_principal_mapping_resolves_through_canonical_person(fx):
    alex_p = cp.establish_principal_person(fx.dir, principal_actor_id=fx.alex, requester_actor_id=fx.alex,
                                           occurred_at=10)["person_id"]
    mid = _snowflake(time.time() - 7200)
    eid, _ = _inbound(fx, ALEX_DISCORD, mid, int(time.time()) - 7200)
    dam.map_author(fx.dir, discord_author_id=ALEX_DISCORD, principal_actor_id=fx.alex,
                   requester_actor_id=fx.alex, occurred_at=int(time.time()))
    resolved = cp.resolve_discord_author_person(_ro(fx), ALEX_DISCORD)
    assert (resolved["status"], resolved["person_id"], resolved["basis"]) == ("established", alex_p, "principal_mapping")
    hist = dc.historical_inbound_attribution(fx.dir, eid)
    assert hist["later_binding"]["display_label"] and hist["later_person_identity"]["person_id"] == alex_p
    assert "canonical person Alex" in dc.render_historical_attribution(hist)
    # the Caret authorization law is unchanged: the message that arrived earlier stays undeliverable
    assert dam.resolve_delivery(_ro(fx), ALEX_DISCORD, mid)["status"] == dam.STATUS_BEFORE_MAPPING


def test_external_speaker_established_later_is_reported_as_such_in_person_history(fx):
    wife, cole, view = _pair(fx)
    mid = _snowflake(time.time() - 3600)
    eid, (m, c) = _inbound(fx, COLE_DISCORD, mid, int(time.time()) - 3600)
    cp.bind_identifier(fx.dir, person_id=cole, identifier_kind="discord_author_id", identifier_value=COLE_DISCORD,
                       requester_actor_id=fx.alex, occurred_at=int(time.time()))
    _rec(fx, action="record", statement_class="self_report", source_component_id=c,
         quoted_text="I am the one who wrote this", speaker_person_id=cole)
    row = _ro(fx).execute("SELECT speaker_basis, speaker_identifier_value FROM canonical_person_statement_records"
                          ).fetchone()
    assert row == ("discord_author_binding", COLE_DISCORD)
    # (an inbound event has no FS1 scope, so under family mode it is not deliverable to a scoped viewer: fail closed)
    assert cp.person_history(_ro(fx), cole, **view)["entries"] == []
    conn = _ro(fx)
    rec = cp.records_for_component(conn, c)[0]
    assert cp._later_speaker_identity(conn, rec)["person_id"] == cole


def test_recurrence_diagnostics_are_not_implemented_and_no_relationship_metric_exists():
    for name in dir(cp):
        for word in ("recurrence", "patience", "trust", "closeness", "importance", "health", "burden"):
            assert word not in name.lower(), name


# ============================================================ 26/27 real waking + Caret path

def test_26_caret_owner_turn_retrieves_prior_utterance_framed_by_canonical_person_and_stays_unchanged_without_one(
        monkeypatch, tmp_path):
    from test_caret_inbound_seam import _remote, WORDS, fixture_snowflake
    from test_caret_wake_service import _serve, _service, _setup

    def _last_prompt(s):
        return "\n".join(m["content"] for m in s.pass2_prompts[-1])

    s = _setup(monkeypatch, tmp_path)
    _serve(s, _remote())
    assert _service(s, tmp_path).step()[0] == "delivered"
    conn = sqlite3.connect(s.h.db_path)
    (turn_event,) = conn.execute("SELECT event_id FROM events WHERE event_type='waking_turn'").fetchone()
    conn.close()
    la = s.h.la

    def factory(*a, **k):
        orch = la.AnaxiOrchestrator(*a, **k)
        base = orch.prepare_context

        def prepare(user_id, prompt):
            # the store's own relational human-expression item for turn 1 (same shape sync_hippocampus produces)
            item = hr.RetrievedMemory(
                item_id="hip-test", event_id=turn_event, occurred_at=1, source_store="relational_events",
                source_locator="1", memory_kind="human_expression", attribution_status="unknown",
                authentication_status="authenticated", creator_actor_id=None, creator_actor_type=None,
                pipeline_id=None, session_id=None, session_resolution="unknown", component_kind=None,
                content=WORDS, content_truncated=False, retrieval_method=hr.RETRIEVAL_METHOD, bm25_score=-1.0,
                query_terms=("codeword",))
            return dict(base(user_id, prompt), hippocampal_retrieval_result=hr.RetrievalResult(
                query_terms=("codeword",), items=(item,)))

        orch.prepare_context = prepare
        return orch

    def service():
        return _service(s, tmp_path, run=lambda pending: la.run_caret_occasion_turn(pending, orch_factory=factory))

    _serve(s, _remote(mid=fixture_snowflake(15001), content=WORDS))
    assert service().step()[0] == "delivered"
    assert "person_provenance" not in _last_prompt(s)              # no canonical person: rendering unchanged
    assert "HIPPOCAMPAL_CONTEXT_V1" in _last_prompt(s)
    made = cp.establish_principal_person(s.data_dir, principal_actor_id=s.h.actor_id,
                                         requester_actor_id=s.h.actor_id, occurred_at=int(time.time()))
    label = cp.person_record(sqlite3.connect(s.h.db_path), made["person_id"])["display_label"]
    _serve(s, _remote(mid=fixture_snowflake(15002), content=WORDS))
    assert service().step()[0] == "delivered"
    prompt = _last_prompt(s)
    assert '"person_provenance"' in prompt and '"frame": "historical_utterance"' in prompt
    assert hr.PERSON_PROVENANCE_STATEMENT in prompt
    assert f'"label": "{label}"' in prompt
    tail = prompt.split('"person_provenance"', 1)[1][:500].lower()
    for banned in ("trust", "closeness", "importance", "relationship_score", "friend"):
        assert banned not in tail
