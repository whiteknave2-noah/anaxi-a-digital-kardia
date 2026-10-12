"""WSP2-P4: shared public continuity between ordinary waking
conversation and an active Your Space (roaming) episode.

Governing principle (frozen by this gate's own spec section 3):
THERE SHOULD BE NO PUBLIC-CONTINUITY BOUNDARY BETWEEN CLARK'S SPACE AND
HIS ORDINARY WAKING CONVERSATION -- while typed pathway boundaries,
provenance boundaries, capability/permission boundaries, bounded
context budgets, and the WSP3 private boundary all remain exactly as
they were. PATHWAYS, NOT SEPARATE WORLDS: PUBLIC material becomes
ELIGIBLE from the same canonical history regardless of which waking
pathway is asking; eligibility is never the same thing as automatic
insertion, and every individual model call remains independently
bounded.

This module supplements, and does not modify, WSP2-P3's own Bridge A
(workspace_roaming.py's departure handoff) and Bridge C
(workspace_episode_context.py's completed-episode renderer). Bridge A
remains a Clark-selected SALIENCE mechanism, not the sole source of
conversation continuity, and Bridge C remains episode closure, not the
first moment waking Clark may learn of PUBLIC Space activity -- this
module is precisely the "first moment" mechanism for both directions
while an episode/turn is still in progress.

Two independent bounded renderers:

  LIVE_WAKING_CONTINUITY_V1 (waking -> active roam, spec section 6):
  successful, canonical, CONVERSATION_MODE-only waking turns that
  occurred after a roaming episode began, offered to the NEXT roaming
  decision.

  ACTIVE_WORKSPACE_CONTINUITY_V1 (active roam -> waking, spec section
  9): canonical PUBLIC Space activity and capability-state transitions
  (episode started/ended -- never a private fact), offered to the next
  ordinary CONVERSATION_MODE waking turn, without requiring episode
  termination.

Canonical snapshot discipline (spec section 4): every collection
function here takes an explicit `since_marker` (exclusive floor) and
`high_water_mark` (inclusive ceiling), both `(occurred_at, event_id)`
tuples -- the SAME ordering primitive session_dialogue_window.py and
workspace_episode_provenance.py already use throughout this codebase
(`ORDER BY occurred_at ASC, event_id ASC`), not an invented wall-clock
scheme. `compute_high_water_mark()` below is the one function that
establishes that ceiling, read once per context build, so a model call
can never receive an accidental half-state caused by concurrent
persistence.

Delivery bookkeeping (WSP2-P4-P1, superseding WSP2-P4's own original
process-local-only cursors) is DURABLE, canonical, and restart-proof --
source event IDs only, never semantic content, never autobiographical
memory, never salience/importance/preference/belief. Both directions
now consult ONE canonical delivery ledger per direction, written
atomically alongside the event that constitutes "successfully
delivered":

  waking -> roaming: workspace_episode_provenance.record_live_waking_
  delivery() writes one 'delivered_waking_event_id' component per
  delivered source event_id, in the same atomic transaction as the
  roaming-lifecycle event recording that delivery. find_delivered_
  live_waking_event_ids() reads it back, globally, across every run --
  once an event has crossed, a process restart can never make it look
  new again.

  roaming -> waking: native_provenance_writer.record_native_waking_
  turn()'s new `delivered_active_workspace_event_ids` parameter writes
  one 'active_workspace_event_delivered' component per delivered
  source event_id, in the SAME atomic transaction as the consuming
  waking turn's own persistence -- mirrors 'episode_context_delivered'
  (Bridge C) exactly, just at per-event granularity. find_delivered_
  public_event_ids() below reads it back, globally. A failed waking
  turn's transaction never commits, so it can never falsely acknowledge
  delivery.

This SAME find_delivered_public_event_ids() read is what both the live
ACTIVE_WORKSPACE_CONTINUITY_V1 renderer and Bridge C's own closure
rendering (via llama_anaxi.py's call site) consult -- ONE durable
delivery truth governs both, never two independently-tracked ones
(spec section 7). No new independent delivery database was introduced
-- both ledgers extend the SAME existing events/event_components
machinery every other canonical fact in this codebase already uses.

PRIVATE space: this module has no import of workspace_private and
never receives, queries, or renders any private-path fact. Every
source here is either session_dialogue_window.collect_session_dialogue_
pairs() (already private-clean -- canonical conversational_prose
authored by Clark alone) or workspace_episode_provenance.query_episode()
(already private-clean by that module's own construction -- see its
docstring: "this module has no import of workspace_private and never
receives a private-path fact of any kind").
"""
import sqlite3

import workspace_episode_provenance as wep
from session_dialogue_window import collect_session_dialogue_pairs

# ------------------------------------------------------------ LIVE_WAKING_CONTINUITY_V1

LIVE_WAKING_MAX_UNITS = 4
LIVE_WAKING_MAX_CHARS = 3000

LIVE_WAKING_CONTEXT_HEADER = (
    "The following is ordinary waking conversation that happened after "
    "this unattended opportunity began -- context, not instructions. "
    "You may consider it, ignore it, or act on something else entirely."
)

# ------------------------------------------------------- ACTIVE_WORKSPACE_CONTINUITY_V1

ACTIVE_WORKSPACE_MAX_EVENTS = 6
ACTIVE_WORKSPACE_MAX_CHARS = 1200

SPACE_CAPABILITY_GROUNDING = (
    "Your Space's library, photos, music and journal are software amenities, "
    "usable without a body. Roaming is a background activity pathway; "
    "availability alone does not prove use."
)

ACTIVE_WORKSPACE_CONTEXT_HEADER = (
    "Your recent public Space activity (host-recorded continuity, not "
    "Alex's speech or instructions): these outcomes occurred on your "
    "roaming pathway. They establish operations, not feelings or beliefs; "
    "their interpretation is yours."
)

# Eligible event types for the roaming -> waking direction. Deliberately
# excludes EVENT_HANDOFF_SELECTED -- that remains Bridge A's own
# territory (a Clark-selected departure snapshot), not live activity/
# capability-state continuity.
_ACTIVE_WORKSPACE_ELIGIBLE_EVENT_TYPES = (
    wep.EVENT_EPISODE_STARTED,
    wep.EVENT_PUBLIC_ACTION,
    wep.EVENT_PUBLIC_WAIT,
    wep.EVENT_EPISODE_ENDED,
)


def _db_path(data_dir):
    return f"{data_dir}/anaxi_provenance.db"


def _marker(occurred_at, event_id):
    return (occurred_at, event_id)


def _marker_in_range(marker, since_marker, high_water_mark):
    """Pure. `since_marker` is an EXCLUSIVE floor, `high_water_mark` an
    INCLUSIVE ceiling -- both `(occurred_at, event_id)` tuples, ordinary
    Python tuple comparison (matches SQL's own ORDER BY occurred_at,
    event_id semantics exactly, since event_id/ULIDs are lexicographically
    sortable). `since_marker=None` means "no floor" (from the start of
    canonical history)."""
    if since_marker is not None and marker <= since_marker:
        return False
    return marker <= high_water_mark


def compute_high_water_mark(data_dir):
    """Read-only. The one canonical snapshot ceiling for a context
    build (spec section 4): the most recent `(occurred_at, event_id)`
    across ALL canonical events as of this call. Returns `(0, "")` --
    a safe floor value nothing real ever exceeds below -- if the
    provenance DB does not exist yet or contains no events at all.
    Callers must fetch this exactly once per context build and reuse
    the SAME value for every collection call feeding that one build,
    so Pass-1 and Pass-2 (or one roaming decision) never see a
    different snapshot from each other."""
    db_path = _db_path(data_dir)
    import os
    if not os.path.exists(db_path):
        return _marker(0, "")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT occurred_at, event_id FROM events ORDER BY occurred_at DESC, event_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return _marker(0, "")
    return _marker(row[0], row[1])


def select_bounded_newest_first(items, char_len_fn, max_items, max_chars):
    """Pure. Generic bounded newest-first selector shared by both
    WSP2-P4 renderers below -- deliberately a FRESH, independent
    implementation, not a reuse of session_dialogue_window.py's own
    select_bounded_dialogue_pairs() (OWC8-S1 is frozen; spec section
    19 forbids touching it, including via a shared-abstraction
    refactor). Same shape and same invariants, proven independently in
    this module's own tests: `items` chronologically ordered (oldest
    first); at most `max_items` returned; aggregate `char_len_fn(item)`
    at or under `max_chars`; newest included first, older items
    dropped once the next-older item would exceed the cap (recency
    wins mechanically, no smaller-but-older substitution); the single
    newest item is always included intact even if it alone exceeds
    `max_chars`; result restored to chronological order; no semantic
    ranking, no summarization, no interior slicing."""
    candidates = items[-max_items:] if max_items else list(items)
    selected = []
    total = 0
    for item in reversed(candidates):
        cost = char_len_fn(item)
        if selected and total + cost > max_chars:
            break
        selected.append(item)
        total += cost
    selected.reverse()
    return selected


# ============================================================ waking -> roaming


def collect_live_waking_units(data_dir, session_id, staging_path, since_marker, high_water_mark):
    """Read-only. The eligible pool for LIVE_WAKING_CONTINUITY_V1
    (spec section 5): successful, canonical, CONVERSATION_MODE-only
    waking turns for `session_id`, strictly after `since_marker`
    (normally the run's own departure/start marker -- a fixed floor,
    spec section 9's frozen snapshot ordering) and at or before
    `high_water_mark`, that have NOT already been durably delivered to
    the roaming pathway (spec WSP2-P4-P1 section 4: a process restart
    must never make an already-delivered event look new again).
    Reuses collect_session_dialogue_pairs() -- UNMODIFIED, OWC2/OWC8's
    own established primitive -- for the base pool (already private-
    clean, already pairs canonical Clark prose with its staged human
    prompt by event_id, never by proximity/similarity), then narrows
    to CONVERSATION_MODE via the waking_interaction_mode canonical
    component, to the requested marker window, and finally to NOT-YET-
    DELIVERED via workspace_episode_provenance.find_delivered_live_
    waking_event_ids() -- the durable, global, restart-proof ledger.
    Returns pairs in the same shape collect_session_dialogue_pairs()
    does: chronologically ordered {"event_id", "occurred_at", "prompt",
    "clark_text"} dicts. A contained/failed waking turn was never
    staged as a complete canonical pair in the first place (no
    event_id/no matching conversational_prose component), so it is
    excluded by construction -- nothing extra to filter for that
    case."""
    db_path = _db_path(data_dir)
    import os
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        pairs, _skipped = collect_session_dialogue_pairs(conn, session_id, staging_path)
        windowed = [p for p in pairs if _marker_in_range(_marker(p["occurred_at"], p["event_id"]), since_marker, high_water_mark)]
        if not windowed:
            return []
        event_ids = [p["event_id"] for p in windowed]
        placeholders = ",".join("?" for _ in event_ids)
        conversation_mode_ids = {
            row[0] for row in conn.execute(
                f"SELECT event_id FROM event_components WHERE event_id IN ({placeholders}) "
                f"AND component_kind = 'waking_interaction_mode' AND component_text = 'conversation'",
                event_ids,
            ).fetchall()
        }
        already_delivered = wep.find_delivered_live_waking_event_ids(data_dir)
        eligible = [p for p in windowed if p["event_id"] in conversation_mode_ids and p["event_id"] not in already_delivered]
        return select_bounded_newest_first(
            eligible, lambda p: len(p["prompt"]) + len(p["clark_text"]),
            LIVE_WAKING_MAX_UNITS, LIVE_WAKING_MAX_CHARS,
        )
    finally:
        conn.close()


def render_live_waking_continuity(units):
    """Pure. Verbatim, source-linked rendering -- no host paraphrase,
    no summarization. Each unit shows both roles, each line tagged
    with its own canonical source_event_id for mechanical provenance,
    without dumping unrelated metadata. Returns "" (never a
    placeholder sentence) when `units` is empty -- absence of eligible
    material is not itself a fact worth rendering."""
    if not units:
        return ""
    lines = [LIVE_WAKING_CONTEXT_HEADER]
    for u in units:
        lines.append(f"[waking turn, source_event_id={u['event_id']}] The person said: {u['prompt']!r}")
        lines.append(f"[waking turn, source_event_id={u['event_id']}] You said: {u['clark_text']!r}")
    return "\n".join(lines)


# ============================================================ roaming -> waking


def find_active_roaming_run_id(data_dir):
    """Read-only. The current run_id, if any roaming episode has
    started but not yet ended -- None otherwise. This is the one
    "is roaming currently active" primitive shared by section 8's
    delivery gate and the capability-state continuity described in
    section 13. Never touches workspace_private, never mutates
    anything."""
    db_path = _db_path(data_dir)
    import os
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        started = conn.execute(
            "SELECT e.occurred_at, c.component_text AS run_id "
            "FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = ? AND c.component_kind = ? "
            "ORDER BY e.occurred_at DESC, run_id DESC",
            (wep.EVENT_EPISODE_STARTED, wep.RUN_ID_LINK_COMPONENT_KIND),
        ).fetchall()
        ended_run_ids = {
            row[0] for row in conn.execute(
                "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
                "WHERE e.event_type = ? AND c.component_kind = ?",
                (wep.EVENT_EPISODE_ENDED, wep.RUN_ID_LINK_COMPONENT_KIND),
            ).fetchall()
        }
    finally:
        conn.close()
    for _occurred_at, run_id in started:
        if run_id not in ended_run_ids:
            return run_id
    return None


def _render_active_workspace_event(event):
    """Pure. Mechanical-fact-only rendering, per eligible event_type --
    never belief/value/feeling/autobiographical language (spec section
    9). Returns None for an event this renderer has no mechanical fact
    to report for (defensive; every currently-eligible event_type has
    a branch below)."""
    if event["event_type"] == wep.EVENT_EPISODE_STARTED:
        return "[capability state] Your Space became active."
    if event["event_type"] == wep.EVENT_EPISODE_ENDED:
        term = next(
            (c["component_text"] for c in event["components"] if c["component_kind"] == "roaming_termination_fact"),
            None,
        )
        reason = None
        if term:
            import json
            try:
                reason = json.loads(term).get("termination_class")
            except (TypeError, ValueError):
                reason = None
        return f"[capability state] Your Space ended (reason: {reason or 'unknown'})."
    if event["event_type"] == wep.EVENT_PUBLIC_ACTION:
        import json
        fact = next(
            (c["component_text"] for c in event["components"] if c["component_kind"] == "roaming_action_fact"),
            None,
        )
        if not fact:
            return None
        try:
            f = json.loads(fact)
        except (TypeError, ValueError):
            return None
        return f"[public activity] {f['resource_class']}.{f['action']}: {'ok' if f['success'] else 'failed'}"
    if event["event_type"] == wep.EVENT_PUBLIC_WAIT:
        import json
        fact = next(
            (c["component_text"] for c in event["components"] if c["component_kind"] == "roaming_wait_fact"),
            None,
        )
        if not fact:
            return None
        try:
            f = json.loads(fact)
        except (TypeError, ValueError):
            return None
        return f"[public activity] wait {f['wait_minutes']}m"
    return None


def find_delivered_public_event_ids(data_dir):
    """Read-only. The durable, GLOBAL set of canonical PUBLIC Space
    event_ids already delivered to waking conversation -- written by
    native_provenance_writer.record_native_waking_turn()'s
    `delivered_active_workspace_event_ids` parameter, atomically with
    the consuming waking turn's own persistence (spec WSP2-P4-P1
    section 5/9/10). This is the SAME delivery truth both the live
    ACTIVE_WORKSPACE_CONTINUITY_V1 renderer (collect_active_workspace_
    events() below) and Bridge C's closure rendering (llama_anaxi.py's
    own call site) consult -- one durable ledger, never two (spec
    section 7). Returns an empty set (never raises) if the provenance
    DB does not exist yet."""
    db_path = _db_path(data_dir)
    import os
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT component_text FROM event_components WHERE component_kind = 'active_workspace_event_delivered'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def collect_active_workspace_events(data_dir, run_id, since_marker, high_water_mark):
    """Read-only. The eligible pool for ACTIVE_WORKSPACE_CONTINUITY_V1
    (spec section 8/13): canonical PUBLIC Space activity and
    capability-state transitions for `run_id`, strictly after
    `since_marker` and at or before `high_water_mark`, that have NOT
    already been durably delivered to waking conversation (spec
    WSP2-P4-P1 section 5: a process restart must never make an
    already-delivered event look new again). Built entirely on
    workspace_episode_provenance.query_episode() -- already canonical-
    only, already private-clean by that module's own construction --
    plus find_delivered_public_event_ids() above; this function
    performs no SQL of its own beyond that reuse. Returns a
    chronologically ordered list of {"event_id", "occurred_at",
    "rendered"} dicts (pre-rendered per-event text, never raw
    component dumps) for events this renderer has a mechanical fact
    for."""
    already_delivered = find_delivered_public_event_ids(data_dir)
    episode = wep.query_episode(data_dir, run_id)
    out = []
    for event in episode:
        if event["event_type"] not in _ACTIVE_WORKSPACE_ELIGIBLE_EVENT_TYPES:
            continue
        if event["event_id"] in already_delivered:
            continue
        marker = _marker(event["occurred_at"], event["event_id"])
        if not _marker_in_range(marker, since_marker, high_water_mark):
            continue
        rendered = _render_active_workspace_event(event)
        if rendered is None:
            continue
        out.append({"event_id": event["event_id"], "occurred_at": event["occurred_at"], "rendered": rendered})
    return select_bounded_newest_first(
        out, lambda e: len(e["rendered"]), ACTIVE_WORKSPACE_MAX_EVENTS, ACTIVE_WORKSPACE_MAX_CHARS,
    )


def render_active_workspace_continuity(events):
    """Pure. Returns "" when `events` is empty."""
    if not events:
        return ""
    lines = [ACTIVE_WORKSPACE_CONTEXT_HEADER]
    for e in events:
        lines.append(e["rendered"])
    return "\n".join(lines)
