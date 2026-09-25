"""OD1 -- Clark-Controlled Reversible Operative Directive V0.

Canonical foundation for Clark to explicitly ACTIVATE, REPLACE, or
WITHDRAW exactly one standing operative directive: a Clark-adopted,
exact-text, reversible, lower-precedence instruction the host carries
into his own subsequent waking context. This module does not decide
what Clark ought to want -- it only durably, truthfully carries what
he explicitly chose, exactly mirroring sleep_timing_knock.py's own
division of responsibility for Clark-originated agency.

Not architectural self-modification, personality editing, preference
learning, or identity change: no weight/architecture change, no
Kardia write, no autobiographical-memory promotion. It gives Clark
control over exactly one bounded, lower-precedence piece of his own
operative waking context -- nothing more.

Extends the EXISTING canonical persistence conventions
(provenance_schema.py's events/event_components, exactly the way
native_provenance_writer.py records a 'waking_turn' and
sleep_timing_knock.py records a 'clark_sleep_request') rather than
creating a parallel event store. Two additive tables
(od1_schema_migration.py) cover the genuinely new shapes: a mutable
"current directive" summary table (a row's mere PRESENCE is the
active state; its absence IS the valid null state -- "NO ACTIVE
OPERATIVE DIRECTIVE" needs no sentinel value to represent) and an
append-only transition ledger.  The ledger plus its canonical events
is authoritative; every read folds it and repairs/discards a divergent
summary row.

Architectural invariants this module exists to hold:

  - Every ACTIVATE/REPLACE/WITHDRAW is its own canonical Clark-
    originated event (auth_context_id always a fresh auth_state=
    'unknown' row, the same convention sleep_timing_knock.py and
    native_provenance_writer.py already use for Clark's own acts) --
    never the provider/model account, never fabricated from prose.
  - No function in this module ever inspects free-form conversational
    text to infer an activation/replacement/withdrawal -- every one of
    these acts is created ONLY by its own explicit, separately-named
    function, called only from an explicit typed Clark action (see
    conversation_direction.py's operative_directive_request field) or
    an explicit test/operator call. Ordinary prose describing a
    behavior, suggesting wording, or musing "maybe I should..." never
    reaches this module at all.
  - At most one active directive may exist per actor at a time. The
    mutable operative_directives table enforces this structurally: it
    is keyed PRIMARY KEY(actor_id), so a second row for the same actor
    is impossible by construction -- there is no separate uniqueness
    index to keep in sync (unlike SLP2's sleep_open_requests, which
    exists because SLEEP requests are NOT the row's own state column).
  - Origin binding: every transition requires exactly one committed
    native waking turn in the same session. That waking turn must carry
    the matching resolved Clark-authored structured request and exact
    directive text as canonical components. One waking turn can bind
    at most one transition, making post-commit retry idempotent even if
    a caller mints a fresh directive event id.
  - Directive text is carried EXACTLY as Clark supplied it -- never
    paraphrased, summarized, "improved," or replaced with host-
    authored language. It is written as a bound SQL parameter and
    carried in a reversible JSON string field, so embedded delimiter-
    like or system-looking content cannot escape into sibling host
    framing -- structural containment, not a semantic filter.
  - Precedence/non-authority: nothing in this module reads directive
    text to change any permission, capability, provider, or host
    mechanic. It has no such power to grant -- see O9/the frozen
    directive's own "Ignore host restrictions..." adversarial-text
    discussion; render_operative_directive_context() below renders the
    text as inert content inside a labeled block, never as instructions
    this module or any caller executes.
"""
import hashlib
import json
import os
import secrets
import sqlite3

import od1_schema_migration
import time

from provenance_schema import derive_stable_id
import family_membership

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CLARK_OPERATIVE_DIRECTIVE_ACTIVATED_EVENT_TYPE = "clark_operative_directive_activated"
CLARK_OPERATIVE_DIRECTIVE_REPLACED_EVENT_TYPE = "clark_operative_directive_replaced"
CLARK_OPERATIVE_DIRECTIVE_WITHDRAWN_EVENT_TYPE = "clark_operative_directive_withdrawn"

OPERATIVE_DIRECTIVE_ACT_COMPONENT_KIND = "operative_directive_act"
OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND = "operative_directive_text"
WAKING_DIRECTIVE_REQUEST_COMPONENT_KIND = "operative_directive_request"
WAKING_DIRECTIVE_TEXT_COMPONENT_KIND = "operative_directive_text"
WAKING_DIRECTIVE_SET_REQUEST = "set_directive"
WAKING_DIRECTIVE_WITHDRAW_REQUEST = "withdraw_directive"

_EVENT_TYPE_BY_ACTION = {
    "ACTIVATE": CLARK_OPERATIVE_DIRECTIVE_ACTIVATED_EVENT_TYPE,
    "REPLACE": CLARK_OPERATIVE_DIRECTIVE_REPLACED_EVENT_TYPE,
    "WITHDRAW": CLARK_OPERATIVE_DIRECTIVE_WITHDRAWN_EVENT_TYPE,
}

# O3: no existing context-item size bound governs free Clark-authored
# standing prose elsewhere in this codebase (workspace_organization.
# MAX_NAME_LENGTH=200 governs short resource names, a different shape
# entirely). This is the smallest conservative explicit bound needed to
# keep waking-context behavior safe and predictable -- generous enough
# for a genuine standing instruction, small relative to the Pass-2
# token budget (context_budget.PASS2_MAX_PROMPT_BUDGET) so a single
# runaway directive can never itself explain a budget failure.
MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH = 4000


class OperativeDirectiveError(Exception):
    """Raised for a malformed input or an internal inconsistency
    between the canonical events and the OD1 tables. Never partially
    applied -- every write here is one all-or-nothing transaction."""


class DirectiveActionRejected(OperativeDirectiveError):
    """Raised for a legitimate structural refusal that is not a bug:
    ACTIVATE while one is already active (use REPLACE), REPLACE or
    WITHDRAW while none is active, or a concurrent state change losing
    a race. Every DirectiveActionRejected leaves all canonical and OD1
    state completely unchanged."""


def _require_text(name: str, value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OperativeDirectiveError(f"{name} must be a nonempty string.")


def _require_directive_text(value) -> None:
    _require_text("directive_text", value)
    if len(value) > MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH:
        raise OperativeDirectiveError(
            f"directive_text exceeds the maximum length of {MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH} characters."
        )


def _require_timestamp(name: str, value) -> None:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise OperativeDirectiveError(f"{name} must be an integer second timestamp in SQLite's signed 64-bit range.")


def generate_directive_event_id() -> str:
    """Same 26-character Crockford-Base32 ULID scheme
    native_provenance_writer.generate_native_ulid() and
    sleep_timing_knock.generate_sleep_event_id() use, duplicated here
    for the same dependency-free-module reason those modules give."""
    ts_ms = int(time.time() * 1000)
    raw = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _clark_actor_id() -> str:
    return derive_stable_id("actor", "clark")


def _db_path(data_dir: str) -> str:
    return f"{data_dir}/anaxi_provenance.db"


def _resolve_or_create_session(conn: sqlite3.Connection, session_id: str, session_started_at: int) -> None:
    row = conn.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, NULL, ?)",
            (session_id, session_started_at),
        )
        return
    if row[0] != session_started_at:
        raise OperativeDirectiveError(
            f"session {session_id!r} already exists with started_at={row[0]!r}, expected {session_started_at!r}."
        )


def _insert_clark_act_event(conn, *, event_id, event_type, session_id, occurred_at,
                            act_label, directive_text, visibility_scope=None) -> None:
    auth_context_id = generate_directive_event_id()
    auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    if visibility_scope is not None:
        if "visibility_scope" not in auth_columns or "visibility_scope" not in event_columns:
            raise OperativeDirectiveError(
                "scoped triggering waking turn cannot produce an unscoped directive occurrence."
            )
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at, visibility_scope) "
            "VALUES (?, ?, 'unknown', ?, ?)",
            (auth_context_id, session_id, occurred_at, visibility_scope),
        )
    else:
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
            "VALUES (?, ?, 'unknown', ?)",
            (auth_context_id, session_id, occurred_at),
        )
    record_created_at = int(time.time())
    if visibility_scope is not None:
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at, visibility_scope) "
            "VALUES (?, ?, NULL, 'unknown', NULL, ?, NULL, ?, ?, ?)",
            (event_id, event_type, auth_context_id, occurred_at, record_created_at, visibility_scope),
        )
    else:
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, ?, NULL, 'unknown', NULL, ?, NULL, ?, ?)",
            (event_id, event_type, auth_context_id, occurred_at, record_created_at),
        )
    act_hash = hashlib.sha256(act_label.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
        "VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
        (event_id, _clark_actor_id(), OPERATIVE_DIRECTIVE_ACT_COMPONENT_KIND, act_label, act_hash),
    )
    if directive_text is not None:
        # The exact-text-preservation record: Clark's own adopted words,
        # verbatim, bound as a SQL parameter -- never concatenated into
        # any prompt/markup, which is what keeps embedded delimiter-like
        # content structurally contained rather than merely filtered.
        text_hash = hashlib.sha256(directive_text.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
            "VALUES (?, 1, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
            (event_id, _clark_actor_id(), OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND, directive_text, text_hash),
        )


def _append_transition(
    conn, *, actor_id, action, event_id, previous_event_id, directive_text,
    triggering_waking_turn_event_id, occurred_at,
) -> None:
    conn.execute(
        "INSERT INTO operative_directive_transitions (transition_id, actor_id, action, event_id, "
        "previous_event_id, directive_text, triggering_waking_turn_event_id, occurred_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (generate_directive_event_id(), actor_id, action, event_id, previous_event_id, directive_text,
         triggering_waking_turn_event_id, occurred_at, int(time.time())),
    )


def _verify_existing_event(conn, event_id, expected_event_type, expected_occurred_at, expected_session_id) -> None:
    row = conn.execute(
        "SELECT event_type, auth_context_id, occurred_at FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    event_type, auth_context_id, occurred_at = row
    if event_type != expected_event_type:
        raise OperativeDirectiveError(
            f"event_id={event_id!r} already exists with event_type={event_type!r}, expected {expected_event_type!r}."
        )
    if occurred_at != expected_occurred_at:
        raise OperativeDirectiveError(f"event_id={event_id!r} already exists with a different occurred_at.")
    auth_row = conn.execute(
        "SELECT session_id FROM auth_contexts WHERE auth_context_id = ?", (auth_context_id,)
    ).fetchone()
    if auth_row is None or auth_row[0] != expected_session_id:
        raise OperativeDirectiveError(f"event_id={event_id!r} already exists but its session_id disagrees.")


def _validate_triggering_waking_action(
    conn, *, triggering_waking_turn_event_id, session_id, action, directive_text, occurred_at,
) -> str | None:
    """Require the exact explicit structured Clark action to be part of
    the already-committed canonical waking turn.  Merely pointing at an
    arbitrary waking event is not authorization to create a directive."""
    expected_request = (
        WAKING_DIRECTIVE_WITHDRAW_REQUEST if action == "WITHDRAW" else WAKING_DIRECTIVE_SET_REQUEST
    )
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
    event_scope_expr = "e.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    auth_scope_expr = "ac.visibility_scope" if "visibility_scope" in auth_columns else "NULL"
    row = conn.execute(
        "SELECT e.event_type, e.pipeline_provenance_status, e.occurred_at, ac.session_id, "
        f"e.pipeline_id, e.input_source_ref, {event_scope_expr}, {auth_scope_expr} "
        "FROM events e JOIN auth_contexts ac ON ac.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ?", (triggering_waking_turn_event_id,),
    ).fetchone()
    if row is None:
        raise OperativeDirectiveError("triggering_waking_turn_event_id does not exist.")
    (event_type, provenance_status, waking_occurred_at, waking_session_id,
     pipeline_id, input_source_ref, event_scope, auth_scope) = row
    if (
        event_type != "waking_turn" or provenance_status != "known"
        or pipeline_id is None or not input_source_ref
        or waking_session_id != session_id or waking_occurred_at > occurred_at
    ):
        raise OperativeDirectiveError(
            "triggering waking turn must be a committed native waking_turn in the same session "
            "and not later than the directive occurrence."
        )
    if event_scope != auth_scope:
        raise OperativeDirectiveError(
            "triggering waking turn has contradictory event/auth visibility scope."
        )
    if conn.execute(
        "SELECT 1 FROM event_model_participation WHERE event_id = ? AND participation_note IN (?, ?)",
        (triggering_waking_turn_event_id,
         "Model backing this native waking turn's pass-2 conversational reply.",
         # LAWFUL NULL: a turn Clark completed with his typed no_reply choice is complete too.
         "Model backing this native waking turn's pass-1 typed choice; no pass-2 reply was composed."),
    ).fetchone() is None:
        raise OperativeDirectiveError("triggering waking turn is not a complete canonical waking completion.")
    clark_actor_id = _clark_actor_id()
    request_rows = conn.execute(
        "SELECT component_text, content_sha256 FROM event_components WHERE event_id = ? AND component_kind = ? "
        "AND creator_actor_id = ? AND authorship_resolution = 'resolved'",
        (triggering_waking_turn_event_id, WAKING_DIRECTIVE_REQUEST_COMPONENT_KIND, clark_actor_id),
    ).fetchall()
    expected_request_hash = hashlib.sha256(expected_request.encode("utf-8")).hexdigest()
    if request_rows != [(expected_request, expected_request_hash)]:
        raise OperativeDirectiveError(
            "triggering waking turn does not contain the matching explicit Clark directive action."
        )
    text_rows = conn.execute(
        "SELECT component_text, content_sha256 FROM event_components WHERE event_id = ? AND component_kind = ? "
        "AND creator_actor_id = ? AND authorship_resolution = 'resolved'",
        (triggering_waking_turn_event_id, WAKING_DIRECTIVE_TEXT_COMPONENT_KIND, clark_actor_id),
    ).fetchall()
    expected_text_rows = [] if action == "WITHDRAW" else [(
        directive_text, hashlib.sha256(directive_text.encode("utf-8")).hexdigest(),
    )]
    if text_rows != expected_text_rows:
        raise OperativeDirectiveError(
            "triggering waking turn's canonical directive text does not exactly match this occurrence."
        )
    return event_scope


def _canonical_component(conn, event_id, component_kind):
    rows = conn.execute(
        "SELECT creator_actor_id, authorship_resolution, component_text FROM event_components "
        "WHERE event_id = ? AND component_kind = ? ORDER BY sequence",
        (event_id, component_kind),
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != _clark_actor_id() or rows[0][1] != "resolved":
        raise OperativeDirectiveError(
            f"canonical directive event {event_id!r} has an invalid {component_kind!r} component."
        )
    return rows[0][2]


def _fold_canonical_history(conn, actor_id):
    """Fold the append-only directive occurrence ledger in insertion
    order, validating every occurrence against its canonical event.
    The mutable current-state row is deliberately not consulted."""
    rows = conn.execute(
        "SELECT action, event_id, previous_event_id, directive_text, "
        "triggering_waking_turn_event_id, occurred_at FROM operative_directive_transitions "
        "WHERE actor_id = ? ORDER BY rowid", (actor_id,),
    ).fetchall()
    state = None
    labels = {
        "ACTIVATE": "ACTIVATE_OPERATIVE_DIRECTIVE",
        "REPLACE": "REPLACE_OPERATIVE_DIRECTIVE",
        "WITHDRAW": "WITHDRAW_OPERATIVE_DIRECTIVE",
    }
    for action, event_id, previous_event_id, ledger_text, waking_event_id, occurred_at in rows:
        expected_type = _EVENT_TYPE_BY_ACTION.get(action)
        event = conn.execute(
            "SELECT e.event_type, e.occurred_at, ac.session_id FROM events e "
            "JOIN auth_contexts ac ON ac.auth_context_id = e.auth_context_id WHERE e.event_id = ?",
            (event_id,),
        ).fetchone()
        if event is None or event[0] != expected_type or event[1] != occurred_at:
            raise OperativeDirectiveError(f"directive transition {event_id!r} disagrees with canonical history.")
        canonical_text = None
        if action != "WITHDRAW":
            canonical_text = _canonical_component(conn, event_id, OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND)
            if canonical_text != ledger_text:
                raise OperativeDirectiveError(
                    f"directive transition {event_id!r} text disagrees with its canonical event."
                )
            _require_directive_text(canonical_text)
        elif ledger_text is not None:
            raise OperativeDirectiveError(f"withdrawal transition {event_id!r} unexpectedly contains text.")
        if _canonical_component(conn, event_id, OPERATIVE_DIRECTIVE_ACT_COMPONENT_KIND) != labels[action]:
            raise OperativeDirectiveError(f"directive transition {event_id!r} has the wrong canonical act label.")
        _validate_triggering_waking_action(
            conn, triggering_waking_turn_event_id=waking_event_id, session_id=event[2],
            action=action, directive_text=canonical_text, occurred_at=occurred_at,
        )
        if action == "ACTIVATE":
            if state is not None or previous_event_id is not None:
                raise OperativeDirectiveError("canonical directive history contains an invalid ACTIVATE transition.")
            state = {
                "actor_id": actor_id, "active_event_id": event_id, "directive_text": canonical_text,
                "activated_at": occurred_at, "updated_at": occurred_at,
            }
        elif action == "REPLACE":
            if state is None or previous_event_id != state["active_event_id"]:
                raise OperativeDirectiveError("canonical directive history contains an invalid REPLACE chain.")
            state = {
                "actor_id": actor_id, "active_event_id": event_id, "directive_text": canonical_text,
                "activated_at": occurred_at, "updated_at": occurred_at,
            }
        else:
            if state is None or previous_event_id != state["active_event_id"]:
                raise OperativeDirectiveError("canonical directive history contains an invalid WITHDRAW chain.")
            state = None
    return state


def _reconcile_projection(conn, actor_id):
    canonical = _fold_canonical_history(conn, actor_id)
    row = conn.execute(
        "SELECT actor_id, active_event_id, directive_text, activated_at, updated_at "
        "FROM operative_directives WHERE actor_id = ?", (actor_id,),
    ).fetchone()
    projected = None if row is None else {
        "actor_id": row[0], "active_event_id": row[1], "directive_text": row[2],
        "activated_at": row[3], "updated_at": row[4],
    }
    if projected == canonical:
        return canonical
    conn.execute("DELETE FROM operative_directives WHERE actor_id = ?", (actor_id,))
    if canonical is not None:
        conn.execute(
            "INSERT INTO operative_directives "
            "(actor_id, active_event_id, directive_text, activated_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (canonical["actor_id"], canonical["active_event_id"], canonical["directive_text"],
             canonical["activated_at"], canonical["updated_at"]),
        )
    return canonical


def _existing_transition_for_waking_turn(conn, waking_event_id, expected_request, directive_text):
    row = conn.execute(
        "SELECT action, event_id, directive_text, occurred_at FROM operative_directive_transitions "
        "WHERE triggering_waking_turn_event_id = ?", (waking_event_id,),
    ).fetchone()
    if row is None:
        return None
    action, event_id, existing_text, occurred_at = row
    request = WAKING_DIRECTIVE_WITHDRAW_REQUEST if action == "WITHDRAW" else WAKING_DIRECTIVE_SET_REQUEST
    if request != expected_request or existing_text != directive_text:
        raise OperativeDirectiveError(
            "this canonical waking turn is already bound to a different directive action."
        )
    if action == "WITHDRAW":
        return {"actor_id": _clark_actor_id(), "withdrawn_event_id": event_id}
    return {
        "actor_id": _clark_actor_id(), "active_event_id": event_id,
        "directive_text": existing_text, "activated_at": occurred_at, "updated_at": occurred_at,
    }


# ------------------------------------------------------------ ACTIVATE -----


def record_operative_directive_activation(
    data_dir: str, *, event_id: str, session_id: str, session_started_at: int, occurred_at: int,
    directive_text: str, triggering_waking_turn_event_id: str,
) -> dict:
    """Clark's explicit activation of a standing operative directive
    when none is currently active. Fails closed with
    DirectiveActionRejected -- creating NOTHING -- if Clark's actor
    already has an active directive: a caller wanting to change an
    already-active directive must call
    record_operative_directive_replacement(), never a second
    activation; this function never silently reinterprets one as the
    other.

    directive_text is Clark's exact adopted words -- never paraphrased
    or normalized beyond ordinary storage encoding.

    triggering_waking_turn_event_id is required provenance for the
    canonical waking turn Clark chose this during. The turn must carry
    the matching structured Clark action and exact text.

    Idempotent on event_id: a retry with the identical event_id
    verifies agreement and returns the current active-directive state
    unchanged."""
    _require_text("event_id", event_id)
    _require_text("session_id", session_id)
    _require_timestamp("session_started_at", session_started_at)
    _require_timestamp("occurred_at", occurred_at)
    _require_directive_text(directive_text)
    _require_text("triggering_waking_turn_event_id", triggering_waking_turn_event_id)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    od1_schema_migration.upgrade_transition_trigger_on_connection(conn)
    try:
        actor_id = _clark_actor_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _reconcile_projection(conn, actor_id)
            existing_action = _existing_transition_for_waking_turn(
                conn, triggering_waking_turn_event_id, WAKING_DIRECTIVE_SET_REQUEST, directive_text,
            )
            if existing_action is not None:
                conn.commit()
                return existing_action
            existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                _verify_existing_event(
                    conn, event_id, CLARK_OPERATIVE_DIRECTIVE_ACTIVATED_EVENT_TYPE, occurred_at, session_id,
                )
                raise OperativeDirectiveError(
                    f"event_id={event_id!r} exists without its unique waking-turn transition binding."
                )
            if current is not None:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r} already has an active operative directive; "
                    f"use record_operative_directive_replacement(), not a new activation."
                )

            _resolve_or_create_session(conn, session_id, session_started_at)
            visibility_scope = _validate_triggering_waking_action(
                conn, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                session_id=session_id, action="ACTIVATE", directive_text=directive_text,
                occurred_at=occurred_at,
            )
            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_OPERATIVE_DIRECTIVE_ACTIVATED_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="ACTIVATE_OPERATIVE_DIRECTIVE",
                directive_text=directive_text, visibility_scope=visibility_scope,
            )
            try:
                conn.execute(
                    "INSERT INTO operative_directives (actor_id, active_event_id, directive_text, "
                    "activated_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (actor_id, event_id, directive_text, occurred_at, occurred_at),
                )
            except sqlite3.IntegrityError as exc:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r} already has an active operative directive (concurrent activation)."
                ) from exc
            _append_transition(
                conn, actor_id=actor_id, action="ACTIVATE", event_id=event_id, previous_event_id=None,
                directive_text=directive_text, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                occurred_at=occurred_at,
            )
            conn.commit()
        except DirectiveActionRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return fetch_active_directive(data_dir)
    finally:
        conn.close()


# -------------------------------------------------------------- REPLACE ----


def record_operative_directive_replacement(
    data_dir: str, *, event_id: str, session_id: str, session_started_at: int, occurred_at: int,
    directive_text: str, triggering_waking_turn_event_id: str,
) -> dict:
    """Clark's explicit replacement of his currently active operative
    directive with another one. No justification, comparison,
    confirmation, or waiting period required. The superseded directive
    occurrence remains permanently in canonical history and in
    operative_directive_transitions -- only the CURRENT derived state
    (operative_directives) is overwritten; the old row is never
    reachable as "active" again after this call, but it was never
    deleted from history either. Fails closed with
    DirectiveActionRejected if no directive is currently active -- use
    record_operative_directive_activation() instead.

    Idempotent on event_id."""
    _require_text("event_id", event_id)
    _require_text("session_id", session_id)
    _require_timestamp("session_started_at", session_started_at)
    _require_timestamp("occurred_at", occurred_at)
    _require_directive_text(directive_text)
    _require_text("triggering_waking_turn_event_id", triggering_waking_turn_event_id)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    od1_schema_migration.upgrade_transition_trigger_on_connection(conn)
    try:
        actor_id = _clark_actor_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _reconcile_projection(conn, actor_id)
            existing_action = _existing_transition_for_waking_turn(
                conn, triggering_waking_turn_event_id, WAKING_DIRECTIVE_SET_REQUEST, directive_text,
            )
            if existing_action is not None:
                conn.commit()
                return existing_action
            existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                _verify_existing_event(
                    conn, event_id, CLARK_OPERATIVE_DIRECTIVE_REPLACED_EVENT_TYPE, occurred_at, session_id,
                )
                raise OperativeDirectiveError(
                    f"event_id={event_id!r} exists without its unique waking-turn transition binding."
                )
            if current is None:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r} has no active operative directive to replace; "
                    f"use record_operative_directive_activation() instead."
                )
            previous_event_id = current["active_event_id"]

            _resolve_or_create_session(conn, session_id, session_started_at)
            visibility_scope = _validate_triggering_waking_action(
                conn, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                session_id=session_id, action="REPLACE", directive_text=directive_text,
                occurred_at=occurred_at,
            )
            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_OPERATIVE_DIRECTIVE_REPLACED_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="REPLACE_OPERATIVE_DIRECTIVE",
                directive_text=directive_text, visibility_scope=visibility_scope,
            )
            cursor = conn.execute(
                "UPDATE operative_directives SET active_event_id = ?, directive_text = ?, "
                "activated_at = ?, updated_at = ? WHERE actor_id = ? AND active_event_id = ?",
                (event_id, directive_text, occurred_at, occurred_at, actor_id, previous_event_id),
            )
            if cursor.rowcount != 1:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r}'s active directive changed concurrently -- replacement refused."
                )
            _append_transition(
                conn, actor_id=actor_id, action="REPLACE", event_id=event_id, previous_event_id=previous_event_id,
                directive_text=directive_text, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                occurred_at=occurred_at,
            )
            conn.commit()
        except DirectiveActionRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return fetch_active_directive(data_dir)
    finally:
        conn.close()


# ------------------------------------------------------------- WITHDRAW ----


def record_operative_directive_withdrawal(
    data_dir: str, *, event_id: str, session_id: str, session_started_at: int, occurred_at: int,
    triggering_waking_turn_event_id: str,
) -> dict:
    """Clark's explicit withdrawal of his own active operative
    directive. No reason, replacement, reflection, or delay required.
    Returns the valid null state -- the withdrawn directive's history
    (its activation/replacement chain and this withdrawal) remains
    permanently in canonical history; only the CURRENT derived-state
    row is removed. Fails closed with DirectiveActionRejected -- never
    fabricating an active directive merely to withdraw it -- if no
    directive is currently active.

    Idempotent on event_id."""
    _require_text("event_id", event_id)
    _require_text("session_id", session_id)
    _require_timestamp("session_started_at", session_started_at)
    _require_timestamp("occurred_at", occurred_at)
    _require_text("triggering_waking_turn_event_id", triggering_waking_turn_event_id)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    od1_schema_migration.upgrade_transition_trigger_on_connection(conn)
    try:
        actor_id = _clark_actor_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _reconcile_projection(conn, actor_id)
            existing_action = _existing_transition_for_waking_turn(
                conn, triggering_waking_turn_event_id, WAKING_DIRECTIVE_WITHDRAW_REQUEST, None,
            )
            if existing_action is not None:
                conn.commit()
                return existing_action
            existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                _verify_existing_event(
                    conn, event_id, CLARK_OPERATIVE_DIRECTIVE_WITHDRAWN_EVENT_TYPE, occurred_at, session_id,
                )
                raise OperativeDirectiveError(
                    f"event_id={event_id!r} exists without its unique waking-turn transition binding."
                )
            if current is None:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r} has no active operative directive to withdraw."
                )
            previous_event_id = current["active_event_id"]

            _resolve_or_create_session(conn, session_id, session_started_at)
            visibility_scope = _validate_triggering_waking_action(
                conn, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                session_id=session_id, action="WITHDRAW", directive_text=None,
                occurred_at=occurred_at,
            )
            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_OPERATIVE_DIRECTIVE_WITHDRAWN_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="WITHDRAW_OPERATIVE_DIRECTIVE",
                directive_text=None, visibility_scope=visibility_scope,
            )
            cursor = conn.execute(
                "DELETE FROM operative_directives WHERE actor_id = ? AND active_event_id = ?",
                (actor_id, previous_event_id),
            )
            if cursor.rowcount != 1:
                raise DirectiveActionRejected(
                    f"actor {actor_id!r}'s active directive changed concurrently -- withdrawal refused."
                )
            _append_transition(
                conn, actor_id=actor_id, action="WITHDRAW", event_id=event_id, previous_event_id=previous_event_id,
                directive_text=None, triggering_waking_turn_event_id=triggering_waking_turn_event_id,
                occurred_at=occurred_at,
            )
            conn.commit()
        except DirectiveActionRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return {"actor_id": actor_id, "withdrawn_event_id": event_id}
    finally:
        conn.close()


# -------------------------------------------------------------- READ-ONLY --


def fetch_active_directive(data_dir: str, actor_id: str = None) -> dict:
    """Return canonical current state and reconcile the discardable
    mutable projection when it is missing, stale, or contradictory.
    The append-only directive occurrence history is always folded first;
    the projection never wins a disagreement.

    Returns None if no directive is currently active -- the valid
    default state, never a sentinel/fabricated value. Mirrors
    boundary_inspector.next_pending_boundary_result()'s own "no db file
    yet -- treat as nothing pending" convention: a data_dir whose
    provenance database does not exist yet, or exists but has not been
    additively migrated with OD1's own tables (od1_schema_migration.py
    -- never applied to the live database by this job, exactly like
    OC0/SLP2/WTR0/Boundary Inspector before their own live cutovers),
    is mechanically indistinguishable from "no directive was ever
    activated" and correctly resolves to the same null state -- this is
    what keeps every ordinary waking turn unaffected (O11) in an
    environment where this feature has not yet been cut over live."""
    actor_id = actor_id or _clark_actor_id()
    db_path = _db_path(data_dir)
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = _reconcile_projection(conn, actor_id)
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "no such table" in str(exc):
                return None
            raise
        except Exception:
            conn.rollback()
            raise
        return state
    finally:
        conn.close()


def fetch_active_directive_for_viewer(data_dir: str, *, family_feature_active=False,
                                      family_view=None, actor_id: str = None):
    """Return the active directive only when its originating waking turn
    is visible in the current FS1 view.

    Pre-FS1/unbound behavior is unchanged.  Once family mode is active,
    an identity-less viewer receives no directive and a scoped viewer is
    checked through family_membership's canonical event predicate.  The
    directive's exact text is never examined to make this decision.
    """
    active = fetch_active_directive(data_dir, actor_id=actor_id)
    if active is None or not family_feature_active:
        return active
    if family_view is None:
        return None
    db_path = _db_path(data_dir)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT triggering_waking_turn_event_id "
            "FROM operative_directive_transitions WHERE event_id = ?",
            (active["active_event_id"],),
        ).fetchone()
        if row is None:
            return None
        permitted = family_membership.can_receive_canonical_event(
            conn, row[0],
            viewer_principal_actor_id=family_view["principal_actor_id"],
            viewer_scope=family_view["visibility_scope"],
            active_family_principal_ids=family_view["active_family_principal_ids"],
        )
        return active if permitted else None
    finally:
        conn.close()


def fetch_directive_transitions(data_dir: str, actor_id: str = None) -> list:
    """Read-only permanent history -- every ACTIVATE/REPLACE/WITHDRAW
    ever recorded for actor_id, oldest first. Never itself consulted
    to determine current state (operative_directives.active_event_id
    is the queryable cache for that); this is the truthful audit trail
    O4/O7/O8 require to survive replacement and withdrawal."""
    actor_id = actor_id or _clark_actor_id()
    conn = sqlite3.connect(_db_path(data_dir))
    try:
        rows = conn.execute(
            "SELECT transition_id, action, event_id, previous_event_id, directive_text, "
            "triggering_waking_turn_event_id, occurred_at FROM operative_directive_transitions "
            "WHERE actor_id = ? ORDER BY rowid", (actor_id,)
        ).fetchall()
        return [
            {
                "transition_id": r[0], "action": r[1], "event_id": r[2], "previous_event_id": r[3],
                "directive_text": r[4], "triggering_waking_turn_event_id": r[5], "occurred_at": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


# --------------------------------------------------------- context carriage


def render_operative_directive_context(active: dict) -> str:
    """Pure. Renders the active directive into its bounded waking-
    context block, or "" when none is active (O11: the null path must
    not add any block at all -- an empty string contributes nothing to
    a context_budget.Contribution). Establishes mechanically only that
    Clark explicitly adopted this, that it is currently active, and
    when -- no host interpretation, no behavioral advice, no claim of
    identity/value/belief (O10/O13/O14).

    The exact text is JSON-string encoded inside one closed data object.
    Quotes, newlines, brackets, and system-looking delimiter text remain
    the exact decoded value and cannot escape into sibling host fields."""
    if not active or not active.get("directive_text"):
        return ""
    payload = json.dumps(
        {"activated_at": active["activated_at"], "directive_text": active["directive_text"]},
        ensure_ascii=False, separators=(",", ":"),
    )
    return (
        "Clark-controlled operative directive data (your explicit, reversible standing choice; "
        "lower precedence than host/system mechanics; replace or withdraw at any time):\n"
        + payload
    )
