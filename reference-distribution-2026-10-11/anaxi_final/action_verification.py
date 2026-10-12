"""Read-only verification of mechanically knowable action completion.

This module is deliberately a projection *function*, not a projection store.
It reads the authorities ANAXI already has:

* exact workspace side-effect receipts in ``workspace_action_log.jsonl``;
* Clark-authored Discord action components on canonical waking turns; and
* the existing outward projection-attempt receipt ledger.

It never searches resource names fuzzily, reads note/message content, performs
an action, retries an action, or writes any record.  The returned dict is a
bounded mechanical observation.  Clark remains responsible for interpreting
and wording it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

import discord_correspondence_registry as dcr
import epistemic_status
from provenance_schema import derive_stable_id


SCHEMA = "anaxi.action_verification.v0"

REQUEST_NONE = "none"
WORKSPACE_APPEND = "workspace_append"
DISCORD_SEND = "discord_send"
REQUEST_KINDS = frozenset({REQUEST_NONE, WORKSPACE_APPEND, DISCORD_SEND})

COMPLETED = "COMPLETED"
NOT_COMPLETED = "NOT_COMPLETED"
UNKNOWN = "UNKNOWN"

QUERY_COMPONENT_KIND = "action_verification_query"
RESULT_COMPONENT_KIND = "action_verification_result"

MAX_TARGET_CHARS = 200
MESSAGE_EXCERPT_CHARS = 80
_UNSAFE_NOTE_NAME = re.compile(r"[\x00-\x1f<>:\"|?*]")


class VerificationQueryError(ValueError):
    pass


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_CANONICAL_SUFFIXES = {"notes:": ".md", "journal:": ".json"}


def make_query(action_kind, exact_target, *, canonical=False):
    """Validate and canonicalize Clark's typed query (no outcome field).

    ``canonical=True`` re-validates an already canonical target (verify_action): its typed length is
    measured without the .md/.json suffix this function itself appends, so a lawful typed target at the
    full MAX_TARGET_CHARS stays lawful once canonicalized (whole-occasion capacity, 2026-10-08)."""
    if action_kind not in (WORKSPACE_APPEND, DISCORD_SEND):
        raise VerificationQueryError("unknown action verification kind")
    if not isinstance(exact_target, str) or not exact_target.strip():
        raise VerificationQueryError("action verification target must be nonblank text")
    target = exact_target.strip()
    typed_length = len(target)
    if canonical and action_kind == WORKSPACE_APPEND:
        suffix = next((x for p, x in _CANONICAL_SUFFIXES.items() if target.startswith(p)), None)
        if suffix and target.endswith(suffix):
            typed_length -= len(suffix)
    if typed_length > MAX_TARGET_CHARS:
        raise VerificationQueryError("action verification target is too long")
    if action_kind == WORKSPACE_APPEND:
        if target.startswith("notes:"):
            target = target[6:].replace("\\", "/")
            if (not target or target.startswith("/") or ".." in target.split("/")
                    or _UNSAFE_NOTE_NAME.search(target)):
                raise VerificationQueryError("workspace note target is not an exact lawful note name")
            if not target.lower().endswith(".md"):
                target += ".md"
            target_kind = "workspace_note_path"
        elif target.startswith("journal:"):
            target = target[8:]
            if not target or "/" in target or "\\" in target or ".." in target or any(
                    ord(ch) < 32 for ch in target):
                raise VerificationQueryError("journal target is not an exact lawful entry id")
            if not target.lower().endswith(".json"):
                target += ".json"
            target_kind = "workspace_journal_entry_path"
        else:
            raise VerificationQueryError("workspace append target requires notes: or journal: identity")
    else:
        if any(ord(ch) < 32 for ch in target):
            raise VerificationQueryError("Discord verification target contains control characters")
        target_kind = "waking_turn_event_id" if target.startswith("turn:") else "discord_destination_id"
        if target_kind == "waking_turn_event_id":
            target = target[5:]
            if not target:
                raise VerificationQueryError("turn: requires an event id")
    return {
        "schema": SCHEMA,
        "action_kind": action_kind,
        "target": {"identity_kind": target_kind, "exact": target},
        "scope": "complete_recorded_history",
    }


def _result(query, completion, mechanical_outcome, *, scope_complete, provenance=(), detail=None):
    value = {
        "schema": SCHEMA,
        "query": query,
        "completion": completion,
        "mechanical_outcome": mechanical_outcome,
        "scope_complete": bool(scope_complete),
        "provenance": list(provenance),
    }
    if detail is not None:
        value["detail"] = detail
    return value


def _valid_workspace_record(record):
    required = ("action_id", "timestamp", "requester_actor_id", "resource_class", "action", "result")
    return (
        isinstance(record, dict)
        and all(isinstance(record.get(key), str) and record[key] for key in required)
        and record["result"] in {"performed", "denied", "resolved", "stopped_by_subject", "unresolved"}
        and (record.get("relative_path") is None or isinstance(record.get("relative_path"), str))
    )


def _read_workspace_receipts(action_log_path):
    """Return a complete parsed ledger or None.  Never treats unavailable or
    malformed history as evidence that an action did not happen."""
    try:
        with open(action_log_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    records, seen = [], set()
    try:
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            if not _valid_workspace_record(record) or record["action_id"] in seen:
                return None
            seen.add(record["action_id"])
            records.append(record)
    except (TypeError, ValueError):
        return None
    return records


def _verify_workspace_action(query, action_log_path):
    records = _read_workspace_receipts(action_log_path)
    if records is None:
        return _result(
            query, UNKNOWN, "scope_unavailable", scope_complete=False,
            detail="workspace receipt history is unavailable or incomplete",
        )
    exact = query["target"]["exact"]
    if query["target"]["identity_kind"] == "workspace_note_path":
        resource_class, action = "notes", "append"
    else:
        resource_class, action = "journal", "append"
    matches = [
        row for row in records
        if row["resource_class"] == resource_class and row["action"] == action
        and row.get("relative_path") == exact
    ]
    provenance = [
        {
            "source": "workspace_action_receipt",
            "receipt_id": row["action_id"],
            "timestamp": row["timestamp"],
            "receipt_outcome": row["result"],
            "exact_relative_path": row.get("relative_path"),
        }
        for row in matches
    ]
    performed = [row for row in matches if row["result"] == "performed"]
    if performed:
        return _result(query, COMPLETED, "performed", scope_complete=True, provenance=provenance)
    if any(row["result"] in {"unresolved", "resolved"} for row in matches):
        # ``unresolved`` is expressly not negative evidence.  ``resolved`` is
        # a read-side outcome and is not a lawful append completion receipt;
        # if it appears on this exact write identity, fail closed as well.
        return _result(
            query, UNKNOWN, "outcome_not_established", scope_complete=True,
            provenance=provenance,
        )
    if matches:
        return _result(query, NOT_COMPLETED, "failed", scope_complete=True, provenance=provenance)
    return _result(query, NOT_COMPLETED, "no_matching_receipt", scope_complete=True)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _discord_actions(conn):
    """Return integrity-checked canonical Discord choices, or None if the
    supposedly complete canonical scope contains an incomplete/corrupt choice."""
    kinds = (
        dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND,
        dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND,
        dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND,
    )
    rows = conn.execute(
        "SELECT e.event_id, e.occurred_at, c.component_kind, c.component_text, "
        "c.creator_actor_id, c.content_sha256 FROM events e JOIN event_components c "
        "ON c.event_id = e.event_id WHERE e.event_type = 'waking_turn' "
        "AND c.component_kind IN (?, ?, ?) ORDER BY e.rowid, c.sequence",
        kinds,
    ).fetchall()
    grouped = {}
    for event_id, occurred_at, kind, text, creator, digest in rows:
        item = grouped.setdefault(event_id, {"event_id": event_id, "occurred_at": occurred_at, "parts": {}})
        if kind in item["parts"]:
            return None
        item["parts"][kind] = (text, creator, digest)
    clark = derive_stable_id("actor", "clark")
    actions = []
    for item in grouped.values():
        parts = item["parts"]
        if set(parts) != set(kinds):
            return None
        for text, creator, digest in parts.values():
            if creator != clark or digest != hashlib.sha256(text.encode("utf-8")).hexdigest():
                return None
        request = parts[dcr.DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND][0]
        if request not in {"send_message", "reply_through_caret", "draft_message"}:
            return None
        actions.append({
            "event_id": item["event_id"],
            "occurred_at": item["occurred_at"],
            "request": request,
            "destination_id": parts[dcr.DISCORD_DESTINATION_ID_COMPONENT_KIND][0],
            "text": parts[dcr.DISCORD_MESSAGE_TEXT_COMPONENT_KIND][0],
        })
    return actions


def _outward_is_bound(conn, outward_event_id, waking_turn_event_id):
    row = conn.execute(
        "SELECT event_type, input_source_ref FROM events WHERE event_id = ?", (outward_event_id,)
    ).fetchone()
    return row == ("clark_outward_act", waking_turn_event_id)


def _projection_state(conn, outward_event_id, waking_turn_event_id):
    # A multipart plan has its own authoritative per-part receipt ledger.
    part_rows = conn.execute(
        "SELECT p.part_index, a.part_attempt_id, a.status FROM discord_outbound_parts p "
        "LEFT JOIN discord_outbound_part_attempts a ON a.outward_event_id = p.outward_event_id "
        "AND a.part_index = p.part_index WHERE p.outward_event_id = ? "
        "ORDER BY p.part_index, a.attempt_sequence",
        (outward_event_id,),
    ).fetchall()
    if part_rows:
        if not _outward_is_bound(conn, outward_event_id, waking_turn_event_id):
            return None
        latest = {}
        receipt_ids = []
        for index, receipt_id, status in part_rows:
            if status is not None:
                latest[index] = status
                receipt_ids.append(receipt_id)
        indices = {row[0] for row in part_rows}
        part_count = len(indices)
        if indices != set(range(1, part_count + 1)):
            return None
        statuses = [latest.get(index) for index in range(1, part_count + 1)]
        if statuses and all(status == "succeeded" for status in statuses):
            state = dcr.DISPATCH_CONFIRMED_SENT
        elif any(status == "not_established" for status in statuses):
            state = dcr.DISPATCH_OUTCOME_NOT_ESTABLISHED
        elif any(status == "attempted" for status in statuses):
            state = dcr.DISPATCH_STARTED
        elif any(status == "failed" for status in statuses) and any(
                status == "succeeded" for status in statuses):
            state = dcr.DISPATCH_MULTIPART_PARTIALLY_SENT
        elif any(status == "failed" for status in statuses):
            state = dcr.DISPATCH_FAILED_BEFORE_DISPATCH
        elif any(status == "succeeded" for status in statuses):
            state = dcr.DISPATCH_MULTIPART_PARTIALLY_SENT
        else:
            state = dcr.DISPATCH_MULTIPART_PENDING
        return {"state": state, "receipt_ids": receipt_ids}

    rows = conn.execute(
        "SELECT projection_attempt_id, status FROM outward_projection_attempts "
        "WHERE outward_event_id = ? ORDER BY attempt_sequence",
        (outward_event_id,),
    ).fetchall()
    if not rows:
        return {"state": dcr.DISPATCH_AUTHORIZED_READY, "receipt_ids": []}
    if not _outward_is_bound(conn, outward_event_id, waking_turn_event_id):
        return None
    latest = rows[-1][1]
    state = {
        "attempted": dcr.DISPATCH_STARTED,
        "succeeded": dcr.DISPATCH_CONFIRMED_SENT,
        "failed": dcr.DISPATCH_FAILED_BEFORE_DISPATCH,
        "not_established": dcr.DISPATCH_OUTCOME_NOT_ESTABLISHED,
    }.get(latest)
    return {"state": state, "receipt_ids": [row[0] for row in rows]}


def _verify_discord(query, data_dir):
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return _result(query, UNKNOWN, "scope_unavailable", scope_complete=False)
    try:
        required = {
            "events", "event_components", "outward_projection_attempts",
            "discord_outbound_parts", "discord_outbound_part_attempts",
        }
        if not all(_table_exists(conn, table) for table in required):
            return _result(query, UNKNOWN, "scope_unavailable", scope_complete=False)
        actions = _discord_actions(conn)
        if actions is None:
            return _result(query, UNKNOWN, "canonical_action_scope_incomplete", scope_complete=False)
        target = query["target"]
        if target["identity_kind"] == "waking_turn_event_id":
            matches = [row for row in actions if row["event_id"] == target["exact"]]
        else:
            matches = [row for row in actions if row["destination_id"] == target["exact"]]
        provenance = [
            {
                "source": "canonical_waking_action",
                "waking_turn_event_id": row["event_id"],
                "occurred_at": row["occurred_at"],
                "typed_request": row["request"],
                "exact_destination_id": row["destination_id"],
                # Clark's own canonical outbound words (integrity-checked above), bounded: the identity
                # by which he can tell his own actions apart.  Sandbox finding: an ambiguous result that
                # listed only turn ids was a dead end -- no way to bind either id to the message meant.
                "clark_text_excerpt": row["text"][:MESSAGE_EXCERPT_CHARS],
            }
            for row in matches
        ]
        if not matches:
            return _result(query, NOT_COMPLETED, "no_matching_canonical_action", scope_complete=True)
        if len(matches) != 1:
            for item, row in zip(provenance, matches):
                if row["request"] == "draft_message":
                    item["dispatch_state"] = "draft"
                    continue
                state = _projection_state(conn, derive_stable_id("discord_outward_act", row["event_id"]), row["event_id"])
                item["dispatch_state"] = state["state"] if state else "receipt_binding_invalid"
            return _result(
                query, UNKNOWN, "ambiguous_exact_identity", scope_complete=True,
                provenance=provenance,
            )
        action = matches[0]
        if action["request"] == "draft_message":
            return _result(query, NOT_COMPLETED, "drafted_not_sent", scope_complete=True, provenance=provenance)
        outward_event_id = derive_stable_id("discord_outward_act", action["event_id"])
        projection = _projection_state(conn, outward_event_id, action["event_id"])
        if projection is None:
            return _result(
                query, UNKNOWN, "canonical_receipt_binding_invalid", scope_complete=False,
                provenance=provenance,
            )
        state = projection["state"]
        provenance.append({"source": "outward_projection_receipts", "outward_event_id": outward_event_id,
                           "dispatch_state": state, "receipt_ids": projection["receipt_ids"]})
        if state == dcr.DISPATCH_CONFIRMED_SENT:
            return _result(query, COMPLETED, "confirmed_sent", scope_complete=True, provenance=provenance)
        if state in (dcr.DISPATCH_AUTHORIZED_READY, dcr.DISPATCH_FAILED_BEFORE_DISPATCH):
            outcome = "selected_only" if state == dcr.DISPATCH_AUTHORIZED_READY else "failed_before_dispatch"
            return _result(query, NOT_COMPLETED, outcome, scope_complete=True, provenance=provenance)
        if state == dcr.DISPATCH_MULTIPART_PARTIALLY_SENT:
            return _result(
                query, NOT_COMPLETED, "multipart_partially_sent", scope_complete=True,
                provenance=provenance,
            )
        if state == dcr.DISPATCH_MULTIPART_PENDING:
            return _result(
                query, NOT_COMPLETED, "multipart_pending", scope_complete=True,
                provenance=provenance,
            )
        return _result(query, UNKNOWN, "outcome_not_established", scope_complete=True, provenance=provenance)
    except sqlite3.Error:
        return _result(query, UNKNOWN, "canonical_scope_unreadable", scope_complete=False)
    finally:
        conn.close()


def verify_action(data_dir, query, *, workspace_action_log_path):
    """Purely read-only authority lookup.  ``query`` is revalidated so a
    caller cannot smuggle a claimed outcome or alternate scope into it."""
    if not isinstance(query, dict):
        raise VerificationQueryError("verification query must be an object")
    target = query.get("target") or {}
    prefixes = {
        "waking_turn_event_id": "turn:",
        "workspace_note_path": "notes:",
        "workspace_journal_entry_path": "journal:",
    }
    rebuilt = make_query(
        query.get("action_kind"),
        prefixes.get(target.get("identity_kind"), "") + target.get("exact", ""),
        canonical=True,
    )
    if query != rebuilt:
        raise VerificationQueryError("verification query is not canonical")
    if query["action_kind"] == WORKSPACE_APPEND:
        return _verify_workspace_action(query, workspace_action_log_path)
    return _verify_discord(query, data_dir)


# What each mechanical outcome says about the RECORDS (never about what Clark did,
# meant, or should conclude).  EPISTEMIC RECOVERABILITY SANDBOX real-model finding:
# carried as raw JSON, the receipt was echoed back verbatim as the reply, imitated
# into a counterfeit result ("completion":"YES" for a real UNKNOWN), or ignored.
OUTCOME_RECORD_TEXT = {
    "performed": "a receipt records this exact action as performed",
    "failed": "the receipts for this exact action record only an unsuccessful attempt",
    "no_matching_receipt": "no receipt exists for this exact identity",
    "outcome_not_established": "a record of this action exists, but its outcome was never established",
    "scope_unavailable": "the records could not be read completely, so neither completion nor non-completion is established",
    "canonical_action_scope_incomplete": "the action records are incomplete, so neither completion nor non-completion is established",
    "canonical_scope_unreadable": "the action records could not be read, so neither completion nor non-completion is established",
    "canonical_receipt_binding_invalid": "the action's dispatch receipts could not be bound to it, so its outcome is not established",
    "no_matching_canonical_action": "no recorded send or draft exists for this exact identity",
    "ambiguous_exact_identity": "more than one recorded action matches this identity (each is listed below with its turn id, which can be checked on its own)",
    "drafted_not_sent": "the recorded action is a draft, and drafts are not sent",
    "confirmed_sent": "dispatch receipts confirm this send",
    "selected_only": "the send was chosen, but no dispatch receipt exists",
    "failed_before_dispatch": "dispatch receipts record a failure before sending",
    "multipart_partially_sent": "only some parts of this send are confirmed",
    "multipart_pending": "parts of this send are still pending",
}
# Per-candidate dispatch states (ambiguous results) in the same record wording as OUTCOME_RECORD_TEXT.
_DISPATCH_STATE_TEXT = {
    dcr.DISPATCH_CONFIRMED_SENT: "dispatch receipts confirm this send",
    dcr.DISPATCH_AUTHORIZED_READY: "chosen, but no dispatch receipt exists",
    dcr.DISPATCH_FAILED_BEFORE_DISPATCH: "dispatch receipts record a failure before sending",
    dcr.DISPATCH_OUTCOME_NOT_ESTABLISHED: "attempted, but its outcome was never established",
    dcr.DISPATCH_STARTED: "attempt begun, no outcome recorded",
    dcr.DISPATCH_MULTIPART_PENDING: "parts still pending",
    dcr.DISPATCH_MULTIPART_PARTIALLY_SENT: "only some parts confirmed",
    "draft": "a draft, and drafts are not sent",
    "receipt_binding_invalid": "its dispatch receipts could not be bound to it",
}
_KIND_TEXT = {
    "workspace_note_path": "note creation", "workspace_journal_entry_path": "journal append",
    "discord_destination_id": "Discord send to destination", "waking_turn_event_id": "Discord send in turn",
}


def _provenance_line(item):
    source = item.get("source")
    if source == "workspace_action_receipt":
        return (f"receipt {item['receipt_id']} ({item['timestamp']}): {item['receipt_outcome']} "
                f"for {item['exact_relative_path']}")
    if source == "canonical_waking_action":
        import datetime
        when = datetime.datetime.fromtimestamp(item["occurred_at"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        line = (f"turn {item['waking_turn_event_id']} ({when}): {item['typed_request']} "
                f"to {item['exact_destination_id']}")
        if item.get("clark_text_excerpt"):
            line += f", your words: \"{item['clark_text_excerpt']}\""
        if item.get("dispatch_state"):
            line += f"; record: {_DISPATCH_STATE_TEXT.get(item['dispatch_state'], item['dispatch_state'])}"
        return line
    if source == "outward_projection_receipts":
        return f"dispatch receipts {', '.join(item['receipt_ids']) or 'none'}: {item['dispatch_state']}"
    return canonical_json(item)


def exact_current_presence(query, *, notes_dir, journal_dir):
    """Read-only, exact-path present-state fact for a workspace verification
    target: does an item exist at EXACTLY this identity now?  No search, no
    similar names, no content.  None for Discord queries or when the
    collection is unavailable.  Presence is a separate fact from the receipt:
    it never changes the canonical completion result (a file may exist that
    Clark did not write, or a receipted write may since have been removed)."""
    target = query["target"]
    base = {"workspace_note_path": notes_dir, "workspace_journal_entry_path": journal_dir}.get(target["identity_kind"])
    if not base or not os.path.isdir(base):
        return None
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(root, target["exact"]))
    if os.path.commonpath([root, path]) != root:
        return None
    return {"exact": target["exact"], "exists_now": os.path.isfile(path)}


def render_result(result, current_presence=None):
    """Bounded mechanical carriage of the receipt: what was looked up, the
    mechanical status, what the records say, the exact records, and scope.
    No host-written conclusion about what Clark did; interpretation is his."""
    query = result["query"]["target"]
    lines = [
        "Result of the verify_action you chose this turn (host-delivered lookup of canonical action receipts; "
        "read-only, it changed nothing; not the human's words):",
        f"looked up: {_KIND_TEXT.get(query['identity_kind'], query['identity_kind'])} \"{query['exact']}\"",
    ]
    if current_presence is not None:
        what = "note" if query["identity_kind"] == "workspace_note_path" else "journal entry"
        lines.append("present state (exact name only, separate from the receipts): "
                     + ("a" if current_presence["exists_now"] else "no")
                     + f" {what} exists at exactly \"{current_presence['exact']}\" now")
    lines.append(f"receipt status: {result['completion']} ({result['mechanical_outcome']}): "
                 + OUTCOME_RECORD_TEXT.get(result["mechanical_outcome"], result["mechanical_outcome"].replace("_", " ")))
    # CONTENT provenance: these are host mechanical records of the receiving actor's own
    # actions, so the relation is HOST.  Stated explicitly so the receipt's authority is
    # never inferred from the envelope's prose.
    lines.append(f"actor relation of these records: {epistemic_status.HOST}")
    lines.extend("  " + _provenance_line(item) for item in result.get("provenance") or [])
    lines.append("receipt scope: " + ("complete recorded history" if result["scope_complete"] else "incomplete"))
    return "\n".join(lines)
