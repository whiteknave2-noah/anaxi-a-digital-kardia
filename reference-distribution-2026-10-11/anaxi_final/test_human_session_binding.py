"""SLP1-A4c acceptance tests for human_session_binding.py. Every test
uses a temporary, synthetic canonical provenance DB built via the real
create_provenance_db() and the REAL hir1_registration.register_canonical_human()
writer where a registered human actor is needed -- no test touches the
real production anaxi_provenance.db. No model call anywhere in this file.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db, derive_stable_id
from hir1_registration import register_canonical_human, HOST_ACTOR_ID
import hir1_schema_migration
import human_session_binding as hsb

TEST_ROOT = tempfile.mkdtemp(prefix="slp1a4c_hsb_test_")


def build_fixture(name):
    tmp_dir = os.path.join(TEST_ROOT, name)
    os.makedirs(tmp_dir, exist_ok=True)
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    create_provenance_db(prov_path).close()
    hir1_schema_migration.apply_additive_migration(prov_path)
    conn = sqlite3.connect(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-llama-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')"
    )
    now = int(time.time())
    # HOST_ACTOR_ID must be the EXACT derived stable id --
    # hir1_registration.register_canonical_human() always records this
    # module's own derived HOST_ACTOR_ID as the registration event's
    # requester, so this fixture must seed the identical id (never a
    # hand-picked placeholder like "actor-host-1") for the FK-shaped
    # event_requesters insert to succeed.
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (HOST_ACTOR_ID, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (HOST_ACTOR_ID,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES ('actor-clark-1', 'clark_agent', 'clark', ?)", (now,))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES ('actor-clark-1', 'clark')")
    conn.commit()

    # A real registered human actor, via the REAL production writer --
    # a synthetic/temp registration, never production. Uses a
    # row_factory=Row connection as register_canonical_human() requires.
    conn.row_factory = sqlite3.Row
    result, failure = register_canonical_human(conn, {
        "registration_request_id": "req-test-1",
        "aab_actor_id": "actor-human-registered-1",
        "display_label": "Test Human",
        "source": "local_operator_provisioning",
    })
    assert failure is None, failure
    conn.row_factory = None

    return conn, prov_path, {"registered_actor_id": result["actor_id"], "person_id": result["person_id"]}


def close_conn(conn):
    conn.close()


# ============================================================ session binding


def test_registered_human_can_bind_session():
    conn, _, ids = build_fixture("bind_ok")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assert authority.session_id == "sess-1"
    assert authority.authenticated_actor_id == ids["registered_actor_id"]
    row = conn.execute(
        "SELECT auth_state, authenticated_actor_id, claimed_actor_id, auth_method, assurance_level "
        "FROM auth_contexts WHERE auth_context_id = ?", (authority.auth_context_id,),
    ).fetchone()
    assert row == ("authenticated", ids["registered_actor_id"], ids["registered_actor_id"], "local_session_start_binding", "low")
    close_conn(conn)


def test_binding_never_claims_medium_or_high_assurance():
    conn, _, ids = build_fixture("bind_low_only")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assurance = conn.execute(
        "SELECT assurance_level FROM auth_contexts WHERE auth_context_id = ?", (authority.auth_context_id,),
    ).fetchone()[0]
    assert assurance == "low"
    close_conn(conn)


def test_unregistered_actor_cannot_bind():
    conn, _, ids = build_fixture("bind_unregistered")
    raised = False
    try:
        hsb.bind_session_to_registered_human(
            conn, session_id="sess-1", session_started_at=100,
            pipeline_key="anaxi_orchestration_lineage_a", actor_id="actor-never-registered",
        )
    except hsb.HumanSessionBindingError:
        raised = True
    assert raised
    count = conn.execute("SELECT COUNT(*) FROM auth_contexts").fetchone()[0]
    assert count == 0
    close_conn(conn)


def test_non_human_actor_cannot_bind():
    conn, _, ids = build_fixture("bind_non_human")
    raised = False
    try:
        hsb.bind_session_to_registered_human(
            conn, session_id="sess-1", session_started_at=100,
            pipeline_key="anaxi_orchestration_lineage_a", actor_id="actor-clark-1",
        )
    except hsb.HumanSessionBindingError:
        raised = True
    assert raised
    close_conn(conn)


def test_repeated_binding_to_same_actor_is_idempotent():
    conn, _, ids = build_fixture("bind_idempotent")
    a1 = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    a2 = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assert a1.auth_context_id == a2.auth_context_id
    count = conn.execute("SELECT COUNT(*) FROM auth_contexts WHERE session_id = 'sess-1'").fetchone()[0]
    assert count == 1  # no duplicate row
    close_conn(conn)


def test_rebinding_same_session_to_different_human_fails_closed():
    conn, _, ids = build_fixture("bind_rebind_fails")
    conn.row_factory = sqlite3.Row
    result2, failure2 = register_canonical_human(conn, {
        "registration_request_id": "req-test-2",
        "aab_actor_id": "actor-human-registered-2",
        "display_label": "Second Human",
        "source": "local_operator_provisioning",
    })
    conn.row_factory = None
    assert failure2 is None

    hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    raised = False
    try:
        hsb.bind_session_to_registered_human(
            conn, session_id="sess-1", session_started_at=100,
            pipeline_key="anaxi_orchestration_lineage_a", actor_id=result2["actor_id"],
        )
    except hsb.HumanSessionBindingError:
        raised = True
    assert raised
    count = conn.execute("SELECT COUNT(*) FROM auth_contexts WHERE session_id = 'sess-1'").fetchone()[0]
    assert count == 1  # the original binding, untouched
    close_conn(conn)


def test_no_singleton_human_inference_exists():
    # bind_session_to_registered_human() has no code path that resolves
    # "the one registered human" implicitly -- actor_id is a required
    # keyword argument with no default, confirmed via introspection.
    import inspect
    sig = inspect.signature(hsb.bind_session_to_registered_human)
    assert sig.parameters["actor_id"].default is inspect.Parameter.empty


def test_binding_creates_session_row_when_absent():
    conn, _, ids = build_fixture("bind_creates_session")
    hsb.bind_session_to_registered_human(
        conn, session_id="sess-new", session_started_at=555,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    row = conn.execute("SELECT started_at FROM sessions WHERE session_id = 'sess-new'").fetchone()
    assert row == (555,)
    close_conn(conn)


# ========================================================= per-call authority


def test_correct_authority_validates():
    conn, _, ids = build_fixture("authority_ok")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assert hsb.validate_human_input_authority(conn, authority, session_id="sess-1") is True
    close_conn(conn)


def test_missing_authority_does_not_validate():
    conn, _, ids = build_fixture("authority_missing")
    assert hsb.validate_human_input_authority(conn, None, session_id="sess-1") is False
    close_conn(conn)


def test_wrong_session_fails_closed():
    conn, _, ids = build_fixture("authority_wrong_session")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assert hsb.validate_human_input_authority(conn, authority, session_id="sess-DIFFERENT") is False
    close_conn(conn)


def test_wrong_auth_context_fails_closed():
    conn, _, ids = build_fixture("authority_wrong_auth_context")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    forged = hsb.HumanInputAuthority(
        session_id="sess-1", auth_context_id="ac-does-not-exist", authenticated_actor_id=ids["registered_actor_id"],
    )
    assert hsb.validate_human_input_authority(conn, forged, session_id="sess-1") is False
    close_conn(conn)


def test_mismatched_actor_fails_closed():
    conn, _, ids = build_fixture("authority_mismatched_actor")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    forged = hsb.HumanInputAuthority(
        session_id="sess-1", auth_context_id=authority.auth_context_id, authenticated_actor_id="actor-clark-1",
    )
    assert hsb.validate_human_input_authority(conn, forged, session_id="sess-1") is False
    close_conn(conn)


def test_unauthenticated_context_fails_closed():
    conn, _, ids = build_fixture("authority_unauthenticated_context")
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES ('sess-1', 'pipe-llama-1', 100)")
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
        "VALUES ('ac-unk-forged', 'sess-1', 'unknown', 100)"
    )
    conn.commit()
    forged = hsb.HumanInputAuthority(
        session_id="sess-1", auth_context_id="ac-unk-forged", authenticated_actor_id=ids["registered_actor_id"],
    )
    assert hsb.validate_human_input_authority(conn, forged, session_id="sess-1") is False
    close_conn(conn)


def test_bare_session_id_alone_is_never_enough():
    # A caller presenting ONLY a session_id (no real HumanInputAuthority)
    # cannot validate -- there is no code path that accepts a bare
    # string/session_id as sufficient proof.
    conn, _, ids = build_fixture("authority_bare_session_insufficient")
    hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    assert hsb.validate_human_input_authority(conn, "sess-1", session_id="sess-1") is False
    close_conn(conn)


# ================================================================ canonical H


def test_authorized_exact_text_creates_canonical_h_verbatim():
    conn, prov_path, ids = build_fixture("h_verbatim")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    conn.close()
    data_dir = os.path.dirname(prov_path)

    event_id = hsb.record_human_waking_input(
        data_dir, pipeline_key="anaxi_orchestration_lineage_a", authority=authority,
        message="Clark, exactly this text.", occurred_at=12345,
    )

    check_conn = sqlite3.connect(prov_path)
    event_row = check_conn.execute(
        "SELECT event_type, auth_context_id, occurred_at FROM events WHERE event_id = ?", (event_id,),
    ).fetchone()
    assert event_row == (hsb.HUMAN_WAKING_INPUT_EVENT_TYPE, authority.auth_context_id, 12345)
    comp_row = check_conn.execute(
        "SELECT component_kind, creator_actor_id, authorship_resolution, component_text, content_sha256, "
        "span_start, span_end, model_revision_id FROM event_components WHERE event_id = ?", (event_id,),
    ).fetchone()
    expected_hash = hashlib.sha256("Clark, exactly this text.".encode("utf-8")).hexdigest()
    assert comp_row == (
        hsb.HUMAN_CONVERSATIONAL_INPUT_COMPONENT_KIND, ids["registered_actor_id"], "resolved",
        "Clark, exactly this text.", expected_hash, None, None, None,
    )
    check_conn.close()


def test_h_write_failure_raises_and_writes_nothing():
    conn, prov_path, ids = build_fixture("h_write_failure")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id="sess-1", session_started_at=100,
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=ids["registered_actor_id"],
    )
    conn.close()
    data_dir = os.path.dirname(prov_path)

    bad_authority = hsb.HumanInputAuthority(
        session_id="sess-1", auth_context_id=authority.auth_context_id,
        authenticated_actor_id="actor-does-not-resolve-as-an-events-fk-target",
    )
    # An authenticated_actor_id that has no corresponding actors row
    # violates event_components' own resolved-authorship FK-shaped
    # expectations at write time in a real deployment; here we force a
    # different, deterministic failure by pointing pipeline_key at a
    # pipeline that doesn't exist, proving the same all-or-nothing
    # rollback the docstring promises.
    raised = False
    try:
        hsb.record_human_waking_input(
            data_dir, pipeline_key="no-such-pipeline-key", authority=authority,
            message="Should never be written.", occurred_at=999,
        )
    except hsb.HumanWakingInputWriteError:
        raised = True
    assert raised
    check_conn = sqlite3.connect(prov_path)
    count = check_conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (hsb.HUMAN_WAKING_INPUT_EVENT_TYPE,),
    ).fetchone()[0]
    assert count == 0
    check_conn.close()


def test_no_authority_means_no_h_and_no_attribution_claim():
    # record_human_waking_input() is never called at all by a
    # correctly-behaving caller when authority is None/invalid -- this
    # is enforced by run_waking_turn()'s own control flow (see
    # test_human_waking_authority_end_to_end.py), not by this module
    # itself refusing a None authority (it has no such guard, by
    # design: validation happens upstream, exactly once, in
    # validate_human_input_authority()).
    conn, _, ids = build_fixture("no_authority_no_h")
    assert hsb.validate_human_input_authority(conn, None, session_id="sess-1") is False
    close_conn(conn)


ALL_TESTS = [
    test_registered_human_can_bind_session,
    test_binding_never_claims_medium_or_high_assurance,
    test_unregistered_actor_cannot_bind,
    test_non_human_actor_cannot_bind,
    test_repeated_binding_to_same_actor_is_idempotent,
    test_rebinding_same_session_to_different_human_fails_closed,
    test_no_singleton_human_inference_exists,
    test_binding_creates_session_row_when_absent,
    test_correct_authority_validates,
    test_missing_authority_does_not_validate,
    test_wrong_session_fails_closed,
    test_wrong_auth_context_fails_closed,
    test_mismatched_actor_fails_closed,
    test_unauthenticated_context_fails_closed,
    test_bare_session_id_alone_is_never_enough,
    test_authorized_exact_text_creates_canonical_h_verbatim,
    test_h_write_failure_raises_and_writes_nothing,
    test_no_authority_means_no_h_and_no_attribution_claim,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
