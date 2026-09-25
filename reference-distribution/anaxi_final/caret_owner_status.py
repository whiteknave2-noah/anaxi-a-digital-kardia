"""Owner-side Caret correspondence status: a read-only derivation, never a record.

Every fact is read from the canonical provenance DB that already holds it:

  received / staged   the `discord_inbound_message` event
  delivered           its `discord_inbound_delivered` marker, naming the committed waking turn
                      that carried it (`discord_inbound_carriage`)
  reply selected      that waking turn's `discord_correspondence_request` component
  dispatch result     OC0's `outward_projection_attempts` for the turn's outward act
                      (the same rows `discord_correspondence.outbound_dispatch_state` reads); a reply
                      longer than one Discord message is transported as 2-3 parts whose per-part
                      ledger (`discord_outbound_part_attempts`) is summarized read-only

Author admission is derived too: a message from a Discord author id that does not resolve, now, to a
currently valid canonical principal is reported as "not delivered" with its raw author id and the
non-authoritative username -- never its words.

Only two facts are not canonical, and both come from the existing content-free
`caret_wake_trace.jsonl` (an owner diagnostic, never fed to Clark): that a wake was attempted /
failed for a not-yet-delivered message, and that a reply route was chosen but its body was
unusable (nothing sent).

Observability is not interpretation.  "No outbound reply selected" means only that the committed
waking turn carries no Discord send/route action.  It says nothing about why, nor about what
Clark understood, weighed, wanted or endorsed.  This module writes nothing, calls no model and no
network, and its output is never placed in Clark's conversation.
"""
import json
import os
import sqlite3

import discord_author_mapping as dam
import discord_correspondence as dc
import discord_correspondence_registry as dcr

DEFAULT_LIMIT = 3
_TRACE_TAIL_BYTES = 1_000_000

RECEIVED_NOT_WOKEN = "RECEIVED_WAKING_NOT_ATTEMPTED"
WAKING_ATTEMPTED = "WAKING_ATTEMPTED_COMPLETION_NOT_RECORDED"
WAKING_FAILED = "WAKING_FAILED"
DESTINATION_NOT_AUTHORIZED = "STAGED_DESTINATION_NOT_AUTHORIZED"
AUTHOR_NOT_DELIVERABLE = "STAGED_AUTHOR_NOT_DELIVERABLE"
NO_OUTBOUND_REPLY_SELECTED = "DELIVERED_NO_OUTBOUND_REPLY_SELECTED"
# LAWFUL NULL: the only state that is Clark's silence -- his canonical typed no_reply choice.
CLARK_CHOSE_NO_REPLY = "DELIVERED_CLARK_CHOSE_NO_REPLY"
# Durable inbound lifecycle (correspondence_surfaces): host / owner facts, never Clark's silence.
RETRY_EXHAUSTED = "EXPRESSION_NOT_ESTABLISHED_RETRY_EXHAUSTED"
CLOSED_BY_OWNER = "CLOSED_BY_OWNER_NOT_DELIVERED"
SOURCE_BLOCKED = "STAGED_SOURCE_BLOCKED_BY_OWNER"
REPLY_BODY_UNUSABLE = "DELIVERED_REPLY_ROUTE_BODY_UNUSABLE_NOTHING_SENT"
REPLY_DISPATCH_NOT_RECORDED = "DELIVERED_REPLY_SELECTED_DISPATCH_NOT_RECORDED"
REPLY_DISPATCH_STARTED = "DELIVERED_REPLY_SELECTED_DISPATCH_STARTED"
REPLY_SENT = "DELIVERED_REPLY_SELECTED_CONFIRMED_SENT"
REPLY_SEND_FAILED = "DELIVERED_REPLY_SELECTED_SEND_FAILED"
REPLY_NOT_AUTHORIZED = "DELIVERED_REPLY_SELECTED_NOT_AUTHORIZED"
REPLY_OUTCOME_NOT_ESTABLISHED = "OUTCOME_NOT_ESTABLISHED"
REPLY_TRANSPORT_CAP_EXCEEDED = "DELIVERED_REPLY_ROUTE_BODY_EXCEEDS_TRANSPORT_CAP_NOTHING_SENT"
# One canonical reply transported as 2-3 consecutive Discord messages (derived from the per-part ledger).
MP_PENDING = "MULTIPART_PENDING"
MP_IN_PROGRESS = "MULTIPART_DISPATCH_STARTED"
MP_PARTIAL = "MULTIPART_PARTIALLY_SENT"
MP_SENT = "MULTIPART_CONFIRMED_COMPLETE"
MP_FAILED = "MULTIPART_FAILED"
MP_OUTCOME_NOT_ESTABLISHED = "MULTIPART_OUTCOME_NOT_ESTABLISHED"
WAKING_RECORD_UNREADABLE = "OUTCOME_NOT_ESTABLISHED_WAKING_RECORD_UNREADABLE"

_SUMMARY = {
    RECEIVED_NOT_WOKEN: "Received / staged → waking not yet attempted",
    WAKING_ATTEMPTED: "Received / staged → waking attempted; completion not recorded",
    WAKING_FAILED: "Received / staged → waking attempt failed (message remains pending)",
    DESTINATION_NOT_AUTHORIZED: "Received / staged → destination no longer authorized; will not wake",
    AUTHOR_NOT_DELIVERABLE: "Incoming Caret message from an author who is not a valid mapped principal; not delivered",
    NO_OUTBOUND_REPLY_SELECTED: "Delivered → waking completed → no outbound reply selected",
    CLARK_CHOSE_NO_REPLY: "Delivered → waking completed → Clark chose not to reply (his own typed choice)",
    RETRY_EXHAUSTED: "Received / staged → waking failed repeatedly; host retry exhausted (not delivered; not Clark's choice)",
    CLOSED_BY_OWNER: "Received / staged → administratively closed by the owner (not delivered; not Clark's choice)",
    SOURCE_BLOCKED: "Received / staged → author is a source the owner blocked (safety boundary); not delivered",
    REPLY_BODY_UNUSABLE: "Delivered → waking completed → reply route chosen, body unusable; nothing sent",
    REPLY_DISPATCH_NOT_RECORDED: "Delivered → waking completed → outbound reply selected → dispatch not recorded",
    REPLY_DISPATCH_STARTED: "Delivered → waking completed → outbound reply selected → dispatch started; outcome not yet recorded",
    REPLY_SENT: "Delivered → waking completed → outbound reply selected → sent (confirmed)",
    REPLY_SEND_FAILED: "Delivered → waking completed → outbound reply selected → send failed; not sent",
    REPLY_NOT_AUTHORIZED: "Delivered → waking completed → outbound reply selected → not sent (destination not authorized)",
    REPLY_OUTCOME_NOT_ESTABLISHED: "Delivered → waking completed → outbound reply selected → OUTCOME_NOT_ESTABLISHED",
    REPLY_TRANSPORT_CAP_EXCEEDED: "Delivered → waking completed → reply route chosen, body exceeds 6000-char transport cap; nothing sent",
    WAKING_RECORD_UNREADABLE: "Delivered marker present → waking record not readable → OUTCOME_NOT_ESTABLISHED",
}



# Grammatical owner-facing lead per held reason (HELD_REASON_TEXT is a noun phrase for other surfaces).
_HELD_LEAD = {
    dam.STATUS_UNMAPPED: "Incoming Caret message from an unmapped Discord author",
    dam.STATUS_REVOKED: "Incoming Caret message from a Discord author whose mapping was revoked",
    dam.STATUS_AMBIGUOUS: "Incoming Caret message from a Discord author with an ambiguous mapping",
    dam.STATUS_PRINCIPAL_INACTIVE: "Incoming Caret message from a Discord author whose mapped principal is no longer active",
    dam.STATUS_BEFORE_MAPPING: "Incoming Caret message that arrived before its author was mapped (mapping is prospective)",
}

def _ro(data_dir):
    path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _read_trace(trace_path):
    """Per inbound event id, its content-free trace records in order.  Unreadable => {}."""
    by_id = {}
    try:
        with open(trace_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TRACE_TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return by_id
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event_id"):
            by_id.setdefault(record["event_id"], []).append(record)
    return by_id


def _multipart_code(summary):
    n, k, bad = summary["part_count"], summary["confirmed_parts"], summary["unresolved_part"]
    bad_status = next((p["status"] for p in summary["parts"] if p["index"] == bad), None)
    lead = "Delivered → waking completed → outbound reply selected → "
    state = summary["state"]
    if state == dcr.DISPATCH_CONFIRMED_SENT:
        return MP_SENT, f"{lead}{n}-part send confirmed"
    if state == dcr.DISPATCH_OUTCOME_NOT_ESTABLISHED:
        return MP_OUTCOME_NOT_ESTABLISHED, (
            f"{lead}multipart outcome not established: {k}/{n} sent, part {bad} OUTCOME_NOT_ESTABLISHED")
    if state == dcr.DISPATCH_STARTED:
        return MP_IN_PROGRESS, f"{lead}multipart dispatch started: {k}/{n} confirmed; part {bad} outcome not yet recorded"
    if state == dcr.DISPATCH_MULTIPART_PARTIALLY_SENT:
        if bad is not None:
            return MP_PARTIAL, f"{lead}multipart partially sent: {k}/{n} sent, part {bad} {bad_status}"
        return MP_PARTIAL, f"{lead}multipart partially sent: {k}/{n} sent, remaining parts pending"
    if state == dcr.DISPATCH_MULTIPART_PENDING:
        return MP_PENDING, f"{lead}multipart pending: {n} parts planned, none sent yet"
    return MP_FAILED, f"{lead}multipart failed: 0/{n} sent, part {bad} {bad_status or 'failed'}; nothing else sent"


def _dispatch_code(conn, waking_turn_event_id):
    """(code, summary_override_or_None)"""
    outward_id = dc.derive_stable_id("discord_outward_act", waking_turn_event_id)
    if conn.execute("SELECT 1 FROM events WHERE event_id = ?", (outward_id,)).fetchone() is None:
        return REPLY_DISPATCH_NOT_RECORDED, None
    multipart = dc._multipart_summary(conn, outward_id)
    if multipart is not None:
        return _multipart_code(multipart)
    row = conn.execute(
        "SELECT status, detail FROM outward_projection_attempts WHERE outward_event_id = ? "
        "ORDER BY attempt_sequence DESC LIMIT 1", (outward_id,),
    ).fetchone()
    if row is None:
        return REPLY_DISPATCH_NOT_RECORDED, None
    status, detail = row
    if status == "succeeded":
        return REPLY_SENT, None
    if status == "attempted":
        return REPLY_DISPATCH_STARTED, None
    if status == "not_established":
        return REPLY_OUTCOME_NOT_ESTABLISHED, None
    if detail == "not_authorized_at_dispatch" or (detail or "").startswith("destination was revoked"):
        return REPLY_NOT_AUTHORIZED, None
    return REPLY_SEND_FAILED, None


def _delivered_code(conn, waking_turn_event_id, trace_records):
    turn = conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (waking_turn_event_id,)).fetchone()
    if turn is None or turn[0] != "waking_turn":
        return WAKING_RECORD_UNREADABLE, None
    request = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (waking_turn_event_id, dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND),
    ).fetchone()
    if request is not None and request[0] not in ("none", ""):
        return _dispatch_code(conn, waking_turn_event_id)
    null_choice = conn.execute(
        "SELECT 1 FROM event_components c JOIN actors a ON a.actor_id = c.creator_actor_id "
        "WHERE c.event_id = ? AND c.component_kind = 'clark_reply_choice' AND c.component_text = 'no_reply' "
        "AND a.actor_type = 'clark_agent'",
        (waking_turn_event_id,),
    ).fetchone()
    if null_choice is not None:
        return CLARK_CHOSE_NO_REPLY, None
    intent = conn.execute(
        "SELECT 1 FROM event_components WHERE event_id = ? AND component_kind = ?",
        (waking_turn_event_id, dcr.CARET_REPLY_ROUTE_INTENT_COMPONENT_KIND),
    ).fetchone()
    if intent is not None:   # canonical: route chosen (new turns); no send components => body unusable
        for r in trace_records:
            if r.get("event") == "wake_result" and r.get("caret_reply_reason") == "reply_body_exceeds_transport_cap":
                return REPLY_TRANSPORT_CAP_EXCEEDED, None
        return REPLY_BODY_UNUSABLE, None
    for r in trace_records:
        if r.get("event") == "wake_result" and r.get("caret_reply_status") == "not_dispatched":
            if r.get("caret_reply_reason") == "reply_body_exceeds_transport_cap":
                return REPLY_TRANSPORT_CAP_EXCEEDED, None
            return REPLY_BODY_UNUSABLE, None
    return NO_OUTBOUND_REPLY_SELECTED, None


def _undelivered_code(data_dir, destination_id, trace_records, conn=None, author_id=None, message_id=None,
                      inbound_event_id=None):
    if destination_id and dc.resolve_destination(data_dir, destination_id) is None:
        return DESTINATION_NOT_AUTHORIZED, {}
    if conn is not None and inbound_event_id is not None:
        held = dc._occasion_held_reason(conn, inbound_event_id)
        if held == dc.HELD_CLOSED_BY_OWNER:
            return CLOSED_BY_OWNER, {}
        if held == dc.HELD_RETRY_EXHAUSTED:
            return RETRY_EXHAUSTED, {}
    if conn is not None:
        destination = dc._reconstruct_registry(conn).get(destination_id) if destination_id else None
        if destination is not None:
            correspondent, held = dc._deliverable_correspondent(
                conn, {"author_id": author_id, "discord_message_id": message_id}, destination)
            if correspondent is None:
                if held == dc.HELD_SOURCE_BLOCKED:
                    return SOURCE_BLOCKED, {}
                return AUTHOR_NOT_DELIVERABLE, {"author_status": held}
        else:
            author_status = dam.resolve_delivery(conn, author_id, message_id)["status"]
            if author_status != dam.STATUS_MAPPED:
                return AUTHOR_NOT_DELIVERABLE, {"author_status": author_status}
    lifecycle = [r for r in trace_records
                 if r.get("event") in ("wake_attempted", "wake_failed", "wake_result", "wake_skipped")]
    last = lifecycle[-1] if lifecycle else None
    if last is None:
        return RECEIVED_NOT_WOKEN, {}
    if last["event"] == "wake_failed":
        return WAKING_FAILED, {"attempt": last.get("attempt"), "held": bool(last.get("held"))}
    if last["event"] == "wake_attempted":
        return WAKING_ATTEMPTED, {}
    return RECEIVED_NOT_WOKEN, {}


def caret_owner_status(data_dir, *, trace_path=None, limit=DEFAULT_LIMIT):
    """Newest-first mechanical status of the latest `limit` staged inbound Caret messages.
    Read-only; returns [] when nothing is staged or the DB/tables are absent."""
    conn = _ro(data_dir)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT e.event_id, e.occurred_at, m.component_text, d.component_text "
            "FROM events e "
            "LEFT JOIN event_components m ON m.event_id = e.event_id AND m.component_kind = ? "
            "LEFT JOIN event_components d ON d.event_id = e.event_id AND d.component_kind = ? "
            "WHERE e.event_type = ? ORDER BY e.rowid DESC LIMIT ?",
            (dcr.DISCORD_INBOUND_METADATA_COMPONENT_KIND, dcr.DISCORD_INBOUND_DELIVERED_COMPONENT_KIND,
             dcr.DISCORD_INBOUND_MESSAGE_EVENT_TYPE, max(1, int(limit))),
        ).fetchall()
        trace = _read_trace(trace_path) if trace_path else {}
        out = []
        for event_id, occurred_at, metadata_text, delivered_turn in rows:
            try:
                metadata = json.loads(metadata_text) if metadata_text else {}
            except (TypeError, ValueError):
                metadata = {}
            records = trace.get(event_id, [])
            detail = {}
            override = None
            if delivered_turn:
                code, override = _delivered_code(conn, delivered_turn, records)
            else:
                code, detail = _undelivered_code(
                    data_dir, metadata.get("destination_id"), records, conn, metadata.get("author_id"),
                    metadata.get("discord_message_id"), inbound_event_id=event_id)
            summary = override or _SUMMARY[code]
            if code == AUTHOR_NOT_DELIVERABLE:
                lead = _HELD_LEAD.get(detail.get("author_status")) or (
                    "Incoming Caret message that arrived before the owner opened this channel to unknown sources"
                    if detail.get("author_status") == dc.HELD_NOT_ADMITTED else _HELD_LEAD[dam.STATUS_UNMAPPED])
                summary = (f"{lead} (Discord id {metadata.get('author_id')}, username "
                           f"{metadata.get('author_name')!r} unverified); not delivered to Clark")
            if code == WAKING_FAILED:
                summary += (f"; attempt {detail.get('attempt')}, "
                            + ("held for this process" if detail.get("held") else "will retry"))
            out.append({
                "inbound_event_id": event_id, "discord_message_id": metadata.get("discord_message_id"),
                "occurred_at": occurred_at, "code": code, "summary": summary,
                "waking_turn_event_id": delivered_turn,
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def render_owner_status(statuses):
    """(accordion_label, markdown).  Deliberately quiet: one collapsed line, ids only."""
    if not statuses:
        return "Caret correspondence — none staged", "No staged Caret messages."
    label = f"Caret correspondence — latest: {statuses[0]['summary']}"
    lines = [f"- `{s['discord_message_id'] or s['inbound_event_id']}` — {s['summary']}" for s in statuses]
    lines.append("\n*Mechanical record only; it does not describe why Clark did or did not send anything.*")
    return label, "\n".join(lines)
