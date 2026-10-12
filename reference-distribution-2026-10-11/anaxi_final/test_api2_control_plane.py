"""API2-S1 acceptance test suite. Zero live Ollama/model calls. No test
writes to the live anaxi_provenance.db -- every test operates on a temp
COPY (the live DB, which already carries the API1/HIR1 migrations and
one real registered human actor, with api2's own migration applied on
top of the copy).

Run: python test_api2_control_plane.py
"""
import hashlib
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
sys.path.insert(0, ANAXI_FINAL)

import api2_schema_migration as mig
import api2_control_plane as api2
from provenance_schema import derive_stable_id

TEST_DIR = tempfile.mkdtemp(prefix="api2_test_")
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")

CLARK_ACTOR_ID = derive_stable_id("actor", "clark")


def fresh_copy_db(name):
    path = os.path.join(TEST_DIR, f"{name}.db")
    shutil.copy(SOURCE_DB, path)
    mig.apply_additive_migration(path)
    conn = sqlite3.connect(path)
    api2.install_authoritative_waking_decision_pipeline(conn)
    conn.close()
    return path


def open_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def seed_human_actor(conn, suffix):
    actor_id = f"human-test-{suffix}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, display_label, created_at) "
        "VALUES (?, 'human_person', ?, ?, ?)",
        (actor_id, actor_id, f"Test Human {suffix}", now),
    )
    conn.commit()
    return actor_id


def seed_decision(conn, counterpart_actor_id, decision_id=None, owner_actor_id=CLARK_ACTOR_ID,
                   decision_state="open", resolution_kind=None, last_transition_event_id=None):
    decision_id = decision_id or f"dec-test-{uuid.uuid4().hex[:10]}"
    event_id = last_transition_event_id or f"pde-event-test-{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO protected_decisions (decision_id, decision_text, decision_context, "
        "owner_actor_id, decision_state, resolution_kind, counterpart_actor_id, created_event_id, "
        "last_transition_event_id, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, "Whether this fixture is A or B is your decision.", owner_actor_id,
         decision_state, resolution_kind, counterpart_actor_id, event_id, event_id, now, now),
    )
    conn.commit()
    return decision_id, event_id


def fake_prepared_context(prompt="hello"):
    return {
        "memory_context": "some memory",
        "kardia": {"name": "Test Kardia", "traits": ["curious"]},
        "controls": {"temperature": 0.7, "top_p": 0.9},
        "messages": [{"role": "user", "content": prompt}],
    }


def counting_callable(return_value_or_fn):
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if callable(return_value_or_fn):
            return return_value_or_fn()
        return return_value_or_fn

    _fn.calls = calls
    return _fn


def route_for(conn, decision_id):
    return api2.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)


def snapshot(dbpath, tables):
    conn = sqlite3.connect(dbpath)
    conn.row_factory = sqlite3.Row
    out = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}
    conn.close()
    return out


# ------------------------------------------------------------ migration ----


def test_migration_additive_idempotent_and_data_unchanged():
    # SOURCE_DB (live anaxi_provenance.db) already has this migration
    # applied as of API2-S1's own live-migration step -- copying it
    # would make "new_tables_created == all three" a stale assumption
    # (the same category of staleness API2-LR1-S1 section 32/33 fixed
    # elsewhere). Use the genuine pre-migration backup instead, which
    # robustly proves the additive-from-nothing behavior regardless of
    # the live file's current state.
    pre_migration_source = os.path.join(ANAXI_FINAL, "anaxi_provenance_api2s1_preschema_backup_20260901T161040.db")
    db_path = os.path.join(TEST_DIR, "migration_only.db")
    shutil.copy(pre_migration_source, db_path)
    before = snapshot(db_path, ("events", "protected_decisions", "protected_decision_events", "actors"))

    r1 = mig.apply_additive_migration(db_path)
    assert set(r1["new_tables_created"]) == set(mig.NEW_TABLE_NAMES)
    r2 = mig.apply_additive_migration(db_path)
    assert r2["new_tables_created"] == []

    after = snapshot(db_path, ("events", "protected_decisions", "protected_decision_events", "actors"))
    assert before == after

    state = mig.verify_migration_state(db_path)
    assert all(state.values())


# --------------------------------------------------------- attempt FSM ----


def test_attempt_normal_success_one_prepare_call():
    db_path = fresh_copy_db("attempt_normal")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a1")
    decision_id, event_id = seed_decision(conn, counterpart)

    attempt, failure = api2.create_attempt(conn, decision_id, event_id)
    assert failure is None
    assert attempt["status"] == "created"

    prep = counting_callable(fake_prepared_context)
    result, failure, calls = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure is None
    assert calls == 1
    assert prep.calls["n"] == 1

    row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    assert row["status"] == "context_ready"
    assert row["snapshot_id"] == result["snapshot_id"]


def test_created_recovery_safe():
    db_path = fresh_copy_db("created_recovery")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a2")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    conn.close()

    conn2 = open_conn(db_path)
    prep = counting_callable(fake_prepared_context)
    result, failure, calls = api2.run_preparation(conn2, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure is None
    assert calls == 1


def test_preparing_context_recovery_is_indeterminate():
    db_path = fresh_copy_db("indeterminate")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a3")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    # simulate process boundary lost exactly after begin_preparation's
    # own commit, before prepare_callable was ever invoked
    api2.begin_preparation(conn, attempt["attempt_id"])
    conn.close()

    conn2 = open_conn(db_path)
    prep = counting_callable(fake_prepared_context)
    result, failure, calls = api2.run_preparation(conn2, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure == api2.Api2Failure.PREPARATION_INDETERMINATE
    assert calls == 0
    assert prep.calls["n"] == 0

    # never re-prepared on a SECOND recovery attempt either
    result2, failure2, calls2 = api2.run_preparation(conn2, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure2 == api2.Api2Failure.PREPARATION_INDETERMINATE
    assert calls2 == 0
    assert prep.calls["n"] == 0

    row = conn2.execute("SELECT status FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    assert row["status"] == "preparing_context"  # left as the terminal indeterminate representation


def test_synchronous_prepare_failure_is_terminal():
    db_path = fresh_copy_db("prepare_failed")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a4")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)

    def _raises():
        raise ValueError("synthetic prepare failure")

    prep = counting_callable(_raises)
    result, failure, calls = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure == api2.Api2Failure.SYNCHRONOUS_PREPARE_FAILURE
    assert calls == 1

    row = conn.execute("SELECT status, failure_code FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    assert row["status"] == "prepare_failed"
    assert row["failure_code"] == "PREPARE_RAISED"

    # terminal: no retry even if called again
    result2, failure2, calls2 = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure2 == api2.Api2Failure.ATTEMPT_TERMINAL
    assert calls2 == 0
    assert prep.calls["n"] == 1


def test_context_ready_requires_valid_snapshot_hash():
    db_path = fresh_copy_db("snapshot_hash")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a5")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)

    conn.execute(
        "UPDATE protected_decision_attempt_snapshots SET prepared_context_json = ? WHERE attempt_id = ?",
        (json.dumps({"tampered": True}), attempt["attempt_id"]),
    )
    conn.commit()

    result, failure, calls = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure == api2.Api2Failure.SNAPSHOT_INVALID
    assert calls == 0


def test_context_ready_recovery_zero_calls_exact_snapshot_reuse():
    db_path = fresh_copy_db("context_ready_recovery")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a6")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    first, _, _ = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    conn.close()

    conn2 = open_conn(db_path)
    second, failure, calls = api2.run_preparation(conn2, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure is None
    assert calls == 0
    assert prep.calls["n"] == 1
    assert second["snapshot_hash"] == first["snapshot_hash"]
    assert second["prepared_context_json"] == first["prepared_context_json"]


def test_same_attempt_never_prepares_twice_intentionally():
    db_path = fresh_copy_db("no_double_prepare")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a7")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    for _ in range(3):
        api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert prep.calls["n"] == 1


# ------------------------------------------------------ attempt/stage cardinality


def test_attempt_stage_cardinality_enforced_by_schema():
    db_path = fresh_copy_db("cardinality")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "a8")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    snap = conn.execute("SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()

    now = int(time.time())
    conn.execute(
        "INSERT INTO protected_decision_stages (stage_id, attempt_id, decision_id, "
        "expected_last_transition_event_id, prepared_snapshot_id, prepared_snapshot_hash, action_id, "
        "actor_id, raw_action_json, validated_action_json, status, selected_at) "
        "VALUES ('stage-1', ?, ?, ?, ?, ?, 'action-1', ?, '{}', '{}', 'awaiting_expression', ?)",
        (attempt["attempt_id"], decision_id, event_id, snap["snapshot_id"], snap["snapshot_hash"], CLARK_ACTOR_ID, now),
    )
    conn.commit()

    raised = None
    try:
        conn.execute(
            "INSERT INTO protected_decision_stages (stage_id, attempt_id, decision_id, "
            "expected_last_transition_event_id, prepared_snapshot_id, prepared_snapshot_hash, action_id, "
            "actor_id, raw_action_json, validated_action_json, status, selected_at) "
            "VALUES ('stage-2', ?, ?, ?, ?, ?, 'action-2', ?, '{}', '{}', 'awaiting_expression', ?)",
            (attempt["attempt_id"], decision_id, event_id, snap["snapshot_id"], snap["snapshot_hash"], CLARK_ACTOR_ID, now),
        )
    except sqlite3.IntegrityError as exc:
        raised = exc
    conn.rollback()
    assert raised is not None, "schema must enforce 0..1 stage per attempt"


# --------------------------------------------------------------- Pass 1 ----


def _stage_via_pass1(conn, decision_id, event_id, act, content, delegate_recipient_check=True):
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = route_for(conn, decision_id)
    pass1 = counting_callable({"decision_id": decision_id, "act": act, "content": content})
    stage, failure, calls = api2.run_pass1(conn, dict(attempt_row), route, pass1, CLARK_ACTOR_ID)
    return attempt, stage, failure, calls, pass1


def test_all_six_acts_stage_correctly():
    for act, content in (
        ("choose", "I choose option A."),
        ("refuse", "I decline."),
        ("defer", "Let me think about this."),
        ("ask_clarification", "Can you clarify X?"),
        ("request_advice", "What would you suggest?"),
        ("delegate", "You should decide this one."),
    ):
        db_path = fresh_copy_db(f"pass1_{act}")
        conn = open_conn(db_path)
        counterpart = seed_human_actor(conn, f"pass1_{act}")
        decision_id, event_id = seed_decision(conn, counterpart)
        attempt, stage, failure, calls, pass1 = _stage_via_pass1(conn, decision_id, event_id, act, content)
        assert failure is None, f"act={act} failure={failure}"
        assert calls == 1
        assert stage["status"] == "awaiting_expression"
        validated = json.loads(stage["validated_action_json"])
        assert validated["act"] == act
        if act == "delegate":
            assert validated["delegate_to_actor_id"] == counterpart


def test_invalid_pass1_no_stage_created():
    db_path = fresh_copy_db("pass1_invalid")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "b1")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = route_for(conn, decision_id)
    pass1 = counting_callable({"decision_id": decision_id, "act": "not_a_real_act", "content": "x"})
    stage, failure, calls = api2.run_pass1(conn, dict(attempt_row), route, pass1, CLARK_ACTOR_ID)
    assert failure is not None
    assert calls == 1
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_stages WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0] == 0

    decision = conn.execute("SELECT * FROM protected_decisions WHERE decision_id=?", (decision_id,)).fetchone()
    assert decision["decision_state"] == "open"
    assert decision["owner_actor_id"] == CLARK_ACTOR_ID
    attempt_after = conn.execute("SELECT status FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    assert attempt_after["status"] == "context_ready"


def test_supervised_pass1_reentry_allowed_after_failure_no_new_prepare():
    db_path = fresh_copy_db("pass1_reentry")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "b2")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = route_for(conn, decision_id)

    bad_pass1 = counting_callable({"decision_id": decision_id, "act": "bogus", "content": "x"})
    api2.run_pass1(conn, dict(attempt_row), route, bad_pass1, CLARK_ACTOR_ID)

    good_pass1 = counting_callable({"decision_id": decision_id, "act": "defer", "content": "later"})
    stage, failure, calls = api2.run_pass1(conn, dict(attempt_row), route, good_pass1, CLARK_ACTOR_ID)
    assert failure is None
    assert calls == 1
    assert prep.calls["n"] == 1  # no new prepare_context() call for the re-entry


def test_stage_prevents_pass1_rerun():
    db_path = fresh_copy_db("pass1_no_rerun")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "b3")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, failure, calls, pass1 = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")
    assert failure is None

    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = route_for(conn, decision_id)
    stage2, failure2, calls2 = api2.run_pass1(conn, dict(attempt_row), route, pass1, CLARK_ACTOR_ID)
    assert failure2 is None
    assert calls2 == 0
    assert pass1.calls["n"] == 1
    assert stage2["stage_id"] == stage["stage_id"]


def test_delegate_recipient_invalid_when_no_counterpart():
    db_path = fresh_copy_db("delegate_no_counterpart")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "b4")
    decision_id, event_id = seed_decision(conn, counterpart)
    route = route_for(conn, decision_id)
    route_no_counterpart = dict(route)
    route_no_counterpart["counterpart_actor_id"] = None
    validated, failure = api2.validate_pass1_action(
        route_no_counterpart, {"decision_id": decision_id, "act": "delegate", "content": "x"}, CLARK_ACTOR_ID
    )
    assert failure == api2.ValidationFailure.DELEGATE_RECIPIENT_INVALID


# --------------------------------------------------------------- Pass 2 ----


def test_pass2_structured_linkage_not_message_text():
    db_path = fresh_copy_db("pass2_linkage")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c1")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")
    snapshot_row = conn.execute("SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    pass2_input = api2.build_pass2_input(snapshot_row, stage)
    assert pass2_input["staged_action"]["act"] == "choose"
    assert pass2_input["staged_action"]["content"] == "A"
    for msg in pass2_input["prepared_messages"]:
        assert "choose" not in json.dumps(msg)


def test_pass2_failure_retains_action_no_tx2():
    db_path = fresh_copy_db("pass2_failure")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c2")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")

    pass2 = counting_callable({"wrong_key": "oops"})
    result, failure, calls = api2.run_pass2(conn, stage, pass2)
    assert failure is not None
    assert calls == 1
    row = conn.execute("SELECT status, validated_action_json FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
    assert row["status"] == "awaiting_expression"
    assert json.loads(row["validated_action_json"])["act"] == "choose"
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_events WHERE event_type='decision_state_transitioned'").fetchone()[0] == 0


def test_expression_ready_durability_and_no_double_pass2():
    db_path = fresh_copy_db("pass2_success")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c3")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")

    pass2 = counting_callable({"action_id": stage["action_id"], "expression": "I'll go with A."})
    result, failure, calls = api2.run_pass2(conn, stage, pass2)
    assert failure is None
    assert calls == 1
    assert result["status"] == "expression_ready"

    result2, failure2, calls2 = api2.run_pass2(conn, result, pass2)
    assert failure2 is None
    assert calls2 == 0
    assert pass2.calls["n"] == 1


def test_supervised_pass2_retry_after_failure_same_action_only():
    db_path = fresh_copy_db("pass2_retry")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c4")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")

    bad_pass2 = counting_callable({"wrong": "shape"})
    api2.run_pass2(conn, stage, bad_pass2)
    good_pass2 = counting_callable({"action_id": stage["action_id"], "expression": "Going with A."})
    result, failure, calls = api2.run_pass2(conn, stage, good_pass2)
    assert failure is None
    assert result["action_id"] == stage["action_id"]


def test_pass2_protocol_format_violation_on_json_lookalike():
    db_path = fresh_copy_db("pass2_protocol")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c5")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "choose", "A")
    pass2 = counting_callable({"action_id": stage["action_id"], "expression": '{"sneaky": "payload"}'})
    result, failure, calls = api2.run_pass2(conn, stage, pass2)
    assert failure == api2.ReleaseFailure.PROTOCOL_FORMAT_VIOLATION


def test_semantic_expression_does_not_alter_control_state():
    db_path = fresh_copy_db("semantic_nonauthority")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "c6")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage, _, _, _ = _stage_via_pass1(conn, decision_id, event_id, "refuse", "I refuse.")
    weird_expression = "Absolutely, I'll do exactly what you asked! (structurally valid, semantically contradictory)"
    pass2 = counting_callable({"action_id": stage["action_id"], "expression": weird_expression})
    result, failure, _ = api2.run_pass2(conn, stage, pass2)
    assert failure is None
    tx2_result, tx2_failure = api2.commit_tx2(conn, result["stage_id"], CLARK_ACTOR_ID)
    assert tx2_failure is None
    assert tx2_result["resolution_kind"] == "refused"  # follows the typed act, not the expression wording
    assert tx2_result["decision_state"] == "resolved"


# ----------------------------------------------------------------- TX2 ----


def _fully_staged_and_expressed(conn, decision_id, event_id, act, content, expression):
    attempt, stage, failure, _, _ = _stage_via_pass1(conn, decision_id, event_id, act, content)
    assert failure is None
    pass2 = counting_callable({"action_id": stage["action_id"], "expression": expression})
    result, failure2, _ = api2.run_pass2(conn, stage, pass2)
    assert failure2 is None
    return attempt, result


def test_tx2_all_six_transitions():
    cases = [
        ("choose", "A", "chosen", "resolved", None),
        ("refuse", "no", "refused", "resolved", None),
        ("defer", "later", None, "open", None),
        ("ask_clarification", "what?", None, "open", None),
        ("request_advice", "help?", None, "open", None),
    ]
    for act, content, expected_resolution, expected_state, _ in cases:
        db_path = fresh_copy_db(f"tx2_{act}")
        conn = open_conn(db_path)
        counterpart = seed_human_actor(conn, f"tx2_{act}")
        decision_id, event_id = seed_decision(conn, counterpart)
        attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, act, content, f"Expressing {act}.")
        result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
        assert failure is None, f"act={act} failure={failure}"
        assert result["resolution_kind"] == expected_resolution
        assert result["decision_state"] == expected_state
        assert result["owner_actor_id"] == CLARK_ACTOR_ID

    # delegate separately: owner changes to counterpart
    db_path = fresh_copy_db("tx2_delegate")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "tx2_delegate")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "delegate", "you decide", "This is yours to decide.")
    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure is None
    assert result["owner_actor_id"] == counterpart
    assert result["decision_state"] == "open"
    assert result["resolution_kind"] is None


def test_no_change_acts_still_advance_transition_token():
    db_path = fresh_copy_db("token_advance")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "d1")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "defer", "later", "I'll think about it.")
    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure is None
    decision = conn.execute("SELECT last_transition_event_id FROM protected_decisions WHERE decision_id=?", (decision_id,)).fetchone()
    assert decision["last_transition_event_id"] != event_id
    assert decision["last_transition_event_id"] == result["transition_event_id"]


def test_delegate_independent_of_current_human_auth():
    db_path = fresh_copy_db("delegate_no_auth")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "d2")
    decision_id, event_id = seed_decision(conn, counterpart)
    # no auth_contexts / sessions rows touched anywhere in this flow
    before_auth = conn.execute("SELECT COUNT(*) FROM auth_contexts").fetchone()[0]
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "delegate", "yours", "This is yours.")
    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure is None
    assert result["owner_actor_id"] == counterpart
    after_auth = conn.execute("SELECT COUNT(*) FROM auth_contexts").fetchone()[0]
    assert after_auth == before_auth


def test_different_current_human_cannot_replace_counterpart():
    db_path = fresh_copy_db("different_human")
    conn = open_conn(db_path)
    human_a = seed_human_actor(conn, "human_a")
    human_b = seed_human_actor(conn, "human_b")
    decision_id, event_id = seed_decision(conn, human_a)
    route = route_for(conn, decision_id)
    assert route["counterpart_actor_id"] == human_a
    validated, failure = api2.validate_pass1_action(
        route, {"decision_id": decision_id, "act": "delegate", "content": "x"}, CLARK_ACTOR_ID
    )
    assert failure is None
    assert validated["delegate_to_actor_id"] == human_a
    assert validated["delegate_to_actor_id"] != human_b


def test_tx2_atomic_write_set_exact():
    db_path = fresh_copy_db("tx2_write_set")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "e1")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")

    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_pde_events = conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0]

    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure is None

    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events + 1
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == before_pde_events + 2

    stage_row = conn.execute("SELECT * FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
    assert stage_row["status"] == "committed"
    assert stage_row["committed_event_id"] == result["transition_event_id"]

    main_event = conn.execute("SELECT * FROM events WHERE event_id=?", (result["main_event_id"],)).fetchone()
    assert main_event["event_type"] == "protected_decision_expression"
    assert main_event["pipeline_provenance_status"] == "known"
    assert main_event["pipeline_id"] == api2.resolve_pipeline_id(conn, api2.AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY)

    assert conn.execute("SELECT COUNT(*) FROM event_requesters WHERE event_id=?", (result["main_event_id"],)).fetchone()[0] == 0

    components = conn.execute("SELECT creator_actor_id, component_kind, component_text, content_sha256 FROM event_components WHERE event_id=?", (result["main_event_id"],)).fetchall()
    assert len(components) == 1
    assert components[0]["creator_actor_id"] == CLARK_ACTOR_ID
    assert components[0]["component_kind"] == "protected_decision_expression"
    assert components[0]["component_text"] == "Choosing A."

    subjects = conn.execute("SELECT subject_actor_id, role FROM event_subjects WHERE event_id=?", (result["main_event_id"],)).fetchall()
    assert [(s["subject_actor_id"], s["role"]) for s in subjects] == [(counterpart, "addressee")]


def test_main_event_does_not_claim_aab_authenticated_clark():
    db_path = fresh_copy_db("no_false_auth")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "e2")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")
    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure is None
    main_event = conn.execute("SELECT * FROM events WHERE event_id=?", (result["main_event_id"],)).fetchone()
    assert main_event["auth_context_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM event_requesters WHERE event_id=?", (result["main_event_id"],)).fetchone()[0] == 0
    components = conn.execute("SELECT creator_actor_id FROM event_components WHERE event_id=?", (result["main_event_id"],)).fetchall()
    assert all(c["creator_actor_id"] != counterpart for c in components)


def test_stale_tx2_rejected_no_transition():
    db_path = fresh_copy_db("stale_tx2")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "e3")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")

    # mutate the canonical decision's transition token concurrently, as
    # if some other committed transition happened first
    now = int(time.time())
    conn.execute(
        "UPDATE protected_decisions SET last_transition_event_id = 'someone-else-moved-this', updated_at=? WHERE decision_id=?",
        (now, decision_id),
    )
    conn.commit()

    before_pde_events = conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    result, failure = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure == api2.Api2Failure.STALE_DECISION_STATE
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == before_pde_events
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    stage_row = conn.execute("SELECT status FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
    assert stage_row["status"] == "expression_ready"


def test_tx2_rollback_at_each_injection_point():
    for point in ("before_any_write", "transition_event_insert", "expression_event_insert", "decision_update", "stage_commit", "main_event_insert", "component_insert"):
        db_path = fresh_copy_db(f"tx2_rollback_{point}")
        conn = open_conn(db_path)
        counterpart = seed_human_actor(conn, f"rb_{point}")
        decision_id, event_id = seed_decision(conn, counterpart)
        attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")

        before_pde_events = conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_components = conn.execute("SELECT COUNT(*) FROM event_components").fetchone()[0]
        before_subj = conn.execute("SELECT COUNT(*) FROM event_subjects").fetchone()[0]

        raised = None
        try:
            api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID, _test_inject_failure_after=point)
        except RuntimeError as exc:
            raised = exc
        assert raised is not None, f"point={point}"

        assert conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == before_pde_events, point
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events, point
        assert conn.execute("SELECT COUNT(*) FROM event_components").fetchone()[0] == before_components, point
        assert conn.execute("SELECT COUNT(*) FROM event_subjects").fetchone()[0] == before_subj, point

        decision = conn.execute("SELECT decision_state, owner_actor_id FROM protected_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        assert decision["decision_state"] == "open", point
        assert decision["owner_actor_id"] == CLARK_ACTOR_ID, point

        stage_row = conn.execute("SELECT status FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
        assert stage_row["status"] == "expression_ready", point  # recoverable, not falsely committed


def test_committed_replay_idempotent_zero_new_rows():
    db_path = fresh_copy_db("tx2_replay")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "e4")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")
    result1, failure1 = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure1 is None

    before_pde_events = conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    result2, failure2 = api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    assert failure2 is None
    assert result2 == result1
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_events").fetchone()[0] == before_pde_events
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events


def test_release_never_before_tx2():
    db_path = fresh_copy_db("release_boundary")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "e5")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, stage = _fully_staged_and_expressed(conn, decision_id, event_id, "choose", "A", "Choosing A.")
    row = conn.execute("SELECT status FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
    assert row["status"] == "expression_ready"  # not yet committed/release-eligible
    api2.commit_tx2(conn, stage["stage_id"], CLARK_ACTOR_ID)
    row2 = conn.execute("SELECT status FROM protected_decision_stages WHERE stage_id=?", (stage["stage_id"],)).fetchone()
    assert row2["status"] == "committed"


# ------------------------------------------------------------- gate/capability


def test_top_level_gate_defaults_false():
    assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False


def test_gate_off_refuses_before_any_preparation():
    db_path = fresh_copy_db("gate_off")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "f1")
    decision_id, event_id = seed_decision(conn, counterpart)
    cap = api2.mint_capability(decision_id)
    prep = counting_callable(fake_prepared_context)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})
    pass2 = counting_callable({"action_id": "irrelevant", "expression": "x"})
    result, failure = api2.execute_authoritative_owner_route(
        conn, cap, decision_id, CLARK_ACTOR_ID, event_id, prep, pass1, pass2, "gemma4:e4b", False, enabled=False,
    )
    assert failure == api2.Api2Failure.FEATURE_DISABLED
    assert prep.calls["n"] == 0
    assert pass1.calls["n"] == 0
    assert pass2.calls["n"] == 0
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_attempts").fetchone()[0] == 0


def test_no_capability_refuses():
    db_path = fresh_copy_db("no_cap")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "f2")
    decision_id, event_id = seed_decision(conn, counterpart)
    prep = counting_callable(fake_prepared_context)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})
    pass2 = counting_callable({"action_id": "irrelevant", "expression": "x"})
    result, failure = api2.execute_authoritative_owner_route(
        conn, None, decision_id, CLARK_ACTOR_ID, event_id, prep, pass1, pass2, "gemma4:e4b", False, enabled=True,
    )
    assert failure == api2.Api2Failure.CAPABILITY_INVALID
    assert prep.calls["n"] == 0


def test_consumed_capability_cannot_run_second_decision():
    db_path = fresh_copy_db("consumed_cap")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "f3")
    decision_id, event_id = seed_decision(conn, counterpart)
    decision_id2, event_id2 = seed_decision(conn, counterpart)
    cap = api2.mint_capability(decision_id)
    prep = counting_callable(fake_prepared_context)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})
    pass2 = counting_callable({"action_id": None, "expression": "x"})

    def pass2_fn():
        stage = conn.execute("SELECT action_id FROM protected_decision_stages WHERE decision_id=?", (decision_id,)).fetchone()
        return {"action_id": stage["action_id"], "expression": "Going with A."}

    result, failure = api2.execute_authoritative_owner_route(
        conn, cap, decision_id, CLARK_ACTOR_ID, event_id, prep, pass1, pass2_fn, "gemma4:e4b", False, enabled=True,
    )
    assert failure is None

    result2, failure2 = api2.execute_authoritative_owner_route(
        conn, cap, decision_id2, CLARK_ACTOR_ID, event_id2, prep, pass1, pass2_fn, "gemma4:e4b", False, enabled=True,
    )
    assert failure2 == api2.Api2Failure.CAPABILITY_CONSUMED


def test_capability_scoped_to_its_own_decision_only():
    db_path = fresh_copy_db("cap_scope")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "f4")
    decision_id, event_id = seed_decision(conn, counterpart)
    decision_id2, event_id2 = seed_decision(conn, counterpart)
    cap = api2.mint_capability(decision_id)
    _cap, failure = api2.consume_capability(cap, decision_id2)
    assert failure == api2.Api2Failure.CAPABILITY_INVALID


def test_ordinary_prose_cannot_mint_capability():
    ordinary_message = {"decision_id": "dec-1", "capability": True, "role": "supervisor"}
    assert not isinstance(ordinary_message, api2.Capability)
    _cap, failure = api2.consume_capability(ordinary_message, "dec-1")
    assert failure == api2.Api2Failure.CAPABILITY_INVALID


def test_end_to_end_full_chain_enabled_on_temp_copy():
    db_path = fresh_copy_db("full_chain")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "g1")
    decision_id, event_id = seed_decision(conn, counterpart)
    cap = api2.mint_capability(decision_id)
    prep = counting_callable(fake_prepared_context)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})

    def pass2_fn():
        stage = conn.execute("SELECT action_id FROM protected_decision_stages WHERE decision_id=?", (decision_id,)).fetchone()
        return {"action_id": stage["action_id"], "expression": "I'll go with A."}

    result, failure = api2.execute_authoritative_owner_route(
        conn, cap, decision_id, CLARK_ACTOR_ID, event_id, prep, pass1, pass2_fn, "gemma4:e4b", False, enabled=True,
    )
    assert failure is None
    assert result["decision_state"] == "resolved"
    assert result["resolution_kind"] == "chosen"
    assert prep.calls["n"] == 1
    assert pass1.calls["n"] == 1


# ---------------------------------------------------------- noninterference


def test_no_direct_hippocampal_write():
    with open(os.path.join(ANAXI_FINAL, "api2_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    assert "hippocampus_store" not in source
    assert "hippocampus_retrieval" not in source
    assert "sync_hippocampus" not in source


def test_no_legacy_projections():
    with open(os.path.join(ANAXI_FINAL, "api2_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("log_entry(", "anaxi_log.jsonl", "turn_generation_log", "native_turn_staging"):
        assert forbidden not in source


def test_ordinary_waking_path_noninterference():
    for fn in ("llama_anaxi.py", "orchestration.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        assert "api2_control_plane" not in source, f"{fn} must not import api2_control_plane"


def test_zero_live_model_calls_in_source():
    for fn in ("api2_control_plane.py", "api2_schema_migration.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read().lower()
        for pattern in ("import ollama", "ollama.chat(", "ollama.generate("):
            assert pattern not in source


ALL_TESTS = [
    test_migration_additive_idempotent_and_data_unchanged,
    test_attempt_normal_success_one_prepare_call,
    test_created_recovery_safe,
    test_preparing_context_recovery_is_indeterminate,
    test_synchronous_prepare_failure_is_terminal,
    test_context_ready_requires_valid_snapshot_hash,
    test_context_ready_recovery_zero_calls_exact_snapshot_reuse,
    test_same_attempt_never_prepares_twice_intentionally,
    test_attempt_stage_cardinality_enforced_by_schema,
    test_all_six_acts_stage_correctly,
    test_invalid_pass1_no_stage_created,
    test_supervised_pass1_reentry_allowed_after_failure_no_new_prepare,
    test_stage_prevents_pass1_rerun,
    test_delegate_recipient_invalid_when_no_counterpart,
    test_pass2_structured_linkage_not_message_text,
    test_pass2_failure_retains_action_no_tx2,
    test_expression_ready_durability_and_no_double_pass2,
    test_supervised_pass2_retry_after_failure_same_action_only,
    test_pass2_protocol_format_violation_on_json_lookalike,
    test_semantic_expression_does_not_alter_control_state,
    test_tx2_all_six_transitions,
    test_no_change_acts_still_advance_transition_token,
    test_delegate_independent_of_current_human_auth,
    test_different_current_human_cannot_replace_counterpart,
    test_tx2_atomic_write_set_exact,
    test_main_event_does_not_claim_aab_authenticated_clark,
    test_stale_tx2_rejected_no_transition,
    test_tx2_rollback_at_each_injection_point,
    test_committed_replay_idempotent_zero_new_rows,
    test_release_never_before_tx2,
    test_top_level_gate_defaults_false,
    test_gate_off_refuses_before_any_preparation,
    test_no_capability_refuses,
    test_consumed_capability_cannot_run_second_decision,
    test_capability_scoped_to_its_own_decision_only,
    test_ordinary_prose_cannot_mint_capability,
    test_end_to_end_full_chain_enabled_on_temp_copy,
    test_no_direct_hippocampal_write,
    test_no_legacy_projections,
    test_ordinary_waking_path_noninterference,
    test_zero_live_model_calls_in_source,
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
