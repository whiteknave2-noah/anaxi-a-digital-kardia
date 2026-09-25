"""
Anaxi -- Llama, browser-based chat window. A thin presentation layer
over llama_anaxi.py's run_waking_turn() -- everything constitutional
(Kardia, memory, logging, relational history) lives there, once, and
this file has no copy of any of it. Deleting this file removes a way
to talk to the agent; it removes none of the agent's actual state.

Setup: same folder, same dependencies as llama_anaxi.py, plus:
    pip install gradio

Run:
    python llama_gui.py                  -- TASK mode (unchanged default)
    python llama_gui.py --conversation   -- CONVERSATION mode

OWC6-G1: interaction mode is resolved ONCE, here, at process start,
from argv -- never inferred from what gets typed in the chat box, and
never re-resolved per turn. It is authoritative for this GUI process's
entire lifetime; there is no in-chat toggle and no mid-session switch
(a later gate may add one, deliberately, if ever needed). The
resolved mode is shown in the window's title/description, visible
before the first turn, host-derived, not model-generated, not part of
the chat history the model itself ever sees.

OWC7-S2: a compact directional-ownership control surface sits above
the chat window -- a read-only status ("Direction owner: UNKNOWN/
ALEX/CLARK") and exactly two buttons ("Take direction"/"Give direction
to Clark"). This is HOST-SIDE ONLY: button clicks never call the
model (zero Pass-1/Pass-2/Kardia/hippocampal involvement), and the
status display is refreshed on page load/reconnect by a read-only
function that can never itself write direction_owner (see
refresh_direction_status() vs _perform_human_direction_control()
below -- two structurally distinct functions, never merged, so a
render/reconnect event has no code path to a mutation). Ownership
changes only through an explicit button click, which mechanically
resolves the canonical human actor from the live actors registry
(direction_control.resolve_canonical_human_actor_id() -- never a
hardcoded ID, never inferred from chat content) before calling
conversation_direction.apply_human_direction_control(). Every attempt,
successful or failed, is durably recorded to direction_control_trace.jsonl
-- operational control telemetry only, never fed into Clark's memory/
context pathways (see direction_control.py's own module docstring).

Opens a local web page (Gradio will print the address, usually
http://127.0.0.1:7860) with a GPT-style chat window -- message
bubbles, scrolling history, all handled by Gradio itself. Nothing
here leaves your machine; "local" in that URL means local, not a
hosted service reachable from outside it.

Gradio's own chat history (the `history` argument below) is accepted
but intentionally unused -- it would be a second, competing record of
the conversation. The real one is the database, reconstructed fresh
by run_waking_turn() -> prepare_context() on every turn.

No Sleep cycle runs automatically from this window, at any point,
including on close. Clark may originate a typed Sleep request through
ordinary waking; the separately rendered owner controls require an
explicit registered-human disposition and a second explicit confirmed
execution click. That surface invokes only the authoritative Sleep-v1
cycle and cannot rewrite Kardia or fabricate a request.

Ctrl+C in the terminal stops the local server when you're done.
"""

import functools
import json
import os
from datetime import datetime, timezone
import sqlite3
import family_membership
import sys
import time

import gradio as gr
import ollama

import conversation_direction
import conversation_projection
import conversation_display
import direction_control
import readonly_db
import human_session_binding
import session_dialogue_window
import ui_turn_diagnostics
import workspace_capability
import workspace_episode_provenance
import workspace_private
import workspace_roaming
import workspace_supervisor
import sleep_owner_control
import waking_recovery_control
import wtr0_cold_reset
import waking_pipeline_lock
import caret_wake
import caret_owner_status
import caret_correspondent_admin
import correspondence_admin
import obsidian_workspace
from conversation_direction import ConversationDirectionFailure, get_last_conversation_direction_trace
from waking_turn_failure_capture import run_waking_turn_capturing_failure
from llama_anaxi import (
    MODEL, DB_PATH, PROVENANCE_DB_DIR, PIPELINE_KEY, STAGING_PATH, run_waking_turn, resolve_launch_mode, ask_llama_for_json,
    TASK_MODE, CONVERSATION_MODE, DIRECTION_HUMAN, DIRECTION_CLARK, DIRECTION_UNKNOWN,
    apply_human_direction_control, get_working_set, set_working_set, get_current_session_id,
    get_current_session_started_at,
)
from orchestration import AnaxiOrchestrator

# OWC6-G1: resolved ONCE, at process start, from this process's own
# argv -- authoritative for this GUI process's entire lifetime. See
# respond() below: every turn passes this explicitly, never relying on
# conversation_direction/_interaction_mode_state's session-stickiness
# (spec section 3) -- this IS what sets that state, explicitly, each
# call, not something read from a prior turn's leftover value.
LAUNCH_MODE, _extra_launch_args = resolve_launch_mode(sys.argv[1:])
if _extra_launch_args:
    raise SystemExit(
        f"llama_gui.py accepts only an optional --conversation flag as its "
        f"sole argument; unexpected argument(s): {_extra_launch_args!r}"
    )

# OWC6-G1: the mode-visible operator surface (spec section 5) -- host-
# derived from LAUNCH_MODE alone, never from the model, never from
# chat content. Exposed as a plain string so it's independently
# testable without starting a real Gradio server.
INTERACTION_MODE_LABEL = "CONVERSATION" if LAUNCH_MODE == CONVERSATION_MODE else "TASK"

# SLP1-A4c: the ONLY way this process's waking session may ever become
# bound to a registered human actor -- an explicit, honest environment
# variable naming a specific already-registered actor_id, resolved
# ONCE at process start (same "resolve once, at process start, fail
# loudly if wrong" convention LAUNCH_MODE itself already follows two
# lines above). NEVER inferred from USER_ID="nate", localhost,
# "exactly one registered human," or the current OS user. Absent (the
# default -- no production human registration has been performed as of
# this gate) means this process's session remains genuinely unbound,
# safely and identically to every prior gate's behavior. Present but
# naming an actor that isn't already a genuinely registered
# human_person actor fails LOUDLY at import time (human_session_
# binding.HumanSessionBindingError propagates, refusing to start the
# server silently misconfigured) rather than silently degrading to
# unbound, which would mask a real operator mistake.
BOUND_HUMAN_ACTOR_ID = os.environ.get("ANAXI_BOUND_HUMAN_ACTOR_ID")

# FS1: optional principal visibility scope for this process's bound
# session, opt-in via ANAXI_BOUND_VISIBILITY_SCOPE ('principal_private'
# or 'family_shared'). Absent/unset (the default) keeps the session
# UNBOUND-scoped -- today's exact behavior, byte-identical, and the
# integration tests that bind without a scope stay untouched. A value
# that names an unknown scope, or that binds an actor who is not a
# currently-active family principal, fails LOUDLY at import time
# (HumanSessionBindingError propagates) -- never silently degrades,
# exactly like BOUND_HUMAN_ACTOR_ID above.
# FS1 completion: once the family feature exists an unscoped session would receive no continuity, so an
# unconfigured owner window binds principal_private (see family_membership.default_bound_scope).
BOUND_VISIBILITY_SCOPE = family_membership.default_bound_scope(
    f"{PROVENANCE_DB_DIR}/anaxi_provenance.db", BOUND_HUMAN_ACTOR_ID, os.environ.get("ANAXI_BOUND_VISIBILITY_SCOPE"))

BOUND_HUMAN_AUTHORITY = None
if BOUND_HUMAN_ACTOR_ID:
    _hib_conn = sqlite3.connect(f"{PROVENANCE_DB_DIR}/anaxi_provenance.db")
    _hib_conn.execute("PRAGMA foreign_keys = ON;")
    try:
        BOUND_HUMAN_AUTHORITY = human_session_binding.bind_session_to_registered_human(
            _hib_conn,
            session_id=get_current_session_id(),
            session_started_at=get_current_session_started_at(),
            pipeline_key=PIPELINE_KEY,
            actor_id=BOUND_HUMAN_ACTOR_ID,
            visibility_scope=BOUND_VISIBILITY_SCOPE,
        )
    finally:
        _hib_conn.close()

# WTR0-CORRECTION-1: the exact canonical provenance DB path used by
# run_waking_turn_capturing_failure() below to positively re-read
# whether a given H has since acquired canonical X -- the same path
# already used just above to bind BOUND_HUMAN_AUTHORITY, named once
# here so respond() doesn't recompute it every turn.
_PROVENANCE_DB_PATH = os.path.join(PROVENANCE_DB_DIR, "anaxi_provenance.db")


def _authenticated_projection_kwargs():
    """Bind GUI history to the same principal/scope as waking ingress."""
    if BOUND_HUMAN_AUTHORITY is None:
        return {}
    return {
        "viewer_principal_actor_id": BOUND_HUMAN_AUTHORITY.authenticated_actor_id,
        "viewer_visibility_scope": BOUND_HUMAN_AUTHORITY.visibility_scope,
    }

# OWC7-S2: overridable by tests, exactly like llama_anaxi.
# CONVERSATION_DIRECTION_TRACE_PATH -- never left pointed at the
# production-relative default during a test run.
DIRECTION_CONTROL_TRACE_PATH = direction_control.DEFAULT_TRACE_PATH

# WSP2-S1: overridable by tests, same convention.
WORKSPACE_ROAMING_TRACE_PATH = workspace_roaming.DEFAULT_TRACE_PATH


# RECURRENT TURN-ACCUMULATION UI FAILURE gate: overridable by tests,
# same convention as every other *_TRACE_PATH/*_PATH constant in this
# file -- never left pointed at the production-relative default during
# a test run.
UI_TURN_DIAGNOSTICS_LOG_PATH = ui_turn_diagnostics.DEFAULT_LOG_PATH


# ------------------------------------------- OWC7-P2: control-failure containment

# OWC7-P2 (spec section 9): the deterministic, non-Clark operator
# status shown in the genuinely separate status panel
# (control_failure_status_display, below) -- wired via explicit submission
# outputs, the same "host-owned, read-only status surface"
# pattern already established by direction_status_display and
# roaming_status_display in this same file. Wording follows the
# gate's own example text.
GENERATION_INCOMPLETE_CODES = frozenset({
    "COMPLETION_LIMIT_REACHED", "COMPLETION_LIMIT_EXCEEDED", "INCOMPLETE_MODEL_COMPLETION",
    "INCOMPLETE_EXPRESSION_BOUNDARY", "EMPTY_EXPRESSION", "MALFORMED_EXPRESSION",
})

CONTROL_FAILURE_STATUS_TEXT = (
    "**Clark's conversational-direction control could not be validated.**  \n"
    "The submitted turn did not complete. No Clark response was committed.  \n"
    "Preserve the submitted input; use the supported same-input continuation after repair."
)

# Backward-compatible marker returned by respond() to direct callers. The
# supported UI now renders a failed turn only in its host status panel; it
# never inserts this marker into the canonical conversation projection.
CONTROL_FAILURE_CHAT_MARKER = (
    "[Clark did not respond. This message was generated by the host, not Clark, "
    "after a conversational-direction control failure -- see the status panel "
    "above. The submitted input must not be resent as a new message.]"
)

# The status panel's resting value on every ordinary successful turn --
# explicitly restored each time (never gr.skip()), so failure text from
# an earlier contained turn can never linger past the next turn that
# actually completes.
CONTROL_FAILURE_STATUS_NEUTRAL = ""


def contain_control_failure(exc):
    """Pure host diagnostics, distinguished by the recorded failure code.

    No state mutation, retry, or canonical Clark prose. A budget rejection
    can follow a valid Pass 1, so it must not claim control validation failed
    or that the working set was unchanged. The legacy marker remains explicitly
    host-authored; the supported UI uses the separate status output only.
    """
    if exc.stage in ("pass2", "workspace_pass2") and exc.failure_code in GENERATION_INCOMPLETE_CODES:
        # A reply that began and never finished is a GENERATION failure, not a control-validation
        # failure: say so, and say what did and did not happen (live: a workspace turn whose page
        # image was delivered, then the vision model spent its whole window reasoning).
        return (
            "[Host diagnostic: Clark's reply did not finish generating. This is not a Clark response.]",
            f"**Clark's reply did not finish generating ({exc.failure_code} at {exc.stage}).**  \n"
            "Any workspace action this turn already ran; its result was delivered to the model but no Clark "
            "response was committed.  \n"
            "The input is recorded as unanswered. Same-input recovery is offered only where the failure is "
            "proven retry-safe; otherwise send the request again as a new message.",
        )
    if exc.failure_code == "BUDGET_EXCEEDED":
        return (
            "[Host diagnostic: Clark did not respond because the waking prompt exceeded its budget. "
            "This is not a Clark response. Preserve the submitted input for same-input continuation.]",
            f"**Waking prompt budget exceeded at {exc.stage}.**  \n"
            "No Clark response was committed. This is a budget rejection, not an invalid Open state.  \n"
            "Preserve the submitted input; use the supported same-input continuation after repair.",
        )
    return CONTROL_FAILURE_CHAT_MARKER, CONTROL_FAILURE_STATUS_TEXT


def respond(message: str, history: list):
    # Opened fresh per call, not shared across the server's lifetime --
    # Gradio runs each message in its own worker thread, and a single
    # shared sqlite connection created in the main thread can't be used
    # from a different one. Same pattern run_waking_turn() already uses
    # for RelationalHistory, which is why that half never hit this.
    orch = AnaxiOrchestrator(DB_PATH)

    # Observability only (ui_turn_diagnostics.py) -- wraps the exact
    # same run_waking_turn() call with per-turn timing/size/exception
    # capture, then returns or re-raises exactly what that call itself
    # would have. No conversational behavior is changed by this wrap:
    # on success, `result` is identical to calling run_waking_turn()
    # directly; on failure, the exact same exception propagates.
    #
    # OWC6-G1: LAUNCH_MODE, explicitly, every single turn -- the fix
    # for OWC6-O2's confirmed gap (this call used to omit
    # interaction_mode entirely, which silently meant TASK mode always,
    # regardless of launch flag or conversation content).
    #
    # WTR0-CORRECTION-1: routed through run_waking_turn_capturing_
    # failure() instead of calling run_waking_turn() directly -- the
    # real waking pathway now prospectively records bounded WTR0
    # failure evidence on a genuine future waking failure, through
    # actual program control flow, rather than leaving that an
    # unimplemented manual/integration step. On success this is
    # byte-for-byte the same call (the wrapper returns run_waking_
    # turn()'s own result unchanged); on failure it re-raises the
    # exact same exception this `try` already handled before this
    # correction, so ConversationDirectionFailure containment below
    # and Gradio's own generic-error behavior for anything else are
    # both completely unchanged.
    #
    # WTR0-CORRECTION-2: run_waking_turn_capturing_failure() no longer
    # takes an actor_id -- it attributes a failure to the exact H
    # run_waking_turn() itself positively records via ui_turn_
    # diagnostics.record_human_input_event_id() (per-thread, cleared
    # before this call) before any model-adjacent call, never a
    # "latest unanswered H for this actor" guess. BOUND_HUMAN_AUTHORITY
    # may be None (session genuinely unbound); when it is, run_waking_
    # turn() records no H at all for this call, so nothing is
    # attributed on failure -- never fabricated.
    try:
        result = ui_turn_diagnostics.wrap_conversation_callback(
            message, history,
            run_fn=lambda: run_waking_turn_capturing_failure(
                run_waking_turn, orch, message, provenance_db_path=_PROVENANCE_DB_PATH,
                interaction_mode=LAUNCH_MODE,
                human_input_authority=BOUND_HUMAN_AUTHORITY,
            ),
            log_path=UI_TURN_DIAGNOSTICS_LOG_PATH,
            session_id_fn=get_current_session_id,
        )
    except ConversationDirectionFailure as exc:
        # OWC7-P2 (spec section 8): the ONLY exception family contained
        # at this boundary -- any other exception still propagates
        # completely unchanged, exactly as before this gate (Gradio
        # still shows its generic red Error for a genuinely unexpected
        # failure; that is intentionally out of scope here). The
        # diagnostic record for this exact failure was already written
        # by wrap_conversation_callback() above, before it re-raised --
        # this block does not write a second one, and orch is not
        # closed here (no successful turn to persist against; matches
        # this function's pre-existing orch.close()-only-on-success
        # shape for every other exception path).
        return contain_control_failure(exc)
    orch.close()
    if LAUNCH_MODE == CONVERSATION_MODE:
        # WSP2-P5 (spec section 10): the canonical trigger a prior
        # wait_for_human choice was actually waiting for -- a
        # successful ordinary CONVERSATION_MODE waking turn, never a
        # timer, never presence detection. Best-effort and swallowed:
        # a bug in the background lifecycle must never turn an
        # otherwise-successful waking turn into a failed one. No-op
        # unless background activity is currently WAITING_FOR_HUMAN.
        try:
            workspace_roaming.release_wait_for_human_and_maybe_resume(
                **_background_activity_kwargs(get_current_session_id())
            )
        except Exception:
            pass
        # WSP2-P5-P1 (spec section 6/11): Clark's own typed waking
        # resume_own_pause choice -- applied ONLY when llama_anaxi.py
        # already durably recorded it (result["background_activity_
        # resume_recorded"] is True only once that write succeeded
        # after this turn's own canonical persistence; see run_waking_
        # turn()'s own comments). apply_clark_self_resume_if_valid() is
        # itself idempotent/no-harm (a no-op unless background activity
        # is CURRENTLY exactly PAUSED_BY_CLARK -- spec section 8), so
        # this can never override a human pause or a fault pause.
        # Best-effort and swallowed, same discipline as the hook above.
        try:
            if result.get("background_activity_resume_recorded") and workspace_roaming.apply_clark_self_resume_if_valid():
                _auto_start_background_activity()
        except Exception:
            pass
    # Explicitly restored to neutral on every successful turn (never
    # left as whatever it last showed) -- so failure text from an
    # earlier contained turn can never linger past the next turn that
    # actually completes.
    return result["reply"], CONTROL_FAILURE_STATUS_NEUTRAL


# ------------------------------------------- OWC7-S2: direction control --

# B10 (OWC7-P2 gate): family-facing presentation ONLY, never a second
# source of truth. The mechanical direction_owner/direction_request
# values themselves are completely unchanged by this section -- they
# remain exactly what conversation_direction.py/direction_control.py
# already compute, still fully available raw in
# conversation_direction_trace.jsonl, DIRECTION_CONTROL_TRACE_PATH,
# get_working_set(), and every existing test (spec section B13). This
# section only decides what a family member SEES.

# spec B3: color is supplemental (B11) -- these swatches are always
# paired with plain-language text below; a colorblind reader is never
# left with color as the only signal.
_LEAD_SWATCH = {"amber": "\U0001f7e0", "blue": "\U0001f535", "green": "\U0001f7e2"}


def _conversation_lead_label(direction_owner):
    """Pure. Maps the raw direction_owner value to the frozen
    family-facing (color, word) pair (spec section B1/B3). Any
    unrecognized value falls back to the same rendering as
    DIRECTION_UNKNOWN -- fail-visible, never a blank/crashed display."""
    if direction_owner == DIRECTION_HUMAN:
        return "blue", "You"
    if direction_owner == DIRECTION_CLARK:
        return "green", "Clark"
    return "amber", "Open"


def _conversation_lead_text(direction_owner, note=None):
    """Family-facing rendering of the CURRENT conversation lead only
    (spec B1/B2) -- never the separate request indicator (see
    _conversation_lead_request_text() below, spec B4/B5: request is
    always shown separately, never folded into this same string)."""
    color, who = _conversation_lead_label(direction_owner)
    text = f"{_LEAD_SWATCH[color]} **Conversation lead: {who}**"
    if note:
        text += f"  \n*{note}*"
    return text


def refresh_conversation_lead():
    """Read-only. Reads the authoritative process/session working set
    and renders its current direction_owner -- NEVER writes anything.
    Safe to wire to page load/reconnect (spec section 3/10): a render
    event has no code path to a mutation, because this function and
    _perform_human_direction_control() below are structurally separate
    -- there is no shared "maybe write" branch between them."""
    working_set = get_working_set()
    return _conversation_lead_text(working_set.get("direction_owner", DIRECTION_UNKNOWN))


# serial of the last-turn trace whose direction_request the owner has already answered with a lead control.
_ANSWERED_LEAD_REQUEST_TRACE_SERIAL = None


def _conversation_lead_request_text():
    """Read-only. Returns family-facing markdown for a currently
    outstanding, VALIDLY-established direction_request from the most
    recent conversation-mode turn, or "" if there is nothing to show.

    Gated on pass1_status == "ok" specifically (spec section B10): a
    contained ConversationDirectionFailure (MALFORMED_ACT,
    UNAUTHORIZED_RELINQUISH, PROTOCOL_LEAKAGE, INVALID_ACT, etc.) must
    never surface a request banner, even for UNAUTHORIZED_RELINQUISH
    where direction_request itself happened to structurally parse --
    the overall turn was rejected, so nothing was validly established
    from a family-facing perspective. pass2_status is deliberately NOT
    required to be "ok" here -- direction_request/direction_owner are
    resolved entirely from Pass 1's validated act, independent of
    whether Pass 2's expression later succeeded (see llama_anaxi.py's
    run_waking_turn(): set_working_set() there runs unconditionally
    once Pass 1 validates, before Pass 2 is even called)."""
    trace = get_last_conversation_direction_trace()
    if trace is None or trace.get("pass1_status") != "ok":
        return ""
    direction_request = trace.get("direction_request")
    # A request is outstanding only until it is answered: by the owner operating any lead control after
    # the turn that made it, or because the lead already sits where it asked. Showing "Clark is asking you
    # to lead" beside an owner-selected "Conversation lead: Clark" (or a lead already with the person)
    # would contradict the state the owner just chose.
    if conversation_direction.last_conversation_direction_trace_serial() == _ANSWERED_LEAD_REQUEST_TRACE_SERIAL:
        return ""
    current_owner = get_working_set().get("direction_owner", DIRECTION_UNKNOWN)
    if direction_request == conversation_direction.REQUEST_CLARK and current_owner != DIRECTION_CLARK:
        return f"{_LEAD_SWATCH['green']} Clark is asking to lead."
    if direction_request == conversation_direction.REQUEST_HUMAN and current_owner != DIRECTION_HUMAN:
        return f"{_LEAD_SWATCH['blue']} Clark is asking you to lead."
    return ""  # REQUEST_NONE, or any other/unrecognized value -- nothing to show


def refresh_conversation_lead_request():
    """Read-only, same safety contract as refresh_conversation_lead()
    above -- no code path to a mutation."""
    return _conversation_lead_request_text()


def _resolve_canonical_human_actor_id_or_error():
    """Read-only DB lookup. Returns (actor_id, None) on success or
    (None, error_message) on failure -- never raises past this point,
    never hardcodes a fallback ID."""
    db_path = os.path.join(PROVENANCE_DB_DIR, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return None, "canonical provenance database not found"
    conn = sqlite3.connect(readonly_db.readonly_sqlite_uri(db_path), uri=True)
    try:
        actor_id = direction_control.resolve_canonical_human_actor_id(conn)
        return actor_id, None
    except direction_control.CanonicalHumanActorResolutionError as exc:
        return None, str(exc)
    finally:
        conn.close()



def _holding_waking_lock(fn):
    """Owner controls that rewrite session state or reset the waking inference
    path serialize with every waking turn -- Mac-originated or a Caret occasion."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with waking_pipeline_lock.LOCK:
            return fn(*args, **kwargs)
    return wrapper


@_holding_waking_lock
def _perform_human_direction_control(control_act, target_owner):
    """The ONLY function in this file that may write direction_owner.
    Invoked exclusively by an explicit button click (see the three
    thin wrappers below) -- never by page load, render, or reconnect.
    Zero model calls: no Pass 1, no Pass 2, no Kardia, no hippocampal
    retrieval. Every attempt (success or failure) is durably recorded
    to DIRECTION_CONTROL_TRACE_PATH."""
    session_id = get_current_session_id()
    working_set = get_working_set()
    current_owner = working_set.get("direction_owner", DIRECTION_UNKNOWN)

    source_actor_id, resolution_error = _resolve_canonical_human_actor_id_or_error()
    if resolution_error is not None:
        direction_control.record_direction_control_trace(
            session_id=session_id, source_actor_id=None, control_act=control_act,
            direction_owner_before=current_owner, direction_owner_after=None,
            status="RESOLUTION_FAILED", trace_path=DIRECTION_CONTROL_TRACE_PATH,
        )
        return _conversation_lead_text(current_owner, note=f"control failed: {resolution_error}")

    # source_actor_id and canonical_human_actor_id are the same
    # mechanically-resolved value -- this project has no separate
    # multi-user login/session system to supply a distinct "claimed"
    # identity, so the resolution step itself IS the authentication
    # (see direction_control.py's own module docstring).
    new_owner, failure = apply_human_direction_control(
        source_actor_id, current_owner, target_owner, source_actor_id,
    )
    if failure is not None:
        direction_control.record_direction_control_trace(
            session_id=session_id, source_actor_id=source_actor_id, control_act=control_act,
            direction_owner_before=current_owner, direction_owner_after=None,
            status=failure, trace_path=DIRECTION_CONTROL_TRACE_PATH,
        )
        return _conversation_lead_text(current_owner, note=f"control failed: {failure}")

    updated_working_set = dict(working_set)
    updated_working_set["direction_owner"] = new_owner
    direction_control.record_direction_control_trace(
        session_id=session_id, source_actor_id=source_actor_id, control_act=control_act,
        direction_owner_before=current_owner, direction_owner_after=new_owner,
        status="ok", trace_path=DIRECTION_CONTROL_TRACE_PATH,
    )
    set_working_set(updated_working_set)
    global _ANSWERED_LEAD_REQUEST_TRACE_SERIAL
    _ANSWERED_LEAD_REQUEST_TRACE_SERIAL = conversation_direction.last_conversation_direction_trace_serial()
    return _conversation_lead_text(new_owner)


def take_direction():
    return _perform_human_direction_control(direction_control.TAKE_DIRECTION, DIRECTION_HUMAN)


def give_direction_to_clark():
    return _perform_human_direction_control(direction_control.GIVE_DIRECTION_TO_CLARK, DIRECTION_CLARK)


def open_direction():
    return _perform_human_direction_control(direction_control.OPEN_DIRECTION, DIRECTION_UNKNOWN)


# ---------------------------------------- WSP2-S1: workspace roaming control


_BACKGROUND_STATE_LABELS = {
    workspace_roaming.BACKGROUND_STATE_ACTIVE: "Active",
    workspace_roaming.BACKGROUND_STATE_WAITING: "Waiting",
    workspace_roaming.BACKGROUND_STATE_WAITING_FOR_HUMAN: "Waiting for you",
    workspace_roaming.BACKGROUND_STATE_PAUSED_BY_CLARK: "Paused",
    workspace_roaming.BACKGROUND_STATE_PAUSED_BY_HUMAN: "Paused",
    workspace_roaming.BACKGROUND_STATE_BACKOFF: "Recovering",
    workspace_roaming.BACKGROUND_STATE_FAULT_PAUSED: "Paused after technical failures",
    workspace_roaming.BACKGROUND_STATE_IDLE: "Idle",
    # WSP2-P5-P2 (spec section 11): a distinct, honest label -- never
    # "Paused" (would falsely imply a real pause decision was made by
    # Clark or a human), never "Recovering" (that label already means
    # ordinary bounded technical backoff, a different, known state).
    workspace_roaming.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN: "Recovery needed",
}


def _roaming_status_text(note=None):
    """WSP2-P5 (spec section 13): reports the BACKGROUND-ACTIVITY
    pathway honestly -- never as though Your Space itself were
    available/unavailable. background_activity_state alone is
    authoritative (host-owned fact, set synchronously by every control
    path below) rather than a live is_worker_running() check, so a
    just-issued pause/stop is reported correctly even in the instant
    before the worker thread has actually finished unwinding. No
    private branch identity, no raw model failure is ever surfaced
    here."""
    state = workspace_roaming.get_roaming_state()
    lifecycle = state.get("background_activity_state", workspace_roaming.BACKGROUND_STATE_IDLE)
    fallback = "Active" if workspace_roaming.is_worker_running() else "Idle"
    label = _BACKGROUND_STATE_LABELS.get(lifecycle, fallback)
    text = f"**Background Space activity: {label}**"
    if note:
        text += f"  \n*{note}*"
    return text


def refresh_roaming_status():
    """Read-only. Reads the authoritative process-local roaming state
    and renders it -- NEVER writes anything, NEVER starts/stops the
    worker. Safe to wire to page load/reconnect for the same reason
    refresh_direction_status() is (spec section 10: a render event has
    no code path to a mutation)."""
    return _roaming_status_text()


def _fetch_departure_dialogue_pairs(session_id):
    """WSP2-P3 Bridge A. Read-only, cheap DB read -- same synchronous-
    handler pattern as _resolve_canonical_human_actor_id_or_error()
    above; the button click stays instant/zero-model-call, exactly as
    before this gate. Returns [] (never raises) if no provenance DB
    exists yet or the read fails for any reason -- the empty-handoff
    path (spec: 'absence of selectable context, not a host semantic
    choice') then correctly makes zero model calls, exactly like
    session_dialogue_window.build_session_dialogue_window() itself
    already degrades to [] for a fresh process (see llama_anaxi.py's
    own run_waking_turn())."""
    db_path = os.path.join(PROVENANCE_DB_DIR, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(readonly_db.readonly_sqlite_uri(db_path), uri=True)
        try:
            pairs, _skipped = session_dialogue_window.collect_session_dialogue_pairs(conn, session_id, STAGING_PATH)
            return pairs
        finally:
            conn.close()
    except Exception:
        return []


def _background_activity_kwargs(session_id):
    """WSP2-P5: the shared parameter set every background-activity
    start path needs (human resume, process-init auto-start, and the
    wait_for_human release hook in respond() below) -- assembled
    identically to how this file's own worker-start call has always
    assembled it, so none of these paths can drift from the canonical
    preflight/Bridge-A/P4/MA2 route a human-initiated start already
    took. Zero model calls occur in this function itself."""
    return {
        "ask_llama_for_json": ask_llama_for_json,
        "clark_actor_id": workspace_supervisor.resolve_canonical_actor_id("clark"),
        "workspace_paths": workspace_capability.WorkspacePaths.production_defaults(),
        "session_id": session_id,
        "trace_path": WORKSPACE_ROAMING_TRACE_PATH,
        # WSP3-P1: ROOT PATH only -- no private content/filename/
        # observation is read or rendered here or anywhere in this file.
        "private_paths": workspace_private.PrivatePaths.production_defaults(),
        "data_dir": PROVENANCE_DB_DIR,
        "pipeline_key": PIPELINE_KEY,
        # WSP2-P3 Bridge A: fetched fresh at each start attempt (cheap,
        # synchronous, zero model calls) so every path below offers
        # Clark the same bounded departure-dialogue handoff a human-
        # initiated start always has.
        "dialogue_pairs": _fetch_departure_dialogue_pairs(session_id),
        "staging_path": STAGING_PATH,
    }


def _auto_start_background_activity():
    """Thin, no-auth wrapper around workspace_roaming's own eligibility-
    gated auto-start (spec section 6) -- called at process init and
    after a human resume/wait_for_human release, never from a bare
    button click that grants access. No-op (returns False) if not
    currently eligible; makes zero model calls itself."""
    return workspace_roaming.maybe_auto_start_background_activity(
        **_background_activity_kwargs(get_current_session_id())
    )


def pause_background_activity():
    """WSP2-P5 (spec section 12): the ONLY function in this file that
    may PAUSE background Space activity by human action. This pauses
    the BACKGROUND PATHWAY only -- it never implies Your Space itself
    is unavailable, never blocks ordinary supervised workspace actions,
    never alters the private-space contract, never alters conversation
    direction ownership (spec section 1's governing distinction)."""
    session_id = get_current_session_id()

    source_actor_id, resolution_error = _resolve_canonical_human_actor_id_or_error()
    if resolution_error is not None:
        return _roaming_status_text(note=f"could not pause: {resolution_error}")

    authorized, failure = workspace_roaming.apply_human_roaming_authorization(
        source_actor_id, source_actor_id, False,
    )
    if failure is not None:
        return _roaming_status_text(note=f"could not pause: {failure}")

    state = workspace_roaming.get_roaming_state()
    clark_actor_id = workspace_supervisor.resolve_canonical_actor_id("clark")
    workspace_roaming.pause_background_activity_by_human()
    workspace_roaming.record_roaming_trace(
        session_id=session_id, clark_actor_id=clark_actor_id, authorization_state=False,
        roaming_act=None, wait_minutes=None, workspace_action_id=None, workspace_action_status=None,
        roaming_action_count=state.get("roaming_action_count", 0),
        run_status=workspace_roaming.RUN_STATUS_STOPPED_BY_HUMAN, trace_path=WORKSPACE_ROAMING_TRACE_PATH,
    )
    return _roaming_status_text()


def resume_background_activity():
    """WSP2-P5 (spec section 12): the ONLY function in this file that
    may RESUME background Space activity after a human pause (or clear
    a FAULT_PAUSED state) by human action. Makes a new episode eligible
    again -- does not fabricate continuity with whatever came before,
    does not rewrite prior episode state.

    WSP2-P5-P1 (spec section 9): human Resume remains able to clear a
    Clark-owned pause too (no longer the SOLE way, since Clark's own
    typed resume_own_pause now also can). When it does, this ALSO
    durably reconciles the same canonical lifecycle-control ledger
    Clark's own stop/self-resume writes to -- otherwise a later restart
    could resurrect a stale PAUSED_BY_CLARK a human already cleared
    live, forking durable truth from what actually happened."""
    session_id = get_current_session_id()

    source_actor_id, resolution_error = _resolve_canonical_human_actor_id_or_error()
    if resolution_error is not None:
        return _roaming_status_text(note=f"could not resume: {resolution_error}")

    authorized, failure = workspace_roaming.apply_human_roaming_authorization(
        source_actor_id, source_actor_id, True,
    )
    if failure is not None:
        return _roaming_status_text(note=f"could not resume: {failure}")

    state = workspace_roaming.get_roaming_state()

    # WSP2-P5-P2 (spec section 8): human Resume must not silently
    # bypass an unresolved lifecycle-authority uncertainty -- the host
    # cannot safely know what authority it would be clearing while
    # canonical truth is unreadable. One safe, explicit re-read is
    # attempted here (never a retry loop, never polling): if canonical
    # truth is readable now, reconstruct_clark_owned_pause_from_
    # canonical_truth() already correctly updates state (to PAUSED_BY_
    # CLARK if a real pause is found, or back to IDLE otherwise) --
    # execution simply falls through to the ordinary logic below,
    # which already knows how to handle both outcomes. If it is STILL
    # unreadable, refuse to start anything and remain uncertain.
    if state.get("background_activity_state") == workspace_roaming.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN:
        authority = workspace_roaming.reconstruct_clark_owned_pause_from_canonical_truth(PROVENANCE_DB_DIR)
        if authority == workspace_roaming.CLARK_PAUSE_AUTHORITY_UNKNOWN:
            return _roaming_status_text(note="could not resume: background lifecycle state is still unavailable")

    was_paused_by_clark = state.get("background_activity_state") == workspace_roaming.BACKGROUND_STATE_PAUSED_BY_CLARK
    clark_actor_id = workspace_supervisor.resolve_canonical_actor_id("clark")
    workspace_roaming.resume_background_activity_by_human()
    if was_paused_by_clark:
        try:
            workspace_episode_provenance.record_background_lifecycle_control(
                PROVENANCE_DB_DIR, actor_id=source_actor_id, pipeline_key=PIPELINE_KEY,
                occurred_at=int(time.time()), control=workspace_episode_provenance.BACKGROUND_CONTROL_RESUMED,
            )
        except Exception:
            pass
    workspace_roaming.record_roaming_trace(
        session_id=session_id, clark_actor_id=clark_actor_id, authorization_state=True,
        roaming_act=None, wait_minutes=None, workspace_action_id=None, workspace_action_status=None,
        roaming_action_count=state.get("roaming_action_count", 0),
        run_status=workspace_roaming.RUN_STATUS_ENABLED, trace_path=WORKSPACE_ROAMING_TRACE_PATH,
    )
    _auto_start_background_activity()
    return _roaming_status_text()


def refresh_waking_conversation():
    """Read-only, same safety contract as refresh_conversation_lead()
    and refresh_roaming_status() above: reads canonical provenance
    directly (human_waking_input H events joined to waking_turn X
    events via conversation_projection.py) and NEVER writes anything.

    This is what makes the visible chat window a projection of durable
    canonical history rather than purely ephemeral, process-local
    Gradio state -- a process restart or browser reconnect (demo.load()
    fires on both, same as the other refresh_* functions in this file)
    re-derives the same chat from the same canonical record instead of
    starting from an empty list. See conversation_projection.py's own
    module docstring for why this can never fabricate a missing turn."""
    db_path = os.path.join(PROVENANCE_DB_DIR, "anaxi_provenance.db")
    return conversation_display.display_messages(
        conversation_projection.project_waking_conversation(
            db_path, include_event_metadata=True, **_authenticated_projection_kwargs()))


SLEEP_CONTROL_TRACE = "sleep_control_trace.jsonl"


def _trace_sleep_control(action, request_id, status, detail=None):
    """Append-only host evidence of every owner Sleep control attempt -- including refusals, which the
    canonical ledger (rightly) never records.  Live 2026-09-24: an Authorize click left no durable
    record at all and its cause could not be established afterwards.  Never content; best effort."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "owner_actor_id": BOUND_HUMAN_ACTOR_ID,
              "action": action, "request_id": request_id, "status": status, "detail": detail}
    try:
        with open(os.path.join(PROVENANCE_DB_DIR, SLEEP_CONTROL_TRACE), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def refresh_sleep_controls(note=None, keep=None):
    """Read-only discovery for the ordinary owner Sleep surface.  ``keep``: the request the owner just
    acted on stays selected while it is still listed -- the selection never silently moves to another
    request after an action."""
    if BOUND_HUMAN_ACTOR_ID is None:
        return gr.update(choices=[], value=None, interactive=False), (
            "**Sleep control:** no explicitly bound human owner for this process."
        )
    try:
        receipts = sleep_owner_control.list_requests(PROVENANCE_DB_DIR)
    except Exception as exc:
        return gr.update(choices=[], value=None, interactive=False), f"**Sleep control unavailable:** {exc}"
    choices = [
        (f"{receipt['state']} — {receipt['request_id']}", receipt["request_id"])
        for receipt in receipts
    ]
    by_id = {receipt["request_id"]: receipt for receipt in receipts}
    selected = keep if keep in by_id else (choices[0][1] if choices else None)
    status = "**Sleep control:** no actionable Clark-originated request."
    if receipts:
        status = f"**Sleep request:** `{selected}` — {by_id[selected]['state']}"
    if note:
        status += f"  \n*{note}*"
    return gr.update(choices=choices, value=selected, interactive=bool(choices)), status


def apply_sleep_owner_action(request_id, action):
    if not request_id or BOUND_HUMAN_ACTOR_ID is None:
        _trace_sleep_control(action, request_id, "refused", "no bound owner or no request selected")
        return refresh_sleep_controls("No bound owner/actionable request.")
    try:
        sleep_owner_control.apply_owner_action(
            PROVENANCE_DB_DIR, request_id=request_id,
            owner_actor_id=BOUND_HUMAN_ACTOR_ID, action=action,
        )
        _trace_sleep_control(action, request_id, "recorded")
        return refresh_sleep_controls(f"Owner action {action} recorded.", keep=request_id)
    except Exception as exc:
        _trace_sleep_control(action, request_id, "refused", str(exc))
        return refresh_sleep_controls(f"Owner action refused: {exc}", keep=request_id)


@_holding_waking_lock
def execute_sleep_from_owner_surface(request_id, confirmation):
    if not request_id or BOUND_HUMAN_ACTOR_ID is None:
        _trace_sleep_control("EXECUTE", request_id, "refused", "no bound owner or no request selected")
        dropdown, status = refresh_sleep_controls("No bound owner/actionable request.")
        return dropdown, status, False
    try:
        import sleep_cycle
        outcome = sleep_owner_control.execute_authorized_request(
            PROVENANCE_DB_DIR, request_id=request_id,
            owner_actor_id=BOUND_HUMAN_ACTOR_ID,
            confirmation=confirmation,
            background_activity_running=workspace_roaming.is_worker_running(),
            run_sleep_cycle=sleep_cycle.run_sleep_cycle_with_production_defaults,
        )
        _trace_sleep_control("EXECUTE", request_id, "attempt_recorded", outcome.get("outcome"))
        dropdown, status = refresh_sleep_controls(
            f"Sleep attempt recorded as {outcome['outcome']}.", keep=request_id
        )
    except Exception as exc:
        _trace_sleep_control("EXECUTE", request_id, "refused", str(exc))
        dropdown, status = refresh_sleep_controls(f"Sleep execution refused: {exc}", keep=request_id)
    return dropdown, status, False


WAKING_RECOVERY_CONFIRM_LABEL = (
    "I explicitly confirm one real same-input waking recovery attempt"
)


def _recovery_confirm_update(bound_h):
    """Reset the confirmation and name the exact H it would authorize."""
    label = WAKING_RECOVERY_CONFIRM_LABEL + (f" for H {bound_h}" if bound_h else "")
    return gr.update(value=False, label=label, interactive=bool(bound_h))


def load_caret_owner_status():
    """Owner-only, read-only Caret status from canonical records (see caret_owner_status).
    Never wakes, dispatches, writes or calls a model; never enters Clark's conversation."""
    try:
        statuses = caret_owner_status.caret_owner_status(
            PROVENANCE_DB_DIR,
            trace_path=os.path.join(PROVENANCE_DB_DIR, caret_wake.DEFAULT_TRACE_PATH),
        )
        label, body = caret_owner_status.render_owner_status(statuses)
    except Exception as exc:
        label, body = "Caret correspondence — status unavailable", f"Status unavailable: {exc}"
    return gr.update(label=label), body


def load_caret_correspondents(note=None):
    """Owner-only, read-only view of Discord author -> principal bindings and held (undeliverable)
    authors.  No model call, no network, no write; never shown to Clark."""
    try:
        view = caret_correspondent_admin.load_view(PROVENANCE_DB_DIR)
    except Exception as exc:
        return (f"Correspondent mapping unavailable: {exc}",
                gr.update(choices=[], value=None), gr.update(choices=[], value=None))
    text = view["markdown"] + (f"\n\n*{note}*" if note else "")
    return (
        text,
        gr.update(choices=view["principals"], value=(view["principals"][0][1] if view["principals"] else None)),
        gr.update(choices=view["active_author_ids"], value=None),
    )


def map_caret_correspondent(author_id, principal_id, display_label):
    """Explicit owner act through the owner-gated ledger (never Clark)."""
    if not BOUND_HUMAN_ACTOR_ID:
        return load_caret_correspondents("No explicitly bound human owner for this process.")
    _ok, note = caret_correspondent_admin.map_author(
        PROVENANCE_DB_DIR, requester_actor_id=BOUND_HUMAN_ACTOR_ID, discord_author_id=author_id,
        principal_actor_id=principal_id, display_label=display_label,
    )
    return load_caret_correspondents(note)


def revoke_caret_correspondent(author_id):
    if not BOUND_HUMAN_ACTOR_ID:
        return load_caret_correspondents("No explicitly bound human owner for this process.")
    _ok, note = caret_correspondent_admin.revoke_author(
        PROVENANCE_DB_DIR, requester_actor_id=BOUND_HUMAN_ACTOR_ID, discord_author_id=author_id,
    )
    return load_caret_correspondents(note)


@_holding_waking_lock
def write_owner_shared_note(title, text):
    """Owner act: author ONE note into the shared Obsidian vault with canonical owner authorship
    (obsidian_workspace.author_owner_note).  Clears the fields only when the note was written."""
    try:
        paths = workspace_capability.WorkspacePaths.production_defaults()
        if not paths.notes_available():
            return "*The shared vault is not available (not configured or not present); nothing was written.*", title, text
        ok, message = obsidian_workspace.author_owner_note(
            PROVENANCE_DB_DIR, workspace=obsidian_workspace.ObsidianWorkspace(paths.notes_dir),
            authority=BOUND_HUMAN_AUTHORITY, pipeline_key=PIPELINE_KEY, title=title, text=text)
    except Exception as exc:
        ok, message = False, f"Not written: {exc}"
    return f"*{message}*", ("" if ok else title), ("" if ok else text)


def load_correspondence_admin(note=None):
    """Owner-only, read-only view of correspondence doors, correspondents (standing read-only), held and
    pending correspondence.  No model call, no network, no write; never shown to Clark."""
    try:
        view = correspondence_admin.load_view(PROVENANCE_DB_DIR)
    except Exception as exc:
        empty = gr.update(choices=[], value=None)
        return f"Correspondence administration unavailable: {exc}", empty, empty, empty
    text = view["markdown"] + (f"\n\n*{note}*" if note else "")
    return (text,
            gr.update(choices=view["destinations"], value=(view["destinations"][0][1] if view["destinations"] else None)),
            gr.update(choices=view["occasions"], value=None),
            gr.update(choices=view["pending_sends"], value=None))


def load_door_policy(destination_id):
    """Read-only: the selected destination's DURABLE door state, for the two door checkboxes."""
    try:
        allow, permit = correspondence_admin.door_policy(PROVENANCE_DB_DIR, destination_id)
    except Exception:
        return gr.update(), gr.update()
    return gr.update(value=allow), gr.update(value=permit)


def _owner_correspondence_act(fn, **kwargs):
    if not BOUND_HUMAN_ACTOR_ID:
        return load_correspondence_admin("No explicitly bound human owner for this process.")
    _ok, note = fn(PROVENANCE_DB_DIR, requester_actor_id=BOUND_HUMAN_ACTOR_ID, **kwargs)
    return load_correspondence_admin(note)


def set_correspondence_door(destination_id, allow_unknown, permit_initiation):
    return _owner_correspondence_act(correspondence_admin.set_door, destination_id=destination_id,
                                     allow_unknown_sources=allow_unknown, permit_independent_initiation=permit_initiation)


def authorize_correspondence_channel(channel_id, label):
    return _owner_correspondence_act(correspondence_admin.authorize_channel, discord_channel_id=channel_id,
                                     display_label=label)


def revoke_correspondence_destination(destination_id):
    return _owner_correspondence_act(correspondence_admin.revoke_destination, destination_id=destination_id)


def record_correspondent_identity(user_id, name):
    return _owner_correspondence_act(correspondence_admin.record_identity, discord_user_id=user_id, name=name)


def block_correspondence_source(user_id):
    return _owner_correspondence_act(correspondence_admin.set_block, discord_user_id=user_id, blocked=True)


def unblock_correspondence_source(user_id):
    return _owner_correspondence_act(correspondence_admin.set_block, discord_user_id=user_id, blocked=False)


def close_correspondence_occasion(inbound_event_id):
    return _owner_correspondence_act(correspondence_admin.occasion_act, inbound_event_id=inbound_event_id, action="close")


def reopen_correspondence_occasion(inbound_event_id):
    return _owner_correspondence_act(correspondence_admin.occasion_act, inbound_event_id=inbound_event_id, action="reopen")


def close_pending_correspondence_send(send_turn_event_id):
    return _owner_correspondence_act(correspondence_admin.close_pending_send, send_turn_event_id=send_turn_event_id)


WAKING_RECOVERY_RECENT_COUNT = 3


def _recovery_header(receipts):
    """Presentation only.  Count and attention come from the canonical receipts
    (list_candidates -> assess_eligibility) that the selector itself uses; nothing
    is recomputed.  Open by default only when the NEWEST evidenced H is ELIGIBLE."""
    eligible = sum(1 for r in receipts if r["decision"] == "ELIGIBLE")
    needs_attention = bool(receipts) and receipts[0]["decision"] == "ELIGIBLE"
    return f"Waking recovery — {eligible} eligible", needs_attention


def _recovery_receipts():
    if BOUND_HUMAN_AUTHORITY is None:
        return None
    try:
        return waking_recovery_control.list_candidates(_PROVENANCE_DB_PATH, BOUND_HUMAN_ACTOR_ID)
    except Exception:
        return None


def load_waking_recovery_header():
    receipts = _recovery_receipts()
    if receipts is None:
        return gr.update(label="Waking recovery — unavailable", open=False)
    label, needs_attention = _recovery_header(receipts)
    return gr.update(label=label, open=needs_attention)


def refresh_waking_recovery_header():
    """Label only: never opens or closes what the owner has expanded."""
    receipts = _recovery_receipts()
    if receipts is None:
        return gr.update(label="Waking recovery — unavailable")
    return gr.update(label=_recovery_header(receipts)[0])


def _recovery_surface(selected, note=None, show_older=False):
    """Single derivation of every recovery-surface value from ONE selection.

    Returns (dropdown_update, status, confirm_update, bound_h).  The dropdown
    value, the status line, the confirmation label and bound_h (the only H
    execution will accept) are all computed from `selected` and one fresh
    read of canonical evidence.  A selection that is absent, no longer
    evidenced, or no longer ELIGIBLE yields bound_h=None and is never
    replaced by another H: the owner selects explicitly, or nothing runs.
    """
    if BOUND_HUMAN_AUTHORITY is None:
        return (gr.update(choices=[], value=None, interactive=False), (
            "**Waking recovery:** no explicitly bound human owner for this process."
        ), _recovery_confirm_update(None), None)
    try:
        receipts = waking_recovery_control.list_candidates(
            _PROVENANCE_DB_PATH, BOUND_HUMAN_ACTOR_ID,
        )
    except Exception as exc:
        return (gr.update(choices=[], value=None, interactive=False), (
            f"**Waking recovery unavailable:** {exc}"
        ), _recovery_confirm_update(None), None)
    # Newest-first receipts: the recent few are listed; older evidenced H stay reachable
    # behind "Show older events".  A currently selected H is always kept listed.
    visible = [
        r for i, r in enumerate(receipts)
        if show_older or i < WAKING_RECOVERY_RECENT_COUNT or r["human_input_event_id"] == selected
    ]
    choices = [
        (
            f"{receipt['decision']} — {receipt['basis']} — "
            f"{receipt['human_input_event_id']}",
            receipt["human_input_event_id"],
        )
        for receipt in visible
    ]
    by_id = {receipt["human_input_event_id"]: receipt for receipt in receipts}
    eligible_count = sum(1 for r in receipts if r["decision"] == "ELIGIBLE")
    summary = f"{len(receipts)} evidenced H event(s), {eligible_count} eligible"
    if len(visible) < len(receipts):
        summary += f" ({len(receipts) - len(visible)} older hidden)"
    bound_h = None
    shown = None
    if not receipts:
        status = "**Waking recovery:** no canonically evidenced failed H events."
    elif not selected:
        status = (
            f"**Waking recovery:** {summary}; no H selected. "
            "Select one to see its eligibility."
        )
    elif selected not in by_id:
        status = (
            f"**Waking recovery:** {summary}; selected H `{selected}` is no longer "
            "an evidenced H. Nothing is selected and nothing can run."
        )
    else:
        shown = selected
        receipt = by_id[selected]
        if receipt["decision"] == "ELIGIBLE":
            bound_h = selected
            status = (
                f"**Waking recovery:** {summary}; selected eligible H `{selected}` "
                f"({receipt['basis']})."
            )
        else:
            status = (
                f"**Waking recovery:** {summary}; selected H `{selected}` is "
                f"{receipt['decision']} ({receipt['basis']}); it cannot be recovered."
            )
    if note:
        status += f"  \n*{note}*"
    return (
        gr.update(choices=choices, value=shown, interactive=bool(choices)),
        status, _recovery_confirm_update(bound_h), bound_h,
    )


def refresh_waking_recovery_controls(selected=None, note=None):
    """Read-only discovery.  Preserves `selected` when still evidenced;
    never picks a default (initial load selects nothing)."""
    dropdown, status, _confirm, _bound = _recovery_surface(selected, note)
    return dropdown, status


def render_waking_recovery_selection(selected):
    """Selector change: re-derive status, confirmation and the bound H from
    the visibly selected value, so none of them can describe another H."""
    _dropdown, status, confirm, bound_h = _recovery_surface(selected)
    return status, confirm, bound_h


def load_waking_recovery_surface():
    return _recovery_surface(None)


def toggle_older_waking_recovery(selected, show_older):
    """Read-only list expansion/collapse.  Keeps `selected` only if still evidenced."""
    dropdown, status, confirm, bound_h = _recovery_surface(selected, None, bool(show_older))
    return dropdown, status, confirm, bound_h


@_holding_waking_lock
def execute_waking_recovery_from_owner_surface(human_input_event_id, confirmation, bound_h,
                                               show_older=False):
    """Execute one explicit same-H recovery through the real waking path.

    Fails closed unless the visibly selected H, the H the status/confirmation
    were rendered for (bound_h) and a fresh read of canonical eligibility all
    name the same H.  That single H is what is authorized and executed."""
    if not human_input_event_id or BOUND_HUMAN_AUTHORITY is None:
        dropdown, status, confirm, bound = _recovery_surface(
            human_input_event_id, "No bound owner/evidenced H was selected.", show_older,
        )
        return dropdown, status, False, refresh_waking_conversation(), bound
    if bound_h != human_input_event_id:
        dropdown, status, confirm, bound = _recovery_surface(
            human_input_event_id,
            "Refused: the confirmation was not given for this exact H. "
            "Re-confirm with this H selected.", show_older,
        )
        return dropdown, status, False, refresh_waking_conversation(), bound

    orch = None
    bound = None
    try:
        fresh = {r["human_input_event_id"]: r for r in waking_recovery_control.list_candidates(
            _PROVENANCE_DB_PATH, BOUND_HUMAN_ACTOR_ID)}
        receipt = fresh.get(human_input_event_id)
        if receipt is None or receipt["decision"] != "ELIGIBLE":
            raise RuntimeError("selected H is no longer an eligible evidenced H")
        orch = AnaxiOrchestrator(DB_PATH)

        def generation_fn(prompt):
            return run_waking_turn(
                orch, prompt, interaction_mode=CONVERSATION_MODE,
                human_input_authority=BOUND_HUMAN_AUTHORITY,
                existing_human_input_event_id=human_input_event_id,
            )

        outcome = waking_recovery_control.execute_candidate(
            _PROVENANCE_DB_PATH,
            human_input_event_id=human_input_event_id,
            actor_id=BOUND_HUMAN_ACTOR_ID,
            confirmation=confirmation,
            background_activity_running=workspace_roaming.is_worker_running(),
            expected_visibility_scope=BOUND_HUMAN_AUTHORITY.visibility_scope,
            reset_fn=lambda: wtr0_cold_reset.cold_reset_waking_inference_path(
                ollama, MODEL,
            ),
            generation_fn=generation_fn,
        )
        dropdown, status, _confirm, bound = _recovery_surface(
            human_input_event_id,
            f"Recovery finished with terminal state {outcome['terminal_state']}.",
            show_older,
        )
    except Exception as exc:
        dropdown, status, _confirm, bound = _recovery_surface(
            human_input_event_id,
            f"Recovery refused or failed before execution: {exc}",
            show_older,
        )
    finally:
        if orch is not None:
            orch.close()
    return dropdown, status, False, refresh_waking_conversation(), bound


def stage_waking_submission(message, displayed_history):
    """Display pending input without assigning it an invented occurrence time."""
    if not message or not message.strip():
        raise gr.Error("Enter a message first.")
    pending = list(displayed_history or []) + [{'role':'user','content':message}]
    return gr.update(value='', interactive=False), gr.update(interactive=False), message, pending


def submit_waking_projection(message):
    """The existing respond path receives only raw input and raw canonical history.

    Both successful new messages and historical hydration use the same canonical
    display projection. A contained host failure is ephemeral, uses its recorded
    callback time, and cannot acquire a fabricated canonical X identity.
    """
    db_path = os.path.join(PROVENANCE_DB_DIR, 'anaxi_provenance.db')
    raw_history = conversation_projection.project_waking_conversation(
        db_path, **_authenticated_projection_kwargs())
    reply, status = respond(message, raw_history)
    projected = refresh_waking_conversation()
    if status:
        metadata = ui_turn_diagnostics.get_last_callback_display_metadata()
        status = conversation_display.display_host_status(status, metadata.get('timestamp_end'))
    return projected, status, refresh_conversation_lead(), refresh_conversation_lead_request()


with gr.Blocks(title=f"Anaxi -- {MODEL} -- Interaction mode: {INTERACTION_MODE_LABEL}") as demo:
    gr.Markdown(
        f"### Anaxi -- {MODEL}\n"
        f"Interaction mode: {INTERACTION_MODE_LABEL} -- fixed for this process, "
        f"set at launch via `python llama_gui.py [--conversation]`, never by "
        f"anything typed here."
    )
    # OWC7-P2 gate B: family-facing "Conversation lead" surface --
    # direction_owner/direction_request themselves are completely
    # unchanged; this is presentation only (spec section B1-B13).
    # Current lead and any outstanding lead REQUEST are always shown
    # as two visually separate elements (spec B5: request != state) --
    # never combined into one string, so "Clark is asking to lead"
    # can never be mistaken for "Conversation lead: Clark".
    conversation_lead_display = gr.Markdown(refresh_conversation_lead())
    conversation_lead_request_display = gr.Markdown(refresh_conversation_lead_request())
    with gr.Row():
        take_direction_btn = gr.Button("Take the lead")
        give_direction_btn = gr.Button("Let Clark lead")
        open_direction_btn = gr.Button("Open")
    # Serialize explicit controls with waking submission: a turn must not
    # publish an older working-set snapshot over a human control click.
    take_direction_btn.click(fn=take_direction, inputs=None, outputs=conversation_lead_display,
                             concurrency_limit=1, concurrency_id="ordinary-waking-ui").then(
        fn=refresh_conversation_lead_request, inputs=None, outputs=conversation_lead_request_display)
    give_direction_btn.click(fn=give_direction_to_clark, inputs=None, outputs=conversation_lead_display,
                             concurrency_limit=1, concurrency_id="ordinary-waking-ui").then(
        fn=refresh_conversation_lead_request, inputs=None, outputs=conversation_lead_request_display)
    open_direction_btn.click(fn=open_direction, inputs=None, outputs=conversation_lead_display,
                             concurrency_limit=1, concurrency_id="ordinary-waking-ui").then(
        fn=refresh_conversation_lead_request, inputs=None, outputs=conversation_lead_request_display)

    # WSP2-P5 (spec sections 2/12/13): human control is now an
    # administrative pause/resume of the BACKGROUND pathway only --
    # never framed as granting/revoking access to Your Space itself.
    roaming_status_display = gr.Markdown(refresh_roaming_status())
    with gr.Row():
        pause_background_btn = gr.Button("Pause background activity")
        resume_background_btn = gr.Button("Resume background activity")
    pause_background_btn.click(fn=pause_background_activity, inputs=None, outputs=roaming_status_display)
    resume_background_btn.click(fn=resume_background_activity, inputs=None, outputs=roaming_status_display)

    # OWC7-S2/WSP2-S1 spec: reconnect must refresh displayed status
    # from authoritative state -- demo.load() fires on every page load,
    # including a browser reconnect/reload, and both refresh functions
    # are read-only (see their own docstrings).
    demo.load(fn=refresh_conversation_lead, inputs=None, outputs=conversation_lead_display)
    demo.load(fn=refresh_conversation_lead_request, inputs=None, outputs=conversation_lead_request_display)
    demo.load(fn=refresh_roaming_status, inputs=None, outputs=roaming_status_display)

    # Clark originates REQUEST/KNOCK/WITHDRAW through waking. These controls
    # are a separate, explicit owner boundary: refresh is read-only, owner
    # disposition requires a click, and execution additionally requires a
    # checked confirmation and a stopped background worker.
    sleep_request_selector = gr.Dropdown(label="Clark-originated Sleep request")
    sleep_control_status = gr.Markdown()
    with gr.Row():
        authorize_sleep_btn = gr.Button("Authorize Sleep")
        defer_sleep_btn = gr.Button("Defer Sleep")
        decline_sleep_btn = gr.Button("Decline Sleep")
    confirm_real_sleep = gr.Checkbox(label="I explicitly confirm one real Sleep cycle attempt")
    execute_sleep_btn = gr.Button("Execute one authorized Sleep cycle")
    authorize_sleep_btn.click(
        fn=lambda request_id: apply_sleep_owner_action(request_id, "AUTHORIZE"),
        inputs=sleep_request_selector, outputs=[sleep_request_selector, sleep_control_status],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui",
    )
    defer_sleep_btn.click(
        fn=lambda request_id: apply_sleep_owner_action(request_id, "DEFER"),
        inputs=sleep_request_selector, outputs=[sleep_request_selector, sleep_control_status],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui",
    )
    decline_sleep_btn.click(
        fn=lambda request_id: apply_sleep_owner_action(request_id, "DECLINE"),
        inputs=sleep_request_selector, outputs=[sleep_request_selector, sleep_control_status],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui",
    )
    execute_sleep_btn.click(
        fn=execute_sleep_from_owner_surface,
        inputs=[sleep_request_selector, confirm_real_sleep],
        outputs=[sleep_request_selector, sleep_control_status, confirm_real_sleep],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui",
    )
    demo.load(
        fn=refresh_sleep_controls, inputs=None,
        outputs=[sleep_request_selector, sleep_control_status],
    )

    # OWC7-P2: a genuinely separate, host-owned status surface for a
    # contained conversational-direction control failure -- the same
    # kind of component (gr.Markdown, updated only by explicit host
    # logic, never by the model) as direction_status_display and
    # roaming_status_display above. Wired to respond()'s second return
    # value via the explicit submission outputs below. Host diagnostics
    # never occupy a Clark message bubble in this surface.
    control_failure_status_display = gr.Markdown(CONTROL_FAILURE_STATUS_NEUTRAL)

    # PRODUCTION WAKING CONVERSATION RECOVERY gate: the chat window's
    # own gr.Chatbot is constructed explicitly (rather than letting
    # ChatInterface build an empty one implicitly) so its initial
    # `value` can be seeded from canonical provenance, and so a
    # demo.load() hook -- the exact same pattern already used for
    # conversation_lead_display/roaming_status_display above -- can
    # re-seed it on every browser reconnect/reload, not just process
    # start. type="messages" matches conversation_projection.py's own
    # {"role": ..., "content": ...} output shape directly.
    # PRODUCTION LIVE UI HYDRATION FIX (production incident, 2026-09-05):
    # gr.Chatbot's own group_consecutive_messages defaults to True --
    # "consecutive messages from the same role" are rendered as ONE
    # bubble. Canonical history includes hundreds of pre-A4c historical
    # waking_turn (X) events with no paired human_waking_input (H) --
    # conversation_projection.py correctly, honestly projects each as
    # its own separate assistant-role dict (never fabricating a missing
    # human turn to pair them with -- see its own module docstring), but
    # that produced a long unbroken run of consecutive assistant-role
    # messages, which Gradio's default then visually collapsed into one
    # enormous bubble. The canonical data was never wrong or mutated;
    # only the display grouping was. group_consecutive_messages=False
    # is a pure presentation setting -- every message renders in its
    # own bubble, exactly matching the underlying canonical turns.
    waking_conversation_chatbot = gr.Chatbot(
        value=refresh_waking_conversation(), label="Clark", group_consecutive_messages=False,
        elem_classes=["anaxi-conversation"],
    )
    demo.load(fn=refresh_waking_conversation, inputs=None, outputs=waking_conversation_chatbot)

    # WTR0: every canonically evidenced failed H is enumerated here, not a
    # newest-N sample.  Selection is read-only; a real attempt additionally
    # requires a checked confirmation, an exact bound actor/scope match, and a
    # stopped background worker.  The handler then cold-resets the model
    # runtime and re-enters the authoritative run_waking_turn() path with the
    # same durable H.  Its final output rehydrates this same canonical chat.
    # Compact by default: collapsed unless the newest evidenced H is ELIGIBLE; header count is
    # the canonical eligible count.  Opening it invokes nothing.
    with gr.Accordion("Waking recovery", open=False) as waking_recovery_accordion:
        waking_recovery_selector = gr.Dropdown(label="Evidenced waking input recovery")
        waking_recovery_show_older = gr.Checkbox(label="Show older events", value=False)
        waking_recovery_status = gr.Markdown()
        # The one H that status, confirmation and execution all agree on.
        waking_recovery_bound_h = gr.State(None)
        confirm_waking_recovery = gr.Checkbox(label=WAKING_RECOVERY_CONFIRM_LABEL)
        execute_waking_recovery_btn = gr.Button("Recover selected waking input once")
    execute_waking_recovery_btn.click(
        fn=execute_waking_recovery_from_owner_surface,
        inputs=[waking_recovery_selector, confirm_waking_recovery,
                waking_recovery_bound_h, waking_recovery_show_older],
        outputs=[waking_recovery_selector, waking_recovery_status,
                 confirm_waking_recovery, waking_conversation_chatbot,
                 waking_recovery_bound_h],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui",
    ).then(
        fn=refresh_waking_recovery_header, inputs=None, outputs=waking_recovery_accordion,
        show_progress="hidden", api_visibility="private",
    )
    waking_recovery_selector.change(
        fn=render_waking_recovery_selection, inputs=waking_recovery_selector,
        outputs=[waking_recovery_status, confirm_waking_recovery,
                 waking_recovery_bound_h],
        show_progress="hidden", api_visibility="private",
    )
    waking_recovery_show_older.change(
        fn=toggle_older_waking_recovery,
        inputs=[waking_recovery_selector, waking_recovery_show_older],
        outputs=[waking_recovery_selector, waking_recovery_status,
                 confirm_waking_recovery, waking_recovery_bound_h],
        show_progress="hidden", api_visibility="private",
    )
    demo.load(
        fn=load_waking_recovery_surface, inputs=None,
        outputs=[waking_recovery_selector, waking_recovery_status,
                 confirm_waking_recovery, waking_recovery_bound_h],
    )
    demo.load(
        fn=load_waking_recovery_header, inputs=None, outputs=waking_recovery_accordion,
        show_progress="hidden", api_visibility="private",
    )

    # Caret owner observability: a quiet, collapsed, read-only panel.  Derived on load and on
    # the owner's explicit Refresh only -- no timer, no model call, never shown to Clark.
    with gr.Accordion("Caret correspondence", open=False) as caret_status_accordion:
        caret_status_markdown = gr.Markdown()
        caret_status_refresh_btn = gr.Button("Refresh Caret status", size="sm")
    caret_status_refresh_btn.click(
        fn=load_caret_owner_status, inputs=None,
        outputs=[caret_status_accordion, caret_status_markdown],
        show_progress="hidden", api_visibility="private",
    )
    demo.load(
        fn=load_caret_owner_status, inputs=None,
        outputs=[caret_status_accordion, caret_status_markdown],
        show_progress="hidden", api_visibility="private",
    )

    with gr.Accordion("Caret correspondents", open=False) as caret_correspondents_accordion:
        caret_map_markdown = gr.Markdown()
        with gr.Row():
            caret_map_author_id = gr.Textbox(label="Discord user id (digits)", scale=3)
            caret_map_principal = gr.Dropdown(label="ANAXI principal", choices=[], scale=3)
            caret_map_label = gr.Textbox(label="Label (optional)", scale=2)
            caret_map_btn = gr.Button("Map", size="sm", scale=1)
        with gr.Row():
            caret_revoke_author = gr.Dropdown(label="Mapped Discord id", choices=[], scale=6)
            caret_revoke_btn = gr.Button("Revoke", size="sm", scale=1)
            caret_map_refresh_btn = gr.Button("Refresh", size="sm", scale=1)
    _caret_map_outputs = [caret_map_markdown, caret_map_principal, caret_revoke_author]
    caret_map_btn.click(
        fn=map_caret_correspondent, inputs=[caret_map_author_id, caret_map_principal, caret_map_label],
        outputs=_caret_map_outputs, show_progress="hidden", api_visibility="private",
    )
    caret_revoke_btn.click(
        fn=revoke_caret_correspondent, inputs=[caret_revoke_author],
        outputs=_caret_map_outputs, show_progress="hidden", api_visibility="private",
    )
    # The panel is a read of canonical state at one moment.  A page loaded earlier (another tab or the desktop
    # window) would otherwise keep showing that older moment, so it is re-read on load, on opening the panel,
    # and on an explicit Refresh -- never a source of truth itself.
    for _caret_reload_trigger in (demo.load, caret_correspondents_accordion.expand, caret_map_refresh_btn.click):
        _caret_reload_trigger(
            fn=load_caret_correspondents, inputs=None, outputs=_caret_map_outputs,
            show_progress="hidden", api_visibility="private",
        )

    with gr.Accordion("Correspondence doors & lifecycle", open=False) as correspondence_accordion:
        correspondence_markdown = gr.Markdown()
        with gr.Row():
            corr_channel_id = gr.Textbox(label="Discord channel id (digits)", scale=4)
            corr_channel_label = gr.Textbox(label="Label", scale=3)
            corr_authorize_btn = gr.Button("Authorize channel", size="sm", scale=1)
        with gr.Row():
            corr_destination = gr.Dropdown(label="Authorized destination", choices=[], scale=4)
            corr_allow_unknown = gr.Checkbox(label="Open to correspondents who are not ANAXI principals", scale=3)
            corr_permit_initiation = gr.Checkbox(label="Clark may write first (to correspondents he has standing with)", scale=3)
            corr_set_door_btn = gr.Button("Set door", size="sm", scale=1)
            corr_revoke_dest_btn = gr.Button("Revoke destination", size="sm", scale=1)
        with gr.Row():
            corr_block_id = gr.Textbox(label="Discord user id (digits)", scale=5)
            corr_block_btn = gr.Button("Block", size="sm", scale=1)
            corr_unblock_btn = gr.Button("Unblock", size="sm", scale=1)
            corr_identity_name = gr.Textbox(label="Who this id is (name)", scale=3)
            corr_identity_btn = gr.Button("Record identity", size="sm", scale=1)
        with gr.Row():
            corr_occasion = gr.Dropdown(label="Held inbound message", choices=[], scale=5)
            corr_close_occasion_btn = gr.Button("Close", size="sm", scale=1)
            corr_reopen_occasion_btn = gr.Button("Reopen (new retry budget)", size="sm", scale=1)
        with gr.Row():
            corr_pending_send = gr.Dropdown(label="Clark's undelivered send", choices=[], scale=5)
            corr_close_send_btn = gr.Button("Close (do not deliver)", size="sm", scale=1)
            corr_refresh_btn = gr.Button("Refresh", size="sm", scale=1)
    with gr.Accordion("Shared workspace: write a note", open=False):
        gr.Markdown("A new note in the shared Obsidian vault, recorded as yours (canonical owner authorship; "
                    "never overwrites an existing note).")
        owner_note_title = gr.Textbox(label="Title (the note's name)")
        owner_note_text = gr.Textbox(label="Text", lines=6)
        owner_note_btn = gr.Button("Write note to shared workspace", size="sm")
        owner_note_status = gr.Markdown()
    owner_note_btn.click(fn=write_owner_shared_note, inputs=[owner_note_title, owner_note_text],
                         outputs=[owner_note_status, owner_note_title, owner_note_text],
                         show_progress="hidden", api_visibility="private")
    _corr_outputs = [correspondence_markdown, corr_destination, corr_occasion, corr_pending_send]
    for _btn, _fn, _inputs in (
            (corr_authorize_btn, authorize_correspondence_channel, [corr_channel_id, corr_channel_label]),
            (corr_revoke_dest_btn, revoke_correspondence_destination, [corr_destination]),
            (corr_set_door_btn, set_correspondence_door, [corr_destination, corr_allow_unknown, corr_permit_initiation]),
            (corr_block_btn, block_correspondence_source, [corr_block_id]),
            (corr_identity_btn, record_correspondent_identity, [corr_block_id, corr_identity_name]),
            (corr_unblock_btn, unblock_correspondence_source, [corr_block_id]),
            (corr_close_occasion_btn, close_correspondence_occasion, [corr_occasion]),
            (corr_reopen_occasion_btn, reopen_correspondence_occasion, [corr_occasion]),
            (corr_close_send_btn, close_pending_correspondence_send, [corr_pending_send])):
        _btn.click(fn=_fn, inputs=_inputs, outputs=_corr_outputs, show_progress="hidden", api_visibility="private").then(
            fn=load_door_policy, inputs=[corr_destination], outputs=[corr_allow_unknown, corr_permit_initiation],
            show_progress="hidden", api_visibility="private")
    for _corr_trigger in (demo.load, correspondence_accordion.expand, corr_refresh_btn.click):
        _corr_trigger(fn=load_correspondence_admin, inputs=None, outputs=_corr_outputs,
                      show_progress="hidden", api_visibility="private").then(
            fn=load_door_policy, inputs=[corr_destination], outputs=[corr_allow_unknown, corr_permit_initiation],
            show_progress="hidden", api_visibility="private")
    # The door checkboxes always show the selected destination's durable policy (never a stale default).
    corr_destination.change(fn=load_door_policy, inputs=[corr_destination],
                            outputs=[corr_allow_unknown, corr_permit_initiation],
                            show_progress="hidden", api_visibility="private")

    # Explicit submission outputs let canonical hydration own the displayed H/X
    # timestamps, without feeding display blocks back as a new human message.
    pending_waking_input = gr.State('')
    with gr.Row():
        waking_input = gr.Textbox(placeholder="Message Clark", show_label=False, lines=2, scale=8)
        waking_send = gr.Button("Send", variant="primary", scale=1)
    staged = gr.on(
        triggers=[waking_input.submit, waking_send.click], fn=stage_waking_submission,
        inputs=[waking_input, waking_conversation_chatbot],
        outputs=[waking_input, waking_send, pending_waking_input, waking_conversation_chatbot],
        queue=False, api_visibility="private",
    )
    submitted = staged.success(
        fn=submit_waking_projection, inputs=pending_waking_input,
        outputs=[waking_conversation_chatbot, control_failure_status_display,
                 conversation_lead_display, conversation_lead_request_display],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui", api_visibility="private",
    )
    submitted.then(
        fn=lambda: (gr.update(interactive=True), gr.update(interactive=True)), inputs=None,
        outputs=[waking_input, waking_send], queue=False, api_visibility="private",
    )
    # A Sleep request Clark records during this turn is canonical only once the turn commits;
    # the owner surface otherwise re-reads it on page load alone, so re-read it here (read-only).
    submitted.then(
        fn=refresh_sleep_controls, inputs=None,
        outputs=[sleep_request_selector, sleep_control_status],
        concurrency_limit=1, concurrency_id="ordinary-waking-ui", api_visibility="private",
    )


_CARET_WAKE_SERVICE = None


def _owner_has_paused_background_activity():
    """The owner's Pause control (PAUSED_BY_HUMAN) also pauses Caret waking.  A
    Clark-placed pause governs his own roaming, not correspondence addressed to him."""
    state = workspace_roaming.get_roaming_state()
    return state.get("background_activity_state") == workspace_roaming.BACKGROUND_STATE_PAUSED_BY_HUMAN


def start_caret_wake_service():
    """Authorized inbound Caret correspondence creates its own waking occasion while
    this process runs, so Discord need not be relayed through a Mac turn.  Started
    from launch_production_backend() only, conversation mode only, idempotent."""
    global _CARET_WAKE_SERVICE
    if LAUNCH_MODE != CONVERSATION_MODE:
        return False
    if _CARET_WAKE_SERVICE is None:
        from llama_anaxi import run_caret_occasion_turn
        _CARET_WAKE_SERVICE = caret_wake.CaretWakeService(
            data_dir=PROVENANCE_DB_DIR, run_occasion=run_caret_occasion_turn,
            owner_paused=_owner_has_paused_background_activity,
            trace_path=os.path.join(PROVENANCE_DB_DIR, caret_wake.DEFAULT_TRACE_PATH),
        )
    return _CARET_WAKE_SERVICE.start()


def launch_production_backend(**launch_kwargs):
    """WSP2-P5 (spec section 6): the ONE shared "start the real
    backend" entrypoint every production launcher -- this file's own
    __main__ block below, llama_desktop.py, and llama_launch.py --
    calls INSTEAD OF calling demo.launch() directly. Centralizes "the
    process is now genuinely ready" in exactly one place, so background
    Space activity auto-starts on every normal launch regardless of
    which surface wraps it, WITHOUT any launcher file needing to import
    or reference workspace_roaming/roaming internals itself -- this
    preserves llama_launch.py's own pre-existing, deliberate boundary
    (see its test_launcher_never_references_workspace_roaming). No
    human grant action is required (spec sections 2/5). demo.launch()
    runs first, completely unchanged, with the exact kwargs the caller
    already passed; auto-start is only attempted if that call succeeds,
    and is itself best-effort -- a background-lifecycle bug here must
    never prevent the backend from starting.

    WSP2-P5-P1 (spec section 5): an unresolved Clark-owned pause is
    reconstructed from durable canonical truth FIRST, strictly before
    auto-start eligibility is ever evaluated -- process memory always
    starts empty on a fresh launch, so without this step a genuine
    prior Clark stop would be silently overridden by ordinary P5
    default availability on every restart."""
    launch_kwargs['css'] = (launch_kwargs.get('css') or '') + '\n' + conversation_display.TIMESTAMP_CSS
    demo.launch(**launch_kwargs)
    try:
        workspace_roaming.reconstruct_clark_owned_pause_from_canonical_truth(PROVENANCE_DB_DIR)
        _auto_start_background_activity()
    except Exception:
        pass
    try:
        start_caret_wake_service()
    except Exception:
        pass


if __name__ == "__main__":
    launch_production_backend()
