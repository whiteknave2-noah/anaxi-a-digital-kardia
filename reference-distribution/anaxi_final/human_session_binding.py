"""SLP1-A4c: durable human-session binding + per-call human-input
authority + canonical human_waking_input (H) event writer.

Reuses EXISTING canonical tables only -- persons, actors,
actor_human_person, sessions, auth_contexts, events, event_components.
NO SCHEMA MIGRATION *of its own*: the FS1 visibility_scope column /
family tables are ensured by the additive fs1_schema_migration, applied
here on writable connections (and only when a scope is actually being
used; pre-FS1 databases keep working untouched).

Two structurally distinct facts, kept apart exactly as SLP1-A4b's own
audit insisted they must be, and as SLP1-A4c's correction 1 sharpened:

  1. SESSION BINDING (`bind_session_to_registered_human`) -- a durable,
     one-time, EXPLICIT, host-mediated act naming which registered
     human actor a waking session belongs to. Established once per
     session, at low assurance, never re-derived per turn, never
     inferred from USER_ID="nate", localhost, "exactly one registered
     human," current OS user, a caller's own claimed actor_id, or text
     content.

  2. PER-CALL AUTHORITY (`HumanInputAuthority`, re-validated fresh by
     `validate_human_input_authority` on EVERY call) -- session binding
     ALONE is not sufficient proof that one particular waking-function
     invocation genuinely originated from the human-facing ingress: an
     unrelated internal caller could invoke the same waking function
     while a session happens to be bound. A bare session_id is never
     enough; the caller must present the exact auth_context_id/
     actor_id the binding itself established, and every field is
     re-checked against canonical state before any human attribution
     is authorized. A HumanInputAuthority is NOT a general
     authorization token, NOT a file capability, NOT a secret, NOT a
     private-workspace credential -- it proves exactly one narrow
     fact when it validates, nothing more.

Ordering law (SLP1-A4c correction 2), enforced by the CALLER
(llama_anaxi.run_waking_turn()), not by this module: for a positively-
authorized call, the canonical human_waking_input event H this module
writes MUST commit before that exact text is ever delivered to a model
call. If H cannot be committed, the caller must abort before any model
call -- never silently downgrade to unauthenticated and proceed.
"""
import dataclasses
import hashlib
import sqlite3
import time
from typing import Optional

from migrate_historical_data import resolve_pipeline_id
from native_provenance_writer import generate_native_ulid
from family_membership import (
    VALID_SCOPES,
    active_family_principals,
    apply_fs1_scope_migration_on_connection,
    family_feature_used,
)

_HUMAN_ACTOR_TYPE = "human_person"
_AUTH_METHOD_SESSION_BINDING = "local_session_start_binding"
_ASSURANCE_LEVEL = "low"
_ALLOWED_ASSURANCE_LEVELS = ("low", "medium", "high")

HUMAN_WAKING_INPUT_EVENT_TYPE = "human_waking_input"
HUMAN_CONVERSATIONAL_INPUT_COMPONENT_KIND = "human_conversational_input"
# The owner authors a note into the shared vault from the owner panel: one canonical event, authored by the
# owner's own authenticated actor, binding the note's path and body hash (never the body itself).
OWNER_VAULT_NOTE_EVENT_TYPE = "owner_vault_note"
OWNER_VAULT_NOTE_BINDING_COMPONENT_KIND = "owner_vault_note_binding"


class HumanSessionBindingError(Exception):
    """Raised for any binding precondition failure. Never partially
    applied -- either the binding transaction fully commits or nothing
    is written."""


class HumanWakingInputWriteError(Exception):
    """Raised when the canonical human_waking_input event H cannot be
    committed. The caller (run_waking_turn()) MUST treat this as
    'abort before model inference' -- never as a silent fallback to
    unauthenticated delivery."""


@dataclasses.dataclass(frozen=True)
class HumanInputAuthority:
    """The smallest immutable per-call proof object. Carries only
    mechanically necessary identity/provenance fields -- no secrets, no
    semantic metadata, no general capability. `authority_source` is a
    plain descriptive label (e.g. "human_facing_ingress"), never
    itself trusted as proof; only session_id/auth_context_id/
    authenticated_actor_id are ever checked against canonical state.

    FS1: `visibility_scope` is the binding's frozen scope (None = the
    legacy unbound/unfiltered session; 'principal_private' or
    'family_shared' = a scoped session). It is part of the binding and
    re-validated freshly on every validation call -- a caller cannot
    attach a scope a binding never received."""
    session_id: str
    auth_context_id: str
    authenticated_actor_id: str
    authority_source: str = "human_facing_ingress"
    visibility_scope: Optional[str] = None


def _auth_contexts_has_scope_column(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
    return "visibility_scope" in cols


def _resolve_actor_type(conn: sqlite3.Connection, actor_id: str) -> Optional[str]:
    row = conn.execute("SELECT actor_type FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()
    return row[0] if row is not None else None


def _actor_is_registered_human(conn: sqlite3.Connection, actor_id: Optional[str]) -> bool:
    """A registered human actor is one that BOTH resolves to
    actor_type='human_person' AND has a genuine actor_human_person
    link row (established only by hir1_registration.py's own
    register_canonical_human(), never fabricated here). Never infers
    registration from actor_type alone."""
    if actor_id is None:
        return False
    if _resolve_actor_type(conn, actor_id) != _HUMAN_ACTOR_TYPE:
        return False
    row = conn.execute("SELECT 1 FROM actor_human_person WHERE actor_id = ?", (actor_id,)).fetchone()
    return row is not None


def _existing_binding_row(conn: sqlite3.Connection, session_id: str):
    """The most recent REAL (auth_state='authenticated') auth_contexts
    row for this session, or None. Never returns a synthetic/unknown
    per-turn row (those are Clark's own waking_turn auth_contexts,
    structurally distinguishable by auth_state alone). FS1: also
    returns the row's frozen visibility_scope when the column exists
    (pre-FS1 databases simply expose None)."""
    scope_expr = (
        "visibility_scope, " if _auth_contexts_has_scope_column(conn) else "NULL AS visibility_scope, "
    )
    return conn.execute(
        "SELECT auth_context_id, authenticated_actor_id, auth_method, assurance_level, "
        f"{scope_expr}"
        "established_at FROM auth_contexts WHERE session_id = ? AND auth_state = 'authenticated' "
        "ORDER BY established_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def bind_session_to_registered_human(
    conn: sqlite3.Connection, *, session_id: str, session_started_at: int, pipeline_key: str, actor_id: str,
    visibility_scope: Optional[str] = None,
) -> HumanInputAuthority:
    """Explicit, host-mediated, one-time act. `actor_id` is REQUIRED
    and always caller-supplied -- multi-human compatible by
    construction; this function never assumes "the one registered
    human" and never resolves an actor implicitly.

    FS1: `visibility_scope` (None | 'principal_private' | 'family_shared')
    freezes the session's scope as part of the binding. A scoped bind
    is permitted ONLY for an already-registered human who is a
    currently-ACTIVE family principal (owner or enrolled member) --
    a stranger cannot self-attach a scope, and a deactivated member
    cannot re-claim one. The scope column/tables are ensured idempotently
    only when a scope is actually used; unbound bindings keep today's
    exact schema and semantics.

    Preconditions (fail closed via HumanSessionBindingError; nothing
    written on any failure):
      - actor_id resolves to a genuine, ALREADY-registered human_person
        actor (actor_type='human_person' AND a real actor_human_person
        row) -- this function performs no registration of its own.

    Idempotency/immutability (kept simple by design):
      - binding an UNBOUND session to P succeeds, writing one real
        auth_contexts row.
      - repeating the IDENTICAL binding (same session_id, same actor_id,
        same scope) returns the EXISTING authority unchanged -- no
        duplicate row.
      - binding an already-P-bound session to a DIFFERENT human Q, or to
        P with a DIFFERENT scope, fails closed (the scope is part of the
        binding). v1 never implements "latest auth_context wins" identity
        switching inside one session; a human or scope change requires a
        new session.

    Honest, low-assurance semantics: `auth_state='authenticated'`,
    `assurance_level='low'`, `auth_method='local_session_start_binding'`
    -- never 'medium'/'high', never a claim of legal identity.
    """
    if visibility_scope is not None and visibility_scope not in VALID_SCOPES:
        raise HumanSessionBindingError(
            f"visibility_scope={visibility_scope!r} is not a known scope "
            f"({', '.join(VALID_SCOPES)}); refuse to bind."
        )
    if not _actor_is_registered_human(conn, actor_id):
        raise HumanSessionBindingError(
            f"actor_id={actor_id!r} does not resolve to an already-registered human_person "
            f"actor -- refusing to bind. This function performs no registration of its own; "
            f"registration must already exist (see hir1_registration.py)."
        )
    if visibility_scope is None and family_feature_used(conn):
        raise HumanSessionBindingError(
            "the family feature has canonical membership history; a human session must "
            "bind an explicit principal_private or family_shared visibility_scope"
        )
    if visibility_scope is not None:
        if not family_feature_used(conn):
            raise HumanSessionBindingError(
                "scoped binding requires canonical family membership history; "
                "missing/empty membership state is not active enrollment"
            )
        apply_fs1_scope_migration_on_connection(conn)
        if actor_id not in active_family_principals(conn):
            raise HumanSessionBindingError(
                f"actor_id={actor_id!r} is not a currently-active family principal -- scoped "
                f"binding is reserved for active family principals; refuse to bind."
            )

    pipeline_id = resolve_pipeline_id(conn, pipeline_key)
    if pipeline_id is None:
        raise HumanSessionBindingError(
            f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id -- "
            f"seed_reference_data() must run before any binding."
        )

    existing = _existing_binding_row(conn, session_id)
    if existing is not None:
        existing_auth_context_id, existing_actor_id, _existing_method, _existing_assurance, existing_scope, _established = existing
        if existing_actor_id != actor_id:
            raise HumanSessionBindingError(
                f"session_id={session_id!r} is already bound to actor {existing_actor_id!r} -- "
                f"refusing to rebind to a different human actor {actor_id!r} within the same "
                f"session. Start a new session to switch which human is present."
            )
        if existing_scope != visibility_scope:
            raise HumanSessionBindingError(
                f"session_id={session_id!r} is already bound to actor {actor_id!r} with "
                f"visibility_scope={existing_scope!r} -- refusing to rebind with a different "
                f"scope {visibility_scope!r}. Start a new session to change scope."
            )
        return HumanInputAuthority(
            session_id=session_id, auth_context_id=existing_auth_context_id,
            authenticated_actor_id=existing_actor_id,
            visibility_scope=existing_scope,
        )

    session_row = conn.execute("SELECT pipeline_id, started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session_row is None:
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, ?, ?)",
            (session_id, pipeline_id, session_started_at),
        )

    auth_context_id = generate_native_ulid()
    established_at = int(time.time())
    scope_columns = ", visibility_scope" if visibility_scope is not None else ""
    scope_placeholder = ", ?" if visibility_scope is not None else ""
    scope_values = (visibility_scope,) if visibility_scope is not None else ()
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, "
        "authenticated_actor_id, auth_state, auth_method, assurance_level, established_at"
        f"{scope_columns}) VALUES (?, ?, ?, ?, 'authenticated', ?, ?, ?{scope_placeholder})",
        (auth_context_id, session_id, actor_id, actor_id, _AUTH_METHOD_SESSION_BINDING, _ASSURANCE_LEVEL, established_at) + scope_values,
    )
    conn.commit()
    return HumanInputAuthority(
        session_id=session_id, auth_context_id=auth_context_id,
        authenticated_actor_id=actor_id, visibility_scope=visibility_scope,
    )


def resolve_bound_human_for_session(conn: sqlite3.Connection, session_id: str) -> Optional[HumanInputAuthority]:
    """Read-only. Returns a HumanInputAuthority for the session's
    existing real binding, or None if the session has never been
    bound. Never creates a binding; never guesses."""
    existing = _existing_binding_row(conn, session_id)
    if existing is None:
        return None
    auth_context_id, actor_id, _method, _assurance, scope, _established = existing
    return HumanInputAuthority(
        session_id=session_id, auth_context_id=auth_context_id,
        authenticated_actor_id=actor_id, visibility_scope=scope,
    )


def validate_human_input_authority(conn: sqlite3.Connection, authority: Optional[HumanInputAuthority], *, session_id: str) -> bool:
    """SLP1-A4c correction 1: per-call re-validation. A caller
    presenting `authority` is NOT trusted merely for possessing an
    object shaped like one -- every field is re-checked fresh against
    canonical state:
      - authority.session_id must equal the ACTUAL waking session_id
        for THIS call (never trust a stale/foreign session_id);
      - authority.auth_context_id must exist;
      - that auth_context must belong to that same session_id;
      - its auth_state must be exactly 'authenticated';
      - its assurance_level must be an allowed tier (low/medium/high --
        'none'/NULL is rejected);
      - its authenticated_actor_id must equal authority.authenticated_actor_id;
      - that actor must STILL resolve as a genuine registered
        human_person (re-checked fresh, never cached, never assumed
        stable since binding time).

    FS1 additions (same never-trust discipline):
      - authority.visibility_scope (None | 'principal_private' |
        'family_shared') must be a known scope and must EXACTLY equal
        the scope the binding itself stored -- a caller cannot attach a
        scope a binding never received;
      - when the session IS scoped, the principal must STILL be a
        currently-active family principal: a principal deactivated
        after binding has its stale binding fail validation (revocation
        fails closed).

    Returns False for any ordinary mismatch (the caller decides what
    "no human attribution" means for its own control flow -- normally,
    fall back to today's unbound behavior). Never raises for an
    ordinary mismatch."""
    if not isinstance(authority, HumanInputAuthority):
        # Covers both None and any bare string/dict/other caller
        # mistake -- a bare session_id or any other non-authority
        # value is never sufficient, structurally, not merely by
        # convention.
        return False
    if authority.session_id != session_id:
        return False
    if authority.visibility_scope is not None and authority.visibility_scope not in VALID_SCOPES:
        return False
    row = conn.execute(
        "SELECT session_id, auth_state, authenticated_actor_id, assurance_level "
        "FROM auth_contexts WHERE auth_context_id = ?",
        (authority.auth_context_id,),
    ).fetchone()
    if row is None:
        return False
    row_session_id, auth_state, authenticated_actor_id, assurance_level = row
    if row_session_id != session_id:
        return False
    if auth_state != "authenticated":
        return False
    if assurance_level not in _ALLOWED_ASSURANCE_LEVELS:
        return False
    if authenticated_actor_id != authority.authenticated_actor_id:
        return False
    if not _actor_is_registered_human(conn, authenticated_actor_id):
        return False
    stored_scope = None
    if _auth_contexts_has_scope_column(conn):
        scope_row = conn.execute(
            "SELECT visibility_scope FROM auth_contexts WHERE auth_context_id = ?",
            (authority.auth_context_id,),
        ).fetchone()
        stored_scope = None if scope_row is None else scope_row[0]
    if authority.visibility_scope != stored_scope:
        return False
    if authority.visibility_scope is not None and authenticated_actor_id not in active_family_principals(conn):
        return False
    return True


def validate_existing_human_input_continuation(
    conn: sqlite3.Connection,
    authority: Optional[HumanInputAuthority],
    *,
    current_session_id: str,
    human_input_event_id: str,
    prompt: str,
):
    """Validate reuse of one canonical, still-unanswered H occurrence.

    A recovery continuation may run in a fresh process/session.  The
    current authority therefore has to be valid for *current_session_id*,
    while the historical H is bound to its own original session.  The
    invariant is principal + visibility-scope continuity, not process-
    session identity: the canonical H must have the same authenticated
    human author, the same frozen FS1 scope, and byte-identical text.

    Returns ``(True, "ok")`` or ``(False, reason)``.  It never mutates
    canonical state and never treats possession of an event id as
    authority.
    """
    if not validate_human_input_authority(conn, authority, session_id=current_session_id):
        return False, "invalid_current_authority"

    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    scope_expr = "ev.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    row = conn.execute(
        "SELECT ev.event_type, ec.creator_actor_id, ec.component_text, "
        f"ac.authenticated_actor_id, {scope_expr} "
        "FROM events ev JOIN event_components ec ON ec.event_id = ev.event_id "
        "JOIN auth_contexts ac ON ac.auth_context_id = ev.auth_context_id "
        "WHERE ev.event_id = ? AND ec.sequence = 0",
        (human_input_event_id,),
    ).fetchone()
    if (
        row is None
        or row[0] != HUMAN_WAKING_INPUT_EVENT_TYPE
        or row[1] != authority.authenticated_actor_id
        or row[2] != prompt
        or row[3] != authority.authenticated_actor_id
        or row[4] != authority.visibility_scope
    ):
        return False, "canonical_input_mismatch"

    answered = conn.execute(
        "SELECT 1 FROM event_components ec JOIN events ev ON ev.event_id = ec.event_id "
        "WHERE ec.component_kind = 'human_input_event_id' AND ec.component_text = ? "
        "AND ev.event_type = 'waking_turn' LIMIT 1",
        (human_input_event_id,),
    ).fetchone()
    if answered is not None:
        return False, "already_answered"
    return True, "ok"


HUMAN_INPUT_SOURCE_COMPONENT_KIND = "discord_inbound_source"


def record_human_waking_input(data_dir: str, *, pipeline_key: str, authority: HumanInputAuthority, message: str, occurred_at: int,
                              source_inbound_event_id: Optional[str] = None) -> str:
    """SLP1-A4c: commits the canonical human_waking_input event H in
    ONE atomic transaction. The caller validates before invoking this
    function for early rejection and must invoke it before any model
    call for this submission (see this module's own docstring and
    run_waking_turn()'s ordering contract). This writer additionally
    re-validates the authority inside the same IMMEDIATE
    transaction that appends H, so deactivation or binding drift cannot
    race between an earlier caller-side check and the canonical write.

    FS1: H carries the binding's frozen visibility_scope when the
    session is scoped (the column is ensured idempotently only then;
    unbound calls write today's exact row shape).

    component_text is EXACTLY `message`, byte-for-byte -- no prompt
    expansion, no context, no history, no system instructions, no
    normalized paraphrase. span_start/span_end are NULL: this
    component has no reply-assembly role (unlike bounded_clause/
    conversational_prose on Clark's own waking_turn event), and must
    never overload that unrelated existing meaning.

    Raises HumanWakingInputWriteError on any failure, after a full
    rollback -- the caller MUST propagate this as "abort before model
    inference," never catch-and-continue."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if authority.visibility_scope is not None:
            apply_fs1_scope_migration_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not validate_human_input_authority(conn, authority, session_id=authority.session_id):
                raise HumanWakingInputWriteError(
                    "human input authority is no longer valid at the canonical interaction boundary"
                )
            pipeline_id = resolve_pipeline_id(conn, pipeline_key)
            if pipeline_id is None:
                raise HumanWakingInputWriteError(
                    f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id -- "
                    f"seed_reference_data() must run before any native write."
                )
            event_id = generate_native_ulid()
            record_created_at = int(time.time())
            content_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
            scope_columns = ", visibility_scope" if authority.visibility_scope is not None else ""
            scope_placeholder = ", ?" if authority.visibility_scope is not None else ""
            scope_values = (authority.visibility_scope,) if authority.visibility_scope is not None else ()
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at"
                f"{scope_columns}) VALUES (?, ?, ?, 'known', NULL, ?, NULL, ?, ?{scope_placeholder})",
                (event_id, HUMAN_WAKING_INPUT_EVENT_TYPE, pipeline_id, authority.auth_context_id, occurred_at, record_created_at) + scope_values,
            )
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
                "VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                (event_id, authority.authenticated_actor_id, HUMAN_CONVERSATIONAL_INPUT_COMPONENT_KIND, message, content_sha256),
            )
            if source_inbound_event_id is not None:
                # A Caret-originated H (a mapped correspondent's exact words, in that principal's own shared
                # session).  The marker is the mechanical, canonical fact that this H did not come from the Mac
                # window; it links H to the inbound event and never alters component 0.
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
                    "VALUES (?, 1, (SELECT actor_id FROM actors WHERE actor_type='host_system' "
                    "AND stable_key='bounded_clause_renderer'), ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, HUMAN_INPUT_SOURCE_COMPONENT_KIND, source_inbound_event_id,
                     hashlib.sha256(source_inbound_event_id.encode("utf-8")).hexdigest()),
                )
            conn.commit()
        except HumanWakingInputWriteError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HumanWakingInputWriteError(f"failed to commit canonical human_waking_input event: {e}") from e
        return event_id
    finally:
        conn.close()


def record_owner_vault_note(data_dir: str, *, pipeline_key: str, authority: HumanInputAuthority,
                            relative_path: str, content_sha256: str, occurred_at: int) -> str:
    """Commit the canonical owner_vault_note event BEFORE the note is written: authored by the bound,
    re-validated human authority (inside the same IMMEDIATE transaction, exactly like H), and only when that
    human is the canonical owner.  The component binds the vault-relative path and the body's sha256; the
    body stays in the vault, not in canonical history.  Raises HumanWakingInputWriteError after rollback."""
    import json as _json
    from family_membership import resolve_owner_actor_id
    conn = sqlite3.connect(f"{data_dir}/anaxi_provenance.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if authority.visibility_scope is not None:
            apply_fs1_scope_migration_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not validate_human_input_authority(conn, authority, session_id=authority.session_id):
                raise HumanWakingInputWriteError("owner authority is no longer valid; nothing was recorded")
            if resolve_owner_actor_id(conn) != authority.authenticated_actor_id:
                raise HumanWakingInputWriteError("only the canonical owner may author a shared-workspace note")
            pipeline_id = resolve_pipeline_id(conn, pipeline_key)
            if pipeline_id is None:
                raise HumanWakingInputWriteError(f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id")
            event_id = generate_native_ulid()
            binding = _json.dumps({"relative_path": relative_path, "content_sha256": content_sha256},
                                  sort_keys=True, separators=(",", ":"))
            scope_columns = ", visibility_scope" if authority.visibility_scope is not None else ""
            scope_placeholder = ", ?" if authority.visibility_scope is not None else ""
            scope_values = (authority.visibility_scope,) if authority.visibility_scope is not None else ()
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at"
                f"{scope_columns}) VALUES (?, ?, ?, 'known', NULL, ?, NULL, ?, ?{scope_placeholder})",
                (event_id, OWNER_VAULT_NOTE_EVENT_TYPE, pipeline_id, authority.auth_context_id, occurred_at,
                 int(time.time())) + scope_values,
            )
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
                "VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                (event_id, authority.authenticated_actor_id, OWNER_VAULT_NOTE_BINDING_COMPONENT_KIND, binding,
                 hashlib.sha256(binding.encode("utf-8")).hexdigest()),
            )
            conn.commit()
        except HumanWakingInputWriteError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HumanWakingInputWriteError(f"failed to commit the owner_vault_note event: {e}") from e
        return event_id
    finally:
        conn.close()


def owner_vault_note_record(conn: sqlite3.Connection, event_id: str) -> Optional[dict]:
    """Read-only: the canonical binding of an owner-authored vault note, or None.  Confirms only a record
    whose single component was authored by the actor who is the canonical owner now."""
    import json as _json
    from family_membership import resolve_owner_actor_id
    row = conn.execute(
        "SELECT c.creator_actor_id, c.component_text, a.display_label FROM events e "
        "JOIN event_components c ON c.event_id = e.event_id AND c.component_kind = ? "
        "LEFT JOIN actors a ON a.actor_id = c.creator_actor_id "
        "WHERE e.event_id = ? AND e.event_type = ?",
        (OWNER_VAULT_NOTE_BINDING_COMPONENT_KIND, event_id, OWNER_VAULT_NOTE_EVENT_TYPE)).fetchone()
    if row is None or row[0] != resolve_owner_actor_id(conn):
        return None
    try:
        binding = _json.loads(row[1])
    except (TypeError, ValueError):
        return None
    return {"actor_id": row[0], "label": row[2] or row[0], "relative_path": binding.get("relative_path"),
            "content_sha256": binding.get("content_sha256")}
