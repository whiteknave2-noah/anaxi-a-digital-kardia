"""OWC7-S2: minimal human directional-control surface support.

Two things live here, both deliberately host-side-only:

1. resolve_canonical_human_actor_id() -- mechanical resolution of the
   canonical human actor from the existing actors registry (read-only
   DB lookup, keyed on actor_type='human_person', never a hardcoded
   ID, never inferred from dialogue/display_label). This project has
   no multi-user login/session system -- "authentication" here means
   the value passed to conversation_direction.apply_human_direction_
   control() is always freshly, mechanically resolved from the
   canonical actors registry, never hardcoded, and never reachable
   from any Clark/model-output code path. Fails closed (raises
   CanonicalHumanActorResolutionError) if zero or more than one
   actor_type='human_person' row exists -- ambiguity is never guessed.

2. record_direction_control_trace()/query_direction_control_trace() --
   an append-only JSONL trace, structurally identical in spirit to
   conversation_direction_trace.py but for a completely different
   event class: explicit human directional-control actions (button
   clicks), not typed Clark turns. OPERATIONAL CONTROL TELEMETRY ONLY.
   This is explicitly NOT autobiographical memory, dialogue, a
   journal, Kardia input, hippocampal input, or Sleep/REM input --
   nothing in this module is ever inserted into a messages list, read
   by any Clark/model-facing code path, or fed into memory/context
   pathways of any kind.

Pure/read-only-DB module: no model call, no Kardia/hippocampus/journal
import or interaction anywhere.
"""
import datetime
import json
import os
import uuid

import family_membership

DEFAULT_TRACE_PATH = "direction_control_trace.jsonl"

TAKE_DIRECTION = "take_direction"
GIVE_DIRECTION_TO_CLARK = "give_direction_to_clark"
OPEN_DIRECTION = "open_direction"
VALID_CONTROL_ACTS = {TAKE_DIRECTION, GIVE_DIRECTION_TO_CLARK, OPEN_DIRECTION}


class CanonicalHumanActorResolutionError(Exception):
    """Raised when the canonical human actor cannot be mechanically
    resolved from the actors registry -- zero or more than one
    actor_type='human_person' row found. Fails closed; never guesses,
    never falls back to a hardcoded or display-label-derived value."""
    pass


def resolve_canonical_human_actor_id(conn):
    """Read-only. Queries the canonical actors registry for the single
    actor_type='human_person' row and returns its actor_id. Never
    hardcoded, never inferred from dialogue or display_label -- conn
    is the only source of truth. Raises CanonicalHumanActorResolutionError
    if zero or more than one such row exists.

    FS1: once the family feature is in use (a designated owner exists),
    that designated owner -- a canonical designer-named principal from
    family_principal_events -- IS the canonical human actor. This is
    the multi-human ownership model: several registered humans exist
    and the owner is the single authoritative principal. While the
    feature is unused (no family tables, or an empty ledger -- the
    pre-FS1 database shape every existing test builds), the legacy
    exactly-one-human rule applies byte-identically."""
    owner = family_membership.designated_owner_actor_id(conn)
    if owner is not None:
        return owner
    rows = conn.execute("SELECT actor_id FROM actors WHERE actor_type = 'human_person'").fetchall()
    if len(rows) != 1:
        raise CanonicalHumanActorResolutionError(
            f"expected exactly one actor_type='human_person' row in the canonical "
            f"actors registry, found {len(rows)}"
        )
    return rows[0][0]


def _new_trace_id():
    return f"dctrace-{uuid.uuid4().hex[:16]}"


def record_direction_control_trace(
    *,
    session_id,
    source_actor_id,
    control_act,
    direction_owner_before,
    direction_owner_after,
    status,
    trace_path=DEFAULT_TRACE_PATH,
):
    """Appends exactly one durable control-trace record. Never
    overwrites or truncates -- opened in 'a' mode exclusively. For a
    failed resolution/authorization attempt, `source_actor_id`/
    `direction_owner_after` may legitimately be None -- callers must
    never fabricate an unknown actor/state value; None means
    genuinely unknown, not a guess. Returns the written record (a
    defensive copy), never re-read from disk."""
    record = {
        "trace_id": _new_trace_id(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
        "source_actor_id": source_actor_id,
        "control_act": control_act,
        "direction_owner_before": direction_owner_before,
        "direction_owner_after": direction_owner_after,
        "status": status,
    }
    parent = os.path.dirname(os.path.abspath(trace_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(trace_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return dict(record)


def query_direction_control_trace(trace_path=DEFAULT_TRACE_PATH):
    """Read-only. Never modifies the trace file. Returns [] if the
    file does not exist yet."""
    if not os.path.exists(trace_path):
        return []
    records = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records
