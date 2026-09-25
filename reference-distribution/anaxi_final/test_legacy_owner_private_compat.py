"""Legacy single-owner (pre-Family) continuity compatibility.

Family activation makes FS1 fail closed for NULL-scope history, which would
strand the owner's own pre-Family continuity (Clark-authored replies, memory
derived from them, the legacy graph).  The ONE compatibility seam in
family_membership (`legacy_owner_private_*`) restores it as a READ-TIME
eligibility fact for the canonical owner's principal_private session only.

These tests pin the law, its canonical boundary, its non-mutation of history,
and that no Family/private isolation was widened.

Run:
    python -m pytest test_legacy_owner_private_compat.py -q
"""
import hashlib
import os
import sqlite3
import time

import pytest

import conversation_projection
import family_membership as fm
import fs1_schema_migration
import hir1_schema_migration
import session_dialogue_window
import test_fs1_family_shared_expansion_v0 as fs1t
from hir1_registration import HOST_ACTOR_ID, register_canonical_human
from provenance_schema import create_provenance_db

PRIVATE = fm.SCOPE_PRINCIPAL_PRIVATE
SHARED = fm.SCOPE_FAMILY_SHARED
CLARK = fs1t.CLARK_ACTOR_ID
ACTIVATION_OFFSET = 100000    # the canonical boundary sits well after "now"


def _raw_event(prov, event_id, event_type, when, components=(), scope=None):
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    with conn:
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            " auth_context_id, occurred_at, record_created_at, visibility_scope) "
            "VALUES (?, ?, 'pipe-llama-1', 'known', NULL, ?, ?, ?)",
            (event_id, event_type, when, when, scope),
        )
        for i, (kind, creator, text) in enumerate(components):
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                " authorship_resolution, component_text, content_sha256) "
                "VALUES (?, ?, ?, ?, 'resolved', ?, ?)",
                (event_id, i, creator, kind, text, hashlib.sha256(text.encode()).hexdigest()),
            )
    conn.close()


def _ro(prov):
    conn = sqlite3.connect(f"file:{prov}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _snapshot(prov):
    conn = _ro(prov)
    try:
        h = hashlib.sha256()
        for table in ("events", "event_components", "auth_contexts"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
                h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    tmp = str(tmp_path)
    prov = os.path.join(tmp, "anaxi_provenance.db")
    create_provenance_db(prov).close()
    hir1_schema_migration.apply_additive_migration(prov)
    fs1_schema_migration.apply_additive_migration(prov)
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-llama-1', ?, 'llama', 'test')", (fs1t.PIPELINE_KEY,),
    )
    now = int(time.time())
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (HOST_ACTOR_ID, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) "
                 "VALUES (?, 'bounded_clause_renderer')", (HOST_ACTOR_ID,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
                 "VALUES (?, 'clark_agent', 'clark', ?)", (CLARK, now))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (CLARK,))
    conn.commit()
    conn.row_factory = sqlite3.Row
    humans = {}
    for req, aab, label in (("req-o", "actor-o", "Owner"), ("req-m", "actor-m", "Member")):
        res, failure = register_canonical_human(
            conn, {"registration_request_id": req, "aab_actor_id": aab,
                   "display_label": label, "source": "local_operator_provisioning"},
        )
        assert failure is None, failure
        humans[label] = res["actor_id"]
    conn.row_factory = None
    conn.commit()
    owner, member = humans["Owner"], humans["Member"]

    # ---- the pre-Family single-owner regime: unscoped, NULL-scope history
    staging = os.path.join(tmp, "native_turn_staging.jsonl")
    legacy_auth = fs1t._bind(tmp, prov, owner, None, "legacy-sess", 100)
    legacy_h = fs1t._write_answered_turn(tmp, staging, legacy_auth, "LEGACY_PROMPT", "LEGACY_REPLY", 110)
    legacy_x = conn.execute(
        "SELECT event_id FROM event_components WHERE component_kind='human_input_event_id' "
        "AND component_text=?", (legacy_h,),
    ).fetchone()[0]

    # ---- pre-Family workspace episodes (host-written, NULL scope, as they always were)
    import workspace_episode_provenance as wep
    for run, t0, ended in (("run-legacy-pending", 1000, True), ("run-legacy-active", 1100, False)):
        wep.record_episode_started(tmp, run_id=run, clark_actor_id=CLARK,
                                   pipeline_key=fs1t.PIPELINE_KEY, occurred_at=t0)
        wep.record_public_wait(tmp, run_id=run, pipeline_key=fs1t.PIPELINE_KEY, occurred_at=t0 + 1,
                               wait_minutes=1, model_revision_id=None)
        if ended:
            wep.record_episode_ended(tmp, run_id=run, pipeline_key=fs1t.PIPELINE_KEY, occurred_at=t0 + 2,
                                     termination_class=wep.TERMINATION_CLARK_STOP)

    # ---- canonical Family activation, on a controlled canonical clock
    boundary = int(time.time()) + ACTIVATION_OFFSET
    with monkeypatch.context() as m:
        m.setattr(fm.time, "time", lambda: boundary)
        fm.designate_owner(conn, principal_actor_id=owner, display_label="Owner",
                           requester_actor_id=HOST_ACTOR_ID)
        fm.enroll_family_member(conn, principal_actor_id=member, display_label="Member",
                                requester_actor_id=owner)
    conn.commit()
    conn.close()
    assert fm.family_activation_boundary(_ro(prov)) == boundary

    # ---- material around the boundary (direct rows: writers refuse NULL scope now)
    prose = lambda t: [("conversational_prose", CLARK, t)]  # noqa: E731
    _raw_event(prov, "evt-post", "waking_turn", boundary + 10, prose("POST_FAMILY_NULL"))
    _raw_event(prov, "evt-same-second", "waking_turn", boundary, prose("SAME_SECOND_NULL"))
    _raw_event(prov, "evt-pre-orphan", "waking_turn", boundary - 50, prose("ORPHAN_LEGACY_REPLY"))
    _raw_event(prov, "evt-pre-member-authored", "waking_turn", boundary - 50,
               prose("X") + [("human_conversational_input", member, "MEMBER_WORDS")])
    _raw_event(prov, "evt-pre-external-type", "discord_inbound_message", boundary - 50, prose("EXT_TYPE"))
    _raw_event(prov, "evt-pre-external-component", "waking_turn", boundary - 50,
               prose("EXT_COMP") + [("discord_inbound_carriage", CLARK, "carried")])
    _raw_event(prov, "evt-pre-link-missing", "waking_turn", boundary - 50,
               prose("LINK_MISSING") + [("human_input_event_id", CLARK, "evt-does-not-exist")])
    _raw_event(prov, "evt-post-orphan", "waking_turn", boundary + 20, prose("POST_ORPHAN_REPLY"))

    # ambiguous / post-Family workspace material
    _raw_event(prov, "evt-ws-amb-ended", "workspace_roaming_episode_ended", boundary - 50,
               [("workspace_roaming_run_id", HOST_ACTOR_ID, "run-amb"),
                ("roaming_handoff_source_excerpt", member, "MEMBER_WORDS")])
    _raw_event(prov, "evt-ws-post-null", "workspace_roaming_public_wait", boundary + 10,
               [("workspace_roaming_run_id", HOST_ACTOR_ID, "run-post-null")])

    # ---- Sleep derivations
    import sleep_c_schema
    sleep_c_schema.apply_additive_migration(prov)
    c = sqlite3.connect(prov)
    with c:
        for cycle_id, done in (("cyc-pre", boundary - 20), ("cyc-post", boundary + 5)):
            c.execute(
                "INSERT INTO sleep_cycles (cycle_id, window_start_component_id, window_end_component_id, "
                "selected_count, derivation_count, model_tag, started_at, completed_at) "
                "VALUES (?, 0, 0, 1, 1, 't', ?, ?)", (cycle_id, done - 1, done),
            )
        for did, cycle, done, sources in (
            ("deriv-ok", "cyc-pre", boundary - 20, [legacy_x]),
            ("deriv-post-cycle", "cyc-post", boundary + 5, [legacy_x]),
            ("deriv-bad-source", "cyc-pre", boundary - 20, ["evt-post"]),
            ("deriv-no-source", "cyc-pre", boundary - 20, []),
        ):
            c.execute(
                "INSERT INTO sleep_derivations (derivation_id, cycle_id, batch_index, item_index, "
                "derived_text, derived_text_sha256, model_tag, created_at) VALUES (?, ?, 0, 0, 'd', 'h', 't', ?)",
                (did, cycle, done),
            )
            for i, src in enumerate(sources):
                c.execute(
                    "INSERT INTO sleep_derivation_sources (derivation_id, wmu_id, primary_event_id, "
                    "cite_order, source_event_id, source_component_id, segment_id, segment_index) "
                    "VALUES (?, 'w', ?, ?, ?, ?, 's', 0)", (did, src, i, src, i),
                )
    c.close()

    # ---- scoped post-Family material (real writers)
    owner_priv = fs1t._bind(tmp, prov, owner, PRIVATE, "owner-private-old", 100)
    fs1t._write_answered_turn(tmp, staging, owner_priv, "OWNER_PRIVATE_PROMPT", "OWNER_PRIVATE_REPLY", 300)
    member_shared = fs1t._bind(tmp, prov, member, SHARED, "member-shared-old", 100)
    fs1t._write_answered_turn(tmp, staging, member_shared, "MEMBER_SHARED_PROMPT", "MEMBER_SHARED_REPLY", 310)

    return {"tmp": tmp, "prov": prov, "owner": owner, "member": member, "boundary": boundary,
            "legacy_h": legacy_h, "legacy_x": legacy_x, "staging": staging}


def _can(w, viewer, scope, event_id):
    conn = _ro(w["prov"])
    try:
        return fm.can_receive_canonical_event(
            conn, event_id, viewer_principal_actor_id=viewer, viewer_scope=scope,
            active_family_principal_ids=fm.active_family_principals(conn),
        )
    finally:
        conn.close()


def _hippo(w, viewer, scope, *event_ids):
    import dataclasses
    # Faithful to hippocampus_store: a Sleep-derivation item carries source_store "sleep_derivations".
    items = [dataclasses.replace(i, source_store="sleep_derivations") if i.event_id.startswith("deriv-") else i
             for i in fs1t._mk_hippo_items(*[(e, CLARK, "content") for e in event_ids])]
    conn = _ro(w["prov"])
    try:
        return {i.event_id for i in fm.permitted_hippocampal_items(
            conn, items, viewer_principal_actor_id=viewer, viewer_scope=scope,
            active_family_principal_ids=fm.active_family_principals(conn),
        )}
    finally:
        conn.close()


# ------------------------------------------------------------------ law


def test_legacy_history_stays_historically_null_and_is_never_rewritten(world):
    w = world
    before = _snapshot(w["prov"])
    conn = _ro(w["prov"])
    null_rows = conn.execute(
        "SELECT event_id, visibility_scope FROM events WHERE event_id IN (?, ?)",
        (w["legacy_h"], w["legacy_x"]),
    ).fetchall()
    conn.close()
    assert {r[0] for r in null_rows} == {w["legacy_h"], w["legacy_x"]}
    assert all(r[1] is None for r in null_rows)          # original NULL scope intact

    # Exercise every consumer, then prove nothing was written or backfilled.
    for viewer, scope in ((w["owner"], PRIVATE), (w["owner"], SHARED), (w["member"], PRIVATE), (w["member"], SHARED)):
        _can(w, viewer, scope, w["legacy_x"])
        _hippo(w, viewer, scope, w["legacy_x"], "deriv-ok")
    conversation_projection.project_waking_conversation(
        w["prov"], viewer_principal_actor_id=w["owner"], viewer_visibility_scope=PRIVATE)
    assert _snapshot(w["prov"]) == before
    conn = _ro(w["prov"])
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE visibility_scope IS NULL AND event_id IN (?, ?)",
        (w["legacy_h"], w["legacy_x"]),
    ).fetchone()[0] == 2
    conn.close()


def test_owner_private_session_receives_proven_legacy_history(world):
    w = world
    assert _can(w, w["owner"], PRIVATE, w["legacy_x"])
    assert _can(w, w["owner"], PRIVATE, w["legacy_h"])
    assert _can(w, w["owner"], PRIVATE, "evt-pre-orphan")
    assert _hippo(w, w["owner"], PRIVATE, w["legacy_x"], "evt-pre-orphan") == {w["legacy_x"], "evt-pre-orphan"}
    conn = _ro(w["prov"])
    reason = fm.legacy_owner_private_reason(
        conn, w["legacy_x"], viewer_principal_actor_id=w["owner"], viewer_scope=PRIVATE)
    conn.close()
    assert reason == fm.LEGACY_OWNER_PRIVATE_REASON
    assert "before Family scope existed" in reason and "not retroactively changed" in reason


def test_pre_family_sleep_derivation_qualifies_only_from_legacy_sources(world):
    w = world
    assert _hippo(w, w["owner"], PRIVATE, "deriv-ok") == {"deriv-ok"}
    # Sleep-derivation intersection law (whole-system completion): a derivation is deliverable iff
    # EVERY cited source is deliverable to the viewer.  deriv-post-cycle cites only the owner's own
    # legacy history, so it now reaches his private session even though its cycle ran after Family
    # activation (the old cycle-time rule withheld every post-Family consolidation from everyone).
    assert _hippo(w, w["owner"], PRIVATE, "deriv-post-cycle") == {"deriv-post-cycle"}
    assert _hippo(w, w["owner"], PRIVATE, "deriv-bad-source", "deriv-no-source") == set()
    assert _hippo(w, w["member"], PRIVATE, "deriv-ok") == set()
    assert _hippo(w, w["member"], SHARED, "deriv-ok") == set()


def test_legacy_history_is_not_eligible_in_shared_or_other_principal_sessions(world):
    w = world
    for eid in (w["legacy_x"], w["legacy_h"], "evt-pre-orphan"):
        assert not _can(w, w["member"], SHARED, eid)             # Blair's shared Caret session
        assert not _can(w, w["member"], PRIVATE, eid)            # another principal's private session
        assert not _can(w, w["owner"], SHARED, eid)              # Alex's own shared Caret session
        assert not _can(w, None, None, eid)                      # identity-less
    assert _hippo(w, w["member"], SHARED, w["legacy_x"], "evt-pre-orphan") == set()
    assert _hippo(w, w["member"], PRIVATE, w["legacy_x"], "evt-pre-orphan") == set()
    assert _hippo(w, w["owner"], SHARED, w["legacy_x"], "evt-pre-orphan") == set()
    assert _hippo(w, None, None, w["legacy_x"]) == set()


def test_post_family_and_ambiguous_null_scope_remain_fail_closed(world):
    w = world
    denied = (
        "evt-post", "evt-post-orphan",               # post-Family NULL
        "evt-same-second",                           # boundary second is ambiguous
        "evt-pre-member-authored",                   # another human contradicts owner provenance
        "evt-pre-external-type", "evt-pre-external-component",   # external / correspondent data
        "evt-pre-link-missing",                      # X whose H cannot be established
        "evt-does-not-exist",
    )
    for eid in denied:
        assert not _can(w, w["owner"], PRIVATE, eid), eid
    assert _hippo(w, w["owner"], PRIVATE, *denied) == set()


def test_explicit_scopes_behave_exactly_as_before(world):
    w = world
    conn = _ro(w["prov"])
    rows = dict(conn.execute(
        "SELECT ct.component_text, x.event_id FROM events x JOIN event_components ct ON ct.event_id=x.event_id "
        "WHERE ct.component_kind='conversational_prose' AND x.visibility_scope IS NOT NULL"
    ).fetchall())
    conn.close()
    owner_private_x, member_shared_x = rows["OWNER_PRIVATE_REPLY"], rows["MEMBER_SHARED_REPLY"]
    # principal_private: owner only, private session only
    assert _can(w, w["owner"], PRIVATE, owner_private_x)
    assert not _can(w, w["member"], PRIVATE, owner_private_x)
    assert not _can(w, w["owner"], SHARED, owner_private_x)
    # family_shared: every active principal, private or shared session
    for viewer, scope in ((w["owner"], PRIVATE), (w["owner"], SHARED), (w["member"], PRIVATE), (w["member"], SHARED)):
        assert _can(w, viewer, scope, member_shared_x)
    assert not _can(w, None, None, member_shared_x)
    # Blair's shared session receives only FAMILY_SHARED continuity
    assert _hippo(w, w["member"], SHARED, owner_private_x, member_shared_x, w["legacy_x"]) == {member_shared_x}


# ---------------------------------------------------- consumers / windows


def _current_h(w, actor, scope, sid):
    auth = fs1t._bind(w["tmp"], w["prov"], actor, scope, sid, 100)
    import human_session_binding as hsb
    return hsb.record_human_waking_input(
        w["tmp"], pipeline_key=fs1t.PIPELINE_KEY, authority=auth, message="CURRENT", occurred_at=900,
    )


def _window(w, actor, scope, sid):
    h = _current_h(w, actor, scope, sid)
    conn = _ro(w["prov"])
    try:
        msgs = session_dialogue_window.build_authenticated_dialogue_window(
            conn, h, viewer_visibility_scope=scope, viewer_principal_actor_id=actor,
            active_family_principal_ids=fm.active_family_principals(conn),
        )
    finally:
        conn.close()
    return "\n".join(m["content"] for m in msgs)


def test_owner_private_dialogue_window_keeps_pre_family_continuity(world):
    w = world
    text = _window(w, w["owner"], PRIVATE, "owner-window-private")
    assert "LEGACY_PROMPT" in text and "LEGACY_REPLY" in text
    assert "OWNER_PRIVATE_REPLY" in text
    assert "MEMBER_SHARED_REPLY" in text          # lawful family_shared continuity in a private session


def test_shared_dialogue_windows_get_only_family_shared_continuity(world):
    w = world
    for actor, sid in ((w["member"], "member-window-shared"), (w["owner"], "owner-window-shared")):
        text = _window(w, actor, SHARED, sid)
        assert "MEMBER_SHARED_REPLY" in text
        assert "LEGACY_PROMPT" not in text and "LEGACY_REPLY" not in text
        assert "OWNER_PRIVATE" not in text
    mtext = _window(w, w["member"], PRIVATE, "member-window-private")
    assert "LEGACY_PROMPT" not in mtext and "OWNER_PRIVATE" not in mtext


def test_gui_projection_keeps_owner_legacy_history_and_isolates_others(world):
    w = world
    def contents(actor, scope):
        return "\n".join(r["content"] for r in conversation_projection.project_waking_conversation(
            w["prov"], viewer_principal_actor_id=actor, viewer_visibility_scope=scope))
    owner_view = contents(w["owner"], PRIVATE)
    assert "LEGACY_PROMPT" in owner_view and "LEGACY_REPLY" in owner_view
    assert "ORPHAN_LEGACY_REPLY" in owner_view            # pre-A4c orphan X via the seam
    assert "POST_ORPHAN_REPLY" not in owner_view and "SAME_SECOND_NULL" not in owner_view
    for actor, scope in ((w["member"], SHARED), (w["member"], PRIVATE), (w["owner"], SHARED)):
        view = contents(actor, scope)
        assert "LEGACY_" not in view and "ORPHAN_LEGACY_REPLY" not in view


# -------------------------------------------------------- seam boundaries


def test_seam_view_is_owner_private_only_and_inert_before_family(world, tmp_path):
    w = world
    conn = _ro(w["prov"])
    assert fm.legacy_owner_private_view(conn, viewer_principal_actor_id=w["owner"], viewer_scope=PRIVATE)["boundary"] == w["boundary"]
    for viewer, scope in ((w["owner"], SHARED), (w["member"], PRIVATE), (w["member"], SHARED), (None, None), (w["owner"], None)):
        assert fm.legacy_owner_private_view(conn, viewer_principal_actor_id=viewer, viewer_scope=scope) is None
    conn.close()

    # A database in which Family was never activated has no boundary and no seam.
    fresh = os.path.join(str(tmp_path), "fresh.db")
    create_provenance_db(fresh).close()
    fs1_schema_migration.apply_additive_migration(fresh)
    c = _ro(fresh)
    assert fm.family_activation_boundary(c) is None
    assert fm.legacy_owner_private_view(c, viewer_principal_actor_id="x", viewer_scope=PRIVATE) is None
    c.close()


# ----------------------------------------------------- legacy graph blob


def _system_text(env):
    return env["system_content"]


def test_legacy_graph_continuity_only_for_owner_private_and_only_pre_boundary(tmp_path):
    seen = {}
    for variant in ("unbound", "owner_private", "member_shared"):
        d = tmp_path / variant
        d.mkdir()
        seen[variant] = _system_text(fs1t._run_family_e2e(str(d), variant, legacy_graph=True))
    assert "LEGACY_GRAPH_PRE" in seen["owner_private"]
    assert "LEGACY_GRAPH_POST" not in seen["owner_private"]       # asserted after the boundary
    assert "LEGACY_RETRIEVAL_MARKER" not in seen["owner_private"]  # the unbounded blob stays withheld
    for variant in ("unbound", "member_shared"):
        assert "LEGACY_GRAPH_PRE" not in seen[variant]
        assert "LEGACY_GRAPH_POST" not in seen[variant]


# ------------------------------------------------ workspace active/episode


def _ws_continuity(w, monkeypatch, actor, scope, session_label):
    import llama_anaxi as la
    import workspace_public_continuity as wpc
    monkeypatch.setattr(la, "PROVENANCE_DB_DIR", w["tmp"])
    conn = _ro(w["prov"])
    try:
        view = fm.family_session_view(conn, principal_actor_id=actor, visibility_scope=scope)
    finally:
        conn.close()
    run = wpc.find_active_roaming_run_id(w["tmp"])
    active = wpc.collect_active_workspace_events(w["tmp"], run, None, wpc.compute_high_water_mark(w["tmp"])) if run else []
    return la._family_workspace_continuity(view, frozenset(), run, active), run, active


def test_owner_private_keeps_pre_family_workspace_episode_and_active_continuity(world, monkeypatch):
    w = world
    (run_id, _enc, text, active_out), active_run, active_in = _ws_continuity(w, monkeypatch, w["owner"], PRIVATE, "o")
    assert active_run == "run-legacy-active" and active_in
    assert run_id == "run-legacy-pending"          # newer ambiguous run-amb is skipped, not the whole capability
    assert "Public waits: 1" in text
    assert [e["event_id"] for e in active_out] == [e["event_id"] for e in active_in]


def test_workspace_continuity_is_unavailable_to_shared_and_other_principals(world, monkeypatch):
    w = world
    for actor, scope in ((w["member"], SHARED), (w["member"], PRIVATE), (w["owner"], SHARED), (None, None)):
        (run_id, enc, text, active_out), _r, active_in = _ws_continuity(w, monkeypatch, actor, scope, "x")
        assert active_in                       # the material exists ...
        assert (run_id, enc, text, active_out) == (None, None, "", [])   # ... and is withheld


def test_ambiguous_legacy_workspace_material_stays_fail_closed(world):
    w = world
    for eid in ("evt-ws-amb-ended", "evt-ws-post-null"):
        assert not _can(w, w["owner"], PRIVATE, eid)
    assert _hippo(w, w["owner"], PRIVATE, "evt-ws-amb-ended", "evt-ws-post-null") == set()


def test_post_family_workspace_writes_use_native_scope_not_legacy_compat(world, monkeypatch):
    import workspace_episode_provenance as wep
    w = world
    run = "run-post-native"
    now = int(time.time())
    started = wep.record_episode_started(w["tmp"], run_id=run, clark_actor_id=CLARK,
                                         pipeline_key=fs1t.PIPELINE_KEY, occurred_at=now)
    wep.record_public_wait(w["tmp"], run_id=run, pipeline_key=fs1t.PIPELINE_KEY, occurred_at=now + 1,
                           wait_minutes=1, model_revision_id=None)
    ended = wep.record_episode_ended(w["tmp"], run_id=run, pipeline_key=fs1t.PIPELINE_KEY, occurred_at=now + 2,
                                     termination_class=wep.TERMINATION_CLARK_STOP)
    conn = _ro(w["prov"])
    scopes = {r[0]: r[1] for r in conn.execute("SELECT event_id, visibility_scope FROM events WHERE event_id IN (?, ?)",
                                              (started, ended))}
    legacy_scopes = [r[0] for r in conn.execute(
        "SELECT visibility_scope FROM events WHERE event_type LIKE 'workspace_%' AND event_id NOT IN "
        "(SELECT event_id FROM events WHERE visibility_scope IS NOT NULL) AND occurred_at < 2000")]
    conn.close()
    assert set(scopes.values()) == {PRIVATE}                 # native current scope
    assert legacy_scopes and all(v is None for v in legacy_scopes)   # history untouched
    for eid in (started, ended):
        assert _can(w, w["owner"], PRIVATE, eid)
        assert not _can(w, w["member"], PRIVATE, eid)
        assert not _can(w, w["member"], SHARED, eid)
        assert not _can(w, w["owner"], SHARED, eid)
        assert not _can(w, None, None, eid)
    assert _hippo(w, w["owner"], PRIVATE, started, ended) == {started, ended}
    assert _hippo(w, w["member"], SHARED, started, ended) == set()
    (run_id, _e, _t, _a), _r, _i = _ws_continuity(w, monkeypatch, w["owner"], PRIVATE, "o")
    assert run_id == run                                     # newest eligible pending episode
    (run_id, *_rest), _r, _i = _ws_continuity(w, monkeypatch, w["member"], SHARED, "m")
    assert run_id is None


def test_workspace_continuity_reads_never_rewrite_history(world, monkeypatch):
    w = world
    before = _snapshot(w["prov"])
    _ws_continuity(w, monkeypatch, w["owner"], PRIVATE, "o")
    _ws_continuity(w, monkeypatch, w["member"], SHARED, "m")
    assert _snapshot(w["prov"]) == before


def test_post_family_sleep_derivations_follow_the_intersection_of_their_sources(world):
    """Post-Family consolidation of SCOPED material reaches exactly the viewers who may receive every
    source -- never widened (a mixed-source derivation stays with the narrowest), never withheld from
    everyone (the pre-repair behavior)."""
    w = world
    conn = sqlite3.connect(w["prov"])
    rows = dict(conn.execute(
        "SELECT ct.component_text, x.event_id FROM events x JOIN event_components ct ON ct.event_id=x.event_id "
        "WHERE ct.component_kind='conversational_prose' AND x.visibility_scope IS NOT NULL").fetchall())
    owner_private_x, member_shared_x = rows["OWNER_PRIVATE_REPLY"], rows["MEMBER_SHARED_REPLY"]
    conn.execute("INSERT INTO sleep_cycles (cycle_id, window_start_component_id, window_end_component_id, "
                 "selected_count, derivation_count, model_tag, started_at, completed_at) "
                 "VALUES ('cyc-scoped', 0, 0, 1, 1, 't', ?, ?)", (w["boundary"] + 400, w["boundary"] + 401))
    for did, sources in (("deriv-owner-private", [owner_private_x]), ("deriv-shared", [member_shared_x]),
                         ("deriv-mixed", [owner_private_x, member_shared_x])):
        conn.execute("INSERT INTO sleep_derivations (derivation_id, cycle_id, batch_index, item_index, "
                     "derived_text, derived_text_sha256, model_tag, created_at) VALUES (?, 'cyc-scoped', 0, 0, 'd', 'h', 't', ?)",
                     (did, w["boundary"] + 401))
        for i, src in enumerate(sources):
            conn.execute("INSERT INTO sleep_derivation_sources (derivation_id, wmu_id, primary_event_id, cite_order, "
                         "source_event_id, source_component_id, segment_id, segment_index) VALUES (?, 'w', ?, ?, ?, ?, 's', 0)",
                         (did, src, i, src, i))
    conn.commit()
    conn.close()
    everything = ("deriv-owner-private", "deriv-shared", "deriv-mixed")
    assert _hippo(w, w["owner"], PRIVATE, *everything) == set(everything)
    assert _hippo(w, w["owner"], SHARED, *everything) == {"deriv-shared"}
    assert _hippo(w, w["member"], PRIVATE, *everything) == {"deriv-shared"}
    assert _hippo(w, w["member"], SHARED, *everything) == {"deriv-shared"}
    assert _hippo(w, None, None, *everything) == set()
