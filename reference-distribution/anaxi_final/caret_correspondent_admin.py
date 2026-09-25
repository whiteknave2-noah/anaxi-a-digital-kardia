"""Owner administration of Caret correspondents: which Discord author id is which ANAXI principal.

A thin, GUI-independent layer over discord_author_mapping.  Rendering (`load_view`) is read-only: it
opens the provenance DB read-only, calls no model and makes no network request.  Only the explicit
owner acts (`map_author`, `revoke_author`) write, and only through the owner-gated append-only ledger.
Clark never administers mappings and is never shown this panel.

The owner can see, per Discord author id: the raw id, the current username as NON-authoritative
metadata, the mapped canonical principal, and the active/revoked state; plus, content-free, any
authorized-channel messages held because their author is not (or no longer) deliverable -- so the
owner can learn an id WITHOUT that message ever being delivered to Clark.
"""
import time

import discord_author_mapping as dam
import discord_correspondence as dc

_STATE_TEXT = {
    dam.STATUS_MAPPED: "active",
    dam.STATUS_REVOKED: "revoked",
    dam.STATUS_AMBIGUOUS: "AMBIGUOUS (fails closed)",
    dam.STATUS_PRINCIPAL_INACTIVE: "principal no longer active (fails closed)",
    dam.STATUS_UNMAPPED: "unmapped",
}


def load_view(data_dir):
    """Read-only. Returns {"markdown", "principals": [(label, id)], "active_author_ids": [...]}."""
    mappings = dam.list_mappings(data_dir)
    held = dc.held_inbound(data_dir)
    principals = [(f"{label} ({role})", pid) for pid, label, role in dam.assignable_principals(data_dir)]
    lines = ["**Discord author → ANAXI principal** (username is display metadata only; identity is the id)"]
    if mappings:
        for m in mappings:
            lines.append(
                f"- `{m['discord_author_id']}` ({m['discord_username_snapshot'] or 'no username recorded'}) → "
                f"{m['display_label'] or m['principal_actor_id']} — {_STATE_TEXT.get(m['status'], m['status'])}"
            )
    else:
        lines.append("- No Discord author is mapped; no Caret message can wake Clark yet.")
    if held:
        lines.append("\n**Not delivered (author not a valid mapped principal):**")
        seen = set()
        for h in held:
            key = (h["discord_author_id"], h["reason"])
            if key in seen:
                continue
            seen.add(key)
            count = sum(1 for x in held if (x["discord_author_id"], x["reason"]) == key)
            lines.append(
                f"- Discord id `{h['discord_author_id']}` (username {h['discord_username']!r}, unverified): "
                f"{h['reason_text']}; {count} message(s) held, not delivered"
                + ("; mapping is prospective, so these are never delivered" if h["reason"] == dam.STATUS_BEFORE_MAPPING
                   else "")
            )
    return {
        "markdown": "\n".join(lines),
        "principals": principals,
        "active_author_ids": [m["discord_author_id"] for m in mappings if m["status"] == dam.STATUS_MAPPED],
    }


def map_author(data_dir, *, requester_actor_id, discord_author_id, principal_actor_id, display_label=None):
    """Explicit owner act. Returns (ok, message). The username snapshot is taken from the most recent
    staged message by that author id, when one exists -- retained as non-authoritative metadata only."""
    author_id = (discord_author_id or "").strip()
    try:
        dam.map_author(
            data_dir, discord_author_id=author_id, principal_actor_id=principal_actor_id,
            requester_actor_id=requester_actor_id, occurred_at=int(time.time()),
            display_label=display_label, discord_username=_last_username(data_dir, author_id),
        )
    except dam.AuthorMappingError as exc:
        return False, f"Not mapped: {exc}"
    return True, f"Mapped Discord id {author_id}."


def revoke_author(data_dir, *, requester_actor_id, discord_author_id):
    try:
        dam.revoke_author(
            data_dir, discord_author_id=(discord_author_id or "").strip(),
            requester_actor_id=requester_actor_id, occurred_at=int(time.time()),
        )
    except dam.AuthorMappingError as exc:
        return False, f"Not revoked: {exc}"
    return True, f"Revoked Discord id {discord_author_id}."


def _last_username(data_dir, author_id):
    try:
        for held in reversed(dc.held_inbound(data_dir)):
            if held["discord_author_id"] == author_id and held["discord_username"]:
                return held["discord_username"]
    except Exception:
        pass
    return None
