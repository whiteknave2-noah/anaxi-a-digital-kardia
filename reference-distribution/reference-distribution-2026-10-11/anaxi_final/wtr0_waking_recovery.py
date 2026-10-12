"""WTR0: Waking Turn Recovery Foundation -- eligibility assessment,
durable recovery-identity reservation, and recovery execution.

Frozen semantics this module implements (see the WTR0 contract,
`.anaxi-dev/contracts/wtr0_waking_turn_recovery.md`, for the complete
owner specification):

  1. The original H is canonical -- never copied, never replaced. Every
     read of H's text here goes through `_load_canonical_human_input()`,
     the same predicate resume_human_input.read_unanswered_input()
     already uses (authenticated, authored by the exact actor,
     component_kind='human_conversational_input', sequence=0).

  2. No-X is necessary but not sufficient. Initial eligibility requires H,
     no X, qualifying failure evidence, and no existing identity. Resume
     eligibility additionally requires the exact same identity and the one
     recognized pre-staging Pass-2 completion-limit receipt.

  3/5. Three-valued eligibility outcome -- ELIGIBLE, INELIGIBLE,
     NOT_ESTABLISHED -- never collapsed into a single boolean. See
     `assess_eligibility()`'s own docstring for exactly which
     condition produces which outcome.

  4. Independent trigger only: nothing in this module is invoked by
     the failed waking pathway itself. The only callers are
     wtr0_cli.py (an explicit operator command) and this module's own
     test suite. No daemon, scheduler, poll loop, or heartbeat exists
     anywhere in this file.

  6/7. Exactly one recovery identity per H remains enforced by
     wtr0_recovery.human_input_event_id UNIQUE. Physical executions have
     ordinal receipts; at most one is ACTIVE. The only repeat is an explicit
     same-identity resume after Pass-2 COMPLETION_LIMIT_REACHED is proven to
     have occurred before staging/release/persistence ambiguity.

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
from conversation_direction import ConversationDirectionFailure
from native_provenance_writer import generate_native_ulid
from waking_failure_evidence import latest_failure_evidence
from wtr0_cold_reset import ColdResetOutcome


class EligibilityDecision:
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


# One recovery identity may execute once and, only after the exact proven
# pre-staging Pass-2 completion-limit outcome, may be resumed once.  This is a
# physical-execution ceiling, not a second recovery identity: attempt 2 keeps
# the original recovery_id and is terminal whether it succeeds or fails.
MAX_EXECUTIONS_PER_RECOVERY_IDENTITY = 2


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
        "SELECT recovery_id, terminal_state, failure_evidence_id "
        "FROM wtr0_recovery WHERE human_input_event_id = ?",
        (human_input_event_id,),
    ).fetchone()


def _latest_attempt(conn, recovery_id):
    row = conn.execute(
        "SELECT attempt_ordinal, actor_id, visibility_scope, reset_status, "
        "generation_status, failure_stage, failure_code, pre_staging_proven, "
        "subject_output_released, persistence_status, canonical_x_event_id, "
        "retry_safe_resume, terminal_state "
        "FROM wtr0_recovery_attempt WHERE recovery_id=? "
        "ORDER BY attempt_ordinal DESC LIMIT 1",
        (recovery_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "attempt_ordinal", "actor_id", "visibility_scope", "reset_status",
        "generation_status", "failure_stage", "failure_code", "pre_staging_proven",
        "subject_output_released", "persistence_status", "canonical_x_event_id",
        "retry_safe_resume", "terminal_state",
    )
    return dict(zip(keys, row))


def _assess_same_identity_resume(conn, human_input_event_id, actor_id, existing_recovery):
    """The one narrow physical re-execution law for an existing identity."""
    recovery_id, terminal_state, failure_evidence_id = existing_recovery
    attempt = _latest_attempt(conn, recovery_id)
    if attempt is None:
        basis = "RECOVERY_ATTEMPT_HISTORY_NOT_ESTABLISHED"
    elif attempt["terminal_state"] == "ACTIVE":
        basis = "RECOVERY_EXECUTION_ACTIVE"
    else:
        evidence = latest_failure_evidence(conn, human_input_event_id)
        try:
            scope = canonical_human_input_visibility_scope(conn, human_input_event_id, actor_id)
        except ValueError:
            scope = object()
        exact_safe_failure = (
            attempt["attempt_ordinal"] < MAX_EXECUTIONS_PER_RECOVERY_IDENTITY
            and terminal_state == "GENERATION_FAILED"
            and attempt["terminal_state"] == "GENERATION_FAILED"
            and attempt["reset_status"] == "SUCCEEDED"
            and attempt["generation_status"] == "FAILED"
            and attempt["failure_stage"] == "pass2"
            and attempt["failure_code"] == "COMPLETION_LIMIT_REACHED"
            and attempt["pre_staging_proven"] == 1
            and attempt["subject_output_released"] == 0
            and attempt["persistence_status"] == "not_attempted"
            and attempt["canonical_x_event_id"] is None
            and attempt["retry_safe_resume"] == 1
            and attempt["actor_id"] == actor_id
            and attempt["visibility_scope"] == scope
            and evidence is not None
            and evidence["failure_evidence_id"] == failure_evidence_id
            and evidence["retry_safe"] is True
        )
        if exact_safe_failure:
            return {
                "decision": EligibilityDecision.ELIGIBLE,
                "basis": "RETRY_SAFE_SAME_IDENTITY_RESUME",
                "human_input_event_id": human_input_event_id,
                "canonical_x_event_id": None,
                "failure_evidence_id": failure_evidence_id,
                "failure_class": evidence["failure_class"],
                "existing_recovery_id": recovery_id,
                "previous_attempt_ordinal": attempt["attempt_ordinal"],
                "next_attempt_ordinal": attempt["attempt_ordinal"] + 1,
            }
        basis = (
            "RECOVERY_EXECUTION_CEILING_REACHED"
            if attempt["attempt_ordinal"] >= MAX_EXECUTIONS_PER_RECOVERY_IDENTITY
            else "RECOVERY_ALREADY_RESERVED_OR_CONSUMED"
        )
    return {
        "decision": EligibilityDecision.INELIGIBLE, "basis": basis,
        "human_input_event_id": human_input_event_id, "canonical_x_event_id": None,
        "failure_evidence_id": failure_evidence_id,
        "existing_recovery_id": recovery_id, "existing_terminal_state": terminal_state,
    }


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
      3. An existing identity is delegated to the narrow resume assessor.
         Only its latest terminal attempt's exact Pass-2 completion-limit,
         pre-staging/no-release/no-persistence receipt can be ELIGIBLE;
         ACTIVE, ambiguous, successful, and every other failure stay closed.
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
        return _assess_same_identity_resume(
            conn, human_input_event_id, actor_id, existing_recovery,
        )

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

    Returns the durable recovery_id on success. Initial execution creates the
    identity and attempt 1; a narrowly eligible resume preserves the identity
    and appends the next physical attempt receipt."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        receipt = assess_eligibility(conn, human_input_event_id, actor_id)
        if receipt["decision"] != EligibilityDecision.ELIGIBLE:
            conn.execute("ROLLBACK")
            raise RecoveryDenied(receipt)
        now = int(time.time())
        scope = canonical_human_input_visibility_scope(conn, human_input_event_id, actor_id)
        recovery_id = receipt.get("existing_recovery_id")
        attempt_ordinal = receipt.get("next_attempt_ordinal", 1)
        try:
            if recovery_id is None:
                recovery_id = generate_native_ulid()
                conn.execute(
                    "INSERT INTO wtr0_recovery "
                    "(recovery_id, human_input_event_id, failure_evidence_id, eligibility_decision, "
                    " eligibility_basis, reserved_at, terminal_state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?)",
                    (recovery_id, human_input_event_id, receipt["failure_evidence_id"],
                     receipt["decision"], receipt["basis"], now, now),
                )
            conn.execute(
                "INSERT INTO wtr0_recovery_attempt "
                "(recovery_id, attempt_ordinal, actor_id, visibility_scope, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (recovery_id, attempt_ordinal, actor_id, scope, now, now),
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


def _update_attempt(conn, recovery_id, attempt_ordinal, **fields):
    fields["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE wtr0_recovery_attempt SET {set_clause} "
        "WHERE recovery_id = ? AND attempt_ordinal = ?",
        (*fields.values(), recovery_id, attempt_ordinal),
    )
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
    from H, never caller-supplied) plus the exact durable `recovery_id`.
    The production closure passes both through to llama_anaxi.
    run_waking_turn(), allowing canonical persistence to prove this is
    the reserved recovery path rather than an ordinary caller asking
    for cross-session relaxation. It returns a dict with at least a
    "native_event_id" key on success, or raises.

    Returns a dict describing the terminal outcome. Never raises for
    an ordinary post-reservation failure (reset/generation) -- those
    are recorded as terminal states and returned normally. Raises
    RecoveryDenied only if reservation itself is refused (no
    consequential action of any kind was taken)."""
    recovery_id = reserve_recovery(db_path, human_input_event_id, actor_id)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        active_attempt = conn.execute(
            "SELECT attempt_ordinal FROM wtr0_recovery_attempt "
            "WHERE recovery_id=? AND terminal_state='ACTIVE'",
            (recovery_id,),
        ).fetchall()
        if len(active_attempt) != 1:
            _update_recovery(conn, recovery_id, terminal_state="ANOMALY_ACTIVE_ATTEMPT_UNESTABLISHED")
            return {
                "recovery_id": recovery_id,
                "terminal_state": "ANOMALY_ACTIVE_ATTEMPT_UNESTABLISHED",
            }
        attempt_ordinal = active_attempt[0][0]
        prompt = _load_canonical_human_input(conn, human_input_event_id, actor_id)
        # Reservation only just succeeded under its own fresh re-check
        # (H existed, no X, retry-safe evidence, no prior recovery);
        # `prompt` cannot legitimately be None here. If it ever were,
        # fail closed rather than proceed with no canonical text.
        if prompt is None:
            _update_attempt(
                conn, recovery_id, attempt_ordinal,
                terminal_state="ANOMALY_H_UNREADABLE_AFTER_RESERVATION",
            )
            _update_recovery(conn, recovery_id, terminal_state="ANOMALY_H_UNREADABLE_AFTER_RESERVATION")
            return {"recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                    "terminal_state": "ANOMALY_H_UNREADABLE_AFTER_RESERVATION"}

        reset_result = reset_fn()
        _update_attempt(
            conn, recovery_id, attempt_ordinal, reset_status=reset_result["status"],
            reset_detail=reset_result.get("basis"), reset_at=int(time.time()),
        )
        _update_recovery(
            conn, recovery_id, reset_status=reset_result["status"],
            reset_detail=reset_result.get("basis"), reset_at=int(time.time()),
        )
        if reset_result["status"] != ColdResetOutcome.SUCCEEDED:
            _update_attempt(
                conn, recovery_id, attempt_ordinal,
                terminal_state="RESET_FAILED", retry_safe_resume=0,
            )
            _update_recovery(conn, recovery_id, terminal_state="RESET_FAILED")
            return {"recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                    "terminal_state": "RESET_FAILED", "reset_result": reset_result}

        try:
            gen_result = generation_fn(prompt, recovery_id)
        except Exception as exc:
            linked_x = _linked_canonical_x_event_id(conn, human_input_event_id)
            # This is intentionally narrower than waking failure capture's
            # broader first-recovery allowlist. Ordinary Pass-2 completion
            # rejection is inside run_waking_turn before native staging and
            # before every deferred outward mutation. A workspace action uses
            # stage="workspace_pass2" and cannot enter here; pre-Pass-2 public
            # evidence lookup is read-only. No other GENERATION_FAILED class
            # inherits resumability.
            exact_retry_safe_limit = (
                linked_x is None
                and type(exc) is ConversationDirectionFailure
                and exc.stage == "pass2"
                and exc.failure_code == "COMPLETION_LIMIT_REACHED"
            )
            retry_safe_resume = (
                exact_retry_safe_limit
                and attempt_ordinal < MAX_EXECUTIONS_PER_RECOVERY_IDENTITY
            )
            _update_attempt(
                conn, recovery_id, attempt_ordinal,
                generation_status="FAILED", generation_detail=repr(exc),
                generation_at=int(time.time()),
                failure_stage=exc.stage if type(exc) is ConversationDirectionFailure else None,
                failure_code=exc.failure_code if type(exc) is ConversationDirectionFailure else None,
                pre_staging_proven=1 if exact_retry_safe_limit else None,
                subject_output_released=0 if exact_retry_safe_limit else None,
                canonical_x_event_id=linked_x,
                retry_safe_resume=1 if retry_safe_resume else 0,
                terminal_state="GENERATION_FAILED",
            )
            _update_recovery(
                conn, recovery_id, generation_status="FAILED",
                generation_detail=repr(exc), generation_at=int(time.time()),
                terminal_state="GENERATION_FAILED",
            )
            return {
                "recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                "terminal_state": "GENERATION_FAILED", "error": repr(exc),
                "failure_stage": exc.stage if type(exc) is ConversationDirectionFailure else None,
                "failure_code": exc.failure_code if type(exc) is ConversationDirectionFailure else None,
                "retry_safe_resume": retry_safe_resume,
            }

        x_event_id = gen_result.get("native_event_id") if isinstance(gen_result, dict) else None
        _update_attempt(
            conn, recovery_id, attempt_ordinal,
            generation_status="SUCCEEDED", generation_at=int(time.time()),
        )
        _update_recovery(
            conn, recovery_id, generation_status="SUCCEEDED", generation_at=int(time.time()),
        )
        if not x_event_id:
            _update_attempt(
                conn, recovery_id, attempt_ordinal, persistence_status="FAILED",
                subject_output_released=None, retry_safe_resume=0,
                terminal_state="PERSISTENCE_FAILED",
            )
            _update_recovery(conn, recovery_id, persistence_status="FAILED",
                              terminal_state="PERSISTENCE_FAILED")
            return {"recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                    "terminal_state": "PERSISTENCE_FAILED"}

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
            _update_attempt(
                conn, recovery_id, attempt_ordinal, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=x_event_id,
                retry_safe_resume=0, terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            _update_recovery(
                conn, recovery_id, persistence_status="UNCONFIRMED",
                persistence_detail=detail, terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            return {
                "recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                "terminal_state": "PERSISTENCE_UNCONFIRMED",
                "error": detail,
            }
        confirmed_x = linked_xs[0]
        recovery_links = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id=? "
            "AND component_kind='wtr0_recovery_id' ORDER BY sequence",
            (confirmed_x,),
        ).fetchall()
        if recovery_links != [(recovery_id,)]:
            detail = (
                f"returned event {confirmed_x!r} does not carry the one exact WTR0 "
                f"recovery identity {recovery_id!r}"
            )
            _update_attempt(
                conn, recovery_id, attempt_ordinal, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=confirmed_x,
                retry_safe_resume=0, terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            _update_recovery(
                conn, recovery_id, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=confirmed_x,
                terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            return {
                "recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                "terminal_state": "PERSISTENCE_UNCONFIRMED",
                "canonical_x_event_id": confirmed_x, "error": detail,
            }
        try:
            projection_receipt = conversation_projection.verify_recovered_exchange_projection(
                conn, human_input_event_id, confirmed_x, actor_id,
            )
        except Exception as exc:
            detail = (
                str(exc) if isinstance(exc, conversation_projection.ProjectionIntegrityError)
                else f"projection verification error: {type(exc).__name__}: {exc}"
            )
            _update_attempt(
                conn, recovery_id, attempt_ordinal, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=confirmed_x,
                retry_safe_resume=0, terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            _update_recovery(
                conn, recovery_id, persistence_status="UNCONFIRMED",
                persistence_detail=detail, canonical_x_event_id=confirmed_x,
                terminal_state="PERSISTENCE_UNCONFIRMED",
            )
            return {
                "recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
                "terminal_state": "PERSISTENCE_UNCONFIRMED",
                "canonical_x_event_id": confirmed_x, "error": detail,
            }

        subject_null = projection_receipt.get("reply_choice") == "no_reply"
        persistence_detail = (
            "Clark's canonical typed no_reply choice visible in authenticated canonical GUI projection"
            if subject_null else
            "substantive Clark prose visible in authenticated canonical GUI projection"
        )
        _update_attempt(
            conn, recovery_id, attempt_ordinal, persistence_status="SUCCEEDED",
            canonical_x_event_id=confirmed_x, persistence_detail=persistence_detail,
            subject_output_released=1, retry_safe_resume=0, terminal_state="SUCCESS",
        )
        _update_recovery(
            conn, recovery_id, persistence_status="SUCCEEDED", canonical_x_event_id=confirmed_x,
            persistence_detail=persistence_detail,
            terminal_state="SUCCESS",
        )
        return {
            "recovery_id": recovery_id, "attempt_ordinal": attempt_ordinal,
            "terminal_state": "SUCCESS",
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
    status = dict(zip(cols, row))
    attempt_rows = conn.execute(
        "SELECT attempt_ordinal, actor_id, visibility_scope, started_at, reset_status, "
        "reset_detail, reset_at, generation_status, generation_detail, generation_at, "
        "failure_stage, failure_code, pre_staging_proven, subject_output_released, "
        "persistence_status, persistence_detail, canonical_x_event_id, retry_safe_resume, "
        "terminal_state, updated_at FROM wtr0_recovery_attempt "
        "WHERE recovery_id=? ORDER BY attempt_ordinal",
        (status["recovery_id"],),
    ).fetchall()
    attempt_cols = [
        "attempt_ordinal", "actor_id", "visibility_scope", "started_at", "reset_status",
        "reset_detail", "reset_at", "generation_status", "generation_detail", "generation_at",
        "failure_stage", "failure_code", "pre_staging_proven", "subject_output_released",
        "persistence_status", "persistence_detail", "canonical_x_event_id", "retry_safe_resume",
        "terminal_state", "updated_at",
    ]
    status["attempts"] = [dict(zip(attempt_cols, attempt)) for attempt in attempt_rows]
    return status
