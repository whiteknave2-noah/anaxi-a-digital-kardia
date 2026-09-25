"""OWC6-O1/OWC7-S1: durable operational/diagnostic trace binding a
released conversation-mode turn to its prospective typed
conversational-direction control act and directional-ownership
control state.

CONTROL / DIAGNOSTIC TELEMETRY ONLY. This is explicitly NOT:
autobiographical memory, canonical belief, Kardia, hippocampal memory,
journal content, hidden monologue, or chain of thought. It is
read-only evidence for operators and later provenance design, and is
never fed into hippocampal indexing, Kardia, memory retrieval, the
journal, Sleep/REM, or ordinary dialogue context -- nothing in this
module is ever inserted into a messages list or shown to the model.

Deliberately excludes Pass-1's free-form `content` field. OWC7-S1
removed `content` from the active control schema entirely (Pass 1
returns control decisions, not free-form reasoning) -- there is no
longer any free-form Pass-1 text for this module to be tempted to
persist. Only the closed-enum `act` (raw and validated), the short
`thread` label, the four mechanical OWC5 working-set fields, and the
OWC7-S1 directional-control fields (direction_owner_before/after,
direction_request, relinquish_direction) are persisted -- see
extract_raw_act() below for the one function that ever touches a raw
Pass-1 payload, which is written specifically to never return
`content` even from a malformed/adversarial payload (kept even though
`content` no longer exists in the schema, as a structural guarantee
against a future adversarial/malformed payload smuggling one back in).

Old trace rows (written before OWC7-S1) simply lack the four
directional-control fields -- never backfilled, never inferred.
Missing fields on an old row mean exactly: predates directional-
ownership tracking.

Append-only JSONL, mirroring workspace_capability.py's
_log_action()/query_action_log() convention (itself mirroring
signal_observation_log.py before it) -- the established pattern for
narrow, host-authored operational evidence that is durable but
explicitly not canonical provenance. Pure I/O module: no model call,
no Kardia/hippocampus/journal import or interaction anywhere.
"""
import datetime
import json
import os
import uuid

DEFAULT_TRACE_PATH = "conversation_direction_trace.jsonl"


def extract_raw_act(raw_pass1_output):
    """Pure. Returns the raw `act` value from a Pass-1 output (whatever
    type it happens to be -- a malformed payload might not even have a
    string there), or None if the payload could not be parsed as a
    JSON object at all. Deliberately extracts ONLY `act` -- never
    returns `content` or `thread` from the raw payload, so a
    malformed/adversarial payload can never leak free-form reasoning
    into the durable trace through this function."""
    if isinstance(raw_pass1_output, str):
        try:
            raw = json.loads(raw_pass1_output)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_pass1_output, dict):
        raw = raw_pass1_output
    else:
        return None
    if not isinstance(raw, dict):
        return None
    return raw.get("act")


def _new_trace_id():
    return f"cdtrace-{uuid.uuid4().hex[:16]}"


def record_trace(
    *,
    session_id,
    raw_act,
    validated_act,
    thread,
    pass1_status,
    working_set_before,
    working_set_after,
    direction_owner_before,
    direction_owner_after,
    direction_request,
    relinquish_direction,
    pass2_status=None,
    event_id=None,
    staging_id=None,
    retry_count=0,
    fallback_used=False,
    trace_path=DEFAULT_TRACE_PATH,
):
    """Appends exactly one durable trace record. Never overwrites or
    truncates -- opened in 'a' mode exclusively. `working_set_before`/
    `working_set_after` are expected to already be the plain OWC5
    working-set dicts -- copied here verbatim, never augmented with
    any inferred field. `direction_owner_before`/`direction_owner_
    after`/`direction_request`/`relinquish_direction` are the OWC7-S1
    directional-control fields -- required (not optional) since every
    conversation-mode Pass-1 turn now always produces them, unlike
    `event_id`/`staging_id`, which remain optional/None until
    persistence for this turn has actually succeeded (callers must
    never guess or backfill those). Returns the written record (a
    defensive copy), never re-read from disk."""
    record = {
        "trace_id": _new_trace_id(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
        "raw_act": raw_act,
        "validated_act": validated_act,
        "thread": thread,
        "pass1_status": pass1_status,
        "working_set_before": dict(working_set_before) if working_set_before is not None else None,
        "working_set_after": dict(working_set_after) if working_set_after is not None else None,
        "direction_owner_before": direction_owner_before,
        "direction_owner_after": direction_owner_after,
        "direction_request": direction_request,
        "relinquish_direction": relinquish_direction,
        "pass2_status": pass2_status,
        "event_id": event_id,
        "staging_id": staging_id,
        "retry_count": retry_count,
        "fallback_used": fallback_used,
    }
    parent = os.path.dirname(os.path.abspath(trace_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(trace_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return dict(record)


def query_trace(trace_path=DEFAULT_TRACE_PATH):
    """Read-only. Never modifies the trace file. Returns [] if the
    file does not exist yet (mirrors workspace_capability.
    query_action_log()'s own convention)."""
    if not os.path.exists(trace_path):
        return []
    records = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records
