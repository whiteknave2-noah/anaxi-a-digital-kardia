"""Explicit owner/UI boundary for one WTR0 same-H recovery attempt."""

import sqlite3

import wtr0_waking_recovery as recovery


def list_candidates(db_path, actor_id):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return recovery.list_recovery_candidates(conn, actor_id)
    finally:
        conn.close()


def execute_candidate(
    db_path, *, human_input_event_id, actor_id, confirmation,
    background_activity_running, expected_visibility_scope,
    reset_fn, generation_fn,
):
    if confirmation is not True:
        raise recovery.RecoveryDenied({
            "decision": recovery.EligibilityDecision.INELIGIBLE,
            "basis": "EXPLICIT_CONFIRMATION_REQUIRED",
            "human_input_event_id": human_input_event_id,
        })
    if background_activity_running:
        raise recovery.RecoveryDenied({
            "decision": recovery.EligibilityDecision.INELIGIBLE,
            "basis": "BACKGROUND_ACTIVITY_MUST_BE_PAUSED",
            "human_input_event_id": human_input_event_id,
        })
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        actual_scope = recovery.canonical_human_input_visibility_scope(
            conn, human_input_event_id, actor_id,
        )
    finally:
        conn.close()
    if actual_scope != expected_visibility_scope:
        raise recovery.RecoveryDenied({
            "decision": recovery.EligibilityDecision.INELIGIBLE,
            "basis": "CURRENT_BOUND_SCOPE_DOES_NOT_MATCH_H",
            "human_input_event_id": human_input_event_id,
        })
    return recovery.execute_recovery(
        db_path, human_input_event_id, actor_id,
        reset_fn=reset_fn, generation_fn=generation_fn,
    )
