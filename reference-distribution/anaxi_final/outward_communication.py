"""OC0 -- Outward Communication / Correspondence Foundation.

Canonical representation of a Clark-ORIGINATED outward communication
act: Clark speaking first, not replying to a human. Transport-neutral
foundation only -- this module never contacts Discord, email, or any
other real transport; it records that an act occurred, who it was to
(by an ANAXI-local reference, never a transport account/channel id),
and what mechanical outcome (if any) a later projection/delivery
attempt observed.

Extends the EXISTING canonical persistence conventions
(provenance_schema.py's events/event_components, exactly the way
native_provenance_writer.py records a 'waking_turn') rather than
creating a parallel event store. Two additive tables
(oc0_schema_migration.py) cover the two genuinely new shapes: a
projection/delivery-attempt ledger, and an explicit reply-linkage
table from a later real human_waking_input event back to an earlier
outward act.

Architectural invariants this module exists to hold:

  - A clark_outward_act event never requires a preceding
    human_waking_input event -- record_clark_outward_act() takes no H
    at all, by construction.
  - auth_context_id is always a fresh auth_state='unknown' row, the
    same convention native_provenance_writer.py uses for Clark's own
    waking_turn auth context -- never borrowed from any human's
    authentication.
  - The recipient/audience is recorded as a bare ANAXI-local reference
    string (component_kind='outward_recipient_reference'), never a
    Discord channel id or other transport account identity. Mapping
    that reference to a real transport endpoint is explicitly a later,
    out-of-scope concern.
  - Canonical-before-projection: record_clark_outward_act() commits
    the act in its own, independent transaction; record_projection_attempt()
    can only ever be called against an event_id that already exists,
    and a rolled-back or failed projection attempt never touches the
    events/event_components rows at all.
  - A projection attempt is pinned to the canonical act's OWN recorded
    content_sha256 (read back from event_components, never accepted
    from the caller) -- a retry mechanically refers to the same
    content; content that actually changed is a new outward act with
    a new event_id, never a mutated attempt.
  - Reply linkage is purely mechanical: record_reply_link() takes two
    already-existing event_ids and nothing else. No text similarity,
    no inference, no implicit "most recent human turn" guess.

This module never imports workspace_private.py (Private Space) and
never should -- it has no reason to read private material, matching
every other non-roaming production module in this codebase.
"""
import hashlib
import secrets
import sqlite3
import time

from provenance_schema import derive_stable_id

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CLARK_OUTWARD_ACT_EVENT_TYPE = "clark_outward_act"
OUTWARD_CONTENT_COMPONENT_KIND = "outward_content"
OUTWARD_RECIPIENT_REFERENCE_COMPONENT_KIND = "outward_recipient_reference"
PROJECTION_STATUSES = ("attempted", "succeeded", "failed", "not_established")


class OutwardCommunicationError(Exception):
    """Raised for any precondition failure -- a malformed/nonexistent
    reference, a content/identity disagreement on an idempotent
    replay, or an invalid status. Never partially applied: every write
    in this module is one all-or-nothing transaction."""


def _require_text(name: str, value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OutwardCommunicationError(f"{name} must be a nonempty string.")


def _require_timestamp(name: str, value) -> None:
    # SQLite INTEGER affinity alone accepts arbitrary text and fractional
    # numbers. Do not let that bypass chronology or alter receipt values.
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise OutwardCommunicationError(f"{name} must be an integer second timestamp in SQLite's signed 64-bit range.")


def generate_outward_event_id() -> str:
    """Plain 26-character ULID (48-bit ms timestamp + 80-bit random
    tail, Crockford Base32), the same scheme
    native_provenance_writer.generate_native_ulid() uses. Duplicated
    here (rather than imported) so this module stays a thin,
    dependency-free writer with no coupling to native_provenance_writer's
    own inference-adjacent import graph (migrate_historical_data,
    native_turn_staging) -- OC0 runs no inference and stages nothing."""
    ts_ms = int(time.time() * 1000)
    raw = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _clark_actor_id() -> str:
    return derive_stable_id("actor", "clark")


def _outward_writer_actor_id() -> str:
    """Reuses the SAME existing host_system actor
    native_provenance_writer.py already seeds and uses for its own
    host-authored, host-mechanical components (bounded_clause,
    human_input_event_id) -- never a new, unseeded actor key that
    would require its own reference-data migration."""
    return derive_stable_id("actor", "bounded_clause_renderer")


def _resolve_or_create_session(conn: sqlite3.Connection, session_id: str, session_started_at: int) -> None:
    """Runs inside the caller's already-open transaction, mirroring
    native_provenance_writer._resolve_or_create_session(): whichever
    call reaches this first performs the real INSERT, every other call
    (including a resumed/retried one) verifies the existing row
    matches exactly."""
    row = conn.execute(
        "SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, NULL, ?)",
            (session_id, session_started_at),
        )
        return
    if row[0] != session_started_at:
        raise OutwardCommunicationError(
            f"session {session_id!r} already exists with started_at={row[0]!r}, "
            f"expected {session_started_at!r} -- refusing to treat this as the same session."
        )


def _verify_existing_outward_act(conn: sqlite3.Connection, event_id: str, expected: dict) -> None:
    """Idempotent-replay guard: 'event_id already exists' is never
    sufficient by itself to treat a call as already-committed -- every
    field this exact call would have written is checked for exact
    agreement, mirroring native_provenance_writer.verify_native_bundle_contract()'s
    philosophy. Any disagreement is a hard stop, never silently
    accepted."""
    event_row = conn.execute(
        "SELECT event_type, auth_context_id, occurred_at, input_source_ref "
        "FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    event_type, auth_context_id, occurred_at, input_source_ref = event_row
    if event_type != CLARK_OUTWARD_ACT_EVENT_TYPE:
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists with event_type={event_type!r}, "
            f"not {CLARK_OUTWARD_ACT_EVENT_TYPE!r} -- refusing to treat this as the same outward act."
        )
    if occurred_at != expected["occurred_at"]:
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists with occurred_at={occurred_at!r}, "
            f"expected {expected['occurred_at']!r}."
        )
    if input_source_ref != expected.get("input_source_ref"):
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists with input_source_ref={input_source_ref!r}, "
            f"expected {expected.get('input_source_ref')!r}."
        )
    auth_row = conn.execute(
        "SELECT session_id, established_at FROM auth_contexts WHERE auth_context_id = ?",
        (auth_context_id,),
    ).fetchone()
    session_id, established_at = auth_row
    if session_id != expected["session_id"] or established_at != expected["occurred_at"]:
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists but its auth_context disagrees with the "
            f"expected session_id/occurred_at."
        )
    session_row = conn.execute(
        "SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if session_row is None or session_row[0] != expected["session_started_at"]:
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists but its session started_at disagrees."
        )
    components = {
        row[0]: row for row in conn.execute(
            "SELECT sequence, component_kind, component_text, content_sha256 FROM event_components "
            "WHERE event_id = ? ORDER BY sequence",
            (event_id,),
        ).fetchall()
    }
    expected_content_hash = hashlib.sha256(expected["content"].encode("utf-8")).hexdigest()
    expected_recipient_hash = hashlib.sha256(expected["recipient_reference"].encode("utf-8")).hexdigest()
    content_row = components.get(0)
    if (content_row is None or content_row[1] != OUTWARD_CONTENT_COMPONENT_KIND
            or content_row[2] != expected["content"] or content_row[3] != expected_content_hash):
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists but its content component disagrees with the "
            f"expected content -- a retry must carry the identical content, never regenerated text."
        )
    recipient_row = components.get(1)
    if (recipient_row is None or recipient_row[1] != OUTWARD_RECIPIENT_REFERENCE_COMPONENT_KIND
            or recipient_row[2] != expected["recipient_reference"] or recipient_row[3] != expected_recipient_hash):
        raise OutwardCommunicationError(
            f"event_id={event_id!r} already exists but its recipient reference component disagrees "
            f"with the expected recipient_reference."
        )


def record_clark_outward_act(
    data_dir: str,
    *,
    event_id: str,
    session_id: str,
    session_started_at: int,
    content: str,
    recipient_reference: str,
    occurred_at: int,
    input_source_ref: str = None,
) -> dict:
    """The single atomic canonical transaction for one Clark-originated
    outward act. Takes no human_input_event_id parameter at all --
    there is no H to link to, structurally, not merely by omission.

    Idempotent: if event_id already exists, the existing bundle is
    verified field-for-field against what THIS call would have
    written (_verify_existing_outward_act) and the existing act is
    returned unchanged -- nothing is ever re-written.

    input_source_ref is optional transport-specific provenance.  When a
    bounded transport has an already-canonical Clark action that caused
    this outward act, it may name that exact event here; idempotent replay
    then verifies the binding rather than accepting a caller-restated act.

    content and recipient_reference must both be genuinely nonempty:
    this function never fabricates a placeholder outward act for an
    opportunity that produced no real content, and never guesses a
    recipient -- both are the caller's responsibility to supply
    honestly."""
    _require_text("event_id", event_id)
    _require_text("session_id", session_id)
    _require_timestamp("session_started_at", session_started_at)
    _require_timestamp("occurred_at", occurred_at)
    if not isinstance(content, str) or not content.strip():
        raise OutwardCommunicationError(
            "content must be a nonempty string -- an outward act must carry real content, "
            "never a placeholder for an opportunity that produced none."
        )
    if not isinstance(recipient_reference, str) or not recipient_reference.strip():
        raise OutwardCommunicationError(
            "recipient_reference must be a nonempty string -- the transport-neutral audience "
            "must be explicit, never guessed or defaulted."
        )
    if input_source_ref is not None:
        _require_text("input_source_ref", input_source_ref)

    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        expected = {
            "session_id": session_id, "occurred_at": occurred_at,
            "session_started_at": session_started_at,
            "content": content, "recipient_reference": recipient_reference,
            "input_source_ref": input_source_ref,
        }
        existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            _verify_existing_outward_act(conn, event_id, expected)
            return _load_outward_act(conn, event_id)

        conn.execute("BEGIN")
        try:
            _resolve_or_create_session(conn, session_id, session_started_at)

            auth_context_id = generate_outward_event_id()
            conn.execute(
                "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                "VALUES (?, ?, 'unknown', ?)",
                (auth_context_id, session_id, occurred_at),
            )

            record_created_at = int(time.time())
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, ?, NULL, 'unknown', NULL, ?, ?, ?, ?)",
                (event_id, CLARK_OUTWARD_ACT_EVENT_TYPE, auth_context_id, input_source_ref,
                 occurred_at, record_created_at),
            )

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
                "VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, 0, ?)",
                (event_id, _clark_actor_id(), OUTWARD_CONTENT_COMPONENT_KIND, content, content_hash, len(content)),
            )

            recipient_hash = hashlib.sha256(recipient_reference.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
                "VALUES (?, 1, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                (event_id, _outward_writer_actor_id(), OUTWARD_RECIPIENT_REFERENCE_COMPONENT_KIND,
                 recipient_reference, recipient_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return _load_outward_act(conn, event_id)
    finally:
        conn.close()


def _load_outward_act(conn: sqlite3.Connection, event_id: str) -> dict:
    row = conn.execute(
        "SELECT event_type, occurred_at, record_created_at, auth_context_id, input_source_ref "
        "FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    event_type, occurred_at, record_created_at, auth_context_id, input_source_ref = row
    if event_type != CLARK_OUTWARD_ACT_EVENT_TYPE:
        raise OutwardCommunicationError(
            f"event_id={event_id!r} exists but is event_type={event_type!r}, "
            f"not {CLARK_OUTWARD_ACT_EVENT_TYPE!r}."
        )
    components = conn.execute(
        "SELECT sequence, creator_actor_id, component_kind, component_text, content_sha256 "
        "FROM event_components WHERE event_id = ? ORDER BY sequence",
        (event_id,),
    ).fetchall()
    by_sequence = {c[0]: c for c in components}
    content_row = by_sequence.get(0)
    recipient_row = by_sequence.get(1)
    if content_row is None or content_row[2] != OUTWARD_CONTENT_COMPONENT_KIND:
        raise OutwardCommunicationError(
            f"event_id={event_id!r}: missing or invalid {OUTWARD_CONTENT_COMPONENT_KIND!r} component."
        )
    if recipient_row is None or recipient_row[2] != OUTWARD_RECIPIENT_REFERENCE_COMPONENT_KIND:
        raise OutwardCommunicationError(
            f"event_id={event_id!r}: missing or invalid {OUTWARD_RECIPIENT_REFERENCE_COMPONENT_KIND!r} component."
        )
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "record_created_at": record_created_at,
        "auth_context_id": auth_context_id,
        "input_source_ref": input_source_ref,
        "actor_id": content_row[1],
        "content": content_row[3],
        "content_sha256": content_row[4],
        "recipient_reference": recipient_row[3],
        "recipient_reference_sha256": recipient_row[4],
    }


def fetch_outward_act(data_dir: str, event_id: str) -> dict:
    """Read-only. Returns None if event_id does not exist at all;
    raises OutwardCommunicationError if it exists but is not a
    complete/valid clark_outward_act bundle."""
    _require_text("event_id", event_id)
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        return _load_outward_act(conn, event_id)
    finally:
        conn.close()


def record_projection_attempt(
    data_dir: str,
    *,
    outward_event_id: str,
    status: str,
    attempted_at: int,
    observed_at: int,
    detail: str = None,
) -> dict:
    """Records one projection/delivery attempt against an ALREADY-
    canonical outward act. Refuses (OutwardCommunicationError) if
    outward_event_id does not resolve to an existing clark_outward_act
    event -- a projection attempt can never be recorded against
    nothing.

    attempt_sequence is allocated under BEGIN IMMEDIATE (1, 2, 3,
    ...), with a unique index as a durable backstop. Contention may
    raise SQLite's lock error; it never reports false success or
    silently retries. content_sha256 is read back from the canonical act's own
    event_components row, never accepted from the caller, so every
    attempt for this event_id is pinned to the SAME content by
    construction -- content that genuinely changed must be recorded as
    a new outward act (a new event_id), never as an attempt row here.

    status distinguishes exactly: 'attempted' (in flight / no outcome
    yet), 'succeeded' (mechanical success only -- never a claim of
    human attention/reading/agreement), 'failed', or 'not_established'
    (the attempt happened but its outcome could not be confirmed). The
    absence of any row for an outward_event_id IS 'projection not
    attempted' -- there is no separate row for that state.

    A failed or retried attempt never touches events/event_components
    -- this function only ever inserts into outward_projection_attempts,
    an entirely separate table, so the canonical act itself is
    structurally unreachable from here."""
    if status not in PROJECTION_STATUSES:
        raise OutwardCommunicationError(
            f"status must be one of {PROJECTION_STATUSES!r}, got {status!r}."
        )
    _require_text("outward_event_id", outward_event_id)
    _require_timestamp("attempted_at", attempted_at)
    _require_timestamp("observed_at", observed_at)
    if detail is not None and not isinstance(detail, str):
        raise OutwardCommunicationError("detail must be a string or None.")

    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        act_row = conn.execute(
            "SELECT ec.content_sha256 FROM events e "
            "JOIN event_components ec ON ec.event_id = e.event_id AND ec.sequence = 0 "
            "WHERE e.event_id = ? AND e.event_type = ?",
            (outward_event_id, CLARK_OUTWARD_ACT_EVENT_TYPE),
        ).fetchone()
        if act_row is None:
            raise OutwardCommunicationError(
                f"outward_event_id={outward_event_id!r} does not reference an existing "
                f"{CLARK_OUTWARD_ACT_EVENT_TYPE!r} canonical event with a content component -- "
                f"refusing to record a projection attempt against nothing."
            )
        content_sha256 = act_row[0]

        conn.execute("BEGIN IMMEDIATE")
        try:
            next_sequence = 1 + (conn.execute(
                "SELECT COALESCE(MAX(attempt_sequence), 0) FROM outward_projection_attempts "
                "WHERE outward_event_id = ?",
                (outward_event_id,),
            ).fetchone()[0])
            projection_attempt_id = generate_outward_event_id()
            created_at = int(time.time())
            conn.execute(
                "INSERT INTO outward_projection_attempts (projection_attempt_id, outward_event_id, "
                "attempt_sequence, content_sha256, status, attempted_at, observed_at, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (projection_attempt_id, outward_event_id, next_sequence, content_sha256, status,
                 attempted_at, observed_at, detail, created_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "projection_attempt_id": projection_attempt_id,
            "outward_event_id": outward_event_id,
            "attempt_sequence": next_sequence,
            "content_sha256": content_sha256,
            "status": status,
            "attempted_at": attempted_at,
            "observed_at": observed_at,
            "detail": detail,
        }
    finally:
        conn.close()


def fetch_projection_attempts(data_dir: str, outward_event_id: str) -> list:
    """Read-only, ordered by attempt_sequence. Empty list means
    'projection not attempted' -- there is no synthesized row for that
    state."""
    _require_text("outward_event_id", outward_event_id)
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT projection_attempt_id, attempt_sequence, content_sha256, status, "
            "attempted_at, observed_at, detail FROM outward_projection_attempts "
            "WHERE outward_event_id = ? ORDER BY attempt_sequence",
            (outward_event_id,),
        ).fetchall()
        return [
            {
                "projection_attempt_id": r[0], "attempt_sequence": r[1], "content_sha256": r[2],
                "status": r[3], "attempted_at": r[4], "observed_at": r[5], "detail": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


def record_reply_link(data_dir: str, *, outward_event_id: str, human_event_id: str, linked_at: int) -> dict:
    """Purely mechanical linkage from an already-committed, genuine
    human_waking_input event back to an already-committed
    clark_outward_act event. Takes only two event_ids and a timestamp
    -- no text, no similarity matching, no inference. A malformed or
    nonexistent reference on either side is rejected by
    oc0_schema_migration.py's own trg_outward_reply_links_refs_exist
    insert trigger (surfaces here as sqlite3.IntegrityError, wrapped
    into OutwardCommunicationError), never silently accepted.

    Chronology (enforced by a SEPARATE insert trigger,
    trg_outward_reply_links_chronology -- kept apart from the
    reference-existence trigger above specifically so it installs on
    an already-migrated database via CREATE TRIGGER IF NOT EXISTS,
    never only on a fresh one; from canonical occurred_at, never from
    caller-supplied linked_at): a human event
    canonically earlier than the outward act is rejected -- a reply
    link must not assert a chronology contradicted by the referenced
    canonical events. linked_at itself must not predate either
    referenced event's occurred_at either. occurred_at is
    second-resolution and the only canonical chronology field
    available (event_id ULIDs here carry a random, not counter-based,
    sub-millisecond tail, so they are not a safe secondary ordering
    signal), so only a PROVEN-earlier human event is rejected; equal
    occurred_at values are accepted -- not established as earlier,
    never invented as a false strict ordering. This is the narrowest
    fail-closed rule the existing chronology convention actually
    supports, not artificial sub-second precision. On an idempotent
    replay (below), this chronology check never re-runs -- the
    existing canonical row is returned unchanged, exactly as it was
    validated at its original insert.

    Idempotent: relinking the SAME (outward_event_id, human_event_id)
    pair returns the existing link unchanged. human_event_id is UNIQUE
    at the schema level -- attempting to link the same human event to
    a DIFFERENT outward act is refused, since a real human reply
    genuinely references at most one prior outward act by construction
    here (v0 scope; a human event that is not a reply to any outward
    act simply never appears in this table at all, which is the
    default/ordinary case)."""
    _require_text("outward_event_id", outward_event_id)
    _require_text("human_event_id", human_event_id)
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing = conn.execute(
            "SELECT reply_link_id, outward_event_id, linked_at FROM outward_act_reply_links "
            "WHERE human_event_id = ?",
            (human_event_id,),
        ).fetchone()
        if existing is not None:
            existing_reply_link_id, existing_outward_event_id, existing_linked_at = existing
            if existing_outward_event_id != outward_event_id:
                raise OutwardCommunicationError(
                    f"human_event_id={human_event_id!r} is already linked to a different "
                    f"outward_event_id ({existing_outward_event_id!r} != {outward_event_id!r})."
                )
            return {
                "reply_link_id": existing_reply_link_id, "outward_event_id": outward_event_id,
                "human_event_id": human_event_id, "linked_at": existing_linked_at,
            }

        # Replays above return stored chronology, even historical chronology;
        # validate only the timestamp that would actually be newly persisted.
        _require_timestamp("linked_at", linked_at)
        reply_link_id = generate_outward_event_id()
        try:
            conn.execute(
                "INSERT INTO outward_act_reply_links (reply_link_id, outward_event_id, human_event_id, linked_at) "
                "VALUES (?, ?, ?, ?)",
                (reply_link_id, outward_event_id, human_event_id, linked_at),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise OutwardCommunicationError(
                f"cannot record reply link outward_event_id={outward_event_id!r} -> "
                f"human_event_id={human_event_id!r}: {exc}"
            ) from exc

        return {
            "reply_link_id": reply_link_id, "outward_event_id": outward_event_id,
            "human_event_id": human_event_id, "linked_at": linked_at,
        }
    finally:
        conn.close()


def fetch_reply_link_for_human_event(data_dir: str, human_event_id: str) -> dict:
    """Read-only. Returns None if this human event has never been
    explicitly linked to any outward act -- the default, ordinary case
    for a human message that is not a reply to a Clark-initiated act."""
    _require_text("human_event_id", human_event_id)
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT reply_link_id, outward_event_id, linked_at FROM outward_act_reply_links "
            "WHERE human_event_id = ?",
            (human_event_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "reply_link_id": row[0], "outward_event_id": row[1],
            "human_event_id": human_event_id, "linked_at": row[2],
        }
    finally:
        conn.close()
