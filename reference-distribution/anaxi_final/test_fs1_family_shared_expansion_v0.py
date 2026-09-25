"""FS1: focused tests for the V0 family/shared expansion.

Covers the load-bearing new seams added by the family-shared milestone:

1. The pure delivery matrix -- can_receive_event() -- and the dialogue
   pair predicate session_dialogue_window._pair_permitted_for_viewer().
2. Fail-closed hippocampal item filtering against the canonical events
   table (permitted_hippocampal_items), including the missing-row and
   NULL-scope cases that must NEVER be guessed broader.
3. Schema tolerance -- designated_owner_actor_id()/active_family_principals()
   /family_session_view() on the pre-FS1 database shape return the exact
   legacy fallbacks, so a DB that never ran the FS1 migration behaves
   byte-identically to before.
4. Designated-owner preference in the canonical-human resolution used by
   direction_control and workspace_episode_provenance.
5. Scoped native-writer cross-validation (H scope vs X scope) and the
   scope actually written onto the canonical event/auth_context rows.
6. One end-to-end waking turn through the REAL llama_anaxi.run_waking_turn()
   in three variants -- unbound, owner-private, member-shared -- proving
   the legacy/hippocampal suppression seam and the scope persisted by the
   real writer. Only ollama.chat() is faked (no real model call), and
   prepare_context() is a stub, exactly like the repo's existing
   test_native_waking_turn_end_to_end.py harness.

Run:
    python -m pytest test_fs1_family_shared_expansion_v0.py -q
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import types

import pytest

import family_membership as fm
import human_session_binding as hsb
import session_dialogue_window
import direction_control
import workspace_episode_provenance
import native_provenance_writer as npw
import conversation_projection
import wtr0_waking_recovery

from provenance_schema import create_provenance_db, derive_stable_id
from hir1_registration import register_canonical_human, HOST_ACTOR_ID
import hir1_schema_migration
import fs1_schema_migration
import hippocampus_retrieval
import od1_schema_migration
import operative_directive as od1


CLARK_ACTOR_ID = derive_stable_id("actor", "clark")
PIPELINE_KEY = "anaxi_orchestration_lineage_a"
NEUTRAL_PROMPT = "So what's on your mind?"
_CALIBRATED_MODEL_DIGEST = "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb"
_CALIBRATED_SERVER_VERSION = "0.34.0"
_CALIBRATED_CHAT_TEMPLATE_SHA256 = "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315"
_PASS1_TYPED_ACT = {"act": "develop_current", "thread": "pattern recognition",
                    "direction_request": "none", "relinquish_direction": False}


def _is_pass1_format(fmt):
    return fmt == "json" or (isinstance(fmt, dict) and "act" in fmt.get("properties", {}))


# ---------------------------------------------------------------- fixtures


def _seed_fs1_env(tmp):
    """Build a fresh provenance DB with FS1 schema + a two-principal
    family (owner + member). Returns (prov_path, ids) where ids maps
    'owner'/'member' to derived actor ids."""
    prov = os.path.join(tmp, "anaxi_provenance.db")
    create_provenance_db(prov).close()
    hir1_schema_migration.apply_additive_migration(prov)
    fs1_schema_migration.apply_additive_migration(prov)
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-llama-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)",
        (HOST_ACTOR_ID, now),
    )
    conn.execute(
        "INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')",
        (HOST_ACTOR_ID,),
    )
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
        (CLARK_ACTOR_ID, now),
    )
    conn.execute(
        "INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')",
        (CLARK_ACTOR_ID,),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    humans = {}
    for req, aab, label in (
        ("req-owner-fs1", "actor-owner-fs1", "Owner"),
        ("req-member-fs1", "actor-member-fs1", "Member"),
    ):
        res, failure = register_canonical_human(
            conn, {"registration_request_id": req, "aab_actor_id": aab,
                   "display_label": label, "source": "local_operator_provisioning"},
        )
        assert failure is None, failure
        humans[label] = res["actor_id"]
    conn.row_factory = None
    conn.commit()
    fm.designate_owner(conn, principal_actor_id=humans["Owner"], display_label="Owner",
                       requester_actor_id=HOST_ACTOR_ID)
    fm.enroll_family_member(conn, principal_actor_id=humans["Member"], display_label="Member",
                            requester_actor_id=humans["Owner"])
    conn.commit()
    ids = {"owner": humans["Owner"], "member": humans["Member"]}
    return prov, ids


def _bind(tmp, prov, actor_id, scope, session_id=None, session_started_at=100):
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        return hsb.bind_session_to_registered_human(
            conn, session_id=session_id or f"sess-{actor_id}-{scope}",
            session_started_at=session_started_at,
            pipeline_key=PIPELINE_KEY, actor_id=actor_id, visibility_scope=scope,
        )
    finally:
        conn.close()


def test_fs1_continuation_allows_restart_without_weakening_scope(tmp_path):
    """Recovery continuity is principal+scope continuity, not PID/session identity."""
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    owner, member = ids["owner"], ids["member"]
    prompt = "the exact unanswered occurrence"

    original = _bind(tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE, "h-original")
    h_event_id = hsb.record_human_waking_input(
        tmp, pipeline_key=PIPELINE_KEY, authority=original,
        message=prompt, occurred_at=150,
    )

    def check(authority, session_id, event_id=h_event_id, text=prompt):
        conn = sqlite3.connect(prov)
        try:
            return hsb.validate_existing_human_input_continuation(
                conn, authority, current_session_id=session_id,
                human_input_event_id=event_id, prompt=text,
            )
        finally:
            conn.close()

    # Ordinary same-session continuation remains lawful.
    assert check(original, original.session_id) == (True, "ok")

    # A new process/session for the same active principal and exact scope
    # may continue the same canonical H.
    restarted = _bind(tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE, "h-restarted")
    assert check(restarted, restarted.session_id) == (True, "ok")

    # Principal and scope boundaries remain exact.
    wrong_principal = _bind(tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE, "h-member")
    assert check(wrong_principal, wrong_principal.session_id)[0] is False
    wrong_scope = _bind(tmp, prov, owner, fm.SCOPE_FAMILY_SHARED, "h-owner-shared")
    assert check(wrong_scope, wrong_scope.session_id)[0] is False

    # Possession/fabrication of an event-shaped id grants nothing.
    assert check(restarted, restarted.session_id, event_id="event-not-canonical")[0] is False

    # A stale/foreign session authority object cannot simply be replayed
    # under the restarted session id. Auth contexts are intentionally
    # append-only, so staleness is exercised by that real mismatch rather
    # than by illegally mutating canonical history in the test.
    assert check(original, restarted.session_id) == (False, "invalid_current_authority")

    # A deactivated family principal cannot continue even its own H from
    # a fresh, previously valid session authority.
    member_original = _bind(tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE, "member-original")
    member_h = hsb.record_human_waking_input(
        tmp, pipeline_key=PIPELINE_KEY, authority=member_original,
        message="member occurrence", occurred_at=160,
    )
    member_restarted = _bind(tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE, "member-restarted")
    conn = sqlite3.connect(prov)
    with conn:
        fm.deactivate_family_member(
            conn, principal_actor_id=member, requester_actor_id=owner,
        )
    conn.close()
    assert check(
        member_restarted, member_restarted.session_id,
        event_id=member_h, text="member occurrence",
    ) == (False, "invalid_current_authority")


# ------------------------------------------------------------------- matrix


def test_fs1_can_receive_event_matrix():
    active = frozenset({"a1", "a2"})
    # Unbound viewer keeps legacy NULL-scope behavior but cannot receive
    # any explicit FS1 scope without an authoritative principal identity.
    assert fm.can_receive_event(None, None, "zzz", None)
    assert not fm.can_receive_event(None, None, "zzz", fm.SCOPE_PRINCIPAL_PRIVATE)
    assert not fm.can_receive_event(None, None, "zzz", fm.SCOPE_FAMILY_SHARED)
    # Shared event: any currently-active principal, private or shared session (F9).
    assert fm.can_receive_event("a1", fm.SCOPE_PRINCIPAL_PRIVATE, "a2", fm.SCOPE_FAMILY_SHARED, active)
    assert fm.can_receive_event("a1", fm.SCOPE_FAMILY_SHARED, "a2", fm.SCOPE_FAMILY_SHARED, active)
    # ...but NOT an in-family-but-deactivated / non-principal actor in a scoped session.
    assert not fm.can_receive_event("zzz", fm.SCOPE_FAMILY_SHARED, "a2", fm.SCOPE_FAMILY_SHARED, active)
    assert not fm.can_receive_event("a3", fm.SCOPE_FAMILY_SHARED, "a2", fm.SCOPE_FAMILY_SHARED, active)
    # Principal-private: only the session's OWN author, private session only.
    assert fm.can_receive_event("a1", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", fm.SCOPE_PRINCIPAL_PRIVATE, active)
    assert not fm.can_receive_event("a2", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", fm.SCOPE_PRINCIPAL_PRIVATE, active)
    assert not fm.can_receive_event("a1", fm.SCOPE_FAMILY_SHARED, "a1", fm.SCOPE_PRINCIPAL_PRIVATE, active)
    # Fail closed: NULL/unknown scope -> own author only, private session only.
    assert fm.can_receive_event("a1", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", None, active)
    assert not fm.can_receive_event("a2", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", None, active)
    assert not fm.can_receive_event("a1", fm.SCOPE_FAMILY_SHARED, "a1", None, active)
    assert fm.can_receive_event("a1", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", "nonsense_scope", active)
    assert not fm.can_receive_event("a2", fm.SCOPE_PRINCIPAL_PRIVATE, "a1", "nonsense_scope", active)


def test_fs1_dialogue_pair_predicate_fail_closed():
    active = frozenset({"a1", "a2"})
    # Disagreeing H/X scopes are anomalous and never delivered.
    assert not session_dialogue_window._pair_permitted_for_viewer(
        h_scope=fm.SCOPE_PRINCIPAL_PRIVATE, x_scope=fm.SCOPE_FAMILY_SHARED,
        human_actor_id="a1", viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        viewer_principal_actor_id="a1", active_family_principal_ids=active,
    )
    # Another principal's private pair never leaks into a scoped session.
    assert not session_dialogue_window._pair_permitted_for_viewer(
        h_scope=fm.SCOPE_PRINCIPAL_PRIVATE, x_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        human_actor_id="a2", viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        viewer_principal_actor_id="a1", active_family_principal_ids=active,
    )
    # Shared pair is visible to any active-family principal.
    assert session_dialogue_window._pair_permitted_for_viewer(
        h_scope=fm.SCOPE_FAMILY_SHARED, x_scope=fm.SCOPE_FAMILY_SHARED,
        human_actor_id="a2", viewer_visibility_scope=fm.SCOPE_FAMILY_SHARED,
        viewer_principal_actor_id="a1", active_family_principal_ids=active,
    )
    # Unbound viewer cannot receive an explicitly scoped pair.
    assert not session_dialogue_window._pair_permitted_for_viewer(
        h_scope=fm.SCOPE_PRINCIPAL_PRIVATE, x_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        human_actor_id="a2", viewer_visibility_scope=None,
        viewer_principal_actor_id=None, active_family_principal_ids=frozenset(),
    )
    # One-sided missing scope is an inconsistent pair, not a legacy pair.
    assert not session_dialogue_window._pair_permitted_for_viewer(
        h_scope=fm.SCOPE_PRINCIPAL_PRIVATE, x_scope=None,
        human_actor_id="a1", viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        viewer_principal_actor_id="a1", active_family_principal_ids=active,
    )


# ------------------------------------------------------------ hippocampal


def _mk_hippo_items(*specs):
    return [hippocampus_retrieval.RetrievedMemory(
        item_id=s[0], event_id=s[0], occurred_at=0, source_store="s", source_locator="l",
        memory_kind="m", attribution_status="attributed", authentication_status="authenticated",
        creator_actor_id=s[1], creator_actor_type="human_person", pipeline_id=None,
        session_id=None, session_resolution="single_principal", component_kind=None,
        content=s[2], content_truncated=False, retrieval_method="bm25",
        bm25_score=1.0, query_terms=()) for s in specs]


def test_fs1_permitted_hippocampal_items_fail_closed(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    owner, member = ids["owner"], ids["member"]
    staging = os.path.join(tmp, "native_turn_staging.jsonl")
    private_auth = _bind(
        tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE, "hippo-private", 100,
    )
    private_h = _write_answered_turn(
        tmp, staging, private_auth, "private prompt", "private answer", 110,
    )
    shared_auth = _bind(
        tmp, prov, member, fm.SCOPE_FAMILY_SHARED, "hippo-shared", 100,
    )
    shared_h = _write_answered_turn(
        tmp, staging, shared_auth, "shared prompt", "shared answer", 120,
    )
    conn = sqlite3.connect(prov)
    priv_member = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (private_h,),
    ).fetchone()[0]
    shared_member = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (shared_h,),
    ).fetchone()[0]
    missing = "evt-no-row"
    nulls = "evt-null-scope"
    conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
        " auth_context_id, occurred_at, record_created_at, visibility_scope) "
        "VALUES (?, 'waking_turn', 'pipe-llama-1', 'known', NULL, ?, ?, NULL)",
        (nulls, now, now),
    )
    conn.commit()
    conn.close()

    items = _mk_hippo_items(
        # X is Clark-authored; access derives from canonical X -> H,
        # never this retrieval object's creator field.
        (priv_member, CLARK_ACTOR_ID, "MEMBER_PRIVATE_CONTENT"),
        (shared_member, CLARK_ACTOR_ID, "MEMBER_SHARED_CONTENT"),
        (missing, member, "MISSING_ROW_CONTENT"),
        (nulls, member, "NULL_SCOPE_CONTENT"),
    )
    active = frozenset(fm.active_family_principals(sqlite3.connect(prov)))

    def permitted(viewer, scope):
        c = sqlite3.connect(f"file:{prov}?mode=ro", uri=True)
        try:
            return [i.event_id for i in fm.permitted_hippocampal_items(
                c, items, viewer_principal_actor_id=viewer, viewer_scope=scope,
                active_family_principal_ids=active)]
        finally:
            c.close()

    # Owner private session: sees shared; own-author NULL/missing rows; never
    # the member's private row.
    assert set(permitted(owner, fm.SCOPE_PRINCIPAL_PRIVATE)) == {shared_member}
    # Member shared session: sees the shared row -- but NOT the member's own
    # private row (private stays inside a private session, never surfacing in
    # a shared session even for its own author -- the scope IS the gate).
    assert set(permitted(member, fm.SCOPE_FAMILY_SHARED)) == {shared_member}
    # Member private: sees shared + own private + own NULL row. A missing
    # canonical event is denied even when the retrieval item claims member.
    assert set(permitted(member, fm.SCOPE_PRINCIPAL_PRIVATE)) == {
        shared_member, priv_member, nulls,
    }
    # Unbound viewer retains only legacy unknown/NULL items; explicitly
    # scoped private/shared events require an authoritative recipient.
    assert set(permitted(None, None)) == set()


def test_fs1_membership_schema_tolerant_legacy_fallbacks(tmp_path):
    tmp = str(tmp_path)
    prov = os.path.join(tmp, "anaxi_provenance.db")
    create_provenance_db(prov).close()
    hir1_schema_migration.apply_additive_migration(prov)
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-llama-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)",
        (HOST_ACTOR_ID, now),
    )
    conn.execute(
        "INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')",
        (HOST_ACTOR_ID,),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    res, failure = register_canonical_human(
        conn, {"registration_request_id": "req-solo", "aab_actor_id": "actor-solo",
               "display_label": "Solo", "source": "local_operator_provisioning"},
    )
    assert failure is None, failure
    solo = res["actor_id"]
    conn.row_factory = None
    conn.commit()

    # Pre-FS1 DB (no family tables): every read side falls back exactly.
    assert fm.designated_owner_actor_id(conn) is None
    assert fm.resolve_owner_actor_id(conn) == solo
    assert fm.active_family_principals(conn) == frozenset()
    view = fm.family_session_view(conn, principal_actor_id=solo,
                                  visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE)
    assert view["feature_used"] is False
    assert view["owner_actor_id"] == solo
    assert view["active_family_principal_ids"] == frozenset()
    with pytest.raises(hsb.HumanSessionBindingError):
        hsb.bind_session_to_registered_human(
            conn, session_id="no-membership-history", session_started_at=100,
            pipeline_key=PIPELINE_KEY, actor_id=solo,
            visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        )
    conn.close()


def test_fs1_canonical_membership_wins_projection_and_revokes_stale_session(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    owner, member = ids["owner"], ids["member"]

    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    # Missing projection cannot make canonical enrollment disappear or
    # permit a second enrollment/reactivation.
    conn.execute("DELETE FROM family_membership_state WHERE actor_id=?", (member,))
    conn.commit()
    with pytest.raises(fm.FamilyMembershipError):
        fm.enroll_family_member(
            conn, principal_actor_id=member, display_label="Relabeled",
            requester_actor_id=owner,
        )
    with pytest.raises(fm.FamilyMembershipError):
        fm.enroll_family_member(
            conn, principal_actor_id=member, display_label="Member",
            requester_actor_id=None,
        )
    fm.refresh_projection(conn)
    conn.close()

    authority = _bind(tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE,
                      session_id="member-stale-session")
    conn = sqlite3.connect(prov)
    fm.deactivate_family_member(
        conn, principal_actor_id=member, requester_actor_id=owner,
    )
    # A contradictory mutable projection cannot override the canonical
    # deactivate or authorize a second deactivate.
    conn.execute(
        "UPDATE family_membership_state SET status='active', deactivated_at=NULL "
        "WHERE actor_id=?", (member,),
    )
    conn.commit()
    assert member not in fm.active_family_principals(conn)
    assert not hsb.validate_human_input_authority(
        conn, authority, session_id=authority.session_id,
    )
    with pytest.raises(fm.FamilyMembershipError):
        fm.deactivate_family_member(
            conn, principal_actor_id=member, requester_actor_id=owner,
        )
    with pytest.raises(hsb.HumanSessionBindingError):
        hsb.bind_session_to_registered_human(
            conn, session_id="unsafe-unscoped-family-session", session_started_at=100,
            pipeline_key=PIPELINE_KEY, actor_id=owner, visibility_scope=None,
        )
    conn.close()

    with pytest.raises(hsb.HumanWakingInputWriteError):
        hsb.record_human_waking_input(
            tmp, pipeline_key=PIPELINE_KEY, authority=authority,
            message="stale session attempt", occurred_at=200,
        )


def test_fs1_contradictory_or_unauthorized_canonical_history_fails_closed(tmp_path):
    prov, ids = _seed_fs1_env(str(tmp_path))
    owner, member = ids["owner"], ids["member"]
    conn = sqlite3.connect(prov)
    now = int(time.time())
    # Simulate a canonical-store integrity failure outside the guarded
    # API: a member purports to enroll itself a second time. The ledger
    # is still append-only, but its impossible authority/state sequence
    # must not be interpreted as active membership.
    conn.execute(
        "INSERT INTO family_principal_events "
        "(event_id, group_key, principal_actor_id, actor_role, event_kind, "
        "display_label, requester_actor_id, occurred_at, record_created_at) "
        "VALUES ('forged-second-enroll', 'family_v0', ?, 'family_member', "
        "'enroll', 'Forged', ?, ?, ?)",
        (member, member, now, now),
    )
    conn.commit()
    assert fm.designated_owner_actor_id(conn) == owner
    assert fm.active_family_principals(conn) == frozenset()
    with pytest.raises(fm.FamilyMembershipError):
        fm.deactivate_family_member(
            conn, principal_actor_id=member, requester_actor_id=owner,
        )
    conn.close()


def _write_answered_turn(tmp, staging, auth, prompt, answer, occurred_at):
    hid = hsb.record_human_waking_input(
        tmp, pipeline_key=PIPELINE_KEY, authority=auth,
        message=prompt, occurred_at=occurred_at,
    )
    npw.stage_and_record_native_waking_turn(
        tmp, staging, session_id=auth.session_id, session_started_at=100,
        user_id="u", prompt=prompt, bounded_clause="", clark_prose=answer,
        kardia={}, controls={}, waking_model_tag="tag", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=False, occurred_at=occurred_at + 1,
        human_input_event_id=hid, visibility_scope=auth.visibility_scope,
    )
    return hid


def test_fs1_dialogue_delivers_cross_principal_shared_with_attribution(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    owner, member = ids["owner"], ids["member"]
    staging = os.path.join(tmp, "native_turn_staging.jsonl")

    member_shared = _bind(
        tmp, prov, member, fm.SCOPE_FAMILY_SHARED, "member-shared", 100,
    )
    _write_answered_turn(
        tmp, staging, member_shared, "SHARED_FROM_MEMBER", "shared answer", 110,
    )
    member_private = _bind(
        tmp, prov, member, fm.SCOPE_PRINCIPAL_PRIVATE, "member-private", 100,
    )
    _write_answered_turn(
        tmp, staging, member_private, "PRIVATE_FROM_MEMBER", "private answer", 120,
    )
    owner_private = _bind(
        tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE, "owner-private", 100,
    )
    current_h = hsb.record_human_waking_input(
        tmp, pipeline_key=PIPELINE_KEY, authority=owner_private,
        message="CURRENT_OWNER_PROMPT", occurred_at=130,
    )

    conn = sqlite3.connect(prov)
    active = fm.active_family_principals(conn)
    messages = session_dialogue_window.build_authenticated_dialogue_window(
        conn, current_h,
        viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        viewer_principal_actor_id=owner,
        active_family_principal_ids=active,
    )
    conn.close()
    rendered = "\n".join(m["content"] for m in messages)
    assert "SHARED_FROM_MEMBER" in rendered
    assert f"[Author principal: {member}]" in rendered
    assert "PRIVATE_FROM_MEMBER" not in rendered


def test_fs1_scoped_dialogue_cannot_enter_unscoped_workspace_handoff(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    member = ids["member"]
    staging = os.path.join(tmp, "native_turn_staging.jsonl")
    authority = _bind(
        tmp, prov, member, fm.SCOPE_FAMILY_SHARED, "member-handoff", 100,
    )
    hid = _write_answered_turn(
        tmp, staging, authority, "MEMBER_HANDOFF_TEXT", "Clark response", 140,
    )
    conn = sqlite3.connect(prov)
    x_event_id = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (hid,),
    ).fetchone()[0]
    conn.close()
    conn = sqlite3.connect(prov)
    pairs, _skipped = session_dialogue_window.collect_session_dialogue_pairs(
        conn, authority.session_id, staging,
    )
    conn.close()
    assert pairs == []

    with pytest.raises(workspace_episode_provenance.EpisodeProvenanceError):
        workspace_episode_provenance.record_episode_handoff(
            tmp, run_id="run-fs1-handoff", clark_actor_id=CLARK_ACTOR_ID,
            pipeline_key=PIPELINE_KEY, occurred_at=150, note="",
            source_excerpts=[{
                "handle": "d1", "author": "human", "text": "MEMBER_HANDOFF_TEXT",
                "event_id": x_event_id, "sequence": None,
                "principal_actor_id": member,
            }],
            model_revision_id=None,
        )


def test_fs1_designated_owner_preferred_in_canonical_resolution(tmp_path):
    prov, ids = _seed_fs1_env(str(tmp_path))
    conn = sqlite3.connect(prov)
    assert direction_control.resolve_canonical_human_actor_id(conn) == ids["owner"]
    conn.close()
    assert workspace_episode_provenance._resolve_canonical_human_actor_id(str(tmp_path)) == ids["owner"]

    # Pre-FS1 shape with exactly one human: unchanged legacy resolution.
    tmp2 = str(tmp_path / "prefs1")
    os.mkdir(tmp2)
    prov2 = os.path.join(tmp2, "anaxi_provenance.db")
    create_provenance_db(prov2).close()
    hir1_schema_migration.apply_additive_migration(prov2)
    conn2 = sqlite3.connect(prov2)
    conn2.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    conn2.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', 'nate', ?)",
        ("actor-nate-legacy", now),
    )
    conn2.commit()
    assert direction_control.resolve_canonical_human_actor_id(conn2) == "actor-nate-legacy"
    conn2.close()


# ------------------------------------------------------------------- writer


def test_fs1_native_writer_scope_cross_validation(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    owner = ids["owner"]
    auth = _bind(tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE)
    hid = hsb.record_human_waking_input(tmp, pipeline_key=PIPELINE_KEY, authority=auth,
                                        message="hi", occurred_at=100)
    staging = os.path.join(tmp, "native_turn_staging.jsonl")
    res = npw.stage_and_record_native_waking_turn(
        tmp, staging, session_id=auth.session_id, session_started_at=100, user_id="u", prompt="hi",
        bounded_clause="", clark_prose="answer", kardia={}, controls={},
        waking_model_tag="tag", pipeline_key=PIPELINE_KEY, artifact_pass_ran=True,
        occurred_at=110, human_input_event_id=hid, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    conn = sqlite3.connect(prov)
    row = conn.execute("SELECT visibility_scope FROM events WHERE event_id = ?", (res["event_id"],)).fetchone()
    assert row[0] == fm.SCOPE_PRINCIPAL_PRIVATE, row
    ac = conn.execute("SELECT visibility_scope FROM auth_contexts WHERE auth_context_id = ?",
                      (res["auth_context_id"],)).fetchone()
    assert ac[0] == fm.SCOPE_PRINCIPAL_PRIVATE, ac
    assert wtr0_waking_recovery.canonical_human_input_visibility_scope(
        conn, hid, owner,
    ) == fm.SCOPE_PRINCIPAL_PRIVATE
    projection_receipt = conversation_projection.verify_recovered_exchange_projection(
        conn, hid, res["event_id"], owner,
    )
    assert projection_receipt["conversational_text"] == "answer"
    conn.close()

    owner_projection = conversation_projection.project_waking_conversation(
        prov, include_event_metadata=True,
        viewer_principal_actor_id=owner,
        viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    member_projection = conversation_projection.project_waking_conversation(
        prov, include_event_metadata=True,
        viewer_principal_actor_id=ids["member"],
        viewer_visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    assert [row["event_id"] for row in owner_projection[-2:]] == [hid, res["event_id"]]
    assert hid not in {row["event_id"] for row in member_projection}
    assert res["event_id"] not in {row["event_id"] for row in member_projection}

    # visibility_scope is load-bearing in the expected native bundle
    # contract. A recovery contract that omits the private scope cannot
    # classify this scoped event as the already-committed turn.
    conn = sqlite3.connect(prov)
    expected = {
        "staging_id": res["staging_id"], "session_id": auth.session_id,
        "session_started_at": 100, "auth_context_id": res["auth_context_id"],
        "pipeline_id": res["pipeline_id"], "occurred_at": 110,
        "bounded_clause": "", "clark_prose": "answer",
        "artifact_pass_ran": True, "waking_model_tag": "tag",
        "visibility_scope": None,
    }
    with pytest.raises(npw.MigrationStopCondition):
        npw.verify_native_bundle_contract(conn, res["event_id"], expected=expected)
    conn.close()

    # Cross-validation: X scope disagreeing with H (recorded scope) is poisoned.
    auth2 = _bind(tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE)
    hid2 = hsb.record_human_waking_input(tmp, pipeline_key=PIPELINE_KEY, authority=auth2,
                                         message="hi2", occurred_at=100)
    with pytest.raises(npw.IncompleteEventBundleError):
        npw.stage_and_record_native_waking_turn(
            tmp, staging, session_id=auth2.session_id, session_started_at=100, user_id="u", prompt="hi2",
            bounded_clause="", clark_prose="answer", kardia={}, controls={},
            waking_model_tag="tag", pipeline_key=PIPELINE_KEY, artifact_pass_ran=True,
            occurred_at=120, human_input_event_id=hid2, visibility_scope=fm.SCOPE_FAMILY_SHARED,
        )
    # A caller cannot reuse another session's scoped H even when it
    # supplies the same textual scope value.
    with pytest.raises(npw.IncompleteEventBundleError):
        npw.stage_and_record_native_waking_turn(
            tmp, staging, session_id="forged-other-session", session_started_at=100,
            user_id="u", prompt="hi2", bounded_clause="", clark_prose="answer",
            kardia={}, controls={}, waking_model_tag="tag", pipeline_key=PIPELINE_KEY,
            artifact_pass_ran=True, occurred_at=121, human_input_event_id=hid2,
            visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        )


def test_private_operative_directive_keeps_source_scope_and_delivery_boundary(tmp_path):
    tmp = str(tmp_path)
    prov, ids = _seed_fs1_env(tmp)
    od1_schema_migration.apply_additive_migration(prov)
    owner, member = ids["owner"], ids["member"]
    auth = _bind(
        tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE,
        session_id="directive-owner-private", session_started_at=100,
    )
    hid = hsb.record_human_waking_input(
        tmp, pipeline_key=PIPELINE_KEY, authority=auth,
        message="adopt a private directive", occurred_at=100,
    )
    waking = npw.stage_and_record_native_waking_turn(
        tmp, os.path.join(tmp, "directive_staging.jsonl"),
        session_id=auth.session_id, session_started_at=100, user_id="u",
        prompt="adopt a private directive", bounded_clause="", clark_prose="adopted",
        kardia={}, controls={}, waking_model_tag="tag", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=True, occurred_at=110, human_input_event_id=hid,
        visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        operative_directive_request="set_directive",
        operative_directive_text="Keep this private occurrence scoped.",
    )
    directive_event_id = od1.generate_directive_event_id()
    active = od1.record_operative_directive_activation(
        tmp, event_id=directive_event_id, session_id=auth.session_id,
        session_started_at=100, occurred_at=110,
        directive_text="Keep this private occurrence scoped.",
        triggering_waking_turn_event_id=waking["event_id"],
    )
    assert active["active_event_id"] == directive_event_id

    conn = sqlite3.connect(prov)
    directive_scope = conn.execute(
        "SELECT e.visibility_scope, ac.visibility_scope FROM events e "
        "JOIN auth_contexts ac ON ac.auth_context_id=e.auth_context_id WHERE e.event_id=?",
        (directive_event_id,),
    ).fetchone()
    owner_view = fm.family_session_view(
        conn, principal_actor_id=owner, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    member_view = fm.family_session_view(
        conn, principal_actor_id=member, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    conn.close()
    assert directive_scope == (
        fm.SCOPE_PRINCIPAL_PRIVATE, fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    assert od1.fetch_active_directive_for_viewer(
        tmp, family_feature_active=True, family_view=owner_view,
    )["active_event_id"] == directive_event_id
    assert od1.fetch_active_directive_for_viewer(
        tmp, family_feature_active=True, family_view=member_view,
    ) is None
    assert od1.fetch_active_directive_for_viewer(
        tmp, family_feature_active=True, family_view=None,
    ) is None


# ------------------------------------------------------------- end-to-end


def _captured_chat_messages():
    """Return the messages of the LAST fake ollama.chat() call (the one
    that produced the user-visible reply)."""
    captured = _run_family_e2e._captured["messages"]
    return captured[-1] if captured else []


def _run_family_e2e(tmp, variant, legacy_graph=False):
    """One run_waking_turn() against a REAL llama_anaxi with a fake
    ollama, for one of 'unbound' | 'owner_private' | 'member_shared'.
    Returns the run result dict and an auth/ids context."""
    assert variant in ("unbound", "owner_private", "member_shared")
    # Some sibling suites permanently install FAKE module objects into
    # sys.modules (llama_anaxi/orchestration/relational_history/...).
    # Drop any such residue so this harness imports the REAL modules --
    # each test is otherwise order-dependent on whatever ran before it.
    for _m in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite", "relational_history",
               "migrate_historical_data", "native_provenance_writer", "native_turn_staging",
               "conversation_direction", "signal_observation_log", "context_budget"):
        sys.modules.pop(_m, None)
    fake = types.ModuleType("ollama")
    captured = _run_family_e2e._captured = {"messages": []}

    # Targeted dependency stub: sentence_transformers is imported
    # top-level by anaxi_protocol_sqlite but only CONSTRUCTED inside
    # the ConstitutionalMind initializer (which this harness bypasses
    # with __new__ + manual _init_core_tables, exactly like the repo's
    # own test_native_waking_turn_end_to_end.py). Stubbing the package
    # make the REAL waking path importable in this offline environment
    # without ever touching the embedder class.
    _stub = types.ModuleType("sentence_transformers")
    _stub.SentenceTransformer = object
    sys.modules.setdefault("sentence_transformers", _stub)

    def fake_chat(model, messages, format=None, options=None, think=None):
        captured["messages"].append(messages)
        if _is_pass1_format(format):
            content = json.dumps(_PASS1_TYPED_ACT)
        else:
            # Ordinary Pass 2 is a plain chat completion (shared expression seam):
            # the reply text itself, no container, no marker.
            content = "A synthetic scoped-family reply."
        prompt_eval_count = sum(len(m["content"].encode("utf-8")) for m in messages)
        return {"message": {"content": content}, "prompt_eval_count": prompt_eval_count,
                "done": True, "done_reason": "stop", "eval_count": 100}

    class _FakeModelEntry:
        def __init__(self, model, digest):
            self.model = model
            self.digest = digest

    class _FakeListResponse:
        def __init__(self, models):
            self.models = models

    fake.chat = fake_chat
    fake.list = lambda: _FakeListResponse([_FakeModelEntry("gemma4:e4b", _CALIBRATED_MODEL_DIGEST)])
    sys.modules["ollama"] = fake

    import llama_anaxi
    import orchestration
    import anaxi_protocol_sqlite
    from relational_history import RelationalHistory
    from migrate_historical_data import alter_existing_stores_schema

    # Pre-seed the live-environment cache with the SAME calibration
    # fingerprint the Pass-1/Pass-2 scaffold costs were earned against,
    # so the conversation path uses its calibrated (fits) budget rather
    # than the conservative byte estimate. This mirrors the repo's own
    # test_owc5_s2_integration.fresh_llama_anaxi() harness verbatim;
    # without it the unmodified production pass1 budget exceeds its
    # conservative fallback and raises BUDGET_EXCEEDED before the
    # contribution seam this test exists to exercise is ever reached.
    llama_anaxi._ollama_environment_cache.update({
        "checked": True, "model_digest": _CALIBRATED_MODEL_DIGEST,
        "server_version": _CALIBRATED_SERVER_VERSION,
        "chat_template_sha256": _CALIBRATED_CHAT_TEMPLATE_SHA256,
    })
    llama_anaxi.reset_working_set()
    # The production Pass-1/Pass-2 budget constants are calibrated to a
    # specific live Ollama host; this offline harness lifts them only for
    # this test so the hard pass1 preflight is never the thing under test
    # (the contribution-scope seam below is). No production constant is
    # modified outside the test process.
    import context_budget
    context_budget.PASS1_MAX_PROMPT_BUDGET = 400000
    context_budget.PASS2_MAX_PROMPT_BUDGET = 400000

    prov, ids = _seed_fs1_env(tmp)
    mind_db_path = os.path.join(tmp, "anaxi_mind_llama.db")
    mind = anaxi_protocol_sqlite.ConstitutionalMind.__new__(anaxi_protocol_sqlite.ConstitutionalMind)
    mind.db_path = mind_db_path
    mind.conn = sqlite3.connect(mind_db_path, timeout=30.0)
    mind.cursor = mind.conn.cursor()
    mind.cursor.execute("PRAGMA foreign_keys = ON;")
    mind.conn.commit()
    mind._init_core_tables()
    # The owner-private session may now retrieve pre-Family legacy graph
    # nodes (legacy owner-private seam); this graph is empty, so a null
    # embedder is enough to exercise the real retrieval path.
    import numpy as _np
    _vec = _np.ones(4, dtype=_np.float32) if legacy_graph else _np.zeros(4, dtype=_np.float32)
    mind.embedder = types.SimpleNamespace(encode=lambda text, convert_to_numpy=True: _vec)
    if legacy_graph:
        # Two graph nodes with identical embeddings: one asserted long before the
        # Family activation boundary, one after it (relative to the real clock).
        for _nid, _desc, _asserted in (
            ("n-pre", "LEGACY_GRAPH_PRE", 1000), ("n-post", "LEGACY_GRAPH_POST", int(time.time()) + 100000),
        ):
            mind.cursor.execute(
                "INSERT INTO nodes (user_id, id, label, type, salience_score, description, "
                "last_accessed_timestamp, asserted_at) VALUES ('nate', ?, ?, 'fact', 1.0, ?, 0, ?)",
                (_nid, _nid, _desc, _asserted),
            )
            mind.cursor.execute(
                "INSERT INTO node_embeddings (user_id, node_id, embedding) VALUES ('nate', ?, ?)",
                (_nid, _vec.tobytes()),
            )
        mind.conn.commit()

    orch = orchestration.AnaxiOrchestrator.__new__(orchestration.AnaxiOrchestrator)
    orch.mind = mind

    recipient_principal = ids["member"]
    owner = ids["owner"]
    staging_path = os.path.join(tmp, "native_turn_staging.jsonl")
    shared_auth = _bind(
        tmp, prov, recipient_principal, fm.SCOPE_FAMILY_SHARED,
        "e2e-source-shared", 100,
    )
    shared_h = _write_answered_turn(
        tmp, staging_path, shared_auth, "shared source", "MEMBER_SHARED_MARKER", 110,
    )
    private_auth = _bind(
        tmp, prov, recipient_principal, fm.SCOPE_PRINCIPAL_PRIVATE,
        "e2e-source-private", 100,
    )
    private_h = _write_answered_turn(
        tmp, staging_path, private_auth, "private source", "MEMBER_PRIVATE_MARKER", 120,
    )
    conn = sqlite3.connect(prov)
    shared_eid = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (shared_h,),
    ).fetchone()[0]
    private_member_eid = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (private_h,),
    ).fetchone()[0]
    conn.close()

    foreign_items = _mk_hippo_items(
        (private_member_eid, CLARK_ACTOR_ID, "MEMBER_PRIVATE_MARKER"),
        (shared_eid, CLARK_ACTOR_ID, "MEMBER_SHARED_MARKER"),
    )
    legacy_marker = "LEGACY_RETRIEVAL_MARKER"

    seen_messages = {}

    def fake_prepare(user_id, prompt):
        orch.prepare_context = None  # not re-entrant; one-shot stub below
        return {
            "messages": [
                {"role": "system", "content": "You are Clark."},
                {"role": "user", "content": prompt},
            ],
            "controls": {"temperature": 0.4, "top_p": 0.85},
            "kardia": {"aesthetic_valve": "warm"},
            "memory_context": "",
            "legacy_retrieval_text": legacy_marker,
            "hippocampal_retrieval_result": hippocampus_retrieval.RetrievalResult(
                query_terms=("marker",), items=tuple(foreign_items),
            ),
        }

    orch.prepare_context = fake_prepare

    llama_anaxi.OBSIDIAN_WORKSPACE_ROOT = os.path.join(tmp, "obsidian")
    llama_anaxi.DB_PATH = mind_db_path
    llama_anaxi.RELATIONAL_DB_PATH = os.path.join(tmp, "anaxi_relational_llama.db")
    llama_anaxi.PROVENANCE_DB_DIR = tmp
    llama_anaxi.STAGING_PATH = staging_path
    llama_anaxi.LOG_FILE = os.path.join(tmp, "anaxi_log.jsonl")
    RelationalHistory(llama_anaxi.RELATIONAL_DB_PATH).close()
    # Same §3 additive schema application the repo's own e2e harness does:
    # the real waking path writes the additive generation-controls columns.
    _rel_conn = sqlite3.connect(llama_anaxi.RELATIONAL_DB_PATH)
    _alt = alter_existing_stores_schema(_rel_conn, mind.conn)
    _rel_conn.close()

    import signal_observation_log
    real_record_observation = signal_observation_log.record_observation
    test_signal_log_path = os.path.join(tmp, "signal_observations.jsonl")
    llama_anaxi.record_observation = lambda text: real_record_observation(text, log_path=test_signal_log_path)

    if variant == "unbound":
        authority = None
    else:
        # Bind to THIS process's real native session id, so
        # validate_human_input_authority() (which run_waking_turn calls
        # with its own _get_native_session() id) genuinely matches.
        _sid = llama_anaxi.get_current_session_id()
        _sat = llama_anaxi.get_current_session_started_at()
        if variant == "owner_private":
            authority = _bind(tmp, prov, owner, fm.SCOPE_PRINCIPAL_PRIVATE, _sid, _sat)
        else:
            authority = _bind(tmp, prov, recipient_principal, fm.SCOPE_FAMILY_SHARED, _sid, _sat)

    llama_anaxi.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(tmp, "conversation_direction_trace.jsonl")
    result = llama_anaxi.run_waking_turn(
        orch, NEUTRAL_PROMPT, interaction_mode=llama_anaxi.CONVERSATION_MODE,
        human_input_authority=authority,
    )
    mind.conn.close()
    sys.modules.pop("llama_anaxi", None)
    sys.modules.pop("orchestration", None)
    sys.modules.pop("anaxi_protocol_sqlite", None)
    sys.modules.pop("relational_history", None)

    system_content = ""
    for call_messages in _run_family_e2e._captured["messages"]:
        for m in call_messages:
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                system_content += "\n" + m["content"]

    scope_row = sqlite3.connect(prov).execute(
        "SELECT visibility_scope FROM events WHERE event_id = ?", (result["native_event_id"],),
    ).fetchone()[0]
    return {
        "result": result,
        "system_content": system_content,
        "event_scope": scope_row,
        "authority": authority,
    }


def test_fs1_run_waking_turn_scope_suppression_end_to_end(tmp_path):
    for variant, expect_legacy, expect_member_private, expect_member_shared, expect_scope in (
        ("unbound", False, False, False, None),
        ("owner_private", False, False, True, fm.SCOPE_PRINCIPAL_PRIVATE),
        ("member_shared", False, False, True, fm.SCOPE_FAMILY_SHARED),
    ):
        tmp = str(tmp_path / variant)
        os.mkdir(tmp)
        env = _run_family_e2e(tmp, variant)
        assert env["event_scope"] == expect_scope, (variant, env["event_scope"])
        assert ("LEGACY_RETRIEVAL_MARKER" in env["system_content"]) is expect_legacy, variant
        assert ("MEMBER_PRIVATE_MARKER" in env["system_content"]) is expect_member_private, variant
        assert ("MEMBER_SHARED_MARKER" in env["system_content"]) is expect_member_shared, variant
