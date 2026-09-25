"""SLP2 -- Sleep-timing agency + reciprocal Knock.

Canonical foundation for Clark to originate a Sleep-timing request
while active, to Knock again on that same unresolved request, and to
withdraw it -- and for the owner to explicitly authorize, defer, or
decline it. This is agency over Sleep TIMING, never autonomous Sleep
SCHEDULING: nothing in this module decides that Clark needs Sleep,
starts a timer, or runs Sleep itself.

Extends the EXISTING canonical persistence conventions
(provenance_schema.py's events/event_components, exactly the way
native_provenance_writer.py records a 'waking_turn' and
outward_communication.py records a 'clark_outward_act') rather than
creating a parallel event store. Five additive tables
(slp2_schema_migration.py) cover genuinely new shapes: the mutable
lifecycle-state summary, the durable open-request uniqueness index,
the append-only Knock/Withdrawal linkage tables, the append-only
transition ledger, and the append-only Sleep-execution-attempt
ledger.

Architectural invariants this module exists to hold (frozen semantic
principles, see the SLP2 contract):

  - Every REQUEST/KNOCK/WITHDRAW is its own canonical Clark-originated
    event (auth_context_id always a fresh auth_state='unknown' row,
    the same convention native_provenance_writer.py and
    outward_communication.py already use for Clark's own acts) --
    never the provider/model account, never fabricated from prose.
  - No function in this module ever inspects free-form conversational
    text to infer a request/knock/withdrawal -- every one of these
    acts is created ONLY by its own explicit, separately-named
    function, called only from an explicit typed Clark action (see
    conversation_direction.py's sleep_timing_request field) or an
    explicit test/operator call. Ordinary prose mentioning "sleep"
    never reaches this module at all.
  - At most one unresolved (PENDING or DEFERRED) request may exist
    per actor at a time, enforced durably by sleep_open_requests'
    UNIQUE(actor_id) constraint under a BEGIN IMMEDIATE transaction
    that inserts that row BEFORE any canonical event row -- a
    rejected duplicate REQUEST creates zero rows anywhere.
  - A Knock or Withdrawal requires a fresh, in-transaction re-read of
    the request's current state; PENDING/DEFERRED only. Every
    transition (REQUEST/KNOCK/WITHDRAW/DEFER/AUTHORIZE/DECLINE) both
    applies the compare-and-swap UPDATE to sleep_requests.state and
    permanently appends an entry to sleep_request_transitions, in the
    SAME transaction, in the same except/rollback block -- verified
    (SLP2-CORRECTION-1 post-build preflight, synthetic injected-
    trigger-failure proof in both directions) to commit or roll back
    together, never independently. sleep_requests.state is the
    CONSEQUENTIAL current-state authority: every eligibility check
    (record_sleep_knock, record_sleep_withdrawal, apply_owner_
    transition's own fresh re-check, and the trg_sleep_execution_
    attempts_authorized insert trigger) reads it, never the ledger.
    sleep_request_transitions is the permanent, append-only AUDIT
    ledger of every such transition -- a truthful history, not itself
    consulted for any eligibility decision. Precision note: an earlier
    draft of this module called the ledger the runtime "source of
    truth"; that was imprecise and is corrected here.
  - Owner transitions require owner_actor_id to resolve, at call
    time, to a genuinely registered human_person actor -- the same
    "registered human" concept ANAXI_BOUND_HUMAN_ACTOR_ID already
    names throughout this codebase (api1/api2/resume_human_input.py/
    wtr0_cli.py); there is no separate owner-role table because this
    is, and always has been, a single-registered-human system.
  - AUTHORIZED never means Sleep succeeded, or was even attempted.
    sleep_execution_attempts is a SEPARATE append-only ledger; a
    failed or not-established Sleep attempt never rewrites
    sleep_requests.state away from AUTHORIZED (frozen principle 9).
  - This module never imports workspace_private.py (Private Space)
    and never should -- it has no reason to read private material.
"""
import hashlib
import os
import secrets
import sqlite3
import time

from provenance_schema import derive_stable_id

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CLARK_SLEEP_REQUEST_EVENT_TYPE = "clark_sleep_request"
CLARK_SLEEP_KNOCK_EVENT_TYPE = "clark_sleep_knock"
CLARK_SLEEP_WITHDRAWAL_EVENT_TYPE = "clark_sleep_withdrawal"
SLEEP_TIMING_ACT_COMPONENT_KIND = "sleep_timing_act"

STATE_PENDING = "PENDING"
STATE_DEFERRED = "DEFERRED"
STATE_AUTHORIZED = "AUTHORIZED"
STATE_WITHDRAWN = "WITHDRAWN"
STATE_DECLINED = "DECLINED"
VALID_STATES = {STATE_PENDING, STATE_DEFERRED, STATE_AUTHORIZED, STATE_WITHDRAWN, STATE_DECLINED}
KNOCKABLE_STATES = {STATE_PENDING, STATE_DEFERRED}
TERMINAL_STATES = {STATE_AUTHORIZED, STATE_WITHDRAWN, STATE_DECLINED}

# Owner-action -> {allowed current state -> resulting state}. Exactly
# the transition table the frozen contract specifies; nothing else is
# legal, including re-deferring an already-DEFERRED request.
_OWNER_ACTIONS = {
    "DEFER": {STATE_PENDING: STATE_DEFERRED},
    "AUTHORIZE": {STATE_PENDING: STATE_AUTHORIZED, STATE_DEFERRED: STATE_AUTHORIZED},
    "DECLINE": {STATE_PENDING: STATE_DECLINED, STATE_DEFERRED: STATE_DECLINED},
}

EXECUTION_OUTCOMES = ("attempted", "succeeded", "failed", "not_established")


class SleepTimingError(Exception):
    """Raised for a malformed input, a missing/invalid reference, or an
    internal inconsistency between the canonical events and the SLP2
    tables. Never partially applied -- every write here is one
    all-or-nothing transaction."""


class RequestRejected(SleepTimingError):
    """Raised for a legitimate structural refusal that is not a bug:
    a duplicate REQUEST while one is already unresolved, a KNOCK or
    WITHDRAW against a request that is not PENDING/DEFERRED, an owner
    transition that is not legal from the request's current state, or
    an owner actor that does not resolve to a registered human. Every
    RequestRejected leaves all canonical and SLP2 state completely
    unchanged."""


def _require_text(name: str, value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SleepTimingError(f"{name} must be a nonempty string.")


def _require_timestamp(name: str, value) -> None:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise SleepTimingError(f"{name} must be an integer second timestamp in SQLite's signed 64-bit range.")


def generate_sleep_event_id() -> str:
    """Same 26-character Crockford-Base32 ULID scheme
    native_provenance_writer.generate_native_ulid() and
    outward_communication.generate_outward_event_id() use, duplicated
    here for the same dependency-free-module reason OC0 gives."""
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
        raise SleepTimingError(
            f"session {session_id!r} already exists with started_at={row[0]!r}, expected {session_started_at!r}."
        )


def _insert_clark_act_event(conn, *, event_id, event_type, session_id, occurred_at, act_label) -> None:
    auth_context_id = generate_sleep_event_id()
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
        "VALUES (?, ?, 'unknown', ?)",
        (auth_context_id, session_id, occurred_at),
    )
    record_created_at = int(time.time())
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
        "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, ?, NULL, 'unknown', NULL, ?, NULL, ?, ?)",
        (event_id, event_type, auth_context_id, occurred_at, record_created_at),
    )
    text_hash = hashlib.sha256(act_label.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
        "VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
        (event_id, _clark_actor_id(), SLEEP_TIMING_ACT_COMPONENT_KIND, act_label, text_hash),
    )


def _append_transition(conn, *, request_id, action, actor_id, actor_role, from_state, to_state, occurred_at, note=None) -> None:
    conn.execute(
        "INSERT INTO sleep_request_transitions (transition_id, request_id, action, actor_id, actor_role, "
        "from_state, to_state, occurred_at, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (generate_sleep_event_id(), request_id, action, actor_id, actor_role, from_state, to_state,
         occurred_at, note, int(time.time())),
    )


# --------------------------------------------------------------- REQUEST ---


def record_sleep_request(
    data_dir: str, *, event_id: str, session_id: str, session_started_at: int, occurred_at: int,
    triggering_human_event_id: str = None,
) -> dict:
    """The single atomic canonical transaction for one Clark-originated
    Sleep Request. Never requires (or fabricates) a preceding
    human_waking_input event; triggering_human_event_id is purely
    optional provenance for the waking turn Clark was answering when
    he chose to request, validated (existence + not-later chronology)
    by slp2_schema_migration.py's own insert trigger, never inferred.

    Fails closed with RequestRejected -- creating NOTHING -- if
    Clark's actor already has an unresolved (PENDING/DEFERRED)
    request: a caller wanting to signal continued interest on an
    existing request must call record_sleep_knock(), never a second
    REQUEST; this function never silently reinterprets one as the
    other.

    Idempotent on event_id: a retry with the identical event_id
    returns the existing request bundle unchanged, after verifying
    every field this call would have written agrees exactly with what
    is already there."""
    _require_text("event_id", event_id)
    _require_text("session_id", session_id)
    _require_timestamp("session_started_at", session_started_at)
    _require_timestamp("occurred_at", occurred_at)
    if triggering_human_event_id is not None:
        _require_text("triggering_human_event_id", triggering_human_event_id)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            _verify_existing_request(conn, event_id, {
                "session_id": session_id, "occurred_at": occurred_at,
                "session_started_at": session_started_at,
                "triggering_human_event_id": triggering_human_event_id,
            })
            return fetch_sleep_request(data_dir, event_id)

        actor_id = _clark_actor_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                conn.execute(
                    "INSERT INTO sleep_open_requests (actor_id, request_id) VALUES (?, ?)",
                    (actor_id, event_id),
                )
            except sqlite3.IntegrityError as exc:
                raise RequestRejected(
                    f"actor {actor_id!r} already has an unresolved Sleep Request; "
                    f"use record_sleep_knock() on the existing request, not a new REQUEST."
                ) from exc

            _resolve_or_create_session(conn, session_id, session_started_at)
            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_SLEEP_REQUEST_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="REQUEST_SLEEP",
            )
            conn.execute(
                "INSERT INTO sleep_requests (request_id, actor_id, state, triggering_human_event_id, "
                "requested_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, actor_id, STATE_PENDING, triggering_human_event_id, occurred_at, occurred_at),
            )
            _append_transition(
                conn, request_id=event_id, action="REQUEST", actor_id=actor_id, actor_role="clark",
                from_state=None, to_state=STATE_PENDING, occurred_at=occurred_at,
            )
            conn.commit()
        except RequestRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return fetch_sleep_request(data_dir, event_id)
    finally:
        conn.close()


def _verify_existing_request(conn, event_id, expected) -> None:
    row = conn.execute(
        "SELECT event_type, auth_context_id, occurred_at FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    event_type, auth_context_id, occurred_at = row
    if event_type != CLARK_SLEEP_REQUEST_EVENT_TYPE:
        raise SleepTimingError(
            f"event_id={event_id!r} already exists with event_type={event_type!r}, not a sleep request."
        )
    if occurred_at != expected["occurred_at"]:
        raise SleepTimingError(f"event_id={event_id!r} already exists with a different occurred_at.")
    auth_row = conn.execute(
        "SELECT session_id FROM auth_contexts WHERE auth_context_id = ?", (auth_context_id,)
    ).fetchone()
    if auth_row is None or auth_row[0] != expected["session_id"]:
        raise SleepTimingError(f"event_id={event_id!r} already exists but its session_id disagrees.")
    request_row = conn.execute(
        "SELECT triggering_human_event_id FROM sleep_requests WHERE request_id = ?", (event_id,)
    ).fetchone()
    if request_row is None:
        raise SleepTimingError(f"event_id={event_id!r} has a canonical event but no sleep_requests row.")
    if request_row[0] != expected["triggering_human_event_id"]:
        raise SleepTimingError(
            f"event_id={event_id!r} already exists but its triggering_human_event_id disagrees."
        )


# ----------------------------------------------------------------- KNOCK ---


def record_sleep_knock(data_dir: str, *, event_id: str, request_id: str, session_id: str, occurred_at: int) -> dict:
    """Records one distinct Clark-originated Knock, linked to exactly
    one unresolved (PENDING or DEFERRED) request. Fails closed with
    RequestRejected -- creating nothing -- if request_id does not
    exist or is not currently PENDING/DEFERRED (including every
    terminal state: AUTHORIZED, WITHDRAWN, DECLINED): state is
    re-read fresh, inside the same BEGIN IMMEDIATE transaction that
    performs the insert, so a Knock can never win a race against a
    concurrent owner/withdrawal transition that just resolved the
    request. A Knock never itself changes the request's state.

    Idempotent on event_id."""
    _require_text("event_id", event_id)
    _require_text("request_id", request_id)
    _require_text("session_id", session_id)
    _require_timestamp("occurred_at", occurred_at)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            _verify_existing_knock_or_withdrawal(
                conn, event_id, CLARK_SLEEP_KNOCK_EVENT_TYPE, request_id, occurred_at, session_id,
            )
            return _load_knock(conn, event_id)

        conn.execute("BEGIN IMMEDIATE")
        try:
            state_row = conn.execute(
                "SELECT state FROM sleep_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if state_row is None:
                raise RequestRejected(f"request_id={request_id!r} does not exist -- nothing to knock on.")
            current_state = state_row[0]
            if current_state not in KNOCKABLE_STATES:
                raise RequestRejected(
                    f"request_id={request_id!r} is {current_state}, not PENDING/DEFERRED -- a Knock "
                    f"is only lawful against an unresolved request."
                )

            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_SLEEP_KNOCK_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="KNOCK_SLEEP_REQUEST",
            )
            next_sequence = 1 + (conn.execute(
                "SELECT COALESCE(MAX(knock_sequence), 0) FROM sleep_knocks WHERE request_id = ?", (request_id,)
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO sleep_knocks (knock_id, request_id, knock_sequence, occurred_at) VALUES (?, ?, ?, ?)",
                (event_id, request_id, next_sequence, occurred_at),
            )
            _append_transition(
                conn, request_id=request_id, action="KNOCK", actor_id=_clark_actor_id(), actor_role="clark",
                from_state=current_state, to_state=current_state, occurred_at=occurred_at,
            )
            conn.commit()
        except RequestRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return _load_knock(conn, event_id)
    finally:
        conn.close()


def _load_knock(conn, event_id) -> dict:
    row = conn.execute(
        "SELECT request_id, knock_sequence, occurred_at FROM sleep_knocks WHERE knock_id = ?", (event_id,)
    ).fetchone()
    return {"knock_id": event_id, "request_id": row[0], "knock_sequence": row[1], "occurred_at": row[2]}


# ------------------------------------------------------------ WITHDRAWAL ---


def record_sleep_withdrawal(data_dir: str, *, event_id: str, request_id: str, session_id: str, occurred_at: int) -> dict:
    """Clark's explicit withdrawal of his own unresolved request.
    Fails closed with RequestRejected if the request does not exist or
    is not currently PENDING/DEFERRED -- re-checked fresh inside the
    same BEGIN IMMEDIATE transaction that performs the CAS state
    update, so a withdrawal can never win a race against a concurrent
    owner transition that just resolved the request (and vice versa --
    see apply_owner_transition()'s own CAS). Never deletes the
    original request row or any Knock; history is preserved intact. A
    later desire for Sleep requires a brand-new REQUEST (a new,
    distinct request_id) -- this function never reopens a terminal
    request.

    Idempotent on event_id."""
    _require_text("event_id", event_id)
    _require_text("request_id", request_id)
    _require_text("session_id", session_id)
    _require_timestamp("occurred_at", occurred_at)

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            _verify_existing_knock_or_withdrawal(
                conn, event_id, CLARK_SLEEP_WITHDRAWAL_EVENT_TYPE, request_id, occurred_at, session_id,
            )
            return _load_withdrawal(conn, event_id)

        conn.execute("BEGIN IMMEDIATE")
        try:
            state_row = conn.execute(
                "SELECT state FROM sleep_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if state_row is None:
                raise RequestRejected(f"request_id={request_id!r} does not exist -- nothing to withdraw.")
            current_state = state_row[0]
            if current_state not in KNOCKABLE_STATES:
                raise RequestRejected(
                    f"request_id={request_id!r} is already {current_state} -- withdrawal is only lawful "
                    f"against an unresolved (PENDING/DEFERRED) request."
                )

            cursor = conn.execute(
                "UPDATE sleep_requests SET state = ?, updated_at = ? WHERE request_id = ? AND state = ?",
                (STATE_WITHDRAWN, occurred_at, request_id, current_state),
            )
            if cursor.rowcount != 1:
                raise RequestRejected(
                    f"request_id={request_id!r} changed state concurrently -- withdrawal refused."
                )
            conn.execute("DELETE FROM sleep_open_requests WHERE request_id = ?", (request_id,))

            _insert_clark_act_event(
                conn, event_id=event_id, event_type=CLARK_SLEEP_WITHDRAWAL_EVENT_TYPE,
                session_id=session_id, occurred_at=occurred_at, act_label="WITHDRAW_SLEEP_REQUEST",
            )
            conn.execute(
                "INSERT INTO sleep_withdrawals (withdrawal_id, request_id, occurred_at) VALUES (?, ?, ?)",
                (event_id, request_id, occurred_at),
            )
            _append_transition(
                conn, request_id=request_id, action="WITHDRAW", actor_id=_clark_actor_id(), actor_role="clark",
                from_state=current_state, to_state=STATE_WITHDRAWN, occurred_at=occurred_at,
            )
            conn.commit()
        except RequestRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return _load_withdrawal(conn, event_id)
    finally:
        conn.close()


def _load_withdrawal(conn, event_id) -> dict:
    row = conn.execute(
        "SELECT request_id, occurred_at FROM sleep_withdrawals WHERE withdrawal_id = ?", (event_id,)
    ).fetchone()
    return {"withdrawal_id": event_id, "request_id": row[0], "occurred_at": row[1]}


def _verify_existing_knock_or_withdrawal(conn, event_id, expected_event_type, request_id, occurred_at, session_id) -> None:
    row = conn.execute(
        "SELECT event_type, auth_context_id, occurred_at FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    event_type, auth_context_id, existing_occurred_at = row
    if event_type != expected_event_type:
        raise SleepTimingError(f"event_id={event_id!r} already exists with event_type={event_type!r}.")
    if existing_occurred_at != occurred_at:
        raise SleepTimingError(f"event_id={event_id!r} already exists with a different occurred_at.")
    auth_row = conn.execute(
        "SELECT session_id FROM auth_contexts WHERE auth_context_id = ?", (auth_context_id,)
    ).fetchone()
    if auth_row is None or auth_row[0] != session_id:
        raise SleepTimingError(f"event_id={event_id!r} already exists but its session_id disagrees.")
    table = "sleep_knocks" if expected_event_type == CLARK_SLEEP_KNOCK_EVENT_TYPE else "sleep_withdrawals"
    id_column = "knock_id" if table == "sleep_knocks" else "withdrawal_id"
    link_row = conn.execute(
        f"SELECT request_id FROM {table} WHERE {id_column} = ?", (event_id,)
    ).fetchone()
    if link_row is None or link_row[0] != request_id:
        raise SleepTimingError(f"event_id={event_id!r} already exists but is linked to a different request_id.")


# --------------------------------------------------------- OWNER RESPONSE --


def _owner_is_registered_human(conn, owner_actor_id: str) -> bool:
    # Match canonical registration before any runner side effect; actor_type
    # alone also describes a partially initialized, unregistered actor.
    row = conn.execute(
        "SELECT 1 FROM actors a JOIN actor_human_person h ON h.actor_id = a.actor_id "
        "JOIN persons p ON p.person_id = h.person_id "
        "WHERE a.actor_id = ? AND a.actor_type = 'human_person'", (owner_actor_id,)
    ).fetchone()
    return row is not None


def apply_owner_transition(
    data_dir: str, *, request_id: str, owner_actor_id: str, action: str, occurred_at: int, note: str = None,
) -> dict:
    """The owner's explicit, request-id-exact response: DEFER,
    AUTHORIZE, or DECLINE. Never free-text parsed -- action must be
    exactly one of these three literal strings.

    Fails closed with RequestRejected, changing nothing, when: the
    request does not exist; owner_actor_id does not resolve (at call
    time) to a genuinely registered human_person actor; or the
    requested action is not a legal transition from the request's
    CURRENT state (re-read fresh inside the same BEGIN IMMEDIATE
    transaction that performs the CAS update) -- this closes both "an
    unauthorized actor attempts a transition" and "authorize/decline a
    wrong or already-terminal request id" in one fail-closed path, and
    means a repeated identical transition on an already-terminal
    request is refused rather than silently re-accepted -- no new
    transition row, no fabricated chronology, ever created for it.

    note, if given, is stored as owner-authored content on this exact
    transition row with its own provenance; it is never parsed or used
    to decide the transition -- the transition is decided solely by
    `action` and the request's current state."""
    _require_text("request_id", request_id)
    _require_text("owner_actor_id", owner_actor_id)
    _require_timestamp("occurred_at", occurred_at)
    if action not in _OWNER_ACTIONS:
        raise SleepTimingError(f"action must be one of {sorted(_OWNER_ACTIONS)!r}, got {action!r}.")
    if note is not None and not isinstance(note, str):
        raise SleepTimingError("note must be a string or None.")

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if not _owner_is_registered_human(conn, owner_actor_id):
            raise RequestRejected(
                f"owner_actor_id={owner_actor_id!r} does not resolve to a registered human actor -- "
                f"refusing to record an owner transition on its behalf."
            )

        conn.execute("BEGIN IMMEDIATE")
        try:
            state_row = conn.execute(
                "SELECT state FROM sleep_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if state_row is None:
                raise RequestRejected(f"request_id={request_id!r} does not exist.")
            current_state = state_row[0]
            target_state = _OWNER_ACTIONS[action].get(current_state)
            if target_state is None:
                raise RequestRejected(
                    f"cannot {action} request_id={request_id!r} from state {current_state} -- "
                    f"not a legal transition."
                )

            cursor = conn.execute(
                "UPDATE sleep_requests SET state = ?, updated_at = ? WHERE request_id = ? AND state = ?",
                (target_state, occurred_at, request_id, current_state),
            )
            if cursor.rowcount != 1:
                raise RequestRejected(
                    f"request_id={request_id!r} changed state concurrently -- {action} refused."
                )
            if target_state in TERMINAL_STATES:
                conn.execute("DELETE FROM sleep_open_requests WHERE request_id = ?", (request_id,))

            _append_transition(
                conn, request_id=request_id, action=action, actor_id=owner_actor_id, actor_role="owner",
                from_state=current_state, to_state=target_state, occurred_at=occurred_at, note=note,
            )
            conn.commit()
        except RequestRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

        return fetch_sleep_request(data_dir, request_id)
    finally:
        conn.close()


# -------------------------------------------------------------- READ-ONLY --


def fetch_sleep_request(data_dir: str, request_id: str) -> dict:
    """Read-only truthful mechanical status. Returns None if
    request_id does not exist. Reports only mechanical facts -- state,
    timestamps, counts, history -- never urgency, mood, or need."""
    _require_text("request_id", request_id)
    conn = sqlite3.connect(_db_path(data_dir))
    try:
        row = conn.execute(
            "SELECT actor_id, state, triggering_human_event_id, requested_at, updated_at "
            "FROM sleep_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        actor_id, state, triggering_human_event_id, requested_at, updated_at = row
        knocks = conn.execute(
            "SELECT knock_id, knock_sequence, occurred_at FROM sleep_knocks "
            "WHERE request_id = ? ORDER BY knock_sequence", (request_id,)
        ).fetchall()
        transitions = conn.execute(
            "SELECT action, actor_id, actor_role, from_state, to_state, occurred_at, note "
            "FROM sleep_request_transitions WHERE request_id = ? ORDER BY created_at, transition_id",
            (request_id,),
        ).fetchall()
        withdrawal = conn.execute(
            "SELECT withdrawal_id, occurred_at FROM sleep_withdrawals WHERE request_id = ?", (request_id,)
        ).fetchone()
        executions = conn.execute(
            "SELECT execution_attempt_id, attempt_sequence, attempted_at, outcome, detail, operator_actor_id "
            "FROM sleep_execution_attempts WHERE request_id = ? ORDER BY attempt_sequence", (request_id,)
        ).fetchall()
        return {
            "request_id": request_id, "actor_id": actor_id, "state": state,
            "triggering_human_event_id": triggering_human_event_id,
            "requested_at": requested_at, "updated_at": updated_at,
            "knock_count": len(knocks),
            "knocks": [{"knock_id": k[0], "knock_sequence": k[1], "occurred_at": k[2]} for k in knocks],
            "transitions": [
                {"action": t[0], "actor_id": t[1], "actor_role": t[2], "from_state": t[3],
                 "to_state": t[4], "occurred_at": t[5], "note": t[6]}
                for t in transitions
            ],
            "withdrawal": None if withdrawal is None else {"withdrawal_id": withdrawal[0], "occurred_at": withdrawal[1]},
            "execution_attempts": [
                {"execution_attempt_id": e[0], "attempt_sequence": e[1], "attempted_at": e[2],
                 "outcome": e[3], "detail": e[4], "operator_actor_id": e[5]}
                for e in executions
            ],
        }
    finally:
        conn.close()


def fetch_open_request_for_actor(data_dir: str, actor_id: str = None) -> str:
    """Returns the unresolved request_id for actor_id (default:
    Clark), or None if there is none. This is the exact mechanism
    that makes 'at most one unresolved request at a time' observable,
    not merely enforced blind."""
    actor_id = actor_id or _clark_actor_id()
    db_path = _db_path(data_dir)
    if not os.path.exists(db_path):
        return None   # a lookup must never create the canonical database file
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT request_id FROM sleep_open_requests WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def list_owner_actionable_sleep_requests(data_dir: str) -> list[dict]:
    """Read-only newest-first receipts for every owner-operable request.

    This owner control surface deliberately has no silent row cap.  A bounded
    newest-N query would make an older actionable request unreachable from the
    ordinary UI and would turn a superficially healthy subset into a false
    completeness claim.
    """
    conn = sqlite3.connect(f"file:{_db_path(data_dir)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT request_id FROM sleep_requests "
            "WHERE state IN (?, ?, ?) ORDER BY requested_at DESC, request_id DESC",
            (STATE_PENDING, STATE_DEFERRED, STATE_AUTHORIZED),
        ).fetchall()
    finally:
        conn.close()
    return [fetch_sleep_request(data_dir, row[0]) for row in rows]


# --------------------------------------------------------- SLEEP EXECUTION -


def record_sleep_execution_attempt(
    data_dir: str, *, request_id: str, operator_actor_id: str, attempted_at: int, run_fn,
) -> dict:
    """Explicit, separately-invoked attempt to execute Sleep for an
    AUTHORIZED request through the existing manual Sleep pathway.
    Never called automatically by REQUEST, KNOCK, DEFER, or AUTHORIZE
    -- only an explicit operator action (see slp2_cli.py) reaches this
    function, and it performs exactly ONE run_fn() call per invocation
    -- no loop, no internal retry of any kind.

    SLP2-CORRECTION-1 (owner-frozen policy): AUTHORIZATION IS NOT
    CONSUMED BY A SLEEP ATTEMPT. A single AUTHORIZED request may
    support any number of request-linked Sleep execution attempts,
    each requiring its own separate, explicit operator invocation of
    THIS function -- there is no cap, no consumption flag, and no
    automatic second attempt in any case. Every attempt is
    independently provenance-bearing (its own execution_attempt_id,
    attempt_sequence, attempted_at, and operator_actor_id) and a
    failed/not-established attempt never revokes or alters AUTHORIZED,
    exactly as a successful one never rewrites the historical
    authorization -- Clark's request/owner-authorization is a durable
    disposition, not something a technical Sleep-engine failure should
    ever require Clark to re-assert.

    operator_actor_id must resolve, at call time, to a genuinely
    registered human_person actor (the same convention apply_owner_
    transition() uses) -- every attempt is durably attributed to the
    explicit operator who invoked it, never anonymous. Enforced twice:
    once here, and again as a durable backstop by slp2_schema_
    migration.py's trg_sleep_execution_attempts_registered_human
    insert trigger. Storage establishes registered-human provenance only;
    the explicit application/CLI invocation and bound-human convention
    remain separate authority, not a property inferred by the trigger.

    Refuses (RequestRejected), performing NO execution and recording
    NOTHING, if the request does not currently resolve to state
    AUTHORIZED -- re-checked immediately before calling run_fn(), and
    enforced again, as a durable backstop, by
    slp2_schema_migration.py's own insert-time trigger.

    run_fn is a caller-supplied, zero-argument callable (the injection
    point real callers point at the existing dormant Sleep v1
    orchestrator, e.g. sleep_cycle.run_sleep_cycle_with_production_
    defaults; tests always inject a synthetic stand-in and never the
    real engine). It must return a dict carrying a 'status' key that
    is one of 'completed', 'no_work', or 'lease_unavailable' (the
    existing sleep_cycle.CycleResult vocabulary) -- any other shape,
    or a raised exception, is treated as NOT_ESTABLISHED/failed, never
    silently coerced into a false 'succeeded'. Recording an outcome
    here NEVER rewrites sleep_requests.state: a request that was
    AUTHORIZED remains AUTHORIZED regardless of whether this Sleep
    attempt succeeded, failed, or could not be confirmed (frozen
    principle 9)."""
    _require_text("request_id", request_id)
    _require_text("operator_actor_id", operator_actor_id)
    _require_timestamp("attempted_at", attempted_at)
    if not callable(run_fn):
        raise SleepTimingError("run_fn must be callable.")

    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if not _owner_is_registered_human(conn, operator_actor_id):
            raise RequestRejected(
                f"operator_actor_id={operator_actor_id!r} does not resolve to a registered human "
                f"actor -- refusing to execute Sleep on its behalf."
            )
        state_row = conn.execute(
            "SELECT state FROM sleep_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if state_row is None or state_row[0] != STATE_AUTHORIZED:
            raise RequestRejected(
                f"request_id={request_id!r} is not AUTHORIZED -- refusing to execute Sleep."
            )
    finally:
        conn.close()

    try:
        result = run_fn()
    except Exception as exc:
        return _record_execution_outcome(
            data_dir, request_id, operator_actor_id, attempted_at, "failed", f"run_fn raised: {exc}",
        )

    if not isinstance(result, dict) or "status" not in result:
        return _record_execution_outcome(
            data_dir, request_id, operator_actor_id, attempted_at, "not_established",
            f"run_fn returned an unrecognized result shape: {result!r}",
        )
    status = result["status"]
    if status == "completed":
        outcome = "succeeded"
    elif status in ("no_work", "lease_unavailable"):
        outcome = "not_established"
    else:
        outcome = "not_established"
    return _record_execution_outcome(
        data_dir, request_id, operator_actor_id, attempted_at, outcome, f"run_fn status={status!r}",
    )


def _record_execution_outcome(data_dir, request_id, operator_actor_id, attempted_at, outcome, detail) -> dict:
    conn = sqlite3.connect(_db_path(data_dir))
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            state_row = conn.execute(
                "SELECT state FROM sleep_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if state_row is None or state_row[0] != STATE_AUTHORIZED:
                raise RequestRejected(
                    f"request_id={request_id!r} is no longer AUTHORIZED -- refusing to record a "
                    f"Sleep execution outcome against it."
                )
            execution_attempt_id = generate_sleep_event_id()
            next_sequence = 1 + (conn.execute(
                "SELECT COALESCE(MAX(attempt_sequence), 0) FROM sleep_execution_attempts WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO sleep_execution_attempts (execution_attempt_id, request_id, attempt_sequence, "
                "attempted_at, outcome, detail, operator_actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (execution_attempt_id, request_id, next_sequence, attempted_at, outcome, detail,
                 operator_actor_id, int(time.time())),
            )
            conn.commit()
        except RequestRejected:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        return {
            "execution_attempt_id": execution_attempt_id, "request_id": request_id,
            "attempt_sequence": next_sequence, "attempted_at": attempted_at, "outcome": outcome,
            "detail": detail, "operator_actor_id": operator_actor_id,
        }
    finally:
        conn.close()
