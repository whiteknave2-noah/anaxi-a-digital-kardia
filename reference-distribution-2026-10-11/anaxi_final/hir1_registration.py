"""HIR1-S1: canonical human identity registration mechanics.

NOT wired into any automatic path -- not AAB authentication, not PDE
submission, not ordinary waking, not memory, not the GUI. The only
caller in this codebase is test_hir1_registration.py, exercised against
production-schema COPIES. No live invocation happens in HIR1-S1; the
actual first live registration is a separately authorized HIR1-S2 gate.

Identity semantics (frozen, restated here for the code that implements
them):
    REGISTRATION = this canonical system knows of this person/actor.
    AUTHENTICATION = this current session may act as that actor.
    PERSON IDENTITY != RELATIONSHIP ROLE != PERMISSIONS.

Bootstrap authority: the FIRST registration of a human actor is a
local_operator_provisioning administrative act, not something the new
human actor authenticated and did to themselves -- that would be
circular. The registration event's `requester` (via event_requesters)
is the existing production host_system actor
(derive_stable_id("actor","bounded_clause_renderer"), already present
in production from the existing waking-turn write path); the new human
actor is recorded only as the event's `subject` (via event_subjects),
never its author.

Human actor type: discovered by read-only inspection of the live
schema in API1-S1 and re-verified here -- actors.actor_type is
CHECK-constrained to ('human_person','clark_agent','host_system').
HUMAN_ACTOR_TYPE below is exactly that discovered value, not a guess.
"""
import enum
import hashlib
import json
import sqlite3
import sys
import os
import time
import uuid

_ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if _ANAXI_FINAL not in sys.path:
    sys.path.insert(0, _ANAXI_FINAL)

from provenance_schema import derive_stable_id  # noqa: E402  (pure function, existing production mechanism)

HUMAN_ACTOR_TYPE = "human_person"
ALLOWED_SOURCES = {"local_operator_provisioning"}
HOST_ACTOR_ID = derive_stable_id("actor", "bounded_clause_renderer")  # existing production host_system actor
REGISTRATION_EVENT_TYPE = "human_actor_registered"


class HirFailure(str, enum.Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_SOURCE = "INVALID_SOURCE"
    REGISTRATION_REQUEST_CONFLICT = "REGISTRATION_REQUEST_CONFLICT"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    ACTOR_REGISTRATION_CONFLICT = "ACTOR_REGISTRATION_CONFLICT"
    PERSON_LINK_CONFLICT = "PERSON_LINK_CONFLICT"


REQUIRED_FIELDS = {"registration_request_id", "aab_actor_id", "display_label", "source"}
ALLOWED_FIELDS = REQUIRED_FIELDS
HOST_ONLY_FORBIDDEN_FIELDS = {
    "person_id", "actor_type", "canonical_event_id", "sid", "password",
    "session_id", "auth_context_id", "relationship_role", "permissions",
}


def _validate_shape(raw_request):
    if not isinstance(raw_request, dict):
        return HirFailure.INVALID_REQUEST
    present = set(raw_request.keys())
    if present & HOST_ONLY_FORBIDDEN_FIELDS:
        return HirFailure.INVALID_REQUEST
    if present - ALLOWED_FIELDS:
        return HirFailure.INVALID_REQUEST
    if not REQUIRED_FIELDS.issubset(present):
        return HirFailure.INVALID_REQUEST
    for field in ("registration_request_id", "aab_actor_id", "display_label", "source"):
        if not isinstance(raw_request.get(field), str) or not raw_request[field].strip():
            return HirFailure.INVALID_REQUEST
    if raw_request["source"] not in ALLOWED_SOURCES:
        return HirFailure.INVALID_SOURCE
    return None


def _payload_hash(raw_request):
    normalized = {
        "registration_request_id": raw_request.get("registration_request_id"),
        "aab_actor_id": raw_request.get("aab_actor_id"),
        "display_label": raw_request.get("display_label"),
        "source": raw_request.get("source"),
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()


def register_canonical_human(prov_conn, raw_request, _test_inject_failure_after=None):
    """The one entry point. prov_conn: open sqlite3 connection with
    row_factory = sqlite3.Row, to anaxi_provenance.db or a copy of it,
    with PRAGMA foreign_keys=ON.

    Returns (result_dict, None) on success/idempotent-replay, or
    (None, HirFailure) on failure. On failure, nothing is written.
    """
    shape_failure = _validate_shape(raw_request)
    if shape_failure is not None:
        return None, shape_failure

    request_id = raw_request["registration_request_id"]
    payload_hash = _payload_hash(raw_request)
    aab_actor_id = raw_request["aab_actor_id"]

    existing_request = prov_conn.execute(
        "SELECT * FROM human_registration_requests WHERE registration_request_id = ?",
        (request_id,),
    ).fetchone()
    if existing_request is not None:
        if existing_request["status"] == "committed":
            if existing_request["payload_hash"] == payload_hash:
                return json.loads(existing_request["result_json"]), None
            return None, HirFailure.REGISTRATION_REQUEST_CONFLICT
        return None, HirFailure.DUPLICATE_REQUEST

    existing_actor = prov_conn.execute(
        "SELECT actor_id, actor_type FROM actors WHERE actor_id = ?", (aab_actor_id,)
    ).fetchone()
    if existing_actor is not None:
        if existing_actor["actor_type"] != HUMAN_ACTOR_TYPE:
            return None, HirFailure.ACTOR_REGISTRATION_CONFLICT
        linked = prov_conn.execute(
            "SELECT person_id FROM actor_human_person WHERE actor_id = ?", (aab_actor_id,)
        ).fetchall()
        if len(linked) > 1:
            return None, HirFailure.PERSON_LINK_CONFLICT
        # 0 or 1 linked persons for an actor row not tracked by THIS
        # exact request_id: still ambiguous/untraceable for idempotency
        # purposes -- fail closed rather than silently reusing it.
        return None, HirFailure.ACTOR_REGISTRATION_CONFLICT

    return _commit_registration(prov_conn, raw_request, request_id, payload_hash, aab_actor_id, _test_inject_failure_after)


def _commit_registration(prov_conn, raw_request, request_id, payload_hash, aab_actor_id, _test_inject_failure_after):
    now = int(time.time())
    person_id = f"person-{uuid.uuid4().hex[:12]}"
    event_id = f"hir-event-{uuid.uuid4().hex[:12]}"
    display_label = raw_request["display_label"]

    result = {
        "person_id": person_id,
        "actor_id": aab_actor_id,
        "registration_event_id": event_id,
        "display_label": display_label,
    }

    with prov_conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")

        prov_conn.execute(
            "INSERT INTO persons (person_id, created_at, notes) VALUES (?, ?, NULL)",
            (person_id, now),
        )
        if _test_inject_failure_after == "person_insert":
            raise RuntimeError("injected test failure: person_insert")

        prov_conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, display_label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (aab_actor_id, HUMAN_ACTOR_TYPE, aab_actor_id, display_label, now),
        )
        if _test_inject_failure_after == "actor_insert":
            raise RuntimeError("injected test failure: actor_insert")

        prov_conn.execute(
            "INSERT INTO actor_human_person (actor_id, person_id, canonical_name, "
            "relationship_established_at) VALUES (?, ?, ?, ?)",
            (aab_actor_id, person_id, display_label, now),
        )
        if _test_inject_failure_after == "link_insert":
            raise RuntimeError("injected test failure: link_insert")

        prov_conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, ?, NULL, 'unknown', NULL, NULL, ?, ?, ?)",
            (event_id, REGISTRATION_EVENT_TYPE, request_id, now, now),
        )
        # administrative/bootstrap origin -- host_system is the
        # requester, never the human actor being registered
        prov_conn.execute(
            "INSERT INTO event_requesters (event_id, requester_actor_id) VALUES (?, ?)",
            (event_id, HOST_ACTOR_ID),
        )
        prov_conn.execute(
            "INSERT INTO event_subjects (event_id, subject_actor_id, role) VALUES (?, ?, ?)",
            (event_id, aab_actor_id, "registered_human_actor"),
        )
        if _test_inject_failure_after == "event_insert":
            raise RuntimeError("injected test failure: event_insert")

        prov_conn.execute(
            "INSERT INTO human_registration_requests (registration_request_id, aab_actor_id, "
            "display_label, source, payload_hash, status, person_id, actor_id, "
            "registration_event_id, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?)",
            (
                request_id, aab_actor_id, display_label, raw_request["source"], payload_hash,
                person_id, aab_actor_id, event_id, json.dumps(result), now,
            ),
        )

    return result, None
