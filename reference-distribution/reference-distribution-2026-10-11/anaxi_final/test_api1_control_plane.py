"""API1-S1 acceptance test suite. Zero Ollama calls anywhere.

All tests operate against TEMP COPIES of the real anaxi_provenance.db
schema (copied once per test from the pre-migration backup taken before
any live schema change) plus a temp isolated AAB1 store. The live
production DB itself is touched only by the one-time additive schema
migration applied separately (see the API1-S1 final report) -- no test
here repeatedly mutates it.

Run: python test_api1_control_plane.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
SCRATCHPAD = os.path.join(ANAXI_FINAL, "scratchpad")
sys.path.insert(0, ANAXI_FINAL)
sys.path.insert(0, SCRATCHPAD)

import api1_schema_migration as migration
import api1_control_plane as api1
from aab1.store import AAB1Store
from aab1.provisioning import provision_profile
from aab1.auth import authenticate, logout
from aab1.schemas import CLARK_ACTOR_ID
from hdi2.schemas import validate_owner_state as hdi2_validate_owner_state

TEST_DIR = tempfile.mkdtemp(prefix="api1_test_")
# API2-LR1-S1 section 32: use the current live schema (robust regardless
# of file renames) rather than a specifically-named historical backup --
# apply_additive_migration() is idempotent, so copying an already-
# migrated live DB and re-applying it is exactly as safe as copying a
# genuinely pre-migration one.
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")
PRINCIPAL = {"type": "username_domain", "value": "TESTDOMAIN\\api1tester"}


def fresh_copy_db(name):
    path = os.path.join(TEST_DIR, f"{name}.db")
    shutil.copy(SOURCE_DB, path)
    migration.apply_additive_migration(path)
    return path


def open_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def seed_pipeline_session_and_human(conn):
    """Test-only production fixture: registers a minimal pipeline,
    production session, and human person/actor/actor_human_person row
    so TX1 mechanics can be exercised. This is exactly the registration
    gap documented in api1_control_plane.py's module docstring --
    seeding it here is a TEST fixture, not something API1-S1's runtime
    code does on a live database."""
    pipeline_id = f"pipeline-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT OR IGNORE INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label) "
        "VALUES (?, 'test_pipeline', 'test')",
        (pipeline_id,),
    )
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, ?, ?)",
        (session_id, pipeline_id, int(time.time())),
    )
    person_id = f"person-{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES (?, ?)", (person_id, int(time.time())))
    human_actor_id = f"actor-human-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, ?)",
        (human_actor_id, human_actor_id, int(time.time())),
    )
    conn.execute(
        "INSERT INTO actor_human_person (actor_id, person_id, relationship_established_at) VALUES (?, ?, ?)",
        (human_actor_id, person_id, int(time.time())),
    )
    conn.commit()
    return pipeline_id, session_id, human_actor_id


def make_fixture(name, register_human=True):
    db_path = fresh_copy_db(name)
    conn = open_conn(db_path)
    pipeline_id, prod_session_id, human_actor_id = seed_pipeline_session_and_human(conn)

    aab_store = AAB1Store(os.path.join(TEST_DIR, f"{name}_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL, )
    # Force the AAB profile's actor_id to match the pre-registered
    # production human actor, so create_and_assign can succeed when the
    # test wants it to. Tests that want the UNregistered-actor failure
    # path use a fresh, never-registered AAB profile instead.
    if register_human:
        aab_store.conn.execute(
            "UPDATE human_profiles SET actor_id = ? WHERE actor_id = ?",
            (human_actor_id, profile["actor_id"]),
        )
        aab_store.conn.commit()
        profile = aab_store.get_profile_by_actor_id(human_actor_id)

    session, ac = authenticate(aab_store, profile["actor_id"], principal_provider=lambda: PRINCIPAL)
    return conn, aab_store, profile, session, ac, prod_session_id


def make_request(request_id="req-1", **overrides):
    req = {
        "establishment_request_id": request_id,
        "operation": "create_and_assign",
        "decision_text": "Whether the fixture uses option A or B is your decision.",
        "recipient_actor_id": CLARK_ACTOR_ID,
        "source_input_id": "turn-1",
    }
    req.update(overrides)
    return req


# -------------------------------------------------- 1-3: migration -----


def test_migration_additive_and_idempotent():
    # SOURCE_DB (live anaxi_provenance.db) already has this migration
    # applied -- use the genuine pre-migration backup (still present
    # under its post-rename name) to robustly prove the additive-from-
    # nothing behavior, same fix as API2-LR1-S1 applied to the
    # equivalent API2 migration test.
    pre_migration_source = os.path.join(ANAXI_FINAL, "anaxi_provenance_api1s1_preschema_backup.db")
    db_path = os.path.join(TEST_DIR, "migration_only.db")
    shutil.copy(pre_migration_source, db_path)

    conn = open_conn(db_path)
    before_rows = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("events", "auth_contexts", "actors")
    }
    before_auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)")}
    conn.close()

    report1 = migration.apply_additive_migration(db_path)
    assert set(migration.AUTH_CONTEXTS_ADDITIVE_COLUMNS) == set(report1["auth_contexts_columns_added"])
    assert set(report1["new_tables_created"]) == {
        "protected_decisions", "protected_decision_requests", "protected_decision_events",
    }

    report2 = migration.apply_additive_migration(db_path)  # idempotency
    assert report2["auth_contexts_columns_added"] == []
    assert report2["new_tables_created"] == []

    conn = open_conn(db_path)
    after_rows = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("events", "auth_contexts", "actors")
    }
    after_auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)")}
    conn.close()

    assert before_rows == after_rows, "existing rows must be untouched by an additive migration"
    assert after_auth_columns - before_auth_columns == set(migration.AUTH_CONTEXTS_ADDITIVE_COLUMNS)

    state = migration.verify_migration_state(db_path)
    assert all(state.values())


# ------------------------------------------------ 4: feature gate false -


def test_feature_gate_default_false_and_disabled_result():
    assert api1.AUTHORITATIVE_PDE_ENABLED is False
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("gate_default")
    result, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, prod_session_id,
    )  # enabled omitted -> uses module default (False)
    assert result is None
    assert failure == api1.ApiFailure.FEATURE_DISABLED
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 0


# -------------------------------------------- 5-7: create success/state -


def test_create_and_assign_success():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("create_success")
    result, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert failure is None
    decision = result["decision"]
    # API1-P1: assert against the real canonical CLARK_ACTOR_ID, not the
    # literal "clark" -- the earlier version of this assertion hardcoded
    # the SAME wrong literal _commit_tx1() wrote, which is exactly how
    # the defect went undetected. CLARK_ACTOR_ID is already an opaque,
    # canonical-looking derived id (see aab1.schemas), not "clark".
    assert decision["owner_state"] == {"status": "resolved", "actor_id": CLARK_ACTOR_ID}
    assert CLARK_ACTOR_ID != "clark"
    assert decision["decision_state"] == "open"
    assert decision["resolution_kind"] is None
    assert decision["created_event_id"] == decision["last_transition_event_id"]

    row = conn.execute("SELECT * FROM protected_decisions WHERE decision_id = ?", (decision["decision_id"],)).fetchone()
    assert row["owner_actor_id"] == CLARK_ACTOR_ID
    assert row["counterpart_actor_id"] == profile["actor_id"]

    event = result["event"]
    assert event["actor_id"] == profile["actor_id"]
    assert event["resulting_owner_actor_id"] == CLARK_ACTOR_ID

    ac_row = conn.execute("SELECT * FROM auth_contexts WHERE auth_context_id = ?", (result["auth_context_id"],)).fetchone()
    assert ac_row["auth_state"] == "authenticated"
    assert ac_row["assurance_level"] == "high"  # mapped
    assert ac_row["source_assurance_detail"] == "os_principal_session_bound"  # native value preserved
    assert ac_row["source_aab_auth_context_id"] == ac["auth_context_id"]
    assert ac_row["source_aab_session_id"] == session["session_id"]
    assert ac_row["observed_valid_at"] is not None


# -------------------------------------------- 8: idempotency / conflict -


def test_idempotent_replay_and_conflict():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("idempotency")
    req = make_request()
    r1, f1 = api1.submit_authoritative_create_and_assign(conn, aab_store, req, ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    r2, f2 = api1.submit_authoritative_create_and_assign(conn, aab_store, dict(req), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    assert f1 is None and f2 is None
    assert r1 == r2
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 1

    _, f3 = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(decision_text="a totally different decision text"),
        ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert f3 == api1.ApiFailure.REQUEST_CONFLICT
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 1


# ------------------------------------- 9: replay after AAB logout succeeds -


def test_idempotent_replay_after_aab_logout_succeeds():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("replay_after_logout")
    req = make_request()
    r1, f1 = api1.submit_authoritative_create_and_assign(conn, aab_store, req, ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    assert f1 is None

    logout(aab_store, session["session_id"])

    r2, f2 = api1.submit_authoritative_create_and_assign(
        conn, aab_store, dict(req), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert f2 is None, "exact committed replay must not require the original AAB session to still be active"
    assert r2 == r1


# ----------------------------------------- 10: rollback on injected failure -


def test_tx1_rollback_atomic():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("rollback")

    raised = None
    try:
        api1.submit_authoritative_create_and_assign(
            conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"],
            CLARK_ACTOR_ID, prod_session_id, enabled=True,
            _test_inject_failure_after="decision_insert",
        )
    except RuntimeError as exc:
        raised = exc

    assert raised is not None
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_requests").fetchone()[0] == 0
    # crucially: no orphan auth_contexts snapshot either -- same transaction
    assert conn.execute(
        "SELECT COUNT(*) FROM auth_contexts WHERE source_aab_auth_context_id = ?", (ac["auth_context_id"],)
    ).fetchone()[0] == 0


# ------------------------------------------------- 11: crash/recovery -----


def test_crash_after_commit_recovery():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("crash_recovery")
    req = make_request()
    r1, _ = api1.submit_authoritative_create_and_assign(conn, aab_store, req, ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    conn.close()

    conn2 = open_conn(conn_path := os.path.join(TEST_DIR, "crash_recovery.db"))
    r2, f2 = api1.submit_authoritative_create_and_assign(conn2, aab_store, dict(req), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    assert f2 is None
    assert r2 == r1
    assert conn2.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 1
    assert conn2.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == 1


# -------------------------------- 12: AAB logout after TX1 leaves historical state ---


def test_aab_logout_after_tx1_does_not_mutate_historical_state():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("logout_after_tx1")
    result, _ = api1.submit_authoritative_create_and_assign(conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    decision_id = result["decision"]["decision_id"]
    prod_ac_id = result["auth_context_id"]

    before_decision = dict(conn.execute("SELECT * FROM protected_decisions WHERE decision_id = ?", (decision_id,)).fetchone())
    before_ac = dict(conn.execute("SELECT * FROM auth_contexts WHERE auth_context_id = ?", (prod_ac_id,)).fetchone())

    logout(aab_store, session["session_id"])

    after_decision = dict(conn.execute("SELECT * FROM protected_decisions WHERE decision_id = ?", (decision_id,)).fetchone())
    after_ac = dict(conn.execute("SELECT * FROM auth_contexts WHERE auth_context_id = ?", (prod_ac_id,)).fetchone())

    assert before_decision == after_decision
    assert before_ac == after_ac
    assert after_decision["owner_actor_id"] == CLARK_ACTOR_ID
    assert after_decision["counterpart_actor_id"] == profile["actor_id"]


# ---------------------- 13: production auth snapshot cannot authorize new action ----


def test_production_auth_snapshot_alone_cannot_authorize_new_action():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("snapshot_not_live_auth")
    result, _ = api1.submit_authoritative_create_and_assign(conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    prod_ac_id = result["auth_context_id"]

    # attempt a NEW submission using the PRODUCTION auth_context_id as
    # if it were a live AAB auth_context_id -- it isn't one; the AAB
    # store has never heard of it.
    _, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(request_id="req-using-prod-snapshot"),
        prod_ac_id, session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert failure == api1.ApiFailure.AUTH_NOT_ESTABLISHED
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 1  # only the first one


# ------------------------------- 14: different later actor doesn't replace counterpart --


def test_different_later_actor_does_not_replace_counterpart():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("counterpart_fixed")
    result, _ = api1.submit_authoritative_create_and_assign(conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    decision_id = result["decision"]["decision_id"]

    other_principal = {"type": "username_domain", "value": "TESTDOMAIN\\otherperson"}
    other_profile = provision_profile(aab_store, "Other", "other", other_principal)
    other_session, other_ac = authenticate(aab_store, other_profile["actor_id"], principal_provider=lambda: other_principal)

    row = conn.execute("SELECT counterpart_actor_id FROM protected_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
    assert row["counterpart_actor_id"] == profile["actor_id"]
    assert row["counterpart_actor_id"] != other_profile["actor_id"]

    route = api1.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)
    assert route["counterpart_actor_id"] == profile["actor_id"]


# ------------------------------------------ 15: human actor not registered ----


def test_human_actor_not_registered_fails_closed():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("unregistered", register_human=False)
    result, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert result is None
    assert failure == api1.ApiFailure.HUMAN_ACTOR_NOT_REGISTERED
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auth_contexts WHERE authenticated_actor_id = ?", (profile["actor_id"],)).fetchone()[0] == 0


# ---------------------------------------- 16: various failure-before-TX1 ----


def test_wrong_recipient_rejected():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("wrong_recipient")
    _, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(recipient_actor_id="not-clark"),
        ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert failure == api1.ApiFailure.INVALID_RECIPIENT


def test_terminated_session_rejected():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("terminated")
    logout(aab_store, session["session_id"])
    _, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert failure == api1.ApiFailure.SESSION_NOT_ACTIVE


def test_raw_auth_field_injection_rejected():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("injection")
    req = make_request()
    req["authenticated_actor_id"] = profile["actor_id"]
    _, failure = api1.submit_authoritative_create_and_assign(conn, aab_store, req, ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    assert failure == api1.ApiFailure.PROTOCOL_LEAKAGE


def test_ordinary_prose_cannot_enter():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("prose")
    _, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, "This decision is yours, Clark.", ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True,
    )
    assert failure == api1.ApiFailure.INVALID_OPERATION
    assert conn.execute("SELECT COUNT(*) FROM protected_decisions").fetchone()[0] == 0


def test_transfer_existing_not_productionized():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("no_transfer")
    req = {
        "establishment_request_id": "req-transfer-attempt",
        "operation": "transfer_existing",
        "decision_id": "dec-doesnt-matter",
        "recipient_actor_id": CLARK_ACTOR_ID,
        "expected_last_transition_event_id": "event-doesnt-matter",
        "source_input_id": "turn-x",
    }
    _, failure = api1.submit_authoritative_create_and_assign(conn, aab_store, req, ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    assert failure == api1.ApiFailure.INVALID_OPERATION


# ------------------------------------------------- 17: HDI2 compatibility ---


def test_hdi2_compatibility():
    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("hdi2_compat")
    result, _ = api1.submit_authoritative_create_and_assign(conn, aab_store, make_request(), ac["auth_context_id"], session["session_id"], CLARK_ACTOR_ID, prod_session_id, enabled=True)
    decision_id = result["decision"]["decision_id"]

    route = api1.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)
    assert route is not None
    hdi2_decision = api1.to_hdi2_protected_decision(route)
    required = {"decision_id", "decision_text", "decision_context", "owner_state",
                "decision_state", "resolution_kind", "created_event_id", "last_transition_event_id"}
    assert required.issubset(hdi2_decision.keys())
    assert hdi2_validate_owner_state(hdi2_decision["owner_state"]) is True
    assert hdi2_decision["owner_state"]["actor_id"] == CLARK_ACTOR_ID
    assert hdi2_decision["decision_state"] == "open"
    assert hdi2_decision["resolution_kind"] is None
    assert hdi2_decision["created_event_id"] == hdi2_decision["last_transition_event_id"]

    # host-only fields never leak into the HDI2-facing conversion
    assert "counterpart_actor_id" not in hdi2_decision
    assert "auth_context_id" not in hdi2_decision


# ------------------------------------------ API1-P1: canonical actor-id fix -


def test_tx1_uses_supplied_canonical_clark_actor_id():
    """API1-P1 focused regression. Uses a fixture actor id that is
    deliberately NOT the real production CLARK_ACTOR_ID and NOT the
    literal "clark" -- if _commit_tx1() ever regresses to hardcoding
    ANY fixed string again (whether "clark" or the real production id
    baked in by coincidence), this fails, because the opaque fixture
    id below matches neither."""
    opaque_clark_id = f"actor-opaque-clark-{uuid.uuid4().hex[:12]}"
    assert opaque_clark_id != "clark"
    assert opaque_clark_id != CLARK_ACTOR_ID

    conn, aab_store, profile, session, ac, prod_session_id = make_fixture("opaque_clark")
    req = make_request(recipient_actor_id=opaque_clark_id)

    result, failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store, req, ac["auth_context_id"], session["session_id"],
        opaque_clark_id, prod_session_id, enabled=True,
    )
    assert failure is None, failure

    decision = result["decision"]
    assert decision["owner_state"] == {"status": "resolved", "actor_id": opaque_clark_id}

    row = conn.execute(
        "SELECT owner_actor_id, counterpart_actor_id FROM protected_decisions WHERE decision_id = ?",
        (decision["decision_id"],),
    ).fetchone()
    assert row["owner_actor_id"] == opaque_clark_id
    assert row["owner_actor_id"] != "clark"
    assert row["counterpart_actor_id"] == profile["actor_id"]  # counterpart binding unaffected

    event_row = conn.execute(
        "SELECT resulting_owner_actor_id FROM protected_decision_events WHERE event_id = ?",
        (decision["created_event_id"],),
    ).fetchone()
    assert event_row["resulting_owner_actor_id"] == opaque_clark_id
    assert event_row["resulting_owner_actor_id"] != "clark"

    # auth snapshot semantics unaffected by the identity fix
    ac_row = conn.execute(
        "SELECT authenticated_actor_id, auth_state, assurance_level FROM auth_contexts WHERE auth_context_id = ?",
        (result["auth_context_id"],),
    ).fetchone()
    assert ac_row["authenticated_actor_id"] == profile["actor_id"]
    assert ac_row["auth_state"] == "authenticated"

    # full source audit: zero authoritative TX1 actor-ID writes use "clark"
    with open(os.path.join(ANAXI_FINAL, "api1_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    assert '"clark"' not in source
    assert "'clark'" not in source


# ------------------------------------------------------ 18: zero model calls -


def test_zero_model_calls_and_no_semantic_inference():
    forbidden = ("import ollama", "ollama.chat(", "ollama.generate(", "re.search", "re.match", "decision is yours")
    for fn in ("api1_schema_migration.py", "api1_control_plane.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read().lower()
        for pattern in forbidden:
            assert pattern not in source, f"{fn} contains forbidden pattern: {pattern}"


# ------------------------------------------------------------- runner ----

ALL_TESTS = [
    test_migration_additive_and_idempotent,
    test_tx1_uses_supplied_canonical_clark_actor_id,
    test_feature_gate_default_false_and_disabled_result,
    test_create_and_assign_success,
    test_idempotent_replay_and_conflict,
    test_idempotent_replay_after_aab_logout_succeeds,
    test_tx1_rollback_atomic,
    test_crash_after_commit_recovery,
    test_aab_logout_after_tx1_does_not_mutate_historical_state,
    test_production_auth_snapshot_alone_cannot_authorize_new_action,
    test_different_later_actor_does_not_replace_counterpart,
    test_human_actor_not_registered_fails_closed,
    test_wrong_recipient_rejected,
    test_terminated_session_rejected,
    test_raw_auth_field_injection_rejected,
    test_ordinary_prose_cannot_enter,
    test_transfer_existing_not_productionized,
    test_hdi2_compatibility,
    test_zero_model_calls_and_no_semantic_inference,
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
    print(f"test_dir={TEST_DIR}")
    if failures:
        print()
        print("=== FAILURE DETAILS ===")
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
