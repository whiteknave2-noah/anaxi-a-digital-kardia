"""Evidence-bound WTR0 reassessment for the consolidation no-op mismatch.

This is the one retrospective correction for an older runtime that rejected
the model-facing literal ``"none"`` in inactive consolidation companion
fields.  It does not make Pass-1, re-entry, semantic-validation, tool-using,
or ``UNCLASSIFIED_WAKING_EXCEPTION`` failures generally retry-safe.

The correction appends a new, specifically named failure-evidence row only
when independent durable evidence agrees on the exact pre-Pass2 boundary:

* the old non-retry-safe row is preserved and names
  pass1_reentry/INVALID_CONSOLIDATION_TARGET exactly;
* canonical provenance has one authenticated principal-private H for the
  requested actor/session, no linked X, no source-linked side event, and no
  recovery identity;
* the UI diagnostic records one successful initial Pass 1 and exactly two
  successful re-entry provider completions, no Pass 2 or persistence, and an
  untruncated structured payload whose consolidation request and inactive
  companions are all the literal no-op sentinel;
* the direction trace records the same validation failure, unchanged working
  and ownership state, and no event/staging identity;
* every Workspace action-log record during that attempt is a successful
  read-only journal action (or its content-free selection bookkeeping); and
* the verified staging ledger contains no payload for H.

No historical row or log is edited.  The appended row records only the newer
law's additional mechanical proof; ordinary WTR0 reservation, owner
confirmation, scope binding, background-pause, and cold-reset protections stay
authoritative after this reassessment.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3

import family_membership
from native_turn_staging import compute_staging_id
from provenance_schema import derive_stable_id
from waking_failure_evidence import (
    latest_failure_evidence,
    record_waking_failure_in_transaction,
)


FAILURE_CLASS = "CONSOLIDATION_NOOP_VALIDATION_PRE_PASS2"
_FAILURE_CODE = "INVALID_CONSOLIDATION_TARGET"
_STAGE = "pass1_reentry"
_EXCEPTION_MESSAGE = f"conversation direction failed at {_STAGE}: {_FAILURE_CODE}"
_EXCEPTION_DETAIL = f"ConversationDirectionFailure({_EXCEPTION_MESSAGE!r})"
_BASIS_PREFIX = "consolidation-noop evidence reassessment v1"
_SCOPE = "principal_private"
_NOOP = "none"
_CLARK_ACTOR_ID = derive_stable_id("actor", "clark")

_READ_ONLY_JOURNAL_ACTIONS = frozenset({"list", "read"})
_NON_EFFECT_SELECTION_ACTION = "selection"
_ALLOWED_MODEL_STAGES = frozenset({
    # One-token cost probes are measurements only: they produce no subject
    # prose and cannot dispatch or persist an action.
    "real_text_cost_recovery_probe",
    "retained_dialogue_cost_recovery_probe",
    "pass1",
    "pass1_reentry",
})


class ReassessmentDenied(Exception):
    """The durable evidence did not establish this one safe boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"consolidation-noop reassessment denied: {code}")


def _deny(code):
    raise ReassessmentDenied(code)


def _read_bytes(path, *, required):
    if not os.path.isfile(path):
        if required:
            _deny("EVIDENCE_FILE_NOT_FOUND")
        return b""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        _deny("EVIDENCE_FILE_UNREADABLE")


def _jsonl_snapshot(path, *, required=True):
    raw = _read_bytes(path, required=required)
    records = []
    try:
        for line in raw.splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    _deny("EVIDENCE_RECORD_NOT_OBJECT")
                records.append(value)
    except (UnicodeError, json.JSONDecodeError):
        _deny("EVIDENCE_FILE_UNREADABLE")
    return records, hashlib.sha256(raw).hexdigest()


def _record_digest(record):
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_time(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        _deny("ACTION_TIMESTAMP_NOT_ESTABLISHED")


def _canonical_h(conn, event_id, actor_id):
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)")}
    if "visibility_scope" not in event_columns or "visibility_scope" not in auth_columns:
        _deny("H_SCOPE_NOT_ESTABLISHED")
    rows = conn.execute(
        "SELECT ec.creator_actor_id, a.auth_state, a.authenticated_actor_id, "
        "e.visibility_scope, a.visibility_scope, a.session_id, ar.retired_at "
        "FROM events e JOIN event_components ec ON ec.event_id=e.event_id "
        "JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
        "JOIN actor_human_person hp ON hp.actor_id=ec.creator_actor_id "
        "JOIN actors ar ON ar.actor_id=ec.creator_actor_id "
        "WHERE e.event_id=? AND e.event_type='human_waking_input' "
        "AND ec.sequence=0 AND ec.component_kind='human_conversational_input'",
        (event_id,),
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] != actor_id
        or rows[0][1] != "authenticated"
        or rows[0][2] != actor_id
        or rows[0][3] != _SCOPE
        or rows[0][4] != _SCOPE
        or rows[0][6] is not None
    ):
        _deny("AUTHENTICATED_SCOPED_H_NOT_FOUND")
    return {"session_id": rows[0][5], "visibility_scope": rows[0][3]}


def _has_linked_x(conn, event_id):
    return conn.execute(
        "SELECT 1 FROM event_components c JOIN events e ON e.event_id=c.event_id "
        "WHERE c.component_kind='human_input_event_id' AND c.component_text=? "
        "AND e.event_type='waking_turn' LIMIT 1",
        (event_id,),
    ).fetchone() is not None


def _has_source_linked_event(conn, event_id):
    return conn.execute(
        "SELECT 1 FROM events WHERE input_source_ref=? LIMIT 1", (event_id,),
    ).fetchone() is not None


def _has_consolidation_side_effect(conn, event_id):
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='consolidation_records'"
    ).fetchone()
    if table is None:
        return False
    return conn.execute(
        "SELECT 1 FROM consolidation_records WHERE occasion_event_id=? LIMIT 1",
        (event_id,),
    ).fetchone() is not None


def _check_canonical_state(conn, event_id, actor_id, expected_session_id=None):
    h = _canonical_h(conn, event_id, actor_id)
    if family_membership.session_binding_audience(conn, h["session_id"]) != (
        actor_id, _SCOPE,
    ):
        _deny("SESSION_BINDING_PROVENANCE_MISMATCH")
    if expected_session_id is not None and h["session_id"] != expected_session_id:
        _deny("H_SESSION_PROVENANCE_MISMATCH")
    if _has_linked_x(conn, event_id):
        _deny("X_ALREADY_LINKED")
    if _has_source_linked_event(conn, event_id):
        _deny("SOURCE_LINKED_CANONICAL_SIDE_EFFECT")
    if _has_consolidation_side_effect(conn, event_id):
        _deny("CONSOLIDATION_SIDE_EFFECT_EXISTS")
    if conn.execute(
        "SELECT 1 FROM wtr0_recovery WHERE human_input_event_id=? LIMIT 1", (event_id,)
    ).fetchone() is not None:
        _deny("RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
    return h


def _validate_prior(prior):
    if prior is None:
        _deny("PRIOR_FAILURE_EVIDENCE_MISSING")
    if prior["retry_safe"]:
        return
    if (
        prior["failure_class"] != "UNCLASSIFIED_WAKING_EXCEPTION"
        or prior["detail"] != _EXCEPTION_DETAIL
        or "conversation_direction.ConversationDirectionFailure" not in prior["basis"]
        or repr(_FAILURE_CODE) not in prior["basis"]
    ):
        _deny("PRIOR_EVIDENCE_NOT_EXACT_CONSOLIDATION_NOOP_FAILURE")


def _validate_noop_payload(record):
    preview = record.get("raw_pass1_output_preview")
    total = record.get("raw_pass1_output_total_chars")
    if (
        not isinstance(preview, str)
        or record.get("raw_pass1_output_truncated") is not False
        or total != len(preview)
    ):
        _deny("RAW_PASS1_PAYLOAD_NOT_COMPLETE")
    try:
        payload = json.loads(preview)
    except (TypeError, json.JSONDecodeError):
        _deny("RAW_PASS1_PAYLOAD_NOT_STRUCTURED")
    if not isinstance(payload, dict):
        _deny("RAW_PASS1_PAYLOAD_NOT_STRUCTURED")
    exact_noop = {
        "consolidation_request": _NOOP,
        "consolidation_target": _NOOP,
        "consolidation_interpretation": _NOOP,
        "consolidation_prior_record_id": _NOOP,
    }
    if any(payload.get(key) != value for key, value in exact_noop.items()):
        _deny("RAW_PASS1_NOT_EXACT_CONSOLIDATION_NOOP")
    return payload


def _validate_diagnostic(record, event_id, session_id):
    exact = {
        "human_input_event_id": event_id,
        "session_id": session_id,
        "callback_success": False,
        "waking_turn_success": False,
        "control_failure_contained": True,
        "exception_type": "ConversationDirectionFailure",
        "exception_module": "conversation_direction",
        "exception_message": _EXCEPTION_MESSAGE,
        "pipeline_stage": _STAGE,
        "pass1_done": True,
        "pass1_done_reason": "stop",
        "pass2_response_content_chars": None,
        "pass2_done": None,
        "pass2_done_reason": None,
    }
    if any(record.get(key) != value for key, value in exact.items()):
        _deny("UI_DIAGNOSTIC_SHAPE_MISMATCH")
    if not isinstance(record.get("pass1_response_content_chars"), int) or record[
        "pass1_response_content_chars"
    ] <= 0:
        _deny("REENTRY_COMPLETION_NOT_ESTABLISHED")
    start, end = record.get("timestamp_start"), record.get("timestamp_end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start:
        _deny("UI_TIMESTAMP_NOT_ESTABLISHED")
    stages = record.get("stages")
    if not isinstance(stages, list) or not stages:
        _deny("MODEL_STAGE_EVIDENCE_MISSING")
    if any(
        not isinstance(stage, dict)
        or stage.get("success") is not True
        or stage.get("purpose") not in _ALLOWED_MODEL_STAGES
        for stage in stages
    ):
        _deny("NONQUALIFYING_STAGE_OBSERVED")
    purposes = [stage.get("purpose") for stage in stages]
    if purposes.count("pass1") != 1 or purposes.count("pass1_reentry") != 2:
        _deny("EXACT_PASS1_REENTRY_SEQUENCE_NOT_ESTABLISHED")
    budgets = record.get("context_budget_results")
    if not isinstance(budgets, list):
        _deny("CONTEXT_BUDGET_EVIDENCE_MISSING")
    budget_passes = [item.get("pass") for item in budgets if item.get("fits") is True]
    if budget_passes.count("pass1") != 1 or budget_passes.count("pass1_reentry") != 2:
        _deny("EXACT_PASS1_REENTRY_BUDGET_SEQUENCE_NOT_ESTABLISHED")
    _validate_noop_payload(record)


def _validate_trace(record, session_id, diagnostic):
    exact = {
        "session_id": session_id,
        "validated_act": None,
        "thread": None,
        "pass1_status": _FAILURE_CODE,
        "direction_request": None,
        "relinquish_direction": None,
        "pass2_status": None,
        "event_id": None,
        "staging_id": None,
        "retry_count": 0,
        "fallback_used": False,
    }
    if any(record.get(key) != value for key, value in exact.items()):
        _deny("DIRECTION_TRACE_SHAPE_MISMATCH")
    if not isinstance(record.get("raw_act"), str) or not record["raw_act"]:
        _deny("RAW_ACT_NOT_ESTABLISHED")
    if record.get("working_set_before") != record.get("working_set_after"):
        _deny("WORKING_SET_CHANGED")
    if record.get("direction_owner_before") != record.get("direction_owner_after"):
        _deny("DIRECTION_OWNER_CHANGED")
    trace_time = _parse_time(record.get("timestamp"))
    if not diagnostic["timestamp_start"] - 1.0 <= trace_time <= diagnostic["timestamp_end"] + 1.0:
        _deny("TRACE_UI_TIME_MISMATCH")


def _validate_action_log(records, diagnostic):
    during = []
    for record in records:
        stamp = _parse_time(record.get("timestamp"))
        if diagnostic["timestamp_start"] - 1.0 <= stamp <= diagnostic["timestamp_end"] + 1.0:
            during.append(record)
    if not during:
        _deny("READ_ONLY_ACTIVITY_NOT_ESTABLISHED")
    substantive = []
    for record in during:
        if record.get("requester_actor_id") != _CLARK_ACTOR_ID:
            _deny("WORKSPACE_ACTOR_PROVENANCE_MISMATCH")
        if record.get("resource_class") != "journal":
            _deny("NON_JOURNAL_ACTIVITY_OBSERVED")
        action = record.get("action")
        if action == _NON_EFFECT_SELECTION_ACTION:
            if record.get("result") not in {"resolved", "stopped_by_subject", "unresolved"}:
                _deny("INVALID_SELECTION_BOOKKEEPING")
            continue
        if action not in _READ_ONLY_JOURNAL_ACTIONS or record.get("result") != "performed":
            _deny("CONSEQUENTIAL_OR_FAILED_ACTION_OBSERVED")
        substantive.append(record)
    if not substantive:
        _deny("READ_ONLY_ACTIVITY_NOT_ESTABLISHED")
    return during


def _staging_snapshot(path, event_id):
    raw = _read_bytes(path, required=False)
    try:
        for index, line in enumerate(raw.splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or "staging_id" not in record:
                _deny("STAGING_LEDGER_UNVERIFIED")
            stored = record["staging_id"]
            payload = {key: value for key, value in record.items() if key != "staging_id"}
            if compute_staging_id(payload, index) != stored:
                _deny("STAGING_LEDGER_UNVERIFIED")
            if payload.get("human_input_event_id") == event_id:
                _deny("STAGING_EXISTS_FOR_H")
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        _deny("STAGING_LEDGER_UNVERIFIED")
    return hashlib.sha256(raw).hexdigest()


def reassess_consolidation_noop(
    db_path,
    event_id,
    actor_id,
    *,
    diagnostics_path=None,
    trace_path=None,
    action_log_path=None,
    staging_path=None,
):
    """Append the qualifying row and return its provenance receipt.

    The CLI derives all evidence paths from the deployed ANAXI directory.
    Keyword path overrides exist only for isolated synthetic tests.
    """
    base = os.path.dirname(os.path.abspath(db_path))
    diagnostics_path = diagnostics_path or os.path.join(base, "logs", "ui_turn_diagnostics.jsonl")
    trace_path = trace_path or os.path.join(base, "conversation_direction_trace.jsonl")
    action_log_path = action_log_path or os.path.join(base, "workspace", "workspace_action_log.jsonl")
    staging_path = staging_path or os.path.join(base, "native_turn_staging.jsonl")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        h = _check_canonical_state(conn, event_id, actor_id)
        prior = latest_failure_evidence(conn, event_id)
    finally:
        conn.close()
    _validate_prior(prior)
    if prior["retry_safe"]:
        return {
            "created": False,
            "human_input_event_id": event_id,
            "failure_evidence_id": prior["failure_evidence_id"],
            "basis": "LATEST_EVIDENCE_ALREADY_RETRY_SAFE",
        }

    diagnostics, diagnostics_file_sha = _jsonl_snapshot(diagnostics_path)
    matching_diagnostics = [
        item for item in diagnostics if item.get("human_input_event_id") == event_id
    ]
    if len(matching_diagnostics) != 1:
        _deny("EXACT_UI_DIAGNOSTIC_NOT_UNIQUE")
    diagnostic = matching_diagnostics[0]
    _validate_diagnostic(diagnostic, event_id, h["session_id"])

    traces, trace_file_sha = _jsonl_snapshot(trace_path)
    matching_traces = [
        item for item in traces
        if item.get("session_id") == h["session_id"]
        and item.get("pass1_status") == _FAILURE_CODE
    ]
    if len(matching_traces) != 1:
        _deny("EXACT_DIRECTION_TRACE_NOT_UNIQUE")
    trace = matching_traces[0]
    _validate_trace(trace, h["session_id"], diagnostic)

    action_records, action_file_sha = _jsonl_snapshot(action_log_path)
    actions = _validate_action_log(action_records, diagnostic)
    staging_sha = _staging_snapshot(staging_path, event_id)

    diagnostic_sha = _record_digest(diagnostic)
    trace_sha = _record_digest(trace)
    action_sha = hashlib.sha256(json.dumps(
        actions, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    basis = (
        f"{_BASIS_PREFIX}: prior evidence {prior['failure_evidence_id']}; authenticated "
        f"{_SCOPE} H/session provenance; UI diagnostic sha256={diagnostic_sha}, direction "
        f"trace sha256={trace_sha}, and read-only journal activity sha256={action_sha} agree "
        "on one successful initial Pass 1 plus two successful read-only-inspection re-entries, "
        "followed by the exact literal consolidation no-op target mismatch before Pass 2, "
        "release, staging, persistence, canonical side events, consolidation, or X. Recovery "
        "is authorized only through ordinary explicit one-attempt WTR0."
    )
    detail = json.dumps({
        "kind": _BASIS_PREFIX,
        "prior_failure_evidence_id": prior["failure_evidence_id"],
        "session_id": h["session_id"],
        "visibility_scope": h["visibility_scope"],
        "ui_diagnostic_sha256": diagnostic_sha,
        "ui_diagnostics_file_sha256": diagnostics_file_sha,
        "direction_trace_id": trace.get("trace_id"),
        "direction_trace_sha256": trace_sha,
        "direction_trace_file_sha256": trace_file_sha,
        "workspace_actions_sha256": action_sha,
        "workspace_action_log_sha256": action_file_sha,
        "staging_ledger_sha256": staging_sha,
    }, sort_keys=True, separators=(",", ":"))

    # Hold the canonical writer lock while re-checking H/X/recovery/evidence,
    # and refuse if any adjacent evidence file changed since validation.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        _check_canonical_state(conn, event_id, actor_id, expected_session_id=h["session_id"])
        current_prior = latest_failure_evidence(conn, event_id)
        if current_prior is None:
            _deny("PRIOR_FAILURE_EVIDENCE_MISSING")
        if current_prior["retry_safe"]:
            conn.execute("ROLLBACK")
            return {
                "created": False,
                "human_input_event_id": event_id,
                "failure_evidence_id": current_prior["failure_evidence_id"],
                "basis": "LATEST_EVIDENCE_ALREADY_RETRY_SAFE",
            }
        if current_prior["failure_evidence_id"] != prior["failure_evidence_id"]:
            _deny("PRIOR_EVIDENCE_CHANGED_DURING_REASSESSMENT")
        if _jsonl_snapshot(diagnostics_path)[1] != diagnostics_file_sha:
            _deny("UI_DIAGNOSTICS_CHANGED_DURING_REASSESSMENT")
        if _jsonl_snapshot(trace_path)[1] != trace_file_sha:
            _deny("DIRECTION_TRACE_CHANGED_DURING_REASSESSMENT")
        if _jsonl_snapshot(action_log_path)[1] != action_file_sha:
            _deny("ACTION_LOG_CHANGED_DURING_REASSESSMENT")
        if _staging_snapshot(staging_path, event_id) != staging_sha:
            _deny("STAGING_CHANGED_DURING_REASSESSMENT")
        evidence_id = record_waking_failure_in_transaction(
            conn,
            human_input_event_id=event_id,
            failure_class=FAILURE_CLASS,
            basis=basis,
            detail=detail,
            occurred_at=int(diagnostic["timestamp_end"]),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "created": True,
        "human_input_event_id": event_id,
        "failure_evidence_id": evidence_id,
        "prior_failure_evidence_id": prior["failure_evidence_id"],
        "session_id": h["session_id"],
        "visibility_scope": h["visibility_scope"],
        "ui_diagnostic_sha256": diagnostic_sha,
        "direction_trace_sha256": trace_sha,
        "workspace_actions_sha256": action_sha,
        "staging_ledger_sha256": staging_sha,
    }
