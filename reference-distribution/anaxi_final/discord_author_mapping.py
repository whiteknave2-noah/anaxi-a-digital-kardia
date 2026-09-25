"""Caret correspondent identity: stable Discord author id -> EXISTING canonical ANAXI principal.

Two separate authority axes:

  DESTINATION   "where may Caret correspondence occur?"    (discord_destination_events)
  CORRESPONDENT "who is this person?"                       (discord_author_mapping_events)

A valid destination alone never establishes who wrote a message.  A Discord author id is bound,
by an explicit owner administrative act, to a principal that ALREADY exists under the accepted
Family / Shared law (family_membership).  This module invents no identity system: it stores only the
owner's binding, and at every delivery it re-derives, from canonical state, whether that binding still
names a currently-valid principal.

  * key            the stable Discord author id (never a username / display name)
  * value          an existing canonical human_person principal
  * administration owner-gated (the canonical owner is the only lawful requester), append-only ledger
  * username       a non-authoritative snapshot kept for the owner's eyes; never used to resolve
  * validity       family feature used  -> principal must be in family_membership.active_family_principals
                   family feature never used -> principal must be the legacy owner (resolve_owner_actor_id)
                   deactivated / revoked / stale / ambiguous -> fails closed
  * prospective    a binding authorizes only messages that ARRIVE (Discord snowflake time) after the moment the
                   binding was durably recorded (host clock, never a caller-supplied occurrence time), exactly
                   like a destination authorization.  A message from a then-unmapped author is unauthorized at
                   arrival and stays so forever: mapping the author later never makes it deliverable, and its
                   canonical event is neither deleted nor reclassified.  The later-established identity is
                   available to a reader only as an explicitly later-dated fact (see
                   discord_correspondence.historical_inbound_attribution).
  * inference      none: no content, username similarity, biography or prior conversation is consulted

The ledger is reduced as a state machine: `map` adds a principal to an author's active set, `revoke`
removes it.  Exactly one active principal is a valid mapping; more than one is AMBIGUOUS and resolves
to nothing.  The writer refuses to create such a state; the reader still fails closed if it ever
appears.  Pure read helpers take an open connection (read-only is fine) and write nothing.
"""
import re
import secrets
import sqlite3
import time

import dc0_schema_migration
import family_membership

_SNOWFLAKE_RE = re.compile(r"\A[0-9]{5,32}\Z")
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_LABEL_LENGTH = 200

ACTION_MAP = "map"
ACTION_REVOKE = "revoke"

STATUS_MAPPED = "mapped"
STATUS_UNMAPPED = "unmapped"
STATUS_REVOKED = "revoked"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_PRINCIPAL_INACTIVE = "principal_inactive"
STATUS_BEFORE_MAPPING = "before_mapping"     # author is mapped NOW, but this message arrived before the binding

ROLE_OWNER = "owner"
ROLE_FAMILY_MEMBER = "family_member"

HELD_REASON_TEXT = {
    STATUS_UNMAPPED: "unmapped Discord author",
    STATUS_REVOKED: "Discord author mapping revoked",
    STATUS_AMBIGUOUS: "Discord author mapping ambiguous",
    STATUS_PRINCIPAL_INACTIVE: "mapped principal no longer active",
    STATUS_BEFORE_MAPPING: "arrived before its author was mapped (unauthorized at arrival; never delivered)",
}

_DISCORD_EPOCH_MILLISECONDS = 1420070400000


def baseline_snowflake(recorded_at):
    """Largest Discord snowflake that can predate `recorded_at` (whole second, conservative: messages
    in the binding's own second are skipped rather than importing pre-binding history).  None when the
    time cannot bound a snowflake (no boundary)."""
    if type(recorded_at) is not int:
        return None
    end_of_second_ms = (recorded_at + 1) * 1000 - 1
    if end_of_second_ms < _DISCORD_EPOCH_MILLISECONDS:
        return None
    return str(((end_of_second_ms - _DISCORD_EPOCH_MILLISECONDS) << 22) | ((1 << 22) - 1))


def message_time_seconds(message_id):
    """Discord message creation time (unix seconds) encoded in its snowflake, or None."""
    if not is_valid_author_id(message_id):
        return None
    return ((int(message_id) >> 22) + _DISCORD_EPOCH_MILLISECONDS) // 1000


class AuthorMappingError(Exception):
    """A mapping administrative act cannot be carried out.  Never caught silently."""


def _new_id():
    ts_ms = int(time.time() * 1000)
    raw = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _table_present(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discord_author_mapping_events'"
    ).fetchone() is not None


def is_valid_author_id(author_id):
    return isinstance(author_id, str) and _SNOWFLAKE_RE.match(author_id) is not None


# ------------------------------------------------------------------ principal validity


def principal_is_currently_valid(conn, principal_actor_id):
    """Re-derived from canonical state on every call; never cached.  A valid principal is an
    ALREADY-registered human that is presently an active family principal (family mode) or the
    sole legacy owner (family feature never used)."""
    if not isinstance(principal_actor_id, str) or not principal_actor_id:
        return False
    registered = conn.execute(
        "SELECT 1 FROM actors a JOIN actor_human_person h ON h.actor_id = a.actor_id "
        "WHERE a.actor_id = ? AND a.actor_type = 'human_person'", (principal_actor_id,),
    ).fetchone()
    if registered is None:
        return False
    if family_membership.family_feature_used(conn):
        return principal_actor_id in family_membership.active_family_principals(conn)
    return family_membership.resolve_owner_actor_id(conn) == principal_actor_id


def principal_role(conn, principal_actor_id):
    owner = family_membership.resolve_owner_actor_id(conn)
    return ROLE_OWNER if owner is not None and owner == principal_actor_id else ROLE_FAMILY_MEMBER


def principal_display_label(conn, principal_actor_id, fallback=None):
    """The owner-approved label from the canonical family ledger when one exists, otherwise the
    label the owner supplied when binding."""
    if family_membership.family_feature_used(conn):
        row = conn.execute(
            "SELECT display_label FROM family_principal_events WHERE group_key = ? "
            "AND principal_actor_id = ? ORDER BY rowid DESC LIMIT 1",
            (family_membership.GROUP_KEY_FAMILY, principal_actor_id),
        ).fetchone()
        if row is not None and isinstance(row[0], str) and row[0].strip():
            return row[0].strip()
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


# ------------------------------------------------------------------ pure read side


def _ledger_rows(conn, author_id):
    if not _table_present(conn):
        return []
    return conn.execute(
        "SELECT mapping_event_id, action, principal_actor_id, principal_display_label, "
        "discord_username_snapshot, created_at FROM discord_author_mapping_events "
        "WHERE discord_author_id = ? ORDER BY rowid", (author_id,),
    ).fetchall()


def _reduce(rows):
    active = {}
    ever_mapped = False
    for event_id, action, principal, label, _username, recorded_at in rows:
        if action == ACTION_MAP:
            ever_mapped = True
            active[principal] = (event_id, label, recorded_at)
        elif action == ACTION_REVOKE:
            active.pop(principal, None)
    return active, ever_mapped


def resolve_author(conn, author_id):
    """Fresh, read-only resolution of one Discord author id.

    Returns {"status", "discord_author_id", ...}.  Only STATUS_MAPPED carries a principal and is
    deliverable; every other status fails closed."""
    result = {"status": STATUS_UNMAPPED, "discord_author_id": author_id}
    if not is_valid_author_id(author_id):
        return result
    active, ever_mapped = _reduce(_ledger_rows(conn, author_id))
    if not active:
        result["status"] = STATUS_REVOKED if ever_mapped else STATUS_UNMAPPED
        return result
    if len(active) > 1:
        result["status"] = STATUS_AMBIGUOUS
        return result
    (principal, (mapping_event_id, mapped_label, recorded_at)), = active.items()
    result["principal_actor_id"] = principal
    result["mapping_event_id"] = mapping_event_id
    result["effective_at"] = recorded_at
    result["baseline_snowflake"] = baseline_snowflake(recorded_at)
    if not principal_is_currently_valid(conn, principal):
        result["status"] = STATUS_PRINCIPAL_INACTIVE
        return result
    result["status"] = STATUS_MAPPED
    result["role"] = principal_role(conn, principal)
    result["display_label"] = principal_display_label(conn, principal, mapped_label) or "(unlabeled principal)"
    return result


def resolve_delivery(conn, author_id, message_id):
    """Author resolution FOR ONE MESSAGE: the author must resolve to a currently valid principal AND the
    message must have arrived after the binding was recorded.  A message that arrived earlier reports
    STATUS_BEFORE_MAPPING and is never deliverable (no backfill), however the author is mapped later."""
    resolved = resolve_author(conn, author_id)
    if resolved["status"] != STATUS_MAPPED:
        return resolved
    baseline = resolved.get("baseline_snowflake")
    if baseline is not None and not (
        is_valid_author_id(message_id) and int(message_id) > int(baseline)
    ):
        return dict(resolved, status=STATUS_BEFORE_MAPPING)
    return resolved


def correspondent_for_delivery(conn, author_id, message_id):
    """The correspondent facts Clark may be told, or None when this message is not deliverable."""
    resolved = resolve_delivery(conn, author_id, message_id)
    if resolved["status"] != STATUS_MAPPED:
        return None
    return {
        "principal_actor_id": resolved["principal_actor_id"],
        "display_label": resolved["display_label"],
        "role": resolved["role"],
        "mapping_event_id": resolved["mapping_event_id"],
        "mapping_effective_at": resolved["effective_at"],
        "discord_author_id": author_id,
    }


def binding_history(conn, author_id):
    """Read-only, truthful ledger history for one author id: every map/revoke with its host-recorded
    time, oldest first.  Later-established identity is read from HERE and always carries its own time."""
    if not _table_present(conn):
        return []
    return [
        {"mapping_event_id": r[0], "action": r[1], "principal_actor_id": r[2], "display_label": r[3],
         "recorded_at": r[5]}
        for r in _ledger_rows(conn, author_id)
    ]


# ------------------------------------------------------------------ owner administration


def _connect(data_dir):
    conn = sqlite3.connect(f"{data_dir}/anaxi_provenance.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    dc0_schema_migration.apply_migration_on_connection(conn)
    return conn


def _require_owner(conn, requester_actor_id):
    owner = family_membership.resolve_owner_actor_id(conn)
    if owner is None:
        raise AuthorMappingError("no canonical owner is resolvable; author mapping is impossible")
    if requester_actor_id != owner:
        raise AuthorMappingError(
            "requester is not the canonical owner -- only the owner may bind or revoke a Discord author"
        )


def _clean_label(label):
    if label is None:
        return None
    if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
        raise AuthorMappingError("display label must be text of at most 200 characters")
    return label.strip() or None


def _clean_username(username):
    return username.strip()[:100] if isinstance(username, str) and username.strip() else None


def map_author(data_dir, *, discord_author_id, principal_actor_id, requester_actor_id, occurred_at,
               display_label=None, discord_username=None):
    """Owner-gated: bind ONE Discord author id to ONE existing, currently-valid principal.  Refuses
    a malformed id, an unregistered/inactive principal, and an author that is already actively
    mapped (revoke first: a Discord id can never silently move or resolve to two principals)."""
    if not is_valid_author_id(discord_author_id):
        raise AuthorMappingError("discord_author_id must be a Discord snowflake (digits only)")
    if type(occurred_at) is not int:
        raise AuthorMappingError("occurred_at must be an integer second timestamp")
    label = _clean_label(display_label)
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        if not principal_is_currently_valid(conn, principal_actor_id):
            raise AuthorMappingError(
                "principal is not an already-registered human that is currently active under "
                "the family/owner law; nothing is created automatically"
            )
        active, _ever = _reduce(_ledger_rows(conn, discord_author_id))
        if active:
            raise AuthorMappingError(
                "this Discord author id is already actively mapped; revoke it first"
            )
        event_id = _new_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO discord_author_mapping_events (mapping_event_id, discord_author_id, action, "
                "principal_actor_id, principal_display_label, discord_username_snapshot, "
                "requester_actor_id, occurred_at, created_at) VALUES (?, ?, 'map', ?, ?, ?, ?, ?, ?)",
                (event_id, discord_author_id, principal_actor_id, label,
                 _clean_username(discord_username), requester_actor_id, occurred_at, int(time.time())),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"mapping_event_id": event_id, "discord_author_id": discord_author_id,
            "principal_actor_id": principal_actor_id}


def revoke_author(data_dir, *, discord_author_id, requester_actor_id, occurred_at):
    """Owner-gated: end the author's current binding.  A revoked author fails closed at once."""
    if not is_valid_author_id(discord_author_id):
        raise AuthorMappingError("discord_author_id must be a Discord snowflake (digits only)")
    if type(occurred_at) is not int:
        raise AuthorMappingError("occurred_at must be an integer second timestamp")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        active, _ever = _reduce(_ledger_rows(conn, discord_author_id))
        if not active:
            raise AuthorMappingError("this Discord author id has no active mapping to revoke")
        event_id = _new_id()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for principal in sorted(active):   # normally exactly one; an ambiguous state is cleared whole
                conn.execute(
                    "INSERT INTO discord_author_mapping_events (mapping_event_id, discord_author_id, "
                    "action, principal_actor_id, principal_display_label, discord_username_snapshot, "
                    "requester_actor_id, occurred_at, created_at) VALUES (?, ?, 'revoke', ?, NULL, NULL, ?, ?, ?)",
                    (event_id if principal == sorted(active)[0] else _new_id(), discord_author_id,
                     principal, requester_actor_id, occurred_at, int(time.time())),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"mapping_event_id": event_id, "discord_author_id": discord_author_id}


def list_mappings(data_dir):
    """Read-only owner view: one row per Discord author id ever bound, with raw id, the
    non-authoritative username snapshot, the mapped principal, and current state.  Opens the DB
    read-only; performs no model or network call and writes nothing."""
    import os
    path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _table_present(conn):
            return []
        author_ids = [r[0] for r in conn.execute(
            "SELECT discord_author_id FROM discord_author_mapping_events "
            "GROUP BY discord_author_id ORDER BY MIN(rowid)")]
        out = []
        for author_id in author_ids:
            rows = _ledger_rows(conn, author_id)
            resolved = resolve_author(conn, author_id)
            last_map = next((r for r in reversed(rows) if r[1] == ACTION_MAP), None)
            out.append({
                "discord_author_id": author_id,
                "status": resolved["status"],
                "principal_actor_id": resolved.get("principal_actor_id") or (last_map[2] if last_map else None),
                "display_label": resolved.get("display_label") or (last_map[3] if last_map else None),
                "discord_username_snapshot": last_map[4] if last_map else None,
            })
        return out
    finally:
        conn.close()


def assignable_principals(data_dir):
    """Read-only: (principal_actor_id, label, role) for every principal the owner may lawfully bind
    right now (the owner, plus each active family member)."""
    import os
    path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if family_membership.family_feature_used(conn):
            ids = sorted(family_membership.active_family_principals(conn))
        else:
            owner = family_membership.resolve_owner_actor_id(conn)
            ids = [owner] if owner else []
        return [
            (pid, principal_display_label(conn, pid) or pid, principal_role(conn, pid))
            for pid in ids if principal_is_currently_valid(conn, pid)
        ]
    finally:
        conn.close()
