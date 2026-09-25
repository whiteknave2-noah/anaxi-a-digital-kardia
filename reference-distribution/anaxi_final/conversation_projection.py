"""Canonical, read-only waking-conversation projection.

Durable ``human_waking_input`` (H) and ``waking_turn`` (X) events are
projected into the GUI's role-labelled message history. X is paired to H only
by the canonical ``human_input_event_id`` component. A delayed recovery thus
appears immediately beneath the H it answers while retaining its own timestamp.

Optional viewer arguments bind the projection to the authenticated GUI
principal and the existing FS1 delivery law. Calls which omit them retain the
legacy/unbound projection used by historical tooling and small synthetic tests.
"""
import os
import sqlite3
from pathlib import Path

import family_membership
from human_session_binding import HUMAN_WAKING_INPUT_EVENT_TYPE

WAKING_TURN_EVENT_TYPE = "waking_turn"

_HUMAN_TEXT_QUERY = (
    "SELECT component_text FROM event_components "
    "WHERE event_id = ? AND sequence = 0 "
    "AND component_kind = 'human_conversational_input'"
)
_CLARK_PROSE_QUERY = (
    "SELECT component_text FROM event_components "
    "WHERE event_id = ? AND component_kind = 'conversational_prose'"
)
_MIXED_HISTORICAL_QUERY = (
    "SELECT component_text FROM event_components "
    "WHERE event_id = ? AND authorship_resolution = 'unresolved_mixed_historical'"
)
_HUMAN_LINK_QUERY = (
    "SELECT component_text FROM event_components "
    "WHERE event_id = ? AND component_kind = 'human_input_event_id'"
)


# A Caret waking OCCASION turn: it carries an inbound Caret message and no human authored a turn for it.
# Its conversational_prose is Clark's local Pass-2 text; it is communicated to the correspondent ONLY if
# he chose the reply route, and then through the canonical outward act -- never through this projection.
# It is therefore never an ordinary owner-conversation X (neither sent nor unsent prose is projected as
# an orphan assistant turn).  A Mac turn that merely carried an inbound message has a human H link and
# stays an ordinary exchange.
_CARET_CARRIAGE_QUERY = (
    "SELECT 1 FROM event_components "
    "WHERE event_id = ? AND component_kind = 'discord_inbound_carriage' LIMIT 1"
)
# A family-mode Caret occasion is a shared-session turn with its OWN canonical H (the correspondent's exact
# words), so it does carry a human link.  Two mechanical markers keep such turns out of the ordinary owner
# conversation: the occasion turn records the resolved correspondent principal, and its H carries the
# inbound-source marker.  Neither exists on a Mac H / Mac turn.
_CARET_CORRESPONDENT_QUERY = (
    "SELECT 1 FROM event_components "
    "WHERE event_id = ? AND component_kind = 'caret_correspondent_principal' LIMIT 1"
)
# LAWFUL NULL: Clark's own typed choice to say nothing (native_provenance_writer.
# CLARK_REPLY_CHOICE_COMPONENT_KIND), authored by the Clark actor.  Such an X answers its H with no
# prose; it is projected as its own row carrying reply_choice so the display can state the fact,
# never as words attributed to Clark and never omitted (an omitted X would look like a failure).
_CLARK_NULL_CHOICE_QUERY = (
    "SELECT 1 FROM event_components c JOIN actors a ON a.actor_id = c.creator_actor_id "
    "WHERE c.event_id = ? AND c.component_kind = 'clark_reply_choice' AND c.component_text = 'no_reply' "
    "AND a.actor_type = 'clark_agent' LIMIT 1"
)
_CARET_H_SOURCE_QUERY = (
    "SELECT 1 FROM event_components "
    "WHERE event_id = ? AND component_kind = 'discord_inbound_source' LIMIT 1"
)


class ProjectionIntegrityError(RuntimeError):
    """Canonical H/X exists but cannot lawfully reach the GUI projection."""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _scope_expr(conn, table, alias):
    return f"{alias}.visibility_scope" if "visibility_scope" in _columns(conn, table) else "NULL"


def _canonical_h_delivery_row(conn, event_id):
    event_scope = _scope_expr(conn, "events", "h")
    auth_scope = _scope_expr(conn, "auth_contexts", "a")
    rows = conn.execute(
        "SELECT h.pipeline_id, h.pipeline_provenance_status, hc.creator_actor_id, "
        "hc.component_text, a.auth_state, a.authenticated_actor_id, "
        f"{event_scope}, {auth_scope} "
        "FROM events h JOIN event_components hc ON hc.event_id=h.event_id "
        "JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
        "WHERE h.event_id=? AND h.event_type='human_waking_input' "
        "AND hc.sequence=0 AND hc.component_kind='human_conversational_input'",
        (event_id,),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _canonical_x_delivery_row(conn, event_id):
    event_scope = _scope_expr(conn, "events", "x")
    auth_scope = _scope_expr(conn, "auth_contexts", "a")
    rows = conn.execute(
        "SELECT x.pipeline_id, x.pipeline_provenance_status, "
        f"{event_scope}, {auth_scope} "
        "FROM events x JOIN auth_contexts a ON a.auth_context_id=x.auth_context_id "
        "WHERE x.event_id=? AND x.event_type='waking_turn'",
        (event_id,),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _h_permitted_for_viewer(conn, h_event_id, *, viewer_principal_actor_id,
                            viewer_visibility_scope, active_family_principal_ids):
    # Unbound legacy callers include tiny projection-only schemas with no
    # auth_contexts table. Preserve that historical test/tool contract.
    if viewer_principal_actor_id is None and viewer_visibility_scope is None:
        return not family_membership.family_feature_used(conn)
    row = _canonical_h_delivery_row(conn, h_event_id)
    if row is None:
        return False
    (_pipeline_id, provenance_status, creator_actor_id, _text, auth_state,
     authenticated_actor_id, event_scope, auth_scope) = row
    if (
        provenance_status != "known"
        or auth_state != "authenticated"
        or authenticated_actor_id != creator_actor_id
        or event_scope != auth_scope
    ):
        return False
    if viewer_visibility_scope is None:
        return event_scope is None and creator_actor_id == viewer_principal_actor_id
    return family_membership.can_receive_event(
        viewer_principal_actor_id, viewer_visibility_scope,
        creator_actor_id, event_scope,
        active_family_principal_ids=active_family_principal_ids,
    )


def _legacy_orphan_x_permitted(conn, *, viewer_principal_actor_id,
                               viewer_visibility_scope, x_event_id=None):
    """Pre-A4c X has no H from which to derive a principal."""
    if viewer_principal_actor_id is None and viewer_visibility_scope is None:
        return True
    if viewer_visibility_scope is not None:
        # The single legacy owner-private seam (family_membership): a
        # proven pre-Family X is the owner's own continuity, read-time only.
        return x_event_id is not None and family_membership.legacy_owner_private_reason(
            conn, x_event_id, viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_scope=viewer_visibility_scope,
        ) is not None
    if family_membership.family_feature_used(conn):
        return False
    return family_membership.resolve_owner_actor_id(conn) == viewer_principal_actor_id


def _project_waking_conversation_conn(conn, *, include_event_metadata=False,
                                      viewer_principal_actor_id=None,
                                      viewer_visibility_scope=None):
    # rowid = canonical insertion order: the tie-break within one second (ULIDs minted in the same
    # millisecond are not ordered by creation).
    h_rows = conn.execute(
        "SELECT event_id, occurred_at, rowid FROM events WHERE event_type = ? ORDER BY occurred_at, rowid",
        (HUMAN_WAKING_INPUT_EVENT_TYPE,),
    ).fetchall()
    x_rows = conn.execute(
        "SELECT event_id, occurred_at, rowid FROM events WHERE event_type = ? ORDER BY occurred_at, rowid",
        (WAKING_TURN_EVENT_TYPE,),
    ).fetchall()
    active_principals = (
        family_membership.active_family_principals(conn)
        if viewer_principal_actor_id is not None or viewer_visibility_scope is not None
        else frozenset()
    )
    x_records = {}
    linked_h_to_xs = {}
    unlinked_x_ids = []
    x_rowids = {x_event_id: x_rowid for x_event_id, _at, x_rowid in x_rows}
    for x_event_id, x_occurred_at, _x_rowid in x_rows:
        link_rows = conn.execute(_HUMAN_LINK_QUERY, (x_event_id,)).fetchall()
        if (
            conn.execute(_CARET_CORRESPONDENT_QUERY, (x_event_id,)).fetchone() is not None
            or (not link_rows and conn.execute(_CARET_CARRIAGE_QUERY, (x_event_id,)).fetchone() is not None)
        ):
            continue   # Caret occasion turn: never projected into the ordinary owner conversation
        prose_rows = conn.execute(_CLARK_PROSE_QUERY, (x_event_id,)).fetchall()
        reply_choice = None
        if len(prose_rows) == 1:
            clark_text = prose_rows[0][0]
        elif not prose_rows:
            mixed_rows = conn.execute(_MIXED_HISTORICAL_QUERY, (x_event_id,)).fetchall()
            clark_text = mixed_rows[0][0] if len(mixed_rows) == 1 else None
            if clark_text is None and conn.execute(_CLARK_NULL_CHOICE_QUERY, (x_event_id,)).fetchone():
                clark_text, reply_choice = "", "no_reply"
        else:
            clark_text = None
        if clark_text is None:
            continue
        x_records[x_event_id] = (x_occurred_at, clark_text, reply_choice)
        if len(link_rows) == 1:
            linked_h_to_xs.setdefault(link_rows[0][0], []).append(x_event_id)
        elif not link_rows:
            unlinked_x_ids.append(x_event_id)
        # Multiple H links are structurally ambiguous and suppressed.

    timeline = []
    for h_event_id, h_occurred_at, h_rowid in h_rows:
        if conn.execute(_CARET_H_SOURCE_QUERY, (h_event_id,)).fetchone() is not None:
            continue   # Caret-originated H: a correspondent's words, never the ordinary owner conversation
        human_rows = conn.execute(_HUMAN_TEXT_QUERY, (h_event_id,)).fetchall()
        if len(human_rows) != 1:
            continue
        if not _h_permitted_for_viewer(
            conn, h_event_id,
            viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_visibility_scope=viewer_visibility_scope,
            active_family_principal_ids=active_principals,
        ):
            continue
        # Sort key groups each exchange: (H time, H insertion order, role rank).  Keying on role rank before
        # the tie-break put every same-second H ahead of every same-second X (H1, H2, X1, X2) -- replies
        # detached from what they answer.
        timeline.append(((h_occurred_at, h_rowid, 0), h_event_id, "user", human_rows[0][0], h_occurred_at, None))
        linked_xs = linked_h_to_xs.get(h_event_id, [])
        if len(linked_xs) == 1 and linked_xs[0] in x_records:
            x_event_id = linked_xs[0]
            x_occurred_at, clark_text, reply_choice = x_records[x_event_id]
            event_scope = _scope_expr(conn, "events", "e")
            x_scope_row = conn.execute(
                f"SELECT {event_scope} FROM events e WHERE e.event_id=?", (x_event_id,),
            ).fetchone()
            h_scope = None
            x_delivery_valid = True
            if viewer_principal_actor_id is not None or viewer_visibility_scope is not None:
                h_delivery = _canonical_h_delivery_row(conn, h_event_id)
                x_delivery = _canonical_x_delivery_row(conn, x_event_id)
                h_scope = h_delivery[6]
                x_delivery_valid = bool(
                    x_delivery is not None
                    and x_delivery[0] == h_delivery[0]
                    and x_delivery[1] == "known"
                    and x_delivery[2] == x_delivery[3]
                )
            if x_delivery_valid and x_scope_row is not None and x_scope_row[0] == h_scope:
                # Sort with H, but retain X's own canonical display time.
                timeline.append(((h_occurred_at, h_rowid, 1), x_event_id, "assistant", clark_text, x_occurred_at, reply_choice))

    for x_event_id in unlinked_x_ids:
        if not _legacy_orphan_x_permitted(
            conn,
            viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_visibility_scope=viewer_visibility_scope,
            x_event_id=x_event_id,
        ):
            continue
        x_occurred_at, clark_text, reply_choice = x_records[x_event_id]
        timeline.append(((x_occurred_at, x_rowids[x_event_id], 1), x_event_id, "assistant", clark_text, x_occurred_at, reply_choice))

    timeline.sort(key=lambda row: row[0])
    return [
        {
            "role": role,
            "content": text,
            **({"reply_choice": reply_choice} if reply_choice is not None else {}),
            **({"event_id": event_id, "occurred_at": actual_occurred_at}
               if include_event_metadata else {}),
        }
        for (_sort_key, event_id, role, text, actual_occurred_at, reply_choice) in timeline
    ]


def project_waking_conversation(db_path: str, *, include_event_metadata=False,
                                viewer_principal_actor_id=None,
                                viewer_visibility_scope=None) -> list[dict]:
    """Project canonical history through the exact read used by the GUI."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute("PRAGMA query_only = ON;")
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone() is None:
            return []
        return _project_waking_conversation_conn(
            conn,
            include_event_metadata=include_event_metadata,
            viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_visibility_scope=viewer_visibility_scope,
        )
    finally:
        conn.close()


def verify_recovered_exchange_projection(conn, human_input_event_id, x_event_id, actor_id):
    """Prove one recovered X is substantive and GUI-deliverable.

    This is WTR0's terminal-success boundary. It validates the exact returned
    X, unique H linkage, Clark authorship, pipeline/scope/auth consistency, and
    finally the ordinary authenticated projection. It never repairs content.
    """
    h_row = _canonical_h_delivery_row(conn, human_input_event_id)
    if h_row is None:
        raise ProjectionIntegrityError("canonical H is missing or structurally ambiguous")
    (h_pipeline, h_provenance, h_creator, _h_text, h_auth_state,
     h_authenticated_actor, h_scope, h_auth_scope) = h_row
    if (
        h_provenance != "known"
        or h_auth_state != "authenticated"
        or h_creator != actor_id
        or h_authenticated_actor != actor_id
        or h_scope != h_auth_scope
    ):
        raise ProjectionIntegrityError("canonical H does not establish the requested authenticated principal/scope")

    event_scope = _scope_expr(conn, "events", "x")
    auth_scope = _scope_expr(conn, "auth_contexts", "xa")
    x_rows = conn.execute(
        "SELECT x.pipeline_id, x.pipeline_provenance_status, x.event_type, "
        f"{event_scope}, {auth_scope} "
        "FROM events x JOIN auth_contexts xa ON xa.auth_context_id=x.auth_context_id "
        "WHERE x.event_id=?",
        (x_event_id,),
    ).fetchall()
    if len(x_rows) != 1:
        raise ProjectionIntegrityError("returned X does not resolve uniquely")
    x_pipeline, x_provenance, x_type, x_scope, x_auth_scope = x_rows[0]
    if x_type != WAKING_TURN_EVENT_TYPE or x_provenance != "known" or x_pipeline != h_pipeline:
        raise ProjectionIntegrityError("returned X is not a known waking_turn in H's pipeline")
    if x_scope != x_auth_scope or x_scope != h_scope:
        raise ProjectionIntegrityError("recovered H/X/auth visibility scopes disagree")

    links = conn.execute(_HUMAN_LINK_QUERY, (x_event_id,)).fetchall()
    if len(links) != 1 or links[0][0] != human_input_event_id:
        raise ProjectionIntegrityError("returned X does not carry exactly one link to the recovered H")

    prose_rows = conn.execute(
        "SELECT c.component_text, a.actor_type, a.stable_key "
        "FROM event_components c JOIN actors a ON a.actor_id=c.creator_actor_id "
        "WHERE c.event_id=? AND c.component_kind='conversational_prose'",
        (x_event_id,),
    ).fetchall()
    reply_choice = None
    if not prose_rows and conn.execute(_CLARK_NULL_CHOICE_QUERY, (x_event_id,)).fetchone() is not None:
        # LAWFUL NULL: the recovered turn completed with Clark's own typed choice to say nothing.
        # That ANSWERS the recovered H exactly like prose does (it is his choice, not a failure);
        # the projection must show it as that fact, adjacent to H.
        prose, reply_choice = "", "no_reply"
    else:
        if len(prose_rows) != 1:
            raise ProjectionIntegrityError("returned X does not contain exactly one conversational_prose component")
        prose, actor_type, stable_key = prose_rows[0]
        if actor_type != "clark_agent" or stable_key != "clark":
            raise ProjectionIntegrityError("recovered conversational prose is not canonically Clark-authored")
        if not isinstance(prose, str) or not prose.strip():
            raise ProjectionIntegrityError("recovered X contains no substantive conversational text")

    active_principals = family_membership.active_family_principals(conn)
    if not family_membership.can_receive_event(
        actor_id, h_scope, h_creator, h_scope,
        active_family_principal_ids=active_principals,
    ):
        raise ProjectionIntegrityError("authenticated principal cannot receive the recovered H/X scope")

    projected = _project_waking_conversation_conn(
        conn,
        include_event_metadata=True,
        viewer_principal_actor_id=actor_id,
        viewer_visibility_scope=h_scope,
    )
    positions = {row["event_id"]: index for index, row in enumerate(projected)}
    h_position = positions.get(human_input_event_id)
    x_position = positions.get(x_event_id)
    if (
        h_position is None
        or x_position != h_position + 1
        or projected[x_position]["role"] != "assistant"
        or projected[x_position]["content"] != prose
        or projected[x_position].get("reply_choice") != reply_choice
    ):
        raise ProjectionIntegrityError("exact recovered H/X is not adjacent and visible in authenticated GUI history")
    return {
        "human_input_event_id": human_input_event_id,
        "canonical_x_event_id": x_event_id,
        "conversational_text": prose,
        "reply_choice": reply_choice,
        "visibility_scope": h_scope,
        "projected_index": x_position,
    }
