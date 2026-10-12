"""Owner administration of general correspondence: doors, safety blocks, and the durable lifecycle.

GUI-independent, like caret_correspondent_admin.  `load_view` is read-only (no model, no network, no
write).  Only explicit owner acts write, and only through the owner-gated append-only ledgers in
correspondence_surfaces.  The owner controls DOORS (whether an authorized surface admits sources who
are not ANAXI principals, whether Clark may write first there), hard safety BLOCKS, and administrative
CLOSURE of stuck correspondence.  The owner never sets Clark's standing: it is shown read-only.
Content-free: ids, states and reasons only -- never message text.  Never shown to Clark.
"""
import sqlite3
import time

import canonical_person
import correspondence_surfaces as cs
import discord_correspondence as dc


def _ro(data_dir):
    try:
        return sqlite3.connect(f"file:{data_dir}/anaxi_provenance.db?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def door_policy(data_dir, destination_id):
    """(allow_unknown_sources, permit_independent_initiation) as durably recorded for one destination;
    (False, False) for none/unknown.  Read-only.  The door checkboxes show THIS, never a stale default
    (live 2026-09-24: input-only checkboxes reset to unchecked on a page load while the door stayed open,
    so pressing "Set door" would have closed it)."""
    if not destination_id:
        return False, False
    conn = _ro(data_dir)
    if conn is None:
        return False, False
    try:
        policy = cs.surface_policy(conn, destination_id)
    finally:
        conn.close()
    return bool(policy.get("allow_unknown_sources")), bool(policy.get("permit_independent_initiation"))


def load_view(data_dir):
    """{"markdown", "destinations": [(label, id)], "occasions": [(label, id)], "pending_sends": [(label, id)]}"""
    destinations = dc.list_authorized_destinations(data_dir)
    conn = _ro(data_dir)
    lines = ["**Correspondence doors** (owner policy per authorized destination)"]
    try:
        for d in destinations:
            policy = cs.surface_policy(conn, d["destination_id"]) if conn else {}
            lines.append(
                f"- {d.get('display_label') or d['destination_id']} (`{d['destination_id']}`): "
                + ("open to correspondents who are not ANAXI principals" if policy.get("allow_unknown_sources")
                   else "mapped ANAXI principals only")
                + ("; Clark may write first to correspondents he has standing with"
                   if policy.get("permit_independent_initiation") else ""))
        if not destinations:
            lines.append("- No authorized destination.")
        correspondents = dc.known_correspondents(data_dir)
        lines.append("\n**Correspondents who are not ANAXI principals** (stable id; name unverified; "
                     "standing is Clark's own choice, shown read-only)")
        for c in correspondents:
            lines.append(f"- `{c['ref']}` ({c.get('display_name') or 'no name'}): standing {c['standing']}"
                         + ("; BLOCKED by owner (safety boundary)" if c["blocked"] else ""))
        if not correspondents:
            lines.append("- None encountered.")
        held = dc.held_inbound(data_dir)
        occasions = [(f"{h['discord_message_id']} — {h['reason']}", h["event_id"]) for h in held]
        pending = dc.pending_authored_sends(data_dir)
        lines.append("\n**Stuck or held correspondence** (host / owner facts -- never Clark's silence)")
        for h in held:
            lines.append(f"- inbound `{h['discord_message_id']}` from `{h['discord_author_id']}`: {h['reason_text']}")
        for p in pending:
            lines.append(f"- Clark's authored send `{p['send_turn_event_id']}` to {p['destination_label']}: "
                         "authored; delivery not yet attempted (authorship established, delivery not established)")
        if not held and not pending:
            lines.append("- Nothing held or pending.")
    finally:
        if conn is not None:
            conn.close()
    return {"markdown": "\n".join(lines),
            "destinations": [(d.get("display_label") or d["destination_id"], d["destination_id"]) for d in destinations],
            "occasions": occasions,
            "pending_sends": [(f"{p['send_turn_event_id']} → {p['destination_label']}", p["send_turn_event_id"])
                              for p in pending]}


def set_door(data_dir, *, requester_actor_id, destination_id, allow_unknown_sources, permit_independent_initiation):
    try:
        cs.set_surface_policy(data_dir, destination_id=destination_id, allow_unknown_sources=bool(allow_unknown_sources),
                              permit_independent_initiation=bool(permit_independent_initiation),
                              requester_actor_id=requester_actor_id, occurred_at=int(time.time()),
                              destination_authorized=dc.is_destination_authorized(data_dir, destination_id))
    except cs.CorrespondenceError as exc:
        return False, f"Policy not changed: {exc}"
    return True, "Policy recorded (prospective: it applies to messages arriving from now on)."


def set_block(data_dir, *, requester_actor_id, discord_user_id, blocked):
    try:
        cs.set_source_block(data_dir, kind=cs.SOURCE_KIND_DISCORD_USER, value=(discord_user_id or "").strip(),
                            blocked=bool(blocked), requester_actor_id=requester_actor_id, occurred_at=int(time.time()))
    except cs.CorrespondenceError as exc:
        return False, f"Not changed: {exc}"
    return True, ("Blocked" if blocked else "Unblocked") + f" Discord id {discord_user_id} (standing untouched)."


def occasion_act(data_dir, *, requester_actor_id, inbound_event_id, action):
    try:
        cs.owner_occasion_act(data_dir, inbound_event_id=inbound_event_id, action=action,
                              requester_actor_id=requester_actor_id, occurred_at=int(time.time()))
    except cs.CorrespondenceError as exc:
        return False, f"Not recorded: {exc}"
    return True, f"Owner {action} recorded (the message itself is never deleted)."


def close_pending_send(data_dir, *, requester_actor_id, send_turn_event_id):
    try:
        cs.record_outbound_lifecycle(data_dir, send_turn_event_id=send_turn_event_id, action="closed_by_owner",
                                     requester_actor_id=requester_actor_id, occurred_at=int(time.time()))
    except cs.CorrespondenceError as exc:
        return False, f"Not closed: {exc}"
    return True, "Closed by owner: it will not be delivered (Clark's authorship stays on record)."


def authorize_channel(data_dir, *, requester_actor_id, discord_channel_id, display_label):
    """Owner act: authorize one Discord channel as a correspondence destination (prospective: only messages
    posted after this moment are ever received).  Its door starts closed -- mapped principals only."""
    channel = (discord_channel_id or "").strip()
    if not channel.isdigit():
        return False, "Not authorized: the channel id must be digits (Discord: right-click the channel > Copy Channel ID)."
    try:
        dc.authorize_destination(data_dir, destination_kind="channel", discord_snowflake=channel,
                                 display_label=(display_label or "").strip(), requester_actor_id=requester_actor_id,
                                 occurred_at=int(time.time()))
    except dc.DiscordCorrespondenceError as exc:
        return False, f"Not authorized: {exc}"
    return True, "Channel authorized (door closed: mapped ANAXI principals only until you open it)."


def revoke_destination(data_dir, *, requester_actor_id, destination_id):
    try:
        dc.revoke_destination(data_dir, destination_id=destination_id, requester_actor_id=requester_actor_id,
                              occurred_at=int(time.time()))
    except dc.DiscordCorrespondenceError as exc:
        return False, f"Not revoked: {exc}"
    return True, "Destination revoked: nothing further is received from or sent to it."


def record_identity(data_dir, *, requester_actor_id, discord_user_id, name, entity_kind="human"):
    """Owner act (Step 1): record who a stable Discord id belongs to -- a canonical person with that id bound.
    Identity only: it grants no standing, principal role, visibility or authority, and it never rewrites the
    past (earlier exchanges are re-contextualized at read time, never edited)."""
    user_id = (discord_user_id or "").strip()
    kind = canonical_person.ENTITY_AI_DIGITAL if entity_kind == "ai_digital" else canonical_person.ENTITY_HUMAN
    now = int(time.time())
    try:
        person = canonical_person.create_person(data_dir, label=(name or "").strip(), entity_kind=kind,
                                                requester_actor_id=requester_actor_id, occurred_at=now)
        canonical_person.bind_identifier(data_dir, person_id=person["person_id"],
                                         identifier_kind=canonical_person.IDENTIFIER_DISCORD_AUTHOR,
                                         identifier_value=user_id, requester_actor_id=requester_actor_id,
                                         occurred_at=now)
    except canonical_person.CanonicalPersonError as exc:
        return False, f"Not recorded: {exc}"
    return True, (f"Recorded Discord id {user_id} as {name.strip()!r} (identity only: no standing, "
                  "role or authority; earlier history is re-read in this light, never rewritten).")
