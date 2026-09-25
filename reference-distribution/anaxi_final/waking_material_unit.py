"""SLP1-A4: read-only WMU (Waking Material Unit) assembly layer.

A WMU is a DETERMINISTIC, READ-ONLY PROVENANCE ENVELOPE OVER GENUINE
SELECTION-BEARING WAKING TRACE(S) AND THEIR MECHANICAL SUPPORT.

It is NOT a canonical memory, NOT a database write, NOT a Sleep
result, NOT a summary, NOT a salience score, NOT an autobiographical
claim.

This module performs ZERO writes of any kind, calls no model, and is
COMPLETELY UNWIRED from llama_sleep.py -- nothing in this codebase
imports it as of this gate.

Pure(-ish) function over a caller-supplied collection of already-
eligible SLEEP_EVIDENCE_V1 items (sleep_evidence.build_sleep_evidence_
v1()'s own `items` list -- every item here has ALREADY independently
passed sleep_evidence.is_sleep_eligible(), including that function's
own sibling/hash/auth verification). `prov_conn` (read-only) is used
ONLY for the single, narrowly-allowlisted cross-event support-linkage
resolution described below -- never for independent eligibility
re-derivation, never for a second, broader query, and never to
traverse into any event/component kind outside the small explicit
allowlist this module names by its own literal string constants.

PRIMARY EVENT RULE (frozen, see final report -- SLP1-A4c correction 3
supersedes SLP1-A4's original X-primary proposal for the human-
initiated case):
  - Clark conversational expression from a `waking_turn` event X:
      - if X carries an explicit, positively-reverified
        `human_input_event_id` component naming a genuine
        `human_waking_input` event H -> primary_event_id = H, NOT X.
        Clark's later successful answer NEVER changes the identity an
        unanswered human address already established.
      - otherwise -> primary_event_id = X (unchanged from SLP1-A4).
  - Genuine authenticated human conversational input from a
    `human_waking_input` event H -> primary_event_id = H, ALWAYS --
    whether or not any waking_turn ever links back to it. An
    unanswered address (Clark never replied) forms its own, complete,
    stable H-primary WMU.
  - Clark public roaming journal expression (memory_kind
    CLARK_EXPRESSION arising from a `workspace_roaming_public_action`
    event) -> primary_event_id = that same event_id, unchanged.
  - A delivered-resource-trace (memory_kind DELIVERED_RESOURCE_TRACE)
    whose resource-encounter event Y carries an explicit, positively-
    reverified `triggering_waking_event_id` component naming a genuine
    `waking_turn` event X -> attaches to X's WMU -- UNLESS X itself
    resolves onward to an H (per the rule above), in which case Y
    attaches to H's WMU instead, through the fully explicit relation
    chain Y -> X -> H, with EVERY hop positively revalidated as the
    expected PUBLIC event_type before being trusted (Task 12/13). No
    hop is ever inferred from timestamp/actor/model coincidence.
  - A delivered-resource-trace with no such backlink (or an
    unverifiable/wrong-type target at any hop) forms its OWN resource-
    primary WMU, keyed by its own event_id -- legal only because it
    already independently passed sleep_evidence eligibility (proving
    genuine delivery).

`wmu_id` is a deterministic function of `primary_event_id` ALONE.
Supporting provenance NEVER affects `wmu_id` -- discovering a new
support link later (e.g. a backlink written after this module's
caller already built a WMU) never mints a second identity for the
same primary event, and never requires an already-published WMU's
identity to change.
"""
import hashlib
from typing import Optional

import sleep_evidence as se

_EVENT_TYPE_WAKING_TURN = "waking_turn"
_EVENT_TYPE_RESOURCE_ENCOUNTER = "workspace_resource_encounter"
_EVENT_TYPE_PUBLIC_ACTION = "workspace_roaming_public_action"
_EVENT_TYPE_HUMAN_WAKING_INPUT = "human_waking_input"

_COMPONENT_KIND_TRIGGERING_WAKING_EVENT_ID = "triggering_waking_event_id"
_COMPONENT_KIND_RESOURCE_ENCOUNTER_FACT = "resource_encounter_fact"
_COMPONENT_KIND_HUMAN_INPUT_EVENT_ID = "human_input_event_id"

# Task 12 (safe support traversal): the ONLY component_kinds this
# module will ever attach as SAME-EVENT supporting provenance for an
# authored-waking-expression primary (a conversational waking_turn or
# a public roaming_journal_text action) -- an explicit allowlist, never
# "everything else sharing this event_id." A sibling component whose
# kind is not in this set is simply never attached, regardless of what
# it is -- Task 12's "unknown destination kind -> do not attach" applies
# even within the SAME event, not only across events. SLP1-A4c adds
# 'human_input_event_id' -- Clark's own waking_turn X's mechanical
# backlink to the human_waking_input event H it answers -- so that
# link is visible as supporting provenance under whichever WMU it ends
# up attached to (H's, per the primary-event rule above).
_SAME_EVENT_SUPPORT_COMPONENT_KINDS = frozenset({
    "roaming_action_fact", "roaming_external_result", "workspace_roaming_run_id",
    "bounded_clause", "waking_interaction_mode", "episode_context_delivered",
    "active_workspace_event_delivered", "human_input_event_id",
})

WAKING_MATERIAL_CLASS_AUTHORED_EXPRESSION = "authored_waking_expression"
WAKING_MATERIAL_CLASS_DELIVERED_RESOURCE_TRACE = "delivered_replayable_resource_trace"


class WmuAssemblyError(Exception):
    """Raised when the supplied evidence cannot be safely assembled --
    e.g. an incomplete/gapped/duplicate segment set for a selection-
    bearing canonical component, or an item carrying a memory_kind this
    module has no assembly rule for. Fail closed: this module never
    emits a WMU built from partial or unrecognized evidence."""


def _derive_wmu_id(primary_event_id: str) -> str:
    """Deterministic, domain-separated, length-prefixed SHA-256 id --
    same shape/convention as provenance_schema.derive_stable_id() and
    sleep_evidence._derive_segment_id(), reimplemented locally for the
    same reason sleep_evidence's own segment-id function is: keeping
    this module's own dependency surface small and auditable rather
    than reaching into another module's internals for one pure
    function. A pure function of `primary_event_id` alone -- no
    supporting-provenance field of any kind ever participates in this
    computation, by construction (there is nothing else in this
    function's own signature to participate)."""
    domain = b"anaxi-wmu-id-v1"
    event_id_bytes = primary_event_id.encode("utf-8")
    preimage = domain + b"|" + len(event_id_bytes).to_bytes(8, "big") + event_id_bytes
    digest = hashlib.sha256(preimage).hexdigest()
    return f"wmu-{digest[:26]}"


def _event_type_of(prov_conn, event_id: str) -> Optional[str]:
    """Read-only. The one query this module uses to distinguish which
    primary-event rule applies to an authored-expression component
    (SLP1-A4c correction 3 needs this: memory_kind alone no longer
    disambiguates a waking_turn's Clark prose from a human_waking_
    input's own human expression, both of which classify as either
    CLARK_EXPRESSION or HUMAN_EXPRESSION depending on actor type)."""
    row = prov_conn.execute("SELECT event_type FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return row[0] if row is not None else None


def _resolve_actor_type(prov_conn, actor_id: Optional[str]) -> Optional[str]:
    """Read-only. Deliberately NOT imported from sleep_evidence's own
    (private) helper of the same shape -- see this module's own
    docstring on keeping cross-module reach-in minimal; three lines is
    cheaper than a dependency."""
    if actor_id is None:
        return None
    row = prov_conn.execute("SELECT actor_type FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()
    return row[0] if row is not None else None


def _verify_complete_segment_set(items_for_component: list) -> bool:
    """SLP1-A4 Task 13: a canonical component's segment set is complete
    iff its segment_index values are EXACTLY 0..N-1 (no gap, no
    duplicate) and exactly one segment carries is_final_segment=True,
    and that one is the last in index order. No semantic joining --
    purely a check on the mechanical index/flag fields sleep_evidence.py
    already attaches to every item."""
    indexes = sorted(i["segment_index"] for i in items_for_component)
    if indexes != list(range(len(indexes))):
        return False  # gap or duplicate segment_index
    final_flags = [i["is_final_segment"] for i in items_for_component]
    if sum(1 for f in final_flags if f) != 1:
        return False  # exactly one final segment required, never zero, never more than one
    ordered = sorted(items_for_component, key=lambda i: i["segment_index"])
    return bool(ordered[-1]["is_final_segment"])


def _reconstruct_component(items_for_component: list) -> dict:
    """Reconstructs one canonical component's full expression and
    provenance from its (already verified complete) segment list, in
    exact source order -- no semantic joining, pure concatenation."""
    ordered = sorted(items_for_component, key=lambda i: i["segment_index"])
    first = ordered[0]
    return {
        "component_id": first["component_id"],
        "event_id": first["event_id"],
        "memory_kind": first["memory_kind"],
        "speaker_actor_id": first["speaker_actor_id"],
        "attribution_status": first["attribution_status"],
        "authentication_status": first["authentication_status"],
        "pipeline_id": first["pipeline_id"],
        "model_revision_id": first["model_revision_id"],
        "timestamp": first["timestamp"],
        "segments": [
            {
                "segment_id": i["segment_id"], "segment_index": i["segment_index"],
                "char_start": i["char_start"], "char_end": i["char_end"], "expression": i["expression"],
            }
            for i in ordered
        ],
        "expression": "".join(i["expression"] for i in ordered),
    }


def _resolve_waking_turn_primary(prov_conn, waking_turn_event_id: str) -> str:
    """SLP1-A4c correction 3: if this `waking_turn` event X carries an
    explicit `human_input_event_id` component naming a genuine
    `human_waking_input` event H, the WMU's primary_event_id is H, not
    X -- Clark's later successful answer must never change the
    identity an unanswered human address already established. The
    named target is POSITIVELY RE-VERIFIED to resolve to a real
    `events` row whose `event_type` is exactly `human_waking_input`
    before being trusted (Task 12/13: every hop revalidated, never
    inferred). An absent, unresolvable, or wrong-type target means X
    remains its own primary (current A4 behavior for a waking_turn
    with no authorized human-input event)."""
    row = prov_conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (waking_turn_event_id, _COMPONENT_KIND_HUMAN_INPUT_EVENT_ID),
    ).fetchone()
    if row is None:
        return waking_turn_event_id
    target_event_id = row[0]
    target_row = prov_conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (target_event_id,),
    ).fetchone()
    if target_row is None or target_row[0] != _EVENT_TYPE_HUMAN_WAKING_INPUT:
        return waking_turn_event_id
    return target_event_id


def _resolve_resource_primary(prov_conn, resource_event_id: str) -> str:
    """SLP1-A4 Tasks 9/11/12, extended by SLP1-A4c correction 3 for the
    Y -> X -> H chain: resolve which WMU a delivered-resource-trace's
    own event attaches to.

    Looks ONLY for a `triggering_waking_event_id` component in the SAME
    event -- an explicit, already-proven stored relation (SLP1-A3),
    never timestamp/actor/model-revision/path coincidence. If present,
    the named target event_id is POSITIVELY RE-VERIFIED to resolve to a
    real `events` row whose `event_type` is exactly `waking_turn`
    before being trusted (Task 12: safe support traversal -- a link's
    mere existence is not itself proof its destination is a safe/
    allowed shape). If that waking_turn event X itself resolves onward
    to an authorized human_waking_input event H (via
    _resolve_waking_turn_primary(), the SAME positively-revalidated
    rule), Y attaches to H's WMU instead -- the full chain Y -> X -> H,
    every hop explicit and revalidated, never a single "attach to
    whatever X currently is" shortcut. An absent, unresolvable, or
    wrong-type target at either hop means this resource encounter is
    NOT attached to anything else; it forms its own resource-primary
    WMU instead (legal only because the caller already established --
    via sleep_evidence.is_sleep_eligible()'s own sibling verification --
    that this encounter was genuinely delivered)."""
    row = prov_conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (resource_event_id, _COMPONENT_KIND_TRIGGERING_WAKING_EVENT_ID),
    ).fetchone()
    if row is None:
        return resource_event_id
    target_event_id = row[0]
    target_row = prov_conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (target_event_id,),
    ).fetchone()
    if target_row is None or target_row[0] != _EVENT_TYPE_WAKING_TURN:
        return resource_event_id
    return _resolve_waking_turn_primary(prov_conn, target_event_id)


def _attach_resource_support(prov_conn, wmu: dict, resource_event_id: str) -> None:
    """Appends bounded, purely-factual supporting-provenance entries
    for one resource encounter to `wmu` in place -- the mechanical fact
    itself and (if present) the triggering-link component. Both are
    fetched by an explicit component_kind match within the SAME
    resource_event_id -- no wildcard scan, no traversal beyond these
    two named kinds (Task 12: explicit support-event/component
    allowlist, never an open-ended query)."""
    fact_row = prov_conn.execute(
        "SELECT component_id, component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (resource_event_id, _COMPONENT_KIND_RESOURCE_ENCOUNTER_FACT),
    ).fetchone()
    if fact_row is not None:
        wmu["supporting_provenance"].append({
            "support_event_id": resource_event_id,
            "support_component_id": fact_row[0],
            "support_component_kind": _COMPONENT_KIND_RESOURCE_ENCOUNTER_FACT,
            "linkage_type": "same_event_id",
            "resource_encounter_fact_json": fact_row[1],
        })
    link_row = prov_conn.execute(
        "SELECT component_id, component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (resource_event_id, _COMPONENT_KIND_TRIGGERING_WAKING_EVENT_ID),
    ).fetchone()
    if link_row is not None:
        wmu["supporting_provenance"].append({
            "support_event_id": resource_event_id,
            "support_component_id": link_row[0],
            "support_component_kind": _COMPONENT_KIND_TRIGGERING_WAKING_EVENT_ID,
            "linkage_type": "triggering_waking_event_id",
            "triggering_waking_event_id": link_row[1],
        })


def _attach_same_event_support(prov_conn, wmu: dict, event_id: str, exclude_component_id: int) -> None:
    """Task 12/21: attaches every sibling component in `event_id` whose
    component_kind is in the explicit _SAME_EVENT_SUPPORT_COMPONENT_KINDS
    allowlist (e.g. a public journal action's own roaming_action_fact)
    as supporting provenance -- never as a second selection-bearing
    entry. `exclude_component_id` is the primary selection-bearing
    component itself, never re-attached as its own support."""
    rows = prov_conn.execute(
        "SELECT component_id, component_kind, component_text FROM event_components "
        "WHERE event_id = ? AND component_id != ? ORDER BY sequence",
        (event_id, exclude_component_id),
    ).fetchall()
    for support_component_id, support_component_kind, support_component_text in rows:
        if support_component_kind not in _SAME_EVENT_SUPPORT_COMPONENT_KINDS:
            continue  # unknown/unlisted kind -> never attached, even within the same event
        wmu["supporting_provenance"].append({
            "support_event_id": event_id,
            "support_component_id": support_component_id,
            "support_component_kind": support_component_kind,
            "linkage_type": "same_event_id",
            "support_text": support_component_text,
        })


def _selection_entry(prov_conn, comp: dict, waking_material_class: str) -> dict:
    return {
        "event_id": comp["event_id"],
        "component_id": comp["component_id"],
        "waking_material_class": waking_material_class,
        "memory_kind": comp["memory_kind"],
        "actor_id": comp["speaker_actor_id"],
        "actor_type": _resolve_actor_type(prov_conn, comp["speaker_actor_id"]),
        "attribution_status": comp["attribution_status"],
        "authentication_status": comp["authentication_status"],
        "pipeline_id": comp["pipeline_id"],
        "model_revision_id": comp["model_revision_id"],
        "timestamp": comp["timestamp"],
        "segments": comp["segments"],
        "expression": comp["expression"],
        # Every item reaching this module has already passed
        # sleep_evidence.is_sleep_eligible()'s own content-hash
        # verification (build_sleep_evidence_v1() raises
        # SleepEvidenceIntegrityError before ever emitting an
        # unverified item) -- this field records that fact, never a
        # fresh check performed here.
        "integrity_status": "verified",
    }


def _get_or_create_wmu(wmus: dict, primary_event_id: str) -> dict:
    if primary_event_id not in wmus:
        wmus[primary_event_id] = {
            "wmu_id": _derive_wmu_id(primary_event_id),
            "primary_event_id": primary_event_id,
            "selection_bearing": [],
            "supporting_provenance": [],
        }
    return wmus[primary_event_id]


def snapshot_component_groups(prov_conn, through_component_id: int) -> dict:
    """Conservative public candidate membership, without reading expressions.

    Used only to close a source window before eligibility/assembly. The same
    positive tuple allowlist and primary-link resolvers govern both paths.
    Auth/hash/sibling eligibility is still checked by sleep_evidence; a
    candidate here never grants admission to Selection.
    """
    clauses = []
    params = []
    for event_type, component_kind, actor_type in sorted(se.ELIGIBLE_SELECTION_TUPLES):
        clauses.append("(e.event_type = ? AND c.component_kind = ? AND a.actor_type = ?)")
        params.extend((event_type, component_kind, actor_type))
    rows = prov_conn.execute(
        "SELECT c.component_id, c.event_id, e.event_type FROM event_components c "
        "JOIN events e ON e.event_id = c.event_id "
        "JOIN actors a ON a.actor_id = c.creator_actor_id "
        "WHERE c.component_id <= ? AND (" + " OR ".join(clauses) + ") "
        "ORDER BY c.component_id", (through_component_id, *params),
    )
    groups = {}
    primary_cache = {}
    for component_id, event_id, event_type in rows:
        if event_id not in primary_cache:
            if event_type == _EVENT_TYPE_WAKING_TURN:
                primary = _resolve_waking_turn_primary(prov_conn, event_id)
            elif event_type == _EVENT_TYPE_RESOURCE_ENCOUNTER:
                primary = _resolve_resource_primary(prov_conn, event_id)
            else:
                primary = event_id
            primary_cache[event_id] = primary
        groups.setdefault(primary_cache[event_id], []).append(component_id)
    return groups


def assemble_wmus(prov_conn, evidence_items: list) -> list:
    """The one public entry point. `evidence_items` is exactly the
    `items` list `sleep_evidence.build_sleep_evidence_v1()` returns (or
    a caller-accumulated union of several such calls' `items` lists,
    once every included canonical component's segments are complete --
    see Task 13/module docstring; this function does NOT itself track
    accumulation across calls). Returns a list of WMU dicts.

    Fails closed (raises WmuAssemblyError) rather than emit a
    misleading partial WMU: if ANY included canonical component's
    segment set is incomplete, gapped, or duplicated, the WHOLE call
    raises -- never silently drop just that component and proceed with
    the rest. A caller must supply only fully-accumulated evidence."""
    by_component: dict = {}
    for item in evidence_items:
        by_component.setdefault(item["component_id"], []).append(item)

    components = {}
    for component_id, items_for_component in by_component.items():
        if not _verify_complete_segment_set(items_for_component):
            raise WmuAssemblyError(
                f"component_id={component_id!r}: incomplete/gapped/duplicate segment set -- "
                f"refusing to assemble a WMU from partial evidence."
            )
        components[component_id] = _reconstruct_component(items_for_component)

    wmus: dict = {}
    # Dedup guard: attach same-event/resource support at most once per
    # source event_id, even if (unusually) more than one selection-
    # bearing component in this batch shares that event_id.
    support_attached_for_event: set = set()

    for component_id, comp in components.items():
        memory_kind = comp["memory_kind"]
        if memory_kind in (se.MEMORY_KIND_CLARK_EXPRESSION, se.MEMORY_KIND_HUMAN_EXPRESSION):
            event_type = _event_type_of(prov_conn, comp["event_id"])
            if event_type == _EVENT_TYPE_HUMAN_WAKING_INPUT:
                # SLP1-A4c correction 3: H is ALWAYS its own primary,
                # answered or not.
                primary_event_id = comp["event_id"]
            elif event_type == _EVENT_TYPE_WAKING_TURN:
                # May resolve onward to H if X explicitly links to one.
                primary_event_id = _resolve_waking_turn_primary(prov_conn, comp["event_id"])
            elif event_type == _EVENT_TYPE_PUBLIC_ACTION:
                # Clark's public roaming journal expression -- unchanged.
                primary_event_id = comp["event_id"]
            else:
                raise WmuAssemblyError(
                    f"component_id={component_id!r}: memory_kind {memory_kind!r} arose from an "
                    f"unexpected event_type {event_type!r} -- no WMU assembly rule for this "
                    f"combination; refusing to guess a primary."
                )
            wmu = _get_or_create_wmu(wmus, primary_event_id)
            wmu["selection_bearing"].append(
                _selection_entry(prov_conn, comp, WAKING_MATERIAL_CLASS_AUTHORED_EXPRESSION)
            )
            if comp["event_id"] not in support_attached_for_event:
                _attach_same_event_support(prov_conn, wmu, comp["event_id"], exclude_component_id=comp["component_id"])
                support_attached_for_event.add(comp["event_id"])
        elif memory_kind == se.MEMORY_KIND_DELIVERED_RESOURCE_TRACE:
            primary_event_id = _resolve_resource_primary(prov_conn, comp["event_id"])
            wmu = _get_or_create_wmu(wmus, primary_event_id)
            wmu["selection_bearing"].append(
                _selection_entry(prov_conn, comp, WAKING_MATERIAL_CLASS_DELIVERED_RESOURCE_TRACE)
            )
            if comp["event_id"] not in support_attached_for_event:
                _attach_resource_support(prov_conn, wmu, comp["event_id"])
                support_attached_for_event.add(comp["event_id"])
        else:
            raise WmuAssemblyError(
                f"component_id={component_id!r}: memory_kind {memory_kind!r} has no WMU assembly rule -- "
                f"refusing to guess."
            )

    return list(wmus.values())
