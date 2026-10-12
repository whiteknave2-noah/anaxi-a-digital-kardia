"""WTR0: prospective failure-evidence capture around an ordinary
llama_anaxi.run_waking_turn() call, wired into the real waking pathway.

WTR0-CORRECTION-1: this module is no longer test-only plumbing. The
real GUI waking entrypoint (`llama_gui.py`'s `respond()`) now calls
`run_waking_turn_capturing_failure()` instead of calling
`llama_anaxi.run_waking_turn()` directly, so a genuine future waking
failure prospectively records bounded, mechanical WTR0 failure
evidence through actual program control flow -- not as an
operator-invented manual step. This module still changes NOTHING in
llama_anaxi.py, resume_human_input.py, or wtr0_cli.py; it remains a
separate, additive seam any caller can opt into by calling this
function instead of run_waking_turn() directly.

On success, this wrapper returns llama_anaxi.run_waking_turn()'s own
result unchanged -- no WTR0 row of any kind is written for an ordinary
successful turn.

On an exception, this wrapper:
  1. Re-raises the SAME exception object to the caller unchanged
     (ordinary failure handling/visibility above this wrapper, e.g.
     llama_gui.py's ConversationDirectionFailure containment or
     Gradio's own generic error surface, is not altered in any way);
  2. Before re-raising, positively identifies the exact
     human_waking_input event (H) THIS failing attempt concerned (see
     "EXACT-H ATTRIBUTION" below) and, only when that identity is
     positively established, mechanically classifies and records
     exactly one waking_failure_evidence row for it via
     waking_failure_evidence.record_waking_failure().

If no H can be positively attributed to this exact attempt, no failure
evidence is recorded -- there is nothing truthfully to tie it to, and
this module never fabricates an event id, guesses one, or falls back
to "the actor's latest unanswered H" to attach one.

EXACT-H ATTRIBUTION (WTR0-CORRECTION-2)

WTR0-CORRECTION-1's original mechanism looked up "the most recently
committed unanswered human_waking_input event authored by this actor"
whenever no `existing_human_input_event_id` was supplied. Independent
verification established that this is a correlation, not proof of
attempt ownership: a new failing attempt that created no H of its own
(e.g. it failed before H persistence, or human_input_authority was
invalid) could silently attach retry-safe evidence to an unrelated,
genuinely historical unanswered H belonging to the SAME actor -- making
that historical H incorrectly ELIGIBLE for recovery.

This module now derives the exact current-attempt H id from the one
mechanism that is truthfully attempt-scoped: `llama_anaxi.run_waking_
turn()` itself calls `ui_turn_diagnostics.record_human_input_event_id()`
exactly once per call, unconditionally, immediately after H is
positively validated (a supplied `existing_human_input_event_id` is
re-verified fresh against canonical state before being accepted -- see
that function's own docstring) or newly persisted, and strictly BEFORE
`prepare_context()` or any other model-adjacent call that could raise.
`ui_turn_diagnostics` stores this per-thread (`threading.local`), and
Gradio already runs each conversational turn in its own worker thread
(see llama_gui.py's `respond()` docstring) -- the same isolation
boundary `ui_turn_diagnostics.py`'s own per-turn call tracking already
relies on.

This wrapper therefore:
  - explicitly clears that per-thread marker (`ui_turn_diagnostics.
    record_human_input_event_id(None)`) immediately before invoking
    `run_waking_turn_fn`, so a value read back afterward can only have
    been set by THIS exact call -- never a stale value left over from
    an earlier turn/test that happened to run on the same thread
    without going through `ui_turn_diagnostics.start_model_call_
    tracking()` first;
  - on exception, reads that marker back. A non-None value is the
    exact H `run_waking_turn_fn` itself positively established for
    THIS attempt before failing -- never an inference from actor
    identity, session, or timing. A None value means the exception
    occurred before any H was persisted/validated for this attempt (or
    no human attribution applies), so no H-targeted evidence exists to
    record. There is no "latest unanswered" fallback and no scan for a
    plausible event.

This module does not modify `ui_turn_diagnostics.py` -- it only reads
the same per-thread field that module's own existing, already-wired
`record_human_input_event_id()` call populates from inside the real
canonical H writer's call path.

BOUNDED, FAIL-CLOSED CLASSIFICATION (WTR0-CORRECTION-1, corrected by
WTR0-CORRECTION-2, corrected again by WTR0-CORRECTION-3)

The single try/except this module can install sits around the ENTIRE
run_waking_turn() call -- there is no narrower truthful boundary
available without editing llama_anaxi.py (out of this correction's
mutation scope), so this module cannot mechanically prove *why* an
arbitrary exception was raised, only *what it positively is* (its
actual, trusted type identity -- never a name/message string) and
*whether* a fresh canonical read shows X now exists. Classification is
therefore restricted to exception shapes this repository's own code
already documents as positively retry-safe or positively not, in this
priority order (a caller can never override any of this; retry_safe is
always looked up from waking_failure_evidence.FAILURE_CLASSES, never
supplied):

  1. A fresh canonical read shows X now exists for this H anyway
     (e.g. best-effort post-persistence bookkeeping raised after the
     canonical transaction already committed) -> POST_PERSISTENCE_
     EXCEPTION_X_EXISTS, never retry-safe, regardless of exception
     type. A positive database fact always outranks any inference
     from exception type.
  2. `type(exc) is native_turn_staging.StagingIndeterminateStateError`
     -> AMBIGUOUS_PERSISTENCE_OUTCOME. That exception's own docstring
     states retry-whole is explicitly FORBIDDEN until an operator
     inspects the raw staging file.
  3. `type(exc) is native_turn_staging.StagingDurabilityError` ->
     WAKING_EXECUTION_FAILURE_NO_X_PERSISTED, retry-safe. That
     exception's own docstring states the rollback succeeded and "the
     turn simply did not happen yet ... retry whole is genuinely
     safe."
  4. `type(exc) is conversation_direction.ConversationDirectionFailure`
     at ordinary `stage == "pass1"` whose `.failure_code` is exactly
     `WORKSPACE_ACTION_CONFLICT` -> WAKING_EXECUTION_FAILURE_NO_X_
     PERSISTED, retry-safe. This cross-validation branch is before
     working-set mutation, Workspace selection/action, Pass 2, staging,
     canonical X, and every durable external effect.
  5. `type(exc) is conversation_direction.ConversationDirectionFailure`
     whose `.failure_code` is exactly context_budget.BUDGET_EXCEEDED,
     except at `stage == "workspace_pass2"`, -> WAKING_EXECUTION_
     FAILURE_NO_X_PERSISTED, retry-safe. This is the WTR0 contract's own
     named example of a "deterministic budget/scaffold failure after H
     persistence" -- a mechanical scaffold check, not a nondeterministic
     model output, and it occurs before canonical X persistence.
     Workspace Pass-2 is excluded because its selected local action has
     already executed before that scaffold check; retry-whole safety
     cannot be inferred merely from the absence of X.
  6. An ordinary Pass-1/Pass-2 provider completion failure, or the
     Pass-2 missing completion-handshake failure, occurs before X and
     before every durable external effect -> WAKING_EXECUTION_FAILURE_
     NO_X_PERSISTED, retry-safe for the existing explicit same-H WTR0
     recovery only. There is still no automatic retry.
  7. Anything else -- an unrecognized exception type (regardless of
     what class name or message it happens to carry), a
     ConversationDirectionFailure with any other failure_code
     (MALFORMED_ACT, PROTOCOL_LEAKAGE, INVALID_ACT, ...), a bug in
     surrounding host code, an unfamiliar provider/runtime exception
     -- -> UNCLASSIFIED_WAKING_EXCEPTION, never retry-safe. This
     repository's current failure boundary genuinely cannot
     distinguish "ordinary transient provider hiccup" from "an
     unrelated programming bug" by type alone; per the WTR0 contract's
     explicit instruction not to guess, an unrecognized failure is
     recorded (never silently discarded) but is fail-closed non-retry-
     safe rather than optimistically treated as safe to retry.

WTR0-CORRECTION-3's classification fix: every branch above now checks
`type(exc) is TrustedClass` against the actual imported exception
class object -- never `isinstance()`, `type(exc).__name__`, `str(exc)`,
or any other name/message string. Independent verification of
WTR0-CORRECTION-2 established that `isinstance()` itself is not a
trustworthy authority check: CPython's `isinstance(obj, cls)` consults
`obj.__class__`, not only `type(obj)`, so an unrelated exception
instance that overrides `__class__` as a property returning the
trusted class (`class UnrelatedFailure(Exception):
@property\n    def __class__(self): return StagingDurabilityError`)
is accepted by `isinstance(exc, StagingDurabilityError)` even though
`type(exc)` is the wholly unrelated `UnrelatedFailure` -- the exact
gap WTR0-CORRECTION-2 left open despite already rejecting
`type(exc).__name__` string comparison. `type(exc)` reads the actual
runtime type slot directly and is not influenced by an instance- or
class-level `__class__` property, so `type(exc) is TrustedClass`
cannot be spoofed this way.

There is no legitimate subclass relationship to honor for any of the
three trusted types: `StagingDurabilityError`,
`StagingIndeterminateStateError`, and `ConversationDirectionFailure`
are each declared as direct, concrete `Exception` subclasses in this
repository (`native_turn_staging.py`, `conversation_direction.py`),
never subclassed elsewhere for waking-turn purposes --
`StagingIndeterminateStateError` is explicitly documented as
"deliberately not a subclass of StagingDurabilityError" so a caller
cannot silently conflate the two. Exact-type-identity semantics
(`type(exc) is TrustedClass`) is therefore the positively-justified
rule for all three categories; a genuine subclass introduced in the
future would need this module's classification extended deliberately,
not inherited implicitly. Nothing here trusts a caller-suppliable
name, label, message, or `__class__` presentation.

This module never lets a caller assert retry-safety directly, and
never invents a failure basis: `basis` always names the exact
mechanical rule that produced the classification.
"""
import sqlite3

import ui_turn_diagnostics
from conversation_direction import ConversationDirectionFailure, DirectionFailure
from native_turn_staging import StagingDurabilityError, StagingIndeterminateStateError
from waking_failure_evidence import record_waking_failure

try:
    import context_budget
    _BUDGET_EXCEEDED = context_budget.BUDGET_EXCEEDED
except Exception:
    # This repository's own context_budget module is a small, stdlib-
    # only, always-available constant module (see its own imports);
    # if it is somehow unavailable, budget-exceeded classification
    # cannot be positively established, so fail closed to a sentinel
    # no real failure_code will ever equal -- never a guessed string.
    _BUDGET_EXCEEDED = object()


def _human_input_event_has_canonical_x(conn, human_input_event_id):
    return conn.execute(
        "SELECT 1 FROM event_components c JOIN events e ON e.event_id = c.event_id "
        "WHERE c.component_kind = 'human_input_event_id' AND c.component_text = ? "
        "AND e.event_type = 'waking_turn' LIMIT 1",
        (human_input_event_id,),
    ).fetchone() is not None


# Pass-2 generation/handshake failures after a workspace action ran. (BUDGET_EXCEEDED stays
# unclassified: it is deterministic, so a replay would fail identically.)
_WORKSPACE_PASS2_REPLAYABLE_CODES = frozenset({
    "COMPLETION_LIMIT_REACHED", "INCOMPLETE_MODEL_COMPLETION", "COMPLETION_LIMIT_EXCEEDED",
    "INCOMPLETE_EXPRESSION_BOUNDARY", "MALFORMED_EXPRESSION", "EMPTY_EXPRESSION",
})


def _read_only_executed_workspace_action(exc):
    """(resource_class, action) when this failure was raised, by the genuine
    workspace_direction.WorkspaceDirectionFailure (compared by identity of the real class, never by
    name or __class__ presentation), AFTER a workspace action from the fixed READ-ONLY allowlist
    ran; otherwise None. Fails closed on any import problem."""
    try:
        import workspace_direction as wd
    except Exception:
        return None
    cause = getattr(exc, "__cause__", None)
    if type(cause) is not wd.WorkspaceDirectionFailure:
        return None
    executed = getattr(cause, "executed_action", None)
    if type(executed) is tuple and len(executed) == 2 and executed in wd.READ_ONLY_WORKSPACE_ACTIONS:
        return executed
    return None


def _classify_waking_failure(exc, has_x):
    """Pure. Returns (failure_class, basis) per the bounded, fail-
    closed priority order documented in this module's own docstring.
    Never returns a class outside waking_failure_evidence.FAILURE_CLASSES;
    never accepts a caller-supplied retry-safe flag -- retry_safe is
    always looked up from that fixed allowlist by the returned class
    name, never decided here.

    WTR0-CORRECTION-3: classification is derived solely from the
    exception's ACTUAL runtime type, `type(exc)`, compared by identity
    (`is`) against the genuine imported trusted exception class objects
    -- never `isinstance()` (which can be fooled by an instance- or
    class-level `__class__` property override, as independent
    verification of WTR0-CORRECTION-2 established), never
    `type(exc).__name__`, and never any other name/message/attribute
    string a spoofed or unrelated exception could supply."""
    if has_x:
        return (
            "POST_PERSISTENCE_EXCEPTION_X_EXISTS",
            "an exception propagated out of run_waking_turn(), but a fresh canonical read "
            "shows a linked waking_turn (X) already exists for this H",
        )

    actual_type = type(exc)

    if actual_type is StagingIndeterminateStateError:
        return (
            "AMBIGUOUS_PERSISTENCE_OUTCOME",
            "type(exc) is native_turn_staging.StagingIndeterminateStateError; retry-whole "
            "is forbidden per that exception's own contract",
        )

    if actual_type is StagingDurabilityError:
        return (
            "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
            "type(exc) is native_turn_staging.StagingDurabilityError; its own contract "
            "states the pre-append rollback succeeded, so no staged entry remains and "
            "retry-whole is safe",
        )

    if actual_type is ConversationDirectionFailure:
        failure_code = getattr(exc, "failure_code", None)
        if (
            getattr(exc, "stage", None) == "pass1"
            and failure_code == DirectionFailure.WORKSPACE_ACTION_CONFLICT
        ):
            return (
                "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                "type(exc) is conversation_direction.ConversationDirectionFailure at pass1 "
                "with failure_code=WORKSPACE_ACTION_CONFLICT; this cross-validation branch "
                "structurally precedes working-set mutation, Workspace selection/action, "
                "Pass 2, staging, canonical X persistence, and every durable external effect, "
                "so one explicit WTR0 same-H recovery is retry-whole safe",
            )
        if getattr(exc, "stage", None) == "workspace_pass2" and failure_code in _WORKSPACE_PASS2_REPLAYABLE_CODES:
            executed = _read_only_executed_workspace_action(exc)
            if executed is not None:
                return (
                    "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                    "type(exc) is conversation_direction.ConversationDirectionFailure at workspace_pass2 "
                    f"with failure_code={failure_code}, caused by the genuine WorkspaceDirectionFailure "
                    f"after the READ-ONLY workspace action {executed[0]}.{executed[1]} ran: no write and "
                    "no outward effect exists to duplicate, and the encounter record is written only after "
                    "a canonical X commits, so one explicit WTR0 same-H recovery is retry-whole safe",
                )
        if failure_code == _BUDGET_EXCEEDED:
            if getattr(exc, "stage", None) == "workspace_pass2":
                return (
                    "UNCLASSIFIED_WAKING_EXCEPTION",
                    "workspace Pass-2 budget failure occurred after the selected workspace "
                    "action may already have produced a durable effect; retry-whole is not "
                    "established safe",
                )
            return (
                "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                "type(exc) is conversation_direction.ConversationDirectionFailure with "
                "failure_code=BUDGET_EXCEEDED -- a deterministic scaffold/budget check that "
                "structurally precedes canonical X persistence, not a nondeterministic model "
                "output",
            )
        stage = getattr(exc, "stage", None)
        provider_completion_codes = {
            "COMPLETION_LIMIT_REACHED",
            "INCOMPLETE_MODEL_COMPLETION",
            "COMPLETION_LIMIT_EXCEEDED",
        }
        if (
            stage in {"pass1", "pass2"}
            and failure_code in provider_completion_codes
        ) or (
            # Ordinary Pass-2 expression-not-established failures (a blank or non-text completion
            # under the plain-reply seam; INCOMPLETE_EXPRESSION_BOUNDARY from historical marker-era
            # records): the typed act was valid, nothing was persisted as X and no outward effect
            # was dispatched.  Never Clark's silence -- his silence is only the typed no_reply choice,
            # which commits a canonical X and so is never recovery-eligible.
            stage == "pass2"
            and failure_code in ("INCOMPLETE_EXPRESSION_BOUNDARY", "EMPTY_EXPRESSION", "MALFORMED_EXPRESSION")
        ):
            return (
                "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                "type(exc) is conversation_direction.ConversationDirectionFailure at ordinary "
                f"{stage} with failure_code={failure_code}; this boundary is before canonical X "
                "persistence and before every durable external side effect, so one explicit "
                "WTR0 same-H recovery is retry-whole safe",
            )
        return (
            "UNCLASSIFIED_WAKING_EXCEPTION",
            f"type(exc) is conversation_direction.ConversationDirectionFailure but "
            f"failure_code={failure_code!r} is not the BUDGET_EXCEEDED case this module can "
            f"positively prove retry-safe; treated conservatively as not retry-safe",
        )

    return (
        "UNCLASSIFIED_WAKING_EXCEPTION",
        f"an exception (actual type={actual_type.__module__}.{actual_type.__qualname__}) "
        f"propagated out of run_waking_turn() and a fresh canonical read confirms no "
        f"waking_turn (X) is linked to this H, but this exception's actual runtime type is "
        f"not identical to any trusted retry-safe type this module positively imports -- "
        f"treated conservatively as not retry-safe rather than guessed from its name, "
        f"message, or __class__ presentation",
    )


def run_waking_turn_capturing_failure(
    run_waking_turn_fn, orch, prompt, *, provenance_db_path,
    interaction_mode=None, human_input_authority=None, existing_human_input_event_id=None,
):
    """Calls `run_waking_turn_fn` (ordinarily llama_anaxi.run_waking_turn,
    passed explicitly so tests can substitute a fake without patching
    module globals) with the exact same arguments an ordinary caller
    would use, and additionally records bounded WTR0 failure evidence
    on exception, attributed only to the exact H `run_waking_turn_fn`
    itself positively establishes for this attempt (see this module's
    own "EXACT-H ATTRIBUTION" docstring section) -- never a guess.

    WTR0-CORRECTION-2: this function no longer accepts an `actor_id`
    parameter. It was used only by the removed "latest unanswered H by
    actor" heuristic; exact-H attribution now comes solely from
    `ui_turn_diagnostics`'s per-thread marker, which `run_waking_turn_fn`
    itself populates. A caller-supplied `existing_human_input_event_id`
    is still passed through to `run_waking_turn_fn` unchanged (it still
    decides whether this is a continuation attempt), but this wrapper
    no longer trusts it directly for evidence attribution -- it trusts
    only what `run_waking_turn_fn` itself confirms it actually used,
    via the same per-thread marker, whether that came from a validated
    `existing_human_input_event_id` or a freshly persisted H.

    Always re-raises the SAME exception object the wrapped call itself
    raised, unchanged, even if evidence capture below fails for its own
    reasons (e.g. the provenance DB is briefly locked) -- an evidence-
    capture failure is swallowed (best-effort, matching this
    repository's existing ui_turn_diagnostics.py convention) and never
    replaces, masks, or reclassifies the real waking failure."""
    # WTR0-CORRECTION-2: positively establish a clean per-attempt H
    # marker before calling the wrapped function. A value read back
    # after an exception can then only have been set by THIS exact
    # call to run_waking_turn_fn -- never a value left over from an
    # earlier turn/test that happened to run on the same thread.
    ui_turn_diagnostics.record_human_input_event_id(None)
    try:
        return run_waking_turn_fn(
            orch, prompt, interaction_mode=interaction_mode,
            human_input_authority=human_input_authority,
            existing_human_input_event_id=existing_human_input_event_id,
        )
    except Exception as exc:
        try:
            # The ONLY source of H attribution: the exact value (or
            # None) run_waking_turn_fn itself positively recorded for
            # this attempt, via ui_turn_diagnostics.record_human_
            # input_event_id(), before this exception was raised. No
            # "latest unanswered H" fallback, no scan, no inference
            # from actor identity or timing.
            human_input_event_id = getattr(
                ui_turn_diagnostics._model_call_local, "human_input_event_id", None
            )
            if human_input_event_id is not None:
                conn = sqlite3.connect(f"file:{provenance_db_path}?mode=ro", uri=True)
                try:
                    has_x = _human_input_event_has_canonical_x(conn, human_input_event_id)
                finally:
                    conn.close()
                failure_class, basis = _classify_waking_failure(exc, has_x)
                record_waking_failure(
                    provenance_db_path, human_input_event_id=human_input_event_id,
                    failure_class=failure_class, basis=basis, detail=repr(exc),
                )
            # else: no H was positively established for this exact
            # attempt before it failed (e.g. the exception occurred
            # before H persistence/validation, or no human attribution
            # applies) -- nothing truthful to attach evidence to, so
            # nothing is recorded. Never fabricate or guess an id.
        except Exception:
            # Evidence capture itself must never conceal, replace, or
            # reclassify the real waking failure -- best-effort only,
            # exactly like ui_turn_diagnostics.py's own `_append_record`.
            pass
        raise
