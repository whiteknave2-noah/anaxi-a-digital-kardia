"""Evidence-bound WTR0 reassessment for a pre-action Workspace conflict.

This is a narrow corrective surface for an already-recorded failure that an
older classifier conservatively stored as ``UNCLASSIFIED_WAKING_EXCEPTION``.
It does not infer from an unanswered H, accept a caller-supplied failure
class, or parse arbitrary exception prose by itself.  It requires three
independent durable records to agree on the same failed attempt:

* canonical provenance: one authenticated H for the bound actor, no linked X,
  and no recovery reservation;
* the outer UI diagnostic: the exact typed ConversationDirectionFailure at
  Pass 1 after a completed provider response, with no Pass 2 or persistence;
* the conversation-direction trace: validated ``use_workspace``, unchanged
  working/direction state, and no Pass 2 event or staging identity.

Only that mechanically established pre-action boundary may append a newer
retry-safe evidence row.  Existing evidence is never edited or deleted.  The
normal WTR0 eligibility and one-attempt reservation checks remain authoritative
after reassessment.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3

from waking_failure_evidence import (
    latest_failure_evidence,
    record_waking_failure_in_transaction,
)


_FAILURE_CODE = "WORKSPACE_ACTION_CONFLICT"
_EXCEPTION_MESSAGE = (
    "conversation direction failed at pass1: WORKSPACE_ACTION_CONFLICT"
)
_EXCEPTION_DETAIL = f"ConversationDirectionFailure({_EXCEPTION_MESSAGE!r})"
_BASIS_PREFIX = "workspace-conflict evidence reassessment v1"


class ReassessmentDenied(Exception):
    """The durable evidence did not establish the narrow safe boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"workspace-conflict reassessment denied: {code}")


def _deny(code):
    raise ReassessmentDenied(code)


def _load_jsonl(path):
    if not os.path.isfile(path):
        _deny("EVIDENCE_FILE_NOT_FOUND")
    records = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        _deny("EVIDENCE_RECORD_NOT_OBJECT")
                    records.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError):
        _deny("EVIDENCE_FILE_UNREADABLE")
    return records


def _record_digest(record):
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authenticated_h_exists(conn, event_id, actor_id):
    rows = conn.execute(
        "SELECT ec.creator_actor_id, a.auth_state, a.authenticated_actor_id "
        "FROM events e JOIN event_components ec ON ec.event_id=e.event_id "
        "JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
        "WHERE e.event_id=? AND e.event_type='human_waking_input' "
        "AND ec.sequence=0 AND ec.component_kind='human_conversational_input'",
        (event_id,),
    ).fetchall()
    return (
        len(rows) == 1
        and rows[0][0] == actor_id
        and rows[0][1] == "authenticated"
        and rows[0][2] == actor_id
    )


def _has_linked_x(conn, event_id):
    return conn.execute(
        "SELECT 1 FROM event_components c JOIN events e ON e.event_id=c.event_id "
        "WHERE c.component_kind='human_input_event_id' AND c.component_text=? "
        "AND e.event_type='waking_turn' LIMIT 1",
        (event_id,),
    ).fetchone() is not None


def _validate_diagnostic(record, event_id):
    exact = {
        "human_input_event_id": event_id,
        "callback_success": False,
        "waking_turn_success": False,
        "control_failure_contained": True,
        "exception_type": "ConversationDirectionFailure",
        "exception_module": "conversation_direction",
        "exception_message": _EXCEPTION_MESSAGE,
        "pipeline_stage": "pass1",
        "pass1_done": True,
        "pass1_done_reason": "stop",
        "pass2_response_content_chars": None,
        "pass2_done": None,
        "pass2_done_reason": None,
        "raw_pass1_output_preview": None,
        "raw_pass1_output_total_chars": None,
        "raw_pass1_output_truncated": None,
    }
    if any(record.get(key) != value for key, value in exact.items()):
        _deny("UI_DIAGNOSTIC_SHAPE_MISMATCH")
    if not isinstance(record.get("session_id"), str) or not record["session_id"]:
        _deny("UI_SESSION_NOT_ESTABLISHED")
    if not isinstance(record.get("pass1_response_content_chars"), int) or record[
        "pass1_response_content_chars"
    ] <= 0:
        _deny("PASS1_COMPLETION_NOT_ESTABLISHED")
    stages = record.get("stages")
    if not isinstance(stages, list) or not stages:
        _deny("MODEL_STAGE_EVIDENCE_MISSING")
    pass1_stages = [stage for stage in stages if stage.get("purpose") == "pass1"]
    if len(pass1_stages) != 1 or pass1_stages[0].get("success") is not True:
        _deny("PASS1_STAGE_NOT_SUCCESSFUL")
    if any(
        stage.get("purpose") in {"pass2", "persistence", "workspace_pass1", "workspace_pass2"}
        for stage in stages
    ):
        _deny("LATER_STAGE_OBSERVED")
    budgets = record.get("context_budget_results")
    if not isinstance(budgets, list) or not any(
        item.get("pass") == "pass1" and item.get("fits") is True for item in budgets
    ):
        _deny("PASS1_BUDGET_SUCCESS_NOT_ESTABLISHED")
    if not isinstance(record.get("timestamp_start"), (int, float)) or not isinstance(
        record.get("timestamp_end"), (int, float)
    ):
        _deny("UI_TIMESTAMP_NOT_ESTABLISHED")


def _validate_trace(record, session_id, diagnostic):
    exact = {
        "session_id": session_id,
        "raw_act": "use_workspace",
        "validated_act": "use_workspace",
        "pass1_status": _FAILURE_CODE,
        "pass2_status": None,
        "event_id": None,
        "staging_id": None,
        "retry_count": 0,
        "fallback_used": False,
    }
    if any(record.get(key) != value for key, value in exact.items()):
        _deny("DIRECTION_TRACE_SHAPE_MISMATCH")
    if record.get("working_set_before") != record.get("working_set_after"):
        _deny("WORKING_SET_CHANGED")
    if record.get("direction_owner_before") != record.get("direction_owner_after"):
        _deny("DIRECTION_OWNER_CHANGED")
    if not isinstance(record.get("relinquish_direction"), bool):
        _deny("DIRECTION_CONTROL_NOT_ESTABLISHED")
    try:
        trace_time = datetime.datetime.fromisoformat(record["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        _deny("TRACE_TIMESTAMP_NOT_ESTABLISHED")
    if not (
        diagnostic["timestamp_start"] - 1.0
        <= trace_time
        <= diagnostic["timestamp_end"] + 1.0
    ):
        _deny("TRACE_UI_TIME_MISMATCH")


def reassess_workspace_conflict(
    db_path, event_id, actor_id, *, diagnostics_path=None, trace_path=None,
):
    """Append qualifying evidence and return an auditable receipt.

    Paths are normally derived from the canonical database directory.  The
    keyword overrides exist only so synthetic tests can exercise the same
    parser; the operator CLI intentionally exposes no path override.
    """
    base = os.path.dirname(os.path.abspath(db_path))
    diagnostics_path = diagnostics_path or os.path.join(
        base, "logs", "ui_turn_diagnostics.jsonl",
    )
    trace_path = trace_path or os.path.join(base, "conversation_direction_trace.jsonl")

    # Fast read-only preflight. The exact same canonical conditions are checked
    # again under BEGIN IMMEDIATE immediately before insertion below; this first
    # pass avoids parsing evidence files for an already-ineligible target.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not _authenticated_h_exists(conn, event_id, actor_id):
            _deny("AUTHENTICATED_H_NOT_FOUND")
        if _has_linked_x(conn, event_id):
            _deny("X_ALREADY_LINKED")
        if conn.execute(
            "SELECT 1 FROM wtr0_recovery WHERE human_input_event_id=? LIMIT 1", (event_id,)
        ).fetchone() is not None:
            _deny("RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
        prior = latest_failure_evidence(conn, event_id)
    finally:
        conn.close()

    if prior is None:
        _deny("PRIOR_FAILURE_EVIDENCE_MISSING")
    if prior["retry_safe"]:
        return {
            "created": False,
            "human_input_event_id": event_id,
            "failure_evidence_id": prior["failure_evidence_id"],
            "basis": "LATEST_EVIDENCE_ALREADY_RETRY_SAFE",
        }
    if (
        prior["failure_class"] != "UNCLASSIFIED_WAKING_EXCEPTION"
        or prior["detail"] != _EXCEPTION_DETAIL
        or "conversation_direction.ConversationDirectionFailure" not in prior["basis"]
        or repr(_FAILURE_CODE) not in prior["basis"]
    ):
        _deny("PRIOR_EVIDENCE_NOT_EXACT_WORKSPACE_CONFLICT")

    diagnostics = [
        item for item in _load_jsonl(diagnostics_path)
        if item.get("human_input_event_id") == event_id
    ]
    if len(diagnostics) != 1:
        _deny("EXACT_UI_DIAGNOSTIC_NOT_UNIQUE")
    diagnostic = diagnostics[0]
    _validate_diagnostic(diagnostic, event_id)

    traces = [
        item for item in _load_jsonl(trace_path)
        if item.get("session_id") == diagnostic["session_id"]
        and item.get("pass1_status") == _FAILURE_CODE
    ]
    if len(traces) != 1:
        _deny("EXACT_DIRECTION_TRACE_NOT_UNIQUE")
    trace = traces[0]
    _validate_trace(trace, diagnostic["session_id"], diagnostic)

    diagnostic_sha = _record_digest(diagnostic)
    trace_sha = _record_digest(trace)
    basis = (
        f"{_BASIS_PREFIX}: prior evidence {prior['failure_evidence_id']}; UI diagnostic "
        f"sha256={diagnostic_sha} and direction trace sha256={trace_sha} agree on session "
        f"{diagnostic['session_id']}, a completed ordinary Pass 1 followed by validated "
        "use_workspace cross-validation failure, with unchanged working/direction state "
        "and no Workspace action, Pass 2, staging id, or canonical X. This branch "
        "structurally precedes every durable external effect and is retry-whole safe only "
        "through the existing explicit one-attempt WTR0 same-H path."
    )
    detail = json.dumps({
        "kind": _BASIS_PREFIX,
        "prior_failure_evidence_id": prior["failure_evidence_id"],
        "session_id": diagnostic["session_id"],
        "ui_diagnostic_sha256": diagnostic_sha,
        "direction_trace_id": trace.get("trace_id"),
        "direction_trace_sha256": trace_sha,
    }, sort_keys=True, separators=(",", ":"))
    # Re-check canonical state and the expected prior row while holding the
    # database's write lock, then append in that SAME transaction. X creation,
    # recovery reservation, or another evidence correction cannot race between
    # the authoritative re-check and this row.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        if not _authenticated_h_exists(conn, event_id, actor_id):
            _deny("AUTHENTICATED_H_NOT_FOUND")
        if _has_linked_x(conn, event_id):
            _deny("X_ALREADY_LINKED")
        if conn.execute(
            "SELECT 1 FROM wtr0_recovery WHERE human_input_event_id=? LIMIT 1", (event_id,)
        ).fetchone() is not None:
            _deny("RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
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
        evidence_id = record_waking_failure_in_transaction(
            conn,
            human_input_event_id=event_id,
            failure_class="WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
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
        "session_id": diagnostic["session_id"],
        "ui_diagnostic_sha256": diagnostic_sha,
        "direction_trace_sha256": trace_sha,
    }
