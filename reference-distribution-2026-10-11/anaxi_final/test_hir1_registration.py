"""HIR1-S1 acceptance test suite. Zero Ollama calls. No live production
write -- every test operates against a temp COPY of the real
anaxi_provenance.db schema (post-API1-S1-migration backup), never the
live file itself.

Run: python test_hir1_registration.py
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

import hir1_schema_migration as hir1_migration
import hir1_registration as hir1
import api1_control_plane as api1
from aab1.store import AAB1Store
from aab1.provisioning import provision_profile
from aab1.auth import authenticate
from aab1.schemas import CLARK_ACTOR_ID
from provenance_schema import derive_stable_id

TEST_DIR = tempfile.mkdtemp(prefix="hir1_test_")
# The LIVE db already has the API1-S1 migration applied; copy it as the
# realistic schema baseline (never the live file itself).
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")
PRINCIPAL = {"type": "username_domain", "value": "TESTDOMAIN\\hir1tester"}


def fresh_copy_db(name, apply_hir1_migration=True):
    path = os.path.join(TEST_DIR, f"{name}.db")
    shutil.copy(SOURCE_DB, path)
    if apply_hir1_migration:
        hir1_migration.apply_additive_migration(path)
    return path


def open_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def make_request(aab_actor_id, request_id="hir-req-1", **overrides):
    req = {
        "registration_request_id": request_id,
        "aab_actor_id": aab_actor_id,
        "display_label": "Synthetic Test Human",
        "source": "local_operator_provisioning",
    }
    req.update(overrides)
    return req


def snapshot_table(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


# -------------------------------------------------- 1-2: migration -------


def test_migration_additive_and_idempotent():
    # SOURCE_DB (live) already has the HIR1 migration applied -- use
    # the genuine pre-migration backup taken at HIR1-S2's own preflight
    # (API2-LR1-S1 section 32/33 fix, same pattern applied to API1/API2's
    # equivalent migration tests).
    pre_migration_source = os.path.join(ANAXI_FINAL, "anaxi_provenance_hir1s2_prereg_backup_20260901T154107.db")
    db_path = os.path.join(TEST_DIR, "migration_only.db")
    shutil.copy(pre_migration_source, db_path)
    conn = open_conn(db_path)
    before = {t: snapshot_table(conn, t) for t in ("persons", "actors", "actor_human_person", "events")}
    conn.close()

    report1 = hir1_migration.apply_additive_migration(db_path)
    assert report1["new_tables_created"] == ["human_registration_requests"]
    report2 = hir1_migration.apply_additive_migration(db_path)
    assert report2["new_tables_created"] == []

    conn = open_conn(db_path)
    after = {t: snapshot_table(conn, t) for t in ("persons", "actors", "actor_human_person", "events")}
    conn.close()
    assert before == after, "existing rows must be untouched by the additive migration"

    state = hir1_migration.verify_migration_state(db_path)
    assert state["has_human_registration_requests"] is True


# ---------------------------------------- 3: human actor type discovery --


def test_human_actor_type_matches_live_schema():
    # Attempting the WRONG type (an earlier, incorrect guess used during
    # API1-S1 development) must be rejected by the real CHECK constraint --
    # proving HUMAN_ACTOR_TYPE is the verified value, not a guess.
    db_path = fresh_copy_db("actor_type_check")
    conn = open_conn(db_path)
    raised = None
    try:
        conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human', ?, ?)",
            ("actor-wrong-type", "actor-wrong-type", int(time.time())),
        )
    except sqlite3.IntegrityError as exc:
        raised = exc
    assert raised is not None
    assert hir1.HUMAN_ACTOR_TYPE == "human_person"
    # and the correct value is accepted
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, ?, ?, ?)",
        ("actor-right-type", hir1.HUMAN_ACTOR_TYPE, "actor-right-type", int(time.time())),
    )
    conn.commit()
    conn.close()


# --------------------------------------- 4-5: AAB actor-ID reuse ---------


def test_registration_success_reuses_aab_actor_id():
    db_path = fresh_copy_db("success")
    conn = open_conn(db_path)

    aab_store = AAB1Store(os.path.join(TEST_DIR, "success_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    aab_actor_id = profile["actor_id"]

    result, failure = hir1.register_canonical_human(conn, make_request(aab_actor_id))
    assert failure is None
    assert result["actor_id"] == aab_actor_id  # exact reuse, no translation layer

    actor_row = conn.execute("SELECT * FROM actors WHERE actor_id = ?", (aab_actor_id,)).fetchone()
    assert actor_row["actor_type"] == "human_person"
    assert actor_row["display_label"] == "Synthetic Test Human"

    link_row = conn.execute("SELECT * FROM actor_human_person WHERE actor_id = ?", (aab_actor_id,)).fetchone()
    assert link_row["person_id"] == result["person_id"]

    person_row = conn.execute("SELECT * FROM persons WHERE person_id = ?", (result["person_id"],)).fetchone()
    assert person_row is not None


# ------------------------------------------------- 6: lookup key isolation -


def test_lookup_key_never_consulted():
    pkg = "hir1_registration.py"
    with open(os.path.join(ANAXI_FINAL, pkg), encoding="utf-8") as f:
        source = f.read().lower()
    for forbidden in ("memory_lookup_key", '"nate"', "'nate'", "kardia"):
        assert forbidden not in source


# ------------------------------------------------------- 7: SID isolation -


def test_sid_never_stored():
    db_path = fresh_copy_db("sid_check")
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, "sid_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    result, _ = hir1.register_canonical_human(conn, make_request(profile["actor_id"]))

    for table in ("persons", "actors", "actor_human_person"):
        rows = snapshot_table(conn, table)
        blob = json.dumps(rows)
        assert "S-1-5-21" not in blob  # no SID-shaped string anywhere
        assert "TESTDOMAIN" not in blob


# ------------------------------------------------- 8: explicit only ------


def test_no_automatic_registration_hook():
    for fn in ("llama_anaxi.py", "orchestration.py", "api1_control_plane.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        assert "hir1_registration" not in source, f"{fn} must not import/call hir1_registration"


# -------------------------------------- 9: bootstrap origin not the human -


def test_bootstrap_origin_is_host_not_new_human():
    db_path = fresh_copy_db("origin")
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, "origin_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    result, _ = hir1.register_canonical_human(conn, make_request(profile["actor_id"]))

    event_id = result["registration_event_id"]
    requester_row = conn.execute(
        "SELECT requester_actor_id FROM event_requesters WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert requester_row["requester_actor_id"] == hir1.HOST_ACTOR_ID
    assert requester_row["requester_actor_id"] != profile["actor_id"]

    subject_row = conn.execute(
        "SELECT subject_actor_id, role FROM event_subjects WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert subject_row["subject_actor_id"] == profile["actor_id"]

    event_row = conn.execute("SELECT event_type FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert event_row["event_type"] == "human_actor_registered"


# --------------------------------------- 10-11: atomic commit / idempotent -


def test_idempotent_replay_and_conflict():
    db_path = fresh_copy_db("idempotency")
    conn = open_conn(db_path)
    before_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    aab_store = AAB1Store(os.path.join(TEST_DIR, "idempotency_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    req = make_request(profile["actor_id"])

    r1, f1 = hir1.register_canonical_human(conn, req)
    r2, f2 = hir1.register_canonical_human(conn, dict(req))
    assert f1 is None and f2 is None
    assert r1 == r2
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons + 1

    _, f3 = hir1.register_canonical_human(conn, make_request(profile["actor_id"], display_label="A different label"))
    assert f3 == hir1.HirFailure.REGISTRATION_REQUEST_CONFLICT
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons + 1


# --------------------------------------------- 12: actor collision -------


def test_actor_collision_with_non_human_fails_closed():
    db_path = fresh_copy_db("collision")
    conn = open_conn(db_path)
    before_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    clark_actor_id = derive_stable_id("actor", "clark")
    _, failure = hir1.register_canonical_human(conn, make_request(clark_actor_id))
    assert failure == hir1.HirFailure.ACTOR_REGISTRATION_CONFLICT
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons


# ------------------------------------------- 13: person-link ambiguity ---


def test_person_link_ambiguity_fails_closed():
    # The real schema declares actor_human_person.actor_id PRIMARY KEY
    # (and person_id UNIQUE) -- a second link row for the same actor_id
    # is structurally impossible to insert at all. That makes the
    # `len(linked) > 1` PERSON_LINK_CONFLICT branch in
    # register_canonical_human() unreachable dead code under the real
    # schema: defense-in-depth, not a reachable path. Prove both halves
    # of that claim here: (a) the schema itself rejects a second link,
    # and (b) the one-link case correctly fails closed as
    # ACTOR_REGISTRATION_CONFLICT (an existing human actor already
    # linked to a person is never silently re-registered/relinked).
    db_path = fresh_copy_db("ambiguity")
    conn = open_conn(db_path)
    aab_actor_id = "actor-preexisting-ambiguous"
    now = int(time.time())
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, ?, ?, ?)",
        (aab_actor_id, hir1.HUMAN_ACTOR_TYPE, aab_actor_id, now),
    )
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES (?, ?)", ("person-preexisting-0", now))
    conn.execute(
        "INSERT INTO actor_human_person (actor_id, person_id, relationship_established_at) VALUES (?, ?, ?)",
        (aab_actor_id, "person-preexisting-0", now),
    )
    conn.commit()

    conn.execute("INSERT INTO persons (person_id, created_at) VALUES (?, ?)", ("person-preexisting-1", now))
    raised = None
    try:
        conn.execute(
            "INSERT INTO actor_human_person (actor_id, person_id, relationship_established_at) VALUES (?, ?, ?)",
            (aab_actor_id, "person-preexisting-1", now),
        )
    except sqlite3.IntegrityError as exc:
        raised = exc
    conn.rollback()
    assert raised is not None, "schema must make a second actor_human_person link structurally impossible"

    _, failure = hir1.register_canonical_human(conn, make_request(aab_actor_id, request_id="hir-req-ambiguous"))
    assert failure == hir1.HirFailure.ACTOR_REGISTRATION_CONFLICT


# --------------------------------------------- 14: trigger compatibility -


def test_auth_context_human_trigger_satisfied_after_registration():
    db_path = fresh_copy_db("trigger_compat")
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, "trigger_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    hir1.register_canonical_human(conn, make_request(profile["actor_id"]))

    # This INSERT would previously fail with trg_auth_context_human_only --
    # now it must succeed, without disabling/weakening the trigger.
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (session_id, started_at) VALUES (?, ?)",
        ("session-trigger-test", now),
    )
    raised = None
    try:
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, "
            "authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) "
            "VALUES ('authctx-trigger-test', 'session-trigger-test', ?, ?, 'authenticated', "
            "'os_principal_plus_profile_confirmation', 'high', ?)",
            (profile["actor_id"], profile["actor_id"], now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raised = exc
    assert raised is None, f"trg_auth_context_human_only should now be satisfied, got: {raised}"


# --------------------------------------------------- 15-16: rollback -----


def test_rollback_on_injected_failure():
    # Compare against BEFORE snapshots, not absolute zero -- fresh_copy_db()
    # copies the current live DB, which legitimately already has rows in
    # some of these tables post-HIR1-S2 (same fix as elsewhere in this
    # file). actor_human_person/human_registration_requests are keyed to
    # the NEW test actor specifically and independently asserted absent
    # for that actor_id, which remains an absolute (not before/after) check.
    for stage in ("person_insert", "actor_insert", "link_insert", "event_insert"):
        db_path = fresh_copy_db(f"rollback_{stage}")
        conn = open_conn(db_path)
        before_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_actor_registered'").fetchone()[0]
        aab_store = AAB1Store(os.path.join(TEST_DIR, f"rollback_{stage}_aab.sqlite3"))
        profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)

        raised = None
        try:
            hir1.register_canonical_human(conn, make_request(profile["actor_id"]), _test_inject_failure_after=stage)
        except RuntimeError as exc:
            raised = exc
        assert raised is not None, f"stage={stage}"
        assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons, f"stage={stage}"
        assert conn.execute("SELECT COUNT(*) FROM actors WHERE actor_id = ?", (profile["actor_id"],)).fetchone()[0] == 0, f"stage={stage}"
        assert conn.execute("SELECT COUNT(*) FROM actor_human_person WHERE actor_id = ?", (profile["actor_id"],)).fetchone()[0] == 0, f"stage={stage}"
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_actor_registered'").fetchone()[0] == before_events, f"stage={stage}"
        assert conn.execute("SELECT COUNT(*) FROM human_registration_requests WHERE aab_actor_id = ?", (profile["actor_id"],)).fetchone()[0] == 0, f"stage={stage}"


# ------------------------------------------------ 17: crash/recovery -----


def test_crash_after_commit_recovery():
    # Compare against a BEFORE snapshot rather than an absolute count --
    # fresh_copy_db() copies the current live DB, which legitimately
    # already has one canonical human post-HIR1-S2 (API2-LR1-S1 section
    # 33's fix, applied here too for the same reason).
    db_path = fresh_copy_db("crash_recovery")
    conn = open_conn(db_path)
    before_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_actor_registered'").fetchone()[0]
    aab_store = AAB1Store(os.path.join(TEST_DIR, "crash_recovery_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    req = make_request(profile["actor_id"])

    r1, _ = hir1.register_canonical_human(conn, req)
    conn.close()

    conn2 = open_conn(db_path)
    r2, f2 = hir1.register_canonical_human(conn2, dict(req))
    assert f2 is None
    assert r2 == r1
    assert conn2.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons + 1
    assert conn2.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_actor_registered'").fetchone()[0] == before_events + 1


# ------------------------------------------- 18: no relationship/perms ---


def test_no_relationship_or_permission_records():
    # authorization_scopes is a static scope-name catalog (already
    # nonempty in production, pre-populated independent of any actor) --
    # it is not a per-actor permission grant table, so it is excluded
    # here. The tables below ARE per-actor relationship/permission
    # records and must stay untouched by registration alone.
    db_path = fresh_copy_db("no_relationships")
    conn = open_conn(db_path)
    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in (
            "relationship_records", "guardian_authorization_events",
            "persistence_authorization_events",
            "event_relied_upon_guardian_authorization",
            "event_relied_upon_persistence_authorization",
            "event_relationship_context", "assertion_relations",
            "guardian_requirement_policies",
        )
    }
    aab_store = AAB1Store(os.path.join(TEST_DIR, "no_rel_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    hir1.register_canonical_human(conn, make_request(profile["actor_id"]))

    for table, before_count in before.items():
        after_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert after_count == before_count == 0, f"{table} should remain empty after registration"


# ------------------------------------------ 19: historical rows unchanged -


def test_historical_rows_unchanged():
    db_path = fresh_copy_db("historical")
    conn = open_conn(db_path)
    before_events = snapshot_table(conn, "events")
    before_auth = snapshot_table(conn, "auth_contexts")

    aab_store = AAB1Store(os.path.join(TEST_DIR, "historical_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    hir1.register_canonical_human(conn, make_request(profile["actor_id"]))

    after_events = snapshot_table(conn, "events")
    after_auth = snapshot_table(conn, "auth_contexts")
    # old rows unchanged; exactly one NEW event appended
    assert after_events[: len(before_events)] == before_events
    assert len(after_events) == len(before_events) + 1
    assert after_auth == before_auth  # registration never touches auth_contexts


# ------------------------------------------ 20-21: assurance semantics ---


def test_high_alone_is_not_sufficient_normative_assurance():
    db_path = fresh_copy_db("assurance")
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, "assurance_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)
    hir1.register_canonical_human(conn, make_request(profile["actor_id"]))
    session, ac = authenticate(aab_store, profile["actor_id"], principal_provider=lambda: PRINCIPAL)

    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (session_id, started_at) VALUES (?, ?)", ("session-assurance", now),
    )
    # a coarse "high" auth_contexts row WITHOUT the exact native detail
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, "
        "authenticated_actor_id, auth_state, auth_method, assurance_level, established_at, "
        "source_assurance_detail) VALUES ('authctx-coarse-only', 'session-assurance', ?, ?, "
        "'authenticated', 'some_other_method', 'high', ?, NULL)",
        (profile["actor_id"], profile["actor_id"], now),
    )
    conn.commit()

    row = conn.execute("SELECT assurance_level, source_assurance_detail FROM auth_contexts WHERE auth_context_id = 'authctx-coarse-only'").fetchone()
    assert row["assurance_level"] == "high"
    assert row["source_assurance_detail"] is None
    # api1_control_plane's own _revalidate_aab operates on the LIVE AAB
    # store (not this coarse historical row) and independently checks
    # stored_ac["assurance_level"] == "os_principal_session_bound" against
    # the AAB-side record -- this coarse production row, by itself, is
    # never consulted as authorization evidence anywhere in API1.
    assert "source_assurance_detail" not in open(
        os.path.join(ANAXI_FINAL, "api1_control_plane.py"), encoding="utf-8"
    ).read().split("_revalidate_aab")[1].split("def ")[0], (
        "the AAB revalidation path must not read the coarse production row's "
        "assurance columns as authorization evidence"
    )


# --------------------------------------- 22-23: API1 temp-copy unblock ---


def test_api1_tx1_unblocked_after_registration():
    db_path = fresh_copy_db("unblock")
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, "unblock_aab.sqlite3"))
    profile = provision_profile(aab_store, "Tester", "tester", PRINCIPAL)

    # confirm the ORIGINAL blocker before registration
    session0, ac0 = authenticate(aab_store, profile["actor_id"], principal_provider=lambda: PRINCIPAL)
    now = int(time.time())
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label) VALUES ('pipe-1','test','test')")
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES ('prod-session-1', 'pipe-1', ?)", (now,))
    conn.commit()

    pre_result, pre_failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store,
        {"establishment_request_id": "req-pre", "operation": "create_and_assign",
         "decision_text": "Whether this fixture is A or B is your decision.",
         "recipient_actor_id": CLARK_ACTOR_ID, "source_input_id": "turn-pre"},
        ac0["auth_context_id"], session0["session_id"], CLARK_ACTOR_ID, "prod-session-1", enabled=True,
    )
    assert pre_failure == api1.ApiFailure.HUMAN_ACTOR_NOT_REGISTERED

    # now legitimately register, then retry
    reg_result, reg_failure = hir1.register_canonical_human(conn, make_request(profile["actor_id"]))
    assert reg_failure is None

    post_result, post_failure = api1.submit_authoritative_create_and_assign(
        conn, aab_store,
        {"establishment_request_id": "req-post", "operation": "create_and_assign",
         "decision_text": "Whether this fixture is A or B is your decision.",
         "recipient_actor_id": CLARK_ACTOR_ID, "source_input_id": "turn-post"},
        ac0["auth_context_id"], session0["session_id"], CLARK_ACTOR_ID, "prod-session-1", enabled=True,
    )
    assert post_failure is None, f"blocker should be gone after legitimate registration, got {post_failure}"
    decision = post_result["decision"]
    # API1-P1: assert against the real canonical CLARK_ACTOR_ID -- the
    # literal "clark" was the pre-patch (buggy) value _commit_tx1() used
    # to write, not a valid actors.actor_id.
    assert decision["owner_state"] == {"status": "resolved", "actor_id": CLARK_ACTOR_ID}
    assert decision["decision_state"] == "open"

    row = conn.execute(
        "SELECT counterpart_actor_id FROM protected_decisions WHERE decision_id = ?", (decision["decision_id"],)
    ).fetchone()
    assert row["counterpart_actor_id"] == profile["actor_id"]

    ac_row = conn.execute(
        "SELECT source_assurance_detail FROM auth_contexts WHERE auth_context_id = ?", (post_result["auth_context_id"],)
    ).fetchone()
    assert ac_row["source_assurance_detail"] == "os_principal_session_bound"


# ------------------------------------------- 24: multi-human coexistence -


def test_second_human_registers_without_corrupting_first():
    # API2-LR1-S1 section 33: the live system legitimately has one
    # canonical human now (post-HIR1-S2) -- fresh_copy_db() copies the
    # CURRENT live DB, so this test must compare against a BEFORE
    # snapshot taken from the copy itself rather than assuming a
    # pre-HIR1-S2 world where persons/actor_human_person start at 0.
    db_path = fresh_copy_db("multi_human")
    conn = open_conn(db_path)
    before_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    before_links = conn.execute("SELECT COUNT(*) FROM actor_human_person").fetchone()[0]

    aab_store = AAB1Store(os.path.join(TEST_DIR, "multi_human_aab.sqlite3"))

    profile_a = provision_profile(aab_store, "Human A", "a", PRINCIPAL)
    principal_b = {"type": "username_domain", "value": "TESTDOMAIN\\humanb"}
    profile_b = provision_profile(aab_store, "Human B", "b", principal_b)

    r_a, f_a = hir1.register_canonical_human(conn, make_request(profile_a["actor_id"], request_id="hir-req-a", display_label="Human A"))
    r_b, f_b = hir1.register_canonical_human(conn, make_request(profile_b["actor_id"], request_id="hir-req-b", display_label="Human B"))
    assert f_a is None and f_b is None
    assert r_a["person_id"] != r_b["person_id"]
    assert r_a["actor_id"] != r_b["actor_id"]

    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == before_persons + 2
    assert conn.execute("SELECT COUNT(*) FROM actor_human_person").fetchone()[0] == before_links + 2

    row_a = conn.execute("SELECT person_id FROM actor_human_person WHERE actor_id = ?", (profile_a["actor_id"],)).fetchone()
    assert row_a["person_id"] == r_a["person_id"]


# ---------------------------------------------------- 25: zero model calls -


def test_zero_model_calls_and_no_secret_leakage():
    forbidden = ("import ollama", "ollama.chat(", "ollama.generate(")
    for fn in ("hir1_registration.py", "hir1_schema_migration.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read().lower()
        for pattern in forbidden:
            assert pattern not in source
        # "password" appears only as a rejected field name in the
        # request-shape guard (HOST_ONLY_FORBIDDEN_FIELDS) -- never as
        # a value that is read, stored, or logged. Confirm that
        # specific usage rather than banning the word outright.
        if "password" in source:
            assert '"password"' in source or "'password'" in source
            assert "= raw_request" not in source.split('"password"')[0].split("\n")[-1]


# ------------------------------------------------------------- runner ----

ALL_TESTS = [
    test_migration_additive_and_idempotent,
    test_human_actor_type_matches_live_schema,
    test_registration_success_reuses_aab_actor_id,
    test_lookup_key_never_consulted,
    test_sid_never_stored,
    test_no_automatic_registration_hook,
    test_bootstrap_origin_is_host_not_new_human,
    test_idempotent_replay_and_conflict,
    test_actor_collision_with_non_human_fails_closed,
    test_person_link_ambiguity_fails_closed,
    test_auth_context_human_trigger_satisfied_after_registration,
    test_rollback_on_injected_failure,
    test_crash_after_commit_recovery,
    test_no_relationship_or_permission_records,
    test_historical_rows_unchanged,
    test_high_alone_is_not_sufficient_normative_assurance,
    test_api1_tx1_unblocked_after_registration,
    test_second_human_registers_without_corrupting_first,
    test_zero_model_calls_and_no_secret_leakage,
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
