"""WTR0: prospective, bounded waking-failure evidence writer.

This module records a MECHANICAL classification of a waking-attempt
failure, tied to exactly one canonical human_waking_input event id
(H). It never infers failure from silence/unanswered H and never
synthesizes evidence for a historical/production turn without positive,
attempt-bound durable evidence,
and never lets a caller assert retry-safety directly -- retry_safe is
looked up from the fixed FAILURE_CLASSES allowlist below, keyed by
failure_class, so "guessing" a class into retry-safety is impossible.

The allowlist is deliberately small and derived from the actual
current failure surfaces this repository's native waking write path
(llama_anaxi.run_waking_turn -> native_provenance_writer.
stage_and_record_native_waking_turn -> native_turn_staging.
append_staging_entry) already distinguishes in its own code and
docstrings -- not a guess at hypothetical future failure modes:

  WAKING_EXECUTION_FAILURE_NO_X_PERSISTED (retry_safe=True)
      Any exception propagated out of an instrumented waking call
      (see waking_turn_failure_capture.py) where a fresh canonical
      read POSITIVELY confirms the named H still has no linked
      canonical X afterward. This covers ordinary model/provider
      execution failure and a durably-rolled-back staging failure
      (native_turn_staging.StagingDurabilityError, whose own
      docstring already states retry-whole is safe) -- both leave
      H uncommitted-to-any-X, so nothing durable needs to be undone
      before a single retry.

  AMBIGUOUS_PERSISTENCE_OUTCOME (retry_safe=False)
      native_turn_staging.StagingIndeterminateStateError was raised --
      that exception's own docstring states retry-whole is explicitly
      FORBIDDEN until an operator inspects the raw staging file. WTR0
      must never treat this the same as a plain retry-safe failure.

  POST_PERSISTENCE_EXCEPTION_X_EXISTS (retry_safe=False)
      An exception propagated out of an instrumented waking call, but
      a fresh canonical read shows a linked X now exists for H anyway
      (e.g. a failure in best-effort post-persistence bookkeeping,
      after the canonical transaction already committed). H already
      has a canonical reply; WTR0 must never authorize a second one.

  UNCLASSIFIED_WAKING_EXCEPTION (retry_safe=False)
      WTR0-CORRECTION-1: any exception that escapes an instrumented
      waking call whose TYPE is not one of the small set this module
      can positively prove is retry-safe (see
      waking_turn_failure_capture.py's own classification, which is
      the sole caller entitled to name this class) -- e.g. an
      arbitrary/unrecognized exception, a bug in prompt/context
      construction, a genuinely unknown provider failure, or a
      ConversationDirectionFailure whose failure_code is not
      BUDGET_EXCEEDED. The mechanical failure boundary this repository
      currently has (an outer try/except around the entire
      run_waking_turn() call) cannot truthfully distinguish "ordinary
      transient provider hiccup" from "an unrelated programming bug"
      by exception type alone; per the WTR0 contract's own instruction
      not to guess, this class is recorded (so the failure is never
      silently swallowed) but is fail-closed non-retry-safe. It is
      still recorded, not discarded, so an operator has a mechanical
      trail even though WTR0 will never auto-permit recovery from it.

The sole evidence-bound correction surface for an older conservatively
classified Workspace conflict is wtr0_workspace_conflict_reassessment.py.
It appends rather than edits evidence and requires canonical state, the outer
UI diagnostic, and the direction trace to agree on the same pre-action typed
failure. It cannot derive evidence from silence or from exception prose alone.

No other failure_class may be recorded -- record_waking_failure()
rejects anything not in FAILURE_CLASSES, fail-closed, so an unbounded
or newly-invented class can never silently acquire retry-safety.
"""
import sqlite3
import time

from native_provenance_writer import generate_native_ulid

# failure_class -> retry_safe (bool). This IS the sole authority on
# retry-safety -- never a caller-supplied flag.
FAILURE_CLASSES = {
    "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED": True,
    "AMBIGUOUS_PERSISTENCE_OUTCOME": False,
    "POST_PERSISTENCE_EXCEPTION_X_EXISTS": False,
    "UNCLASSIFIED_WAKING_EXCEPTION": False,
}

RETRY_SAFE_FAILURE_CLASSES = frozenset(
    cls for cls, safe in FAILURE_CLASSES.items() if safe
)


class UnknownFailureClassError(Exception):
    """Raised when a caller passes a failure_class outside the fixed
    allowlist above. Never silently coerced to a known class."""


def record_waking_failure_in_transaction(
    conn, *, human_input_event_id, failure_class, basis, detail=None,
    occurred_at=None, recorded_at=None,
):
    """Append one evidence row inside the caller's active transaction.

    This is the transaction-preserving form used by the evidence-bound
    historical Workspace-conflict correction.  The caller can therefore
    re-check canonical H/X/recovery state and append the correction under one
    ``BEGIN IMMEDIATE`` lock, rather than opening a race between validation and
    insertion.  It deliberately performs the same allowlist, identity, basis,
    retry-safety, and rowid-exhaustion checks as :func:`record_waking_failure`.

    The connection must already be in a transaction; this function never
    commits or rolls back on the caller's behalf.
    """
    if failure_class not in FAILURE_CLASSES:
        raise UnknownFailureClassError(
            f"failure_class={failure_class!r} is not in the fixed WTR0 allowlist "
            f"{sorted(FAILURE_CLASSES)}; refusing to record an unbounded classification."
        )
    if not human_input_event_id:
        raise ValueError("human_input_event_id is required")
    if not basis or not str(basis).strip():
        raise ValueError("basis is required -- mechanical reason for this classification")
    if not conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "record_waking_failure_in_transaction requires an active transaction"
        )

    retry_safe = FAILURE_CLASSES[failure_class]
    failure_evidence_id = generate_native_ulid()
    if recorded_at is None:
        recorded_at = int(time.time())
    maximum = conn.execute("SELECT MAX(rowid) FROM waking_failure_evidence").fetchone()[0]
    if maximum is not None and maximum >= 9223372036854775807:
        raise sqlite3.DatabaseError("failure-evidence insertion sequence exhausted")
    conn.execute(
        "INSERT INTO waking_failure_evidence "
        "(failure_evidence_id, human_input_event_id, failure_class, retry_safe, basis, "
        " detail, occurred_at, occurred_at_unavailable, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            failure_evidence_id, human_input_event_id, failure_class, int(retry_safe), basis,
            detail, occurred_at, 0 if occurred_at is not None else 1, recorded_at,
        ),
    )
    return failure_evidence_id


def record_waking_failure(
    db_path, *, human_input_event_id, failure_class, basis, detail=None, occurred_at=None,
):
    """Insert one waking_failure_evidence row. `occurred_at`, when
    given, is the ACTUAL occurrence time of the failed attempt (never
    invented) -- omitted (None) means genuinely unavailable, recorded
    as such (occurred_at_unavailable=1) rather than backdated to
    "now." `recorded_at` (this insert's own wall-clock time) is always
    the discovery/recording time, kept structurally distinct from
    `occurred_at` per occurrence-vs-observation semantics.

    Returns the new failure_evidence_id. Raises UnknownFailureClassError
    (before any write) if failure_class is not in FAILURE_CLASSES --
    retry_safe is derived from the allowlist, never accepted from the
    caller."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        # This ordinary rowid table is append-only through the local writer.
        # SQLite serializes writers: allocate the rowid and commit while holding
        # the same write transaction. Refuse exhaustion rather than let SQLite
        # fall back to random rowid allocation at the signed 64-bit ceiling.
        conn.execute("BEGIN IMMEDIATE")
        failure_evidence_id = record_waking_failure_in_transaction(
            conn,
            human_input_event_id=human_input_event_id,
            failure_class=failure_class,
            basis=basis,
            detail=detail,
            occurred_at=occurred_at,
        )
        conn.commit()
    finally:
        conn.close()
    return failure_evidence_id


def latest_failure_evidence(conn, human_input_event_id):
    """Latest durably INSERTED evidence for H, not latest occurrence.

    The local schema is an ordinary rowid table (TEXT primary key, no WITHOUT
    ROWID). The sole local writer appends with SQLite-allocated increasing
    rowids under a serialized write transaction; no local WTR0 path deletes,
    copies, imports, or reconstructs these rows. Rollback contributes no durable
    record. Reopening a database/process preserves this order. Timestamps are
    observation metadata; occurred_at may be absent/backdated, and ULID random
    tails cannot order same-millisecond records.

    This contract is limited to this append-only recording path and physical
    database copies that preserve rowids. Unaliased rowids are not an occurrence
    clock or a portable sequence across arbitrary SQL export/import, table
    reconstruction, or maintenance that renumbers rows. Such datasets require
    separate chronology establishment before use for eligibility. No historical
    rows are rewritten or repaired here. Exhausted allocation fails closed.
    """
    row = conn.execute(
        "SELECT failure_evidence_id, failure_class, retry_safe, basis, detail, "
        "occurred_at, occurred_at_unavailable, recorded_at "
        "FROM waking_failure_evidence WHERE human_input_event_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (human_input_event_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "failure_evidence_id": row[0],
        "failure_class": row[1],
        "retry_safe": bool(row[2]),
        "basis": row[3],
        "detail": row[4],
        "occurred_at": row[5],
        "occurred_at_unavailable": bool(row[6]),
        "recorded_at": row[7],
    }
