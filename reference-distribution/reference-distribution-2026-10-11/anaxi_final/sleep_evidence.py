"""SLP1-S1: canonical Sleep evidence boundary.

Builds SLEEP_EVIDENCE_V1 -- the ONLY thing a future authoritative SLP1
Sleep cycle may ever receive as evidence -- from canonical
`event_components` in `anaxi_provenance.db` only. Retires
`anaxi_log.jsonl` as an evidence source for new Sleep (the legacy file
and its loader are untouched; they simply are never called from here).

Central invariant (frozen):

    AUTHORITATIVE CANONICAL WAKING EXPRESSIONS
            -> SLEEP_EVIDENCE_V1
    everything else -X-> Sleep evidence

Eligibility is entirely mechanical -- no semantic classifier, no LLM
judge, no "is this important" heuristic. It depends only on
mechanically stored fields already present in canonical provenance:
`event_components.component_kind`/`authorship_resolution`/
`creator_actor_id`, `actors.actor_type`, and (for human expression
only) `events.auth_context_id` -> `auth_contexts.auth_state`.

Mechanical fact, established by direct inspection of the current
production schema and data (not assumed): today, no `component_kind`
in `event_components` ever resolves to a `human_person` creator --
`conversational_prose` is Clark's own generated speech (creator
resolves to `clark_agent`), `bounded_clause` is host-mechanical
(creator resolves to `host_system`), and `assembled_reply_unsplit_
historical` never has a creator at all (CHECK-constrained NULL). This
module's classifier is nonetheless written generically, by resolved
actor TYPE rather than by a hardcoded component_kind->memory_kind
table, so a future native-write-path component whose creator
genuinely resolves to `human_person` becomes eligible human_expression
automatically, with no code change here -- never by inventing or
guessing a creator this gate has no mechanical basis for.

SLP1-A3: SELECTION-BEARING eligibility (this module's own concern) is
gated by a positive `(event_type, component_kind, actor_type)` triple
allowlist (`ELIGIBLE_SELECTION_TUPLES`), not by `component_kind` alone.
SLP1-A found that a bare `component_kind` string is not, by itself,
positive proof that a component crossed a genuine, structurally-public
waking pathway -- `events.event_type` is a materially stronger signal
that was already sitting unused in this module's own joined query.
`bounded_clause` is deliberately absent from `ELIGIBLE_SELECTION_TUPLES`
entirely (it is host-mechanical and non-selection-bearing by
construction; it is never listed here merely so the actor-type check
can reject it afterward -- that would be the same overclaim this
correction exists to remove). Supporting-mechanical-provenance
eligibility (a separate, NOT-selection-bearing concern) is an
out-of-scope architecture surface for a future gate -- this module
does not implement it.

PATHWAYS, NOT ORGANS: this module does not import hippocampus_store.py
or hippocampus_retrieval.py, even though its actor-subtype-resolution
technique deliberately mirrors that module's own proven algorithm
(`_resolve_actor_subtype`/`_derive_authentication_status`). Sleep
evidence construction and hippocampal ingestion remain two
independent, separately-testable pathways over the same canonical
source, exactly as SLP0-D1's audit found them to already be for the
old Sleep design -- this gate preserves that separation rather than
introducing a new coupling.

Kardia, the legacy memory graph (`anaxi_mind_llama.db`), workspace/
private material, prior `derived_inference` records, and all
operational/control traces are unreachable from this module by
construction: it imports nothing beyond the stdlib plus
`provenance_schema` (for `derive_stable_id`, used only by
`SleepEvidencePaths`' production-default path convention -- not
imported here at all in fact, see below), and its only database
connection is a read-only one to `anaxi_provenance.db`.
"""
import dataclasses
import hashlib
import json
import os
import sqlite3
from typing import Optional


# ============================================================== paths =====


@dataclasses.dataclass(frozen=True)
class SleepEvidencePaths:
    provenance_db_path: str

    @staticmethod
    def production_defaults() -> "SleepEvidencePaths":
        """Relative to this file's own directory, never Path.home(),
        never a user-specific absolute path baked into behavioral
        logic -- the same convention every other production-defaults
        path in this project already follows."""
        base = os.path.dirname(os.path.abspath(__file__))
        return SleepEvidencePaths(provenance_db_path=os.path.join(base, "anaxi_provenance.db"))


class SleepEvidenceUnavailableError(Exception):
    """The canonical provenance database is missing or cannot be
    opened read-only at all. Fail closed -- never fall back to legacy
    anaxi_log.jsonl or any other source."""
    pass


class SleepEvidenceIntegrityError(Exception):
    """A canonical component's recomputed content hash disagrees with
    its own stored content_sha256, or its creator_actor_id does not
    resolve to any canonical actor. Never silently trusted."""
    pass


def _readonly_uri(path: str) -> str:
    import pathlib
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def _open_provenance_readonly(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise SleepEvidenceUnavailableError(f"canonical provenance database not found: {path!r}")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.OperationalError as e:
        raise SleepEvidenceUnavailableError(f"could not open canonical provenance database read-only: {path!r} ({e})") from e
    return conn


# ========================================================= eligibility ====


MEMORY_KIND_HUMAN_EXPRESSION = "human_expression"
MEMORY_KIND_CLARK_EXPRESSION = "clark_expression"
# Reserved, informational only -- no branch of this module's own
# selection-eligibility classifier below ever produces this value.
# Host-mechanical content is excluded by never appearing in
# ELIGIBLE_SELECTION_TUPLES at all, not by being classified into this
# kind and then separately rejected. Kept as a named constant only
# because a future, out-of-scope supporting-mechanical-provenance
# surface (WMU construction, not implemented in this gate) may want a
# stable label for host-authored material it collects for context
# rather than selection.
MEMORY_KIND_MECHANICAL_RECORD = "mechanical_record"
# SLP1-A4: a distinct waking-material class for a host-attributed
# component that is nonetheless SELECTION-BEARING -- the exact,
# genuinely-delivered replayable representation of a resource
# encounter (delivered text or a bounded acoustic measurement view).
# Its creator resolves to host_system (delivered/measured external
# content, never Clark's own authored expression, and never host
# semantic interpretation of it), but it must NOT be folded into
# MEMORY_KIND_MECHANICAL_RECORD -- that would silently make it
# non-selection-bearing again, exactly the misclassification the
# SLP1-A4 mandate calls out by name. Reachable ONLY for the two
# RESOURCE_ENCOUNTER_DELIVERED_*_COMPONENT_KIND kinds, and only after
# _verify_delivered_resource_trace_sibling() below positively confirms
# genuine delivery, modality agreement, and hash agreement against the
# sibling resource_encounter_fact in the SAME event.
MEMORY_KIND_DELIVERED_RESOURCE_TRACE = "delivered_resource_trace"
ELIGIBLE_MEMORY_KINDS = (
    MEMORY_KIND_HUMAN_EXPRESSION, MEMORY_KIND_CLARK_EXPRESSION, MEMORY_KIND_DELIVERED_RESOURCE_TRACE,
)

_HUMAN_PERSON_ACTOR_TYPE = "human_person"
_CLARK_AGENT_ACTOR_TYPE = "clark_agent"
_HOST_SYSTEM_ACTOR_TYPE = "host_system"

_EVENT_TYPE_WAKING_TURN = "waking_turn"
_EVENT_TYPE_PUBLIC_ACTION = "workspace_roaming_public_action"
_EVENT_TYPE_RESOURCE_ENCOUNTER = "workspace_resource_encounter"
_EVENT_TYPE_HUMAN_WAKING_INPUT = "human_waking_input"

_COMPONENT_KIND_CONVERSATIONAL_PROSE = "conversational_prose"
_COMPONENT_KIND_ROAMING_JOURNAL_TEXT = "roaming_journal_text"
_COMPONENT_KIND_ROAMING_RUN_ID_LINK = "workspace_roaming_run_id"
_COMPONENT_KIND_RESOURCE_ENCOUNTER_FACT = "resource_encounter_fact"
_COMPONENT_KIND_DELIVERED_TEXT = "resource_encounter_delivered_text"
_COMPONENT_KIND_DELIVERED_MEASUREMENT = "resource_encounter_delivered_measurement"
_COMPONENT_KIND_HUMAN_CONVERSATIONAL_INPUT = "human_conversational_input"

# SLP1-A4: which sibling-verified modality each delivered-resource-trace
# component_kind must agree with, in the resource_encounter_fact's own
# "modality" field -- see _verify_delivered_resource_trace_sibling().
_DELIVERED_RESOURCE_TRACE_EXPECTED_MODALITY = {
    _COMPONENT_KIND_DELIVERED_TEXT: "text",
    _COMPONENT_KIND_DELIVERED_MEASUREMENT: "audio",
}

# SLP1-A3 correction 3 / SLP1-A4 extension: the positive SELECTION-
# BEARING allowlist, keyed by the full (event_type, component_kind,
# actor_type) triple -- not by component_kind alone. `bounded_clause`
# and `resource_encounter_fact`/`triggering_waking_event_id` are
# deliberately absent in their entirety: all are host-mechanical and
# non-selection-bearing by construction, never listed here merely so a
# downstream check could reject them afterward (that would be exactly
# the overclaim SLP1-A3 correction 3 removed). This is an explicit
# positive-allow condition, never a blacklist of private names/paths --
# any triple not listed here is ineligible, regardless of how plausible
# it looks. Photographs have NO corresponding tuple at all: this
# architecture refuses pixel persistence by design (SLP1-A4 Task 6),
# so no photograph-modality resource-encounter component can ever be
# selection-bearing through this allowlist.
#
# ("waking_turn", "conversational_prose", "human_person") remains
# listed even though NO live production writer currently produces an
# `auth_state='authenticated'` human-attributed conversational
# component (native_provenance_writer.record_native_waking_turn()
# always writes auth_state='unknown' today) -- the existing, already-
# tested authenticated-human-expression code path below is preserved
# exactly, not loosened, and no writer for it is built in this gate
# either (SLP1-A4 found no positive live authentication authority --
# see waking_material_unit.py / SLP1-A4 final report for the source
# evidence).
#
# The two delivered-resource-trace tuples below are host-attributed
# (actor_type=host_system) but ARE selection-bearing -- classified as
# MEMORY_KIND_DELIVERED_RESOURCE_TRACE, never MEMORY_KIND_MECHANICAL_
# RECORD, and additionally gated by _verify_delivered_resource_trace_
# sibling() inside is_sleep_eligible() (allowlist membership alone is
# NOT sufficient for these two kinds -- positive sibling proof of
# genuine delivery/modality/hash agreement is also required).
# SLP1-A4c: genuine, positively-authenticated human conversational
# input finally has a live writer (human_session_binding.py) -- this
# tuple was reserved-but-unreachable through SLP1-A4; it now admits a
# real canonical shape. The mandatory `authentication_status ==
# "authenticated"` check below remains completely unweakened: this
# component is eligible ONLY when its own event's auth_context
# positively resolves to the exact human actor (see
# _derive_authentication_status() and the human_expression branch in
# is_sleep_eligible()) -- a malformed/mismatched actor or auth_context
# is ineligible exactly like any other human_expression candidate,
# never a bare event/component-kind admission.
ELIGIBLE_SELECTION_TUPLES = frozenset({
    (_EVENT_TYPE_WAKING_TURN, _COMPONENT_KIND_CONVERSATIONAL_PROSE, _CLARK_AGENT_ACTOR_TYPE),
    (_EVENT_TYPE_WAKING_TURN, _COMPONENT_KIND_CONVERSATIONAL_PROSE, _HUMAN_PERSON_ACTOR_TYPE),
    (_EVENT_TYPE_PUBLIC_ACTION, _COMPONENT_KIND_ROAMING_JOURNAL_TEXT, _CLARK_AGENT_ACTOR_TYPE),
    (_EVENT_TYPE_RESOURCE_ENCOUNTER, _COMPONENT_KIND_DELIVERED_TEXT, _HOST_SYSTEM_ACTOR_TYPE),
    (_EVENT_TYPE_RESOURCE_ENCOUNTER, _COMPONENT_KIND_DELIVERED_MEASUREMENT, _HOST_SYSTEM_ACTOR_TYPE),
    (_EVENT_TYPE_HUMAN_WAKING_INPUT, _COMPONENT_KIND_HUMAN_CONVERSATIONAL_INPUT, _HUMAN_PERSON_ACTOR_TYPE),
})


def _resolve_actor_type(prov_conn: sqlite3.Connection, actor_id: str) -> Optional[str]:
    """Read-only. Returns the canonical actor_type for actor_id, or
    None if it does not resolve to any canonical actor (never
    guessed)."""
    row = prov_conn.execute(
        "SELECT actor_type FROM actors WHERE actor_id = ?", (actor_id,)
    ).fetchone()
    return row[0] if row is not None else None


def _classify_selection_memory_kind(event_type: Optional[str], component_kind: str, actor_type: Optional[str]) -> Optional[str]:
    """Pure mechanical classification for SELECTION-BEARING eligibility
    only. Returns MEMORY_KIND_HUMAN_EXPRESSION / MEMORY_KIND_CLARK_EXPRESSION /
    MEMORY_KIND_DELIVERED_RESOURCE_TRACE, or None if the (event_type,
    component_kind, actor_type) triple is not in
    ELIGIBLE_SELECTION_TUPLES. No subject inference, no topic
    inference -- classification depends only on exact triple
    membership. NOTE: for the two delivered-resource-trace kinds, this
    function alone does NOT establish eligibility -- is_sleep_eligible()
    additionally requires _verify_delivered_resource_trace_sibling()
    to positively pass."""
    if (event_type, component_kind, actor_type) not in ELIGIBLE_SELECTION_TUPLES:
        return None
    if actor_type == _HUMAN_PERSON_ACTOR_TYPE:
        return MEMORY_KIND_HUMAN_EXPRESSION
    if actor_type == _CLARK_AGENT_ACTOR_TYPE:
        return MEMORY_KIND_CLARK_EXPRESSION
    if actor_type == _HOST_SYSTEM_ACTOR_TYPE and component_kind in _DELIVERED_RESOURCE_TRACE_EXPECTED_MODALITY:
        return MEMORY_KIND_DELIVERED_RESOURCE_TRACE
    return None  # unreachable given tuple membership above; never guessed


def _verify_delivered_resource_trace_sibling(
    prov_conn: sqlite3.Connection, event_id: str, component_kind: str, component_text: str,
) -> bool:
    """SLP1-A4 Task 7: positive sibling-fact corroboration, required in
    addition to allowlist membership for a delivered-resource-trace
    component. Requires the SAME event to also carry a
    resource_encounter_fact component whose own JSON positively proves:
    (a) genuinely_delivered is exactly True; (b) its modality agrees
    with the expected modality for this component_kind (text<->
    resource_encounter_delivered_text, audio<->resource_encounter_
    delivered_measurement); (c) its own content_sha256 agrees with this
    component's own recomputed text hash. Any missing, malformed, or
    disagreeing sibling -> False (ineligible/fail closed) -- never a
    guess, never partial credit, never a bare component-kind-string
    admission."""
    expected_modality = _DELIVERED_RESOURCE_TRACE_EXPECTED_MODALITY.get(component_kind)
    if expected_modality is None:
        return False
    row = prov_conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, _COMPONENT_KIND_RESOURCE_ENCOUNTER_FACT),
    ).fetchone()
    if row is None:
        return False
    try:
        fact = json.loads(row[0])
    except (ValueError, TypeError):
        return False
    if not isinstance(fact, dict):
        return False
    if fact.get("genuinely_delivered") is not True:
        return False
    if fact.get("modality") != expected_modality:
        return False
    expected_hash = fact.get("content_sha256")
    if not isinstance(expected_hash, str):
        return False
    if hashlib.sha256(component_text.encode("utf-8")).hexdigest() != expected_hash:
        return False
    return True


def _has_roaming_run_id_sibling(prov_conn: sqlite3.Connection, event_id: str) -> bool:
    """SLP1-A3 correction 3: structural corroboration for
    `roaming_journal_text` specifically. Confirmed by direct source
    inspection of workspace_episode_provenance.py (SLP1-A2): every
    genuine `roaming_journal_text` component is written by
    record_public_action(), whose `run_id` parameter has no default
    (a required keyword argument) and which always reaches
    _record_episode_event() with a real run_id -- and
    _record_episode_event() unconditionally writes exactly one
    'workspace_roaming_run_id' component in the SAME event whenever
    run_id is not None. This invariant is universal for every current
    writer of this component_kind; if that ever changes, this check
    must be revisited rather than silently weakened."""
    row = prov_conn.execute(
        "SELECT 1 FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, _COMPONENT_KIND_ROAMING_RUN_ID_LINK),
    ).fetchone()
    return row is not None


def _derive_authentication_status(prov_conn: sqlite3.Connection, event_id: str) -> str:
    """Mirrors hippocampus_store.py's own proven derivation
    (independently reimplemented, not imported -- see module
    docstring): events.auth_context_id -> auth_contexts.auth_state.
    Returns 'pre_authentication_layer', 'unauthenticated',
    'claimed_only', 'authenticated', or 'unknown'. Never guessed."""
    migration_row = prov_conn.execute(
        "SELECT migration_status FROM event_migration_status WHERE event_id = ?", (event_id,)
    ).fetchone()
    if migration_row is not None and migration_row[0] == "pre_authentication_layer":
        return "pre_authentication_layer"

    event_row = prov_conn.execute("SELECT auth_context_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if event_row is None or event_row[0] is None:
        return "unknown"
    auth_row = prov_conn.execute(
        "SELECT auth_state FROM auth_contexts WHERE auth_context_id = ?", (event_row[0],)
    ).fetchone()
    if auth_row is None:
        return "unknown"
    auth_state = auth_row[0]
    if auth_state in ("unauthenticated", "claimed_only", "authenticated"):
        return auth_state
    return "unknown"


def is_sleep_eligible(prov_conn: sqlite3.Connection, component_id: int) -> bool:
    """Deterministic, independently callable/testable eligibility
    predicate for exactly one canonical component. No semantic
    classifier, no LLM judge, no importance heuristic -- every branch
    below reads only mechanically stored fields.

    SLP1-A3: gated by the positive (event_type, component_kind,
    actor_type) triple allowlist (ELIGIBLE_SELECTION_TUPLES), not by
    component_kind alone -- see module docstring. Unknown/malformed
    tuples are ineligible by construction (absence from an allowlist),
    never by a name-matching blacklist.

    Excludes (see EXCLUDED_CLASSES_REPORT below for the complete,
    named list): mechanical_record, derived_inference (there is no
    such memory_kind reachable through event_components at all --
    canonical derived_inference components do not yet exist as of this
    gate, and even once they do, this module's classifier has no
    branch that could ever produce that memory_kind), mixed
    historical/unresolved-attribution material, unknown component
    kinds, unknown/malformed event types, and unauthenticated/
    claimed-only human material."""
    row = prov_conn.execute(
        "SELECT ec.component_kind, ec.authorship_resolution, ec.creator_actor_id, ec.event_id, "
        "ev.event_type, ec.component_text "
        "FROM event_components ec JOIN events ev ON ev.event_id = ec.event_id "
        "WHERE ec.component_id = ?",
        (component_id,),
    ).fetchone()
    if row is None:
        return False
    component_kind, authorship_resolution, creator_actor_id, event_id, event_type, component_text = row

    if authorship_resolution != "resolved":
        return False
    if creator_actor_id is None:
        return False

    actor_type = _resolve_actor_type(prov_conn, creator_actor_id)
    if actor_type is None:
        return False

    memory_kind = _classify_selection_memory_kind(event_type, component_kind, actor_type)
    if memory_kind not in ELIGIBLE_MEMORY_KINDS:
        return False

    if memory_kind == MEMORY_KIND_HUMAN_EXPRESSION:
        if _derive_authentication_status(prov_conn, event_id) != "authenticated":
            return False

    if component_kind == _COMPONENT_KIND_ROAMING_JOURNAL_TEXT:
        if not _has_roaming_run_id_sibling(prov_conn, event_id):
            return False

    if memory_kind == MEMORY_KIND_DELIVERED_RESOURCE_TRACE:
        # SLP1-A4 Task 7: allowlist membership alone is NOT sufficient
        # for a delivered-resource-trace component -- positive sibling
        # proof of genuine delivery/modality/hash agreement is also
        # required, or it fails closed (malformed/orphaned trace ->
        # ineligible, never a bare component-kind-string admission).
        if not _verify_delivered_resource_trace_sibling(prov_conn, event_id, component_kind, component_text):
            return False

    return True


# EXCLUDED_CLASSES_REPORT -- documentation only, not consulted by any
# eligibility check above (each exclusion is enforced structurally by
# the predicate itself, never by a name-matching list).
EXCLUDED_CLASSES_REPORT = (
    "mechanical_record", "derived_inference", "mixed_historical_expression",
    "unknown memory kind", "unresolved attribution", "unknown attribution",
    "unauthenticated/claimed-only human material",
    "conversation-direction operational traces", "workspace actions",
    "workspace action log", "workspace roaming trace", "last_workspace_observation",
    "workspace journal", "workspace library", "PDF reads", "music/photo activity",
    "last_private_observation", "workspace/private/", "Kardia", "legacy memory graph",
    "legacy anaxi_log.jsonl as evidence", "historical ambiguous speaker material",
)


# ======================================================= evidence bounds ==


# SLP1-A3 renames: with per-component segmentation, these bounds apply
# to emitted SEGMENTS/ITEMS, not distinct canonical components -- the
# old names (MAX_COMPONENTS_PER_BATCH / MAX_CHARS_PER_COMPONENT) became
# actively misleading once one component can emit many items. There
# are zero production callers of this module (confirmed, SLP1-A/A2),
# so accuracy is preferred over preserving the old names.
MAX_SEGMENTS_PER_BATCH = 20
MAX_CHARS_PER_SEGMENT = 2000
MAX_AGGREGATE_EVIDENCE_CHARS = 20000


class SleepEvidenceConfigurationError(Exception):
    """Raised when the evidence-builder's own bounds are internally
    impossible before any canonical row is ever scanned -- e.g. a
    single maximum-sized segment could never fit the aggregate cap.
    This is a caller/configuration defect, never a canonical-data
    condition, and is checked deterministically up front. A plain
    exception, not a bare `assert`, precisely because these values may
    be supplied dynamically by a caller/test rather than fixed at
    import time -- an `assert` would silently vanish under `python -O`
    and this is a correctness boundary, not a debug aid."""


def _validate_bounds(max_segments: int, max_chars_per_segment: int, max_aggregate_chars: int) -> None:
    if max_segments <= 0:
        raise SleepEvidenceConfigurationError(f"max_segments must be > 0, got {max_segments!r}")
    if max_chars_per_segment <= 0:
        raise SleepEvidenceConfigurationError(f"max_chars_per_segment must be > 0, got {max_chars_per_segment!r}")
    if max_aggregate_chars <= 0:
        raise SleepEvidenceConfigurationError(f"max_aggregate_chars must be > 0, got {max_aggregate_chars!r}")
    if max_chars_per_segment > max_aggregate_chars:
        raise SleepEvidenceConfigurationError(
            f"max_chars_per_segment ({max_chars_per_segment!r}) must be <= max_aggregate_chars "
            f"({max_aggregate_chars!r}) -- otherwise a single maximum-sized segment could never "
            f"fit any batch at all."
        )


# ====================================================== evidence builder ==


def _content_hash_verified(component_text: str, content_sha256: str) -> bool:
    return hashlib.sha256(component_text.encode("utf-8")).hexdigest() == content_sha256


def _derive_segment_id(component_id: int, segment_index: int) -> str:
    """Deterministic, domain-separated, length-prefixed SHA-256 id --
    same algorithm/shape as provenance_schema.derive_stable_id(), but
    reimplemented locally rather than imported. This module has a
    deliberately minimal stdlib-only import contract (see module
    docstring and test_sleep_evidence_module_imports_nothing_beyond_
    minimal_stdlib) that exists precisely so its dependency surface
    stays auditable at a glance; importing provenance_schema here
    would pull in a project module for the sole convenience of reusing
    one small pure function, which is exactly the kind of import this
    module's isolation test is designed to catch. A distinct domain
    tag (from provenance_schema's own) means no id from this function
    could ever collide with a provenance_schema-derived id even given
    identical raw parts. Pure function of (component_id, segment_index)
    alone -- no content, no clock, no random state -- so repeated
    calls with the same inputs always reproduce the same id."""
    domain = b"anaxi-sleep-evidence-segment-id-v1"
    component_id_bytes = str(component_id).encode("utf-8")
    segment_index_bytes = str(segment_index).encode("utf-8")
    preimage = (
        domain + b"|"
        + len(component_id_bytes).to_bytes(8, "big") + component_id_bytes + b"|"
        + len(segment_index_bytes).to_bytes(8, "big") + segment_index_bytes
    )
    digest = hashlib.sha256(preimage).hexdigest()
    return f"seg-{digest[:26]}"


def build_sleep_evidence_v1(
    prov_conn: sqlite3.Connection,
    *,
    after_component_id: int = 0,
    through_component_id: Optional[int] = None,
    max_segments: int = MAX_SEGMENTS_PER_BATCH,
    max_chars_per_segment: int = MAX_CHARS_PER_SEGMENT,
    max_aggregate_chars: int = MAX_AGGREGATE_EVIDENCE_CHARS,
    resume_component_id: Optional[int] = None,
    resume_segment_index: int = 0,
) -> dict:
    """Read-only, pure/stateless. Selects the oldest eligible bounded
    batch of canonical components strictly after `after_component_id`
    (the caller's current watermark position -- see sleep_watermark.py;
    this function never reads or writes the watermark itself, and
    implements no persistence of any kind). Ordering is
    `component_id ASC` -- a strictly monotonic, append-only canonical
    sequence, equivalent to canonical event order for the purpose of
    deterministic chronological batching without depending on
    occurred_at agreeing with insertion order across historical
    backfills.

    SLP1-A3 non-lossy segmentation: an eligible component whose text
    exceeds `max_chars_per_segment` is deterministically split into
    fixed-size, non-overlapping character-window segments (see
    _derive_segment_id) -- never a semantic "best span," never
    sentence-aware, never lossy. As many CONSECUTIVE segments of the
    current component are emitted as fit within the remaining
    `max_segments`/`max_aggregate_chars` budget; the batch STOPS
    ENTIRELY (does not advance to a later canonical component) at the
    first segment that cannot fit. Because processing is strictly in
    ascending component_id order and the batch always stops at the
    first item it cannot fully progress, AT MOST ONE canonical
    component can ever be partially emitted at any batch boundary --
    `pending_component_id` is therefore always a single optional value,
    never a set.

    SLP1-C `through_component_id` (optional, additive, pure): when
    given, constrains the existing ordered query to
    `component_id > after_component_id AND component_id <=
    through_component_id`. Callers use this to freeze a Sleep cycle's
    canonical source window against a snapshot of the maximum
    component_id taken at cycle start, so waking material that arrives
    AFTER the cycle has already begun can never join an
    already-started window. This does not redesign A's own
    segmentation/eligibility semantics at all -- it is exactly the same
    ordered query with one more inclusive upper bound.

    Resuming across calls: pass back `resume_component_id`/
    `resume_segment_index` (this function's own prior
    `pending_component_id`/`pending_next_segment_index`) to continue a
    component's segmentation without re-emitting already-delivered
    segments. This function stores no state of its own -- the caller
    (not implemented in this gate) is responsible for persisting these
    two values between calls. `after_component_id` should NOT be
    advanced past a component that still has a pending segment; the
    unchanged `component_id > after_component_id` filter naturally
    re-includes it on the next call.

    Returns:
        {
            "items": [...],                            # segment-level SLEEP_EVIDENCE_V1 dicts
            "component_ids_scanned": [...],             # DIAGNOSTIC ONLY -- see below
            "component_ids_evidence_complete": [...],   # see below -- NOT watermark authority
            "pending_component_id": <int or None>,
            "pending_next_segment_index": <int>,        # 0 when pending_component_id is None
        }

    `component_ids_scanned` is diagnostic/audit information only: every
    component_id whose row was fetched and reached the top of the
    loop this call, whether ineligible, fully emitted, or the one
    possibly-pending component. It confers no completion authority by
    itself and is never intended to be persisted anywhere.

    `component_ids_evidence_complete` -- READ THIS CAREFULLY, THIS IS
    NOT A WATERMARK-ADVANCE SIGNAL. Its exact, narrow meaning is:
    every selection-bearing segment of this canonical component has
    been emitted by THIS evidence builder for this batch/resumed
    evidence sequence, OR the canonical component was mechanically
    ineligible and therefore requires no selection-bearing emission at
    all. BUILDING EVIDENCE != PROCESSING EVIDENCE (frozen invariant).
    This layer knows only whether a representation was completely
    EMITTED into the evidence batch -- it does NOT know, and this
    field must never be read to imply, whether Clark-side Selection
    successfully received it, whether Selection acted on it, whether
    Transformation succeeded, whether canonical derived persistence
    succeeded, or whether the overall Sleep window completed. Any
    future Sleep-watermark mechanism (not implemented in this gate)
    must derive its own advance decision from the SUCCESS of those
    LATER stages, never from this field alone.

    Never fabricates an absent field; never summarizes, labels, or
    interprets content. Segments a component's text only at the
    explicit `max_chars_per_segment` boundary -- fixed-size character
    windows, no semantic boundary-seeking, no sentence detection, no
    dropped characters, no overlap. Concatenating every segment's
    `expression` for a given `component_id`, in `segment_index` order,
    always exactly reconstructs that component's original
    `component_text`."""
    _validate_bounds(max_segments, max_chars_per_segment, max_aggregate_chars)

    if through_component_id is None:
        rows = prov_conn.execute(
            "SELECT ec.component_id, ec.event_id, ec.sequence, ec.creator_actor_id, "
            "ec.component_kind, ec.authorship_resolution, ec.component_text, "
            "ec.content_sha256, ec.model_revision_id, "
            "ev.occurred_at, ev.pipeline_id, ev.auth_context_id, ev.event_type "
            "FROM event_components ec JOIN events ev ON ev.event_id = ec.event_id "
            "WHERE ec.component_id > ? ORDER BY ec.component_id ASC",
            (after_component_id,),
        ).fetchall()
    else:
        rows = prov_conn.execute(
            "SELECT ec.component_id, ec.event_id, ec.sequence, ec.creator_actor_id, "
            "ec.component_kind, ec.authorship_resolution, ec.component_text, "
            "ec.content_sha256, ec.model_revision_id, "
            "ev.occurred_at, ev.pipeline_id, ev.auth_context_id, ev.event_type "
            "FROM event_components ec JOIN events ev ON ev.event_id = ec.event_id "
            "WHERE ec.component_id > ? AND ec.component_id <= ? ORDER BY ec.component_id ASC",
            (after_component_id, through_component_id),
        ).fetchall()

    items = []
    scanned = []
    evidence_complete = []
    aggregate_chars = 0
    pending_component_id = None
    pending_next_segment_index = 0

    for (component_id, event_id, sequence, creator_actor_id, component_kind,
         authorship_resolution, component_text, content_sha256, model_revision_id,
         occurred_at, pipeline_id, auth_context_id, event_type) in rows:

        scanned.append(component_id)

        if not is_sleep_eligible(prov_conn, component_id):
            # Mechanically ineligible: emits no selection-bearing
            # evidence, but requires no further selection-bearing
            # emission either -- evidence-complete at THIS layer,
            # never re-considered. This is NOT a watermark claim (see
            # docstring); it says only "nothing to emit here."
            evidence_complete.append(component_id)
            continue

        if not _content_hash_verified(component_text, content_sha256):
            raise SleepEvidenceIntegrityError(
                f"component_id={component_id!r}: recomputed content hash disagrees with "
                f"canonical content_sha256 -- refusing to include unverified content as Sleep evidence."
            )

        actor_type = _resolve_actor_type(prov_conn, creator_actor_id)
        memory_kind = _classify_selection_memory_kind(event_type, component_kind, actor_type)
        authentication_status = _derive_authentication_status(prov_conn, event_id)

        total_len = len(component_text)
        start_segment_index = resume_segment_index if component_id == resume_component_id else 0

        finished_all_segments = True
        segment_index = start_segment_index
        while True:
            char_start = segment_index * max_chars_per_segment
            if char_start >= total_len and not (total_len == 0 and segment_index == 0):
                break  # every segment of this component already emitted (this or a prior call)
            char_end = min(char_start + max_chars_per_segment, total_len)
            is_final_segment = (char_end == total_len)
            segment_text = component_text[char_start:char_end]

            if len(items) >= max_segments or aggregate_chars + len(segment_text) > max_aggregate_chars:
                finished_all_segments = False
                break

            items.append({
                "event_id": event_id,
                "component_id": component_id,
                "segment_id": _derive_segment_id(component_id, segment_index),
                "segment_index": segment_index,
                "char_start": char_start,
                "char_end": char_end,
                "is_final_segment": is_final_segment,
                "timestamp": occurred_at,
                "speaker_actor_id": creator_actor_id,
                "memory_kind": memory_kind,
                "attribution_status": authorship_resolution,
                "authentication_status": authentication_status,
                "pipeline_id": pipeline_id,
                "model_revision_id": model_revision_id,
                "expression": segment_text,
            })
            aggregate_chars += len(segment_text)

            if is_final_segment:
                break
            segment_index += 1

        if finished_all_segments:
            evidence_complete.append(component_id)
        else:
            # STOP THE ENTIRE BATCH here -- never advance to a later
            # canonical component while this one still has unemitted
            # segments (the simplifying invariant: at most one pending
            # component at any batch boundary).
            pending_component_id = component_id
            pending_next_segment_index = segment_index
            break

    return {
        "items": items,
        "component_ids_scanned": scanned,
        "component_ids_evidence_complete": evidence_complete,
        "pending_component_id": pending_component_id,
        "pending_next_segment_index": pending_next_segment_index,
    }
