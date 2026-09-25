"""Regression coverage for the evidence-bound Workspace-conflict correction.

Every database and log is synthetic and temporary.  No production database,
Private Space, model, GUI, Sleep path, network, or external side effect is
opened by this suite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from conversation_direction import ConversationDirectionFailure, DirectionFailure
from test_waking_turn_recovery import Env
import ui_turn_diagnostics
import waking_failure_evidence as wfe
from waking_turn_failure_capture import run_waking_turn_capturing_failure
import wtr0_cli
import wtr0_waking_recovery as recovery
import wtr0_workspace_conflict_reassessment as reassessment


OLD_BASIS = (
    "type(exc) is conversation_direction.ConversationDirectionFailure but "
    "failure_code='WORKSPACE_ACTION_CONFLICT' is not the BUDGET_EXCEEDED case "
    "this module can positively prove retry-safe; treated conservatively as not retry-safe"
)
OLD_DETAIL = (
    "ConversationDirectionFailure('conversation direction failed at pass1: "
    "WORKSPACE_ACTION_CONFLICT')"
)


def _write_jsonl(path, *records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _records(h_id, *, session_id="synthetic-session", now=None):
    now = float(time.time() if now is None else now)
    diagnostic = {
        "timestamp_start": now,
        "timestamp_end": now + 4.5,
        "session_id": session_id,
        "human_input_event_id": h_id,
        "callback_success": False,
        "waking_turn_success": False,
        "control_failure_contained": True,
        "exception_type": "ConversationDirectionFailure",
        "exception_module": "conversation_direction",
        "exception_message": (
            "conversation direction failed at pass1: WORKSPACE_ACTION_CONFLICT"
        ),
        "pipeline_stage": "pass1",
        "pass1_response_content_chars": 165,
        "pass1_done": True,
        "pass1_done_reason": "stop",
        "pass2_response_content_chars": None,
        "pass2_done": None,
        "pass2_done_reason": None,
        "raw_pass1_output_preview": None,
        "raw_pass1_output_total_chars": None,
        "raw_pass1_output_truncated": None,
        "stages": [
            {"purpose": "retained_dialogue_cost_recovery_probe", "success": True},
            {"purpose": "pass1", "success": True},
        ],
        "context_budget_results": [{"pass": "pass1", "fits": True}],
    }
    trace = {
        "trace_id": "synthetic-trace",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 2)),
        "session_id": session_id,
        "raw_act": "use_workspace",
        "validated_act": "use_workspace",
        "thread": "library",
        "pass1_status": "WORKSPACE_ACTION_CONFLICT",
        "working_set_before": {
            "active_thread": None,
            "active_thread_origin": None,
            "last_conversation_act": None,
            "open_threads": [],
            "direction_owner": "unknown",
        },
        "working_set_after": {
            "active_thread": None,
            "active_thread_origin": None,
            "last_conversation_act": None,
            "open_threads": [],
            "direction_owner": "unknown",
        },
        "direction_owner_before": "unknown",
        "direction_owner_after": "unknown",
        "direction_request": "request_human",
        "relinquish_direction": False,
        "pass2_status": None,
        "event_id": None,
        "staging_id": None,
        "retry_count": 0,
        "fallback_used": False,
    }
    return diagnostic, trace


def _eligible_fixture(name):
    env = Env(name)
    h_id, _authority = env.make_h("Synthetic Workspace conflict input.")
    old_id = wfe.record_waking_failure(
        env.db_path,
        human_input_event_id=h_id,
        failure_class="UNCLASSIFIED_WAKING_EXCEPTION",
        basis=OLD_BASIS,
        detail=OLD_DETAIL,
    )
    diagnostic, trace = _records(h_id)
    diagnostic_path = os.path.join(env.tmp_root, "logs", "ui_turn_diagnostics.jsonl")
    trace_path = os.path.join(env.tmp_root, "conversation_direction_trace.jsonl")
    _write_jsonl(diagnostic_path, diagnostic)
    _write_jsonl(trace_path, trace)
    return env, h_id, old_id, diagnostic_path, trace_path


def _reassess(env, h_id, diagnostic_path, trace_path):
    return reassessment.reassess_workspace_conflict(
        env.db_path,
        h_id,
        env.actor_id,
        diagnostics_path=diagnostic_path,
        trace_path=trace_path,
    )


def test_exact_corroboration_appends_and_makes_same_h_eligible():
    env, h_id, old_id, diagnostic_path, trace_path = _eligible_fixture("ws_reassess_ok")

    receipt = _reassess(env, h_id, diagnostic_path, trace_path)

    assert receipt["created"] is True
    assert receipt["prior_failure_evidence_id"] == old_id
    with env.conn() as conn:
        rows = conn.execute(
            "SELECT failure_class,retry_safe,occurred_at_unavailable FROM "
            "waking_failure_evidence WHERE human_input_event_id=? ORDER BY rowid",
            (h_id,),
        ).fetchall()
        eligibility = recovery.assess_eligibility(conn, h_id, env.actor_id)
    assert rows == [
        ("UNCLASSIFIED_WAKING_EXCEPTION", 0, 1),
        ("WAKING_EXECUTION_FAILURE_NO_X_PERSISTED", 1, 0),
    ]
    assert eligibility["decision"] == recovery.EligibilityDecision.ELIGIBLE
    assert eligibility["failure_evidence_id"] == receipt["failure_evidence_id"]


def test_reassessment_is_idempotent_and_does_not_append_twice():
    env, h_id, _old_id, diagnostic_path, trace_path = _eligible_fixture("ws_reassess_idem")
    first = _reassess(env, h_id, diagnostic_path, trace_path)
    second = _reassess(env, h_id, diagnostic_path, trace_path)
    assert second == {
        "created": False,
        "human_input_event_id": h_id,
        "failure_evidence_id": first["failure_evidence_id"],
        "basis": "LATEST_EVIDENCE_ALREADY_RETRY_SAFE",
    }
    with env.conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM waking_failure_evidence WHERE human_input_event_id=?",
            (h_id,),
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("target", "field", "value", "code"),
    [
        ("diagnostic", "callback_success", True, "UI_DIAGNOSTIC_SHAPE_MISMATCH"),
        ("diagnostic", "pass1_done", False, "UI_DIAGNOSTIC_SHAPE_MISMATCH"),
        ("diagnostic", "human_input_event_id", "other-H", "EXACT_UI_DIAGNOSTIC_NOT_UNIQUE"),
        ("trace", "validated_act", "ask_human", "DIRECTION_TRACE_SHAPE_MISMATCH"),
        ("trace", "pass2_status", "ok", "DIRECTION_TRACE_SHAPE_MISMATCH"),
        ("trace", "event_id", "fabricated-X", "DIRECTION_TRACE_SHAPE_MISMATCH"),
    ],
)
def test_mismatched_durable_evidence_fails_closed(target, field, value, code):
    env, h_id, _old_id, diagnostic_path, trace_path = _eligible_fixture(
        f"ws_reassess_bad_{target}_{field}"
    )
    diagnostic, trace = _records(h_id)
    record = diagnostic if target == "diagnostic" else trace
    record[field] = value
    _write_jsonl(diagnostic_path, diagnostic)
    _write_jsonl(trace_path, trace)

    with pytest.raises(reassessment.ReassessmentDenied) as caught:
        _reassess(env, h_id, diagnostic_path, trace_path)
    assert caught.value.code == code
    with env.conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM waking_failure_evidence WHERE human_input_event_id=?",
            (h_id,),
        ).fetchone()[0] == 1


def test_wrong_actor_and_duplicate_records_fail_closed():
    env, h_id, _old_id, diagnostic_path, trace_path = _eligible_fixture("ws_reassess_identity")
    with pytest.raises(reassessment.ReassessmentDenied) as caught:
        reassessment.reassess_workspace_conflict(
            env.db_path,
            h_id,
            "different-actor",
            diagnostics_path=diagnostic_path,
            trace_path=trace_path,
        )
    assert caught.value.code == "AUTHENTICATED_H_NOT_FOUND"

    diagnostic, trace = _records(h_id)
    _write_jsonl(diagnostic_path, diagnostic, diagnostic)
    with pytest.raises(reassessment.ReassessmentDenied) as caught:
        _reassess(env, h_id, diagnostic_path, trace_path)
    assert caught.value.code == "EXACT_UI_DIAGNOSTIC_NOT_UNIQUE"


def test_newer_evidence_racing_file_validation_is_not_overwritten(monkeypatch):
    env, h_id, _old_id, diagnostic_path, trace_path = _eligible_fixture("ws_reassess_race")
    real_load = reassessment._load_jsonl
    calls = {"count": 0}

    def racing_load(path):
        records = real_load(path)
        calls["count"] += 1
        if calls["count"] == 2:
            wfe.record_waking_failure(
                env.db_path,
                human_input_event_id=h_id,
                failure_class="UNCLASSIFIED_WAKING_EXCEPTION",
                basis="newer independent conservative evidence",
                detail="different failure",
            )
        return records

    monkeypatch.setattr(reassessment, "_load_jsonl", racing_load)
    with pytest.raises(reassessment.ReassessmentDenied) as caught:
        _reassess(env, h_id, diagnostic_path, trace_path)
    assert caught.value.code == "PRIOR_EVIDENCE_CHANGED_DURING_REASSESSMENT"
    with env.conn() as conn:
        classes = conn.execute(
            "SELECT failure_class,retry_safe FROM waking_failure_evidence "
            "WHERE human_input_event_id=? ORDER BY rowid",
            (h_id,),
        ).fetchall()
    assert classes == [
        ("UNCLASSIFIED_WAKING_EXCEPTION", 0),
        ("UNCLASSIFIED_WAKING_EXCEPTION", 0),
    ]


def test_future_exact_conflict_is_classified_retry_safe_but_other_control_failure_is_not():
    env = Env("ws_conflict_prospective")
    h_conflict, _ = env.make_h("Future exact conflict.")
    exact = ConversationDirectionFailure("pass1", DirectionFailure.WORKSPACE_ACTION_CONFLICT)

    def fail_exact(*args, **kwargs):
        ui_turn_diagnostics.record_human_input_event_id(h_conflict)
        raise exact

    with pytest.raises(ConversationDirectionFailure) as caught:
        run_waking_turn_capturing_failure(
            fail_exact, None, "Future exact conflict.", provenance_db_path=env.db_path,
        )
    assert caught.value is exact

    h_other, _ = env.make_h("Different control failure.")
    other = ConversationDirectionFailure("pass1", DirectionFailure.INVALID_ACT)

    def fail_other(*args, **kwargs):
        ui_turn_diagnostics.record_human_input_event_id(h_other)
        raise other

    with pytest.raises(ConversationDirectionFailure):
        run_waking_turn_capturing_failure(
            fail_other, None, "Different control failure.", provenance_db_path=env.db_path,
        )

    with env.conn() as conn:
        conflict_evidence = wfe.latest_failure_evidence(conn, h_conflict)
        other_evidence = wfe.latest_failure_evidence(conn, h_other)
    assert conflict_evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert conflict_evidence["retry_safe"] is True
    assert other_evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert other_evidence["retry_safe"] is False


def test_transactional_writer_refuses_to_run_without_caller_transaction():
    env = Env("ws_reassess_transaction_contract")
    h_id, _ = env.make_h()
    with env.conn() as conn, pytest.raises(sqlite3.ProgrammingError):
        wfe.record_waking_failure_in_transaction(
            conn,
            human_input_event_id=h_id,
            failure_class="WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
            basis="must not be inserted",
        )
    with env.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0] == 0


def test_cli_reassessment_reports_recorded_and_eligible(monkeypatch, capsys):
    env, h_id, _old_id, _diagnostic_path, _trace_path = _eligible_fixture("ws_reassess_cli")
    monkeypatch.setenv("ANAXI_BOUND_HUMAN_ACTOR_ID", env.actor_id)
    # The CLI intentionally derives both evidence paths from the DB directory;
    # _eligible_fixture created them at exactly those production-shaped paths.
    result = wtr0_cli.cmd_reassess_workspace_conflict(
        SimpleNamespace(db=env.db_path, event_id=h_id)
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["decision"] == "RECORDED"
    assert payload["eligibility"]["decision"] == recovery.EligibilityDecision.ELIGIBLE
