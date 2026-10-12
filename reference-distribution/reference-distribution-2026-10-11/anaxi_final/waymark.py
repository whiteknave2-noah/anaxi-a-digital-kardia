"""Waymark V0: canonical append-only actions and a read-only deterministic fold.

A Waymark is a reference to exact Clark-authored canonical material.  This
module stores no Clark prose and owns no mutable projection table: Clark's
typed actions are components of the already-canonical waking turn, and every
query reconstructs state from those components plus mechanical delivery /
sunset receipts.  Reading this module's projections never writes or changes a
Waymark.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import re
import sqlite3
import urllib.parse

from family_membership import (
    SCOPE_FAMILY_SHARED,
    SCOPE_PRINCIPAL_PRIVATE,
    active_family_principals,
    can_receive_event,
    session_binding_audience,
)
from provenance_schema import derive_stable_id


ACTION_SCHEMA = "anaxi.waymark.action.v0"
RECEIPT_SCHEMA = "anaxi.waymark.receipt.v0"
ACTION_COMPONENT_KIND = "waymark_action"
SURFACE_DELIVERY_COMPONENT_KIND = "waymark_surface_delivery"
MECHANICAL_RECEIPT_COMPONENT_KIND = "waymark_mechanical_receipt"
MECHANICAL_EVENT_TYPE = "waymark_mechanical_event"

OPEN = "OPEN"
UNCERTAIN = "UNCERTAIN"
SET_ASIDE = "SET_ASIDE"
RELINQUISHED = "RELINQUISHED"
CLOSED = "CLOSED"
STATES = frozenset({OPEN, UNCERTAIN, SET_ASIDE, RELINQUISHED, CLOSED})
LIVE_STATES = frozenset({OPEN, UNCERTAIN, SET_ASIDE})
# Only these states let an armed event trigger surface (owner decision 2026-10-09): while SET_ASIDE (or
# relinquished/closed) the trigger stays stored but dormant -- contacts then neither fire, queue, consume nor
# disarm it; back in OPEN/UNCERTAIN it is eligible for contacts from that moment on.
SURFACING_STATES = frozenset({OPEN, UNCERTAIN})

SHELF_ONLY = "SHELF_ONLY"
EVENT_TRIGGERED = "EVENT_TRIGGERED"
SUPPRESSED = "SUPPRESSED"
POLICIES = frozenset({SHELF_ONLY, EVENT_TRIGGERED, SUPPRESSED})

CREATE = "create"
LIST = "list"
INSPECT = "inspect"
SET_STATE = "set_state"
SET_RESURFACING = "set_resurfacing"
REARM = "rearm"
SET_SUNSET = "set_sunset"
CLEAR_SUNSET = "clear_sunset"
REQUESTS = frozenset({CREATE, LIST, INSPECT, SET_STATE, SET_RESURFACING, REARM, SET_SUNSET, CLEAR_SUNSET})
MUTATION_REQUESTS = frozenset({CREATE, SET_STATE, SET_RESURFACING, REARM, SET_SUNSET, CLEAR_SUNSET})
# The closed field vocabulary of each request (the binding request's syntax statement is tested against it).
REQUEST_KEYS = {
    CREATE: frozenset({"state", "policy", "source", "condition", "sunset", "sunset_state"}),
    LIST: frozenset({"view", "cursor", "shelf_digest"}),
    INSPECT: frozenset({"handle", "projection_cursor", "projection_sha256"}),
    SET_STATE: frozenset({"handle", "state"}),
    SET_RESURFACING: frozenset({"handle", "policy", "condition"}),
    REARM: frozenset({"handle"}),
    SET_SUNSET: frozenset({"handle", "condition", "consequence"}),
    CLEAR_SUNSET: frozenset({"handle"}),
}

MAX_SPEC_CHARS = 512
# The workspace's resource collections (workspace_capability.RESOURCE_CLASSES; pinned equal by test).
RESOURCE_CLASSES = ("library", "music", "photographs", "journal", "notes")
MAX_SOURCE_EXCERPT_CHARS = 1000
QUERY_RESULT_MAX_TOKENS = 1280  # UTF-8 byte estimator => authoritative token upper bound
NOTICE_MAX_TOKENS = 256
_HANDLE_RE = re.compile(r"^waymark\.[0-9A-HJKMNP-TV-Z]{26}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


class WaymarkError(ValueError):
    """A Waymark request the host refuses.  ``code`` is the stable mechanical refusal class; ``reason`` the
    exact host text.  Base instances (and the subclasses below other than WaymarkSpecError) are terminal for
    the occasion: authority, scope, target state, control and integrity refusals are never retried."""

    def __init__(self, code_or_reason, reason=None):
        if reason is None:
            code_or_reason, reason = "WAYMARK_REFUSED", code_or_reason
        super().__init__(reason)
        self.code, self.reason = code_or_reason, reason


class WaymarkSpecError(WaymarkError):
    """A correctable refusal of Clark's own authored spec: its syntax, keys, value vocabulary, handle or
    condition form, or a continuation cursor/digest that does not name the present result.  Exactly one
    same-turn correction of the already-chosen Waymark request may follow (llama_anaxi._resolve_waymark)."""


class WaymarkAuthorityError(WaymarkError):
    """No authority context, or a target/source unavailable in it (absent and out-of-scope refuse alike)."""


class WaymarkRoomError(WaymarkError):
    """A read whose smallest whole result does not fit the room this occasion's carrier has left for it."""


class WaymarkStateError(WaymarkError):
    """The exact target exists but its present state forbids the request (rearm without an event trigger)."""


RETRYABLE_REFUSAL_CODES = frozenset({"SPEC_SYNTAX", "SPEC_KEYS", "SPEC_HANDLE", "SPEC_CONDITION", "SPEC_VALUE",
                                     "SPEC_CONTINUATION", "SPEC_ALREADY_DELIVERED"})


class WaymarkIntegrityError(RuntimeError):
    pass


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_pairs(spec: str) -> dict:
    if not isinstance(spec, str) or not spec or len(spec) > MAX_SPEC_CHARS:
        raise WaymarkSpecError("SPEC_SYNTAX", f"waymark_spec must contain 1..{MAX_SPEC_CHARS} characters")
    out = {}
    # Conventional, not semantic, tolerance (live acceptance 2: "state=OPEN; policy=SHELF_ONLY" read the
    # whole remainder as the value of state).  A ';' separates fields where the next field's name begins,
    # whatever whitespace surrounds the ';' or the '='; whitespace around a name or a whole value is not part
    # of it, interior whitespace always is.  So an item path that itself contains ';' still reads exactly
    # (it may also be written %3B, see _condition).  Names stay exact lower-case words: no aliases, no
    # spelling correction, and the vocabulary is closed per request (REQUEST_KEYS).
    for raw in re.split(r"\s*;\s*(?=[a-z][a-z0-9_]*\s*=)", spec.strip()):
        if "=" not in raw:
            raise WaymarkSpecError("SPEC_SYNTAX", "each waymark_spec field must be name=value")
        key, value = (part.strip() for part in raw.split("=", 1))
        if not key or not value or key in out or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise WaymarkSpecError("SPEC_SYNTAX", "waymark_spec contains an empty, duplicate, or invalid field name")
        out[key] = value
    return out


def _only(values: dict, allowed) -> None:
    extra = set(values) - set(allowed)
    if extra:
        raise WaymarkSpecError("SPEC_KEYS", f"unexpected waymark_spec field(s) {sorted(extra)!r}; this request "
                                            f"accepts only: {', '.join(sorted(allowed))}")


def _handle(value: str) -> str:
    if not isinstance(value, str) or _HANDLE_RE.fullmatch(value) is None:
        raise WaymarkSpecError("SPEC_HANDLE", "Waymark handle must be exact waymark.<26-character-id>")
    return value


def _condition(value: str) -> dict:
    if value.startswith("time:"):
        raw = value[5:]
        try:
            parsed = _datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WaymarkSpecError("SPEC_CONDITION", "time condition must be an exact ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise WaymarkSpecError("SPEC_CONDITION", "time condition must include a timezone")
        canonical = parsed.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        return {"kind": "exact_time", "value": canonical, "epoch": int(parsed.timestamp())}
    if value.startswith("resource:"):
        # The exact resource handle Clark is shown wherever a resource reaches him -- "<collection>/<item path>"
        # (the encounter fact's resource_class and relative_path, e.g. "library/Books/Tides.md").  Matching is
        # exact equality of both against canonical genuinely-delivered encounters: no name, topic, similarity
        # or near match.  Only ';' and '%' may be escaped (%3B, %25), so a path is otherwise copied verbatim.
        resource_class, sep, relative_path = value[len("resource:"):].partition("/")
        relative_path = relative_path.replace("%3B", ";").replace("%3b", ";").replace("%25", "%")
        if (not sep or resource_class not in RESOURCE_CLASSES or not relative_path
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in relative_path)):
            raise WaymarkSpecError("SPEC_CONDITION", "resource condition must be resource:<collection>/<exact item path>, collection one of "
                               + "|".join(RESOURCE_CLASSES))
        return {"kind": "exact_resource", "resource_class": resource_class, "relative_path": relative_path}
    raise WaymarkSpecError("SPEC_CONDITION", "condition must use exact time: or resource: mechanical syntax")


def resource_handle(resource_class: str, relative_path: str) -> str:
    """The canonical condition text of one exact resource (the inverse of _condition)."""
    return f"resource:{resource_class}/" + relative_path.replace("%", "%25").replace(";", "%3B")


def _require_continuation_identity(projection_cursor, projection_sha256):
    """A continuation names the exact projection it continues; an unnamed one could splice two snapshots."""
    if projection_cursor is not None and int(projection_cursor) > 0 and projection_sha256 is None:
        raise WaymarkSpecError("SPEC_CONTINUATION", "a projection_cursor beyond 0 requires the projection_sha256 it continues")


def parse_request(request: str, spec: str) -> dict:
    """Parse Clark's bounded mechanical syntax without interpreting prose."""
    if request not in REQUESTS:
        raise WaymarkError("CONTROL_REQUEST_UNKNOWN", "unknown Waymark request")
    values = _parse_pairs(spec)
    if request == CREATE:
        _only(values, REQUEST_KEYS[CREATE])
        state = values.get("state", OPEN)
        policy = values.get("policy", SHELF_ONLY)
        if state not in LIVE_STATES or policy not in POLICIES:
            raise WaymarkSpecError("SPEC_VALUE", "create state must be OPEN|UNCERTAIN|SET_ASIDE and policy "
                                                  "SHELF_ONLY|EVENT_TRIGGERED|SUPPRESSED")
        source = values.get("source", "self")
        if source != "self":
            match = re.fullmatch(r"turn:([A-Za-z0-9._:-]{1,120}):(\d+)", source)
            if match is None:
                raise WaymarkSpecError("SPEC_VALUE", "source must be self or exact turn:<event-id>:<sequence>")
            source = {"event_id": match.group(1), "sequence": int(match.group(2))}
        else:
            source = {"same_turn_clark_prose": True}
        resurfacing = {"policy": policy}
        if policy == EVENT_TRIGGERED:
            if "condition" not in values:
                raise WaymarkSpecError("SPEC_VALUE", "EVENT_TRIGGERED requires an exact condition")
            resurfacing["condition"] = _condition(values["condition"])
        elif "condition" in values:
            raise WaymarkSpecError("SPEC_VALUE", "only EVENT_TRIGGERED accepts a condition")
        sunset = None
        if "sunset" in values or "sunset_state" in values:
            if "sunset" not in values or values.get("sunset_state") not in {RELINQUISHED, CLOSED, SET_ASIDE, UNCERTAIN}:
                raise WaymarkSpecError("SPEC_VALUE", "sunset requires condition and exact lawful consequence state")
            sunset = {"condition": _condition(values["sunset"]), "consequence": values["sunset_state"]}
        return {"schema": ACTION_SCHEMA, "request": CREATE, "source": source, "state": state,
                "resurfacing": resurfacing, "sunset": sunset}
    if request == LIST:
        _only(values, REQUEST_KEYS[LIST])
        if values.get("view") not in {"live", "all"}:
            raise WaymarkSpecError("SPEC_VALUE", "list view must be live or all")
        cursor = values.get("cursor")
        if cursor is not None and (not cursor.isdigit() or int(cursor) < 0):
            raise WaymarkSpecError("SPEC_CONTINUATION", "list cursor must be the exact nonnegative cursor previously returned")
        shelf_digest = values.get("shelf_digest")
        if shelf_digest is not None and not re.fullmatch(r"[0-9a-f]{16}", shelf_digest):
            raise WaymarkSpecError("SPEC_CONTINUATION", "shelf_digest must be the exact 16-hex digest a shelf page returned")
        if cursor is not None and shelf_digest is None:
            raise WaymarkSpecError("SPEC_CONTINUATION", "a shelf cursor requires the shelf_digest of the page it continues")
        return {"schema": ACTION_SCHEMA, "request": LIST, "view": values["view"],
                "cursor": int(cursor) if cursor is not None else None, "shelf_digest": shelf_digest}
    if request == INSPECT:
        _only(values, REQUEST_KEYS[INSPECT])
        projection_cursor = values.get("projection_cursor")
        if projection_cursor is not None and (not projection_cursor.isdigit() or int(projection_cursor) < 0):
            raise WaymarkSpecError("SPEC_CONTINUATION", "projection_cursor must be a nonnegative byte cursor")
        projection_sha256 = values.get("projection_sha256")
        if projection_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", projection_sha256):
            raise WaymarkSpecError("SPEC_CONTINUATION", "projection_sha256 must be exact")
        _require_continuation_identity(projection_cursor, projection_sha256)
        return {"schema": ACTION_SCHEMA, "request": INSPECT, "handle": _handle(values.get("handle")),
                "projection_cursor": int(projection_cursor) if projection_cursor is not None else 0,
                "projection_sha256": projection_sha256}
    _only(values, REQUEST_KEYS[request])
    action = {"schema": ACTION_SCHEMA, "request": request, "handle": _handle(values.get("handle"))}
    if request == SET_STATE:
        if set(values) != {"handle", "state"} or values["state"] not in STATES:
            raise WaymarkSpecError("SPEC_VALUE", "set_state requires handle and exact lawful state")
        action["state"] = values["state"]
    elif request == SET_RESURFACING:
        if values.get("policy") not in POLICIES:
            raise WaymarkSpecError("SPEC_VALUE", "set_resurfacing requires a lawful policy")
        if values["policy"] == EVENT_TRIGGERED:
            if set(values) != {"handle", "policy", "condition"}:
                raise WaymarkSpecError("SPEC_VALUE", "EVENT_TRIGGERED requires exactly one mechanical condition")
            action["condition"] = _condition(values["condition"])
        elif set(values) != {"handle", "policy"}:
            raise WaymarkSpecError("SPEC_VALUE", "non-event resurfacing has no condition")
        action["policy"] = values["policy"]
    elif request == REARM:
        if set(values) != {"handle"}:
            raise WaymarkSpecError("SPEC_VALUE", "rearm accepts only a handle")
    elif request == SET_SUNSET:
        if set(values) != {"handle", "condition", "consequence"} or values["consequence"] not in {
                RELINQUISHED, CLOSED, SET_ASIDE, UNCERTAIN}:
            raise WaymarkSpecError("SPEC_VALUE", "set_sunset requires condition and exact lawful consequence")
        action["condition"] = _condition(values["condition"])
        action["consequence"] = values["consequence"]
    elif request == CLEAR_SUNSET:
        if set(values) != {"handle"}:
            raise WaymarkSpecError("SPEC_VALUE", "clear_sunset accepts only a handle")
    return action


def bind_creation(action: dict, event_id: str, clark_prose: str, *, data_dir: str = None) -> dict:
    """Bind creation to an exact source after this turn's event id exists."""
    if action.get("request") != CREATE:
        return dict(action)
    if not clark_prose and action["source"].get("same_turn_clark_prose"):
        raise WaymarkError("same-turn Waymark creation requires committed Clark prose")
    bound = json.loads(canonical_json(action))
    if bound["source"].pop("same_turn_clark_prose", False):
        bound["source"] = {
            "event_id": event_id,
            "sequence": 1,
            "content_sha256": hashlib.sha256(clark_prose.encode("utf-8")).hexdigest(),
        }
    elif "content_sha256" not in bound["source"]:
        if data_dir is None:
            raise WaymarkError("existing source binding requires the canonical store")
        db_path = os.path.join(data_dir, "anaxi_provenance.db")
        if not os.path.exists(db_path):
            raise WaymarkError("existing Waymark source store is unavailable")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = _component(conn, bound["source"])
            clark = derive_stable_id("actor", "clark")
            if (row is None or row[3] != clark or row[7] != "resolved"
                    or hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]):
                raise WaymarkError("existing source is absent, non-Clark-authored, or fails integrity")
            bound["source"]["content_sha256"] = row[6]
        finally:
            conn.close()
    bound["handle"] = f"waymark.{event_id}"
    return bound


def _table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _event_scope(conn, event_id):
    expr = "visibility_scope" if "visibility_scope" in _table_columns(conn, "events") else "NULL"
    return conn.execute(f"SELECT {expr} FROM events WHERE event_id=?", (event_id,)).fetchone()


def _event_principal(conn, event_id):
    """Principal of a scoped waking/H event; None means not established.

    A human-started turn resolves through its H.  A non-human (H-less)
    occasion turn carries no H and its own auth context is 'unknown'; it
    resolves to the one real human binding of the session it ran in, and
    only when the turn's own canonical visibility scope is exactly that
    binding's scope (the writer refuses to record an occasion turn under
    any other scope).  Every fact used is canonical, so a restart
    reconstructs the same principal.
    """
    row = conn.execute(
        "SELECT hc.component_text FROM event_components hc WHERE hc.event_id=? "
        "AND hc.component_kind='human_input_event_id'", (event_id,),
    ).fetchone()
    target = row[0] if row else event_id
    found = conn.execute(
        "SELECT ac.authenticated_actor_id FROM events e JOIN auth_contexts ac "
        "ON ac.auth_context_id=e.auth_context_id WHERE e.event_id=? "
        "AND ac.auth_state='authenticated'", (target,),
    ).fetchone()
    if found is not None or row is not None:
        return found
    occasion = conn.execute(
        "SELECT ac.session_id FROM events e JOIN auth_contexts ac ON ac.auth_context_id=e.auth_context_id "
        "WHERE e.event_id=? AND e.event_type='waking_turn' AND EXISTS (SELECT 1 FROM event_components oc "
        "WHERE oc.event_id=e.event_id AND oc.component_kind='waking_occasion_source')", (event_id,),
    ).fetchone()
    if occasion is None:
        return None
    audience = session_binding_audience(conn, occasion[0])
    scope_row = _event_scope(conn, event_id)
    if audience is None or scope_row is None or scope_row[0] != audience[1]:
        return None
    return (audience[0],)


def _component(conn, source):
    return conn.execute(
        "SELECT e.rowid,e.event_type,e.occurred_at,c.creator_actor_id,c.component_kind,c.component_text,"
        "c.content_sha256,c.authorship_resolution FROM events e JOIN event_components c "
        "ON c.event_id=e.event_id WHERE e.event_id=? AND c.sequence=?",
        (source["event_id"], source["sequence"]),
    ).fetchone()


def preflight_mutation(data_dir: str, *, session_id: str, action: dict) -> None:
    """Read-only early rejection for the ordinary waking path.

    The canonical writer repeats the authoritative validation inside its
    transaction.  This preflight exists so a request that is already known
    to lack a creating principal, visible exact source, or owned target is
    rejected before durable turn staging rather than leaving a staged bundle
    that can never lawfully commit.
    """
    if not isinstance(action, dict) or action.get("request") not in MUTATION_REQUESTS:
        raise WaymarkAuthorityError("AUTHORITY_UNAVAILABLE", "Waymark mutation is unavailable")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        raise WaymarkAuthorityError("AUTHORITY_UNAVAILABLE", "Waymark mutation is unavailable in this authority context")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        audience = session_binding_audience(conn, session_id)
        if audience is None:
            raise WaymarkAuthorityError("AUTHORITY_UNAVAILABLE", "Waymark mutation is unavailable in this authority context")
        principal, scope = audience
        if action["request"] == CREATE:
            source = action.get("source") or {}
            if source.get("same_turn_clark_prose"):
                return
            row = _component(conn, source)
            scope_row = _event_scope(conn, source.get("event_id"))
            source_principal = _event_principal(conn, source.get("event_id"))
            source_author = source_principal[0] if source_principal else None
            if (row is None or scope_row is None
                    or row[3] != derive_stable_id("actor", "clark") or row[7] != "resolved"
                    or hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]
                    or not can_receive_event(principal, scope, source_author, scope_row[0],
                                             active_family_principals(conn))):
                raise WaymarkAuthorityError("SOURCE_UNAVAILABLE", "exact Clark-authored source is unavailable in this authority context")
            return
        item, _audience = _visible_item(conn, action.get("handle"), session_id=session_id)
        if item is None or item["creator_principal_actor_id"] != principal:
            raise WaymarkAuthorityError("TARGET_UNAVAILABLE", "Waymark target is unavailable in this authority context")
        if action["request"] == REARM and item["resurfacing"]["policy"] != EVENT_TRIGGERED:
            raise WaymarkStateError("TARGET_STATE", "only EVENT_TRIGGERED may be rearmed")
    finally:
        conn.close()


def validate_canonical_action(conn: sqlite3.Connection, action: dict, *, event_id: str,
                              visibility_scope=None) -> None:
    """Validate the exact action inside the same transaction that records it."""
    if not isinstance(action, dict) or action.get("schema") != ACTION_SCHEMA or action.get("request") not in MUTATION_REQUESTS:
        raise WaymarkError("noncanonical Waymark mutation")
    # Round-trip through the public parser is intentionally not used: the
    # bound source digest/handle do not exist in Clark's pre-binding syntax.
    if action["request"] == CREATE:
        if action.get("handle") != f"waymark.{event_id}" or action.get("state") not in LIVE_STATES:
            raise WaymarkError("creation handle/state does not bind to this waking turn")
        source = action.get("source") or {}
        if set(source) != {"event_id", "sequence", "content_sha256"}:
            raise WaymarkError("creation source is not an exact durable reference")
        row = _component(conn, source)
        clark = derive_stable_id("actor", "clark")
        if row is None or row[3] != clark or row[6] != source["content_sha256"] or row[7] != "resolved":
            raise WaymarkError("creation source is absent, non-Clark-authored, or fails integrity")
        source_scope_row = _event_scope(conn, source["event_id"])
        source_scope = source_scope_row[0] if source_scope_row else None
        current_principal = _event_principal(conn, event_id)
        source_principal = _event_principal(conn, source["event_id"])
        cp = current_principal[0] if current_principal else None
        sp = source_principal[0] if source_principal else None
        if cp is None:
            raise WaymarkError("Waymark creation requires an established creating principal")
        if sp is not None and sp != cp and source_scope != SCOPE_FAMILY_SHARED:
            raise WaymarkError("private/legacy source belongs to another principal")
        if cp is not None and not can_receive_event(cp, visibility_scope, sp, source_scope,
                                                    active_family_principals(conn)):
            raise WaymarkError("Waymark scope cannot lawfully resolve its source")
        resurfacing = action.get("resurfacing") or {}
        if resurfacing.get("policy") not in POLICIES:
            raise WaymarkError("invalid creation resurfacing policy")
        if (resurfacing.get("policy") == EVENT_TRIGGERED) != ("condition" in resurfacing):
            raise WaymarkError("event policy/condition mismatch")
        if "condition" in resurfacing:
            _validate_condition_object(resurfacing["condition"])
        sunset = action.get("sunset")
        if sunset is not None:
            if (set(sunset) != {"condition", "consequence"}
                    or sunset["consequence"] not in {RELINQUISHED, CLOSED, SET_ASIDE, UNCERTAIN}):
                raise WaymarkError("invalid creation sunset")
            _validate_condition_object(sunset["condition"])
        return
    _handle(action.get("handle"))
    folded = _fold_all(conn)
    target = folded.get(action["handle"])
    if target is None:
        raise WaymarkError("target Waymark does not exist")
    current_principal = _event_principal(conn, event_id)
    cp = current_principal[0] if current_principal else None
    if cp is None or target["creator_principal_actor_id"] != cp:
        raise WaymarkError("target Waymark belongs to a different or unresolved principal")
    request = action["request"]
    if request == SET_STATE and (set(action) != {"schema", "request", "handle", "state"}
                                 or action.get("state") not in STATES):
        raise WaymarkError("invalid canonical state action")
    if request == SET_RESURFACING:
        expected = {"schema", "request", "handle", "policy"}
        if action.get("policy") == EVENT_TRIGGERED:
            expected.add("condition")
        if set(action) != expected or action.get("policy") not in POLICIES:
            raise WaymarkError("invalid canonical resurfacing action")
        if "condition" in action:
            _validate_condition_object(action["condition"])
    if request == SET_SUNSET:
        if (set(action) != {"schema", "request", "handle", "condition", "consequence"}
                or action.get("consequence") not in {RELINQUISHED, CLOSED, SET_ASIDE, UNCERTAIN}):
            raise WaymarkError("invalid canonical sunset action")
        _validate_condition_object(action["condition"])
    if request in {REARM, CLEAR_SUNSET} and set(action) != {"schema", "request", "handle"}:
        raise WaymarkError("invalid canonical handle-only action")
    if action["request"] == REARM and target["resurfacing"]["policy"] != EVENT_TRIGGERED:
        raise WaymarkError("only EVENT_TRIGGERED may be rearmed")


def _validate_condition_object(condition):
    if not isinstance(condition, dict) or condition.get("kind") not in {"exact_time", "exact_resource"}:
        raise WaymarkError("invalid mechanical condition")
    if condition["kind"] == "exact_time":
        if set(condition) != {"kind", "value", "epoch"} or _condition("time:" + condition["value"]) != condition:
            raise WaymarkError("noncanonical exact-time condition")
    else:
        required = {"kind", "resource_class", "relative_path"}
        if set(condition) != required:
            raise WaymarkError("noncanonical exact-resource condition")
        rendered = resource_handle(condition["resource_class"], condition["relative_path"])
        if _condition(rendered) != condition:
            raise WaymarkError("noncanonical exact-resource condition")


def _verified_actions(conn):
    clark = derive_stable_id("actor", "clark")
    rows = conn.execute(
        "SELECT e.rowid,e.event_id,e.occurred_at,c.sequence,c.component_text,c.content_sha256 "
        "FROM events e JOIN event_components c ON c.event_id=e.event_id "
        "WHERE e.event_type='waking_turn' AND c.component_kind=? AND c.creator_actor_id=? "
        "AND c.authorship_resolution='resolved' ORDER BY e.rowid,c.sequence",
        (ACTION_COMPONENT_KIND, clark),
    ).fetchall()
    out = []
    for rowid, event_id, occurred_at, sequence, text, digest in rows:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise WaymarkIntegrityError("Waymark action component hash mismatch")
        try:
            action = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise WaymarkIntegrityError("Waymark action is not canonical JSON") from exc
        if canonical_json(action) != text or action.get("schema") != ACTION_SCHEMA:
            raise WaymarkIntegrityError("Waymark action is not canonical")
        out.append((rowid, event_id, occurred_at, sequence, action))
    return out


def _verified_receipts(conn):
    host = derive_stable_id("actor", "bounded_clause_renderer")
    rows = conn.execute(
        "SELECT e.rowid,e.event_id,e.occurred_at,c.sequence,c.component_kind,c.component_text,c.content_sha256 "
        "FROM events e JOIN event_components c ON c.event_id=e.event_id "
        "WHERE c.component_kind IN (?,?) AND c.creator_actor_id=? "
        "AND c.authorship_resolution='resolved' ORDER BY e.rowid,c.sequence",
        (SURFACE_DELIVERY_COMPONENT_KIND, MECHANICAL_RECEIPT_COMPONENT_KIND, host),
    ).fetchall()
    out = []
    for rowid, event_id, occurred_at, sequence, kind, text, digest in rows:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise WaymarkIntegrityError("Waymark receipt hash mismatch")
        try:
            receipt = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise WaymarkIntegrityError("Waymark receipt is not JSON") from exc
        if canonical_json(receipt) != text or receipt.get("schema") != RECEIPT_SCHEMA:
            raise WaymarkIntegrityError("Waymark receipt is not canonical")
        out.append((rowid, event_id, occurred_at, sequence, kind, receipt))
    return out


def _fold_all(conn):
    """Deterministic reconstruction from canonical history alone: Clark's actions and the host's mechanical
    receipts, folded together in canonical order (event rowid, then component sequence)."""
    items = {}
    stream = ([(rowid, sequence, "action", event_id, occurred_at, action)
               for rowid, event_id, occurred_at, sequence, action in _verified_actions(conn)]
              + [(rowid, sequence, kind, event_id, occurred_at, receipt)
                 for rowid, event_id, occurred_at, sequence, kind, receipt in _verified_receipts(conn)])
    for rowid, _sequence, kind, event_id, occurred_at, record in sorted(stream, key=lambda r: (r[0], r[1])):
        if kind == "action":
            _fold_action(conn, items, rowid, event_id, occurred_at, _sequence, record)
        else:
            _fold_receipt(items, rowid, event_id, occurred_at, kind, record)
    for item in items.values():
        # The current state is the last in canonical order (a later explicit Clark choice wins over an earlier
        # sunset).  ``live_since`` is where the present uninterrupted OPEN/UNCERTAIN period began: the owner's
        # SET_ASIDE law (2026-10-09) -- only contacts inside that period can surface a trigger.
        timeline = item.pop("_state_timeline")
        item["state"] = timeline[-1][2]
        item["live_since_rowid"] = item["live_since_time"] = None
        live = False
        for state_rowid, state_time, state in timeline:
            if state in SURFACING_STATES and not live:
                item["live_since_rowid"], item["live_since_time"] = state_rowid, state_time
            live = state in SURFACING_STATES
    return items


def _fold_action(conn, items, rowid, event_id, occurred_at, sequence, action):
    request = action.get("request")
    if request == CREATE:
        handle = action.get("handle")
        if handle in items or not isinstance(handle, str):
            raise WaymarkIntegrityError("duplicate or invalid Waymark creation handle")
        principal = _event_principal(conn, event_id)
        scope_row = _event_scope(conn, event_id)
        items[handle] = {
            "handle": handle,
            "creation_rowid": rowid,
            "creation_event_id": event_id,
            "creation_component_sequence": sequence,
            "created_at": occurred_at,
            "creator_principal_actor_id": principal[0] if principal else None,
            "visibility_scope": scope_row[0] if scope_row else None,
            "source": action["source"],
            "_state_timeline": [(rowid, occurred_at, action["state"])],
            "resurfacing": dict(action["resurfacing"]),
            "trigger_armed": action["resurfacing"]["policy"] == EVENT_TRIGGERED,
            "trigger_anchor_rowid": rowid,
            "trigger_anchor_time": occurred_at,
            "trigger_anchor_event_id": event_id,
            "sunset": action.get("sunset"),
            "sunset_anchor_rowid": rowid if action.get("sunset") else None,
            "sunset_anchor_event_id": event_id if action.get("sunset") else None,
            "history": [{"event_id": event_id, "occurred_at": occurred_at, "request": request, "action": action}],
            "deliveries": [],
            "automatic_transitions": [],
        }
        return
    item = items.get(action.get("handle"))
    if item is None:
        raise WaymarkIntegrityError("Waymark mutation precedes its creation")
    principal = _event_principal(conn, event_id)
    if (principal[0] if principal else None) != item["creator_principal_actor_id"]:
        raise WaymarkIntegrityError("Waymark mutation principal mismatch")
    item["history"].append({"event_id": event_id, "occurred_at": occurred_at, "request": request, "action": action})
    if request == SET_STATE:
        item["_state_timeline"].append((rowid, occurred_at, action["state"]))
    elif request == SET_RESURFACING:
        item["resurfacing"] = {"policy": action["policy"]}
        if action["policy"] == EVENT_TRIGGERED:
            item["resurfacing"]["condition"] = action["condition"]
            item["trigger_armed"] = True
            item["trigger_anchor_rowid"], item["trigger_anchor_time"] = rowid, occurred_at
            item["trigger_anchor_event_id"] = event_id
        else:
            item["trigger_armed"] = False
    elif request == REARM:
        if item["resurfacing"]["policy"] != EVENT_TRIGGERED:
            raise WaymarkIntegrityError("rearm exists without an event trigger")
        item["trigger_armed"] = True
        item["trigger_anchor_rowid"], item["trigger_anchor_time"] = rowid, occurred_at
        item["trigger_anchor_event_id"] = event_id
    elif request == SET_SUNSET:
        item["sunset"] = {"condition": action["condition"], "consequence": action["consequence"]}
        item["sunset_anchor_rowid"] = rowid
        item["sunset_anchor_event_id"] = event_id
    elif request == CLEAR_SUNSET:
        item["sunset"] = None
        item["sunset_anchor_rowid"] = None
        item["sunset_anchor_event_id"] = None


def _fold_receipt(items, rowid, event_id, occurred_at, kind, receipt):
    item = items.get(receipt.get("handle"))
    if item is None:
        raise WaymarkIntegrityError("receipt references absent Waymark")
    if kind == SURFACE_DELIVERY_COMPONENT_KIND:
        item["deliveries"].append({"event_id": event_id, "occurred_at": occurred_at, "contact": receipt.get("contact")})
        # A delivery disarms only the exact trigger revision it names; a later Clark revision/rearm stands.
        if receipt.get("trigger_anchor_rowid") == item["trigger_anchor_rowid"]:
            item["trigger_armed"] = False
    elif receipt.get("kind") == "sunset_transition":
        # Folded in canonical order, a sunset receipt always names the sunset in force when it was written.
        if (receipt.get("sunset_anchor_rowid") != item["sunset_anchor_rowid"]
                or receipt.get("sunset_action_event_id") != item["sunset_anchor_event_id"]
                or item["sunset"] is None
                or canonical_json(receipt.get("condition")) != canonical_json(item["sunset"]["condition"])):
            raise WaymarkIntegrityError("sunset receipt disagrees with Clark's exact authorization")
        item["_state_timeline"].append((rowid, occurred_at, receipt["consequence"]))
        item["automatic_transitions"].append({
            "event_id": event_id, "occurred_at": occurred_at,
            "consequence": receipt["consequence"], "contact": receipt.get("contact"),
        })
        item["sunset"] = None
        item["sunset_anchor_rowid"] = None
        item["sunset_anchor_event_id"] = None


def _resource_contacts(conn, condition, after_rowid, *, principal_actor_id):
    """The first canonical, hash-verified, genuinely-delivered encounter after ``after_rowid`` of exactly the
    condition's resource (same collection, same item path) in this principal's own waking turns.  The
    encounter's own delivered-representation digest is kept on the contact as integrity evidence."""
    rows = conn.execute(
        "SELECT e.rowid,e.event_id,e.occurred_at,c.component_text,c.content_sha256 "
        "FROM events e JOIN event_components c ON c.event_id=e.event_id "
        "WHERE e.event_type='workspace_resource_encounter' "
        "AND c.component_kind='resource_encounter_fact' AND e.rowid>? ORDER BY e.rowid",
        (after_rowid,),
    ).fetchall()
    for rowid, event_id, occurred_at, text, digest in rows:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            continue
        try:
            fact = json.loads(text)
        except (TypeError, ValueError):
            continue
        if fact.get("genuinely_delivered") is not True:
            continue
        if (fact.get("resource_class"), fact.get("relative_path")) != (
                condition["resource_class"], condition["relative_path"]):
            continue
        trigger = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id=? "
            "AND component_kind='triggering_waking_event_id'", (event_id,)).fetchone()
        contact_principal = _event_principal(conn, trigger[0]) if trigger else None
        if contact_principal is None or contact_principal[0] != principal_actor_id:
            continue
        return {"kind": "exact_resource", "event_id": event_id, "event_rowid": rowid,
                "occurred_at": occurred_at,
                "resource_handle": resource_handle(fact["resource_class"], fact["relative_path"]),
                "delivered_content_sha256": fact.get("content_sha256")}
    return None


def matching_contact(conn, condition, *, after_rowid, now, principal_actor_id):
    """A condition's first contact after ``after_rowid`` (sunsets: from the sunset's own authorization)."""
    if condition["kind"] == "exact_time":
        if now >= condition["epoch"]:
            return {"kind": "exact_time", "condition_value": condition["value"],
                    "established_at": condition["epoch"]}
        return None
    return _resource_contacts(conn, condition, after_rowid, principal_actor_id=principal_actor_id)


def trigger_contact(conn, item, *, now):
    """The contact that can surface ``item`` now, or None.  It must lie inside the present uninterrupted
    OPEN/UNCERTAIN period and after the trigger's own (re)arming: a contact during SET_ASIDE (or any non-surfacing
    state) never fires, queues, consumes or disarms; the trigger is eligible again only for later contacts.  A
    time condition's contact is the later of its instant and the trigger's arming (a past instant set or rearmed
    now is met now)."""
    if (item["state"] not in SURFACING_STATES or not item["trigger_armed"]
            or item["resurfacing"]["policy"] != EVENT_TRIGGERED or item["live_since_rowid"] is None):
        return None
    condition = item["resurfacing"]["condition"]
    if condition["kind"] == "exact_time":
        moment = max(condition["epoch"], item["trigger_anchor_time"])
        if now < condition["epoch"] or moment < item["live_since_time"]:
            return None
        return {"kind": "exact_time", "condition_value": condition["value"], "established_at": condition["epoch"]}
    return _resource_contacts(conn, condition, max(item["trigger_anchor_rowid"], item["live_since_rowid"]),
                              principal_actor_id=item["creator_principal_actor_id"])


def pending_surfaces(data_dir: str, *, session_id: str, now: int, limit: int = 1) -> list:
    """Read-only eligible notices.  No match or inspection disarms anything."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        audience = session_binding_audience(conn, session_id)
        if audience is None:
            return []
        principal, scope = audience
        active = active_family_principals(conn)
        result = []
        for item in sorted(_fold_all(conn).values(), key=lambda x: (x["creation_rowid"], x["handle"])):
            if item["creator_principal_actor_id"] != principal:
                continue
            if item["state"] not in SURFACING_STATES or not item["trigger_armed"]:
                continue
            if item["resurfacing"]["policy"] != EVENT_TRIGGERED:
                continue
            if not can_receive_event(principal, scope, item["creator_principal_actor_id"],
                                     item["visibility_scope"], active):
                continue
            source_status = _source_status(conn, item, principal, scope, active, excerpt=False)
            if source_status["status"] != "resolvable":
                continue
            contact = trigger_contact(conn, item, now=now)
            if contact is None:
                continue
            receipt = {"schema": RECEIPT_SCHEMA, "kind": "surface_delivery", "handle": item["handle"],
                       "trigger_anchor_rowid": item["trigger_anchor_rowid"],
                       "trigger_action_event_id": item["trigger_anchor_event_id"],
                       "condition": item["resurfacing"]["condition"], "contact": contact}
            notice = render_notice(item["handle"], item["state"])
            if len(notice.encode("utf-8")) > NOTICE_MAX_TOKENS:
                raise WaymarkIntegrityError("Waymark notice exceeded its authoritative maximum")
            result.append({"handle": item["handle"], "state": item["state"], "contact": contact,
                           "receipt": receipt, "notice": notice})
            if len(result) >= limit:
                break
        return result
    finally:
        conn.close()


def validate_surface_receipt(conn: sqlite3.Connection, receipt: dict, *, event_id: str, now: int) -> None:
    """Prove a claimed delivery was eligible for this exact committed turn."""
    if (not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("kind") != "surface_delivery"):
        raise WaymarkError("invalid Waymark delivery receipt shape")
    item = _fold_all(conn).get(receipt.get("handle"))
    if item is None or item["state"] not in SURFACING_STATES or not item["trigger_armed"]:
        raise WaymarkError("Waymark delivery is not presently eligible")
    if item["resurfacing"]["policy"] != EVENT_TRIGGERED:
        raise WaymarkError("Waymark delivery lacks EVENT_TRIGGERED authority")
    if receipt.get("trigger_anchor_rowid") != item["trigger_anchor_rowid"]:
        raise WaymarkError("Waymark delivery names a stale trigger revision")
    if (receipt.get("trigger_action_event_id") != item["trigger_anchor_event_id"]
            or canonical_json(receipt.get("condition")) != canonical_json(item["resurfacing"]["condition"])):
        raise WaymarkError("Waymark delivery does not name its exact Clark-authored condition")
    event_principal = _event_principal(conn, event_id)
    if (event_principal[0] if event_principal else None) != item["creator_principal_actor_id"]:
        raise WaymarkError("Waymark delivery crosses principal authority")
    exact = trigger_contact(conn, item, now=now)
    if exact is None or canonical_json(exact) != canonical_json(receipt.get("contact")):
        raise WaymarkError("Waymark delivery contact is not mechanically established")


def _source_status(conn, item, principal, scope, active, *, excerpt):
    source = item["source"]
    scope_row = _event_scope(conn, source["event_id"])
    if scope_row is None:
        return {"status": "unavailable", "reason": "source_event_absent"}
    source_scope = scope_row[0]
    source_principal = _event_principal(conn, source["event_id"])
    source_author = source_principal[0] if source_principal else None
    if not can_receive_event(principal, scope, source_author, source_scope, active):
        return {"status": "inaccessible"}
    row = _component(conn, source)
    if row is None or row[6] != source["content_sha256"] or hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]:
        return {"status": "unavailable", "reason": "source_component_absent_or_integrity_failed"}
    clark = derive_stable_id("actor", "clark")
    if row[3] != clark or row[7] != "resolved":
        return {"status": "unavailable", "reason": "source_authorship_not_exact"}
    result = {"status": "resolvable", "event_id": source["event_id"], "sequence": source["sequence"],
              "content_sha256": source["content_sha256"], "component_kind": row[4]}
    if excerpt:
        result["exact_clark_text"] = row[5][:MAX_SOURCE_EXCERPT_CHARS]
        result["text_truncated"] = len(row[5]) > MAX_SOURCE_EXCERPT_CHARS
    return result


def _visible_item(conn, handle, *, session_id):
    audience = session_binding_audience(conn, session_id)
    if audience is None:
        return None, None
    principal, scope = audience
    item = _fold_all(conn).get(handle)
    if item is None or item["creator_principal_actor_id"] != principal:
        return None, (principal, scope)
    if not can_receive_event(principal, scope, item["creator_principal_actor_id"],
                             item["visibility_scope"], active_family_principals(conn)):
        return None, (principal, scope)
    return item, (principal, scope)


def inspect_waymark(data_dir: str, *, session_id: str, handle: str) -> dict:
    """Read-only exact inspection.  It never commits or rearms anything."""
    _handle(handle)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return {"exists": False}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        item, audience = _visible_item(conn, handle, session_id=session_id)
        if item is None or audience is None:
            # Nonexistence and out-of-scope existence are intentionally
            # indistinguishable: no Waymark side channel.
            return {"exists": False}
        principal, scope = audience
        source = _source_status(conn, item, principal, scope, active_family_principals(conn), excerpt=True)
        return {
            "exists": True,
            "handle": handle,
            "current_state": item["state"],
            "source": source,
            "resurfacing": dict(item["resurfacing"], trigger=trigger_status(item),
                                 trigger_armed=bool(item["trigger_armed"])),
            "sunset": item["sunset"],
            "surface_deliveries": item["deliveries"],
            "automatic_transitions": item["automatic_transitions"],
            # Last: the action history grows with every change, and must never push the source back.
            "history": item["history"],
        }
    finally:
        conn.close()


def trigger_status(item) -> str:
    """none (no event policy) | disarmed (delivered, not rearmed) | dormant (armed, but the state is not
    OPEN/UNCERTAIN: stored, never fires) | armed.  SUPPRESSED stays a policy of its own (trigger none)."""
    if item["resurfacing"]["policy"] != EVENT_TRIGGERED:
        return "none"
    if not item["trigger_armed"]:
        return "disarmed"
    return "armed" if item["state"] in SURFACING_STATES else "dormant"


def _creation_label(position, total) -> str:
    """The entry's place in its view's creation order, as a mechanical fact: '#k/n', plus 'oldest' / 'newest' at
    the two ends ('only' for a one-entry view).  Global to the view, so every page states the same positions."""
    ends = ("only" if total == 1 else "oldest" if position == 1 else "newest" if position == total else "")
    return f"#{position}/{total}" + (f" {ends}" if ends else "")


def _creation_facts(entry) -> str:
    """'created=<UTC time of the creating event> #k/n [oldest|newest|only]' -- canonical facts, never a ranking."""
    created = _datetime.datetime.fromtimestamp(entry["created_at"], _datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return f"created={created} {_creation_label(entry['position'], entry['total'])}"


def _shelf_line(entry) -> str:
    # Live acceptance 8 (H 01M4HPS3KQJVRFB2X964V5XWH6): the receipt stated the order only as a direction sentence,
    # while Clark's own earlier reply in the same prompt labelled each handle "(Most recent)"/"(Older)" the wrong
    # way round.  Each line now carries its own creation facts from canonical history: the creating event's time
    # and its position in the view's creation order.  The handle stays the line's first token.
    return (f"{entry['handle']} {_creation_facts(entry)} "
            f"{entry['current_state']} {entry['resurfacing_policy']} trigger={entry['trigger']} "
            f"source={entry['source']['status']}" + (" sunset=pending" if entry["sunset_pending"] else ""))


def _shelf_text(view, digest, first, last, total, lines, next_cursor):
    span = f"entries {first}-{last} of {total}" if lines else ("no Waymarks in this view" if not total
                                                               else f"no entries after this cursor (of {total})")
    head = f"{LIST_RESULT_LABEL} Shelf view={view}; creation order oldest -> newest; read-only, recommits nothing. " \
           f"shelf_digest={digest}; {span}:"
    tail = (f"more_exists=true; next page: view={view};cursor={next_cursor};shelf_digest={digest}"
            if next_cursor is not None else "more_exists=false")
    return "\n".join([head] + lines + [tail])


def list_waymarks(data_dir: str, *, session_id: str, include_historical=False, cursor=None,
                  shelf_digest=None, max_bytes=None) -> dict:
    """One complete shelf page: as many whole entries, in creation order after ``cursor``, as fit the one
    Waymark result (QUERY_RESULT_MAX_TOKENS), and a truthful continuation.  No ranking, summary or copied prose
    (inspect reads the exact source).  The continuation is bound to the view's exact ordered membership
    (``shelf_digest``, recomputed from canonical history alone, so restart-safe): if a Waymark entered or left
    the view since that page, the continuation is refused rather than silently skipping or repeating one."""
    if cursor is not None and (type(cursor) is not int or cursor < 0):
        raise WaymarkSpecError("SPEC_CONTINUATION", "cursor must be a nonnegative integer")
    view = "all" if include_historical else "live"
    entries = _shelf_entries(data_dir, session_id=session_id, include_historical=include_historical)
    digest = hashlib.sha256(canonical_json([e["handle"] for e in entries]).encode("utf-8")).hexdigest()[:16]
    if shelf_digest is not None and shelf_digest != digest:
        raise WaymarkSpecError("SPEC_CONTINUATION", "the shelf changed since that page (a Waymark entered or left this view); "
                           "list again from the start")
    start = 0
    if cursor is not None:
        if shelf_digest is None:
            raise WaymarkSpecError("SPEC_CONTINUATION", "a shelf cursor requires the shelf_digest of the page it continues")
        positions = [k for k, e in enumerate(entries) if e["cursor"] == cursor]
        if not positions:
            raise WaymarkSpecError("SPEC_CONTINUATION", "that cursor is not an entry of this shelf view; list again from the start")
        start = positions[0] + 1
    # Pack whole entries against the widest header/footer this page could have, within the room the carrier
    # gives this result (never above QUERY_RESULT_MAX_TOKENS).
    bound = QUERY_RESULT_MAX_TOKENS if max_bytes is None else min(QUERY_RESULT_MAX_TOKENS, max_bytes)
    widest = 10 ** len(str(max([len(entries)] + [e["cursor"] for e in entries] + [1]))) - 1
    page = []
    for entry in entries[start:]:
        trial = page + [entry]
        if len(_shelf_text(view, digest, widest, widest, widest, [_shelf_line(e) for e in trial],
                           widest).encode("utf-8")) > bound:
            break
        page = trial
    if entries[start:] and not page:
        if bound < QUERY_RESULT_MAX_TOKENS:
            raise WaymarkRoomError("EVIDENCE_ROOM_INSUFFICIENT",
                                   f"no whole shelf entry fits the {bound} units this occasion has left for it")
        raise WaymarkIntegrityError("a single shelf entry exceeds the Waymark result bound")
    if len(_shelf_text(view, digest, widest, widest, widest, [], widest).encode("utf-8")) > bound:
        raise WaymarkRoomError("EVIDENCE_ROOM_INSUFFICIENT",
                               f"the shelf page does not fit the {bound} units this occasion has left for it")
    more = start + len(page) < len(entries)
    next_cursor = page[-1]["cursor"] if more else None
    rendered = _bounded_result(_shelf_text(view, digest, start + 1, start + len(page), len(entries),
                                           [_shelf_line(e) for e in page], next_cursor))
    return {"items": [{k: v for k, v in e.items() if k != "cursor"} for e in page], "more_exists": more,
            "next_cursor": next_cursor, "shelf_digest": digest, "total": len(entries), "rendered": rendered,
            "snapshot": _shelf_snapshot(entries)}


def _shelf_entries(data_dir, *, session_id, include_historical):
    """The view's whole ordered membership with every status a shelf line shows (read-only)."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    entries = []
    if os.path.exists(db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            audience = session_binding_audience(conn, session_id)
            if audience is not None:
                principal, scope = audience
                active = active_family_principals(conn)
                for item in sorted(_fold_all(conn).values(), key=lambda x: (x["creation_rowid"], x["handle"])):
                    if item["creator_principal_actor_id"] != principal:
                        continue
                    if not include_historical and item["state"] not in LIVE_STATES:
                        continue
                    if not can_receive_event(principal, scope, item["creator_principal_actor_id"],
                                             item["visibility_scope"], active):
                        continue
                    entries.append({
                        "handle": item["handle"], "current_state": item["state"],
                        "resurfacing_policy": item["resurfacing"]["policy"], "trigger": trigger_status(item),
                        "sunset_pending": item["sunset"] is not None,
                        # The shelf names exact references and their honest status; inspect reads the text.
                        "source": _source_status(conn, item, principal, scope, active, excerpt=False),
                        "created_at": item["created_at"],
                        "cursor": item["creation_rowid"],
                    })
        finally:
            conn.close()
    for position, entry in enumerate(entries, 1):
        entry["position"], entry["total"] = position, len(entries)
    return entries


def _shelf_snapshot(entries) -> str:
    """The exact evidence a shelf view carries, independent of its view label and page size: every entry's
    shelf line in order.  Two views (or two moments) with the same snapshot show the same Waymarks with the
    same statuses -- re-reading one adds nothing."""
    return hashlib.sha256(canonical_json([[e["cursor"], _shelf_line(e)] for e in entries]).encode("utf-8")).hexdigest()


def shelf_snapshot(data_dir: str, *, session_id: str, include_historical=False) -> str:
    """Read-only: the current ``snapshot`` of a shelf view (what list_waymarks would report now)."""
    return shelf_view(data_dir, session_id=session_id, include_historical=include_historical)["snapshot"]


def shelf_view(data_dir: str, *, session_id: str, include_historical=False) -> dict:
    """Read-only: a shelf view's current ``snapshot`` and its ordered (cursor, shelf line) entries -- exactly the
    lines a page of it would show."""
    entries = _shelf_entries(data_dir, session_id=session_id, include_historical=include_historical)
    return {"snapshot": _shelf_snapshot(entries), "lines": [(e["cursor"], _shelf_line(e)) for e in entries],
            "creation_facts": {e["handle"]: _creation_facts(e) for e in entries},
            "shelf_digest": hashlib.sha256(canonical_json([e["handle"] for e in entries]).encode("utf-8")).hexdigest()[:16]}


def shelf_line(item) -> str:
    """The exact shelf line of one listed item (a list_waymarks page item)."""
    return _shelf_line(item)


def projection_identity(data_dir: str, *, session_id: str, handle: str) -> str:
    """Read-only: the projection_sha256 an inspect of ``handle`` would report now (the exact projection text's
    digest; an invisible or absent Waymark has the digest of its not-found projection)."""
    projection = projection_text(inspect_waymark(data_dir, session_id=session_id, handle=handle))
    return hashlib.sha256(projection.encode("utf-8")).hexdigest()


# Every Waymark text Clark receives says first that it is the host's mechanical record, not a person's
# words and not a request (a bare user-role message would otherwise read as the human speaking), and WHICH
# operation it is the result of: one operation's outcome is never a statement about another (live acceptance
# 4: a refused list was read as evidence that an earlier create had failed).
def _result_head(operation, status):
    return (f"WAYMARK {str(operation).upper()} HOST RESULT \u2014 {status} "
            "(mechanical host record; not a human message; nothing asked).")


LIST_RESULT_LABEL = _result_head(LIST, "READ")
INSPECT_RESULT_LABEL = _result_head(INSPECT, "READ")


def terminal_outcome(request: str) -> str:
    """The typed outcome of a refused request: only a create can be NOT_CREATED."""
    return "NOT_CREATED" if request == CREATE else "NOT_PERFORMED"
HOST_NOTICE_LABEL = "ANAXI host Waymark notice (mechanical; not a human message; nothing is asked of you)."


def render_notice(handle: str, state: str) -> str:
    """The declarative surface of one eligible trigger: which Waymark, that its own condition matched, and
    that nothing changed.  No source text, summary, salience or suggestion."""
    notice = (f"{HOST_NOTICE_LABEL} The resurfacing condition you set on {handle} matched a mechanically "
              f"established event. Its state is unchanged: {state}.")
    if len(notice.encode("utf-8")) > NOTICE_MAX_TOKENS:
        raise WaymarkIntegrityError("Waymark notice exceeded its authoritative maximum")
    return notice


def _bounded_result(text: str) -> str:
    if len(text.encode("utf-8")) > QUERY_RESULT_MAX_TOKENS:
        raise WaymarkIntegrityError("bounded Waymark result exceeded its authoritative maximum")
    return text


def render_accepted_for_commit(request: str, *, handle=None, resource_seen=None, attempt=1) -> str:
    """A valid mutation, BEFORE canonical commit: accepted, never yet created or changed.  It exists only if
    this waking turn commits; afterward list/inspect read its canonical state.  For a resource condition, the
    one mechanical fact that exposes a mistyped handle without echoing it."""
    target = (f"a new Waymark whose handle will be waymark.<this waking turn's event id>" if handle is None
              else handle)
    seen = ("" if resource_seen is None else
            " The exact resource in its condition " + ("has" if resource_seen else "has NO")
            + " canonical record of having been delivered to you before.")
    corrected = " (accepted on your one correction)" if attempt == 2 else ""
    return _bounded_result(
        f"{_result_head(request, 'ACCEPTED FOR COMMIT')} Your Waymark {request}{corrected} for {target} is accepted but "
        "NOT yet created or applied: it exists only if this whole waking turn commits; list or inspect on a later "
        "turn reads its committed state. A Waymark grants no action authority and creates no obligation." + seen)


_NOT_PERFORMED_SCOPE = {
    CREATE: "No Waymark was created by this create request",
    LIST: ("This list request read and changed no Waymark; it does not establish whether any Waymark exists "
           "or whether an earlier create succeeded"),
    INSPECT: ("This inspect request read and changed no Waymark; it does not establish whether its target "
              "exists"),
}


def render_not_created(request: str, code: str, reason: str, *, attempt: int, max_bytes=None) -> str:
    """The terminal refusal of this occasion's Waymark request, scoped to exactly that operation: what it did
    not do, what it does not establish, and that no further attempt of it remains in this waking turn.
    Out-of-scope and absent targets refuse alike."""
    status = "NOT CREATED" if request == CREATE else "NOT PERFORMED"
    scope = _NOT_PERFORMED_SCOPE.get(
        request, f"This {request} request changed no Waymark; it establishes nothing about any earlier request")
    head = (f"{_result_head(request, status)} {scope}; no further attempt of this request remains in this "
            f"waking turn (attempt {attempt} of 2). Refusal {code}: ")
    bound = QUERY_RESULT_MAX_TOKENS if max_bytes is None else min(QUERY_RESULT_MAX_TOKENS, max_bytes)
    room = max(0, bound - len((head + ".").encode("utf-8")))
    reason = str(reason).encode("utf-8")[:room].decode("utf-8", "ignore")
    return _bounded_result(head + reason + ".")


def action_resource_condition(action: dict):
    """The exact-resource condition a create/set_resurfacing/set_sunset action names, else None."""
    for condition in ((action.get("resurfacing") or {}).get("condition"), action.get("condition"),
                      (action.get("sunset") or {}).get("condition")):
        if isinstance(condition, dict) and condition.get("kind") == "exact_resource":
            return condition
    return None


def resource_delivered_before(data_dir: str, *, session_id: str, condition: dict) -> bool:
    """Read-only: has this session's principal ever genuinely received exactly this resource (canonical)?"""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        audience = session_binding_audience(conn, session_id)
        return audience is not None and _resource_contacts(
            conn, condition, 0, principal_actor_id=audience[0]) is not None
    finally:
        conn.close()


def projection_text(result: dict) -> str:
    """The exact inspect projection, in its deterministic construction order (what Clark most needs first:
    handle, state, exact source; the growing history last).  Nested values are canonical by construction."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def render_query_result(result: dict, *, handle=None, projection_cursor=0, expected_sha256=None,
                        max_bytes=None) -> str:
    """Exact byte-paged projection, never a summary.

    The digest freezes continuation identity. If canonical state changed
    between pages, the old cursor is rejected rather than spliced into a new
    snapshot. UTF-8 byte length is capped by QUERY_RESULT_MAX_TOKENS, the same
    conservative accounting primitive used by context_budget.
    """
    if type(projection_cursor) is not int or projection_cursor < 0:
        raise WaymarkSpecError("SPEC_CONTINUATION", "projection cursor must be a nonnegative integer")
    projection = projection_text(result)
    encoded = projection.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise WaymarkSpecError("SPEC_CONTINUATION", "Waymark projection changed; restart inspection from projection_cursor=0")
    if projection_cursor > len(encoded):
        raise WaymarkSpecError("SPEC_CONTINUATION", "projection cursor lies beyond the exact projection")
    if projection_cursor < len(encoded) and projection_cursor > 0 and encoded[projection_cursor] & 0xC0 == 0x80:
        raise WaymarkSpecError("SPEC_CONTINUATION", "projection cursor is not on an exact UTF-8 boundary")
    def header(next_value):
        return (f"{INSPECT_RESULT_LABEL} handle={handle}; read-only exact projection; reading does not recommit "
                f"or rearm anything. projection_sha256={digest} total_bytes={len(encoded)} cursor={projection_cursor} "
                f"next_projection_cursor={next_value} exact_utf8_fragment:\n")
    # The fragment takes all the room the widest possible header leaves (a continuation's own cursor is
    # never wider than the total), within the room the carrier gives this result.
    bound = QUERY_RESULT_MAX_TOKENS if max_bytes is None else min(QUERY_RESULT_MAX_TOKENS, max_bytes)
    widest = max(len(str(len(encoded))), len("None"))
    room = bound - len(header("9" * widest).encode("utf-8"))
    if room < min(len(encoded) - projection_cursor, 64) or room <= 0:
        raise WaymarkRoomError("EVIDENCE_ROOM_INSUFFICIENT",
                               f"the inspect result does not fit the {bound} units this occasion has left for it")
    end = min(len(encoded), projection_cursor + room)
    while end > projection_cursor:
        try:
            fragment = encoded[projection_cursor:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        fragment = ""
    next_cursor = end if end < len(encoded) else None
    return _bounded_result(header(next_cursor) + fragment)


def reconcile_sunsets(data_dir: str, *, now: int) -> list:
    """Commit only Clark-preauthorized exact mechanical consequences.

    The deterministic receipt id makes retries/restarts idempotent.  Age or
    inactivity is never consulted; only the stored condition vocabulary is.
    """
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    written = []
    try:
        event_columns = _table_columns(conn, "events")
        for item in _fold_all(conn).values():
            # A sunset winds a live thread down to the state Clark preauthorized.  Once Clark has himself
            # relinquished or closed it, no earlier preauthorization may move it back into a live state;
            # the sunset stays visible (inspect) and Clark may clear it.
            if not item["sunset"] or item["state"] not in LIVE_STATES:
                continue
            contact = matching_contact(conn, item["sunset"]["condition"],
                                       after_rowid=item["sunset_anchor_rowid"], now=now,
                                       principal_actor_id=item["creator_principal_actor_id"])
            if contact is None:
                continue
            basis = canonical_json({"handle": item["handle"], "anchor": item["sunset_anchor_rowid"],
                                    "contact": contact})
            event_id = "wmr-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:26].upper()
            creation = conn.execute(
                "SELECT pipeline_id FROM events WHERE event_id=?", (item["creation_event_id"],)
            ).fetchone()
            if creation is None or creation[0] is None:
                continue
            receipt = {
                "schema": RECEIPT_SCHEMA, "kind": "sunset_transition", "handle": item["handle"],
                "sunset_anchor_rowid": item["sunset_anchor_rowid"],
                "sunset_action_event_id": item["sunset_anchor_event_id"],
                "condition": item["sunset"]["condition"],
                "consequence": item["sunset"]["consequence"], "contact": contact,
            }
            text = canonical_json(receipt)
            existing = conn.execute(
                "SELECT ec.creator_actor_id,ec.component_kind,ec.authorship_resolution,"
                "ec.component_text,ec.content_sha256 FROM events e JOIN event_components ec "
                "ON ec.event_id=e.event_id WHERE e.event_id=? AND ec.sequence=0",
                (event_id,),
            ).fetchone()
            if existing is not None:
                expected_creator = derive_stable_id("actor", "bounded_clause_renderer")
                expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if tuple(existing) != (
                        expected_creator, MECHANICAL_RECEIPT_COMPONENT_KIND, "resolved", text, expected_hash):
                    raise WaymarkIntegrityError("deterministic sunset receipt id has conflicting canonical content")
                continue
            # Privacy only inherits: the receipt carries the Waymark's own visibility scope.
            scoped = "visibility_scope" in event_columns and item["visibility_scope"] is not None
            with conn:
                conn.execute(
                    "INSERT INTO events(event_id,event_type,pipeline_id,pipeline_provenance_status,epoch_id,"
                    "auth_context_id,input_source_ref,occurred_at,record_created_at"
                    + (",visibility_scope" if scoped else "") + ") "
                    "VALUES(?,?,?,'known',NULL,NULL,?,?,?" + (",?" if scoped else "") + ")",
                    (event_id, MECHANICAL_EVENT_TYPE, creation[0], item["creation_event_id"], now, now)
                    + ((item["visibility_scope"],) if scoped else ()),
                )
                conn.execute(
                    "INSERT INTO event_components(event_id,sequence,creator_actor_id,component_kind,"
                    "authorship_resolution,component_text,content_sha256,model_revision_id,span_start,span_end) "
                    "VALUES(?,0,?,?,'resolved',?,?,NULL,NULL,NULL)",
                    (event_id, derive_stable_id("actor", "bounded_clause_renderer"),
                     MECHANICAL_RECEIPT_COMPONENT_KIND, text, hashlib.sha256(text.encode("utf-8")).hexdigest()),
                )
            written.append(event_id)
        return written
    finally:
        conn.close()
