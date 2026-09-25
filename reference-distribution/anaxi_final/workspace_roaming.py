"""WSP2-S1: unattended waking workspace initiative -- the smallest
mechanism by which Clark, remaining on the ordinary WAKING substrate,
may independently choose to use his already-existing WSP1 bounded
workspace capabilities while Alex is absent.

This is UNATTENDED WAKING WORKSPACE INITIATIVE. It is explicitly NOT:
REM, sleep, memory consolidation, autonomous web access, Discord, a
hidden monologue, or a requirement to stay busy. Frozen:
PERMISSION != OBLIGATION -- authorization means Clark MAY explore/
read/inspect/journal-append/wait/stop; it never requires him to.

Reuses the existing WSP1 machinery UNCHANGED for the actual workspace
action: workspace_direction.validate_pass1_workspace_action()/
execute_workspace_action(), workspace_capability's own capability/
path-containment checks, and workspace_supervisor.resolve_canonical_
actor_id() for Clark attribution (never the literal "clark"). This
module invents exactly one new thing -- the roaming CHOICE itself
(act/wait/wait_for_human/stop + wait_minutes) -- and the small
background scheduler that offers it repeatedly while authorized.

WSP2-S3 additionally freezes: ACTION WITHOUT OBSERVABLE CONSEQUENCE IS
NOT USEFUL EXPLORATION -- a successful workspace action's own bounded
host result is retained as the single, process-local, short-lived
`last_workspace_observation` and shown to the next roaming choice
(never regenerated/summarized by a model, never durable, never
autobiographical memory); and PERMISSION TO ACT MUST INCLUDE A GENUINE
OPTION NOT TO BE ASKED AGAIN -- `wait_for_human` lets Clark decline
further unattended opportunities for the rest of the current run with
one decision, without claiming to detect Alex's presence or return.

Deliberately does NOT reuse workspace_supervisor.run_one_supervised_
workspace_action() -- that function's Pass 2 and conversational-turn
persistence (stage_and_record_native_waking_turn/log_entry/
RelationalHistory.record_event) treats a workspace action as something
said TO Alex, which is structurally wrong when Alex is absent. Pass 2
was only ever added at workspace_supervisor.py's own orchestration
layer, never inside workspace_direction.py itself -- so calling
workspace_direction's lower-level validate/execute functions directly
requires no Pass 2, no bypass, no rewrite of the proven WSP1 pathway,
and invents no second executor.

Authorization (workspace_roaming_authorized) is a separate state axis
from conversation_direction.direction_owner -- never conflated. Only
an explicit human host-control action may set it True (via the same
kind of canonical-human-actor-resolution mechanism as OWC7-S2's
direction control, resolved by the caller and passed in here already
authenticated -- this module does not itself touch the DB, mirroring
conversation_direction.py's own dependency-free design). Clark/model
output has no code path to authorization at all. Clark MAY stop his
own roaming run at any waking opportunity (roaming_act="stop") without
human approval -- declining further use of a capability he was
permitted, not transferring human-owned authority.

Operational state (last_roaming_act, roaming_action_count, etc.) is
bounded and mechanical only -- no free-form reasoning, no hidden
monologue, no automatic journal write, no automatic promotion to
autobiographical memory. DURABLE CONTROL TRACE != AUTOBIOGRAPHICAL
MEMORY; OBSERVABILITY != SELF-NARRATIVE. The trace here is never fed
into hippocampal retrieval, Kardia, the dialogue window, the journal,
or Sleep/REM.

The background worker is a daemon thread -- terminates automatically
when the hosting process exits; no orphan process, no external
scheduler, no Windows Task Scheduler entry, no persistence mechanism
that restarts roaming across a fresh process launch.
"""
import collections
import datetime
import json
import os
import sqlite3
import threading
import uuid

import context_budget
import workspace_capability as wc
import workspace_delivery
import workspace_direction
import workspace_episode_provenance as wep
import workspace_private
import workspace_public_continuity as wpc
from session_dialogue_window import collect_session_dialogue_pairs

# Pixel-bearing actions need the supervised image-model pass. Unattended
# roaming has no such pass, so it must not downgrade them to metadata while
# claiming the substantive action occurred. Derive this surface mechanically
# from the ordinary live surface; every non-pixel action remains identical.
ROAMING_ALLOWED_SURFACE = {
    resource_class: set(actions) - (
        {wc.VIEW, wc.VIEW_PAGE}
        if resource_class in {wc.PHOTOGRAPHS, wc.LIBRARY}
        else set()
    )
    for resource_class, actions in workspace_direction.LIVE_ALLOWED_SURFACE.items()
    # The shared Obsidian vault is the owner's collaborative space: never reached unattended (no
    # background note creation; no note text copied into roaming records).
    if resource_class != wc.NOTES
}

DEFAULT_TRACE_PATH = "workspace_roaming_trace.jsonl"

# WSP2-P3-I section 3/4: V1 bounds -- deliberately conservative and,
# where a directly-analogous existing bound already exists (session
# dialogue continuity), reused rather than duplicated with a second,
# independently-tunable number.
from session_dialogue_window import DEFAULT_MAX_TURNS as DEPARTURE_DIALOGUE_MAX_PAIRS
DEPARTURE_SOURCE_AGGREGATE_MAX_CHARS = 6000
HANDOFF_MAX_ITEMS = 3
HANDOFF_NOTE_MAX_CHARS = 280
HANDOFF_NOTE_AGGREGATE_MAX_CHARS = 700
HANDOFF_SOURCE_EXCERPT_MAX_CHARS = 900
HANDOFF_SOURCE_EXCERPT_AGGREGATE_MAX_CHARS = 2200
RECENT_PUBLIC_WINDOW_SIZE = 6
RECENT_PUBLIC_PREVIEW_MAX_CHARS = 240
RECENT_PUBLIC_AGGREGATE_MAX_CHARS = 1800

ROAMING_ACT_ACT = "act"
ROAMING_ACT_PRIVATE_ACT = "private_act"
ROAMING_ACT_WAIT = "wait"
ROAMING_ACT_WAIT_FOR_HUMAN = "wait_for_human"
ROAMING_ACT_STOP = "stop"
VALID_ROAMING_ACTS = {ROAMING_ACT_ACT, ROAMING_ACT_PRIVATE_ACT, ROAMING_ACT_WAIT, ROAMING_ACT_WAIT_FOR_HUMAN, ROAMING_ACT_STOP}

VALID_WAIT_MINUTES = {5, 15, 30, 60}
DEFAULT_POST_ACTION_WAIT_MINUTES = 5

# Production always uses 60 (real minutes). Tests may monkeypatch this
# module attribute down to a tiny value so a "5-minute wait" resolves
# in milliseconds rather than actually blocking for minutes -- the
# loop reads this dynamically (module-global lookup) rather than a
# hardcoded literal, so rebinding it here is sufficient and requires
# no other code change.
SECONDS_PER_MINUTE = 60

RAW_ROAMING_CHOICE_ALLOWED_FIELDS = {"roaming_act", "wait_minutes"}

RUN_STATUS_ENABLED = "enabled"
RUN_STATUS_ACTION = "action"
# WSP3-P1: no RUN_STATUS_PRIVATE_ACTION exists -- a successful private
# action deliberately writes no durable trace row at all (see the
# ROAMING_ACT_PRIVATE_ACT branch below), so there is no status
# constant to name it by.
RUN_STATUS_WAITING = "waiting"
RUN_STATUS_STOPPED_BY_CLARK = "stopped_by_clark"
RUN_STATUS_STOPPED_BY_HUMAN = "stopped_by_human"
RUN_STATUS_WAITING_FOR_HUMAN = "waiting_for_human"
RUN_STATUS_FAILED = "failed"

# ============================================ WSP2-P5: background-activity lifecycle
#
# GOVERNING PRINCIPLE (spec section 1): Your Space is normally
# available to Clark; background activity is a PATHWAY through that
# space, not the space itself. SPACE CAPABILITY != BACKGROUND-ACTIVITY
# ENABLEMENT != BACKGROUND WORKER != ROAMING EPISODE != INDIVIDUAL
# CONTROL DECISION. `background_activity_state` below is the ONE
# mechanical concept this gate introduces to keep those layers
# distinct -- it answers "why is no worker currently running" (or
# "active" when one is), and is the sole input to auto-start
# eligibility. `state["authorized"]` keeps its EXACT pre-existing,
# narrower meaning -- "is THIS specific worker thread currently
# permitted to keep looping" -- and is still what the loop itself
# checks every iteration; it is deliberately NOT reused as the
# higher-level gate, so a per-run flag ending (as it always has) never
# reads as "background access revoked forever."
BACKGROUND_STATE_IDLE = "idle"
BACKGROUND_STATE_ACTIVE = "active"
BACKGROUND_STATE_WAITING = "waiting"
BACKGROUND_STATE_WAITING_FOR_HUMAN = "waiting_for_human"
BACKGROUND_STATE_BACKOFF = "backoff"
BACKGROUND_STATE_PAUSED_BY_CLARK = "paused_by_clark"
BACKGROUND_STATE_PAUSED_BY_HUMAN = "paused_by_human"
BACKGROUND_STATE_FAULT_PAUSED = "fault_paused"
# WSP2-P5-P2 (spec section 4): host-owned AUTHORITY UNCERTAINTY only --
# "the host could not establish background lifecycle-control authority
# at launch/re-check time." NEVER means Clark paused, a human paused,
# a technical roaming failure occurred, or Your Space is unavailable --
# it means nothing more than "canonical lifecycle-control truth could
# not be read." Deliberately NOT PAUSED_BY_CLARK (would fabricate a
# Clark choice that never happened) and deliberately NOT FAULT_PAUSED
# (would falsely imply the ordinary P5 technical-failure counter --
# _record_technical_failure()/BACKOFF_MINUTES_BY_CONSECUTIVE_FAILURE --
# caused this; it never touches that counter at all).
BACKGROUND_STATE_LIFECYCLE_UNCERTAIN = "lifecycle_uncertain"

# spec section 8: 1st/2nd consecutive TECHNICAL failure -> a bounded
# backoff window (minutes); the 3rd consecutive technical failure
# enters FAULT_PAUSED instead of a further automatic backoff/retry --
# "no sub-hour rapid retries," and a firm stop to automatic retrying
# altogether once a pattern is established, rather than an ever-longer
# backoff that still eventually fires unattended. Only TECHNICAL
# failures increment this counter -- a valid Clark stop, wait_for_human,
# or human pause are not failures and never touch it (see
# _record_technical_failure()/_reset_technical_failure_count() below).
BACKOFF_MINUTES_BY_CONSECUTIVE_FAILURE = {1: 15, 2: 30}
FAULT_PAUSE_THRESHOLD = 3

ROAMING_SYSTEM_CONTENT = (
    "You are Clark. This is an unattended waking workspace opportunity -- "
    "no live conversation partner is present right now. You have been "
    "explicitly authorized by the person you usually talk with to use "
    "your local workspace (library, music, photographs, journal) "
    "independently while they are away. Using it is entirely optional."
)

ROAMING_TASK_INSTRUCTION = (
    "Choose what you want to do with this unattended opportunity.\n\n"
    "Choose roaming_act: 'act' to perform one ordinary workspace action "
    "now (library/journal/music/photographs), 'private_act' to perform "
    "one action in your own private text workspace instead -- private "
    "material is never shown to Alex, never spoken in conversation, and "
    "never remembered anywhere else unless you separately choose to say "
    "it yourself later -- 'wait' to take no action and be offered "
    "another opportunity later, 'wait_for_human' to decline any further "
    "unattended opportunities for the rest of this run -- you will not "
    "be asked again until a person interacts with you or roaming is "
    "re-authorized -- or 'stop' to end this unattended period until "
    "explicitly re-authorized. None of these is required or preferred "
    "over another; 'wait_for_human' is a genuine option not to be asked "
    "again, not a lesser choice.\n\n"
    "Also choose wait_minutes (5, 15, 30, or 60): for 'wait', how long "
    "before the next opportunity; for 'act' or 'private_act', the "
    "minimum time before the next opportunity after this action "
    "completes; for 'stop' or 'wait_for_human', this value is unused "
    "but must still be one of the four."
)

WORKSPACE_ACTION_PROMPT_STAND_IN = (
    "(Unattended workspace roaming -- no live conversation partner. "
    "Choose one workspace action.)"
)


class RoamingFailure:
    MALFORMED_CHOICE = "MALFORMED_CHOICE"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    INVALID_ROAMING_ACT = "INVALID_ROAMING_ACT"
    INVALID_WAIT_MINUTES = "INVALID_WAIT_MINUTES"


class AuthorizationFailure:
    UNAUTHORIZED_HUMAN_CONTROL = "UNAUTHORIZED_HUMAN_CONTROL"
    INVALID_AUTHORIZATION_TARGET = "INVALID_AUTHORIZATION_TARGET"


# WSP2-MA1 (spec section 15): a structural CONTROL-VALIDATION failure
# (the "MALFORMED_ACTION"-class of failure this gate exists to
# forensic) must be diagnosable at a useful, branch-agnostic level
# WITHOUT the durable trace itself revealing whether the failed choice
# was the ordinary `act` branch or the private `private_act` branch --
# recording `roaming_act="private_act"` (or even ordinary `"act"`,
# since its ABSENCE would then reveal private_act by elimination) plus
# the branch-specific failure code (workspace_direction.DirectionFailure
# and workspace_private.PrivateFailure share some names but not all --
# PATH_ESCAPE/NOT_FOUND/UNSUPPORTED_EXTENSION/IO_ERROR exist ONLY on
# the private side) would let a trace reader infer private use/non-use
# from the failure code alone. Both branches' STRUCTURAL validation
# failures (this map's exact, narrow scope -- NOT the separate,
# untouched EXECUTION-failure surface below, e.g. a denied/PATH_ESCAPE
# outcome after a structurally VALID private action) are translated to
# this one shared, generic vocabulary before ever reaching
# workspace_roaming_trace.jsonl, and `roaming_act`/`wait_minutes` are
# recorded as None for both branches alike -- true indistinguishability,
# not merely private_act-specific redaction (spec section 2: "do not
# infer private activity by omission or subtraction" applies just as
# much to the SHAPE of a redaction as to its content).
_GENERIC_STRUCTURAL_FAILURE_CODES = {
    "INVALID_JSON", "INVALID_TOP_LEVEL_TYPE", "MISSING_REQUIRED_STRUCTURE",
    "UNEXPECTED_STRUCTURE", "INVALID_FIELD_TYPE", "INVALID_ENUM",
    "INVALID_RELATION_BETWEEN_FIELDS", "CONTROL_VALIDATION_FAILURE_OTHER",
}

# workspace_direction.DirectionFailure/workspace_private.PrivateFailure
# structural codes (MALFORMED_ACTION/PROTOCOL_LEAKAGE/INVALID_ACTION
# already mean the SAME mechanical thing in both modules, by
# construction -- see each module's own validate_*() docstring) mapped
# onto the shared generic vocabulary above. MALFORMED_ACTION itself
# does not distinguish "not JSON" from "missing keys" from "wrong
# type" internally, so it maps to the honest catch-all rather than a
# falsely-precise generic class.
_STRUCTURAL_FAILURE_TO_GENERIC = {
    "MALFORMED_ACTION": "CONTROL_VALIDATION_FAILURE_OTHER",
    "PROTOCOL_LEAKAGE": "UNEXPECTED_STRUCTURE",
    "INVALID_ACTION": "INVALID_ENUM",
    "UNKNOWN_RESOURCE_CLASS": "INVALID_ENUM",
    "NOT_IN_ALLOWED_SURFACE": "INVALID_ENUM",
    "INVALID_RELATIVE_PATH": "INVALID_FIELD_TYPE",
    "INVALID_CONTENT": "INVALID_FIELD_TYPE",
}


def _generic_structural_failure_code(specific_code):
    """Pure. Translates a branch-specific structural-validation failure
    code into the shared, branch-agnostic vocabulary above -- see the
    module comment just above this function for why. Never raises;
    an unrecognized input degrades to the honest catch-all rather than
    silently passing the specific code through."""
    return _STRUCTURAL_FAILURE_TO_GENERIC.get(specific_code, "CONTROL_VALIDATION_FAILURE_OTHER")


# WSP3-P2 (spec section 5): EXECUTION-time (post-validation) failures
# are a separate failure class from the structural (parse/shape)
# failures above -- kept in their own vocabulary namespace rather than
# reused, so "the model's output shape was wrong" is never conflated
# with "a validly-shaped action failed when actually attempted."
#
# Deliberately collapsed to ONE shared generic value, not the fuller
# multi-class vocabulary spec section 5 offers as examples
# (EXECUTION_NOT_FOUND / EXECUTION_ACCESS_REJECTED /
# EXECUTION_UNSUPPORTED_RESOURCE / EXECUTION_IO_ERROR) -- a reported,
# deliberate tradeoff (spec section 14). The ordinary "not performed"
# branch has never distinguished its OWN failure reasons at this exact
# call site (only a single `performed: bool`, discarding execute_
# workspace_action()'s own boundary_result/rationale detail); reviving
# that detail honestly would mean parsing free-text rationale strings
# (fragile), and doing so would almost certainly leave an UNEVEN
# distribution across the two branches (e.g. only private_act failures
# would ever realistically show EXECUTION_NOT_FOUND-class results) --
# exactly the "inference by elimination" leak this gate exists to
# close (spec section 5/12). A single shared value is the narrowest
# representation that is genuinely, permanently symmetric between the
# ordinary and private branches, at the cost of execution-failure
# telemetry granularity spec section 14 explicitly permits trading
# away here.
EXECUTION_FAILURE_GENERIC_CODE = "EXECUTION_FAILURE_OTHER"


def _generic_execution_failure_code(specific_code):
    """Pure. Always returns the one shared generic execution-failure
    code, regardless of `specific_code` -- see the module comment
    above. Takes the specific code as an (unused) parameter so this
    function's call sites read the same way _generic_structural_
    failure_code()'s do, and so a future, PROVEN-symmetric, more
    granular mapping (if one is ever justified) has one function body
    to change, not several call sites."""
    return EXECUTION_FAILURE_GENERIC_CODE


class HandoffFailure:
    """WSP2-P3-I section 3: structural failure codes for the departure
    handoff-selection call -- mirrors RoamingFailure's own shape and
    the project-wide validate_pass1_*() convention exactly."""
    MALFORMED_HANDOFF = "MALFORMED_HANDOFF"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    TOO_MANY_ITEMS = "TOO_MANY_ITEMS"
    UNKNOWN_SOURCE_HANDLE = "UNKNOWN_SOURCE_HANDLE"


# --------------------------------------------------------- typed choice --


def validate_roaming_choice(raw_choice):
    """Structural validation only, mirrors conversation_direction.py's
    own validate_pass1_conversation_act() pattern exactly. Returns
    (validated_choice, None) or (None, RoamingFailure)."""
    if isinstance(raw_choice, str):
        try:
            raw = json.loads(raw_choice)
        except (TypeError, ValueError):
            return None, RoamingFailure.MALFORMED_CHOICE
    elif isinstance(raw_choice, dict):
        raw = raw_choice
    else:
        return None, RoamingFailure.MALFORMED_CHOICE

    if not isinstance(raw, dict):
        return None, RoamingFailure.MALFORMED_CHOICE
    if not RAW_ROAMING_CHOICE_ALLOWED_FIELDS.issubset(raw.keys()):
        return None, RoamingFailure.MALFORMED_CHOICE
    if set(raw.keys()) - RAW_ROAMING_CHOICE_ALLOWED_FIELDS:
        return None, RoamingFailure.PROTOCOL_LEAKAGE

    roaming_act = raw.get("roaming_act")
    if roaming_act not in VALID_ROAMING_ACTS:
        return None, RoamingFailure.INVALID_ROAMING_ACT

    wait_minutes = raw.get("wait_minutes")
    if wait_minutes not in VALID_WAIT_MINUTES:
        return None, RoamingFailure.INVALID_WAIT_MINUTES

    return {"roaming_act": roaming_act, "wait_minutes": wait_minutes}, None


def render_previous_observation(observation):
    """Pure. No model call, no summarization -- a direct, host-grounded
    rendering of the current last_workspace_observation (spec section
    4), explicitly labeled as the mechanical result of the previous
    workspace action, never a hidden thought or memory claim. `None`
    renders as an explicit "none", never omitted or left ambiguous."""
    if observation is None:
        return "Previous workspace observation: none"
    return (
        "Previous workspace observation (the actual mechanical result of "
        "your last workspace action -- not a memory, not a thought):\n"
        f"resource_class={observation.get('resource_class')!r}, "
        f"action={observation.get('action')!r}, "
        f"relative_path={observation.get('relative_path')!r}, "
        f"status={observation.get('status')!r}, "
        f"action_id={observation.get('action_id')!r}\n"
        f"bounded_result={json.dumps(observation.get('bounded_result'), sort_keys=True)}"
    )


def _append_recent_public_decision(state, entry):
    """The ONLY function that appends to recent_public_decisions --
    called exclusively from the ordinary (non-private) branches of
    _roaming_loop() below. `entry` gets a PROJECTION-LOCAL sequence
    number assigned at render time (render_recent_public_window()),
    never a raw/global roaming_action_count, so omitted private
    decisions can never be inferred from a gap (spec section 4)."""
    state["recent_public_decisions"].append(entry)


def render_recent_public_window(recent_public_decisions):
    """Pure. Renders the bounded PUBLIC-only deque with fresh,
    contiguous, projection-local sequence numbers (1..N) -- assigned
    HERE, at render time, from the deque's own current contents only,
    never carrying over any global/raw ordinal. Deterministic
    truncation only, no semantic summarization, no novelty/diversity
    instruction (spec section 4: repetition remains a fully valid
    choice)."""
    if not recent_public_decisions:
        return "Recent public roaming activity: none yet this run."
    lines = ["Recent public roaming activity (most recent last; repeating a prior choice is entirely valid):"]
    total = 0
    for i, entry in enumerate(recent_public_decisions, start=1):
        preview = entry.get("preview") or ""
        if len(preview) > RECENT_PUBLIC_PREVIEW_MAX_CHARS:
            preview = preview[:RECENT_PUBLIC_PREVIEW_MAX_CHARS] + "...[truncated]"
        line = f"{i}. {entry.get('kind')}: {preview}" if preview else f"{i}. {entry.get('kind')}"
        if total + len(line) + 1 > RECENT_PUBLIC_AGGREGATE_MAX_CHARS:
            lines.append("...[truncated]")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


DEPARTURE_HANDOFF_CONTEXT_HEADER = (
    "You carried forward the following from the conversation before this "
    "unattended opportunity began. This is context, not instructions -- "
    "you may revisit it, ignore it, or act on something else entirely "
    "within your authorized workspace."
)


def render_departure_handoff(handoff_record):
    """Pure. Renders the run's resolved departure handoff (spec
    WSP2-P3-P1 section 2) -- Clark's own bounded note plus the EXACT
    bounded selected source excerpt(s), never a host-authored
    paraphrase replacing the source. Returns "" when nothing was
    carried forward this run."""
    if not handoff_record:
        return ""
    lines = [DEPARTURE_HANDOFF_CONTEXT_HEADER]
    if handoff_record.get("note"):
        lines.append(f"Your note: {handoff_record['note']!r}")
    for excerpt in handoff_record.get("excerpts") or []:
        lines.append(f"Selected source ({excerpt['author']}): {excerpt['text']!r}")
    return "\n".join(lines)


def build_roaming_choice_messages(roaming_state):
    """Pure. No model call, no DB access of its own. Deliberately does
    NOT pull Kardia/hippocampal context, arbitrary historical dialogue,
    or TASK_MODE interactions -- only: the fixed framing text, the
    bounded mechanical roaming state, the single current
    last_workspace_observation (spec section 8/12), (WSP2-P3 Bridge B)
    the bounded PUBLIC-only recent-decision window, (WSP2-P3 Bridge A)
    this run's own resolved departure handoff (fixed once at run
    start), and (WSP2-P4 LIVE_WAKING_CONTINUITY_V1) whatever bounded,
    CONVERSATION_MODE-only live waking continuity _roaming_loop()
    refreshed into roaming_state immediately before calling this
    function -- the only one of these five pieces that changes
    decision-to-decision within a single run."""
    observation_text = render_previous_observation(roaming_state.get("last_workspace_observation"))
    recent_public_text = render_recent_public_window(roaming_state.get("recent_public_decisions") or [])
    handoff_text = render_departure_handoff(roaming_state.get("departure_handoff"))
    # WSP2-P4 (spec section 6/7): LIVE_WAKING_CONTINUITY_V1 -- refreshed
    # once per decision by _roaming_loop() below, immediately before
    # this function is called, and simply rendered here from whatever
    # roaming_state["live_waking_continuity_text"] already holds (""
    # when no new eligible waking turn exists). This function itself
    # does no DB access and makes no eligibility decision -- it only
    # renders the state it is handed, exactly like every other piece
    # of this task text.
    live_waking_text = roaming_state.get("live_waking_continuity_text") or ""
    task_text = (
        f"{ROAMING_TASK_INSTRUCTION}\n\n"
        f"Roaming authorized: {roaming_state.get('authorized')!r}\n"
        f"Allowed roaming_act values: {sorted(VALID_ROAMING_ACTS)!r}\n"
        f"Allowed wait_minutes values: {sorted(VALID_WAIT_MINUTES)!r}\n\n"
        f"Recent roaming state: last_roaming_act={roaming_state.get('last_roaming_act')!r}, "
        f"last_resource_class={roaming_state.get('last_resource_class')!r}, "
        f"last_relative_path={roaming_state.get('last_relative_path')!r}, "
        f"last_action_status={roaming_state.get('last_action_status')!r}, "
        f"roaming_action_count={roaming_state.get('roaming_action_count')!r}\n\n"
        f"{observation_text}\n\n"
        f"{recent_public_text}\n\n"
        f"{handoff_text}\n\n"
        f"{live_waking_text}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"roaming_act": "<act, private_act, wait, wait_for_human, or stop>", "wait_minutes": <5, 15, 30, or 60>}'
    )
    return [
        {"role": "system", "content": ROAMING_SYSTEM_CONTENT},
        {"role": "user", "content": task_text},
    ]


def _compose_roaming_stage1_budget(roaming_state, live_waking_units):
    """OWC9-P1: aggregate Stage-1 prompt-budget composition -- reuses
    context_budget.py's central authority UNCHANGED (spec section 3:
    "do not fork/copy the implementation"). Pure: no model call, no DB
    access, no mutation of `roaming_state`.

    HARD (spec section 6): the fixed system framing, the fixed task
    instruction + allowed-value/JSON-shape contract, and the small
    mechanical "Recent roaming state" line -- together the minimum
    needed to select a legal roaming act at all; none of it may
    silently disappear.

    SOFT, in context_budget.SOFT_TRIM_ORDER (spec section 7):
    RECENT_PUBLIC_ROAMING (Bridge B) -> ROAMING_HANDOFF (Bridge A) ->
    LIVE_WAKING_CONTINUITY -> LAST_WORKSPACE_OBSERVATION (spec section
    11: the one genuine content encounter -- most protected, dropped
    last, and only via its own existing bound, never re-truncated
    here).

    Returns a context_budget.CompositionResult. Callers use
    `.included_kind(...)`/`.delivered_source_ids(...)` to build the
    actual messages and to compute what may honestly be marked
    delivered -- this function decides nothing about either; it only
    composes."""
    recent_public_units = list(roaming_state.get("recent_public_decisions") or [])
    handoff_record = roaming_state.get("departure_handoff")
    observation = roaming_state.get("last_workspace_observation")

    hard_core_text = (
        f"{ROAMING_TASK_INSTRUCTION}\n\n"
        f"Allowed roaming_act values: {sorted(VALID_ROAMING_ACTS)!r}\n"
        f"Allowed wait_minutes values: {sorted(VALID_WAIT_MINUTES)!r}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"roaming_act": "<act, private_act, wait, wait_for_human, or stop>", "wait_minutes": <5, 15, 30, or 60>}'
    )
    hard_mechanical_text = (
        f"Roaming authorized: {roaming_state.get('authorized')!r}\n"
        f"Recent roaming state: last_roaming_act={roaming_state.get('last_roaming_act')!r}, "
        f"last_resource_class={roaming_state.get('last_resource_class')!r}, "
        f"last_relative_path={roaming_state.get('last_relative_path')!r}, "
        f"last_action_status={roaming_state.get('last_action_status')!r}, "
        f"roaming_action_count={roaming_state.get('roaming_action_count')!r}"
    )
    hard_core = context_budget.Contribution(
        context_budget.CORE_SYSTEM_CONTROL, ROAMING_SYSTEM_CONTENT + "\n\n" + hard_core_text, hard=True,
    )
    hard_mechanical = context_budget.Contribution(
        context_budget.MECHANICAL_STATE, hard_mechanical_text, hard=True,
    )

    soft_recent_public = context_budget.Contribution(
        context_budget.RECENT_PUBLIC_ROAMING, render_recent_public_window(recent_public_units),
        hard=False, droppable_units=recent_public_units, render_fn=render_recent_public_window,
    )
    soft_handoff = context_budget.Contribution(
        context_budget.ROAMING_HANDOFF, render_departure_handoff(handoff_record), hard=False,
    )
    soft_live_waking = context_budget.Contribution(
        context_budget.LIVE_WAKING_CONTINUITY, wpc.render_live_waking_continuity(live_waking_units),
        hard=False, droppable_units=list(live_waking_units), render_fn=wpc.render_live_waking_continuity,
        source_ids=[u["event_id"] for u in live_waking_units] if live_waking_units else None,
    )
    # The observation is the one genuine content encounter. If its result
    # cannot ride whole in the room the hard floor leaves, deliver a truthful
    # window of it (real prefix, rewritten continuation) rather than losing it
    # whole -- the same law as the supervised pathway's Pass 2.
    windowed_observation = observation
    if isinstance(observation, dict) and isinstance(observation.get("bounded_result"), dict):
        hard_floor = hard_core.cost + hard_mechanical.cost
        fitted, _info = workspace_delivery.fit_boundary_result(
            observation["bounded_result"], context_budget.ROAMING_MAX_PROMPT_BUDGET - hard_floor,
            render=lambda bounded: render_previous_observation(dict(observation, bounded_result=bounded)),
        )
        if fitted is not observation["bounded_result"]:
            windowed_observation = dict(observation, bounded_result=fitted)
    soft_observation = context_budget.Contribution(
        context_budget.LAST_WORKSPACE_OBSERVATION, render_previous_observation(windowed_observation), hard=False,
    )
    soft_observation.windowed_observation = windowed_observation

    return context_budget.compose_within_budget(
        [hard_core, hard_mechanical, soft_recent_public, soft_handoff, soft_live_waking, soft_observation],
        context_budget.ROAMING_MAX_PROMPT_BUDGET,
    )


def _roaming_stage1_messages_from_composition(roaming_state, composition_result):
    """Builds the actual Stage-1 messages from a (possibly trimmed)
    context_budget.CompositionResult, WITHOUT mutating `roaming_state`
    -- a shallow-copy 'render view' is passed to the existing, UNCHANGED
    build_roaming_choice_messages() instead (spec sections 3/10/11: do
    not redesign Bridge-B, do not implement new content-continuity
    architecture -- both renderers stay exactly as they were, simply
    given fewer/no units when the compositor trimmed or dropped them).
    Returns (messages, surviving_live_waking_units)."""
    render_state = dict(roaming_state)

    recent_public_contrib = composition_result.included_kind(context_budget.RECENT_PUBLIC_ROAMING)
    render_state["recent_public_decisions"] = (
        recent_public_contrib.droppable_units if recent_public_contrib is not None else []
    )

    handoff_contrib = composition_result.included_kind(context_budget.ROAMING_HANDOFF)
    render_state["departure_handoff"] = (
        roaming_state.get("departure_handoff") if handoff_contrib is not None else None
    )

    observation_contrib = composition_result.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION)
    render_state["last_workspace_observation"] = (
        getattr(observation_contrib, "windowed_observation", None) or roaming_state.get("last_workspace_observation")
        if observation_contrib is not None else None
    )

    live_waking_contrib = composition_result.included_kind(context_budget.LIVE_WAKING_CONTINUITY)
    surviving_live_waking_units = (
        live_waking_contrib.droppable_units if live_waking_contrib is not None else []
    )
    render_state["live_waking_continuity_text"] = wpc.render_live_waking_continuity(surviving_live_waking_units)

    return build_roaming_choice_messages(render_state), surviving_live_waking_units


def _roaming_context_budget_diagnostics(composition_result):
    """OWC9-P1 (spec section 18): the smallest generic/structural
    snapshot -- kind names, per-kind cost, trim/drop outcome, fits --
    NEVER rendered content, NEVER which branch (public/private) Clark
    selected. Process-local only (spec section 12); callers store this
    on roaming_state["last_stage1_context_budget"], never in
    workspace_roaming_trace.jsonl or any other durable store."""
    return {
        "context_ceiling": context_budget.CONTEXT_CEILING,
        "reserve": context_budget.ROAMING_GENERATION_RESERVE,
        "max_prompt_budget": composition_result.max_prompt_budget,
        "final_prompt_cost": composition_result.final_prompt_cost,
        "fits": composition_result.fits,
        "included_kinds": [c.kind for c in composition_result.included],
        "trimmed_kinds": list(composition_result.trimmed_kinds),
        "dropped_kinds": list(composition_result.dropped_kinds),
        "estimator_method": "utf8_byte_count_upper_bound",
    }


def build_workspace_action_messages():
    """Pure. No model call. Reuses workspace_direction.build_pass1_
    interface()/PASS1_TASK_INSTRUCTION/LIVE_ALLOWED_SURFACE completely
    unchanged -- the exact same production Pass-1 interface WSP1-S2's
    own supervised pathway uses, with an empty dialogue window and an
    honest synthetic current_user_message (never a fabricated Alex
    prompt) standing in for the absent live conversation."""
    pass1_iface = workspace_direction.build_pass1_interface(
        ROAMING_SYSTEM_CONTENT, [], WORKSPACE_ACTION_PROMPT_STAND_IN,
        ROAMING_ALLOWED_SURFACE,
    )
    task_text = (
        f"{pass1_iface['task_instruction']}\n\n"
        f"Available workspace capabilities: {pass1_iface['allowed_surface']}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"resource_class": "<resource class>", "action": "<action>", "relative_path": "<string, may be empty>", "content": "<string, may be empty>"}'
    )
    return [
        {"role": "system", "content": pass1_iface["system_content"]},
        {"role": "user", "content": pass1_iface["current_user_message"]},
        {"role": "user", "content": task_text},
    ]


def compose_workspace_action_stage2_budget():
    """OWC9-P4 section 2: aggregate Stage-2 (public) prompt-budget
    composition -- reuses context_budget.py's central authority
    unchanged, exactly like _compose_roaming_stage1_budget() above
    (spec: "do not invent a new cost architecture"). Every piece of
    build_workspace_action_messages()'s own output is a FIXED module
    constant (ROAMING_SYSTEM_CONTENT, the synthetic WORKSPACE_ACTION_
    PROMPT_STAND_IN, workspace_direction.PASS1_TASK_INSTRUCTION/
    LIVE_ALLOWED_SURFACE) -- no dialogue, no retrieval, no workspace
    continuity, no mutable result ever reaches this prompt (spec
    section 2's own "classify... any mutable result/context" turns up
    nothing to classify there), so all three contributions are HARD
    and none is droppable. Reuses WSP1_PASS1_MAX_PROMPT_BUDGET/reserve
    (spec section 2: "derive a reserve from the exact Stage-2
    structured output contract") because this IS, byte-for-byte, WSP1
    Pass-1's own schema (workspace_direction.STAGE2_ACTION_SCHEMA ==
    RAW_ACTION_ALLOWED_FIELDS) -- a justified reuse, not a blind one.
    build_workspace_action_messages() itself is unchanged and remains
    the single source of the actual message text; this function only
    costs it. Pure: no model call. Returns (messages, CompositionResult)."""
    messages = build_workspace_action_messages()
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, messages[0]["content"], hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, messages[1]["content"], hard=True)
    hard_task = context_budget.Contribution(context_budget.MECHANICAL_STATE, messages[2]["content"], hard=True)
    result = context_budget.compose_within_budget(
        [hard_core, hard_human, hard_task], context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
    )
    return messages, result


# ---------------------------------------------------- host authorization --


def apply_human_roaming_authorization(source_actor_id, canonical_human_actor_id, target_authorized):
    """Pure. Mirrors conversation_direction.apply_human_direction_
    control()'s exact authorization shape -- a separate function/state
    axis, never conflated with directional ownership. Fails closed on
    a malformed/mismatched source or a non-bool target. Returns
    (target_authorized, None) on success or (None, failure_code)."""
    if not isinstance(source_actor_id, str) or not source_actor_id:
        return None, AuthorizationFailure.UNAUTHORIZED_HUMAN_CONTROL
    if source_actor_id != canonical_human_actor_id:
        return None, AuthorizationFailure.UNAUTHORIZED_HUMAN_CONTROL
    if not isinstance(target_authorized, bool):
        return None, AuthorizationFailure.INVALID_AUTHORIZATION_TARGET
    return target_authorized, None


# ------------------------------------------------------------ state ------


def empty_roaming_state():
    """A fresh process's roaming state. `authorized` always starts
    False -- never inferred, never restored from a prior process/
    session, never backfilled from historical rows."""
    return {
        "authorized": False,
        "worker_thread": None,
        "last_roaming_act": None,
        "last_resource_class": None,
        "last_relative_path": None,
        "last_action_status": None,
        "recent_action_ids": [],
        "roaming_action_count": 0,
        "last_workspace_observation": None,
        # WSP2-P3 Bridge B: PUBLIC-only, bounded, rolling window --
        # appended to ONLY from the ordinary act/wait branches of
        # _roaming_loop() below, never from the ROAMING_ACT_PRIVATE_ACT
        # branch, so private activity can never enter it (spec section
        # 4's private-sequence-safety requirement).
        "recent_public_decisions": collections.deque(maxlen=RECENT_PUBLIC_WINDOW_SIZE),
        # WSP2-P3: minted fresh by start_roaming_worker() for each run.
        "run_id": None,
        # WSP2-P3-P1 section 2: the resolved departure handoff (note +
        # exact selected source excerpts) for THIS run, set once by
        # _perform_departure_handoff() before the ordinary decision
        # loop begins, then rendered into EVERY roaming-choice call
        # for the rest of the run -- bounded, immutable, never
        # re-selected or re-validated mid-run. None when nothing was
        # carried forward (no eligible dialogue, or Clark selected
        # nothing).
        "departure_handoff": None,
        # WSP2-P4/WSP2-P4-P1 (spec section 5/6): live waking continuity
        # -- refreshed EVERY decision (unlike departure_handoff, fixed
        # once). staging_path is set once at start (needed to resolve a
        # canonical Clark turn's matching staged human prompt, exactly
        # like Bridge A's own dialogue_pairs fetch already requires).
        # live_waking_since_marker is the run's own departure/start
        # marker (the fixed floor -- never look further back than
        # that, since anything before it was already Bridge A's
        # territory; frozen snapshot-ordering primitive, spec P4-P1
        # section 9). Novelty itself ("already delivered?") is no
        # longer decided by a process-local marker here at all -- see
        # workspace_public_continuity.find_delivered_live_waking_
        # event_ids(), the durable, restart-proof ledger WSP2-P4-P1
        # introduced specifically because a process-local cursor could
        # not survive a restart without creating false novelty.
        "staging_path": None,
        "live_waking_since_marker": None,
        "live_waking_continuity_text": "",
        # WSP2-P5: fresh-process default is IDLE -- immediately
        # auto-start-eligible, no human "enable" action required (spec
        # section 5). See the BACKGROUND_STATE_* block above for the
        # full state meaning.
        "background_activity_state": BACKGROUND_STATE_IDLE,
        "consecutive_technical_failures": 0,
        "backoff_until": None,  # epoch seconds; None = no backoff pending
        # OWC9-P1 (spec section 18): the most recent Stage-1 aggregate
        # context-budget composition result, generic/structural fields
        # only (kind names, per-kind costs, fits/trimmed/dropped) --
        # never rendered content, never which branch (public/private)
        # was chosen. Process-local only, overwritten every decision,
        # never written to workspace_roaming_trace.jsonl or any other
        # durable store (spec section 12: the compositor must stay
        # branch-neutral and reveal nothing durable about private use).
        "last_stage1_context_budget": None,
    }


_roaming_state_holder = {"state": None}
_stop_event = threading.Event()
_start_lock = threading.Lock()

MAX_RECENT_ACTION_IDS = 8


def get_roaming_state():
    if _roaming_state_holder["state"] is None:
        _roaming_state_holder["state"] = empty_roaming_state()
    return _roaming_state_holder["state"]


def reset_roaming_state():
    """Explicit reset only -- e.g. test isolation. Never called
    implicitly/automatically. Also clears any stop signal left over
    from a prior run in this process."""
    _roaming_state_holder["state"] = empty_roaming_state()
    _stop_event.clear()


def is_worker_running():
    state = get_roaming_state()
    thread = state.get("worker_thread")
    return thread is not None and thread.is_alive()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _record_technical_failure(state):
    """WSP2-P5 (spec section 7/8): called at every TECHNICAL failure
    termination point (structural/control validation failure,
    execution failure, canonical-preflight/persistence failure) --
    never at a valid Clark stop, wait_for_human, or human pause, none
    of which are failures. See BACKOFF_MINUTES_BY_CONSECUTIVE_FAILURE/
    FAULT_PAUSE_THRESHOLD above for the exact policy."""
    state["consecutive_technical_failures"] = state.get("consecutive_technical_failures", 0) + 1
    n = state["consecutive_technical_failures"]
    if n >= FAULT_PAUSE_THRESHOLD:
        state["background_activity_state"] = BACKGROUND_STATE_FAULT_PAUSED
        state["backoff_until"] = None
    else:
        state["background_activity_state"] = BACKGROUND_STATE_BACKOFF
        state["backoff_until"] = _now() + BACKOFF_MINUTES_BY_CONSECUTIVE_FAILURE[n] * SECONDS_PER_MINUTE


def _reset_technical_failure_count(state):
    """WSP2-P5 (spec section 8): called on any successful, structurally
    -valid Stage-2 progress -- an ordinary act, a private act, or a
    wait, each successfully executed/persisted this run."""
    state["consecutive_technical_failures"] = 0


def is_background_activity_auto_start_eligible(state=None):
    """Read-only. True iff a NEW episode may be started automatically
    right now (spec section 6): no worker already running, and the
    lifecycle state is neither a human/Clark pause, FAULT_PAUSED,
    WAITING_FOR_HUMAN, LIFECYCLE_UNCERTAIN, nor an unexpired BACKOFF
    window.

    WSP2-P5-P2 (spec section 5): LIFECYCLE_UNCERTAIN is ALWAYS
    ineligible -- consequential uncertainty fails closed. No worker
    starts, so (by construction, since nothing downstream of this
    check ever runs) no episode_started is written, no Bridge A
    handoff begins, and no model call occurs."""
    state = state if state is not None else get_roaming_state()
    if is_worker_running():
        return False
    lifecycle = state.get("background_activity_state", BACKGROUND_STATE_IDLE)
    if lifecycle in (
        BACKGROUND_STATE_PAUSED_BY_CLARK, BACKGROUND_STATE_PAUSED_BY_HUMAN,
        BACKGROUND_STATE_FAULT_PAUSED, BACKGROUND_STATE_WAITING_FOR_HUMAN,
        BACKGROUND_STATE_LIFECYCLE_UNCERTAIN,
    ):
        return False
    if lifecycle == BACKGROUND_STATE_BACKOFF:
        backoff_until = state.get("backoff_until")
        if backoff_until is not None and _now() < backoff_until:
            return False
    return True


def maybe_auto_start_background_activity(*, ask_llama_for_json, clark_actor_id, workspace_paths, session_id,
                                          trace_path=DEFAULT_TRACE_PATH, private_paths=None, data_dir=None,
                                          pipeline_key=None, dialogue_pairs=None, staging_path=None):
    """WSP2-P5 (spec section 6): the ONE auto-start path -- reuses
    start_roaming_worker() completely unchanged (never a second
    roaming-loop implementation), gated on is_background_activity_
    auto_start_eligible(). Returns True if a worker was actually
    started, False otherwise (already running, or not currently
    eligible) -- never raises on an ineligible call, so this is safe
    to call opportunistically (process start, after a released
    wait_for_human, after a pause clears, after backoff expires)
    without the caller needing to pre-check eligibility itself. Does
    NOT bypass canonical preflight, Bridge A, P4 shared continuity, or
    MA2's structured Stage-2 envelopes -- it is the exact same
    start_roaming_worker() every existing caller already uses."""
    state = get_roaming_state()
    if not is_background_activity_auto_start_eligible(state):
        return False
    state["authorized"] = True
    state["background_activity_state"] = BACKGROUND_STATE_ACTIVE
    return start_roaming_worker(
        ask_llama_for_json=ask_llama_for_json, clark_actor_id=clark_actor_id, workspace_paths=workspace_paths,
        session_id=session_id, trace_path=trace_path, private_paths=private_paths, data_dir=data_dir,
        pipeline_key=pipeline_key, dialogue_pairs=dialogue_pairs, staging_path=staging_path,
    )


def release_wait_for_human_and_maybe_resume(*, ask_llama_for_json, clark_actor_id, workspace_paths, session_id,
                                             trace_path=DEFAULT_TRACE_PATH, private_paths=None, data_dir=None,
                                             pipeline_key=None, dialogue_pairs=None, staging_path=None):
    """WSP2-P5 (spec section 10): called after a successful ordinary
    CONVERSATION_MODE waking turn -- the canonical event Clark's own
    wait_for_human choice was actually waiting for; never presence
    detection, never a timer. No-op (returns False, changes nothing)
    unless background_activity_state is currently WAITING_FOR_HUMAN."""
    state = get_roaming_state()
    if state.get("background_activity_state") != BACKGROUND_STATE_WAITING_FOR_HUMAN:
        return False
    state["background_activity_state"] = BACKGROUND_STATE_IDLE
    return maybe_auto_start_background_activity(
        ask_llama_for_json=ask_llama_for_json, clark_actor_id=clark_actor_id, workspace_paths=workspace_paths,
        session_id=session_id, trace_path=trace_path, private_paths=private_paths, data_dir=data_dir,
        pipeline_key=pipeline_key, dialogue_pairs=dialogue_pairs, staging_path=staging_path,
    )


def pause_background_activity_by_human():
    """WSP2-P5 (spec section 12): stops/suspends the worker (if
    running) and marks the lifecycle PAUSED_BY_HUMAN -- auto-start
    stays ineligible until resume_background_activity_by_human() is
    called; no timer ever clears a human pause on its own. Mirrors
    stop_workspace_roaming()'s existing effect on state["authorized"]
    exactly (unchanged); this ADDS the lifecycle label on top of it.
    Does not touch conversation direction, the private-space contract,
    or ordinary supervised workspace capability -- none of which this
    module has ever touched."""
    state = get_roaming_state()
    state["authorized"] = False
    state["background_activity_state"] = BACKGROUND_STATE_PAUSED_BY_HUMAN
    stop_roaming_worker()


def resume_background_activity_by_human():
    """WSP2-P5 (spec section 12): clears a human pause, making a new
    episode eligible again -- does not itself start a worker or
    fabricate continuity with whatever came before; the caller's own
    next maybe_auto_start_background_activity() call does that. A
    no-op if the lifecycle is not currently PAUSED_BY_HUMAN, FAULT_
    PAUSED, or (WSP2-P5-P1 addition -- spec section 9) PAUSED_BY_CLARK
    -- human Resume remains able to clear a Clark-owned pause too (it
    is no longer the SOLE way, since Clark's own typed resume_own_
    pause now also can, but it was never meant to stop working for
    that case). Reported distinctly by the caller if the specific
    prior state matters to the UI. Callers clearing a PAUSED_BY_CLARK
    state this way MUST separately reconcile the durable lifecycle-
    control ledger (see workspace_episode_provenance.record_
    background_lifecycle_control()) so a later restart can never
    resurrect a stale Clark-owned pause a human already cleared --
    this function itself has no data_dir/pipeline_key and cannot do
    that write; see llama_gui.py's resume_background_activity()."""
    state = get_roaming_state()
    if state.get("background_activity_state") in (
        BACKGROUND_STATE_PAUSED_BY_HUMAN, BACKGROUND_STATE_FAULT_PAUSED, BACKGROUND_STATE_PAUSED_BY_CLARK,
    ):
        state["background_activity_state"] = BACKGROUND_STATE_IDLE


def apply_clark_self_resume_if_valid():
    """WSP2-P5-P1 (spec section 6/8): the ONE function that may clear
    Clark's OWN background pause via his own typed waking choice.
    Idempotent/no-harm by construction (spec section 8's preferred
    choice among the legality-matrix options): a no-op, returning
    False, unless background_activity_state is CURRENTLY exactly
    BACKGROUND_STATE_PAUSED_BY_CLARK -- it never overrides PAUSED_BY_
    HUMAN or FAULT_PAUSED (spec sections 8/10 -- those remain human/
    host-owned authorities Clark's own resume_own_pause cannot reach),
    and never silently converts a Clark self-resume into a human
    resume. Returns True iff it actually cleared something -- the
    caller (llama_gui.py) uses that to decide whether an auto-start
    attempt and a durable reconciling write are warranted at all."""
    state = get_roaming_state()
    if state.get("background_activity_state") != BACKGROUND_STATE_PAUSED_BY_CLARK:
        return False
    state["background_activity_state"] = BACKGROUND_STATE_IDLE
    return True


CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED = "known_paused_by_clark"
CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED = "known_not_paused_by_clark"
CLARK_PAUSE_AUTHORITY_UNKNOWN = "authority_unknown"


def determine_clark_pause_authority(data_dir):
    """WSP2-P5-P2 (spec section 3): the narrowest explicit tri-state
    result capable of distinguishing "known: no unresolved Clark
    pause" from "the host cannot currently determine background
    lifecycle-control authority" -- never collapsed into a single
    best-effort bool the way WSP2-P5-P1's original reconstruct_clark_
    owned_pause_from_canonical_truth() did. That conflation was the
    exact defect this gate repairs: a genuine read/query FAILURE was
    silently treated identically to "definitely no pause on record,"
    which could wrongly let background activity auto-start over an
    unresolved Clark stop this host simply failed to see (spec
    section 1: CONSEQUENTIAL UNCERTAINTY FAILS CLOSED).

    KNOWN_NOT_PAUSED_BY_CLARK covers every genuinely-KNOWN "no
    unresolved pause" case, never distinguished further because none
    of them need to be (spec sections 13/14): a readable DB with zero
    lifecycle-control rows, and a readable DB whose latest control
    value is 'resumed' (regardless of whether Clark or a resolved
    human actually cleared it -- motive is never inferred here).

    AUTHORITY_UNKNOWN covers: any exception while attempting to read
    the DB, WITHOUT exception -- including the DB file not existing at
    all (WSP2-P5-P2a: find_latest_background_lifecycle_control() now
    raises ProvenanceStoreMissingError for this specific case, rather
    than the P5-P2-original "missing DB == readable-and-empty"
    conflation; an established installation's canonical history must
    never be silently treated as though it never existed merely
    because the store is unreachable -- e.g. Clark launched from an
    unrelated working directory, or the file deleted/moved), any
    genuine read/query failure against a present-but-corrupt file
    (a malformed/incomplete schema, a permissions/IO failure, or any
    other query failure), and an unrecognized/malformed stored control
    value (this module's own write path -- workspace_episode_
    provenance.record_background_lifecycle_control() -- can never
    produce one, since it validates `control` against VALID_
    BACKGROUND_LIFECYCLE_CONTROLS before ever writing, but this
    function does not trust that invariant blindly). Every one of
    these is a genuine READ FAILURE, categorically different from "no
    record exists," and none are ever collapsed together (spec section
    3's own "do not collapse NO RECORD and READ FAILURE"; spec section
    1D's "readable empty canonical history" != "canonical history
    unavailable")."""
    try:
        latest = wep.find_latest_background_lifecycle_control(data_dir)
    except Exception:
        return CLARK_PAUSE_AUTHORITY_UNKNOWN
    if latest is None or latest == wep.BACKGROUND_CONTROL_RESUMED:
        return CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED
    if latest == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK:
        return CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    return CLARK_PAUSE_AUTHORITY_UNKNOWN  # unrecognized value -- fail closed, never guess


def reconstruct_clark_owned_pause_from_canonical_truth(data_dir):
    """WSP2-P5-P1/P5-P2 (spec section 5): the one process-local state
    write below is the ONLY side effect here (everything else is
    read-only). Call at production launch, BEFORE background auto-
    start eligibility is ever evaluated -- process memory always
    starts empty on a fresh launch, so determine_clark_pause_
    authority()'s tri-state result is the sole source of truth for
    what to (re)establish:

      KNOWN_PAUSED_BY_CLARK    -> restore PAUSED_BY_CLARK (auto-start
                                   correctly becomes ineligible).
      KNOWN_NOT_PAUSED_BY_CLARK -> leave state exactly as empty_
                                   roaming_state() already set it
                                   (IDLE) -- ordinary P5 default
                                   availability applies unchanged.
      AUTHORITY_UNKNOWN        -> set LIFECYCLE_UNCERTAIN (auto-start
                                   correctly becomes ineligible for a
                                   DIFFERENT, honestly-labeled reason
                                   -- spec section 4: never fabricates
                                   a Clark choice that never happened).

    Also the one safe, narrow re-check path spec section 8 permits for
    human Resume while LIFECYCLE_UNCERTAIN (see llama_gui.py's own
    resume_background_activity()) and the natural re-establishment
    path spec section 7 asks about for a later healthy state -- no
    separate polling/retry/recovery-daemon mechanism exists or is
    needed; a later call (a subsequent launch, or an explicit human
    Resume) simply re-evaluates canonical truth fresh, exactly like
    this one does.

    Returns the tri-state result itself (CLARK_PAUSE_AUTHORITY_*) --
    strictly more informative than WSP2-P5-P1's original bool, which
    this signature change replaces; existing callers that only relied
    on the state-write side effect (llama_gui.py's launch_production_
    backend()) are unaffected by the return-value change."""
    authority = determine_clark_pause_authority(data_dir)
    state = get_roaming_state()
    if authority == CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED:
        state["background_activity_state"] = BACKGROUND_STATE_PAUSED_BY_CLARK
    elif authority == CLARK_PAUSE_AUTHORITY_UNKNOWN:
        state["background_activity_state"] = BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    else:  # KNOWN_NOT_PAUSED_BY_CLARK
        # Clears a PREVIOUSLY-set LIFECYCLE_UNCERTAIN (e.g. a later
        # re-read, such as human Resume's one safe re-check -- spec
        # section 8) back to ordinary availability. A fresh launch's
        # state is already IDLE, so this is a no-op there. Never
        # touches any OTHER lifecycle state this function has no
        # authority over (PAUSED_BY_HUMAN, FAULT_PAUSED, WAITING_FOR_
        # HUMAN, BACKOFF, ACTIVE, WAITING).
        if state.get("background_activity_state") == BACKGROUND_STATE_LIFECYCLE_UNCERTAIN:
            state["background_activity_state"] = BACKGROUND_STATE_IDLE
    return authority


# ------------------------------------------------------------- trace -----


def _new_trace_id():
    return f"wrtrace-{uuid.uuid4().hex[:16]}"


def record_roaming_trace(
    *,
    session_id,
    clark_actor_id,
    authorization_state,
    roaming_act,
    wait_minutes,
    workspace_action_id,
    workspace_action_status,
    roaming_action_count,
    run_status,
    trace_path=DEFAULT_TRACE_PATH,
):
    """Appends exactly one durable roaming-trace record. Never
    overwrites/truncates. Never carries free-form Pass-1 reasoning --
    only the bounded mechanical fields listed. OPERATIONAL TELEMETRY
    ONLY -- never read by hippocampal retrieval, Kardia, the dialogue
    window, the journal, or Sleep/REM (no import of any of those exists
    anywhere in this module)."""
    record = {
        "trace_id": _new_trace_id(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
        "clark_actor_id": clark_actor_id,
        "authorization_state": authorization_state,
        "roaming_act": roaming_act,
        "wait_minutes": wait_minutes,
        "workspace_action_id": workspace_action_id,
        "workspace_action_status": workspace_action_status,
        "roaming_action_count": roaming_action_count,
        "run_status": run_status,
    }
    parent = os.path.dirname(os.path.abspath(trace_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(trace_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return dict(record)


def query_roaming_trace(trace_path=DEFAULT_TRACE_PATH):
    """Read-only. Never modifies the trace file."""
    if not os.path.exists(trace_path):
        return []
    records = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


# ------------------------------------------------------- departure handoff

DEPARTURE_HANDOFF_TASK_INSTRUCTION = (
    "Before your unattended workspace opportunity begins, you may optionally "
    "select up to three matters from the conversation you just had to carry "
    "forward as context -- not instructions. You may revisit them, ignore "
    "them, or act on something else entirely within your authorized "
    "workspace. Selecting none is a completely valid choice."
)


def build_departure_handles(dialogue_pairs, max_pairs=DEPARTURE_DIALOGUE_MAX_PAIRS,
                             aggregate_max_chars=DEPARTURE_SOURCE_AGGREGATE_MAX_CHARS):
    """Pure. Turns `dialogue_pairs` (session_dialogue_window.
    collect_session_dialogue_pairs()'s own return shape -- each with
    "event_id", "prompt", "clark_text") into host-issued handles
    (d1, d2, ...), newest COMPLETE units preserved first when the
    aggregate character bound forces trimming (never mid-unit
    splicing -- a whole human-prompt or whole Clark-reply unit is kept
    or dropped, never cut). Each pair contributes two independently
    selectable units. Returns (handles_dict, ordered_handle_list).

    Source precision (WSP2-P3-I section 3): the human half of a pair
    has no canonical event_component of its own in the current schema
    (only Clark's own reply is a canonical component; the human
    prompt lives only in the noncanonical staging file) -- so its
    source reference is event-level (sequence=None); Clark's own half
    IS a distinct canonical component and gets component-level
    precision (sequence=1)."""
    # FS1 V0 workspace provenance has no principal visibility_scope.
    # Therefore no scoped family H/X pair may be copied into a public
    # workspace handoff and later become an unscoped continuity fact.
    # Legacy pairs (no scope column/value) retain the established path.
    eligible_pairs = [p for p in dialogue_pairs if p.get("visibility_scope") is None]
    bounded_pairs = eligible_pairs[-max_pairs:] if max_pairs else eligible_pairs
    units = []
    for pair in bounded_pairs:
        human_unit = {
            "author": "human", "text": pair["prompt"],
            "event_id": pair["event_id"], "sequence": None,
        }
        if pair.get("human_actor_id") is not None:
            human_unit["principal_actor_id"] = pair["human_actor_id"]
        units.append(human_unit)
        units.append({"author": "clark", "text": pair["clark_text"], "event_id": pair["event_id"], "sequence": 1})

    total = 0
    kept_reversed = []
    for unit in reversed(units):
        cost = len(unit["text"])
        if total + cost > aggregate_max_chars and kept_reversed:
            break  # always keep at least the single newest unit
        kept_reversed.append(unit)
        total += cost
    kept = list(reversed(kept_reversed))

    handles, ordered = {}, []
    for i, unit in enumerate(kept, start=1):
        handle = f"d{i}"
        handles[handle] = unit
        ordered.append(handle)
    return handles, ordered


def build_departure_handoff_messages(handles, ordered_handles):
    """Pure. No model call. think=False is set by the caller's own
    ask_llama_for_json() (llama_anaxi.py, unconditionally, since the
    Gate-A repair) -- not this module's concern."""
    if not ordered_handles:
        offered_text = "(no eligible prior conversation for this run)"
    else:
        offered_text = "\n".join(f"[{h}] ({handles[h]['author']}): {handles[h]['text']}" for h in ordered_handles)
    task_text = (
        f"{DEPARTURE_HANDOFF_TASK_INSTRUCTION}\n\n"
        f"Offered conversation, host-labeled:\n{offered_text}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"carry_forward": [{"source_handles": ["<handle>", ...], "note": "<your bounded note>"}, ...]}'
    )
    return [
        {"role": "system", "content": ROAMING_SYSTEM_CONTENT},
        {"role": "user", "content": task_text},
    ]


def validate_and_resolve_handoff_choice(raw_choice, handles):
    """Structural validation, mirroring validate_roaming_choice()'s own
    shape exactly. Clark supplies ONLY host-issued handles -- never a
    canonical ID directly; every handle is checked against the exact
    bounded `handles` dict that was actually offered THIS run, so a
    model-generated handle for a different/nonexistent unit is
    rejected the same way an unknown one is. Returns
    (resolved_items, None) or (None, HandoffFailure code). No
    semantic repair, no retry -- malformed or out-of-bound output is
    simply rejected."""
    if isinstance(raw_choice, str):
        try:
            raw = json.loads(raw_choice)
        except (TypeError, ValueError):
            return None, HandoffFailure.MALFORMED_HANDOFF
    elif isinstance(raw_choice, dict):
        raw = raw_choice
    else:
        return None, HandoffFailure.MALFORMED_HANDOFF

    if not isinstance(raw, dict) or set(raw.keys()) != {"carry_forward"}:
        return None, HandoffFailure.PROTOCOL_LEAKAGE
    items = raw["carry_forward"]
    if not isinstance(items, list):
        return None, HandoffFailure.MALFORMED_HANDOFF
    if len(items) > HANDOFF_MAX_ITEMS:
        return None, HandoffFailure.TOO_MANY_ITEMS

    resolved = []
    note_total = 0
    excerpt_total = 0
    for item in items:
        if not isinstance(item, dict) or set(item.keys()) != {"source_handles", "note"}:
            return None, HandoffFailure.MALFORMED_HANDOFF
        source_handles = item["source_handles"]
        note = item["note"]
        if not isinstance(source_handles, list) or not isinstance(note, str):
            return None, HandoffFailure.MALFORMED_HANDOFF
        for h in source_handles:
            if not isinstance(h, str) or h not in handles:
                return None, HandoffFailure.UNKNOWN_SOURCE_HANDLE
        bounded_note = note[:HANDOFF_NOTE_MAX_CHARS]
        note_total += len(bounded_note)
        if note_total > HANDOFF_NOTE_AGGREGATE_MAX_CHARS:
            return None, HandoffFailure.MALFORMED_HANDOFF
        excerpts = []
        for h in source_handles:
            text = handles[h]["text"][:HANDOFF_SOURCE_EXCERPT_MAX_CHARS]
            excerpt_total += len(text)
            # WSP2-P3-P1 section 3: source-LINKED, not merely source-
            # COPIED -- event_id/sequence carry the exact canonical
            # unit this excerpt came from (see build_departure_handles()'s
            # own docstring for why the human half is event-level only
            # and Clark's half is component-level, sequence=1 -- the
            # most precise reference the current schema actually has
            # for each).
            excerpts.append({
                "handle": h, "author": handles[h]["author"], "text": text,
                "event_id": handles[h]["event_id"], "sequence": handles[h]["sequence"],
            })
        if excerpt_total > HANDOFF_SOURCE_EXCERPT_AGGREGATE_MAX_CHARS:
            return None, HandoffFailure.MALFORMED_HANDOFF
        resolved.append({"note": bounded_note, "excerpts": excerpts})
    return resolved, None


def _canonical_preflight_ok(data_dir, pipeline_key, clark_actor_id):
    """WSP2-P3-P1 section 1: read-only check, performed BEFORE any
    episode/handoff write is attempted, that canonical episode
    recording is actually usable -- the provenance DB file exists,
    `pipeline_key` resolves, and both actor rows every canonical write
    in this module depends on (the shared host actor and the given
    Clark actor) already exist. Never writes anything. A False result
    here means a canonical write would fail anyway -- catching that
    BEFORE attempting record_episode_started() is strictly a
    diagnostic-cost saving, not a different correctness guarantee than
    catching the write's own exception would give; both paths fail
    closed identically."""
    db_path = f"{data_dir}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return False
    try:
        if conn.execute("SELECT 1 FROM pipelines WHERE pipeline_key = ?", (pipeline_key,)).fetchone() is None:
            return False
        if conn.execute("SELECT 1 FROM actors WHERE actor_id = ?", (wep.host_actor_id(),)).fetchone() is None:
            return False
        if conn.execute("SELECT 1 FROM actors WHERE actor_id = ?", (clark_actor_id,)).fetchone() is None:
            return False
        return True
    except Exception:
        return False
    finally:
        conn.close()


def _perform_departure_handoff(*, data_dir, run_id, clark_actor_id, ask_llama_for_json,
                                dialogue_pairs, pipeline_key, occurred_at):
    """WSP2-P3 Bridge A. Returns (True, None, handoff_record) on
    success -- including the deterministic empty-handoff path (whose
    `handoff_record` is None), which must ALSO durably persist before
    ordinary roaming begins (WSP2-P3-P1 section 1A: a canonical
    continuity failure fails closed, it does not silently degrade) --
    or (False, failure_reason, None) where failure_reason is
    'CHOICE_INVALID' (Clark's own handoff output failed structural
    validation) or 'CANONICAL_PERSISTENCE_FAILED' (the choice, if any,
    was structurally valid but could not be durably recorded). Both
    failure kinds mean the SAME thing to the caller -- ordinary
    roaming must not begin -- the distinction exists only so the
    noncanonical trace records an operator-diagnosable reason.

    `handoff_record` (spec WSP2-P3-P1 section 2), when not None, is
    {"note": <Clark's joined bounded note>, "excerpts": [...]} -- the
    caller stores this on roaming_state so build_roaming_choice_
    messages() can render it into EVERY subsequent ordinary roaming
    decision for the rest of this run, not just prove it reached
    canonical provenance once. Zero model calls when `dialogue_pairs`
    is empty (spec: 'absence of selectable context, not a host
    semantic choice')."""
    handles, ordered = build_departure_handles(dialogue_pairs)
    if not ordered:
        try:
            wep.record_episode_handoff(
                data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=pipeline_key,
                occurred_at=occurred_at, note="", source_excerpts=[], model_revision_id=None,
            )
            return True, None, None
        except Exception:
            return False, "CANONICAL_PERSISTENCE_FAILED", None

    raw_choice = ask_llama_for_json(build_departure_handoff_messages(handles, ordered))
    resolved, failure = validate_and_resolve_handoff_choice(raw_choice, handles)
    if failure is not None:
        return False, "CHOICE_INVALID", None

    notes = [item["note"] for item in resolved if item["note"]]
    excerpts = [e for item in resolved for e in item["excerpts"]]
    handoff_record = {"note": " / ".join(notes), "excerpts": excerpts} if (notes or excerpts) else None
    try:
        wep.record_episode_handoff(
            data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=pipeline_key,
            occurred_at=occurred_at, note=" / ".join(notes), source_excerpts=excerpts, model_revision_id=None,
        )
        return True, None, handoff_record
    except Exception:
        return False, "CANONICAL_PERSISTENCE_FAILED", None


# --------------------------------------------------------- worker loop ---


def _record(session_id, clark_actor_id, roaming_act, wait_minutes, workspace_action_id, workspace_action_status, run_status, trace_path):
    state = get_roaming_state()
    record_roaming_trace(
        session_id=session_id, clark_actor_id=clark_actor_id,
        authorization_state=state.get("authorized", False),
        roaming_act=roaming_act, wait_minutes=wait_minutes,
        workspace_action_id=workspace_action_id, workspace_action_status=workspace_action_status,
        roaming_action_count=state.get("roaming_action_count", 0),
        run_status=run_status, trace_path=trace_path,
    )


def _record_episode_public_action(data_dir, **kwargs):
    """WSP2-P3-P1 section 1B: REQUIRED, not best-effort, whenever
    `data_dir` was wired up for this run -- canonical continuity
    failure must fail closed, not silently degrade, so real public
    activity can never occur while its durable record silently
    disappears. Returns True on success, False on failure; the CALLER
    (the ACT branch of _roaming_loop() below) MUST stop the run
    immediately on False, never continue to a further decision.
    `data_dir` of None (an older/test caller that never wired up
    canonical recording at all) is the one legitimate case that
    returns True unconditionally -- there is nothing to fail, exactly
    like every other data_dir-gated code path in this module."""
    if not data_dir:
        return True
    try:
        wep.record_public_action(data_dir, **kwargs)
        return True
    except Exception:
        return False


def _record_episode_public_wait(data_dir, **kwargs):
    """Same required (non-best-effort) contract as
    _record_episode_public_action() above, for a 'wait' decision."""
    if not data_dir:
        return True
    try:
        wep.record_public_wait(data_dir, **kwargs)
        return True
    except Exception:
        return False


def _record_episode_ended_best_effort(data_dir, **kwargs):
    if not data_dir:
        return
    try:
        wep.record_episode_ended(data_dir, **kwargs)
    except Exception:
        pass


def _roaming_loop(ask_llama_for_json, clark_actor_id, workspace_paths, session_id, trace_path,
                   private_paths=None, data_dir=None, pipeline_key=None, dialogue_pairs=None, staging_path=None):
    """The unattended loop itself. Runs entirely in a daemon background
    thread -- terminates automatically on process exit. At most one
    roaming decision/action in flight: this function IS that single
    flow of control; nothing else ever calls execute_workspace_action()
    from this module. Checked at every loop-iteration boundary (never
    mid-action) so an in-progress action always finishes safely before
    a stop takes effect.

    `private_paths` (WSP3-S1) is optional -- when None, 'private_act'
    fails closed rather than crashing (spec section 10 support is
    additive; every pre-existing caller that never wires up a private
    root keeps working exactly as before).

    WSP2-P3: `data_dir`/`pipeline_key`/`dialogue_pairs` are ALSO
    optional and additive -- when data_dir is None, this function
    behaves EXACTLY as it did before this gate (noncanonical trace/
    action-log recording only, unchanged). When data_dir is given,
    Bridge A's departure handoff runs first (zero model calls if
    dialogue_pairs is empty), and every ordinary public decision is
    ALSO recorded canonically via workspace_episode_provenance.py,
    never from the ROAMING_ACT_PRIVATE_ACT branch."""
    state = get_roaming_state()
    run_id = state.get("run_id")
    termination_class = [wep.TERMINATION_UNKNOWN]  # mutable cell, set at each break point below

    if data_dir and run_id:
        # WSP2-P3-P1 section 1A: CANONICAL CONTINUITY FAILURE MUST FAIL
        # CLOSED, NOT SILENTLY DEGRADE. Every step below either
        # succeeds or prevents ordinary roaming from ever starting --
        # there is no best-effort/swallowed-exception path left here
        # (contrast the mid-run public-action/wait path further down,
        # and _record_episode_ended_best_effort, both deliberately
        # still best-effort for the narrower, disclosed reasons their
        # own docstrings give).
        occurred_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        canonical_ready = _canonical_preflight_ok(data_dir, pipeline_key, clark_actor_id)
        handoff_ok, failure_reason, handoff_record = False, "CANONICAL_PRECONDITION_FAILED", None
        if canonical_ready:
            try:
                wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id,
                                            pipeline_key=pipeline_key, occurred_at=occurred_at)
            except Exception:
                handoff_ok, failure_reason = False, "CANONICAL_EPISODE_START_FAILED"
            else:
                try:
                    handoff_ok, failure_reason, handoff_record = _perform_departure_handoff(
                        data_dir=data_dir, run_id=run_id, clark_actor_id=clark_actor_id,
                        ask_llama_for_json=ask_llama_for_json, dialogue_pairs=dialogue_pairs or [],
                        pipeline_key=pipeline_key, occurred_at=occurred_at,
                    )
                except Exception:
                    # An unexpected exception here (e.g. the model call
                    # itself raising) is NOT the same as a validated
                    # structural failure -- but with no resolved
                    # choice to trust either, failing closed is the
                    # correct, conservative response.
                    handoff_ok, failure_reason = False, "CANONICAL_PERSISTENCE_FAILED"
        if not handoff_ok:
            # Ordinary roaming must NEVER begin -- state fails closed
            # exactly like an ordinary malformed roaming choice would,
            # whether the cause was Clark's own invalid output or a
            # canonical persistence/precondition failure (spec section
            # 1A/11: "no fabricated episode, no fallback to
            # JSONL-only continuity").
            state["authorized"] = False
            # WSP2-P5 (spec section 7/8): a canonical-preflight/
            # handoff failure is a TECHNICAL failure -- counts toward
            # bounded backoff exactly like a mid-run structural/
            # execution failure does, never a permanent revocation.
            _record_technical_failure(state)
            _record(session_id, clark_actor_id, None, None, None, failure_reason, RUN_STATUS_FAILED, trace_path)
            if canonical_ready:
                # Only attempted if the DB was at least reachable --
                # best-effort here specifically (see
                # _record_episode_ended_best_effort's own docstring):
                # the run is already ending either way, so a failure
                # recording WHY is a diagnostic gap, not a further
                # continuity violation.
                try:
                    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=pipeline_key,
                                              occurred_at=occurred_at, termination_class=wep.TERMINATION_CONTROL_VALIDATION_FAILURE)
                except Exception:
                    pass
            with _start_lock:
                state["worker_thread"] = None
            return
        state["departure_handoff"] = handoff_record
        # WSP2-P4: the floor for LIVE_WAKING_CONTINUITY_V1 -- never
        # look further back than this run's own departure moment
        # (anything earlier already had its chance to cross via Bridge
        # A). event_id "" sorts before every real ULID, so this floor
        # only ever excludes turns strictly before this second.
        state["live_waking_since_marker"] = (occurred_at, "")
        state["staging_path"] = staging_path

    try:
        while True:
            if _stop_event.is_set() or not state.get("authorized"):
                # Defensive: also set authorized=False here even though
                # the normal caller contract (llama_gui.py's stop
                # handler) already sets it before signaling -- a stray
                # stop_roaming_worker() call with authorized still True
                # must never leave the state looking authorized after
                # the loop has actually exited.
                state["authorized"] = False
                state["last_workspace_observation"] = None
                workspace_private.reset_private_state()
                _record(session_id, clark_actor_id, None, None, None, None, RUN_STATUS_STOPPED_BY_HUMAN, trace_path)
                termination_class[0] = wep.TERMINATION_HUMAN_STOP
                break

            # WSP2-P4/WSP2-P4-P1 (spec section 5/6): refreshed fresh
            # EVERY decision, from canonical provenance alone, never
            # from workspace_roaming_trace.jsonl or any other
            # noncanonical log. A single explicit high-water mark is
            # read once here and reused for this one decision's
            # collection call, so this decision can never see a
            # partially-committed waking turn (spec section 4/9).
            # `since` is the run's own FIXED departure floor, never a
            # process-local delivery cursor -- novelty ("already
            # delivered?") is decided entirely by collect_live_waking_
            # units()'s own durable-ledger check now, so this refresh
            # is correct even immediately after a process restart.
            # Best-effort: any failure here (e.g. no provenance DB yet)
            # simply leaves live-waking continuity empty for this
            # decision -- it must never abort or fail closed an
            # otherwise-healthy roaming run merely because this READ-
            # ONLY, additive lookup had a problem.
            live_waking_units = []
            if data_dir and state.get("staging_path"):
                try:
                    hwm = wpc.compute_high_water_mark(data_dir)
                    since = state.get("live_waking_since_marker")
                    live_waking_units = wpc.collect_live_waking_units(data_dir, session_id, state["staging_path"], since, hwm)
                except Exception:
                    live_waking_units = []
                state["live_waking_continuity_text"] = wpc.render_live_waking_continuity(live_waking_units)

            # OWC9-P1: aggregate Stage-1 prompt-budget composition,
            # reusing context_budget.py's central authority unchanged
            # (spec section 3). Composed BEFORE the model call so a
            # hard-only overflow never reaches ask_llama_for_json() at
            # all (spec sections 6/13/14 -- same fail-closed discipline
            # OWC9 already established for ordinary waking).
            stage1_composition = _compose_roaming_stage1_budget(state, live_waking_units)
            state["last_stage1_context_budget"] = _roaming_context_budget_diagnostics(stage1_composition)

            if not stage1_composition.fits:
                # spec section 14: "A budget failure is a HOST
                # composition failure, not Clark selecting wait or
                # stop" -- no model call, no fabricated roaming_act, no
                # fake delivery. Routed through the SAME technical-
                # failure/bounded-backoff/RUN_STATUS_FAILED machinery
                # choice_failure already uses just below (spec section
                # 14 does not forbid backoff treatment, and this is
                # structurally the same class of host-side non-
                # decision), with context_budget.BUDGET_EXCEEDED as its
                # own distinct, honest recorded code -- never conflated
                # with a RoamingFailure structural-validation code.
                state["authorized"] = False
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                _record(session_id, clark_actor_id, None, None, None, context_budget.BUDGET_EXCEEDED, RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                break

            stage1_messages, surviving_live_waking_units = _roaming_stage1_messages_from_composition(state, stage1_composition)
            raw_choice = ask_llama_for_json(stage1_messages)
            # WSP2-P4-P1 (spec section 4/9/10), OWC9-P1 spec section 8
            # ("ELIGIBLE != DELIVERED"): durable delivery acknowledgment,
            # written ONLY after ask_llama_for_json() above returns
            # without raising, and ONLY for the units that actually
            # SURVIVED this decision's final composition and were sent
            # to the model -- an event trimmed out under aggregate
            # pressure must not receive durable
            # workspace_roaming_live_waking_delivered accounting merely
            # because it was originally eligible. "Delivered to model"
            # remains the narrowest true fact this architecture can
            # establish (the messages this call actually sent included
            # the surviving rendered units, and a response came back);
            # it does NOT claim the resulting roaming_act was itself
            # valid or successfully acted upon, which is a separate
            # question handled below exactly as before. Best-effort: a
            # failure writing this acknowledgment leaves the surviving
            # units eligible for re-delivery next decision rather than
            # crashing an otherwise-healthy run -- the safe direction to
            # fail in (at worst, redundant, never falsely marked
            # delivered when it wasn't).
            if data_dir and surviving_live_waking_units:
                try:
                    wep.record_live_waking_delivery(
                        data_dir, run_id=run_id, pipeline_key=pipeline_key,
                        occurred_at=int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                        delivered_event_ids=[u["event_id"] for u in surviving_live_waking_units],
                    )
                except Exception:
                    pass
            validated_choice, choice_failure = validate_roaming_choice(raw_choice)
            if choice_failure is not None:
                state["authorized"] = False
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                _record(session_id, clark_actor_id, None, None, None, choice_failure, RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                break

            roaming_act = validated_choice["roaming_act"]
            wait_minutes = validated_choice["wait_minutes"]
            state["last_roaming_act"] = roaming_act

            if roaming_act == ROAMING_ACT_STOP:
                # WSP2-P5 (spec section 11): a valid Clark-selected
                # stop is NOT a technical failure -- never touches the
                # backoff counter. Sets PAUSED_BY_CLARK, which (unlike
                # BACKOFF) never auto-resumes on a timer; only an
                # explicit later Clark choice or human resume clears it
                # (spec section 11's own disclosed V1 asymmetry -- see
                # this gate's report).
                state["authorized"] = False
                state["background_activity_state"] = BACKGROUND_STATE_PAUSED_BY_CLARK
                state["last_workspace_observation"] = None
                workspace_private.reset_private_state()
                _record(session_id, clark_actor_id, roaming_act, wait_minutes, None, None, RUN_STATUS_STOPPED_BY_CLARK, trace_path)
                # WSP2-P5-P1 (spec section 4): durable, canonical
                # evidence of Clark's own pause, so a later process
                # restart can correctly reconstruct PAUSED_BY_CLARK
                # (spec section 5) instead of silently defaulting back
                # to normal availability. Best-effort: this run is
                # already ending either way (mirrors _record_episode_
                # ended_best_effort's own established discipline just
                # below in this function) -- a failure here is a
                # diagnostic gap, not a further continuity violation.
                if data_dir:
                    try:
                        wep.record_background_lifecycle_control(
                            data_dir, actor_id=clark_actor_id, pipeline_key=pipeline_key,
                            occurred_at=int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                            control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK, run_id=run_id,
                        )
                    except Exception:
                        pass
                termination_class[0] = wep.TERMINATION_CLARK_STOP
                break

            if roaming_act == ROAMING_ACT_WAIT_FOR_HUMAN:
                # Clark declines further unattended opportunities for
                # the rest of THIS run only (spec section 9) -- never a
                # claim that Alex has returned, never presence
                # detection, never an automatic restart. Operationally
                # identical to STOP (authorized=False, worker exits,
                # zero further roaming model calls); the only
                # difference is the audit-trail reason (spec section
                # 10), not a hidden psychological state.
                #
                # WSP2-P5: not a technical failure either. Sets
                # WAITING_FOR_HUMAN, released only by
                # release_wait_for_human_and_maybe_resume() -- called
                # after the next successful ordinary CONVERSATION_MODE
                # waking turn, never by a timer (spec section 10).
                state["authorized"] = False
                state["background_activity_state"] = BACKGROUND_STATE_WAITING_FOR_HUMAN
                state["last_workspace_observation"] = None
                workspace_private.reset_private_state()
                _record(session_id, clark_actor_id, roaming_act, wait_minutes, None, None, RUN_STATUS_WAITING_FOR_HUMAN, trace_path)
                termination_class[0] = wep.TERMINATION_WAIT_FOR_HUMAN
                break

            if roaming_act == ROAMING_ACT_WAIT:
                _record(session_id, clark_actor_id, roaming_act, wait_minutes, None, None, RUN_STATUS_WAITING, trace_path)
                canonical_wait_ok = _record_episode_public_wait(
                    data_dir, run_id=run_id, pipeline_key=pipeline_key,
                    occurred_at=int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                    wait_minutes=wait_minutes, model_revision_id=None,
                )
                if not canonical_wait_ok:
                    # WSP2-P3-P1 section 1B: required canonical
                    # persistence failed AFTER this (harmless, no-
                    # filesystem-side-effect) wait decision was already
                    # noncanonically recorded -- stop immediately,
                    # never continue to a further decision while
                    # canonical continuity is broken. Nothing to
                    # "retain as already-existing mechanical evidence"
                    # beyond the noncanonical trace row above, since a
                    # wait has no other effect to preserve.
                    state["authorized"] = False
                    _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                    _record(session_id, clark_actor_id, None, None, None, "CANONICAL_PERSISTENCE_FAILED", RUN_STATUS_FAILED, trace_path)
                    termination_class[0] = wep.TERMINATION_PROCESS_INTERRUPTION
                    break
                # WSP2-P5 (spec section 8): a successfully persisted
                # wait IS structurally-valid roaming progress -- resets
                # the consecutive technical-failure count.
                _reset_technical_failure_count(state)
                state["background_activity_state"] = BACKGROUND_STATE_WAITING
                _append_recent_public_decision(state, {"kind": "wait", "preview": f"{wait_minutes}m"})
                if _stop_event.wait(timeout=wait_minutes * SECONDS_PER_MINUTE):
                    state["authorized"] = False
                    state["last_workspace_observation"] = None
                    workspace_private.reset_private_state()
                    _record(session_id, clark_actor_id, None, None, None, None, RUN_STATUS_STOPPED_BY_HUMAN, trace_path)
                    termination_class[0] = wep.TERMINATION_HUMAN_STOP
                    break
                state["background_activity_state"] = BACKGROUND_STATE_ACTIVE  # WSP2-P5: wait elapsed, back to active
                continue

            if roaming_act == ROAMING_ACT_PRIVATE_ACT:
                # Mechanically separate from ordinary ACT (spec section
                # 3/10/11): a completely different prompt-builder
                # (workspace_private.build_private_action_messages()),
                # a completely different executor
                # (workspace_private.execute_private_action()), and the
                # result lands ONLY in workspace_private's own
                # last_private_observation -- never in this module's
                # own state["last_workspace_observation"], and never
                # forwarded to ordinary Pass 2. If no private root was
                # ever wired up for this run, fail closed rather than
                # crash.
                if private_paths is None:
                    state["authorized"] = False
                    _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                    # WSP2-MA1: roaming_act/wait_minutes redacted and the
                    # failure code generalized -- see
                    # _generic_structural_failure_code()'s own module
                    # comment. This specific branch is a host
                    # configuration gap (private_paths never wired up),
                    # not a real model output, but it is recorded through
                    # the exact same private-safe shape as a genuine
                    # structural failure so it can never be distinguished
                    # from one by a trace reader.
                    _record(session_id, clark_actor_id, None, None, None,
                            _generic_structural_failure_code(workspace_private.PrivateFailure.INVALID_ACTION),
                            RUN_STATUS_FAILED, trace_path)
                    termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                    break

                # OWC9-P4 section 3: aggregate Stage-2 (private) prompt-
                # budget composition BEFORE the model call, same
                # fail-closed discipline as every other budgeted
                # pathway -- SOURCE CONTRACT ONLY, no production private
                # file/content/existence inspection here (see
                # workspace_private.compose_private_action_budget()'s
                # own docstring: it costs whatever last_private_
                # observation this process's own state already holds,
                # never reads the filesystem itself).
                private_state = workspace_private.get_private_state()
                private_stage2_composition = workspace_private.compose_private_action_budget(private_state)
                if not private_stage2_composition.fits:
                    state["authorized"] = False
                    _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                    _record(session_id, clark_actor_id, None, None, None,
                            context_budget.BUDGET_EXCEEDED, RUN_STATUS_FAILED, trace_path)
                    termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                    break

                # WSP2-MA2: Stage-2 structural envelope -- see
                # workspace_private.STAGE2_PRIVATE_ACTION_SCHEMA's own
                # module comment. validate_private_action() below is
                # completely unchanged and remains fully authoritative;
                # this only narrows what JSON SHAPE the model can emit
                # in the first place.
                raw_private_action = ask_llama_for_json(
                    workspace_private.private_action_messages_from_composition(private_state, private_stage2_composition),
                    structured_schema=workspace_private.STAGE2_PRIVATE_ACTION_SCHEMA,
                )
                validated_private_action, private_choice_failure = workspace_private.validate_private_action(raw_private_action)
                if private_choice_failure is not None:
                    state["authorized"] = False
                    _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                    # WSP2-MA1: see _generic_structural_failure_code()'s
                    # own module comment -- roaming_act/wait_minutes
                    # redacted, failure code generalized, so this
                    # structural private_act failure is recorded in the
                    # SAME shape an ordinary act's structural failure
                    # would be (see the action_failure branch below).
                    _record(session_id, clark_actor_id, None, None, None,
                            _generic_structural_failure_code(private_choice_failure), RUN_STATUS_FAILED, trace_path)
                    termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                    break

                private_result, private_failure = workspace_private.execute_private_action(private_paths, validated_private_action)

                if private_failure is not None:
                    # WSP3-P2: fail closed exactly like a denied/failed
                    # ordinary action (spec section 6) -- only the
                    # SHARED GENERIC execution-failure class is ever
                    # recorded now (see _generic_execution_failure_code()
                    # module comment below), never the branch-specific
                    # PrivateFailure name, never a filename/path/content
                    # (spec section 7/WSP3-P1 section 5), and never
                    # roaming_act/wait_minutes -- identical redaction
                    # shape to the ordinary "not performed" branch below,
                    # so this record can never be told apart from one.
                    # The run ends here, so incrementing
                    # roaming_action_count only for this failed cycle
                    # cannot create a detectable gap against later trace
                    # rows (there are none -- the run is over).
                    state["authorized"] = False
                    state["roaming_action_count"] = state.get("roaming_action_count", 0) + 1
                    _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                    _record(session_id, clark_actor_id, None, None, None,
                            _generic_execution_failure_code(private_failure["failure_class"]), RUN_STATUS_FAILED, trace_path)
                    termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                    break

                # WSP2-P5 (spec section 8): a successfully executed
                # private action IS structurally-valid roaming progress
                # -- resets the consecutive technical-failure count,
                # exactly like a successful ordinary act/wait does.
                _reset_technical_failure_count(state)

                # WSP3-P1 section 4: a SUCCESSFUL private action writes
                # NO durable roaming-trace row at all -- not even the
                # content-free RUN_STATUS_PRIVATE_ACTION marker WSP3-S1
                # used to emit. roaming_action_count is also
                # deliberately left unincremented here: since it is
                # embedded in every later ordinary trace row, bumping it
                # for an invisible event would itself create a
                # detectable gap (this ordinary row's count jumped by
                # more than the number of visible ACT rows) that reveals
                # a private action happened even without naming it.
                # Private continuity is unaffected -- only the DURABLE
                # trace is suppressed; the transient, process-local
                # last_private_observation is set exactly as before.
                workspace_private.set_last_observation(private_result)

                post_action_wait = max(wait_minutes, DEFAULT_POST_ACTION_WAIT_MINUTES)
                if _stop_event.wait(timeout=post_action_wait * SECONDS_PER_MINUTE):
                    state["authorized"] = False
                    state["last_workspace_observation"] = None
                    workspace_private.reset_private_state()
                    _record(session_id, clark_actor_id, None, None, None, None, RUN_STATUS_STOPPED_BY_HUMAN, trace_path)
                    termination_class[0] = wep.TERMINATION_HUMAN_STOP
                    break
                continue

            # roaming_act == ROAMING_ACT_ACT
            # OWC9-P4 section 2: aggregate Stage-2 prompt-budget
            # composition BEFORE the model call, same fail-closed
            # discipline as Stage-1's own composition above -- a
            # hard-only overflow never reaches the model call below at
            # all. In practice this Stage-2 prompt is entirely FIXED
            # content (see compose_workspace_action_stage2_budget()'s
            # own docstring) and always fits; the check still runs
            # every time so a future change to any of its fixed
            # constants cannot silently grow past budget unnoticed.
            stage2_public_messages, stage2_public_composition = compose_workspace_action_stage2_budget()
            if not stage2_public_composition.fits:
                state["authorized"] = False
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                _record(session_id, clark_actor_id, None, None, None, context_budget.BUDGET_EXCEEDED, RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                break

            # WSP2-MA2: Stage-2 structural envelope -- see
            # workspace_direction.STAGE2_ACTION_SCHEMA's own module
            # comment. validate_pass1_workspace_action() below is
            # completely unchanged and remains fully authoritative;
            # this only narrows what JSON SHAPE the model can emit in
            # the first place.
            raw_action = ask_llama_for_json(
                stage2_public_messages, structured_schema=workspace_direction.STAGE2_ACTION_SCHEMA,
            )
            validated_action, action_failure = workspace_direction.validate_pass1_workspace_action(
                raw_action, ROAMING_ALLOWED_SURFACE,
            )
            if action_failure is not None:
                state["authorized"] = False
                state["last_action_status"] = action_failure
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                # WSP2-MA1: roaming_act/wait_minutes redacted and the
                # failure code generalized, mirroring the private_act
                # structural-failure branches exactly (see
                # _generic_structural_failure_code()'s own module
                # comment) -- an ordinary act's structural failure and a
                # private_act's structural failure are now recorded in
                # the IDENTICAL shape, so this branch alone can never be
                # what distinguishes them. state["last_action_status"]
                # above keeps the specific code process-locally only
                # (never durable, never cross-process) -- unaffected.
                _record(session_id, clark_actor_id, None, None, None,
                        _generic_structural_failure_code(action_failure), RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                break

            boundary_result, performed = workspace_direction.execute_workspace_action(
                workspace_paths, validated_action, clark_actor_id,
            )
            action_id = f"wraction-{uuid.uuid4().hex[:16]}"
            state["last_resource_class"] = validated_action["resource_class"]
            state["last_relative_path"] = validated_action["relative_path"]
            state["last_action_status"] = "performed" if performed else "denied"
            recent = list(state.get("recent_action_ids") or [])
            recent.append(action_id)
            state["recent_action_ids"] = recent[-MAX_RECENT_ACTION_IDS:]

            if not performed:
                # Fail closed on a denied action (spec section 16):
                # no silent capability expansion, no retry with a
                # different action -- end the run, mechanically record
                # the denial exactly as it happened. Denial is not
                # reinterpreted as permission, and no observation is
                # exposed since the run is already ending here.
                #
                # WSP3-P2: roaming_act/wait_minutes/action_id redacted
                # and the literal "denied" string replaced with the
                # SAME shared generic execution-failure code the
                # private_act execution-failure branch above now uses
                # (see _generic_execution_failure_code()'s own module
                # comment) -- this is the deliberate symmetric tradeoff
                # spec section 14 asks for: "act" alone was never
                # sensitive, but showing it here while the private
                # branch must stay redacted would make redaction ITSELF
                # the tell (spec section 5: "inference by elimination").
                # state["last_action_status"] above keeps "denied"
                # process-locally only -- never durable, never
                # cross-process visible, unaffected by this change.
                state["authorized"] = False
                state["roaming_action_count"] = state.get("roaming_action_count", 0) + 1
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                _record(session_id, clark_actor_id, None, None, None,
                        _generic_execution_failure_code(None), RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_CONTROL_VALIDATION_FAILURE
                break

            state["roaming_action_count"] = state.get("roaming_action_count", 0) + 1
            # The ONE current observation: replaces whatever was there
            # before (spec sections 2/5/6) -- taken directly from the
            # existing WSP1/WSP2 executor's own bounded result, never
            # regenerated or summarized by a model. A PDF library.read
            # arrives here already as WSP2-S2's bounded extracted text;
            # no separate PDF handling exists in this module.
            state["last_workspace_observation"] = {
                "resource_class": validated_action["resource_class"],
                "action": validated_action["action"],
                "relative_path": validated_action["relative_path"],
                "status": "performed",
                "bounded_result": boundary_result,
                "action_id": action_id,
            }
            _record(session_id, clark_actor_id, roaming_act, wait_minutes, action_id, "performed", RUN_STATUS_ACTION, trace_path)

            is_journal_append = validated_action["resource_class"] == "journal" and validated_action["action"] == "append"
            canonical_action_ok = _record_episode_public_action(
                data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=pipeline_key,
                occurred_at=int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                resource_class=validated_action["resource_class"], action=validated_action["action"],
                relative_path=validated_action["relative_path"], success=True, action_id_backlink=action_id,
                model_revision_id=None,
                journal_text=validated_action.get("content") if is_journal_append else None,
                external_result_text=None if is_journal_append else json.dumps(boundary_result, sort_keys=True),
            )
            if not canonical_action_ok:
                # WSP2-P3-P1 section 1B: the workspace action ITSELF
                # already, mechanically, irreversibly happened (this
                # gate does not attempt filesystem rollback) and is
                # ALREADY correctly recorded in the noncanonical
                # trace/action-log above -- that mechanical evidence
                # is retained, not falsely erased. What stops here is
                # further roaming: no next decision is offered while
                # canonical continuity is broken, so no MORE
                # uncanonical public activity can accumulate.
                state["authorized"] = False
                state["last_workspace_observation"] = None
                _record_technical_failure(state)  # WSP2-P5: technical failure -> bounded backoff
                _record(session_id, clark_actor_id, None, None, None, "CANONICAL_PERSISTENCE_FAILED", RUN_STATUS_FAILED, trace_path)
                termination_class[0] = wep.TERMINATION_PROCESS_INTERRUPTION
                break
            # WSP2-P5 (spec section 8): a successfully executed AND
            # canonically persisted ordinary action IS structurally-
            # valid roaming progress -- resets the consecutive
            # technical-failure count.
            _reset_technical_failure_count(state)
            _append_recent_public_decision(state, {
                "kind": f"{validated_action['resource_class']}.{validated_action['action']}",
                "preview": validated_action.get("content") if is_journal_append else "",
            })

            post_action_wait = max(wait_minutes, DEFAULT_POST_ACTION_WAIT_MINUTES)
            if _stop_event.wait(timeout=post_action_wait * SECONDS_PER_MINUTE):
                state["authorized"] = False
                state["last_workspace_observation"] = None
                workspace_private.reset_private_state()
                _record(session_id, clark_actor_id, None, None, None, None, RUN_STATUS_STOPPED_BY_HUMAN, trace_path)
                termination_class[0] = wep.TERMINATION_HUMAN_STOP
                break
            continue
    finally:
        if data_dir and run_id:
            _record_episode_ended_best_effort(
                data_dir, run_id=run_id, pipeline_key=pipeline_key,
                occurred_at=int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                termination_class=termination_class[0],
            )
        with _start_lock:
            state["worker_thread"] = None


def start_roaming_worker(*, ask_llama_for_json, clark_actor_id, workspace_paths, session_id, trace_path=DEFAULT_TRACE_PATH,
                          private_paths=None, data_dir=None, pipeline_key=None, dialogue_pairs=None, staging_path=None):
    """Starts the background roaming loop as a daemon thread. No-op --
    returns False, starts nothing -- if a worker is already running
    (spec section 16: duplicate start fails closed, never overlapping
    cycles). Returns True if a new worker was started. Caller is
    responsible for having already set state['authorized'] = True and
    for authorization itself (this function does not check who is
    calling it -- it is never reachable from Clark/model output, only
    from the GUI's own host-control handler, exactly like
    conversation_direction.apply_human_direction_control()).

    `private_paths` (WSP3-S1, optional) offers Clark the 'private_act'
    roaming choice for this run when provided; omitting it leaves
    'private_act' offered structurally but failing closed on choice
    (existing callers that never pass it are completely unaffected).

    WSP2-P3 (`data_dir`/`pipeline_key`/`dialogue_pairs`, all optional):
    when `data_dir` is given, a fresh run_id is minted HERE -- once per
    successful start, never reused across a stop/restart -- and Bridge
    A's departure handoff runs as the very first thing inside the
    background thread (never here, never blocking this synchronous
    button-click handler -- see _roaming_loop()'s own docstring).
    `dialogue_pairs` is the CALLER's already-fetched bounded departure
    dialogue (a cheap DB read, done by the caller for the same reason
    the canonical-actor resolution already happens there); this
    function never opens a DB connection itself."""
    state = get_roaming_state()
    with _start_lock:
        if is_worker_running():
            return False
        _stop_event.clear()
        # A fresh roaming authorization/run begins with no prior
        # observation (spec section 5) -- never carries state over
        # from a previous run in the same process.
        state["last_workspace_observation"] = None
        state["recent_public_decisions"] = collections.deque(maxlen=RECENT_PUBLIC_WINDOW_SIZE)
        state["run_id"] = wep.generate_episode_run_id() if data_dir else None
        workspace_private.reset_private_state()
        thread = threading.Thread(
            target=_roaming_loop,
            args=(ask_llama_for_json, clark_actor_id, workspace_paths, session_id, trace_path, private_paths,
                  data_dir, pipeline_key, dialogue_pairs, staging_path),
            daemon=True,
        )
        state["worker_thread"] = thread
    thread.start()
    return True


def stop_roaming_worker():
    """Signals the worker to stop at its next loop-iteration boundary
    -- never interrupts an in-progress execute_workspace_action() call
    (spec section 13). Safe to call even if no worker is running."""
    _stop_event.set()
