"""
Anaxi -- Native (post-cutover, forward-writing) provenance write path.
Production module: every public function takes a single production
data-directory parameter (`data_dir`) and operates directly against it
-- there is no rehearsal-only aliasing guard here. (migrate_historical_data.py's
own assert_is_rehearsal_dir()/create_rehearsal_dir() remain in place for
that module's own historical-migration rehearsal tooling; this module
never called into a "live" directory it needed to avoid, so it never
needed that guard's live_dir-aliasing check to begin with -- there was
only ever one directory in play here, and Amendment A1 (Identity/Provenance
Schema) scopes this module's production activation to the Llama/Gemma
waking path only.)

Historical events (migrate_historical_data.py) replay something that
already exists in anaxi_log.jsonl -- their event_id is deterministically
derived from that external artifact's own bytes, so a partial migration
can resume by re-deriving the identical ID. Native events are not a
replay of anything: two genuinely distinct synthetic turns could carry
identical prompt/response text and must not collide, and re-running the
SAME turn after a crash must reproduce the SAME event_id, not merely
equivalent content under a fresh one -- so native IDs are minted
(plain 26-character ULIDs, not content-derived) and durably staged
BEFORE any canonical write is attempted, via native_turn_staging.py.

Canonical-before-legacy, one transaction, no partial state:
`stage_and_record_native_waking_turn()` is the only entry point a
caller should use. Its ordering is structural, not conventional: the
staging append (native_turn_staging.py -- write the COMPLETE encoded
line, looping until every byte is confirmed written, then os.fsync();
there is no buffered-flush step for a raw file descriptor) must
complete before the function's body ever reaches the code that opens
a database transaction -- there is no try/except between them that
could paper over a staging failure and proceed anyway. If staging
fails, the function raises before touching anaxi_provenance.db at all.

Per-turn, not per-run: a session may be resolved-or-created once per
process run and reused, but per frozen spec's own write-path
contract (`resolve or create the auth_context_id for the current
turn`), every native turn gets its OWN auth_context row, inserted in
the SAME transaction as its event -- never pre-committed ahead of it,
never shared with another turn. `auth_state='unknown'` means identity
was assessed for this turn and the assessment was unresolved, not
that assessment was skipped; the schema's own CHECK constraint makes
attaching an actor to an 'unknown' row structurally impossible, not
merely discouraged.
"""

import hashlib
import json
import secrets
import sqlite3
import time

from migrate_historical_data import (
    MigrationStopCondition,
    IncompleteEventBundleError,
    _verify_or_raise,
    resolve_pipeline_id,
    resolve_or_create_model_revision,
)
from provenance_schema import derive_stable_id
from native_turn_staging import append_staging_entry, StagingDurabilityError
from family_membership import apply_fs1_scope_migration_on_connection
import boundary_rationale_registry as brr
import external_info_registry as eir
import discord_correspondence_registry as dcr
import discord_correspondence

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Fixed literal participation_note values, factored out so the write
# path (record_native_waking_turn) and every reader that needs to
# identify a specific participation row (verify_native_bundle_contract's
# exact-note check, reconcile_native_turn's model_revision_id lookup)
# use the identical string -- never a duplicated literal that could
# silently drift out of sync.
PASS1_ARTIFACT_PARTICIPATION_NOTE = "pass1 artifact-construction JSON judgment"
PASS2_PROSE_PARTICIPATION_NOTE = "Model backing this native waking turn's pass-2 conversational reply."
# LAWFUL NULL: a turn on which Clark's typed Pass-1 choice was to say nothing.  No Pass-2 call ran, so
# recording PASS2_PROSE_PARTICIPATION_NOTE would be false provenance; the participating model is the
# one that made the typed choice.
PASS1_TYPED_CHOICE_PARTICIPATION_NOTE = (
    "Model backing this native waking turn's pass-1 typed choice; no pass-2 reply was composed."
)
CLARK_REPLY_CHOICE_COMPONENT_KIND = "clark_reply_choice"
REPLY_CHOICE_NO_REPLY = "no_reply"
OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND = "operative_directive_request"
OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND = "operative_directive_text"
_VALID_OPERATIVE_DIRECTIVE_REQUESTS = {"none", "set_directive", "withdraw_directive"}
_VALID_EXTERNAL_INFO_REQUESTS = {"none", "web_search", "fetch_url"}
_VALID_DISCORD_CORRESPONDENCE_REQUESTS = {"none", "send_message", "reply_through_caret"}
_MAX_CARET_REPLY_TEXT_LENGTH = 6000   # == discord_correspondence.MAX_CARET_REPLY_TEXT_LENGTH (pinned by test)


def _validate_operative_directive_action(request, text):
    if request not in _VALID_OPERATIVE_DIRECTIVE_REQUESTS:
        raise IncompleteEventBundleError("invalid operative_directive_request for canonical waking turn")
    if not isinstance(text, str):
        raise IncompleteEventBundleError("operative_directive_text must be text")
    if request == "set_directive" and (not text.strip() or len(text) > 4000):
        raise IncompleteEventBundleError("set_directive requires nonblank directive text of at most 4000 characters")
    if request != "set_directive" and text:
        raise IncompleteEventBundleError("directive text is only valid with set_directive")


def _validate_external_info_action(request, target):
    if request not in _VALID_EXTERNAL_INFO_REQUESTS:
        raise IncompleteEventBundleError("invalid external_info_request for canonical waking turn")
    if not isinstance(target, str):
        raise IncompleteEventBundleError("external_info_target must be text")
    if request != "none" and (not target.strip() or len(target) > 2000):
        raise IncompleteEventBundleError("a non-none external_info_request requires nonblank target of at most 2000 characters")
    if request == "none" and target:
        raise IncompleteEventBundleError("external_info_target is only valid with a non-none external_info_request")


def _validate_discord_correspondence_action(request, destination_id, message_text):
    """Shape-only validation, mirroring _validate_external_info_action
    exactly. Whether the named destination is actually owner-authorized
    is decided later, by discord_correspondence.dispatch_outbound() --
    this writer stays a thin, dependency-light recorder and never
    authorizes anything itself."""
    if request not in _VALID_DISCORD_CORRESPONDENCE_REQUESTS:
        raise IncompleteEventBundleError("invalid discord_correspondence_request for canonical waking turn")
    if not isinstance(destination_id, str) or not isinstance(message_text, str):
        raise IncompleteEventBundleError("discord destination id / message text must be text")
    if request == "none":
        if destination_id or message_text:
            raise IncompleteEventBundleError(
                "discord destination id / message text are only valid with send_message"
            )
        return
    if not destination_id.strip():
        raise IncompleteEventBundleError("send_message requires a nonblank discord_destination_id")
    # A Caret reply is ONE canonical reply; its transport may span up to three Discord messages, so its
    # cap is the multipart transport cap (3 x 2000).  The general send_message action keeps 2000.
    cap = _MAX_CARET_REPLY_TEXT_LENGTH if request == "reply_through_caret" else 2000
    if not message_text.strip() or len(message_text) > cap:
        raise IncompleteEventBundleError(
            f"{request} requires nonblank discord_message_text of at most {cap} characters"
        )


def _validate_caret_reply_binding(request, destination_id, message_text, clark_prose, inbound_event_ids):
    """A Caret occasion reply is exactly Clark's own Pass-2 words, sent to the one destination of
    the inbound message this same turn carries.  Anything else cannot be canonicalized."""
    if request != "reply_through_caret":
        return
    if message_text != clark_prose:
        raise IncompleteEventBundleError("caret reply body must be exactly Clark's own reply prose")
    if not inbound_event_ids:
        raise IncompleteEventBundleError("caret reply requires a carried inbound Caret message")
    from outward_expression import is_outward_safe_expression
    if not is_outward_safe_expression(message_text):
        raise IncompleteEventBundleError("caret reply body is not a conversational outward expression")


def _validate_subject_reply_choice(choice, clark_prose, discord_request, caret_route_intent):
    if choice is None:
        return
    if choice != REPLY_CHOICE_NO_REPLY:
        raise IncompleteEventBundleError(f"unknown subject reply choice {choice!r}")
    if clark_prose:
        raise IncompleteEventBundleError("a no_reply turn carries no conversational prose")
    if discord_request == "reply_through_caret" or caret_route_intent:
        raise IncompleteEventBundleError("a no_reply turn cannot bind a reply route")


# GENERAL CORRESPONDENCE components (correspondence_surfaces), in fixed order after every other
# component: (kind, author) -- the source ref is a HOST fact; the rest are Clark's typed choices.
_CORRESPONDENCE_COMPONENTS = (
    ("correspondent_source_ref", "host"),
    ("correspondent_standing_request", "clark"),
    ("discord_correspondent_ref", "clark"),
    ("withdraw_pending_send_request", "clark"),
)


def _correspondence_component_values(correspondent_source_ref, correspondent_standing_request,
                                     discord_correspondent_ref, withdraw_pending_send_request):
    values = {"correspondent_source_ref": correspondent_source_ref,
              "correspondent_standing_request": correspondent_standing_request,
              "discord_correspondent_ref": discord_correspondent_ref,
              "withdraw_pending_send_request": withdraw_pending_send_request}
    for kind, value in values.items():
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise IncompleteEventBundleError(f"{kind} must be a short nonempty string when present")
    request = correspondent_standing_request
    if request is not None and not (request.startswith("establish:discord_user:") or request.startswith("revoke:discord_user:")):
        raise IncompleteEventBundleError("correspondent_standing_request must be '<establish|revoke>:<source ref>'")
    return values


def _validate_caret_occasion_attribution(correspondent_id, route_intent, request, inbound_event_ids,
                                         source_ref=None):
    """Caret occasion attribution facts: only a turn that carries exactly one inbound Caret message
    can name a correspondent; route intent needs one; a dispatched reply implies the intent."""
    if correspondent_id is not None and (
        not isinstance(correspondent_id, str) or not correspondent_id
        or len(inbound_event_ids or []) != 1
    ):
        raise IncompleteEventBundleError(
            "a Caret correspondent principal requires exactly one carried inbound Caret message"
        )
    if source_ref is not None and (correspondent_id is not None or len(inbound_event_ids or []) != 1):
        raise IncompleteEventBundleError(
            "a correspondent source requires exactly one carried inbound message and no principal")
    if route_intent and correspondent_id is None and source_ref is None:
        raise IncompleteEventBundleError("caret reply route intent requires an attributed Caret occasion")
    if request == "reply_through_caret" and not route_intent:
        raise IncompleteEventBundleError("a dispatched Caret reply requires its recorded route intent")


def _verify_external_info_result_for_canonical_carriage(
    conn, query_event_id, *, session_id, pipeline_id, waking_occurred_at,
):
    """Validate the exact already-persisted external-info result before
    the real waking transaction records that its Pass-2 composition
    included it -- mirrors _verify_boundary_result_for_canonical_
    carriage() exactly, substituting the external-info event type/
    component kind."""
    if not isinstance(query_event_id, str) or not query_event_id:
        raise IncompleteEventBundleError("delivered_external_info_query_event_id must be nonempty text")
    row = conn.execute(
        "SELECT e.event_type, e.pipeline_id, e.pipeline_provenance_status, e.occurred_at, a.session_id "
        "FROM events e JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ?", (query_event_id,),
    ).fetchone()
    if row is None:
        raise IncompleteEventBundleError(
            f"external-info query {query_event_id!r} does not exist; canonical carriage is impossible."
        )
    event_type, query_pipeline_id, provenance_status, query_occurred_at, query_session_id = row
    if (
        event_type != eir.EXTERNAL_INFO_EVENT_TYPE
        or provenance_status != "known"
        or query_pipeline_id != pipeline_id
        or query_session_id != session_id
        or query_occurred_at > waking_occurred_at
    ):
        raise IncompleteEventBundleError(
            f"external-info query {query_event_id!r} does not match this waking turn's canonical "
            f"session/pipeline/chronology."
        )
    has_result = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (query_event_id, eir.EXTERNAL_INFO_RESULT_COMPONENT_KIND),
    ).fetchone()
    if has_result is None:
        raise IncompleteEventBundleError(
            f"external-info query {query_event_id!r} has no persisted result to carry."
        )
    spec_rows = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (query_event_id, eir.EXTERNAL_INFO_QUERY_SPEC_COMPONENT_KIND),
    ).fetchall()
    if len(spec_rows) != 1:
        raise IncompleteEventBundleError("external-info query has invalid canonical spec cardinality")
    try:
        spec = json.loads(spec_rows[0][0])
        result = json.loads(has_result[0])
    except (TypeError, ValueError) as exc:
        raise IncompleteEventBundleError("external-info query/result JSON is malformed") from exc
    if (
        not isinstance(spec, dict) or not isinstance(result, dict)
        or result.get("retrieval_id") != query_event_id
        or result.get("operation") != spec.get("operation")
        or result.get("target") != spec.get("target")
    ):
        raise IncompleteEventBundleError(
            "external-info result identity disagrees with its canonical query spec"
        )


def generate_native_ulid() -> str:
    """Plain 26-character ULID: 48-bit millisecond timestamp + 80-bit
    cryptographically random tail, Crockford Base32-encoded. No prefix
    -- native-vs-historical origin is distinguished structurally, by
    the absence of any event_migration_status/legacy_routing_provenance
    row (see verify_native_bundle_contract()), never by ID shape. No
    third-party dependency, matching this project's self-contained-
    script convention."""
    ts_ms = int(time.time() * 1000)
    raw = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _reassemble_reply_from_canonical(conn: sqlite3.Connection, event_id: str) -> str:
    """The reply returned to a caller must reflect canonical truth --
    what is actually committed in event_components -- never a
    re-assembly of whatever strings the caller happened to pass in.
    Used for every return path, not only the resume branch, so a
    caller can never observe a returned reply that disagrees with
    what verify_native_bundle_contract() would independently confirm.

    Sequence 0 (bounded_clause) and sequence 1 (conversational_prose)
    are each independently optional -- a silent operation_status (e.g.
    not_authorized) legitimately omits sequence 0 entirely rather than
    recording a clause that was never actually shown, matching exactly
    how the caller assembled the reply it displayed."""
    rows = conn.execute(
        "SELECT sequence, component_text FROM event_components WHERE event_id = ? ORDER BY sequence",
        (event_id,),
    ).fetchall()
    texts = {seq: text for seq, text in rows}
    if 0 in texts and 1 in texts:
        return f"{texts[0]} {texts[1]}"
    if 0 in texts:
        return texts[0]
    if 1 in texts:
        return texts[1]
    return ""


def _expected_model_revision_id(waking_model_tag: str) -> str:
    """model_revision_id is deterministic from the tag alone
    (resolve_or_create_model_revision()'s own derivation) -- computing
    the expected value here, rather than trusting whatever the DB
    happens to contain, is what makes the exact-verification below
    genuinely exact rather than merely self-referential."""
    return derive_stable_id("modelrev", waking_model_tag, "tag_only_degraded")


_EVENT_RELIANCE_TABLES = (
    "event_subjects", "event_requesters", "event_relied_upon_consent",
    "event_relied_upon_guardian_authorization", "event_relied_upon_persistence_authorization",
)


def _verify_boundary_result_for_canonical_carriage(
    conn, query_event_id, *, session_id, pipeline_id, waking_occurred_at,
):
    """Validate the exact already-persisted result before the real waking
    transaction records that its Pass-2 composition included it."""
    if not isinstance(query_event_id, str) or not query_event_id:
        raise IncompleteEventBundleError("delivered_boundary_query_event_id must be nonempty text")
    row = conn.execute(
        "SELECT e.event_type, e.pipeline_id, e.pipeline_provenance_status, e.occurred_at, a.session_id "
        "FROM events e JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ?", (query_event_id,),
    ).fetchone()
    if row is None:
        raise IncompleteEventBundleError(
            f"boundary query {query_event_id!r} does not exist; canonical carriage is impossible."
        )
    event_type, query_pipeline_id, provenance_status, query_occurred_at, query_session_id = row
    if (
        event_type != brr.BOUNDARY_INSPECTION_EVENT_TYPE
        or provenance_status != "known"
        or query_pipeline_id != pipeline_id
        or query_session_id != session_id
        or query_occurred_at > waking_occurred_at
    ):
        raise IncompleteEventBundleError(
            f"boundary query {query_event_id!r} does not match this waking turn's canonical "
            f"session/pipeline/chronology."
        )
    has_result = conn.execute(
        "SELECT 1 FROM event_components WHERE event_id = ? AND component_kind = ?",
        (query_event_id, brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND),
    ).fetchone()
    if has_result is None:
        raise IncompleteEventBundleError(
            f"boundary query {query_event_id!r} has no persisted result to carry."
        )


def record_native_waking_turn(
    data_dir: str,
    *,
    event_id: str,
    staging_id: str,
    session_id: str,
    session_started_at: int,
    auth_context_id: str,
    prompt: str,
    bounded_clause: str,
    clark_prose: str,
    pipeline_key: str,
    waking_model_tag: str,
    artifact_pass_ran: bool,
    occurred_at: int,
    delivered_episode_run_id: str = None,
    interaction_mode: str = None,
    delivered_active_workspace_event_ids: list = None,
    delivered_boundary_query_event_id: str = None,
    human_input_event_id: str = None,
    operative_directive_request: str = "none",
    operative_directive_text: str = "",
    external_info_request: str = "none",
    external_info_target: str = "",
    delivered_external_info_query_event_id: str = None,
    discord_correspondence_request: str = "none",
    discord_destination_id: str = "",
    discord_message_text: str = "",
    delivered_discord_inbound_event_ids: list = None,
    visibility_scope: str = None,
    caret_correspondent_principal_id: str = None,
    caret_reply_route_intent: bool = False,
    subject_reply_choice: str = None,
    correspondent_source_ref: str = None,
    correspondent_standing_request: str = None,
    discord_correspondent_ref: str = None,
    withdraw_pending_send_request: str = None,
) -> dict:
    """The single atomic canonical transaction for one native waking
    turn. Assumes the caller has ALREADY durably staged this exact
    turn (event_id/staging_id/session_id/session_started_at/
    auth_context_id all pre-minted/resolved and written to the staging
    file before this function is ever called) -- this function never
    mints an ID itself, it only ever uses IDs handed to it, so a
    resumed call after a crash reproduces the same staged logical
    event under the same IDs, with the same deterministic provenance
    content (component text/hashes, model_revision_id, etc.) -- not
    byte-identical rows: record_created_at is intentionally the real
    moment of write and is allowed (expected) to differ between an
    original attempt and a later resumed one, exactly like
    migrate_historical_data.py's migrated_at/occurred_at distinction.

    Idempotent: if event_id already exists (a previous attempt already
    committed), returns the existing bundle's summary WITHOUT writing
    anything -- but "exists" alone is never sufficient to classify it
    as already-committed. verify_native_bundle_contract() is called
    WITH the full expected staged/native turn contract (every value
    this exact call was given), and any disagreement -- wrong event
    type, wrong staged IDs, wrong participation/component content --
    is a hard stop, never silently treated as a match. (Known, narrow,
    disclosed gap: verify_native_bundle_contract()'s expected-contract
    check does not currently include delivered_episode_run_id, so a
    resumed call after a crash does not re-verify the delivery
    acknowledgment component specifically -- resuming a crashed native
    turn is already a rare path, separate from WSP2-P3's own delivery
    guarantee, which is about the FIRST, non-resumed commit.)

    WSP2-P3 (spec section 7): when delivered_episode_run_id is given,
    one additional event_components row (component_kind=
    'episode_context_delivered', component_text=the bare run_id) is
    written in this SAME transaction, immediately after the ordinary
    bounded_clause/conversational_prose components. This is the exact
    mechanism that makes "episode context is acknowledged only after
    the successful waking turn it informed is durably persisted" true:
    if this transaction rolls back for any reason, the acknowledgment
    never exists either, and workspace_episode_provenance.
    find_pending_episode_run_ids() will correctly still list that
    episode as pending.

    WSP2-P4 (spec section 5): when `interaction_mode` is given, one
    additional bounded event_components row (component_kind=
    'waking_interaction_mode', component_text=the literal mode string,
    e.g. 'conversation' or 'task') is written in this SAME transaction.
    This is the one durable, typed-pathway-boundary fact WSP2-P4's
    shared-public-continuity renderers need and that did not exist
    canonically before this gate: without it, there was no way to
    mechanically distinguish a CONVERSATION_MODE waking turn from a
    TASK_MODE one from canonical provenance alone. `interaction_mode`
    is written as a literal string, never interpreted or validated
    against an enum here -- this module stays a thin, dependency-free
    writer, exactly like its handling of every other caller-supplied
    field. Known, narrow, disclosed gap (same shape as
    delivered_episode_run_id above): verify_native_bundle_contract()'s
    expected-contract check does not currently include interaction_mode,
    so a resumed call after a crash does not re-verify this component
    specifically.

    WSP2-P4-P1 (spec section 5/6/9/10): when `delivered_active_workspace_
    event_ids` is given (a list of canonical PUBLIC Space event_ids
    that ACTIVE_WORKSPACE_CONTINUITY_V1 actually rendered into this
    exact turn's prompt), one additional bounded event_components row
    per event_id (component_kind='active_workspace_event_delivered',
    component_text=the bare source event_id) is written in this SAME
    transaction -- exactly the same atomic-with-persistence guarantee
    'episode_context_delivered' above already establishes for Bridge
    C, extended here to per-event granularity for the LIVE (pre-
    closure) delivery path. If this transaction rolls back for any
    reason (including this turn's Pass-1/Pass-2 having already failed
    before persistence is ever reached), none of these rows exist
    either -- a failed waking turn can never falsely acknowledge
    delivery, matching FAIL CLOSED != FAIL DEAD exactly.

    SLP1-A4c: when `human_input_event_id` is given (the caller's own
    ALREADY-committed canonical human_waking_input event H -- see
    human_session_binding.py -- committed strictly BEFORE this turn's
    model call ever ran), one additional host-mechanical
    event_components row (component_kind='human_input_event_id',
    component_text=the bare H event_id, span_start/span_end=NULL, no
    semantic text, no duplicate copy of H's own content) is written in
    THIS SAME transaction, at the next free sequence slot after any
    delivered_active_workspace_event_ids rows. This never changes
    Clark's own authorship or this event's own auth_context/auth_state
    -- Clark remains the creator of sequence=1's conversational_prose,
    and this event's auth_context continues to be constructed exactly
    as before (see the unconditional auth_state='unknown' insert
    below, unchanged) -- the human's authentication belongs to H's own
    auth_context, never retroactively borrowed by X. If this
    transaction rolls back for any reason, the link never exists
    either, but H itself (already committed in its own, earlier,
    independent transaction) remains valid and standalone -- exactly
    representing "human P addressed Clark; Clark did not successfully
    answer," never silently erased by a later failure.

    Boundary Inspector V1: when `delivered_boundary_query_event_id` is
    supplied by the actual Pass-2 composition path, this writer verifies
    that exact persisted query/result against this turn's session,
    pipeline, and chronology, then records its carriage component in the
    SAME transaction as the waking turn.  Later delivery bookkeeping can
    read this fact but cannot create it.

    FS1: when `visibility_scope` ('principal_private'/'family_shared') is
    supplied by the caller (the session's authoritative binding scope),
    the FS1 additive schema is ensured idempotently on this connection
    and the scope is written into BOTH this turn's own auth_context
    row and its `events.visibility_scope`, in this same transaction.
    When `human_input_event_id` is also present, this turn's scope is
    cross-validated against the already-committed H event's own
    `events.visibility_scope` -- any disagreement is a hard stop
    (fail closed: X never silently widens or narrows H's scope, and the
    caller can never attach a scope an H never carried). When
    `visibility_scope` is None (the ordinary, unbound waking turn --
    including every pre-FS1 history) the writer runs literally
    unchanged: no migration, no new columns in any INSERT. The native
    bundle verifier treats scope as load-bearing: event/auth-context
    scope must agree and, on resume/reconciliation, must exactly match
    the staged expected contract."""
    _validate_operative_directive_action(operative_directive_request, operative_directive_text)
    _validate_external_info_action(external_info_request, external_info_target)
    _validate_discord_correspondence_action(
        discord_correspondence_request, discord_destination_id, discord_message_text
    )
    _validate_caret_reply_binding(
        discord_correspondence_request, discord_destination_id, discord_message_text, clark_prose,
        delivered_discord_inbound_event_ids,
    )
    _validate_caret_occasion_attribution(
        caret_correspondent_principal_id, caret_reply_route_intent, discord_correspondence_request,
        delivered_discord_inbound_event_ids, source_ref=correspondent_source_ref,
    )
    correspondence_values = _correspondence_component_values(
        correspondent_source_ref, correspondent_standing_request, discord_correspondent_ref,
        withdraw_pending_send_request)
    _validate_subject_reply_choice(
        subject_reply_choice, clark_prose, discord_correspondence_request, caret_reply_route_intent,
    )
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if visibility_scope is not None:
            apply_fs1_scope_migration_on_connection(conn)
        # FS1-scoped turns require the linked H to prove the same
        # authoritative session/author/scope. Preserve the accepted
        # pre-FS1 unscoped writer contract (legacy recovery fixtures may
        # link an H while reconstructing under a new execution session).
        if human_input_event_id and visibility_scope is not None:
            event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            h_scope_expr = "h.visibility_scope" if "visibility_scope" in event_columns else "NULL"
            h_row = conn.execute(
                "SELECT h.event_type, a.session_id, a.auth_state, a.authenticated_actor_id, "
                f"hc.creator_actor_id, {h_scope_expr} "
                "FROM events h JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
                "JOIN event_components hc ON hc.event_id=h.event_id "
                "WHERE h.event_id=? AND hc.sequence=0 "
                "AND hc.component_kind='human_conversational_input'",
                (human_input_event_id,),
            ).fetchone()
            if (
                h_row is None
                or h_row[0] != "human_waking_input"
                or h_row[1] != session_id
                or h_row[2] != "authenticated"
                or h_row[3] is None
                or h_row[3] != h_row[4]
                or h_row[5] != visibility_scope
            ):
                raise IncompleteEventBundleError(
                    f"human_waking_input event {human_input_event_id!r} does not carry an "
                    "authenticated author/session/scope matching this waking turn"
                )
        pipeline_id = resolve_pipeline_id(conn, pipeline_key)
        if pipeline_id is None:
            raise MigrationStopCondition(
                f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id -- "
                f"seed_reference_data() must run before any native write."
            )

        expected = {
            "staging_id": staging_id, "session_id": session_id,
            "session_started_at": session_started_at, "auth_context_id": auth_context_id,
            "pipeline_id": pipeline_id, "occurred_at": occurred_at,
            "bounded_clause": bounded_clause, "clark_prose": clark_prose,
            "artifact_pass_ran": artifact_pass_ran, "waking_model_tag": waking_model_tag,
            "delivered_boundary_query_event_id": delivered_boundary_query_event_id,
            "operative_directive_request": operative_directive_request,
            "operative_directive_text": operative_directive_text,
            "external_info_request": external_info_request,
            "external_info_target": external_info_target,
            "delivered_external_info_query_event_id": delivered_external_info_query_event_id,
            "discord_correspondence_request": discord_correspondence_request,
            "discord_destination_id": discord_destination_id,
            "discord_message_text": discord_message_text,
            "delivered_discord_inbound_event_ids": delivered_discord_inbound_event_ids,
            "visibility_scope": visibility_scope,
            "caret_correspondent_principal_id": caret_correspondent_principal_id,
            "caret_reply_route_intent": bool(caret_reply_route_intent),
            "subject_reply_choice": subject_reply_choice,
            **correspondence_values,
        }

        existing = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            if not verify_native_bundle_contract(conn, event_id, expected=expected):
                raise IncompleteEventBundleError(
                    f"event_id={event_id!r} exists but is not a complete, valid native bundle -- "
                    f"refusing to treat this as already-migrated."
                )
            return {
                "event_id": event_id,
                "session_id": session_id,
                "auth_context_id": auth_context_id,
                "pipeline_id": pipeline_id,
                "model_participations": _fetch_model_participations(conn, event_id),
                "reassembled_reply": _reassemble_reply_from_canonical(conn, event_id),
            }

        conn.execute("BEGIN")
        try:
            _resolve_or_create_session(conn, session_id, pipeline_id, session_started_at)

            scope_columns = ", visibility_scope" if visibility_scope is not None else ""
            scope_placeholder = ", ?" if visibility_scope is not None else ""
            scope_values = (visibility_scope,) if visibility_scope is not None else ()

            conn.execute(
                "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, "
                "established_at" f"{scope_columns}) VALUES (?, ?, 'unknown', ?{scope_placeholder})",
                (auth_context_id, session_id, occurred_at) + scope_values,
            )

            record_created_at = int(time.time())
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at"
                f"{scope_columns}) VALUES (?, 'waking_turn', ?, 'known', NULL, ?, ?, ?, ?{scope_placeholder})",
                (event_id, pipeline_id, auth_context_id, staging_id, occurred_at, record_created_at) + scope_values,
            )

            model_participations = []

            def _add_participation(note: str):
                model_revision_id = resolve_or_create_model_revision(conn, waking_model_tag, occurred_at)
                conn.execute(
                    "INSERT INTO event_model_participation (event_id, model_revision_id, participation_note) "
                    "VALUES (?, ?, ?)",
                    (event_id, model_revision_id, note),
                )
                model_participations.append({"model_revision_id": model_revision_id, "tag": waking_model_tag})
                return model_revision_id

            if artifact_pass_ran:
                _add_participation(PASS1_ARTIFACT_PARTICIPATION_NOTE)
            prose_model_revision_id = _add_participation(
                PASS1_TYPED_CHOICE_PARTICIPATION_NOTE if subject_reply_choice == REPLY_CHOICE_NO_REPLY
                else PASS2_PROSE_PARTICIPATION_NOTE
            )

            host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
            clark_actor_id = derive_stable_id("actor", "clark")

            # bounded_clause is optional, just like clark_prose below: a
            # silent operation_status (not_authorized) passes bounded_clause
            # == "" and no sequence=0 component is written at all -- never a
            # placeholder empty-text row, since the schema has no invariant
            # requiring one (confirmed against provenance_schema.py's DDL:
            # no CHECK/trigger requires event_components to be nonempty).
            if bounded_clause:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 0, ?, 'bounded_clause', 'resolved', ?, ?, NULL, 0, ?)",
                    (event_id, host_actor_id, bounded_clause,
                     hashlib.sha256(bounded_clause.encode("utf-8")).hexdigest(), len(bounded_clause)),
                )
            if clark_prose:
                span_start = (len(bounded_clause) + 1) if bounded_clause else 0
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 1, ?, 'conversational_prose', 'resolved', ?, ?, ?, ?, ?)",
                    (event_id, clark_actor_id, clark_prose,
                     hashlib.sha256(clark_prose.encode("utf-8")).hexdigest(), prose_model_revision_id,
                     span_start, span_start + len(clark_prose)),
                )

            # WSP2-P3 (spec section 7): the delivery acknowledgment,
            # committed atomically with this exact waking turn -- see
            # this function's own docstring for the invariant this
            # establishes.
            if delivered_episode_run_id:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 2, ?, 'episode_context_delivered', 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, host_actor_id, delivered_episode_run_id,
                     hashlib.sha256(delivered_episode_run_id.encode("utf-8")).hexdigest()),
                )

            # WSP2-P4 (spec section 5): the typed-pathway-boundary
            # marker -- see this function's own docstring. sequence=3,
            # after the existing 0/1/2 slots, so no prior consumer's
            # assumed sequence numbering shifts.
            if interaction_mode:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 3, ?, 'waking_interaction_mode', 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, host_actor_id, interaction_mode,
                     hashlib.sha256(interaction_mode.encode("utf-8")).hexdigest()),
                )

            # WSP2-P4-P1: per-event durable delivery ledger for
            # ACTIVE_WORKSPACE_CONTINUITY_V1 -- see this function's own
            # docstring. sequence continues from 4 (after the fixed
            # 0/1/2/3 slots above), one row per delivered source
            # event_id.
            next_sequence = 4
            for i, delivered_event_id in enumerate(delivered_active_workspace_event_ids or []):
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, 'active_workspace_event_delivered', 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, 4 + i, host_actor_id, delivered_event_id,
                     hashlib.sha256(delivered_event_id.encode("utf-8")).hexdigest()),
                )
                next_sequence = 4 + i + 1

            # Boundary Inspector V1: the actual waking composition path
            # supplies this exact surviving source id to the canonical
            # waking writer.  Validate it against canonical query state,
            # then record carriage in THIS SAME transaction as the turn;
            # no later delivery API can manufacture this component.
            if delivered_boundary_query_event_id is not None:
                _verify_boundary_result_for_canonical_carriage(
                    conn, delivered_boundary_query_event_id,
                    session_id=session_id, pipeline_id=pipeline_id,
                    waking_occurred_at=occurred_at,
                )
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id,
                     brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
                     delivered_boundary_query_event_id,
                     hashlib.sha256(delivered_boundary_query_event_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: exactly the
            # same carriage discipline as Boundary Inspector's own block
            # immediately above -- the actual waking composition path
            # supplies this exact surviving source id; validate it
            # against canonical query state, then record carriage in
            # THIS SAME transaction as the turn. No later delivery API
            # can manufacture this component.
            if delivered_external_info_query_event_id is not None:
                _verify_external_info_result_for_canonical_carriage(
                    conn, delivered_external_info_query_event_id,
                    session_id=session_id, pipeline_id=pipeline_id,
                    waking_occurred_at=occurred_at,
                )
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id,
                     eir.EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND,
                     delivered_external_info_query_event_id,
                     hashlib.sha256(delivered_external_info_query_event_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            # PRIVATE DISCORD CORRESPONDENCE V0: exactly the same
            # carriage discipline as Boundary Inspector / external-info
            # above, extended per-message. The actual waking composition
            # path supplies the surviving inbound source event ids; each
            # is validated against canonical inbound state (correct type,
            # a real content component, not canonically later than this
            # turn), then its carriage is recorded in THIS SAME
            # transaction. No later delivery API can manufacture these
            # components -- record_inbound_delivered() can only read them.
            for inbound_event_id in delivered_discord_inbound_event_ids or []:
                try:
                    discord_correspondence.verify_inbound_event_for_carriage(
                        conn, inbound_event_id, waking_occurred_at=occurred_at,
                        expected_principal_actor_id=caret_correspondent_principal_id,
                        expected_source_ref=correspondent_source_ref,
                    )
                except discord_correspondence.DiscordCorrespondenceError as exc:
                    raise IncompleteEventBundleError(str(exc)) from exc
                if discord_correspondence_request == "reply_through_caret":
                    try:
                        discord_correspondence.verify_reply_destination(
                            conn, inbound_event_id, discord_destination_id,
                        )
                    except discord_correspondence.DiscordCorrespondenceError as exc:
                        raise IncompleteEventBundleError(str(exc)) from exc
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id,
                     dcr.DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND, inbound_event_id,
                     hashlib.sha256(inbound_event_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            if caret_correspondent_principal_id is not None:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id,
                     dcr.CARET_CORRESPONDENT_PRINCIPAL_COMPONENT_KIND, caret_correspondent_principal_id,
                     hashlib.sha256(caret_correspondent_principal_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1
            if caret_reply_route_intent:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id,
                     dcr.CARET_REPLY_ROUTE_INTENT_COMPONENT_KIND, "reply_through_caret",
                     hashlib.sha256(b"reply_through_caret").hexdigest()),
                )
                next_sequence += 1

            # SLP1-A4c: the mechanical H -> X backlink -- see this
            # function's own docstring. Dynamic sequence slot (after
            # however many active_workspace_event_delivered rows were
            # just written) since that list is variable-length.
            if human_input_event_id:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, 'human_input_event_id', 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id, human_input_event_id,
                     hashlib.sha256(human_input_event_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            # OD1: the explicit structured Clark action is canonicalized
            # atomically with the waking turn that produced it.  The later
            # directive occurrence writer accepts only this exact binding.
            if operative_directive_request != "none":
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND,
                     operative_directive_request,
                     hashlib.sha256(operative_directive_request.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1
                if operative_directive_request == "set_directive":
                    conn.execute(
                        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                        "authorship_resolution, component_text, content_sha256, model_revision_id, "
                        "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                        (event_id, next_sequence, clark_actor_id, OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND,
                         operative_directive_text,
                         hashlib.sha256(operative_directive_text.encode("utf-8")).hexdigest()),
                    )
                next_sequence += 1

            # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: exactly the
            # same atomic-with-the-triggering-turn discipline as OD1's
            # own block immediately above -- Clark's explicit typed
            # search/fetch choice is canonicalized atomically with the
            # waking turn that produced it. external_information.py's
            # own record_external_info_query() accepts only this exact
            # committed binding (via its input_source_ref parameter).
            if external_info_request != "none":
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, eir.EXTERNAL_INFO_REQUEST_COMPONENT_KIND,
                     external_info_request,
                     hashlib.sha256(external_info_request.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, eir.EXTERNAL_INFO_TARGET_COMPONENT_KIND,
                     external_info_target,
                     hashlib.sha256(external_info_target.encode("utf-8")).hexdigest()),
                )
                # Advance past the target: without this, any later component of the same turn (a
                # Discord send, a reply choice) collided on (event_id, sequence) and the whole turn
                # failed at persistence.
                next_sequence += 1

            # PRIVATE DISCORD CORRESPONDENCE V0: exactly the same
            # atomic-with-the-triggering-turn discipline as OD1/external-
            # info above -- Clark's explicit typed send choice, the exact
            # destination id, and his exact outgoing words are all
            # canonicalized atomically with the waking turn that produced
            # them. discord_correspondence.dispatch_outbound() later
            # accepts only this committed binding; prose can never create
            # or alter it.
            if discord_correspondence_request != "none":
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id,
                     dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND,
                     discord_correspondence_request,
                     hashlib.sha256(discord_correspondence_request.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND,
                     discord_destination_id,
                     hashlib.sha256(discord_destination_id.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND,
                     discord_message_text,
                     hashlib.sha256(discord_message_text.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            # LAWFUL NULL: Clark's own typed choice to say nothing, canonicalized atomically with the
            # turn it completes.  Authored by Clark (it is his choice, not a host classification).
            if subject_reply_choice is not None:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, clark_actor_id, CLARK_REPLY_CHOICE_COMPONENT_KIND,
                     subject_reply_choice, hashlib.sha256(subject_reply_choice.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            # GENERAL CORRESPONDENCE: whose occasion this is (host fact) and Clark's typed choices.
            for kind, author in _CORRESPONDENCE_COMPONENTS:
                value = correspondence_values[kind]
                if value is None:
                    continue
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, ?, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (event_id, next_sequence, host_actor_id if author == "host" else clark_actor_id, kind,
                     value, hashlib.sha256(value.encode("utf-8")).hexdigest()),
                )
                next_sequence += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "event_id": event_id,
            "session_id": session_id,
            "auth_context_id": auth_context_id,
            "pipeline_id": pipeline_id,
            "model_participations": model_participations,
            "reassembled_reply": _reassemble_reply_from_canonical(conn, event_id),
        }
    finally:
        conn.close()


def stage_and_record_native_waking_turn(
    data_dir: str,
    staging_path: str,
    *,
    session_id: str,
    session_started_at: int,
    user_id: str,
    prompt: str,
    bounded_clause: str,
    clark_prose: str,
    kardia: dict,
    controls: dict,
    waking_model_tag: str,
    pipeline_key: str,
    artifact_pass_ran: bool,
    occurred_at: int,
    delivered_episode_run_id: str = None,
    interaction_mode: str = None,
    delivered_active_workspace_event_ids: list = None,
    delivered_boundary_query_event_id: str = None,
    human_input_event_id: str = None,
    operative_directive_request: str = "none",
    operative_directive_text: str = "",
    external_info_request: str = "none",
    external_info_target: str = "",
    delivered_external_info_query_event_id: str = None,
    discord_correspondence_request: str = "none",
    discord_destination_id: str = "",
    discord_message_text: str = "",
    delivered_discord_inbound_event_ids: list = None,
    visibility_scope: str = None,
    caret_correspondent_principal_id: str = None,
    caret_reply_route_intent: bool = False,
    subject_reply_choice: str = None,
    correspondent_source_ref: str = None,
    correspondent_standing_request: str = None,
    discord_correspondent_ref: str = None,
    withdraw_pending_send_request: str = None,
) -> dict:
    """The one entry point a waking-turn caller should call per turn.
    Mints event_id/auth_context_id, builds the COMPLETE staging
    payload (every noncanonical value needed to reconstruct
    turn_generation_log/anaxi_log.jsonl/relational_events after
    process death -- prompt, bounded_clause, clark_prose, kardia,
    controls, waking_model_tag, pipeline_key, artifact_pass_ran,
    occurred_at, session_id, session_started_at, auth_context_id,
    event_id), stages it durably, and ONLY THEN opens the canonical
    transaction.

    Ordering is structural: append_staging_entry() is called and must
    return successfully BEFORE record_native_waking_turn() is even
    referenced in this function's body -- there is no code path that
    reaches the canonical transaction without a prior successful,
    fsync'd staging write. append_staging_entry() no longer takes a
    line_index -- it now determines the next physical index itself,
    atomically, under its own real cross-process lock (native_turn_staging.py),
    so this function no longer needs (and must not attempt) to compute
    or pass one.

    Two distinct failure outcomes propagate directly to the caller,
    with different retry implications -- nothing in anaxi_provenance.db
    or any legacy store is touched by either: a StagingDurabilityError
    means the failed append was durably rolled back and retry-whole is
    safe; a StagingIndeterminateStateError means the rollback itself
    could not be completed and retry-whole is explicitly FORBIDDEN
    until an operator inspects the raw staging file -- a caller must
    not treat these as interchangeable "just retry" cases."""
    _validate_operative_directive_action(operative_directive_request, operative_directive_text)
    _validate_external_info_action(external_info_request, external_info_target)
    _validate_discord_correspondence_action(
        discord_correspondence_request, discord_destination_id, discord_message_text
    )
    _validate_caret_reply_binding(
        discord_correspondence_request, discord_destination_id, discord_message_text, clark_prose,
        delivered_discord_inbound_event_ids,
    )
    _validate_caret_occasion_attribution(
        caret_correspondent_principal_id, caret_reply_route_intent, discord_correspondence_request,
        delivered_discord_inbound_event_ids, source_ref=correspondent_source_ref,
    )
    correspondence_values = _correspondence_component_values(
        correspondent_source_ref, correspondent_standing_request, discord_correspondent_ref,
        withdraw_pending_send_request)
    _validate_subject_reply_choice(
        subject_reply_choice, clark_prose, discord_correspondence_request, caret_reply_route_intent,
    )
    event_id = generate_native_ulid()
    auth_context_id = generate_native_ulid()

    payload = {
        "session_id": session_id,
        "session_started_at": session_started_at,
        "event_id": event_id,
        "auth_context_id": auth_context_id,
        "user_id": user_id,
        "prompt": prompt,
        "bounded_clause": bounded_clause,
        "clark_prose": clark_prose,
        "kardia": kardia,
        "controls": controls,
        "waking_model_tag": waking_model_tag,
        "pipeline_key": pipeline_key,
        "artifact_pass_ran": artifact_pass_ran,
        "occurred_at": occurred_at,
        "interaction_mode": interaction_mode,
        "delivered_active_workspace_event_ids": delivered_active_workspace_event_ids,
        "delivered_boundary_query_event_id": delivered_boundary_query_event_id,
        "human_input_event_id": human_input_event_id,
        "visibility_scope": visibility_scope,
    }
    if operative_directive_request != "none":
        payload["operative_directive_request"] = operative_directive_request
        payload["operative_directive_text"] = operative_directive_text
    if external_info_request != "none":
        payload["external_info_request"] = external_info_request
        payload["external_info_target"] = external_info_target
    if delivered_external_info_query_event_id is not None:
        payload["delivered_external_info_query_event_id"] = delivered_external_info_query_event_id
    if discord_correspondence_request != "none":
        payload["discord_correspondence_request"] = discord_correspondence_request
        payload["discord_destination_id"] = discord_destination_id
        payload["discord_message_text"] = discord_message_text
    if delivered_discord_inbound_event_ids:
        payload["delivered_discord_inbound_event_ids"] = delivered_discord_inbound_event_ids
    if caret_correspondent_principal_id is not None:
        payload["caret_correspondent_principal_id"] = caret_correspondent_principal_id
    if caret_reply_route_intent:
        payload["caret_reply_route_intent"] = True
    if subject_reply_choice is not None:
        payload["subject_reply_choice"] = subject_reply_choice
    payload.update({k: v for k, v in correspondence_values.items() if v is not None})

    staging_id = append_staging_entry(staging_path, payload)

    canonical_result = record_native_waking_turn(
        data_dir,
        event_id=event_id, staging_id=staging_id, session_id=session_id,
        session_started_at=session_started_at, auth_context_id=auth_context_id,
        prompt=prompt, bounded_clause=bounded_clause, clark_prose=clark_prose,
        pipeline_key=pipeline_key, waking_model_tag=waking_model_tag,
        artifact_pass_ran=artifact_pass_ran, occurred_at=occurred_at,
        delivered_episode_run_id=delivered_episode_run_id,
        interaction_mode=interaction_mode,
        delivered_active_workspace_event_ids=delivered_active_workspace_event_ids,
        delivered_boundary_query_event_id=delivered_boundary_query_event_id,
        human_input_event_id=human_input_event_id,
        operative_directive_request=operative_directive_request,
        operative_directive_text=operative_directive_text,
        external_info_request=external_info_request,
        external_info_target=external_info_target,
        delivered_external_info_query_event_id=delivered_external_info_query_event_id,
        discord_correspondence_request=discord_correspondence_request,
        discord_destination_id=discord_destination_id,
        discord_message_text=discord_message_text,
        delivered_discord_inbound_event_ids=delivered_discord_inbound_event_ids,
        visibility_scope=visibility_scope,
        caret_correspondent_principal_id=caret_correspondent_principal_id,
        caret_reply_route_intent=caret_reply_route_intent,
        subject_reply_choice=subject_reply_choice,
        **correspondence_values,
    )

    return {
        **canonical_result,
        "staging_id": staging_id,
        "user_id": user_id,
        "prompt": prompt,
        "kardia": kardia,
        "controls": controls,
        "occurred_at": occurred_at,
        "waking_model_tag": waking_model_tag,
        "pipeline_key": pipeline_key,
    }


def resume_orphaned_staged_turns(data_dir: str, staging_path: str) -> list:
    """Crash recovery: reads every durably-staged, integrity-verified
    turn (native_turn_staging.py raises StagingIntegrityError on any
    tampered/corrupted record -- propagated here, never swallowed) and,
    for each whose event_id is not yet a committed canonical event,
    re-runs record_native_waking_turn() using the EXACT staged IDs --
    never minting fresh ones, never substituting a later run's session.
    Idempotent: an already-committed turn is skipped via the same
    resume check record_native_waking_turn() already performs.
    Returns a list of per-turn result dicts, in staging order."""
    from native_turn_staging import read_verified_staging_entries

    results = []
    for line_index, payload, staging_id in read_verified_staging_entries(staging_path):
        result = record_native_waking_turn(
            data_dir,
            event_id=payload["event_id"], staging_id=staging_id, session_id=payload["session_id"],
            session_started_at=payload["session_started_at"], auth_context_id=payload["auth_context_id"],
            prompt=payload["prompt"], bounded_clause=payload["bounded_clause"], clark_prose=payload["clark_prose"],
            pipeline_key=payload["pipeline_key"], waking_model_tag=payload["waking_model_tag"],
            artifact_pass_ran=payload["artifact_pass_ran"], occurred_at=payload["occurred_at"],
            # Older staged entries predate WSP2-P4 and have no
            # "interaction_mode" key at all -- .get() degrades to None,
            # matching this parameter's own default and writing no
            # waking_interaction_mode component for that resumed turn,
            # exactly as if it had never been supplied in the first place.
            interaction_mode=payload.get("interaction_mode"),
            delivered_active_workspace_event_ids=payload.get("delivered_active_workspace_event_ids"),
            delivered_boundary_query_event_id=payload.get("delivered_boundary_query_event_id"),
            human_input_event_id=payload.get("human_input_event_id"),
            operative_directive_request=payload.get("operative_directive_request", "none"),
            operative_directive_text=payload.get("operative_directive_text", ""),
            external_info_request=payload.get("external_info_request", "none"),
            external_info_target=payload.get("external_info_target", ""),
            delivered_external_info_query_event_id=payload.get("delivered_external_info_query_event_id"),
            discord_correspondence_request=payload.get("discord_correspondence_request", "none"),
            discord_destination_id=payload.get("discord_destination_id", ""),
            discord_message_text=payload.get("discord_message_text", ""),
            delivered_discord_inbound_event_ids=payload.get("delivered_discord_inbound_event_ids"),
            visibility_scope=payload.get("visibility_scope"),
            caret_correspondent_principal_id=payload.get("caret_correspondent_principal_id"),
            caret_reply_route_intent=bool(payload.get("caret_reply_route_intent", False)),
            subject_reply_choice=payload.get("subject_reply_choice"),
            correspondent_source_ref=payload.get("correspondent_source_ref"),
            correspondent_standing_request=payload.get("correspondent_standing_request"),
            discord_correspondent_ref=payload.get("discord_correspondent_ref"),
            withdraw_pending_send_request=payload.get("withdraw_pending_send_request"),
        )
        results.append({
            **result, "staging_id": staging_id, "user_id": payload["user_id"],
            "kardia": payload["kardia"], "controls": payload["controls"],
        })
    return results


def _find_unambiguous_staging_entry(staging_path: str, event_id: str, expected_staging_id: str) -> dict:
    """Requires EXACTLY ONE staging entry for event_id -- zero is a
    hard stop (nothing to reconstruct from), more than one is a hard
    stop (ambiguous, never silently pick the first). The matched
    entry's OWN staging_id (independently verified by
    read_verified_staging_entries()) must equal expected_staging_id
    (the canonical event's own input_source_ref) -- a disagreement
    means the provenance link itself is broken and must never be
    trusted, regardless of how plausible the payload otherwise looks."""
    from native_turn_staging import read_verified_staging_entries

    matches = [
        (payload, staging_id)
        for _line_index, payload, staging_id in read_verified_staging_entries(staging_path)
        if payload["event_id"] == event_id
    ]
    if len(matches) == 0:
        raise MigrationStopCondition(
            f"event_id={event_id!r} has a complete canonical bundle but no matching staging "
            f"entry -- cannot reconstruct legacy projections without the durable staged source."
        )
    if len(matches) > 1:
        raise MigrationStopCondition(
            f"event_id={event_id!r} matches {len(matches)} staging entries -- ambiguous, "
            f"refusing to pick one arbitrarily."
        )
    payload, staging_id = matches[0]
    if staging_id != expected_staging_id:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: the matched staging entry's own staging_id {staging_id!r} "
            f"does not equal the canonical event's input_source_ref {expected_staging_id!r} -- "
            f"the provenance link disagrees, refusing to trust it."
        )
    return payload


def _check_or_repair_legacy_row(existing_rows: list, expected: dict, exceptions: set, store_name: str) -> str:
    """Shared exact-verification logic for one legacy store. `existing_rows`
    is the list of rows (as dicts) already present for this event_id --
    zero means repair is needed; exactly one is checked field-for-field
    against `expected`, excluding only the narrowly-named fields in
    `exceptions` (execution-time/moment-of-write fields that are
    legitimately allowed to differ); more than one is an unconditional
    hard stop, never resolved by picking one."""
    if len(existing_rows) > 1:
        raise MigrationStopCondition(
            f"{store_name}: {len(existing_rows)} rows already match this event_id -- "
            f"ambiguous/duplicated state, refusing to treat any of them as authoritative."
        )
    if len(existing_rows) == 0:
        return "repair"
    actual = existing_rows[0]
    actual_checked = {k: v for k, v in actual.items() if k not in exceptions}
    expected_checked = {k: v for k, v in expected.items() if k not in exceptions}
    _verify_or_raise(actual_checked, expected_checked, store_name)
    return "already_present"


def reconcile_native_turn(
    data_dir: str, staging_path: str, event_id: str,
    relational_db_path: str, mind_db_path: str, jsonl_path: str,
) -> dict:
    """Repairs missing legacy projections (turn_generation_log,
    anaxi_log.jsonl, relational_events) for an event whose canonical
    bundle is already complete -- reconstructing every value SOLELY
    from durable staged/canonical information (the staging line's
    payload plus what's already committed in anaxi_provenance.db),
    never from in-memory state a crash could have destroyed. Never
    repairs from an incomplete canonical bundle -- that's
    IncompleteEventBundleError, a different, more serious condition
    this function refuses to paper over.

    Per store, an existing matching row is not merely detected -- its
    content is verified field-for-field against what it should be.
    Zero matching rows means repair; exactly one whose fields agree
    (excluding narrowly-named execution-time exceptions) means
    already_present; a content disagreement, or more than one matching
    row, is an unconditional hard stop -- never silently reported as
    already_present, never picked arbitrarily."""
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from migrate_historical_data import resolve_legacy_substrate_label

    prov_conn = sqlite3.connect(f"{data_dir}/anaxi_provenance.db")
    try:
        event_row = prov_conn.execute(
            "SELECT pipeline_id, auth_context_id, occurred_at, input_source_ref FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if event_row is None:
            raise IncompleteEventBundleError(
                f"event_id={event_id!r} does not exist -- nothing to reconcile."
            )
        pipeline_id, auth_context_id, occurred_at, input_source_ref = event_row

        payload = _find_unambiguous_staging_entry(staging_path, event_id, input_source_ref)

        expected = {
            "staging_id": input_source_ref, "session_id": payload["session_id"],
            "session_started_at": payload["session_started_at"], "auth_context_id": auth_context_id,
            "pipeline_id": pipeline_id, "occurred_at": occurred_at,
            "bounded_clause": payload["bounded_clause"], "clark_prose": payload["clark_prose"],
            "artifact_pass_ran": payload["artifact_pass_ran"], "waking_model_tag": payload["waking_model_tag"],
            "operative_directive_request": payload.get("operative_directive_request", "none"),
            "operative_directive_text": payload.get("operative_directive_text", ""),
            "visibility_scope": payload.get("visibility_scope"),
            # The FULL staged contract (the same one record_native_waking_turn verified on commit):
            # without these, the strict verifier would default them to "none" and refuse to
            # reconcile any turn that carried a lookup, a Discord send, Caret attribution or a
            # reply choice.
            "delivered_boundary_query_event_id": payload.get("delivered_boundary_query_event_id"),
            "external_info_request": payload.get("external_info_request", "none"),
            "external_info_target": payload.get("external_info_target", ""),
            "delivered_external_info_query_event_id": payload.get("delivered_external_info_query_event_id"),
            "discord_correspondence_request": payload.get("discord_correspondence_request", "none"),
            "discord_destination_id": payload.get("discord_destination_id", ""),
            "discord_message_text": payload.get("discord_message_text", ""),
            "delivered_discord_inbound_event_ids": payload.get("delivered_discord_inbound_event_ids"),
            "caret_correspondent_principal_id": payload.get("caret_correspondent_principal_id"),
            "caret_reply_route_intent": bool(payload.get("caret_reply_route_intent", False)),
            "subject_reply_choice": payload.get("subject_reply_choice"),
            **{kind: payload.get(kind) for kind, _author in _CORRESPONDENCE_COMPONENTS},
        }
        if not verify_native_bundle_contract(prov_conn, event_id, expected=expected):
            raise IncompleteEventBundleError(
                f"event_id={event_id!r} is not a complete, valid canonical bundle -- "
                f"refusing to reconcile legacy projections from an incomplete/corrupt source."
            )

        reassembled_reply = _reassemble_reply_from_canonical(prov_conn, event_id)
        # The legacy stores' model_revision_id must come from the
        # VERIFIED waking-model (pass-2) participation row, identified
        # by its fixed participation_note -- never from
        # event_components.sequence=1, which legitimately does not
        # exist for a bounded-only turn (clark_prose == ""). The
        # pass-2 model call still happens and is still recorded in
        # event_model_participation even when its resulting text ends
        # up empty, so this is always present, unlike the component.
        # A LAWFUL NULL turn (Clark's typed no_reply) ran no pass-2 call; its waking-model row is the
        # pass-1 typed-choice participation, verified above against the staged contract.
        pass2_row = prov_conn.execute(
            "SELECT model_revision_id FROM event_model_participation "
            "WHERE event_id = ? AND participation_note = ?",
            (event_id, PASS1_TYPED_CHOICE_PARTICIPATION_NOTE
             if payload.get("subject_reply_choice") == REPLY_CHOICE_NO_REPLY else PASS2_PROSE_PARTICIPATION_NOTE),
        ).fetchone()
        if pass2_row is None:
            raise IncompleteEventBundleError(
                f"event_id={event_id!r}: no pass-2 (waking-model) event_model_participation row "
                f"found -- cannot determine the model_revision_id for legacy projection."
            )
        model_revision_id = pass2_row[0]
        model_participations = _fetch_model_participations(prov_conn, event_id)

        pipeline_key = payload["pipeline_key"]
        legacy_substrate_label = resolve_legacy_substrate_label(prov_conn, pipeline_key)
        occurred_at_iso = datetime.fromtimestamp(occurred_at, tz=timezone.utc).isoformat()

        report = {}

        # --- turn_generation_log: timestamp (moment-of-write) is the
        # only named exception. Everything else is deterministic from
        # canonical/staged state and must agree exactly. ---
        mind_conn = sqlite3.connect(mind_db_path)
        try:
            controls = payload["controls"]
            expected_tgl = {
                "user_id": payload["user_id"], "temperature": controls.get("temperature"),
                "top_p": controls.get("top_p"), "style_instruction": controls.get("style_instruction"),
                "model_revision_id": model_revision_id, "pipeline_id": pipeline_id,
                "epoch_id": None, "event_id": event_id,
            }
            existing_tgl_rows = [
                dict(zip(
                    ("user_id", "temperature", "top_p", "style_instruction", "model_revision_id",
                     "pipeline_id", "epoch_id", "event_id"),
                    row,
                ))
                for row in mind_conn.execute(
                    "SELECT user_id, temperature, top_p, style_instruction, model_revision_id, "
                    "pipeline_id, epoch_id, event_id FROM turn_generation_log WHERE event_id = ?",
                    (event_id,),
                ).fetchall()
            ]
            outcome = _check_or_repair_legacy_row(existing_tgl_rows, expected_tgl, set(), "turn_generation_log")
            if outcome == "repair":
                mind_conn.execute(
                    "INSERT INTO turn_generation_log (user_id, timestamp, temperature, top_p, "
                    "style_instruction, model_revision_id, pipeline_id, epoch_id, event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (payload["user_id"], int(time.time()), controls.get("temperature"), controls.get("top_p"),
                     controls.get("style_instruction"), model_revision_id, pipeline_id, event_id),
                )
                mind_conn.commit()
                report["turn_generation_log"] = "repaired"
            else:
                report["turn_generation_log"] = "already_present"
        finally:
            mind_conn.close()

        # --- anaxi_log.jsonl: fully deterministic from canonical/staged
        # state, including timestamp (derived from occurred_at, not a
        # fresh clock read) -- no fields are excluded from equality
        # for this store. ---
        expected_entry = {
            "timestamp": occurred_at_iso, "substrate": legacy_substrate_label,
            "model": payload["waking_model_tag"], "waking_model_tag": payload["waking_model_tag"],
            "prompt": payload["prompt"], "response": reassembled_reply, "kardia": payload["kardia"],
            "pipeline_id": pipeline_id, "pipeline_provenance_status": "known",
            "epoch_id": None, "event_id": event_id, "event_model_participation": model_participations,
        }
        existing_jsonl_rows = []
        if _os.path.isfile(jsonl_path):
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        parsed = _json.loads(line)
                        if parsed.get("event_id") == event_id:
                            existing_jsonl_rows.append(parsed)
        outcome = _check_or_repair_legacy_row(existing_jsonl_rows, expected_entry, set(), "anaxi_log.jsonl")
        if outcome == "repair":
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(expected_entry) + "\n")
            report["anaxi_log_jsonl"] = "repaired"
        else:
            report["anaxi_log_jsonl"] = "already_present"

        # --- relational_events: created_at (moment-of-write) is the
        # only named exception. ---
        rel_conn = sqlite3.connect(relational_db_path)
        try:
            expected_rel = {
                "user_id": payload["user_id"], "substrate": legacy_substrate_label,
                "agent_observation": None, "external_observation": payload["prompt"],
                "agent_response": reassembled_reply, "linked_memories": "[]", "linked_proposal_id": None,
                "status": "recorded", "model_revision_id": model_revision_id, "pipeline_id": pipeline_id,
                "epoch_id": None, "auth_context_id": auth_context_id, "event_id": event_id,
            }
            existing_rel_rows = [
                dict(zip(
                    ("user_id", "substrate", "agent_observation", "external_observation", "agent_response",
                     "linked_memories", "linked_proposal_id", "status", "model_revision_id", "pipeline_id",
                     "epoch_id", "auth_context_id", "event_id"),
                    row,
                ))
                for row in rel_conn.execute(
                    "SELECT user_id, substrate, agent_observation, external_observation, agent_response, "
                    "linked_memories, linked_proposal_id, status, model_revision_id, pipeline_id, "
                    "epoch_id, auth_context_id, event_id FROM relational_events WHERE event_id = ?",
                    (event_id,),
                ).fetchall()
            ]
            outcome = _check_or_repair_legacy_row(existing_rel_rows, expected_rel, set(), "relational_events")
            if outcome == "repair":
                rel_conn.execute(
                    "INSERT INTO relational_events (user_id, substrate, created_at, agent_observation, "
                    "external_observation, agent_response, linked_memories, linked_proposal_id, status, "
                    "model_revision_id, pipeline_id, epoch_id, auth_context_id, event_id) "
                    "VALUES (?, ?, ?, NULL, ?, ?, '[]', NULL, 'recorded', ?, ?, NULL, ?, ?)",
                    (payload["user_id"], legacy_substrate_label, int(time.time()), payload["prompt"],
                     reassembled_reply, model_revision_id, pipeline_id, auth_context_id, event_id),
                )
                rel_conn.commit()
                report["relational_events"] = "repaired"
            else:
                report["relational_events"] = "already_present"
        finally:
            rel_conn.close()

        return report
    finally:
        prov_conn.close()


def _resolve_or_create_session(conn: sqlite3.Connection, session_id: str, pipeline_id: str, session_started_at: int) -> None:
    """Runs INSIDE the caller's already-open transaction -- never its
    own separate commit. Whichever turn's transaction actually reaches
    this first performs the real INSERT; every other turn (including a
    resumed/retried one) verifies the existing row matches exactly,
    never trusting it blindly and never re-deriving started_at from
    whichever turn happens to run first."""
    row = conn.execute("SELECT pipeline_id, started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, ?, ?)",
            (session_id, pipeline_id, session_started_at),
        )
        return
    _verify_or_raise(
        {"pipeline_id": row[0], "started_at": row[1]},
        {"pipeline_id": pipeline_id, "started_at": session_started_at},
        f"sessions[{session_id!r}]",
    )


def _fetch_model_participations(conn: sqlite3.Connection, event_id: str) -> list:
    """Ordered by event_model_participation_id -- this feeds directly
    into serialized projections (anaxi_log.jsonl's
    event_model_participation list) and later content-equality
    comparisons (reconcile_native_turn()), neither of which can
    tolerate SQLite's unordered-by-default row return order silently
    differing between the original write and a later read."""
    rows = conn.execute(
        "SELECT mp.model_revision_id, mr.tag FROM event_model_participation mp "
        "JOIN model_revisions mr ON mr.model_revision_id = mp.model_revision_id "
        "WHERE mp.event_id = ? ORDER BY mp.event_model_participation_id",
        (event_id,),
    ).fetchall()
    return [{"model_revision_id": r[0], "tag": r[1]} for r in rows]


def verify_native_bundle_contract(conn: sqlite3.Connection, event_id: str, expected: dict = None) -> bool:
    """Native analog of migrate_historical_data.verify_historical_bundle_contract()
    -- same exact-field-and-cardinality philosophy, adapted for native
    events.

    When `expected` is given (the full staged/native turn contract:
    staging_id, session_id, session_started_at, auth_context_id,
    pipeline_id, occurred_at, bounded_clause, clark_prose,
    artifact_pass_ran, waking_model_tag), every field below is checked
    for EXACT agreement against it -- event-ID presence alone is never
    sufficient to classify an existing row as already-committed, which
    is exactly the case this function is used for on resume. When
    `expected` is omitted, a weaker shape-only check runs instead
    (existence, cardinality bounds, auth_state='unknown') -- callers
    that have the expected contract available (record_native_waking_turn's
    resume check, reconcile_native_turn()) MUST pass it; omission is
    only for genuinely context-free validity checks.

    Checked, when `expected` is given: event_type/pipeline_id/
    pipeline_provenance_status/epoch_id/auth_context_id/
    input_source_ref/occurred_at on events; session_id/pipeline_id/
    started_at on the bound session; auth_state/claimed_actor_id/
    authenticated_actor_id/auth_method/assurance_level/session_id/
    established_at on the auth_context; exact participation cardinality
    from artifact_pass_ran, exact participation_note text, and each
    model_revisions row's tag/identity_confidence; exact component
    cardinality from whether clark_prose is non-empty, exact sequence/
    creator_actor_id/component_kind/authorship_resolution/
    component_text/content_sha256/model_revision_id/span_start/
    span_end; and that no event_subjects/event_requesters/
    event_relied_upon_* row exists for this event (the accepted native
    write path never creates one for an unknown-auth synthetic waking
    turn).

    Returns False only if the event is entirely absent. Raises
    IncompleteEventBundleError for a structurally-impossible partial
    bundle. Raises MigrationStopCondition for any field/cardinality
    disagreement -- a hard stop, never repaired, never silently
    classified as a match."""
    events_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    auth_columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)").fetchall()}
    event_scope_expr = "visibility_scope" if "visibility_scope" in events_columns else "NULL"
    auth_scope_expr = "visibility_scope" if "visibility_scope" in auth_columns else "NULL"
    event_row = conn.execute(
        "SELECT event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        f"auth_context_id, input_source_ref, occurred_at, {event_scope_expr} "
        "FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event_row is None:
        return False
    (event_type, pipeline_id, pipeline_provenance_status, epoch_id,
     auth_context_id, input_source_ref, occurred_at, event_visibility_scope) = event_row

    migration_row = conn.execute(
        "SELECT 1 FROM event_migration_status WHERE event_id = ?", (event_id,)
    ).fetchone()
    routing_row = conn.execute(
        "SELECT 1 FROM legacy_routing_provenance WHERE event_id = ?", (event_id,)
    ).fetchone()
    if migration_row is not None or routing_row is not None:
        raise MigrationStopCondition(
            f"event_id={event_id!r} has a migration-only row (event_migration_status or "
            f"legacy_routing_provenance) -- this contradicts native origin."
        )

    if expected is not None:
        _verify_or_raise(
            {"event_type": event_type, "pipeline_id": pipeline_id,
             "pipeline_provenance_status": pipeline_provenance_status, "epoch_id": epoch_id,
             "auth_context_id": auth_context_id, "input_source_ref": input_source_ref,
             "occurred_at": occurred_at, "visibility_scope": event_visibility_scope},
            {"event_type": "waking_turn", "pipeline_id": expected["pipeline_id"],
             "pipeline_provenance_status": "known", "epoch_id": None,
             "auth_context_id": expected["auth_context_id"], "input_source_ref": expected["staging_id"],
             "occurred_at": expected["occurred_at"],
             "visibility_scope": expected.get("visibility_scope")},
            f"events[{event_id!r}]",
        )
    elif event_type != "waking_turn" or pipeline_provenance_status != "known" or epoch_id is not None:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: events row does not match the native shape "
            f"(event_type={event_type!r}, pipeline_provenance_status={pipeline_provenance_status!r}, "
            f"epoch_id={epoch_id!r})."
        )

    session_row = conn.execute(
        "SELECT ac.session_id, s.pipeline_id, s.started_at FROM auth_contexts ac "
        "JOIN sessions s ON s.session_id = ac.session_id WHERE ac.auth_context_id = ?",
        (auth_context_id,),
    ).fetchone()
    if session_row is None:
        raise IncompleteEventBundleError(
            f"event_id={event_id!r}: auth_context_id does not resolve to any session-bound auth_contexts row."
        )
    session_id, session_pipeline_id, session_started_at = session_row
    if expected is not None:
        _verify_or_raise(
            {"session_id": session_id, "pipeline_id": session_pipeline_id, "started_at": session_started_at},
            {"session_id": expected["session_id"], "pipeline_id": expected["pipeline_id"],
             "started_at": expected["session_started_at"]},
            f"sessions[{session_id!r}] (bound to event {event_id!r})",
        )

    auth_row = conn.execute(
        "SELECT auth_state, claimed_actor_id, authenticated_actor_id, auth_method, "
        f"assurance_level, established_at, {auth_scope_expr} "
        "FROM auth_contexts WHERE auth_context_id = ?",
        (auth_context_id,),
    ).fetchone()
    (auth_state, claimed_actor_id, authenticated_actor_id, auth_method,
     assurance_level, established_at, auth_visibility_scope) = auth_row
    _verify_or_raise(
        {"auth_state": auth_state, "claimed_actor_id": claimed_actor_id,
         "authenticated_actor_id": authenticated_actor_id, "auth_method": auth_method,
         "assurance_level": assurance_level},
        {"auth_state": "unknown", "claimed_actor_id": None, "authenticated_actor_id": None,
         "auth_method": None, "assurance_level": None},
        f"auth_contexts[{auth_context_id!r}]",
    )
    if auth_visibility_scope != event_visibility_scope:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: events.visibility_scope={event_visibility_scope!r} "
            f"disagrees with auth_contexts.visibility_scope={auth_visibility_scope!r}."
        )
    if expected is not None and auth_visibility_scope != expected.get("visibility_scope"):
        raise MigrationStopCondition(
            f"auth_contexts[{auth_context_id!r}].visibility_scope="
            f"{auth_visibility_scope!r}, expected {expected.get('visibility_scope')!r}."
        )
    if expected is not None and established_at != expected["occurred_at"]:
        raise MigrationStopCondition(
            f"auth_contexts[{auth_context_id!r}].established_at={established_at!r}, expected "
            f"{expected['occurred_at']!r} -- refusing to treat this as already-migrated."
        )
    sharing = conn.execute(
        "SELECT COUNT(*) FROM events WHERE auth_context_id = ?", (auth_context_id,)
    ).fetchone()[0]
    if sharing != 1:
        raise MigrationStopCondition(
            f"auth_context_id {auth_context_id!r} is referenced by {sharing} events -- "
            f"expected exactly 1 (every native turn must have its own auth_context)."
        )

    for table in _EVENT_RELIANCE_TABLES:
        if conn.execute(f"SELECT 1 FROM {table} WHERE event_id = ?", (event_id,)).fetchone() is not None:
            raise MigrationStopCondition(
                f"event_id={event_id!r} has a row in {table} -- the accepted native write path "
                f"never creates one for an unknown-auth synthetic waking turn."
            )

    participation_rows = conn.execute(
        "SELECT model_revision_id, participation_note FROM event_model_participation "
        "WHERE event_id = ? ORDER BY event_model_participation_id",
        (event_id,),
    ).fetchall()
    component_rows = conn.execute(
        "SELECT sequence, creator_actor_id, component_kind, authorship_resolution, component_text, "
        "content_sha256, model_revision_id, span_start, span_end FROM event_components "
        "WHERE event_id = ? ORDER BY sequence",
        (event_id,),
    ).fetchall()
    core_component_rows = [
        row for row in component_rows
        if row[2] in {"bounded_clause", "conversational_prose"}
    ]
    allowed_auxiliary_component_kinds = {
        "episode_context_delivered",
        "waking_interaction_mode",
        "active_workspace_event_delivered",
        "human_input_event_id",
        brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
        OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND,
        OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND,
        eir.EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND,
        eir.EXTERNAL_INFO_REQUEST_COMPONENT_KIND,
        eir.EXTERNAL_INFO_TARGET_COMPONENT_KIND,
        dcr.DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND,
        dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND,
        dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND,
        dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND,
        dcr.CARET_CORRESPONDENT_PRINCIPAL_COMPONENT_KIND,
        dcr.CARET_REPLY_ROUTE_INTENT_COMPONENT_KIND,
        CLARK_REPLY_CHOICE_COMPONENT_KIND,
        *(kind for kind, _author in _CORRESPONDENCE_COMPONENTS),
    }
    unexpected_component_kinds = {
        row[2] for row in component_rows
        if row[2] not in {"bounded_clause", "conversational_prose"}
        and row[2] not in allowed_auxiliary_component_kinds
    }
    if unexpected_component_kinds:
        raise MigrationStopCondition(
            f"event_id={event_id!r} has unexpected native component kind(s): "
            f"{sorted(unexpected_component_kinds)!r}."
        )

    if expected is not None:
        expected_request = expected.get("operative_directive_request", "none")
        expected_text = expected.get("operative_directive_text", "")
        request_rows = [row for row in component_rows if row[2] == OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND]
        text_rows = [row for row in component_rows if row[2] == OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND]
        expected_request_count = 0 if expected_request == "none" else 1
        expected_text_count = 1 if expected_request == "set_directive" else 0
        if len(request_rows) != expected_request_count or len(text_rows) != expected_text_count:
            raise MigrationStopCondition(
                f"event_id={event_id!r} has directive action component cardinality inconsistent with its staged contract."
            )
        clark_actor_id = derive_stable_id("actor", "clark")
        if request_rows:
            row = request_rows[0]
            expected_hash = hashlib.sha256(expected_request.encode("utf-8")).hexdigest()
            if row[1:] != (
                clark_actor_id, OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND, "resolved", expected_request,
                expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} directive request component disagrees with its staged contract."
                )
        if text_rows:
            row = text_rows[0]
            expected_hash = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
            if row[1:] != (
                clark_actor_id, OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND, "resolved", expected_text,
                expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} directive text component disagrees with its staged contract."
                )

    if expected is not None:
        expected_ei_request = expected.get("external_info_request", "none")
        expected_ei_target = expected.get("external_info_target", "")
        expected_ei_carriage = expected.get("delivered_external_info_query_event_id")
        ei_request_rows = [row for row in component_rows if row[2] == eir.EXTERNAL_INFO_REQUEST_COMPONENT_KIND]
        ei_target_rows = [row for row in component_rows if row[2] == eir.EXTERNAL_INFO_TARGET_COMPONENT_KIND]
        ei_carriage_rows = [row for row in component_rows if row[2] == eir.EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND]
        expected_ei_request_count = 0 if expected_ei_request == "none" else 1
        expected_ei_target_count = 0 if expected_ei_request == "none" else 1
        expected_ei_carriage_count = 0 if expected_ei_carriage is None else 1
        if (
            len(ei_request_rows) != expected_ei_request_count
            or len(ei_target_rows) != expected_ei_target_count
            or len(ei_carriage_rows) != expected_ei_carriage_count
        ):
            raise MigrationStopCondition(
                f"event_id={event_id!r} has external-info component cardinality inconsistent with its staged contract."
            )
        clark_actor_id = derive_stable_id("actor", "clark")
        if ei_request_rows:
            row = ei_request_rows[0]
            expected_hash = hashlib.sha256(expected_ei_request.encode("utf-8")).hexdigest()
            if row[1:] != (
                clark_actor_id, eir.EXTERNAL_INFO_REQUEST_COMPONENT_KIND, "resolved", expected_ei_request,
                expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} external-info request component disagrees with its staged contract."
                )
        if ei_target_rows:
            row = ei_target_rows[0]
            expected_hash = hashlib.sha256(expected_ei_target.encode("utf-8")).hexdigest()
            if row[1:] != (
                clark_actor_id, eir.EXTERNAL_INFO_TARGET_COMPONENT_KIND, "resolved", expected_ei_target,
                expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} external-info target component disagrees with its staged contract."
                )
        if ei_carriage_rows:
            row = ei_carriage_rows[0]
            expected_hash = hashlib.sha256(expected_ei_carriage.encode("utf-8")).hexdigest()
            if row[1:] != (
                derive_stable_id("actor", "bounded_clause_renderer"),
                eir.EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND, "resolved",
                expected_ei_carriage, expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} external-info carriage component disagrees with its staged contract."
                )

    if expected is not None:
        expected_dc_request = expected.get("discord_correspondence_request", "none")
        expected_dc_destination = expected.get("discord_destination_id", "")
        expected_dc_text = expected.get("discord_message_text", "")
        expected_dc_carriages = expected.get("delivered_discord_inbound_event_ids") or []
        clark_actor_id = derive_stable_id("actor", "clark")
        host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
        dc_request_rows = [row for row in component_rows if row[2] == dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND]
        dc_destination_rows = [row for row in component_rows if row[2] == dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND]
        dc_text_rows = [row for row in component_rows if row[2] == dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND]
        dc_carriage_rows = [row for row in component_rows if row[2] == dcr.DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND]
        expected_dc_request_count = 0 if expected_dc_request == "none" else 1
        expected_dc_destination_count = 0 if expected_dc_request == "none" else 1
        expected_dc_text_count = 0 if expected_dc_request == "none" else 1
        if (
            len(dc_request_rows) != expected_dc_request_count
            or len(dc_destination_rows) != expected_dc_destination_count
            or len(dc_text_rows) != expected_dc_text_count
            or len(dc_carriage_rows) != len(expected_dc_carriages)
        ):
            raise MigrationStopCondition(
                f"event_id={event_id!r} has discord-correspondence component cardinality inconsistent "
                f"with its staged contract."
            )
        for rows, value, kind in (
            (dc_request_rows, expected_dc_request, dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND),
            (dc_destination_rows, expected_dc_destination, dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND),
            (dc_text_rows, expected_dc_text, dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND),
        ):
            if rows:
                row = rows[0]
                expected_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
                if row[1:] != (clark_actor_id, kind, "resolved", value, expected_hash, None, None, None):
                    raise MigrationStopCondition(
                        f"event_id={event_id!r} discord-correspondence {kind!r} component disagrees "
                        f"with its staged contract."
                    )
        delivered_set = {row[4] for row in dc_carriage_rows}
        if delivered_set != set(expected_dc_carriages):
            raise MigrationStopCondition(
                f"event_id={event_id!r} discord inbound carriage components disagree with the staged contract."
            )
        for row in dc_carriage_rows:
            expected_hash = hashlib.sha256(row[4].encode("utf-8")).hexdigest()
            if row[1:] != (
                host_actor_id, dcr.DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND, "resolved",
                row[4], expected_hash, None, None, None,
            ):
                raise MigrationStopCondition(
                    f"event_id={event_id!r} discord inbound carriage component disagrees with its shape contract."
                )

    if expected is not None:
        expected_principal = expected.get("caret_correspondent_principal_id")
        principal_rows = [r for r in component_rows if r[2] == dcr.CARET_CORRESPONDENT_PRINCIPAL_COMPONENT_KIND]
        intent_rows = [r for r in component_rows if r[2] == dcr.CARET_REPLY_ROUTE_INTENT_COMPONENT_KIND]
        if (
            len(principal_rows) != (0 if expected_principal is None else 1)
            or (principal_rows and principal_rows[0][4] != expected_principal)
            or len(intent_rows) != (1 if expected.get("caret_reply_route_intent") else 0)
        ):
            raise MigrationStopCondition(
                f"event_id={event_id!r} Caret correspondent/route-intent components disagree with "
                "the staged contract."
            )
        for kind, author in _CORRESPONDENCE_COMPONENTS:
            want = expected.get(kind)
            rows_k = [r for r in component_rows if r[2] == kind]
            creator = derive_stable_id("actor", "bounded_clause_renderer") if author == "host" else derive_stable_id("actor", "clark")
            if (want is None and rows_k) or (want is not None and (len(rows_k) != 1 or rows_k[0][1:] != (
                    creator, kind, "resolved", want, hashlib.sha256(want.encode("utf-8")).hexdigest(), None, None, None))):
                raise MigrationStopCondition(f"event_id={event_id!r} {kind} component disagrees with the staged contract.")
        expected_choice = expected.get("subject_reply_choice")
        choice_rows = [r for r in component_rows if r[2] == CLARK_REPLY_CHOICE_COMPONENT_KIND]
        if expected_choice is None:
            if choice_rows:
                raise MigrationStopCondition(
                    f"event_id={event_id!r} carries a reply-choice component its staged contract never made.")
        elif len(choice_rows) != 1 or choice_rows[0][1:] != (
                derive_stable_id("actor", "clark"), CLARK_REPLY_CHOICE_COMPONENT_KIND, "resolved", expected_choice,
                hashlib.sha256(expected_choice.encode("utf-8")).hexdigest(), None, None, None):
            raise MigrationStopCondition(
                f"event_id={event_id!r} reply-choice component disagrees with the staged contract.")

    # event_model_participation is never legitimately empty -- the pass-2
    # model call always happens regardless of operation_status. event_components
    # IS legitimately empty now: bounded_clause and clark_prose are each
    # independently optional (see record_native_waking_turn()), so a silent
    # not_authorized turn whose prose also happened to end up empty would
    # have zero components -- a real, valid state, not a partial bundle.
    if len(participation_rows) == 0:
        raise IncompleteEventBundleError(
            f"event_id={event_id!r} has a PARTIAL native bundle: zero event_model_participation rows."
        )

    if expected is not None:
        expected_participation_count = 2 if expected["artifact_pass_ran"] else 1
        expected_component_count = (
            (1 if expected["bounded_clause"] else 0) + (1 if expected["clark_prose"] else 0)
        )
    else:
        expected_participation_count = None
        expected_component_count = None

    if expected_participation_count is not None:
        if len(participation_rows) != expected_participation_count:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: expected exactly {expected_participation_count} "
                f"event_model_participation row(s) given artifact_pass_ran="
                f"{expected['artifact_pass_ran']!r}, found {len(participation_rows)}."
            )
    elif len(participation_rows) not in (1, 2):
        raise MigrationStopCondition(
            f"event_id={event_id!r}: expected 1 or 2 event_model_participation rows, found {len(participation_rows)}."
        )

    if expected_component_count is not None:
        if len(core_component_rows) != expected_component_count:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: expected exactly {expected_component_count} "
                f"core event_components row(s) given bounded_clause "
                f"{'non-empty' if expected['bounded_clause'] else 'empty'} and clark_prose "
                f"{'non-empty' if expected['clark_prose'] else 'empty'}, found "
                f"{len(core_component_rows)}."
            )
    elif len(core_component_rows) not in (0, 1, 2):
        raise MigrationStopCondition(
            f"event_id={event_id!r}: expected 0, 1, or 2 core event_components rows, "
            f"found {len(core_component_rows)}."
        )

    for model_revision_id, participation_note in participation_rows:
        mr_row = conn.execute(
            "SELECT tag, identity_confidence FROM model_revisions WHERE model_revision_id = ?",
            (model_revision_id,),
        ).fetchone()
        if mr_row is None:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: event_model_participation references model_revision_id="
                f"{model_revision_id!r}, but no such row exists in model_revisions."
            )
        if expected is not None:
            # The ID itself must equal the deterministic value derived
            # from the expected tag -- checking only that the REFERENCED
            # row happens to carry the expected tag/confidence is not
            # sufficient: a decoy row under a different, non-deterministic
            # model_revision_id could coincidentally (or deliberately)
            # carry the same tag/confidence and would pass a tag-only
            # check while still being the wrong row.
            expected_model_revision_id = _expected_model_revision_id(expected["waking_model_tag"])
            if model_revision_id != expected_model_revision_id:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: event_model_participation.model_revision_id="
                    f"{model_revision_id!r} does not equal the deterministic expected ID "
                    f"{expected_model_revision_id!r} -- refusing to trust it even though the "
                    f"referenced row's tag/identity_confidence match."
                )
            _verify_or_raise(
                {"tag": mr_row[0], "identity_confidence": mr_row[1]},
                {"tag": expected["waking_model_tag"], "identity_confidence": "tag_only_degraded"},
                f"model_revisions[{model_revision_id!r}]",
            )

    if expected is not None:
        expected_notes = (
            [PASS1_ARTIFACT_PARTICIPATION_NOTE] if expected["artifact_pass_ran"] else []
        ) + [PASS1_TYPED_CHOICE_PARTICIPATION_NOTE if expected.get("subject_reply_choice") == REPLY_CHOICE_NO_REPLY
             else PASS2_PROSE_PARTICIPATION_NOTE]
        actual_notes = [note for _mrid, note in participation_rows]
        if actual_notes != expected_notes:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: event_model_participation.participation_note sequence "
                f"{actual_notes!r} != expected {expected_notes!r}."
            )

        host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
        clark_actor_id = derive_stable_id("actor", "clark")
        expected_prose_model_revision_id = _expected_model_revision_id(expected["waking_model_tag"])

        # Sequence-keyed lookup, not positional (component_rows[0]) -- since
        # sequence 0 (bounded_clause) can now be legitimately absent while
        # sequence 1 (conversational_prose) is present, the first row
        # returned is not necessarily sequence 0 anymore.
        by_sequence = {row[0]: row for row in component_rows}

        if expected["bounded_clause"]:
            seq0 = by_sequence.get(0)
            if seq0 is None:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: bounded_clause was expected to be non-empty but no "
                    f"sequence=0 event_components row exists."
                )
            expected_bc_hash = hashlib.sha256(expected["bounded_clause"].encode("utf-8")).hexdigest()
            _verify_or_raise(
                {"sequence": seq0[0], "creator_actor_id": seq0[1], "component_kind": seq0[2],
                 "authorship_resolution": seq0[3], "component_text": seq0[4], "content_sha256": seq0[5],
                 "model_revision_id": seq0[6], "span_start": seq0[7], "span_end": seq0[8]},
                {"sequence": 0, "creator_actor_id": host_actor_id, "component_kind": "bounded_clause",
                 "authorship_resolution": "resolved", "component_text": expected["bounded_clause"],
                 "content_sha256": expected_bc_hash, "model_revision_id": None,
                 "span_start": 0, "span_end": len(expected["bounded_clause"])},
                f"event_components[{event_id!r}, sequence=0]",
            )
        elif 0 in by_sequence:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: bounded_clause was expected to be empty (silent) but a "
                f"sequence=0 event_components row exists -- a clause was recorded that was never shown."
            )

        if expected["clark_prose"]:
            span_start = (len(expected["bounded_clause"]) + 1) if expected["bounded_clause"] else 0
            seq1 = by_sequence.get(1)
            if seq1 is None:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: clark_prose was expected to be non-empty but no "
                    f"sequence=1 event_components row exists."
                )
            expected_cp_hash = hashlib.sha256(expected["clark_prose"].encode("utf-8")).hexdigest()
            _verify_or_raise(
                {"sequence": seq1[0], "creator_actor_id": seq1[1], "component_kind": seq1[2],
                 "authorship_resolution": seq1[3], "component_text": seq1[4], "content_sha256": seq1[5],
                 "model_revision_id": seq1[6], "span_start": seq1[7], "span_end": seq1[8]},
                {"sequence": 1, "creator_actor_id": clark_actor_id, "component_kind": "conversational_prose",
                 "authorship_resolution": "resolved", "component_text": expected["clark_prose"],
                 "content_sha256": expected_cp_hash, "model_revision_id": expected_prose_model_revision_id,
                 "span_start": span_start, "span_end": span_start + len(expected["clark_prose"])},
                f"event_components[{event_id!r}, sequence=1]",
            )
        elif 1 in by_sequence:
            raise MigrationStopCondition(
                f"event_id={event_id!r}: clark_prose was expected to be empty but a sequence=1 "
                f"event_components row exists -- prose was recorded that was never actually generated."
            )

        expected_boundary_query_id = expected.get("delivered_boundary_query_event_id")
        carriage_rows = [
            row for row in component_rows
            if row[2] == brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND
        ]
        if expected_boundary_query_id is None:
            if carriage_rows:
                raise MigrationStopCondition(
                    f"event_id={event_id!r} unexpectedly carries a Boundary Inspector result."
                )
        else:
            if len(carriage_rows) != 1:
                raise MigrationStopCondition(
                    f"event_id={event_id!r} expected exactly one Boundary Inspector carriage "
                    f"component, found {len(carriage_rows)}."
                )
            carriage = carriage_rows[0]
            expected_hash = hashlib.sha256(expected_boundary_query_id.encode("utf-8")).hexdigest()
            _verify_or_raise(
                {"creator_actor_id": carriage[1], "component_kind": carriage[2],
                 "authorship_resolution": carriage[3], "component_text": carriage[4],
                 "content_sha256": carriage[5], "model_revision_id": carriage[6],
                 "span_start": carriage[7], "span_end": carriage[8]},
                {"creator_actor_id": host_actor_id,
                 "component_kind": brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
                 "authorship_resolution": "resolved", "component_text": expected_boundary_query_id,
                 "content_sha256": expected_hash, "model_revision_id": None,
                 "span_start": None, "span_end": None},
                f"event_components[{event_id!r}, boundary carriage]",
            )
    else:
        for model_revision_id in {r[6] for r in component_rows if r[6] is not None}:
            if conn.execute(
                "SELECT 1 FROM model_revisions WHERE model_revision_id = ?", (model_revision_id,)
            ).fetchone() is None:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: event_components references model_revision_id="
                    f"{model_revision_id!r}, but no such row exists in model_revisions."
                )

    return True
