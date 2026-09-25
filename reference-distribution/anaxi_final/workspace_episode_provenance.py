"""WSP2-P3: canonical, source-clean provenance for public unattended
workspace-roaming episodes.

Load-bearing correction this module exists to satisfy: canonical
continuity must not depend on the noncanonical workspace_roaming_trace.jsonl
or workspace/workspace_action_log.jsonl logs -- both remain in place,
unchanged, for compatibility/diagnostics only. Everything a future
waking renderer needs must be reconstructable from this module's own
canonical rows alone.

Event-style lifecycle, NO SCHEMA CHANGE: reuses the existing append-
only events/event_components/event_model_participation tables (see
provenance_schema.py) with new event_type/component_kind values --
exactly the same technique native_provenance_writer.py already used
to add its own single 'waking_turn' event_type. No new table.

PATHWAYS, NOT ORGANS: this module only records/queries mechanical
facts about what happened. It never decides what an episode MEANS,
never promotes anything to autobiographical memory, and never touches
Kardia, hippocampal retrieval, direction_owner, or Sleep/REM -- no
import of any of those exists anywhere in this file.

Actor attribution follows section 10's four-way distinction per public
action:
  A. Clark-selected choice        -> event_model_participation (which
                                      model/revision proposed it) --
                                      Clark is not his model.
  B. host validation/execution    -> creator_actor_id = the host actor,
                                      component_kind='roaming_action_fact'
                                      (the validated, mechanical fact of
                                      what was chosen and its outcome).
  C. external/filesystem result   -> creator_actor_id = the host actor,
                                      component_kind='roaming_external_result'
                                      (never attributed to Clark -- it is
                                      retrieved source material, not his
                                      expression).
  D. Clark-authored payload       -> creator_actor_id = clark_actor_id,
                                      component_kind='roaming_journal_text'
                                      (only for journal.append, the one
                                      action whose content Clark actually
                                      composed).

Host attribution reuses the SAME generic host-system actor
native_provenance_writer.py already established for host-produced
content ('bounded_clause_renderer', seeded once by
migrate_historical_data.seed_reference_data()) rather than minting a
new host actor that would require its own new seeding step. Its
actor_type ('host_system') and semantic role -- mechanically produced/
recorded content, never Clark's, never Alex's -- apply exactly as well
here. This reuse is a deliberate scope-minimizing choice, not an
accident; see this module's own test file for the exact seeding this
implies for a fresh test DB.

Run linkage: every event belonging to one episode carries an
event_components row of component_kind='workspace_roaming_run_id'
whose component_text is the run_id. This is a DEDICATED component,
not events.input_source_ref -- that field's existing, narrower meaning
(an external staging/source reference, as native_provenance_writer.py
already uses it for staging_id) is preserved completely unchanged.

Private space: this module has no import of workspace_private and
never receives a private-path fact of any kind. Its only job is to
record what its caller (workspace_roaming.py's public-only code paths)
hands it -- see that module for the actual public/private separation.
"""
import hashlib
import json
import os
import sqlite3
import time

import family_membership

from migrate_historical_data import resolve_pipeline_id
from native_provenance_writer import generate_native_ulid
from provenance_schema import derive_stable_id

RUN_ID_LINK_COMPONENT_KIND = "workspace_roaming_run_id"

EVENT_EPISODE_STARTED = "workspace_roaming_episode_started"
EVENT_HANDOFF_SELECTED = "workspace_roaming_handoff_selected"
EVENT_PUBLIC_ACTION = "workspace_roaming_public_action"
EVENT_PUBLIC_WAIT = "workspace_roaming_public_wait"
EVENT_EPISODE_ENDED = "workspace_roaming_episode_ended"

ALL_EPISODE_EVENT_TYPES = (
    EVENT_EPISODE_STARTED, EVENT_HANDOFF_SELECTED, EVENT_PUBLIC_ACTION,
    EVENT_PUBLIC_WAIT, EVENT_EPISODE_ENDED,
)

# WSP2-P4-P1: durable delivery evidence that a canonical waking event
# was included in a roaming decision's live context -- a sibling
# lifecycle event, NOT added to ALL_EPISODE_EVENT_TYPES/query_episode()
# (it is delivery bookkeeping, not episode narrative content; Bridge C
# and query_episode() have no reason to enumerate it). See
# record_live_waking_delivery()/find_delivered_live_waking_event_ids()
# below.
EVENT_LIVE_WAKING_DELIVERED = "workspace_roaming_live_waking_delivered"
LIVE_WAKING_DELIVERED_EVENT_ID_COMPONENT_KIND = "delivered_waking_event_id"

# WSP2-P5-P1 (spec section 4): durable, canonical, restart-surviving
# CONTROL STATE for Clark's own background-activity pause/resume --
# NOT autobiographical memory, no host-inferred reason, no weighting of
# any kind, only the mechanical control value + who chose it + canonical
# ordering. A sibling lifecycle event, like EVENT_LIVE_WAKING_DELIVERED
# above -- deliberately NOT added to ALL_EPISODE_EVENT_TYPES/
# query_episode() (it is standalone control bookkeeping, not one
# episode's own narrative content, and a resume written from a waking
# turn belongs to no run_id at all).
EVENT_BACKGROUND_LIFECYCLE_CONTROL = "workspace_background_lifecycle_control"
BACKGROUND_LIFECYCLE_CONTROL_COMPONENT_KIND = "background_lifecycle_control_fact"
BACKGROUND_CONTROL_PAUSED_BY_CLARK = "paused_by_clark"
BACKGROUND_CONTROL_RESUMED = "resumed"
VALID_BACKGROUND_LIFECYCLE_CONTROLS = {BACKGROUND_CONTROL_PAUSED_BY_CLARK, BACKGROUND_CONTROL_RESUMED}

# Public-safe termination classes only (spec section 6). A private-
# path-triggered failure is collapsed to one of these generic classes
# by the CALLER before it ever reaches this module -- this module
# itself has no way to record anything more specific even if it
# wanted to, since it never receives private information at all.
TERMINATION_CLARK_STOP = "clark_stop"
TERMINATION_WAIT_FOR_HUMAN = "wait_for_human"
TERMINATION_HUMAN_STOP = "human_stop"
TERMINATION_CONTROL_VALIDATION_FAILURE = "control_validation_failure"
TERMINATION_PROCESS_INTERRUPTION = "process_interruption"
TERMINATION_UNKNOWN = "unknown"
VALID_TERMINATION_CLASSES = {
    TERMINATION_CLARK_STOP, TERMINATION_WAIT_FOR_HUMAN, TERMINATION_HUMAN_STOP,
    TERMINATION_CONTROL_VALIDATION_FAILURE, TERMINATION_PROCESS_INTERRUPTION, TERMINATION_UNKNOWN,
}


def host_actor_id():
    """The same host-system actor native_provenance_writer.py already
    uses for host-produced components -- see module docstring for why
    this is reused rather than a new actor being minted."""
    return derive_stable_id("actor", "bounded_clause_renderer")


def generate_episode_run_id():
    """One run_id per roaming authorization/run -- minted once, at
    worker start, and carried unchanged through every lifecycle event
    for that run. Reuses generate_native_ulid()'s existing collision-
    safe generation rather than inventing a second ID scheme."""
    return f"wrun-{generate_native_ulid()}"


class EpisodeProvenanceError(Exception):
    """Raised only for a genuine, unexpected mechanical failure while
    writing/reading episode provenance -- never for an ordinary,
    already-handled roaming/private-path outcome."""


class ProvenanceStoreMissingError(EpisodeProvenanceError):
    """WSP2-P5-P2a (spec section 18): raised by find_latest_
    background_lifecycle_control() when the canonical provenance DB
    FILE ITSELF does not exist -- deliberately kept distinct from
    "DB exists, zero relevant rows" (which still returns None, a
    genuinely KNOWN empty ledger) and from an ordinary read/query
    failure against a present-but-corrupt file (a plain sqlite3
    exception). READABLE EMPTY CANONICAL HISTORY != CANONICAL HISTORY
    UNAVAILABLE (spec section 1D) -- these must never collapse into
    the same return value again."""


def _content_component(event_id, sequence, creator_actor_id, component_kind, text, model_revision_id=None):
    text = text if isinstance(text, str) else json.dumps(text, sort_keys=True)
    return (
        event_id, sequence, creator_actor_id, component_kind, "resolved",
        text, hashlib.sha256(text.encode("utf-8")).hexdigest(), model_revision_id, None, None,
    )


_COMPONENT_INSERT_SQL = (
    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
    "authorship_resolution, component_text, content_sha256, model_revision_id, span_start, span_end) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _record_episode_event(data_dir, *, event_type, run_id=None, pipeline_key, occurred_at, components, model_participation_note=None):
    """The one atomic transaction shared by every episode-lifecycle
    recorder below -- mirrors native_provenance_writer.record_native_
    waking_turn()'s own BEGIN/commit/rollback shape exactly. `components`
    is a list of (creator_actor_id, component_kind, text_or_dict,
    model_revision_id) tuples, written starting at sequence 1 (sequence
    0 is the run_id link, written whenever `run_id` is given -- every
    episode-lifecycle caller always passes one) -- every entry here has
    a real, resolved creator_actor_id (host or Clark); "which model
    proposed this" is NEVER expressed via a null-creator component (the
    schema's own CHECK constraint forbids a resolved component without
    a creator), it goes through event_model_participation instead,
    exactly like native_provenance_writer.record_native_waking_turn()'s
    own _add_participation() -- pass `model_participation_note` (and
    each component's own `model_revision_id`, already resolved by the
    caller) to record it there. Returns the new event_id.

    WSP2-P5-P1: `run_id=None` is legal for a standalone lifecycle-
    control fact belonging to no one episode run (e.g. a waking-turn
    self-resume) -- no run_id-link component is written in that case.
    Every pre-existing caller always passes a real run_id, so this
    stays additive and behavior-preserving for them."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        pipeline_id = resolve_pipeline_id(conn, pipeline_key)
        if pipeline_id is None:
            raise EpisodeProvenanceError(
                f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id -- "
                f"seed_reference_data() must run before any episode write."
            )
        event_id = generate_native_ulid()
        conn.execute("BEGIN")
        try:
            record_created_at = int(time.time())
            # Post-Family workspace facts carry their NATIVE current scope
            # (principal_private, the owner's workspace); before Family
            # they stay NULL exactly as they always were.
            native_scope = family_membership.native_workspace_event_scope(conn)
            scope_columns = ", visibility_scope" if native_scope is not None else ""
            scope_placeholder = ", ?" if native_scope is not None else ""
            scope_values = (native_scope,) if native_scope is not None else ()
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                f"epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at{scope_columns}) "
                f"VALUES (?, ?, ?, 'known', NULL, NULL, NULL, ?, ?{scope_placeholder})",
                (event_id, event_type, pipeline_id, occurred_at, record_created_at) + scope_values,
            )
            if run_id is not None:
                conn.execute(
                    _COMPONENT_INSERT_SQL,
                    _content_component(event_id, 0, host_actor_id(), RUN_ID_LINK_COMPONENT_KIND, run_id),
                )
            for sequence, (creator_actor_id, component_kind, text, model_revision_id) in enumerate(components, start=1):
                conn.execute(
                    _COMPONENT_INSERT_SQL,
                    _content_component(event_id, sequence, creator_actor_id, component_kind, text, model_revision_id),
                )
            model_revision_id = next((c[3] for c in components if c[3]), None)
            if model_participation_note and model_revision_id:
                conn.execute(
                    "INSERT INTO event_model_participation (event_id, model_revision_id, participation_note) "
                    "VALUES (?, ?, ?)",
                    (event_id, model_revision_id, model_participation_note),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return event_id
    finally:
        conn.close()


# ============================================================= recorders


def record_episode_started(data_dir, *, run_id, clark_actor_id, pipeline_key, occurred_at):
    """Bridge A/section 1: the episode's opening canonical fact.
    Clark's actor identity is recorded as a mechanical fact (host-
    established: THIS episode belongs to THIS actor), not as Clark
    expression -- there is no free text here for Clark to have
    authored."""
    return _record_episode_event(
        data_dir, event_type=EVENT_EPISODE_STARTED, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=[(host_actor_id(), "roaming_episode_actor", clark_actor_id, None)],
    )


def _resolve_canonical_human_actor_id(data_dir):
    """Read-only. Same query direction_control.py's own
    resolve_canonical_human_actor_id() uses -- inlined here rather
    than importing that module, so this module's own import list stays
    exactly {hashlib, json, os, sqlite3, time} + the already-audited
    project modules (see test_no_kardia_hippocampus_direction_
    sleep_imports). Raises EpisodeProvenanceError (not the direction_
    control-specific exception type -- this module never imports that
    class) if zero or more than one human_person actor row exists.

    FS1: once the family feature is in use (a designated owner exists),
    that designated owner IS the canonical human actor -- the multi-
    human ownership model; while the feature is unused (no family
    tables in the pre-FS1 database shape, or an empty ledger) the
    legacy exactly-one-human rule applies unchanged."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        owner = family_membership.designated_owner_actor_id(conn)
        if owner is not None:
            return owner
        rows = conn.execute("SELECT actor_id FROM actors WHERE actor_type = 'human_person'").fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise EpisodeProvenanceError(
            f"expected exactly one actor_type='human_person' row, found {len(rows)} -- "
            f"cannot correctly attribute a human-authored handoff source excerpt"
        )
    return rows[0][0]


def _resolve_human_excerpt_actor_id(data_dir, excerpt):
    """Resolve a handoff excerpt's actual human author.

    FS1 family dialogue is rejected here because V0 workspace events
    have no principal visibility_scope and must not turn scoped content
    into an unscoped public continuity fact. For a pre-FS1 deployment,
    a supplied principal is accepted only when it matches the canonical
    X -> H link; older rows without canonical H retain the legacy
    exactly-one-human resolution.
    """
    db_path = f"{data_dir}/anaxi_provenance.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if family_membership.family_feature_used(conn):
            raise EpisodeProvenanceError(
                "FS1 family human excerpts cannot enter unscoped public workspace provenance"
            )
        claimed_principal = excerpt.get("principal_actor_id")
        source_event_id = excerpt.get("event_id")
        if claimed_principal is not None:
            registered = conn.execute(
                "SELECT 1 FROM actors a JOIN actor_human_person hp ON hp.actor_id=a.actor_id "
                "WHERE a.actor_id=? AND a.actor_type='human_person'",
                (claimed_principal,),
            ).fetchone()
            linked = conn.execute(
                "SELECT hc.creator_actor_id FROM event_components link "
                "JOIN events h ON h.event_id=link.component_text "
                "JOIN event_components hc ON hc.event_id=h.event_id "
                "JOIN auth_contexts ac ON ac.auth_context_id=h.auth_context_id "
                "WHERE link.event_id=? AND link.component_kind='human_input_event_id' "
                "AND h.event_type='human_waking_input' "
                "AND hc.component_kind='human_conversational_input' AND hc.sequence=0 "
                "AND ac.auth_state='authenticated' "
                "AND ac.authenticated_actor_id=hc.creator_actor_id",
                (source_event_id,),
            ).fetchall()
            if registered is None or len(linked) != 1 or linked[0][0] != claimed_principal:
                raise EpisodeProvenanceError(
                    "human handoff excerpt principal does not match canonical authenticated H/X provenance"
                )
            return claimed_principal
    finally:
        conn.close()
    return _resolve_canonical_human_actor_id(data_dir)


def record_episode_handoff(
    data_dir, *, run_id, clark_actor_id, pipeline_key, occurred_at,
    note, source_excerpts, model_revision_id,
):
    """Bridge A: the Clark-selected departure handoff. `note` is
    Clark's own bounded expression (creator=clark_actor_id). Each
    entry in `source_excerpts` is a dict {"handle", "author":
    "human"|"clark", "text": <bounded exact source text>, "event_id",
    "sequence"} -- the EXACT selected source material, attributed to
    whoever actually spoke it (WSP2-P3-P1 section 3: the human actor
    is resolved and used as creator_actor_id for a human-authored
    excerpt -- never the host, never Clark) -- this is the load-
    bearing repair: source content, not a paraphrase, actually crosses
    into the record, and it is source-LINKED (event_id/sequence
    preserved verbatim in the stored component_text), not merely
    source-COPIED. If no eligible departure conversation existed,
    `note` is "" and `source_excerpts` is [] (the zero-model-call
    empty-handoff path); the event is still recorded so the episode's
    lifecycle stays complete and honest about the fact that nothing
    was carried forward.

    Fails closed (raises, writes nothing) if any excerpt is human-
    authored but the canonical human actor cannot be resolved -- per
    WSP2-P3-P1 section 1, this is exactly the kind of required-
    persistence-precondition failure that must prevent, not silently
    mis-attribute, the record."""
    components = []
    if note:
        components.append((clark_actor_id, "roaming_handoff_note", note, model_revision_id))
    for excerpt in source_excerpts:
        creator = (
            clark_actor_id if excerpt["author"] == "clark"
            else _resolve_human_excerpt_actor_id(data_dir, excerpt)
        )
        components.append((
            creator,
            "roaming_handoff_source_excerpt",
            {
                "handle": excerpt["handle"], "author": excerpt["author"], "text": excerpt["text"],
                "source_event_id": excerpt.get("event_id"), "source_sequence": excerpt.get("sequence"),
            },
            None,
        ))
    return _record_episode_event(
        data_dir, event_type=EVENT_HANDOFF_SELECTED, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at, components=components,
    )


def record_public_action(
    data_dir, *, run_id, clark_actor_id, pipeline_key, occurred_at,
    resource_class, action, relative_path, success, action_id_backlink,
    model_revision_id, journal_text=None, external_result_text=None,
):
    """Bridge B/C source material, one PUBLIC action. `action_id_backlink`
    is workspace_action_log.jsonl's own action_id -- kept ONLY as an
    audit/source backlink (spec section 1's correction); the fact
    itself is fully reconstructable from this event's own components
    without ever reading that file again. `journal_text` (Clark-
    authored, only for journal.append) and `external_result_text`
    (host-established external/mechanical result, e.g. a bounded
    library.read) are mutually exclusive and both optional -- most
    actions (an ordinary .list) have neither."""
    fact = {
        "resource_class": resource_class, "action": action, "relative_path": relative_path,
        "success": success, "action_id_backlink": action_id_backlink,
    }
    components = [
        (host_actor_id(), "roaming_action_fact", fact, model_revision_id),
    ]
    if journal_text:
        components.append((clark_actor_id, "roaming_journal_text", journal_text, model_revision_id))
    if external_result_text:
        components.append((host_actor_id(), "roaming_external_result", external_result_text, None))
    return _record_episode_event(
        data_dir, event_type=EVENT_PUBLIC_ACTION, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at, components=components,
        model_participation_note="Model backing this public roaming action's choice.",
    )


def record_public_wait(data_dir, *, run_id, pipeline_key, occurred_at, wait_minutes, model_revision_id):
    """Bridge B/C source material, one PUBLIC wait decision."""
    return _record_episode_event(
        data_dir, event_type=EVENT_PUBLIC_WAIT, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=[(host_actor_id(), "roaming_wait_fact", {"wait_minutes": wait_minutes}, model_revision_id)],
        model_participation_note="Model backing this public roaming wait choice.",
    )


def record_episode_ended(data_dir, *, run_id, pipeline_key, occurred_at, termination_class):
    """Bridge C/section 6: the episode's closing canonical fact.
    `termination_class` MUST already be one of VALID_TERMINATION_
    CLASSES -- collapsed by the caller before this module ever sees
    it; this function additionally re-validates and refuses to write
    anything else, so a private-path label can never reach canonical
    provenance even by caller error."""
    if termination_class not in VALID_TERMINATION_CLASSES:
        raise EpisodeProvenanceError(f"refusing to record non-public-safe termination_class: {termination_class!r}")
    return _record_episode_event(
        data_dir, event_type=EVENT_EPISODE_ENDED, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=[(host_actor_id(), "roaming_termination_fact", {"termination_class": termination_class}, None)],
    )


def record_live_waking_delivery(data_dir, *, run_id, pipeline_key, occurred_at, delivered_event_ids):
    """WSP2-P4-P1 (spec section 4/9): durable, restart-surviving
    evidence that these canonical waking event_ids were actually
    included in a roaming decision's context for this run. Mechanical
    fact only -- source waking event_id + this run's own run_id link
    -- never semantic content, never a summary of what was said. One
    component per delivered event_id, all in the same atomic
    transaction (mirrors every other episode-lifecycle recorder in
    this module). `delivered_event_ids` empty is a no-op: caller
    should simply not call this when nothing new was rendered."""
    components = [
        (host_actor_id(), LIVE_WAKING_DELIVERED_EVENT_ID_COMPONENT_KIND, event_id, None)
        for event_id in delivered_event_ids
    ]
    return _record_episode_event(
        data_dir, event_type=EVENT_LIVE_WAKING_DELIVERED, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at, components=components,
    )


def record_background_lifecycle_control(data_dir, *, actor_id, pipeline_key, occurred_at, control, run_id=None):
    """WSP2-P5-P1 (spec section 4): durable, canonical CONTROL STATE --
    never autobiographical memory. Records exactly one mechanical fact:
    `control` (BACKGROUND_CONTROL_PAUSED_BY_CLARK, written when Clark's
    own roaming `stop` fires, or BACKGROUND_CONTROL_RESUMED, written
    when that pause clears -- either through Clark's own typed waking
    resume_own_pause choice, or a human resume reconciling the same
    ledger so a restart can never resurrect a stale Clark-owned pause
    a human already cleared; see workspace_roaming.py and
    llama_gui.py's own callers), the actor who made that choice
    (`actor_id` -- Clark for both his own stop and his own self-resume;
    the resolved human actor for a human's reconciling resume), and
    canonical occurred_at/event ordering for later reconstruction (see
    find_latest_background_lifecycle_control() below). No host-inferred
    reason or weighting of any kind is ever recorded. `run_id` is
    optional -- present when the choice happened during an active
    roaming episode (Clark's own `stop`), absent for a waking-turn
    resume, which belongs to no episode at all. Raises (writes nothing)
    on an unrecognized `control` value -- mirrors record_episode_
    ended()'s own re-validation discipline."""
    if control not in VALID_BACKGROUND_LIFECYCLE_CONTROLS:
        raise EpisodeProvenanceError(f"refusing to record unrecognized background lifecycle control: {control!r}")
    return _record_episode_event(
        data_dir, event_type=EVENT_BACKGROUND_LIFECYCLE_CONTROL, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=[(actor_id, BACKGROUND_LIFECYCLE_CONTROL_COMPONENT_KIND, control, None)],
    )


def find_latest_background_lifecycle_control(data_dir):
    """Read-only -- never creates the store (spec section 7: no
    sqlite3.connect() on a missing path may manufacture a new empty
    one; the existence check below runs strictly before any connect
    attempt). The most recent EVENT_BACKGROUND_LIFECYCLE_CONTROL
    control value, by canonical (occurred_at, event_id) ordering --
    mirrors workspace_public_continuity.compute_high_water_mark()'s own
    established ordering convention exactly (event_id is a ULID, so
    this tiebreak agrees with true generation order even for two
    events sharing the same occurred_at second).

    WSP2-P5-P2a (spec sections 5/8/18): returns None ONLY for a
    genuinely KNOWN empty ledger -- the DB file exists and is
    readable, but no such event has ever been recorded in it. Raises
    ProvenanceStoreMissingError if the DB file itself does not exist
    -- CONSEQUENTIALLY UNCERTAIN LIFECYCLE AUTHORITY FAILS CLOSED (spec
    section 1A): a missing store is never treated the same as a
    readable-and-empty one, since the caller cannot actually tell
    whether canonical history was ever established at all. Any other
    read/query failure (corruption, malformed schema) still propagates
    as whatever plain sqlite3 exception it naturally raises -- also
    never returned as None."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        raise ProvenanceStoreMissingError(
            f"canonical provenance store does not exist at {db_path!r} -- "
            f"missing is not the same as readable-and-empty; callers must "
            f"not treat this as a KNOWN empty ledger"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = ? AND c.component_kind = ? "
            "ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1",
            (EVENT_BACKGROUND_LIFECYCLE_CONTROL, BACKGROUND_LIFECYCLE_CONTROL_COMPONENT_KIND),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# CAP2-E: canonical, source-clean provenance for a genuine public
# resource encounter (book/journal/photograph delivery to Clark's own
# model pathway). NO SCHEMA CHANGE -- same technique this module
# already uses throughout: a new event_type/component_kind pair over
# the existing events/event_components tables. `run_id=None` is legal
# (spec _record_episode_event section WSP2-P5-P1) -- most resource
# encounters this gate come from the WSP1 supervised single-action
# pathway (workspace_supervisor.py), which belongs to no roaming
# episode at all; a future roaming-sourced encounter may still pass a
# real run_id.
#
# Deliberately mechanical-only, per the module's own PATHWAYS NOT
# ORGANS discipline: this event never says Clark remembers, liked, or
# was affected by anything -- only that identified content of a given
# modality was (or, for a denied/failed action, was not) delivered to
# a named model pathway at a given time. No raw image/audio bytes are
# ever accepted as a parameter here -- only `content_sha256`, a digest
# computed by the caller from the exact bytes it attempted to deliver.
#
# CAP2E-P2 audit (load-bearing, confirmed by source audit of every
# writer/reader of this fact's own JSON shape -- not merely reasoned
# about): `content_sha256` has ALWAYS meant "digest of the content
# actually delivered," never "digest of the original source resource."
# This line was true, verbatim, from this field's very first CAP2
# commit -- pre-CAP2E-P1, `content_sha256` for a photograph was simply
# `deliver_photograph_bytes()`'s own digest, which happened to equal
# the source's digest ONLY because no resize step existed yet (delivery
# WAS the source, byte-for-byte). CAP2E-P1's own choice (content_sha256
# = the resized representation's digest; the new, additive
# source_content_sha256 = the original resource's own, separate digest)
# is therefore a continuation of this field's original meaning, not a
# repurposing -- confirmed by grepping every production consumer of a
# resource-encounter fact (workspace_supervisor.py, the writer;
# llama_anaxi.py's own continuity renderer, the only production reader,
# which uses `modality`/`resource_class`/`relative_path`/`source_action`
# only and never inspects `content_sha256` at all) at CAP2E-P2 gate
# time. No schema change, no historical-data migration, no field
# renamed.
EVENT_RESOURCE_ENCOUNTER = "workspace_resource_encounter"
RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND = "resource_encounter_fact"

# SLP1-A3: explicit canonical backlink from a resource encounter (Y) to
# the exact native waking-turn event (X) that occasioned it. X always
# commits BEFORE Y is even created (workspace_supervisor.py calls
# stage_and_record_native_waking_turn() first, then record_resource_
# encounter() afterward, using values already in native_result), and
# events/event_components are append-only (provenance_schema.py's own
# trg_events_no_update/trg_event_components_no_update) -- so X can
# never be retroactively updated to name Y. The later-written record
# (Y) always carries the naming responsibility instead, exactly
# mirroring RUN_ID_LINK_COMPONENT_KIND's/active_workspace_event_
# delivered's own established idiom (a record naming another,
# already-existing record by its bare event_id, never a JSON-blob key
# -- so a future WMU-traversal reader can find the relation with a
# plain component_kind query, no JSON parsing required). Host-
# mechanical (naming which turn triggered this fact is itself a
# mechanical fact, never Clark's own expression), hashed via the same
# canonical component machinery as every other component here.
TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND = "triggering_waking_event_id"

# SLP1-A4: exact, forward-only replayable representation of what was
# GENUINELY delivered into Clark's own model context for a resource
# encounter -- distinct in kind from RESOURCE_ENCOUNTER_FACT_COMPONENT_
# KIND (which never carries the representation itself, only its hash/
# portion/success bookkeeping). Two separate kinds, never one
# overloaded kind, per the frozen v1 modality decision:
#   - TEXT (library/journal reads): the exact delivered text string.
#   - AUDIO (LISTEN/INSPECT_AUDIO): the exact bounded textual/JSON
#     acoustic MEASUREMENT view actually delivered -- never raw audio
#     bytes, never a claim of "hearing."
# PHOTOGRAPHS deliberately have no replayable-representation component
# kind at all in this architecture -- see record_resource_encounter()'s
# own docstring for why pixel persistence is refused by design, not
# merely unimplemented.
RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND = "resource_encounter_delivered_text"
RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND = "resource_encounter_delivered_measurement"

# CAP2-F: sibling delivered-marker event, mirroring EVENT_LIVE_WAKING_
# DELIVERED's own established pattern exactly (see that event type's
# docstring above) but in the OPPOSITE direction -- this tracks which
# resource-encounter events have already been rendered into a later
# waking turn's own immediate continuity, so a process restart or a
# second waking turn can never re-deliver (falsely "re-encounter") the
# same fact. Deliberately NOT added to ALL_EPISODE_EVENT_TYPES/
# query_episode() -- delivery bookkeeping, not one episode's own
# narrative content, exactly like EVENT_LIVE_WAKING_DELIVERED.
EVENT_RESOURCE_ENCOUNTER_CONTINUITY_DELIVERED = "workspace_resource_encounter_continuity_delivered"
RESOURCE_ENCOUNTER_DELIVERED_EVENT_ID_COMPONENT_KIND = "delivered_resource_encounter_event_id"


def record_resource_encounter(
    data_dir, *, clark_actor_id, pipeline_key, occurred_at,
    resource_class, relative_path, modality, delivered_portion,
    content_sha256, source_action, target_model_pathway, genuinely_delivered,
    model_revision_id=None, run_id=None, source_content_sha256=None,
    triggering_waking_event_id=None, delivered_text=None, delivered_measurement=None,
    source_folder=None,
):
    """CAP2-E: the one recorder for every resource-encounter fact
    (library/journal text or a photograph's genuine pixels). Persist
    ONLY after the caller's own final model request has already been
    made (or, for a denied/failed attempt, never will be) -- this
    function itself makes no model call and has no way to verify that
    independently; that discipline is the caller's (workspace_
    supervisor.py's), documented there.

    `modality`: "text" (library/journal) or "image" (photograph) this
    gate; a future audio pathway would add "audio" here, never invent
    a new field shape. `delivered_portion`: a short mechanical range
    description ("chars 0-4000", "entry text, full", "image, full,
    bounded to <=2048px/8MiB") -- never a semantic summary.
    `content_sha256`: the digest of the bytes/text ACTUALLY delivered
    to `target_model_pathway` -- for a resized vision representation
    (CAP2E-P1), this is the REPRESENTATION's own digest, never the
    original source's. `source_content_sha256`: optional, additive --
    the ORIGINAL source resource's own digest, when a caller (CAP2E-P1:
    a resized photograph) has one distinct from what was delivered.
    None (the default) for every pre-existing caller/modality where no
    such distinction exists (the delivered bytes/text ARE the source's
    own, unmodified) -- this is never a required field and never
    implies a lesser or different resource identity; it exists purely
    so provenance can honestly distinguish "what this resource is" from
    "what this specific delivery actually sent."
    `genuinely_delivered`: True only when content actually crossed into
    the final model request; recorded as False (with the same fact
    shape) for a permitted-but-unsuccessful attempt, so the ledger
    never silently omits what was tried.

    `triggering_waking_event_id` (SLP1-A3, optional, additive): the
    exact canonical `event_id` of the native waking turn that
    occasioned this encounter, when the caller positively possesses
    one (e.g. workspace_supervisor.py's own `native_result["event_id"]`,
    already committed before this function is ever called). When
    given, written as a DEDICATED host-mechanical event_components row
    (component_kind=TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND,
    component_text=the bare event_id string) in the SAME atomic
    transaction as this encounter's own resource_encounter_fact
    component -- never a separate write, never a retroactive update to
    the (already-committed, append-only) waking turn itself. Omitted
    (None, the default) for any caller that does not possess a proven
    triggering event: the resulting encounter is simply unlinked, and
    that absence must be read as UNKNOWN by any future reader -- never
    inferred from occurred_at/actor/model coincidence, never
    backfilled. Because this component is appended to the SAME
    `components` list `_record_episode_event()` already writes inside
    one BEGIN/commit/rollback transaction, a partial encounter that
    carries the backlink but is missing its own resource_encounter_fact
    component (or vice versa) is structurally impossible: either both
    exist or neither does.

    `delivered_text`/`delivered_measurement` (SLP1-A4, optional,
    mutually exclusive, additive): the EXACT representation genuinely
    delivered into Clark's own model context this turn, forward-only,
    never a historical backfill. `delivered_text` is legal only for
    `modality == "text"` (library/journal reads); `delivered_measurement`
    only for `modality == "audio"` (LISTEN/INSPECT_AUDIO) -- passing
    either for the wrong modality raises EpisodeProvenanceError rather
    than silently misfiling it. Photographs have no corresponding
    parameter at all: this architecture refuses pixel persistence by
    design (see the module-level v1 decision), and Clark's own genuine
    conversational_prose already IS the correct v1 selection-bearing
    trace for a photograph encounter.

    Gated on `genuinely_delivered` INSIDE this function, not merely by
    caller discipline: a caller may pass `delivered_text`/
    `delivered_measurement` unconditionally (as the WSP1 pathway does)
    and this function silently omits the replayable component whenever
    `genuinely_delivered` is False -- a failed/attempted delivery can
    never produce a replayable trace. When it IS written, its own
    recomputed content hash MUST agree with the already-established
    `content_sha256` for this same encounter (both are defined over
    the identical exact representation) -- disagreement raises
    EpisodeProvenanceError and writes nothing, rather than persisting
    two provenance fields that silently disagree about what was
    actually delivered. Written as its own dedicated component_kind
    (RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND /
    RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND), never
    folded into the resource_encounter_fact JSON blob, so it is
    mechanically distinguishable from mere bookkeeping -- and, like
    every other component here, host-attributed (this is delivered
    external/measured content, not Clark's own authored expression,
    and not host semantic interpretation of it either)."""
    if delivered_text is not None and delivered_measurement is not None:
        raise EpisodeProvenanceError(
            "record_resource_encounter: delivered_text and delivered_measurement are mutually exclusive"
        )
    if delivered_text is not None and modality != "text":
        raise EpisodeProvenanceError(
            f"record_resource_encounter: delivered_text given for modality={modality!r}, expected 'text'"
        )
    if delivered_measurement is not None and modality != "audio":
        raise EpisodeProvenanceError(
            f"record_resource_encounter: delivered_measurement given for modality={modality!r}, expected 'audio'"
        )

    fact = {
        "resource_class": resource_class,
        "relative_path": relative_path,
        "modality": modality,
        "delivered_portion": delivered_portion,
        "content_sha256": content_sha256,
        "source_action": source_action,
        "target_model_pathway": target_model_pathway,
        "genuinely_delivered": bool(genuinely_delivered),
    }
    if source_content_sha256 is not None:
        fact["source_content_sha256"] = source_content_sha256
    if source_folder:
        # The owner's logical source folder (workspace_provenance): host-established
        # filing, never an inference about the resource's content. Optional/additive.
        fact["source_folder"] = source_folder
    components = [
        (host_actor_id(), RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND, fact, model_revision_id),
    ]
    if triggering_waking_event_id is not None:
        components.append(
            (host_actor_id(), TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND, triggering_waking_event_id, None)
        )
    if delivered_text is not None and genuinely_delivered:
        recomputed = hashlib.sha256(delivered_text.encode("utf-8")).hexdigest()
        if recomputed != content_sha256:
            raise EpisodeProvenanceError(
                f"record_resource_encounter: delivered_text's own hash {recomputed!r} disagrees with "
                f"content_sha256 {content_sha256!r} -- refusing to persist an inconsistent replayable "
                f"representation."
            )
        components.append(
            (host_actor_id(), RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND, delivered_text, None)
        )
    if delivered_measurement is not None and genuinely_delivered:
        recomputed = hashlib.sha256(delivered_measurement.encode("utf-8")).hexdigest()
        if recomputed != content_sha256:
            raise EpisodeProvenanceError(
                f"record_resource_encounter: delivered_measurement's own hash {recomputed!r} disagrees "
                f"with content_sha256 {content_sha256!r} -- refusing to persist an inconsistent "
                f"replayable representation."
            )
        components.append(
            (host_actor_id(), RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND, delivered_measurement, None)
        )
    return _record_episode_event(
        data_dir, event_type=EVENT_RESOURCE_ENCOUNTER, run_id=run_id, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=components,
        model_participation_note="Model pathway that received this resource encounter's delivered content." if genuinely_delivered and model_revision_id else None,
    )


def find_pending_resource_encounter_for_continuity(data_dir):
    """CAP2-F, read-only. The single most recent genuinely-delivered
    EVENT_RESOURCE_ENCOUNTER fact not yet marked continuity-delivered
    -- bounded to exactly one (spec: "immediate", not a backlog/digest
    of every past encounter; that is explicitly the LATER historical-
    continuity gate's job, not this one's). Returns None (never raises)
    if the provenance DB does not exist yet or no such fact exists --
    matching this module's own established convention for every other
    read here. Never returns a fact whose genuinely_delivered is False
    -- an attempted-but-failed encounter has nothing to carry forward."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        delivered = {
            row[0] for row in conn.execute(
                "SELECT component_text FROM event_components WHERE component_kind = ?",
                (RESOURCE_ENCOUNTER_DELIVERED_EVENT_ID_COMPONENT_KIND,),
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT e.event_id, e.occurred_at, c.component_text "
            "FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = ? AND c.component_kind = ? "
            "ORDER BY e.occurred_at DESC, e.event_id DESC",
            (EVENT_RESOURCE_ENCOUNTER, RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND),
        ).fetchall()
    finally:
        conn.close()

    for event_id, occurred_at, component_text in rows:
        if event_id in delivered:
            continue
        fact = json.loads(component_text) if isinstance(component_text, str) else component_text
        if not fact.get("genuinely_delivered"):
            continue
        return {"event_id": event_id, "occurred_at": occurred_at, "fact": fact}
    return None


def record_resource_encounter_continuity_delivered(data_dir, *, pipeline_key, occurred_at, event_id):
    """CAP2-F: durable, mechanical delivery marker -- mirrors
    record_live_waking_delivery()'s own single-component shape exactly.
    Called ONLY after the encounter fact has actually survived this
    turn's own compose_within_budget() composition (spec section 18's
    "NOT SELECTED THIS TURN DUE TO BUDGET != DURABLY DELIVERED" applies
    here too, in the opposite direction: a fact dropped for budget must
    remain eligible for a LATER turn, so this must never be called for
    one that didn't actually survive)."""
    return _record_episode_event(
        data_dir, event_type=EVENT_RESOURCE_ENCOUNTER_CONTINUITY_DELIVERED, run_id=None, pipeline_key=pipeline_key,
        occurred_at=occurred_at,
        components=[(host_actor_id(), RESOURCE_ENCOUNTER_DELIVERED_EVENT_ID_COMPONENT_KIND, event_id, None)],
    )


def find_delivered_live_waking_event_ids(data_dir):
    """Read-only. The durable, GLOBAL set of waking event_ids already
    delivered to the roaming pathway across every run, past or
    present -- once an event has crossed, it never becomes "new" again
    merely because a process restarted or a different run began (spec
    section 4: "a process restart must not create false novelty").
    Returns an empty set (never raises) if the provenance DB does not
    exist yet."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = ? AND c.component_kind = ?",
            (EVENT_LIVE_WAKING_DELIVERED, LIVE_WAKING_DELIVERED_EVENT_ID_COMPONENT_KIND),
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


# ================================================================ queries


def query_episode(data_dir, run_id):
    """Read-only. Reconstructs the full canonical lifecycle for one
    run_id, purely from events/event_components -- never touching
    workspace_roaming_trace.jsonl or workspace_action_log.jsonl. This
    is the exact reconstructability proof the load-bearing correction
    requires: everything returned here came from canonical provenance
    alone. Returns a list of {"event_id", "event_type", "occurred_at",
    "components": [{"sequence","creator_actor_id","component_kind","component_text"}, ...]}
    dicts, oldest first. Returns [] (never raises) if the provenance
    DB does not exist yet -- matching the exact established convention
    everywhere else in this codebase (e.g. run_waking_turn()'s own
    OWC2 dialogue-window fetch): a DB that doesn't exist yet trivially
    has no episodes in it either."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT e.event_id, e.event_type, e.occurred_at "
            "FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type IN ({}) AND c.component_kind = ? AND c.component_text = ? "
            "ORDER BY e.occurred_at ASC".format(",".join("?" for _ in ALL_EPISODE_EVENT_TYPES)),
            (*ALL_EPISODE_EVENT_TYPES, RUN_ID_LINK_COMPONENT_KIND, run_id),
        ).fetchall()
        episode = []
        for event_id, event_type, occurred_at in rows:
            comp_rows = conn.execute(
                "SELECT sequence, creator_actor_id, component_kind, component_text "
                "FROM event_components WHERE event_id = ? ORDER BY sequence",
                (event_id,),
            ).fetchall()
            episode.append({
                "event_id": event_id, "event_type": event_type, "occurred_at": occurred_at,
                "components": [
                    {"sequence": s, "creator_actor_id": ca, "component_kind": ck, "component_text": ct}
                    for s, ca, ck, ct in comp_rows
                ],
            })
        return episode
    finally:
        conn.close()


def find_pending_episode_run_ids(data_dir):
    """Read-only. A 'pending' episode is one whose EVENT_EPISODE_ENDED
    event exists but for which no waking turn has yet recorded a
    matching 'episode_context_delivered' acknowledgment component
    (written atomically by native_provenance_writer.record_native_
    waking_turn() -- see that module's delivered_episode_run_id
    parameter). Returns run_ids ordered newest-first. Returns []
    (never raises) if the provenance DB does not exist yet."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ended_rows = conn.execute(
            "SELECT DISTINCT c.component_text, e.occurred_at "
            "FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = ? AND c.sequence = 0 AND c.component_kind = ? "
            "ORDER BY e.occurred_at DESC",
            (EVENT_EPISODE_ENDED, RUN_ID_LINK_COMPONENT_KIND),
        ).fetchall()
        delivered = {
            row[0] for row in conn.execute(
                "SELECT component_text FROM event_components WHERE component_kind = 'episode_context_delivered'"
            ).fetchall()
        }
        return [run_id for run_id, _occurred_at in ended_rows if run_id not in delivered]
    finally:
        conn.close()
