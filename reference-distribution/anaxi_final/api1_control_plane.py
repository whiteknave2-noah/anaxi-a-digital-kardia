"""API1-S1: disabled-by-default authoritative protected-decision control
plane for anaxi_provenance.db.

AUTHORITATIVE_PDE_ENABLED defaults to False. With it False, calling
submit_authoritative_create_and_assign() always returns FEATURE_DISABLED
immediately -- no AAB revalidation, no DB read beyond that constant, no
side effect of any kind. This module is not imported by
llama_anaxi.py/orchestration.py/run_waking_turn() and performs zero
Ollama calls anywhere in its own code.

Only create_and_assign is supported. transfer_existing is intentionally
absent from PRODUCTION_ALLOWED_OPERATIONS -- see spec section 18/39.

IMPORTANT DISCOVERY, documented here rather than worked around (see the
API1-S1 final report for full detail): production `actor_human_person`
/ `persons` are currently EMPTY -- no human actor has ever been
registered in production's identity schema. AAB1's scratch-generated
human actor ID is therefore NOT yet a registered production actor, and
`auth_contexts`'s own `trg_auth_context_human_only` trigger would
reject any snapshot insert referencing it. This module does not
attempt to auto-register a person/actor; it fails closed with
HUMAN_ACTOR_NOT_REGISTERED and leaves that decision to a human
operator. A real authoritative TX1 cannot succeed against the current
live production database until that registration exists.
"""
import enum
import hashlib
import json
import sqlite3
import time
import uuid

AUTHORITATIVE_PDE_ENABLED = False

PRODUCTION_ALLOWED_OPERATIONS = {"create_and_assign"}

# Production auth_contexts.assurance_level is CHECK-constrained to
# ('none','low','medium','high') -- AAB1's native assurance value is
# not in that domain and the constraint cannot be safely altered
# in-place. This is a deliberate, documented, coarse mapping; the exact
# native string is preserved losslessly in the additive
# source_assurance_detail column (see api1_schema_migration.py).
ASSURANCE_LEVEL_MAP = {
    "os_principal_session_bound": "high",
}

MAX_DECISION_TEXT_CHARS = 4000


class ApiFailure(str, enum.Enum):
    FEATURE_DISABLED = "FEATURE_DISABLED"
    AUTH_NOT_ESTABLISHED = "AUTH_NOT_ESTABLISHED"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    ACTOR_BINDING_MISMATCH = "ACTOR_BINDING_MISMATCH"
    INVALID_OPERATION = "INVALID_OPERATION"
    INVALID_RECIPIENT = "INVALID_RECIPIENT"
    INVALID_DECISION_TEXT = "INVALID_DECISION_TEXT"
    REQUEST_CONFLICT = "REQUEST_CONFLICT"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    HUMAN_ACTOR_NOT_REGISTERED = "HUMAN_ACTOR_NOT_REGISTERED"
    PRODUCTION_SESSION_NOT_FOUND = "PRODUCTION_SESSION_NOT_FOUND"


CREATE_REQUIRED_FIELDS = {
    "establishment_request_id", "operation", "decision_text",
    "recipient_actor_id", "source_input_id",
}
CREATE_OPTIONAL_FIELDS = {"decision_context", "accompanying_message"}
CREATE_ALLOWED_FIELDS = CREATE_REQUIRED_FIELDS | CREATE_OPTIONAL_FIELDS
HOST_ONLY_FORBIDDEN_FIELDS = {
    "assignor_actor_id", "authenticated_actor_id", "auth_context_id",
    "session_id", "auth_state", "assurance_level", "owner_state",
    "decision_id", "created_event_id", "last_transition_event_id",
    "expected_last_transition_event_id",
}


def _validate_raw_shape(raw_request):
    if not isinstance(raw_request, dict):
        return ApiFailure.INVALID_OPERATION
    if raw_request.get("operation") not in PRODUCTION_ALLOWED_OPERATIONS:
        # Covers both "not create_and_assign" AND the explicit
        # transfer_existing-not-yet-productionized case (spec 18/39) --
        # transfer_existing is simply never a member of
        # PRODUCTION_ALLOWED_OPERATIONS, so it falls through here.
        return ApiFailure.INVALID_OPERATION

    present = set(raw_request.keys())
    if present & HOST_ONLY_FORBIDDEN_FIELDS:
        return ApiFailure.PROTOCOL_LEAKAGE
    if present - CREATE_ALLOWED_FIELDS:
        return ApiFailure.PROTOCOL_LEAKAGE
    if not CREATE_REQUIRED_FIELDS.issubset(present):
        return ApiFailure.PROTOCOL_LEAKAGE

    if not isinstance(raw_request.get("establishment_request_id"), str) or not raw_request["establishment_request_id"]:
        return ApiFailure.PROTOCOL_LEAKAGE
    if not isinstance(raw_request.get("source_input_id"), str) or not raw_request["source_input_id"]:
        return ApiFailure.PROTOCOL_LEAKAGE
    if not isinstance(raw_request.get("recipient_actor_id"), str) or not raw_request["recipient_actor_id"]:
        return ApiFailure.INVALID_RECIPIENT

    text = raw_request.get("decision_text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DECISION_TEXT_CHARS:
        return ApiFailure.INVALID_DECISION_TEXT

    context = raw_request.get("decision_context")
    if context is not None and not isinstance(context, str):
        return ApiFailure.PROTOCOL_LEAKAGE
    accompanying = raw_request.get("accompanying_message")
    if accompanying is not None and not isinstance(accompanying, str):
        return ApiFailure.PROTOCOL_LEAKAGE

    return None


def _payload_hash(raw_request):
    normalized = {
        "establishment_request_id": raw_request.get("establishment_request_id"),
        "operation": raw_request.get("operation"),
        "decision_text": raw_request.get("decision_text"),
        "decision_context": raw_request.get("decision_context"),
        "recipient_actor_id": raw_request.get("recipient_actor_id"),
        "source_input_id": raw_request.get("source_input_id"),
        "accompanying_message": raw_request.get("accompanying_message"),
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()


def _revalidate_aab(aab_store, auth_context_id, session_id):
    """Re-fetches CANONICAL stored AAB rows -- never trusts caller-
    supplied objects. Mirrors aab1.routing.owner_bound_route_eligible's
    same defensive re-fetch pattern, reimplemented here (not imported)
    because this production module only needs the read, not AAB's
    write-side authentication logic, which stays in aab1.auth."""
    stored_ac = aab_store.get_auth_context(auth_context_id)
    stored_session = aab_store.get_session(session_id)

    if stored_ac is None or stored_session is None:
        return None, ApiFailure.AUTH_NOT_ESTABLISHED
    if stored_ac["auth_state"] != "authenticated":
        return None, ApiFailure.AUTH_NOT_ESTABLISHED
    if stored_ac["assurance_level"] != "os_principal_session_bound":
        return None, ApiFailure.AUTH_NOT_ESTABLISHED
    if stored_ac["authenticated_actor_id"] is None:
        return None, ApiFailure.AUTH_NOT_ESTABLISHED
    if stored_session["status"] != "active":
        return None, ApiFailure.SESSION_NOT_ACTIVE
    if stored_session["auth_context_id"] != stored_ac["auth_context_id"]:
        return None, ApiFailure.ACTOR_BINDING_MISMATCH
    if stored_session["authenticated_actor_id"] != stored_ac["authenticated_actor_id"]:
        return None, ApiFailure.ACTOR_BINDING_MISMATCH

    return stored_ac, None


def _human_actor_registered(prov_conn, actor_id):
    row = prov_conn.execute(
        "SELECT 1 FROM actor_human_person WHERE actor_id = ?", (actor_id,)
    ).fetchone()
    return row is not None


def _iso_to_unix(iso_str):
    import datetime
    return int(datetime.datetime.fromisoformat(iso_str).timestamp())


def submit_authoritative_create_and_assign(
    prov_conn, aab_store, raw_request, auth_context_id, session_id,
    clark_actor_id, production_session_id, enabled=None,
    _test_inject_failure_after=None,
):
    """The one production entry point implemented in API1-S1.

    prov_conn: an open sqlite3 connection to anaxi_provenance.db (or a
        test copy of it) with PRAGMA foreign_keys=ON.
    aab_store: an aab1.store.AAB1Store instance -- the live AAB
        authority. Never reimplemented here.
    raw_request: caller-visible RawEstablishmentRequest dict. Never
        accepted as authoritative for any host-only field.
    auth_context_id/session_id: which AAB session is submitting this --
        NOT part of raw_request, supplied by the control surface acting
        on behalf of an already-authenticated AAB session.
    production_session_id: an existing anaxi_provenance.db `sessions`
        row this snapshot's auth_contexts.session_id will reference
        (production auth_contexts.session_id is NOT NULL with a FK to
        sessions -- this module does not create sessions rows, that
        remains the caller's/orchestrator's responsibility).
    enabled: test-only override of the module-level feature gate.
        Production callers should omit this and rely on
        AUTHORITATIVE_PDE_ENABLED.

    Returns (result_dict, None) on success or (None, ApiFailure) on
    failure. On failure, nothing is written -- not even a diagnostic
    row -- except where the failure IS the recorded idempotent state of
    a prior REQUEST_CONFLICT (which itself writes nothing new either).
    """
    if enabled is None:
        enabled = AUTHORITATIVE_PDE_ENABLED
    if not enabled:
        return None, ApiFailure.FEATURE_DISABLED

    shape_failure = _validate_raw_shape(raw_request)
    if shape_failure is not None:
        return None, shape_failure

    request_id = raw_request["establishment_request_id"]
    payload_hash = _payload_hash(raw_request)

    existing = prov_conn.execute(
        "SELECT * FROM protected_decision_requests WHERE establishment_request_id = ?",
        (request_id,),
    ).fetchone()
    if existing is not None:
        # Adjudication (spec section 29): an exact committed replay is
        # retrieval of existing canonical truth, not new authority --
        # it does NOT require the original AAB session to still be
        # active. A conflicting payload under the same ID always fails,
        # regardless of current auth state.
        if existing["status"] == "committed":
            if existing["payload_hash"] == payload_hash:
                return json.loads(existing["result_json"]), None
            return None, ApiFailure.REQUEST_CONFLICT
        return None, ApiFailure.DUPLICATE_REQUEST

    stored_ac, auth_failure = _revalidate_aab(aab_store, auth_context_id, session_id)
    if auth_failure is not None:
        return None, auth_failure

    authenticated_actor_id = stored_ac["authenticated_actor_id"]

    if raw_request["recipient_actor_id"] != clark_actor_id:
        return None, ApiFailure.INVALID_RECIPIENT

    if not _human_actor_registered(prov_conn, authenticated_actor_id):
        return None, ApiFailure.HUMAN_ACTOR_NOT_REGISTERED

    session_row = prov_conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = ?", (production_session_id,)
    ).fetchone()
    if session_row is None:
        return None, ApiFailure.PRODUCTION_SESSION_NOT_FOUND

    return _commit_tx1(
        prov_conn, raw_request, request_id, payload_hash, stored_ac,
        authenticated_actor_id, clark_actor_id, production_session_id,
        _test_inject_failure_after,
    )


def _commit_tx1(prov_conn, raw_request, request_id, payload_hash, stored_ac, authenticated_actor_id, clark_actor_id, production_session_id, _test_inject_failure_after=None):
    now = int(time.time())
    prod_auth_context_id = f"authctx-{uuid.uuid4().hex[:12]}"
    decision_id = f"dec-{uuid.uuid4().hex[:12]}"
    event_id = f"pde-event-{uuid.uuid4().hex[:12]}"

    mapped_assurance = ASSURANCE_LEVEL_MAP[stored_ac["assurance_level"]]
    established_at = _iso_to_unix(stored_ac["established_at"])

    decision = {
        "decision_id": decision_id,
        "decision_text": raw_request["decision_text"],
        "decision_context": raw_request.get("decision_context"),
        "owner_state": {"status": "resolved", "actor_id": clark_actor_id},
        "decision_state": "open",
        "resolution_kind": None,
        "created_event_id": event_id,
        "last_transition_event_id": event_id,
    }
    event = {
        "event_id": event_id,
        "event_type": "protected_decision_created",
        "decision_id": decision_id,
        "actor_id": authenticated_actor_id,
        "auth_context_id": prod_auth_context_id,
        "session_id": production_session_id,
        "establishment_request_id": request_id,
        "previous_owner_actor_id": None,
        "resulting_owner_actor_id": clark_actor_id,
        "occurred_at": now,
    }
    result = {"decision": decision, "event": event, "auth_context_id": prod_auth_context_id}

    with prov_conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        prov_conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, "
            "authenticated_actor_id, auth_state, auth_method, assurance_level, "
            "established_at, source_aab_auth_context_id, source_aab_session_id, "
            "observed_valid_at, source_assurance_detail) "
            "VALUES (?, ?, ?, ?, 'authenticated', ?, ?, ?, ?, ?, ?, ?)",
            (
                prod_auth_context_id, production_session_id, authenticated_actor_id,
                authenticated_actor_id, stored_ac["authentication_method"], mapped_assurance,
                established_at, stored_ac["auth_context_id"], stored_ac["session_id"],
                now, stored_ac["assurance_level"],
            ),
        )
        if _test_inject_failure_after == "auth_snapshot_insert":
            raise RuntimeError("injected test failure: auth_snapshot_insert")
        prov_conn.execute(
            "INSERT INTO protected_decisions (decision_id, decision_text, decision_context, "
            "owner_actor_id, decision_state, resolution_kind, counterpart_actor_id, "
            "created_event_id, last_transition_event_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', NULL, ?, ?, ?, ?, ?)",
            (
                decision_id, decision["decision_text"], decision["decision_context"],
                clark_actor_id, authenticated_actor_id, event_id, event_id, now, now,
            ),
        )
        if _test_inject_failure_after == "decision_insert":
            raise RuntimeError("injected test failure: decision_insert")
        prov_conn.execute(
            "INSERT INTO protected_decision_events (event_id, event_type, decision_id, "
            "actor_id, auth_context_id, session_id, establishment_request_id, "
            "previous_owner_actor_id, resulting_owner_actor_id, prior_transition_event_id, "
            "related_waking_event_id, occurred_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                event_id, "protected_decision_created", decision_id, authenticated_actor_id,
                prod_auth_context_id, production_session_id, request_id, None, clark_actor_id, now,
                json.dumps({}),
            ),
        )
        prov_conn.execute(
            "INSERT INTO protected_decision_requests (establishment_request_id, operation, "
            "payload_hash, decision_id, source_input_id, auth_context_id, status, "
            "result_json, created_at) VALUES (?, 'create_and_assign', ?, ?, ?, ?, 'committed', ?, ?)",
            (
                request_id, payload_hash, decision_id, raw_request["source_input_id"],
                prod_auth_context_id, json.dumps(result), now,
            ),
        )

    return result, None


def build_protected_decision_route(prov_conn, decision_id, clark_actor_id, prior_action_id=None):
    """Pure read + transform. Converts committed canonical state into
    the frozen ProtectedDecisionRoute shape. Does not invoke HDI2. Does
    not select or stage any action."""
    row = prov_conn.execute(
        "SELECT * FROM protected_decisions WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    if row is None:
        return None

    route = {
        "decision_id": row["decision_id"],
        "decision_text": row["decision_text"],
        "decision_context": row["decision_context"],
        "owner_actor_id": row["owner_actor_id"],
        "decision_state": row["decision_state"],
        "resolution_kind": row["resolution_kind"],
        "recipient_actor_id": clark_actor_id,
        # host-only, not model-visible if this route is later rendered
        # into an HDI2 task/message:
        "counterpart_actor_id": row["counterpart_actor_id"],
        "auth_context_id": None,  # filled by caller if/when needed; not stored redundantly here
        "source_input_id": None,
        "canonical_event_id": row["last_transition_event_id"],
        "prior_action_id": prior_action_id,
    }
    return route


def to_hdi2_protected_decision(route):
    """Lossless conversion into HDI2's frozen ProtectedDecision wire
    shape. No model call. No action selected."""
    return {
        "decision_id": route["decision_id"],
        "decision_text": route["decision_text"],
        "decision_context": route["decision_context"],
        "owner_state": {"status": "resolved", "actor_id": route["owner_actor_id"]},
        "decision_state": route["decision_state"],
        "resolution_kind": route["resolution_kind"],
        "created_event_id": route["canonical_event_id"],
        "last_transition_event_id": route["canonical_event_id"],
    }
