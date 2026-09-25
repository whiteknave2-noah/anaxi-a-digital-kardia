"""General correspondence: surfaces, sources, Clark-controlled standing, and the durable lifecycle.

Owner law (whole-system completion, Step 2): the owner authorizes DOORS (surfaces/capabilities), not
the people who may come through them.  An unknown source may converse on a surface the owner opened
to unknown sources, identified only by its stable source id and non-authoritative display metadata.
Clark alone establishes or revokes ongoing correspondent STANDING with a source.  Standing means only
"I choose ongoing correspondence with this source through this lawful capability": it grants
continuity with that source (prospectively, from the establish act) and lets Clark independently
initiate on a surface whose owner policy permits initiation.  It is never trust, friendship,
verified identity, authority, cross-surface identity or access to anyone else's continuity.

Four axes stay apart: SOURCE (a stable transport id -- read off provenance), IDENTITY (optional
owner-recorded canonical person; canonical_person), SURFACE AUTHORIZATION (owner), STANDING (Clark).
Relationship meaning is never a host field.

Silence attribution: only Clark's typed no_reply is his silence.  Retry exhaustion, owner closure
and undelivered sends are host/owner facts recorded here with their own provenance; none of them is
ever rendered as Clark's choice.

Pure readers take an open connection (read-only is fine); writers open their own and apply the CS1
migration on it.
"""
import json
import secrets
import sqlite3
import time

import cs1_schema_migration
import family_membership

SOURCE_KIND_DISCORD_USER = "discord_user"
SOURCE_KINDS = (SOURCE_KIND_DISCORD_USER,)

STANDING_NONE = "NONE"
STANDING_ESTABLISHED = "ESTABLISHED"
STANDING_REVOKED = "REVOKED"
STANDING_ACTIONS = ("establish", "revoke")

MAX_OCCASION_ATTEMPTS = 5
MAX_HOST_RESUME_FAILURES = 3

# Canonical waking-turn component kinds this capability introduces.
CORRESPONDENT_SOURCE_REF_COMPONENT_KIND = "correspondent_source_ref"          # host: whose occasion
CORRESPONDENT_STANDING_REQUEST_COMPONENT_KIND = "correspondent_standing_request"  # Clark's typed choice
DISCORD_CORRESPONDENT_REF_COMPONENT_KIND = "discord_correspondent_ref"        # Clark: initiation addressee
WITHDRAW_PENDING_SEND_COMPONENT_KIND = "withdraw_pending_send_request"        # Clark: withdraw undelivered

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class CorrespondenceError(Exception):
    """A correspondence act cannot lawfully be carried out.  Never caught silently."""


def _new_id(prefix):
    value = int.from_bytes(int(time.time() * 1000).to_bytes(6, "big") + secrets.token_bytes(10), "big")
    return prefix + "".join(_CROCKFORD[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def source_ref(kind, value):
    return f"{kind}:{value}"


def parse_source_ref(ref):
    """(kind, value) for a well-formed ref, else None.  Never guesses from a display name."""
    if not isinstance(ref, str) or ":" not in ref:
        return None
    kind, value = ref.split(":", 1)
    if kind != SOURCE_KIND_DISCORD_USER or not value.isdigit() or not (5 <= len(value) <= 32):
        return None
    return kind, value


def _present(conn):
    try:
        return cs1_schema_migration.tables_present(conn)
    except sqlite3.Error:
        return False


def _connect(data_dir):
    conn = sqlite3.connect(f"{data_dir}/anaxi_provenance.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    cs1_schema_migration.apply_migration_on_connection(conn)
    return conn


def _require_owner(conn, requester_actor_id):
    owner = family_membership.resolve_owner_actor_id(conn)
    if owner is None or requester_actor_id != owner:
        raise CorrespondenceError("only the canonical owner may change surface policy, blocks or occasion closure")


def _insert(conn, sql, values):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(sql, values)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ----------------------------------------------------------------- surface policy (owner)

def surface_policy(conn, destination_id):
    """Current owner policy for one destination.  Default (no row): closed to unknown sources,
    no independent initiation -- exactly the behavior every surface had before this capability."""
    policy = {"allow_unknown_sources": False, "permit_independent_initiation": False,
              "effective_at": None, "policy_event_id": None}
    if not _present(conn):
        return policy
    row = conn.execute(
        "SELECT policy_event_id, allow_unknown_sources, permit_independent_initiation, created_at "
        "FROM correspondence_surface_policy_events WHERE destination_id = ? ORDER BY rowid DESC LIMIT 1",
        (destination_id,)).fetchone()
    if row is None:
        return policy
    return {"allow_unknown_sources": bool(row[1]), "permit_independent_initiation": bool(row[2]),
            "effective_at": row[3], "policy_event_id": row[0]}


def unknown_sources_admitted_since(conn, destination_id):
    """Host-recorded time from which the CURRENT open-to-unknown-sources policy has held, or None
    when the surface is not open now.  Prospective: a message is admitted only if it arrived after
    this (the same conservative law as destination authorization and author mapping)."""
    if not _present(conn):
        return None
    rows = conn.execute(
        "SELECT allow_unknown_sources, created_at FROM correspondence_surface_policy_events "
        "WHERE destination_id = ? ORDER BY rowid", (destination_id,)).fetchall()
    since = None
    for allow, created_at in rows:
        if allow and since is None:
            since = created_at
        elif not allow:
            since = None
    return since


def set_surface_policy(data_dir, *, destination_id, allow_unknown_sources, permit_independent_initiation,
                       requester_actor_id, occurred_at, destination_authorized):
    """Owner-only.  Opening a surface to unknown sources requires Family scope to be in force: that
    is what keeps a non-principal source's correspondence out of every principal's continuity."""
    if type(occurred_at) is not int:
        raise CorrespondenceError("occurred_at must be an integer second timestamp")
    if not destination_authorized:
        raise CorrespondenceError("policy can only be set on a currently authorized destination")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        if allow_unknown_sources and not family_membership.family_feature_used(conn):
            raise CorrespondenceError(
                "a surface can be opened to unknown sources only once Family scope is active "
                "(it is what isolates their correspondence from every principal's continuity)")
        event_id = _new_id("cspol-")
        _insert(conn, "INSERT INTO correspondence_surface_policy_events (policy_event_id, destination_id, "
                      "allow_unknown_sources, permit_independent_initiation, requester_actor_id, occurred_at, "
                      "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, destination_id, int(bool(allow_unknown_sources)),
                 int(bool(permit_independent_initiation)), requester_actor_id, occurred_at, int(time.time())))
        return {"policy_event_id": event_id}
    finally:
        conn.close()


# ----------------------------------------------------------------- source blocks (owner)

def is_blocked(conn, kind, value):
    if not _present(conn):
        return False
    row = conn.execute(
        "SELECT action FROM correspondence_source_block_events WHERE source_kind = ? AND source_value = ? "
        "ORDER BY rowid DESC LIMIT 1", (kind, value)).fetchone()
    return bool(row and row[0] == "block")


def set_source_block(data_dir, *, kind, value, blocked, requester_actor_id, occurred_at):
    """Owner hard safety boundary.  Never touches standing and carries no relational meaning."""
    if parse_source_ref(source_ref(kind, value)) is None or type(occurred_at) is not int:
        raise CorrespondenceError("a block needs a well-formed stable source id and timestamp")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        event_id = _new_id("csblk-")
        _insert(conn, "INSERT INTO correspondence_source_block_events (block_event_id, source_kind, source_value, "
                      "action, requester_actor_id, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, kind, value, "block" if blocked else "unblock", requester_actor_id, occurred_at,
                 int(time.time())))
        return {"block_event_id": event_id}
    finally:
        conn.close()


# ----------------------------------------------------------------- standing (Clark)

def standing(conn, kind, value):
    """Current standing, freshly reduced.  NONE is a first-class permanent-if-unchanged state."""
    result = {"state": STANDING_NONE, "established_at": None, "revoked_at": None,
              "standing_event_id": None, "history": []}
    if not _present(conn):
        return result
    rows = conn.execute(
        "SELECT standing_event_id, action, occurred_at, created_at, waking_turn_event_id "
        "FROM correspondent_standing_events WHERE source_kind = ? AND source_value = ? ORDER BY rowid",
        (kind, value)).fetchall()
    for event_id, action, occurred_at, created_at, turn in rows:
        result["history"].append({"standing_event_id": event_id, "action": action, "occurred_at": occurred_at,
                                  "recorded_at": created_at, "waking_turn_event_id": turn})
        if action == "establish":
            result.update(state=STANDING_ESTABLISHED, established_at=created_at, revoked_at=None,
                          standing_event_id=event_id)
        else:
            result.update(state=STANDING_REVOKED, revoked_at=created_at, standing_event_id=event_id)
    return result


def record_standing_act(data_dir, *, kind, value, action, waking_turn_event_id, occurred_at):
    """Clark's typed choice, already canonical in `waking_turn_event_id`, becomes a standing act.
    The DB trigger re-proves the binding.  Idempotent per turn (UNIQUE turn id)."""
    if action not in STANDING_ACTIONS or parse_source_ref(source_ref(kind, value)) is None:
        raise CorrespondenceError("unknown standing action or malformed source")
    conn = _connect(data_dir)
    try:
        existing = conn.execute(
            "SELECT standing_event_id, action, source_kind, source_value FROM correspondent_standing_events "
            "WHERE waking_turn_event_id = ?", (waking_turn_event_id,)).fetchone()
        if existing is not None:
            if existing[1:] != (action, kind, value):
                raise CorrespondenceError("that waking turn already recorded a different standing act")
            return {"standing_event_id": existing[0], "replayed": True}
        current = standing(conn, kind, value)["state"]
        if (action == "establish" and current == STANDING_ESTABLISHED) or (
                action == "revoke" and current != STANDING_ESTABLISHED):
            return {"standing_event_id": None, "unchanged": True, "state": current}
        event_id = _new_id("cstand-")
        _insert(conn, "INSERT INTO correspondent_standing_events (standing_event_id, source_kind, source_value, "
                      "action, requester_actor_id, waking_turn_event_id, occurred_at, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, kind, value, action, cs1_schema_migration.CLARK_ACTOR_ID, waking_turn_event_id,
                 occurred_at, int(time.time())))
        return {"standing_event_id": event_id}
    finally:
        conn.close()


def standing_sources(conn):
    """Every source with any standing history (for Clark's menu and the owner's read-only view)."""
    if not _present(conn):
        return []
    rows = conn.execute("SELECT DISTINCT source_kind, source_value FROM correspondent_standing_events").fetchall()
    return [dict(kind=k, value=v, ref=source_ref(k, v), **{x: y for x, y in standing(conn, k, v).items()
                                                            if x != "history"}) for k, v in rows]


# ----------------------------------------------------------------- inbound occasion lifecycle

def occasion_state(conn, inbound_event_id):
    """Durable lifecycle of one inbound occasion: failed attempts since the owner's last reopen,
    retry exhaustion, and owner closure.  Exhaustion is a HOST fact (never Clark's silence)."""
    state = {"failures": 0, "exhausted": False, "closed": False, "last_failure_at": None,
             "last_failure_class": None}
    if not _present(conn):
        return state
    acts = conn.execute(
        "SELECT action, attempt_watermark FROM correspondence_occasion_owner_acts WHERE inbound_event_id = ? "
        "ORDER BY rowid", (inbound_event_id,)).fetchall()
    watermark = 0
    for action, act_watermark in acts:
        state["closed"] = action == "close"
        if action == "reopen":
            watermark = act_watermark      # only failures recorded AFTER the reopen count (exact order)
    rows = conn.execute(
        "SELECT failure_class, attempted_at, created_at FROM correspondence_occasion_attempts "
        "WHERE inbound_event_id = ? AND rowid > ? ORDER BY rowid", (inbound_event_id, watermark)).fetchall()
    state["failures"] = len(rows)
    if rows:
        state["last_failure_class"], state["last_failure_at"] = rows[-1][0], rows[-1][1]
    state["exhausted"] = state["failures"] >= MAX_OCCASION_ATTEMPTS
    return state


def record_occasion_failure(data_dir, *, inbound_event_id, failure_class, attempted_at):
    conn = _connect(data_dir)
    try:
        _insert(conn, "INSERT INTO correspondence_occasion_attempts (attempt_id, inbound_event_id, failure_class, "
                      "attempted_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (_new_id("csatt-"), inbound_event_id, str(failure_class)[:120], attempted_at, int(time.time())))
        return occasion_state(conn, inbound_event_id)
    finally:
        conn.close()


def owner_occasion_act(data_dir, *, inbound_event_id, action, requester_actor_id, occurred_at):
    """Owner administrative closure (terminal for waking; the event is never deleted) or reopen
    (a fresh bounded retry budget).  An owner act -- never Clark's."""
    if action not in ("close", "reopen"):
        raise CorrespondenceError("unknown owner occasion action")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        event = conn.execute("SELECT event_type FROM events WHERE event_id = ?", (inbound_event_id,)).fetchone()
        if event is None or event[0] != "discord_inbound_message":
            raise CorrespondenceError("no such inbound correspondence event")
        act_id = _new_id("csown-")
        conn.execute("BEGIN IMMEDIATE")
        try:
            watermark = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM correspondence_occasion_attempts").fetchone()[0]
            conn.execute("INSERT INTO correspondence_occasion_owner_acts (act_id, inbound_event_id, action, "
                         "requester_actor_id, attempt_watermark, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (act_id, inbound_event_id, action, requester_actor_id, watermark, occurred_at, int(time.time())))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"act_id": act_id}
    finally:
        conn.close()


# ----------------------------------------------------------------- outbound lifecycle

def outbound_lifecycle(conn, send_turn_event_id):
    """{"withdrawn_by_clark", "closed_by_owner", "host_resume_failures"} for one authored send."""
    state = {"withdrawn_by_clark": False, "closed_by_owner": False, "host_resume_failures": 0}
    if not _present(conn):
        return state
    for action, in conn.execute(
            "SELECT action FROM correspondence_outbound_lifecycle_events WHERE send_turn_event_id = ?",
            (send_turn_event_id,)).fetchall():
        if action == "withdrawn_by_clark":
            state["withdrawn_by_clark"] = True
        elif action == "closed_by_owner":
            state["closed_by_owner"] = True
        else:
            state["host_resume_failures"] += 1
    return state


def delivery_authority_ended(state):
    return (state["withdrawn_by_clark"] or state["closed_by_owner"]
            or state["host_resume_failures"] >= MAX_HOST_RESUME_FAILURES)


def record_outbound_lifecycle(data_dir, *, send_turn_event_id, action, requester_actor_id, occurred_at,
                              evidence_event_id=None, detail=None):
    if action == "closed_by_owner":
        pass
    elif action == "withdrawn_by_clark":
        requester_actor_id = cs1_schema_migration.CLARK_ACTOR_ID   # the trigger proves the canonical choice
    elif action != "host_resume_failed":
        raise CorrespondenceError("unknown outbound lifecycle action")
    conn = _connect(data_dir)
    try:
        if action == "closed_by_owner":
            _require_owner(conn, requester_actor_id)
        event_id = _new_id("csout-")
        _insert(conn, "INSERT INTO correspondence_outbound_lifecycle_events (lifecycle_event_id, send_turn_event_id, "
                      "action, requester_actor_id, evidence_event_id, detail, occurred_at, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, send_turn_event_id, action, requester_actor_id, evidence_event_id,
                 (detail or "")[:500] or None, occurred_at, int(time.time())))
        return {"lifecycle_event_id": event_id}
    finally:
        conn.close()


# ----------------------------------------------------------------- canonical keys on events

def _governing_turn(conn, event_id):
    """(turn_event_id, correspondent_ref) for an occasion turn itself or for an inbound event carried
    by one; (None, None) otherwise.  Read ONLY from canonical host-authored components."""
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, CORRESPONDENT_SOURCE_REF_COMPONENT_KIND)).fetchone()
    if row is not None:
        return event_id, row[0]
    carrier = conn.execute(
        "SELECT car.event_id, ref.component_text FROM event_components car "
        "JOIN event_components ref ON ref.event_id = car.event_id AND ref.component_kind = ? "
        "WHERE car.component_kind = 'discord_inbound_carriage' AND car.component_text = ? "
        "ORDER BY car.rowid LIMIT 1",
        (CORRESPONDENT_SOURCE_REF_COMPONENT_KIND, event_id)).fetchone()
    return (carrier[0], carrier[1]) if carrier else (None, None)


def correspondent_key_of_event(conn, event_id):
    """The correspondent source ref an event is governed by, or None."""
    return _governing_turn(conn, event_id)[1]


def correspondent_event_receivable(conn, event_id, viewer_key):
    """Scoped continuity law for a correspondent session: an event is receivable iff it is governed
    by exactly this correspondent key, the key has ESTABLISHED standing now, and its governing turn
    is the establishing turn or later (continuity binds prospectively from the exchange in which
    Clark chose it; a revoke/re-establish gap stays a gap)."""
    parsed = parse_source_ref(viewer_key)
    turn, key = _governing_turn(conn, event_id)
    if parsed is None or key != viewer_key:
        return False
    current = standing(conn, *parsed)
    if current["state"] != STANDING_ESTABLISHED:
        return False
    establishing_turn = current["history"][-1]["waking_turn_event_id"]
    rows = dict(conn.execute(
        "SELECT event_id, rowid FROM events WHERE event_id IN (?, ?)", (turn, establishing_turn)).fetchall())
    return turn in rows and establishing_turn in rows and rows[turn] >= rows[establishing_turn]
