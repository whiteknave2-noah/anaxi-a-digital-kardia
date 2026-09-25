"""WTR0: Waking Turn Recovery Foundation -- eligibility assessment,
durable one-attempt reservation, and recovery execution.

Frozen semantics this module implements (see the WTR0 contract,
`.anaxi-dev/contracts/wtr0_waking_turn_recovery.md`, for the complete
owner specification):

  1. The original H is canonical -- never copied, never replaced. Every
     read of H's text here goes through `_load_canonical_human_input()`,
     the same predicate resume_human_input.read_unanswered_input()
     already uses (authenticated, authored by the exact actor,
     component_kind='human_conversational_input', sequence=0).

  2. No-X is necessary but not sufficient. ELIGIBLE requires H, no X,
     an explicit qualifying (retry_safe=True) waking_failure_evidence
     row, and no existing wtr0_recovery row for H.

  3/5. Three-valued eligibility outcome -- ELIGIBLE, INELIGIBLE,
     NOT_ESTABLISHED -- never collapsed into a single boolean. See
     `assess_eligibility()`'s own docstring for exactly which
     condition produces which outcome.

  4. Independent trigger only: nothing in this module is invoked by
     the failed waking pathway itself. The only callers are
     wtr0_cli.py (an explicit operator command) and this module's own
     test suite. No daemon, scheduler, poll loop, or heartbeat exists
     anywhere in this file.

  6/7. Exactly one recovery attempt per H, enforced at the SQLite
     storage level (wtr0_recovery.human_input_event_id UNIQUE) plus a
     fail-closed immediate re-check inside the SAME BEGIN IMMEDIATE
     transaction as the reservation INSERT -- see `reserve_recovery()`.
     Once that INSERT commits, the slot is permanently consumed
     regardless of what happens afterward; nothing in this module ever
     deletes or updates a wtr0_recovery row to "free" it.

  8/9. Cold reset (wtr0_cold_reset.cold_reset_waking_inference_path)
     must return SUCCEEDED before generation is attempted at all.
     FAILED or UNKNOWN both stop the attempt before any generation
     call -- see `execute_recovery()`.

  10/11/12/13/14. Generation runs through the caller-supplied
     `generation_fn`, which for real use is a thin wrapper around the
     EXISTING lawful llama_anaxi.run_waking_turn(..., existing_human_
     input_event_id=...) contract (see wtr0_cli.py) -- the same
     mechanism resume_human_input.py already uses for continuation.
     A provider response only becomes canonical X via that existing
     lawful persistence pathway; this module never persists X itself
     and never promotes prior unpersisted text. SUCCESS additionally
     requires that exact X to contain substantive Clark prose and pass
     the authenticated canonical GUI projection for H's principal/scope.
"""
import sqlite3
import time

import conversation_projection
from native_provenance_writer import generate_native_ulid
from waking_failure_evidence import latest_failure_evidence
from wtr0_cold_reset import ColdResetOutcome


class EligibilityDecision:
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class RecoveryDenied(Exception):
    """Raised by execute_recovery() when the fail-closed re-check
    immediately before reservation finds the attempt is not eligible.
    Carries the eligibility receipt that produced the denial."""

    def __init__(self, receipt):
        super().__init__(f"WTR0 recovery denied: {receipt['decision']} ({receipt['basis']})")
        self.receipt = receipt


def _load_canonical_human_input(conn, human_input_event_id, actor_id):
    """Read-only. Same predicate as resume_human_input.
    read_unanswered_input(): a genuine, authenticated human_waking_input
    event authored by exactly this actor. Returns the exact canonical
    prompt text, or None if it does not resolve."""
    row = conn.execute(
        "SELECT ec.component_text FROM events e JOIN event_components ec ON ec.event_id = e.event_id "
        "JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ? AND e.event_type = 'human_waking_input' AND ec.sequence = 0 "
        "AND ec.component_kind = 'human_conversational_input' AND ec.creator_actor_id = ? "
        "AND a.auth_state = 'authenticated' AND a.authenticated_actor_id = ec.creator_actor_id",
        (human_input_event_id, actor_id),
    ).fetchone()
    return row[0] if row is not None else None


def canonical_human_input_visibility_scope(conn, human_input_event_id, actor_id):
    """Return H's frozen FS1 scope (``None`` for genuine pre-FS1 H).

    Raises ``ValueError`` when H does not establish the exact authenticated
    actor. Recovery callers use this value to bind their fresh execution
    session without widening, narrowing, or dropping H's original scope.
    """
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
    event_scope = "e.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    auth_scope = "a.visibility_scope" if "visibility_scope" in auth_columns else "NULL"
    rows = conn.execute(
        "SELECT ec.creator_actor_id, a.auth_state, a.authenticated_actor_id, "
        f"{event_scope}, {auth_scope} "
        "FROM events e JOIN event_components ec ON ec.event_id=e.event_id "
        "JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
        "WHERE e.event_id=? AND e.event_type='human_waking_input' "
        "AND ec.sequence=0 AND ec.component_kind='human_conversational_input'",
        (human_input_event_id,),
    ).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] != actor_id
        or rows[0][1] != "authenticated"
        or rows[0][2] != actor_id
        or rows[0][3] != rows[0][4]
    ):
        raise ValueError("canonical H does not establish the requested authenticated actor/scope")
    return rows[0][3]


def _linked_canonical_x_event_ids(conn, human_input_event_id):
    rows = conn.execute(
        "SELECT e.event_id FROM event_components c JOIN events e ON e.event_id = c.event_id "
        "WHERE c.component_kind = 'human_input_event_id' AND c.component_text = ? "
        "AND e.event_type = 'waking_turn' ORDER BY c.component_id",
        (human_input_event_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _linked_canonical_x_event_id(conn, human_input_event_id):
    """Compatibility helper for eligibility/status callers.

    Any linked X closes recovery eligibility.  Execution success uses the
    cardinality-preserving helper above and requires exactly one exact match.
    """
    event_ids = _linked_canonical_x_event_ids(conn, human_input_event_id)
    return event_ids[0] if event_ids else None


def _existing_recovery_row(conn, human_input_event_id):
    return conn.execute(
        "SELECT recovery_id, terminal_state FROM wtr0_recovery WHERE human_input_event_id = ?",
        (human_input_event_id,),
    ).fetchone()


def assess_eligibility(conn, human_input_event_id, actor_id):
    """Read-only mechanical eligibility assessment. Returns a dict:
        {"decision": ELIGIBLE|INELIGIBLE|NOT_ESTABLISHED, "basis": str,
         "human_input_event_id": ..., "canonical_x_event_id": str|None,
         "failure_evidence_id": str|None}

    Decision table (checked in this exact order -- see class docstring
    for the frozen semantics each branch implements):
      1. H does not resolve canonically for this actor -> INELIGIBLE
         (basis="H_NOT_FOUND"). Never NOT_ESTABLISHED: a nonexistent
         or foreign H is a definite negative, not an open question.
      2. H already has a linked canonical X -> INELIGIBLE
         (basis="X_ALREADY_LINKED"). Also closes recovery permanently
         for an H that succeeded independently of WTR0.
      3. A wtr0_recovery row already exists for H (reserved or
         consumed, any terminal_state) -> INELIGIBLE
         (basis="RECOVERY_ALREADY_RESERVED_OR_CONSUMED").
      4. No waking_failure_evidence row exists for H at all ->
         NOT_ESTABLISHED (basis="NO_FAILURE_EVIDENCE"). Unanswered
         H alone is never sufficient; absence of evidence is an open
         question, not a negative.
      5. The most recent failure evidence row is not retry_safe ->
         INELIGIBLE (basis="FAILURE_NOT_RETRY_SAFE"). Known-bad,
         never "unknown."
      6. Otherwise -> ELIGIBLE, referencing the qualifying
         failure_evidence_id."""
    prompt = _load_canonical_human_input(conn, human_input_event_id, actor_id)
    if prompt is None:
        return {
            "decision": EligibilityDecision.INELIGIBLE, "basis": "H_NOT_FOUND",
            "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
            "failure_evidence_id": None,
        }

    x_event_id = _linked_canonical_x_event_id(conn, human_input_event_id)
    if x_event_id is not None:
        return {
            "decision": EligibilityDecision.INELIGIBLE, "basis": "X_ALREADY_LINKED",
            "human_input_event_id": human_input_event_id, "canonical_x_event_id": x_event_id,
            "failure_evidence_id": None,
        }

    existing_recovery = _existing_recovery_row(conn, human_input_event_id)
    if existing_recovery is not None:
        return {
            "decision": EligibilityDecision.INELIGIBLE, "basis": "RECOVERY_ALREADY_RESERVED_OR_CONSUMED",
            "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
            "failure_evidence_id": None, "existing_recovery_id": existing_recovery[0],
            "existing_terminal_state": existing_recovery[1],
        }

    evidence = latest_failure_evidence(conn, human_input_event_id)
    if evidence is None:
        return {
            "decision": EligibilityDecision.NOT_ESTABLISHED, "basis": "NO_FAILURE_EVIDENCE",
            "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
            "failure_evidence_id": None,
        }

    if not evidence["retry_safe"]:
        return {
            "decision": EligibilityDecision.INELIGIBLE, "basis": "FAILURE_NOT_RETRY_SAFE",
            "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
            "failure_evidence_id": evidence["failure_evidence_id"],
            "failure_class": evidence["failure_class"],
        }

    return {
        "decision": EligibilityDecision.ELIGIBLE, "basis": "QUALIFYING_RETRY_SAFE_FAILURE",
        "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
        "failure_evidence_id": evidence["failure_evidence_id"],
        "failure_class": evidence["failure_class"],
    }


def list_recovery_candidates(conn, actor_id):
    """Read-only newest-first eligibility receipts for every evidenced H.

    There is intentionally no hidden top-N cap: every canonically evidenced H
    must remain visible to the owner-facing recovery surface, including older
    eligible items.
    """
    rows = conn.execute(
        "SELECT human_input_event_id, MAX(rowid) AS newest_rowid "
        "FROM waking_failure_evidence GROUP BY human_input_event_id "
        "ORDER BY newest_rowid DESC",
    ).fetchall()
    return [assess_eligibility(conn, row[0], actor_id) for row in rows]


def reserve_recovery(db_path, human_input_event_id, actor_id):
    """Fail-closed reservation: opens its OWN connection/transaction,
    re-runs assess_eligibility() fresh inside a BEGIN IMMEDIATE write
    transaction (so nothing can change canonical state or the
    wtr0_recovery table between the re-check and the INSERT for this
    connection's view), and either commits exactly one new
    wtr0_recovery row or raises RecoveryDenied without writing
    anything. The UNIQUE constraint on human_input_event_id is the
    final backstop against a concurrent second writer racing this
    same re-check-then-insert sequence -- caught below as
    sqlite3.IntegrityError and reported as a denial, never retried.

    Returns the new recovery_id on success."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        receipt = assess_eligibility(conn, human_input_event_id, actor_id)
        if receipt["decision"] != EligibilityDecision.ELIGIBLE:
            conn.execute("ROLLBACK")
            raise RecoveryDenied(receipt)
        recovery_id = generate_native_ulid()
        now = int(time.time())
        try:
            conn.execute(
                "INSERT INTO wtr0_recovery "
                "(recovery_id, human_input_event_id, failure_evidence_id, eligibility_decision, "
                " eligibility_basis, reserved_at, terminal_state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?)",
                (recovery_id, human_input_event_id, receipt["failure_evidence_id"],
                 receipt["decision"], receipt["basis"], now, now),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise RecoveryDenied({
                "decision": EligibilityDecision.INELIGIBLE,
                "basis": "RECOVERY_ALREADY_RESERVED_OR_CONSUMED_RACE",
                "human_input_event_id": human_input_event_id,
            })
        conn.execute("COMMIT")
        return recovery_id
    finally:
        conn.close()


def _update_recovery(conn, recovery_id, **fields):
    fields["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE wtr0_recovery SET {set_clause} WHERE recovery_id = ?",
                 (*fields.values(), recovery_id))
    conn.commit()


def execute_recovery(db_path, human_input_event_id, actor_id, *, reset_fn, generation_fn):
    """The full fail-closed recovery sequence:

        reserve (fresh re-check + durable INSERT, one transaction)
        -> cold reset (must positively SUCCEED)
        -> generation (existing lawful pathway; produces canonical X)
        -> authenticated canonical GUI-projection proof
        -> record terminal outcome

    `reset_fn` takes no arguments and returns a dict shaped like
    wtr0_cold_reset.cold_reset_waking_inference_path()'s own return
    value. `generation_fn` takes the canonical `prompt` (loaded fresh
    from H, never caller-supplied) and returns a dict with at least a
    "native_event_id" key on success (matching llama_anaxi.
    run_waking_turn()'s own result shape), or raises.

    Returns a dict describing the terminal outcome. Never raises for
    an ordinary post-reservation failure (reset/generation) -- those
    are recorded as terminal states and returned normally. Raises
    RecoveryDenied only if reservation itself is refused (no
    consequential action of any kind was taken)."""
    recovery_id = reserve_recovery(db_path, human_input_event_id, actor_id)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        prompt = _load_canonical_human_input(conn, human_input_event_id, actor_id)
        # Reservation only just succeeded under its own fresh re-check
        # (H existed, no X, retry-safe evidence, no prior recovery);
        # `prompt` cannot legitimately be None here. If it ever were,
        # fail closed rather than proceed with no canonical text.
        if prompt is None:
            _update_recovery(conn, recovery_id, terminal_state="ANOMALY_H_UNREADABLE_AFTER_RESERVATION")
            return {"recovery_id": recovery_id, "terminal_state": "ANOMALY_H_UNREADABLE_AFTER_RESERVATION"}

        reset_result = reset_fn()
        _update_recovery(
            conn, recovery_id, reset_status=reset_result["status"],
            reset_detail=reset_result.get("basis"), reset_at=int(time.time()),
        )
        if reset_result["status"] != ColdResetOutcome.SUCCEEDED:
            _update_recovery(conn, recovery_id, terminal_state="RESET_FAILED")
            return {"recovery_id": recovery_id, "terminal_state": "RESET_FAILED", "reset_result": reset_result}

        try:
            gen_result = generation_fn(prompt)
        except Exception as exc:
            _update_recovery(
                conn, recovery_id, generation_status="FAILED",
                generation_detail=repr(exc), generation_at=int(time.time()),
                terminal_state="GENERATION_FAILED",
            )
            return {"recovery_id": recovery_id, "terminal_state": "GENERATION_FAILED", "error": repr(exc)}

        x_event_id = gen_result.get("native_event_id") if isinstance(gen_result, dict) else None
        _update_recovery(
            conn, recovery_id, generation_status="SUCCEEDED", generation_at=int(time.time()),
        )
        if not x_event_id:
            _update_recovery(conn, recovery_id, persistence_status="FAILED",
                              terminal_state="PERSISTENCE_FAILED")
            return {"recovery_id": recovery_id, "terminal_state": "PERSISTENCE_FAILED"}

        # A row named X is not delivery. Require exactly one exact H->X link,
        # substantive Clark prose, matching pipeline/auth/FS1 metadata, and
        # successful placement through the same authenticated canonical
        # projection the ordinary GUI loads. No index or second projection
        # store exists; this is a fresh read of the committed transaction.
        linked_xs = _linked_canonical_x_event_ids(conn, human_input_event_id)
        if linked_xs != [x_event_id]:
            detail = (
                f"returned event {x_event_id!r} is not the one unique canonical X "
                f"linked to H; linked_x_event_ids={linked_xs!r}"
            )
            _update_recovery(
                conn, recovery_id, persistence_status="UNCONFIRMED",
                persistence_detail=detail, terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            return {
                "recovery_id": recovery_id, "terminal_state": "PERSISTENCE_UNCONFIRMED",
                "error": detail,
            }
        confirmed_x = linked_xs[0]
        try:
            projection_receipt = conversation_projection.verify_recovered_exchange_projection(
                conn, human_input_event_id, confirmed_x, actor_id,
            )
        except Exception as exc:
            detail = (
                str(exc) if isinstance(exc, conversation_projection.ProjectionIntegrityError)
                else f"projection verification error: {type(exc).__name__}: {exc}"
            )
            _update_recovery(
                conn, recovery_id, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=confirmed_x,
                terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            return {
                "recovery_id": recovery_id, "terminal_state": "PERSISTENCE_UNCONFIRMED",
                "canonical_x_event_id": confirmed_x, "error": detail,
            }

        subject_null = projection_receipt.get("reply_choice") == "no_reply"
        _update_recovery(
            conn, recovery_id, persistence_status="SUCCEEDED", canonical_x_event_id=confirmed_x,
            persistence_detail=(
                # LAWFUL NULL: Clark's own typed choice not to reply answers H exactly like prose does;
                # the recovery completed (it is never re-offered: H now has its X).
                "Clark's canonical typed no_reply choice visible in authenticated canonical GUI projection"
                if subject_null else
                "substantive Clark prose visible in authenticated canonical GUI projection"),
            terminal_state="SUCCESS",
        )
        return {
            "recovery_id": recovery_id, "terminal_state": "SUCCESS",
            "canonical_x_event_id": confirmed_x,
            "projection_index": projection_receipt["projected_index"],
            "reply_choice": projection_receipt.get("reply_choice"),
        }
    finally:
        conn.close()


def recovery_status(conn, human_input_event_id):
    """Read-only inspection of the wtr0_recovery row for one H, or
    None if no recovery was ever reserved."""
    row = conn.execute(
        "SELECT recovery_id, human_input_event_id, failure_evidence_id, eligibility_decision, "
        "eligibility_basis, reserved_at, reset_status, reset_detail, reset_at, "
        "generation_status, generation_detail, generation_at, persistence_status, "
        "persistence_detail, canonical_x_event_id, terminal_state, updated_at "
        "FROM wtr0_recovery WHERE human_input_event_id = ?",
        (human_input_event_id,),
    ).fetchone()
    if row is None:
        return None
    cols = ["recovery_id", "human_input_event_id", "failure_evidence_id", "eligibility_decision",
            "eligibility_basis", "reserved_at", "reset_status", "reset_detail", "reset_at",
            "generation_status", "generation_detail", "generation_at", "persistence_status",
            "persistence_detail", "canonical_x_event_id", "terminal_state", "updated_at"]
    return dict(zip(cols, row))
