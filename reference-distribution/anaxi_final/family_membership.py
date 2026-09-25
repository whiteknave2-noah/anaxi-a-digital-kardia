"""FS1: family-membership administration + principal visibility-scope
enforcement.

V0 family model (frozen, restated here for the code that implements it):

  - A single family group (group_key='family_v0') contains exactly the
    principal humans Clark may hold conversation with. Membership is
    created ONLY by explicit, host-or-owner-mediated administrative acts
    recorded permanently in the append-only canonical ledger
    family_principal_events (fs1_schema_migration). There is no implicit
    membership, no display-label-derived membership, no self-service.
  - OWNER: named exactly once, by a designate_owner act whose requester
    is the production host_system actor (hir1_registration.HOST_ACTOR_ID)
    and whose subject is an ALREADY-registered human_person actor. V0
    owner permanence: designate_owner may never run twice (no
    reassignment, no revocation), and the owner can never be deactivated
    structurally (the schema's role/kind CHECK pairing exposes deactivate
    only for actor_role='family_member').
  - FAMILY_MEMBER: enrolled by the CURRENT OWNER as requester -- never by
    a member, never by Clark, never by the host (enrollment is
    owner-gated; no chain). A member may be deactivated by the owner.
  - Family administration NEVER registers or unregisters a human.
    Principals must already exist as canonical human_person actors
    (hir1_registration) before any family act may reference them.
  - When the family feature has NEVER been used (empty ledger), the
    legacy single-owner behavior is preserved for legacy material: the
    owner resolves to the exactly-one human_person actor through
    resolve_owner_actor_id. This legacy owner fallback never creates an
    active family principal. Before FS1 is activated, an unbound viewer
    may continue to receive legacy NULL-scope material. Once family
    membership exists, identity-less waking delivery is suppressed.

Visibility-scope enforcement model (fail closed):

  - events/auth_contexts carry an optional visibility_scope (see
    fs1_schema_migration):
      NULL              -- no principal-scope classification; governed by
                           pre-FS1 rules, and in a scoped session
                           deliverable only to its own author.
      'principal_private' -- visible to its author principal only, and
                           only inside that principal's own private
                           session.
      'family_shared'    -- visible to any currently-active family
                           principal (owner or enrolled member), inside
                           either a private or a shared session.
  - (Legacy owner-private compatibility, below, may additionally make NULL-scope
    events that provably predate Family activation deliverable to the owner's
    private session; it never applies to shared or other-principal sessions.)
  - An UNBOUND session (viewer_scope None) keeps today's behavior for
    legacy NULL-scope material only. Explicit FS1 private/shared scope
    always requires an authoritative principal binding.
  - The owner is a principal like any other: the owner's private content
    is never shared, and no code path here ever promotes private content
    to shared (non-promotion invariant).
  - can_receive_event() is a PURE predicate over already-resolved facts
    (never a guess, never derived from display labels).
  - LEGACY COMPATIBILITY (the one exception to NULL-scope fail-closure): NULL
    scope written before Family activation is the legacy single-owner regime.
    See "legacy single-owner seam" below: a read-time eligibility rule for the
    designated owner's own principal_private session only, bounded by the
    canonical activation timestamp, never a rewrite of the historical event.
"""
import json
import sqlite3
import sys
import os
import time

_ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if _ANAXI_FINAL not in sys.path:
    sys.path.insert(0, _ANAXI_FINAL)

from provenance_schema import derive_stable_id  # noqa: E402
from hir1_registration import HOST_ACTOR_ID  # noqa: E402
from fs1_schema_migration import apply_migration_on_connection  # noqa: E402

GROUP_KEY_FAMILY = "family_v0"
ACTOR_ROLE_OWNER = "owner"
ACTOR_ROLE_FAMILY_MEMBER = "family_member"

EVENT_KIND_DESIGNATE_OWNER = "designate_owner"
EVENT_KIND_ENROLL = "enroll"
EVENT_KIND_DEACTIVATE = "deactivate"

SCOPE_PRINCIPAL_PRIVATE = "principal_private"
SCOPE_FAMILY_SHARED = "family_shared"
VALID_SCOPES = (SCOPE_PRINCIPAL_PRIVATE, SCOPE_FAMILY_SHARED)
# General correspondence: a VIEWER scope only (never written into events.visibility_scope, whose FS1
# column CHECK admits only the two values above -- extending it would mean rebuilding the canonical
# events table).  Correspondent-governed events keep NULL scope, so every principal predicate below
# already fails them closed; they are recognized by their canonical host-authored
# correspondent_source_ref component (correspondence_surfaces).  A correspondent viewer's
# "principal" argument is its source ref.
SCOPE_CORRESPONDENT = "correspondent"

apply_fs1_scope_migration_on_connection = apply_migration_on_connection


class FamilyMembershipError(Exception):
    """Raised when a family-administration act or authority predicate
    cannot be carried out mechanically. Never caught silently."""


# ------------------------------------------------------------- read side


def _family_tables_present(conn):
    """True iff the FS1 family tables exist in this database. Every read
    path must tolerate a pre-FS1 schema (feature-unused) because
    direction_control / workspace_episode_provenance and several
    integration tests run against plain create_provenance_db() databases
    that have never been migrated."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='family_principal_events'"
    ).fetchone()
    return row is not None and row[0] >= 1


def _family_act_count(conn):
    if not _family_tables_present(conn):
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM family_principal_events WHERE group_key = ?",
        (GROUP_KEY_FAMILY,),
    ).fetchone()
    return row[0]


def family_feature_used(conn):
    """True only when canonical FS1 membership history exists."""
    return _family_act_count(conn) > 0


def _events_has_scope_column(conn):
    return "visibility_scope" in {
        r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()
    }


def _human_person_actor_ids(conn):
    return [
        r[0] for r in conn.execute(
            "SELECT actor_id FROM actors WHERE actor_type = 'human_person' ORDER BY actor_id"
        ).fetchall()
    ]


def _latest_act_per_principal(conn):
    """Returns {principal_actor_id: event_kind} for each principal's most
    recent family act. Ordering is by rowid -- family_principal_events is
    append-only (no UPDATE/DELETE ever, enforced by triggers), so rowid
    is a strictly monotonic write sequence and MAX(rowid) is exactly the
    chronologically latest act. (event_id is a content hash and is NOT
    time-sortable; celestial seconds-collision makes (record_created_at,
    event_id) unsafe for ordering.)"""
    if not _family_tables_present(conn):
        return {}
    rows = conn.execute(
        """
        SELECT f.principal_actor_id, f.event_kind
        FROM family_principal_events f
        JOIN (
            SELECT principal_actor_id, MAX(rowid) AS latest_rowid
            FROM family_principal_events
            WHERE group_key = ?
            GROUP BY principal_actor_id
        ) g ON g.principal_actor_id = f.principal_actor_id
           AND f.rowid = g.latest_rowid
        WHERE f.group_key = ?
        """,
        (GROUP_KEY_FAMILY, GROUP_KEY_FAMILY),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _latest_act_row(conn, principal_actor_id):
    """Canonical latest act for one principal, never the projection."""
    if not _family_tables_present(conn):
        return None
    return conn.execute(
        "SELECT event_kind, display_label, occurred_at, event_id "
        "FROM family_principal_events WHERE group_key = ? AND principal_actor_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (GROUP_KEY_FAMILY, principal_actor_id),
    ).fetchone()


def designated_owner_actor_id(conn):
    """Read-only. The single currently-designated owner principal, or
    None when no designated owner exists. (Re-designation is forbidden
    by the writer and by owner permanence, so at most one row can exist;
    LIMIT 2 + length check is defensive fail closure.)"""
    if not _family_tables_present(conn):
        return None
    rows = conn.execute(
        "SELECT principal_actor_id, requester_actor_id, actor_role "
        "FROM family_principal_events "
        "WHERE group_key = ? AND event_kind = ? LIMIT 2",
        (GROUP_KEY_FAMILY, EVENT_KIND_DESIGNATE_OWNER),
    ).fetchall()
    if len(rows) != 1:
        return None
    principal_actor_id, requester_actor_id, actor_role = rows[0]
    if (
        actor_role != ACTOR_ROLE_OWNER
        or requester_actor_id != HOST_ACTOR_ID
        or not _is_host_system_actor(conn, requester_actor_id)
        or not _is_registered_human(conn, principal_actor_id)
    ):
        return None
    return principal_actor_id


def _canonical_family_history_is_valid(conn, owner):
    """Validate the complete V0 authority/state machine.

    The append-only ledger is canonical, but canonical does not mean an
    impossible or unauthorized sequence should be interpreted loosely.
    Any forged/contradictory occurrence makes active membership fail
    closed rather than allowing a later row to silently win.
    """
    rows = conn.execute(
        "SELECT principal_actor_id, actor_role, event_kind, requester_actor_id "
        "FROM family_principal_events WHERE group_key=? ORDER BY rowid",
        (GROUP_KEY_FAMILY,),
    ).fetchall()
    member_state = {}
    designation_count = 0
    for principal, role, kind, requester in rows:
        if kind == EVENT_KIND_DESIGNATE_OWNER:
            designation_count += 1
            if (
                designation_count != 1 or principal != owner
                or role != ACTOR_ROLE_OWNER or requester != HOST_ACTOR_ID
            ):
                return False
            continue
        if (
            role != ACTOR_ROLE_FAMILY_MEMBER
            or principal == owner
            or requester != owner
            or not _is_registered_human(conn, principal)
        ):
            return False
        if kind == EVENT_KIND_ENROLL:
            if principal in member_state:
                return False
            member_state[principal] = EVENT_KIND_ENROLL
        elif kind == EVENT_KIND_DEACTIVATE:
            if member_state.get(principal) != EVENT_KIND_ENROLL:
                return False
            member_state[principal] = EVENT_KIND_DEACTIVATE
        else:
            return False
    return designation_count == 1


def is_designated_owner(conn, actor_id):
    """Read-only. True iff actor_id IS the current designated owner."""
    owner = designated_owner_actor_id(conn)
    return owner is not None and owner == actor_id


def resolve_owner_actor_id(conn):
    """Read-only. Prefers a designated family owner. If the family
    feature has never been used (empty ledger), falls back to the legacy
    exactly-one-human_person rule so single-owner deployments keep today's
    behavior. Any ambiguous/unusable state returns None (fail closed --
    the caller decides how to treat an unresolvable owner)."""
    owner = designated_owner_actor_id(conn)
    if owner is not None:
        return owner
    if _family_act_count(conn) == 0:
        humans = _human_person_actor_ids(conn)
        return humans[0] if len(humans) == 1 else None
    return None


def active_family_principals(conn):
    """Read-only. The set of principal actor_ids that may receive
    family_shared content right now: the designated owner plus every
    enrolled member whose most recent family act is enroll (not
    deactivate). If the ledger is empty, contradictory, or has no valid
    designated owner, fails closed to an empty set. Legacy single-human
    ownership is resolved separately and never implies family enrollment."""
    count = _family_act_count(conn)
    if count == 0:
        # Legacy exactly-one-human ownership is not family enrollment.
        # A scoped caller may not turn absent membership history into
        # active membership.
        return frozenset()
    owner = designated_owner_actor_id(conn)
    if owner is None or not _canonical_family_history_is_valid(conn, owner):
        return frozenset()
    latest = _latest_act_per_principal(conn)
    active = {
        actor_id for actor_id, kind in latest.items()
        if kind in (EVENT_KIND_DESIGNATE_OWNER, EVENT_KIND_ENROLL)
    }
    return frozenset(active)


def can_receive_event(viewer_principal_actor_id, viewer_scope,
                      event_creator_actor_id, event_scope,
                      active_family_principal_ids=frozenset()):
    """PURE. Decides whether one event's content may be delivered to a
    given principal's current session. The caller supplies ONLY facts it
    has already mechanically established -- never derived from dialogue
    text or display labels.

      viewer_scope None => UNBOUND session => legacy NULL/unknown scope
      only. Explicit FS1 scope is denied because no recipient principal
      has been authenticated.
      event_scope 'family_shared'     => viewer must be a currently-
          active family principal (private OR shared session both count,
          F9: family-shared continuity inside principal-private sessions).
      event_scope 'principal_private' => viewer must be in a principal-
          private session AND be the event's own author. Never crosses.
      event_scope None/unknown        => fail closed: deliverable ONLY to
          the event's own author, and only inside that author's own
          principal-private session. Never guessed broader.
    """
    if viewer_scope is None:
        return event_scope not in VALID_SCOPES
    if event_scope == SCOPE_FAMILY_SHARED:
        return viewer_principal_actor_id in active_family_principal_ids
    if event_scope == SCOPE_PRINCIPAL_PRIVATE:
        return (
            viewer_scope == SCOPE_PRINCIPAL_PRIVATE
            and event_creator_actor_id == viewer_principal_actor_id
        )
    return (
        viewer_scope == SCOPE_PRINCIPAL_PRIVATE
        and event_creator_actor_id == viewer_principal_actor_id
    )


WORKSPACE_EVENT_TYPE_PREFIX = "workspace_"


def native_workspace_event_scope(conn):
    """Read-only. The scope a NEW workspace/episode canonical event must be
    stamped with: principal_private once Family is validly active (the
    workspace is the designated owner's private surface), otherwise None
    (legacy single-owner mode keeps writing NULL, byte-identically)."""
    if not _events_has_scope_column(conn) or not family_feature_used(conn):
        return None
    if family_activation_boundary(conn) is None:
        return None
    return SCOPE_PRINCIPAL_PRIVATE


def _scoped_event_principal(conn, event_id, event_type, event_scope):
    """Resolve the human principal governing an explicitly scoped event.

    Clark-authored X content is governed by the authenticated human H it
    answers, not by Clark's creator_actor_id. No caller/retrieval-store
    author field is trusted for this decision.
    """
    if event_scope not in VALID_SCOPES:
        return None
    if str(event_type).startswith(WORKSPACE_EVENT_TYPE_PREFIX):
        # Native post-Family workspace facts (host/Clark activity in the
        # designated owner's workspace) are stamped principal_private at
        # write time; workspace availability is itself owner-private law,
        # and V0 owner permanence makes the governing principal the
        # designated owner.  A family_shared workspace fact has no
        # governing principal (fail closed).
        return designated_owner_actor_id(conn) if event_scope == SCOPE_PRINCIPAL_PRIVATE else None
    if event_type == "human_waking_input":
        rows = conn.execute(
            "SELECT ac.authenticated_actor_id, hc.creator_actor_id "
            "FROM events h JOIN auth_contexts ac ON ac.auth_context_id=h.auth_context_id "
            "JOIN event_components hc ON hc.event_id=h.event_id "
            "WHERE h.event_id=? AND hc.component_kind='human_conversational_input' "
            "AND hc.sequence=0 AND ac.auth_state='authenticated'",
            (event_id,),
        ).fetchall()
        if len(rows) == 1 and rows[0][0] is not None and rows[0][0] == rows[0][1]:
            return rows[0][0]
        return None
    if event_type == "waking_turn":
        rows = conn.execute(
            "SELECT ac.authenticated_actor_id, hc.creator_actor_id, h.visibility_scope "
            "FROM event_components link "
            "JOIN events h ON h.event_id=link.component_text "
            "JOIN auth_contexts ac ON ac.auth_context_id=h.auth_context_id "
            "JOIN event_components hc ON hc.event_id=h.event_id "
            "WHERE link.event_id=? AND link.component_kind='human_input_event_id' "
            "AND h.event_type='human_waking_input' "
            "AND hc.component_kind='human_conversational_input' AND hc.sequence=0 "
            "AND ac.auth_state='authenticated'",
            (event_id,),
        ).fetchall()
        if (
            len(rows) == 1 and rows[0][0] is not None
            and rows[0][0] == rows[0][1] and rows[0][2] == event_scope
        ):
            return rows[0][0]
    return None


# ------------------------------------------------- legacy single-owner seam
#
# Before Family activation ANAXI ran in legacy single-owner mode: every
# event was written with a NULL visibility_scope because scope did not
# exist.  Once Family is active, FS1's fail-closed law denies NULL scope to
# everyone but the event's own creator, which strands the owner's own
# pre-Family continuity (Clark-authored replies, memory derived from them,
# and so on).  The seam below is the ONE authoritative compatibility rule:
# a READ-TIME eligibility fact, never a mutation.  The historical event keeps
# its id, NULL scope, timestamps, provenance, content and authorship.
#
# An event is LEGACY OWNER-PRIVATE for a viewer iff ALL of:
#   - the viewer is the designated owner in that owner's own
#     principal_private session (never shared, never another principal);
#   - canonical Family history is valid and yields an activation boundary
#     (the designate_owner act's own canonical timestamp -- never a
#     caller-supplied or guessed time);
#   - the event's scope, and its auth_context's scope, are NULL;
#   - the event was BOTH occurred and recorded strictly before the boundary
#     (same-second is ambiguous and stays fail-closed);
#   - nothing in its provenance contradicts owner-private eligibility: no
#     external/correspondent (Discord/Caret/external-info/outward) event type
#     or component, and no human other than the owner as author,
#     authenticator or claimant; an X must link an H that itself qualifies.
# Anything else (post-Family NULL, ambiguous, foreign) remains fail-closed.

LEGACY_OWNER_PRIVATE_REASON = (
    "legacy_owner_private: this event was created before Family scope existed "
    "(pre-Family single-owner regime); its eligibility derives from proven legacy "
    "single-owner provenance for the canonical owner's private session, and its "
    "historical NULL scope was not retroactively changed."
)

_LEGACY_EXTERNAL_EVENT_TYPE_PREFIXES = ("discord_", "clark_external_info", "clark_outward")
_LEGACY_EXTERNAL_COMPONENT_PREFIXES = ("discord_", "caret_", "external_info_", "outward_")


def family_activation_boundary(conn):
    """Read-only. The canonical Family activation instant: the
    designate_owner act's own record_created_at, or None when Family has
    never been validly activated (fail closed)."""
    if not _family_tables_present(conn):
        return None
    owner = designated_owner_actor_id(conn)
    if owner is None or not _canonical_family_history_is_valid(conn, owner):
        return None
    row = conn.execute(
        "SELECT occurred_at, record_created_at FROM family_principal_events "
        "WHERE group_key = ? AND event_kind = ? LIMIT 1",
        (GROUP_KEY_FAMILY, EVENT_KIND_DESIGNATE_OWNER),
    ).fetchone()
    return None if row is None else max(row[0], row[1])


def legacy_owner_private_view(conn, *, viewer_principal_actor_id, viewer_scope):
    """Read-only. {'owner_actor_id', 'boundary'} when this viewer is the
    designated owner in that owner's own principal_private session after
    Family activation; otherwise None."""
    if viewer_scope != SCOPE_PRINCIPAL_PRIVATE or viewer_principal_actor_id is None:
        return None
    if not _events_has_scope_column(conn) or not family_feature_used(conn):
        return None
    boundary = family_activation_boundary(conn)
    owner = designated_owner_actor_id(conn)
    if boundary is None or owner is None or owner != viewer_principal_actor_id:
        return None
    created = conn.execute(
        "SELECT created_at FROM actors WHERE actor_id = ?", (owner,),
    ).fetchone()
    if created is None or created[0] > boundary:
        return None    # the owner was not part of the pre-Family regime
    return {"owner_actor_id": owner, "boundary": boundary}


def _is_non_owner_human(conn, actor_id, owner):
    if actor_id is None or actor_id == owner:
        return False
    row = conn.execute(
        "SELECT actor_type FROM actors WHERE actor_id = ?", (actor_id,),
    ).fetchone()
    return row is None or row[0] == "human_person"


def _legacy_event_core(conn, event_id, view):
    row = conn.execute(
        "SELECT event_type, visibility_scope, occurred_at, record_created_at, auth_context_id "
        "FROM events WHERE event_id = ?", (event_id,),
    ).fetchone()
    if row is None:
        return False
    event_type, scope, occurred_at, recorded_at, auth_context_id = row
    boundary, owner = view["boundary"], view["owner_actor_id"]
    if scope is not None or occurred_at is None or recorded_at is None:
        return False
    if occurred_at >= boundary or recorded_at >= boundary:
        return False
    if any(str(event_type).startswith(p) for p in _LEGACY_EXTERNAL_EVENT_TYPE_PREFIXES):
        return False
    for kind, creator in conn.execute(
        "SELECT component_kind, creator_actor_id FROM event_components WHERE event_id = ?",
        (event_id,),
    ).fetchall():
        if any(str(kind).startswith(p) for p in _LEGACY_EXTERNAL_COMPONENT_PREFIXES):
            return False
        if _is_non_owner_human(conn, creator, owner):
            return False
    if auth_context_id is not None:
        auth_scope_expr = (
            "visibility_scope"
            if "visibility_scope" in {r[1] for r in conn.execute("PRAGMA table_info(auth_contexts)")}
            else "NULL"
        )
        auth = conn.execute(
            f"SELECT authenticated_actor_id, claimed_actor_id, {auth_scope_expr} "
            "FROM auth_contexts WHERE auth_context_id = ?", (auth_context_id,),
        ).fetchone()
        if auth is None or auth[2] is not None:
            return False
        if _is_non_owner_human(conn, auth[0], owner) or _is_non_owner_human(conn, auth[1], owner):
            return False
    return True


def _legacy_event_eligible(conn, event_id, view):
    if not _legacy_event_core(conn, event_id, view):
        return False
    # An X is governed by the H it answers: that H must itself qualify.
    for (linked,) in conn.execute(
        "SELECT component_text FROM event_components "
        "WHERE event_id = ? AND component_kind = 'human_input_event_id'", (event_id,),
    ).fetchall():
        if not _legacy_event_core(conn, linked, view):
            return False
    return True


def _legacy_derivation_eligible(conn, derivation_id, view):
    """A Sleep derivation has no events row.  It qualifies only when its
    cycle completed strictly before activation and EVERY cited source event
    itself qualifies as legacy owner-private."""
    try:
        cycle = conn.execute(
            "SELECT c.completed_at, d.created_at FROM sleep_derivations d "
            "JOIN sleep_cycles c ON c.cycle_id = d.cycle_id WHERE d.derivation_id = ?",
            (derivation_id,),
        ).fetchone()
        sources = conn.execute(
            "SELECT DISTINCT source_event_id FROM sleep_derivation_sources WHERE derivation_id = ?",
            (derivation_id,),
        ).fetchall()
    except sqlite3.Error:
        return False
    if cycle is None or not sources:
        return False
    if cycle[0] >= view["boundary"] or cycle[1] >= view["boundary"]:
        return False
    return all(_legacy_event_eligible(conn, s[0], view) for s in sources)


def legacy_owner_private_reason(conn, event_id, *, viewer_principal_actor_id,
                                viewer_scope, view=None):
    """Read-only. LEGACY_OWNER_PRIVATE_REASON when the seam makes this
    NULL-scope event (or Sleep derivation id) eligible for the viewer,
    else None.  `view` may be a precomputed legacy_owner_private_view()."""
    if not isinstance(event_id, str) or not event_id:
        return None
    if view is None:
        view = legacy_owner_private_view(
            conn, viewer_principal_actor_id=viewer_principal_actor_id, viewer_scope=viewer_scope,
        )
    if view is None:
        return None
    if _legacy_event_eligible(conn, event_id, view) or _legacy_derivation_eligible(conn, event_id, view):
        return LEGACY_OWNER_PRIVATE_REASON
    return None


def can_receive_canonical_event(conn, event_id, *,
                                viewer_principal_actor_id, viewer_scope,
                                active_family_principal_ids=frozenset()):
    """Apply the existing FS1 delivery law to one canonical event.

    This is the narrow cross-subsystem seam for content whose own module
    holds only an event id (for example an active operative directive's
    triggering waking turn).  Scope and governing principal are derived
    from canonical events/H-X linkage here; callers cannot supply or
    override either fact.

    Explicitly scoped events require a valid canonical governing
    principal.  NULL/legacy events are never guessed into a scoped
    family context.  Before FS1 is in use, an unbound caller retains the
    historical unscoped behavior.
    """
    if not isinstance(event_id, str) or not event_id:
        return False
    import correspondence_surfaces as _cs
    if viewer_scope == SCOPE_CORRESPONDENT:
        return _cs.correspondent_event_receivable(conn, event_id, viewer_principal_actor_id)
    if _correspondent_governed(conn, event_id):
        return False        # a correspondent's continuity never reaches any principal, in any mode
    if not _events_has_scope_column(conn):
        return viewer_scope is None
    row = conn.execute(
        "SELECT event_type, visibility_scope FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return False
    event_type, event_scope = row
    if event_scope not in VALID_SCOPES:
        if viewer_scope is None:
            return not family_feature_used(conn)
        return legacy_owner_private_reason(
            conn, event_id, viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_scope=viewer_scope,
        ) is not None
    governing_principal = _scoped_event_principal(
        conn, event_id, event_type, event_scope,
    )
    if governing_principal is None:
        return False
    return can_receive_event(
        viewer_principal_actor_id, viewer_scope,
        governing_principal, event_scope,
        active_family_principal_ids=active_family_principal_ids,
    )


def permitted_hippocampal_items(conn, items, *,
                                viewer_principal_actor_id, viewer_scope,
                                active_family_principal_ids):
    """Read-only. Filters hippocampus RetrievedMemory `items` (objects
    with .event_id and .creator_actor_id) to the ones whose canonical
    `events.visibility_scope` permits delivery to the viewer's current
    session. `conn` must be a read-only provenance connection. For every
    item the scope is looked up in the canonical events table and an
    explicit scope's governing human principal is derived from canonical
    H/X provenance -- never from the retrieval object's claimed creator.
    An item with no canonical event, or a scoped event with no valid H/X
    principal, is denied. Before family activation, unbound viewers
    retain legacy NULL/unknown items but never explicitly scoped FS1
    items; after activation they receive no hippocampal item."""
    # Once canonical family membership exists, an identity-less viewer
    # receives no memory item at all: unknown legacy/derived provenance
    # cannot be assigned to a principal, and explicit scope cannot be
    # authorized without one.
    if viewer_scope == SCOPE_CORRESPONDENT:
        return [item for item in items if (
            _sleep_derivation_receivable(conn, item, viewer_principal_actor_id=viewer_principal_actor_id,
                                         viewer_scope=viewer_scope, active_family_principal_ids=frozenset())
            if getattr(item, "source_store", None) == SLEEP_DERIVATION_SOURCE_STORE
            else can_receive_canonical_event(conn, item.event_id, viewer_principal_actor_id=viewer_principal_actor_id,
                                             viewer_scope=viewer_scope))]
    if viewer_scope is None and family_feature_used(conn):
        return []
    items = [item for item in items if not _correspondent_governed(conn, item.event_id)]

    # A pre-FS1 database has no scope column and therefore cannot contain
    # an explicitly-scoped FS1 event. Preserve its legacy behavior without
    # pretending a missing privacy column authorizes any scoped content.
    if not _events_has_scope_column(conn):
        return list(items) if viewer_scope is None else [
            item for item in items
            if can_receive_event(
                viewer_principal_actor_id, viewer_scope,
                item.creator_actor_id, None,
                active_family_principal_ids=active_family_principal_ids,
            )
        ]

    legacy_view = legacy_owner_private_view(
        conn, viewer_principal_actor_id=viewer_principal_actor_id, viewer_scope=viewer_scope,
    )
    event_cache = {}
    permitted = []
    for item in items:
        if item.event_id not in event_cache:
            event_row = conn.execute(
                "SELECT event_type, visibility_scope FROM events WHERE event_id = ?",
                (item.event_id,),
            ).fetchone()
            event_cache[item.event_id] = event_row
        event_row = event_cache[item.event_id]
        if event_row is None:
            # A retrieval-store item without a canonical event cannot
            # establish either principal or scope by itself -- except a Sleep
            # derivation, which is derived from exactly its cited source
            # events: it is deliverable to a viewer iff EVERY cited source
            # event is deliverable to that viewer under this same canonical
            # law (can_receive_canonical_event, which includes the legacy
            # owner-private seam for pre-Family NULL events).  Before this,
            # only pre-Family derivations qualified, so every consolidation
            # made after Family activation reached no waking session at all.
            if _sleep_derivation_receivable(
                conn, item, viewer_principal_actor_id=viewer_principal_actor_id,
                viewer_scope=viewer_scope, active_family_principal_ids=active_family_principal_ids,
            ):
                permitted.append(item)
            continue
        event_type, event_scope = event_row
        governing_principal = item.creator_actor_id
        if event_scope in VALID_SCOPES:
            governing_principal = _scoped_event_principal(
                conn, item.event_id, event_type, event_scope,
            )
            if governing_principal is None:
                continue
        if can_receive_event(
            viewer_principal_actor_id, viewer_scope,
            governing_principal, event_scope,
            active_family_principal_ids=active_family_principal_ids,
        ):
            permitted.append(item)
        elif event_scope not in VALID_SCOPES and legacy_view is not None and legacy_owner_private_reason(
            conn, item.event_id, viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_scope=viewer_scope, view=legacy_view,
        ):
            permitted.append(item)
    return permitted


SLEEP_DERIVATION_SOURCE_STORE = "sleep_derivations"


def _correspondent_governed(conn, event_id):
    try:
        import correspondence_surfaces as _cs
        return _cs.correspondent_key_of_event(conn, event_id) is not None
    except sqlite3.Error:
        return False


def _sleep_derivation_receivable(conn, item, *, viewer_principal_actor_id, viewer_scope,
                                 active_family_principal_ids):
    """Read-only intersection rule for one Sleep-derivation retrieval item (see caller)."""
    if getattr(item, "source_store", None) != SLEEP_DERIVATION_SOURCE_STORE:
        return False
    try:
        sources = conn.execute(
            "SELECT DISTINCT source_event_id FROM sleep_derivation_sources WHERE derivation_id = ?",
            (item.event_id,),
        ).fetchall()
    except sqlite3.Error:
        return False
    if not sources:
        return False
    return all(
        can_receive_canonical_event(
            conn, source_event_id, viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_scope=viewer_scope, active_family_principal_ids=active_family_principal_ids,
        )
        for (source_event_id,) in sources
    )


def family_session_view(conn, *, principal_actor_id, visibility_scope):
    """Read-only. A frozen snapshot of the family facts one session's
    delivery filtering needs: the viewer's own principal/scope plus the
    currently-active family principals and the current owner. Callers use
    it to decide legacy/workspace suppression and to seed dialogue /
    retrieval filters. Never widens anything by itself -- the actual
    per-event predicate is can_receive_event()."""
    return {
        "principal_actor_id": principal_actor_id,
        "visibility_scope": visibility_scope,
        "active_family_principal_ids": active_family_principals(conn),
        "owner_actor_id": resolve_owner_actor_id(conn),
        "feature_used": _family_act_count(conn) > 0,
    }


# ------------------------------------------------------------ write side


def _require_family_tables(conn):
    apply_migration_on_connection(conn)


def _is_host_system_actor(conn, actor_id):
    row = conn.execute(
        "SELECT actor_id FROM actors WHERE actor_id = ? AND actor_type = 'host_system'",
        (actor_id,),
    ).fetchone()
    return row is not None


def _is_registered_human(conn, actor_id):
    row = conn.execute(
        "SELECT 1 FROM actors a JOIN actor_human_person h ON h.actor_id = a.actor_id "
        "WHERE a.actor_id = ? AND a.actor_type = 'human_person'",
        (actor_id,),
    ).fetchone()
    return row is not None


def _derive_act_event_id(payload):
    return derive_stable_id(
        "family_principal_event",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _insert_canonical_act(conn, *, event_id, principal_actor_id, actor_role,
                          event_kind, display_label, requester_actor_id,
                          occurred_at, record_created_at, source_ref):
    conn.execute(
        "INSERT INTO family_principal_events "
        "(event_id, group_key, principal_actor_id, actor_role, event_kind, "
        " display_label, requester_actor_id, occurred_at, record_created_at, source_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, GROUP_KEY_FAMILY, principal_actor_id, actor_role, event_kind,
            display_label, requester_actor_id, occurred_at, record_created_at, source_ref,
        ),
    )


def _set_projection(conn, *, actor_id, actor_role, status, display_label,
                    enrolled_at, deactivated_at, source_event_id, updated_at):
    conn.execute(
        "INSERT OR REPLACE INTO family_membership_state "
        "(actor_id, group_key, actor_role, status, display_label, enrolled_at, "
        " deactivated_at, source_event_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            actor_id, GROUP_KEY_FAMILY, actor_role, status, display_label,
            enrolled_at, deactivated_at, source_event_id, updated_at,
        ),
    )


def designate_owner(conn, *, principal_actor_id, display_label=None,
                    requester_actor_id=None, source_ref=None,
                    _test_inject_failure_after=None):
    """Host-mediated bootstrap act naming the single permanent family
    owner. requester_actor_id must equal the production host_system
    actor (hir1_registration.HOST_ACTOR_ID). The principal must already
    be a canonical human_person actor -- family acts never register
    humans. Deterministic act event_id makes a replayed identical act
    return the existing event_id instead of writing a duplicate (idempotent
    replay). Runs in one transaction with its own projection update."""
    _require_family_tables(conn)
    now = int(time.time())
    if requester_actor_id is None:
        raise FamilyMembershipError(
            "designate_owner requires an explicit host_system requester_actor_id"
        )
    requester = requester_actor_id
    event_id = _derive_act_event_id({
        "group_key": GROUP_KEY_FAMILY,
        "event_kind": EVENT_KIND_DESIGNATE_OWNER,
        "principal_actor_id": principal_actor_id,
        "requester_actor_id": requester,
        "occurred_at": now,
    })
    existing = conn.execute(
        "SELECT event_id FROM family_principal_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        return existing[0]

    if requester != HOST_ACTOR_ID or not _is_host_system_actor(conn, requester):
        raise FamilyMembershipError(
            "designate_owner is a host-mediated act; requester must be the host_system actor"
        )
    if not _is_registered_human(conn, principal_actor_id):
        raise FamilyMembershipError(
            "designate_owner principal must already be a registered human_person actor"
        )
    if designated_owner_actor_id(conn) is not None:
        raise FamilyMembershipError(
            "owner already designated; V0 owner permanence forbids re-designation"
        )

    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        _insert_canonical_act(
            conn, event_id=event_id, principal_actor_id=principal_actor_id,
            actor_role=ACTOR_ROLE_OWNER, event_kind=EVENT_KIND_DESIGNATE_OWNER,
            display_label=display_label, requester_actor_id=requester,
            occurred_at=now, record_created_at=now, source_ref=source_ref,
        )
        if _test_inject_failure_after == "canonical_insert":
            raise RuntimeError("injected test failure: canonical_insert")
        _set_projection(
            conn, actor_id=principal_actor_id, actor_role=ACTOR_ROLE_OWNER,
            status="active", display_label=display_label, enrolled_at=now,
            deactivated_at=None, source_event_id=event_id, updated_at=now,
        )
    return event_id


def enroll_family_member(conn, *, principal_actor_id, display_label,
                         requester_actor_id=None, source_ref=None,
                         _test_inject_failure_after=None):
    """Owner-gated act enrolling an already-registered human as a family
    member (no chain: requester MUST be the current designated owner).
    A principal may hold exactly one family membership record in V0 --
    re-enrollment after deactivation is refused (fail closed)."""
    _require_family_tables(conn)
    owner = designated_owner_actor_id(conn)
    if owner is None:
        raise FamilyMembershipError(
            "enroll is owner-gated; no designated owner exists yet"
        )
    if not _canonical_family_history_is_valid(conn, owner):
        raise FamilyMembershipError(
            "canonical family history is contradictory or unauthorized; refusing enrollment"
        )
    if requester_actor_id is None:
        raise FamilyMembershipError(
            "enroll requires an explicit designated-owner requester_actor_id"
        )
    requester = requester_actor_id
    if requester != owner:
        raise FamilyMembershipError(
            "enroll is owner-gated: requester must be the current designated owner"
        )
    if not _is_registered_human(conn, principal_actor_id):
        raise FamilyMembershipError(
            "enroll principal must already be a registered human_person actor"
        )
    if principal_actor_id == owner:
        raise FamilyMembershipError("the owner cannot be enrolled as a family member")
    if _latest_act_row(conn, principal_actor_id) is not None:
        raise FamilyMembershipError(
            "principal already has a family membership record; V0 allows a single enroll"
        )

    now = int(time.time())
    event_id = _derive_act_event_id({
        "group_key": GROUP_KEY_FAMILY,
        "event_kind": EVENT_KIND_ENROLL,
        "principal_actor_id": principal_actor_id,
        "requester_actor_id": requester,
        "occurred_at": now,
    })
    existing_event = conn.execute(
        "SELECT event_id FROM family_principal_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing_event is not None:
        return existing_event[0]

    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        _insert_canonical_act(
            conn, event_id=event_id, principal_actor_id=principal_actor_id,
            actor_role=ACTOR_ROLE_FAMILY_MEMBER, event_kind=EVENT_KIND_ENROLL,
            display_label=display_label, requester_actor_id=requester,
            occurred_at=now, record_created_at=now, source_ref=source_ref,
        )
        if _test_inject_failure_after == "canonical_insert":
            raise RuntimeError("injected test failure: canonical_insert")
        _set_projection(
            conn, actor_id=principal_actor_id, actor_role=ACTOR_ROLE_FAMILY_MEMBER,
            status="active", display_label=display_label, enrolled_at=now,
            deactivated_at=None, source_event_id=event_id, updated_at=now,
        )
    return event_id


def deactivate_family_member(conn, *, principal_actor_id,
                             requester_actor_id=None, source_ref=None,
                             _test_inject_failure_after=None):
    """Owner-gated act deactivating an enrolled, currently-active family
    member. Requires the canonical deactivate; a deactivated principal
    falls out of active_family_principals() (all future scoped delivery
    and re-validation of stale sessions fails closed)."""
    _require_family_tables(conn)
    owner = designated_owner_actor_id(conn)
    if owner is None:
        raise FamilyMembershipError(
            "deactivate is owner-gated; no designated owner exists yet"
        )
    if not _canonical_family_history_is_valid(conn, owner):
        raise FamilyMembershipError(
            "canonical family history is contradictory or unauthorized; refusing deactivation"
        )
    if requester_actor_id is None:
        raise FamilyMembershipError(
            "deactivate requires an explicit designated-owner requester_actor_id"
        )
    requester = requester_actor_id
    if requester != owner:
        raise FamilyMembershipError(
            "deactivate is owner-gated: requester must be the current designated owner"
        )
    current = _latest_act_row(conn, principal_actor_id)
    if current is None or current[0] != EVENT_KIND_ENROLL:
        raise FamilyMembershipError(
            "principal is not a currently-active family member"
        )
    _current_kind, current_display_label, enrolled_at, _current_event_id = current

    now = int(time.time())
    event_id = _derive_act_event_id({
        "group_key": GROUP_KEY_FAMILY,
        "event_kind": EVENT_KIND_DEACTIVATE,
        "principal_actor_id": principal_actor_id,
        "requester_actor_id": requester,
        "occurred_at": now,
    })
    existing_event = conn.execute(
        "SELECT event_id FROM family_principal_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing_event is not None:
        return existing_event[0]

    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        _insert_canonical_act(
            conn, event_id=event_id, principal_actor_id=principal_actor_id,
            actor_role=ACTOR_ROLE_FAMILY_MEMBER, event_kind=EVENT_KIND_DEACTIVATE,
            display_label=None, requester_actor_id=requester,
            occurred_at=now, record_created_at=now, source_ref=source_ref,
        )
        if _test_inject_failure_after == "canonical_insert":
            raise RuntimeError("injected test failure: canonical_insert")
        _set_projection(
            conn, actor_id=principal_actor_id, actor_role=ACTOR_ROLE_FAMILY_MEMBER,
            status="deactivated", display_label=current_display_label,
            enrolled_at=enrolled_at, deactivated_at=now,
            source_event_id=event_id, updated_at=now,
        )
    return event_id


def refresh_projection(conn):
    """Rebuilds family_membership_state from the canonical ledger. Best
    effort for cache consistency -- all authority-bearing reads consult
    the canonical ledger directly, so a stale projection can never widen
    delivery."""
    _require_family_tables(conn)
    rows = conn.execute(
        "SELECT principal_actor_id, event_kind, display_label, occurred_at, event_id "
        "FROM family_principal_events ORDER BY rowid",
    ).fetchall()
    with conn:
        conn.execute("DELETE FROM family_membership_state")
        now = int(time.time())
        projected = {}
        for principal_actor_id, event_kind, display_label, occurred_at, event_id in rows:
            deactivated = event_kind == EVENT_KIND_DEACTIVATE
            actor_role = (
                ACTOR_ROLE_OWNER if event_kind == EVENT_KIND_DESIGNATE_OWNER
                else ACTOR_ROLE_FAMILY_MEMBER
            )
            prior = projected.get(principal_actor_id)
            effective_label = display_label if display_label is not None else (
                prior["display_label"] if prior is not None else None
            )
            enrolled_at = (
                prior["enrolled_at"] if prior is not None else occurred_at
            )
            _set_projection(
                conn, actor_id=principal_actor_id, actor_role=actor_role,
                status="deactivated" if deactivated else "active",
                display_label=effective_label, enrolled_at=enrolled_at,
                deactivated_at=occurred_at if deactivated else None,
                source_event_id=event_id, updated_at=now,
            )
            projected[principal_actor_id] = {
                "display_label": effective_label,
                "enrolled_at": enrolled_at,
            }
    return _canonical_state_summary(conn)


def _canonical_state_summary(conn):
    latest = _latest_act_per_principal(conn)
    summary = {}
    for actor_id, kind in sorted(latest.items()):
        summary[actor_id] = {
            "role": ACTOR_ROLE_OWNER if kind == EVENT_KIND_DESIGNATE_OWNER
            else ACTOR_ROLE_FAMILY_MEMBER,
            "status": "deactivated" if kind == EVENT_KIND_DEACTIVATE else "active",
        }
    return summary


def family_state_summary(conn):
    """Read-only. Canonical-ledger-derived current family state, useful
    for tests and diagnostics. Never reads the projection cache."""
    return {
        "group_key": GROUP_KEY_FAMILY,
        "designated_owner_actor_id": designated_owner_actor_id(conn),
        "active_family_principal_ids": sorted(active_family_principals(conn)),
        "principals": _canonical_state_summary(conn),
    }


def default_bound_scope(db_path, actor_id, configured_scope):
    """Once the family feature exists, an UNSCOPED session is identity-less for delivery purposes and receives
    no continuity at all (FS1 fail-closed law).  The owner's own Mac window is an ordinary owner-private
    session, so when no scope was configured AND the bound human is a currently-active family principal, the
    session binds principal_private -- exactly what ANAXI_BOUND_VISIBILITY_SCOPE=principal_private would do.
    An explicit configuration is never overridden; before the family feature exists nothing changes."""
    if configured_scope or not actor_id or not os.path.exists(db_path):
        return configured_scope
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if (family_feature_used(conn)
                and actor_id in active_family_principals(conn)):
            return SCOPE_PRINCIPAL_PRIVATE
    finally:
        conn.close()
    return configured_scope
