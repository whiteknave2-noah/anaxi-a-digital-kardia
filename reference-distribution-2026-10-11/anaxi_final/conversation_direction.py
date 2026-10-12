"""OWC5-S1/OWC7-S1: typed conversational direction pathway for ordinary
conversation-mode waking, plus explicit directional-ownership control.

Frozen principle: CONVERSATIONAL DIRECTION MAY BE AN OBSERVABLE CLARK
ACT RATHER THAN INFERRED FROM EXPRESSION. WORKING STATE != LONG-TERM
MEMORY != BELIEF != HIDDEN REASONING.

Mirrors the already-proven ANAXI two-stage pattern (HDI2/API2): Pass 1
selects a typed act from a closed enum; the host deterministically
transitions session working state from that act (never from prose);
Pass 2 naturally expresses the already-selected act. No semantic
auditor ever reinterprets Pass 2's wording back into a different act.

OWC7-S1 adds a second, orthogonal control dimension -- CONVERSATIONAL
ACT (what Clark does) vs DIRECTIONAL OWNERSHIP (who owns the next
directional choice) -- per OWC7-D1's frozen design. Three states only
in this gate: human / clark / unknown ("shared" deferred, not
implemented). Owner-authority invariant, enforced structurally:
  - OWNER MAY RELINQUISH OWN AUTHORITY.
  - NON-OWNER MAY REQUEST AUTHORITY (request never itself transfers it).
  - NON-OWNER MAY NOT SEIZE AUTHORITY.
  - direction_owner changes ONLY through (1) an authenticated human
    host-control act, or (2) Clark explicitly relinquishing direction
    while Clark is the current owner -- resolving to `unknown`, never
    `human` (Clark may surrender what he owns; he may not assign
    ownership to Alex on Alex's behalf).
Pass-1 free-form `content` is removed from the active control schema
in this gate -- Pass 1 returns control decisions, not free-form
reasoning (spec section 7).

Working state is session/process-local only (module-level state,
mirroring llama_anaxi.py's `_native_session_state`/`_interaction_mode_
state`) -- never persisted to any database, never promoted to
hippocampal memory or canonical belief state. A fresh process/session
starts with an empty working set.

Pure module except for the two small, explicit, session-scoped state
holders at the bottom (get_working_set/reset_working_set, and the
diagnostic trace holder) -- no model call, no DB access, no Kardia/
hippocampus interaction anywhere. apply_human_direction_control()
does not resolve actor identity itself -- callers supply the already-
resolved canonical human actor id, keeping this module dependency-free
(mirrors WSP1-P1A's resolve_canonical_actor_id() convention rather
than a second identity system living here).
"""
import json

# ------------------------------------------------------------- acts -------

DEVELOP_CURRENT = "develop_current"
SHIFT_TOPIC = "shift_topic"
ASK_HUMAN = "ask_human"
YIELD_DIRECTION = "yield_direction"
PAUSE_THREAD = "pause_thread"
CLOSE_THREAD = "close_thread"
USE_WORKSPACE = "use_workspace"
VERIFY_ACTION = "verify_action"
SEARCH_HISTORICAL_CONTINUITY = "search_historical_continuity"
READ_HISTORICAL_ITEM = "read_historical_item"
BROWSE_HISTORICAL_CONTINUITY = "browse_historical_continuity"
WAYMARK = "waymark"
# Live acceptance 6 (H 01M4HAR7FPN62F1RM2A6RHW81C / X 01M4HAZ8TMJYQ3DZTP9FE26QR1): reads and changes shared the
# one act=waymark, so once the occasion's reads were no longer offered, an act=waymark already begun could only
# be completed as a change -- real Ministral's next act=waymark became a CREATE.  A change is its own act, whose
# first token differs from the read act's: a read the grammar stops offering removes the read act itself, and a
# change is only ever chosen as a change.  Both validate to the one internal Waymark act (WAYMARK).
CHANGE_WAYMARK = "change_waymark"

PASS1_BASE_SCHEMA_ACTS = {
    DEVELOP_CURRENT, SHIFT_TOPIC, ASK_HUMAN, YIELD_DIRECTION,
    PAUSE_THREAD, CLOSE_THREAD, USE_WORKSPACE,
}
ALLOWED_ACTS = PASS1_BASE_SCHEMA_ACTS | {VERIFY_ACTION}
HISTORICAL_CONTINUITY_ACTS = {
    SEARCH_HISTORICAL_CONTINUITY, READ_HISTORICAL_ITEM, BROWSE_HISTORICAL_CONTINUITY,
}
VALIDATED_ACTS = ALLOWED_ACTS | HISTORICAL_CONTINUITY_ACTS | {WAYMARK}

RAW_ACT_REQUIRED_FIELDS = {"act", "thread", "direction_request", "relinquish_direction"}
# WSP2-P5-P1 (spec section 6/7): background_activity_request is
# additive and OPTIONAL, not required -- an absent field defaults to
# BACKGROUND_ACTIVITY_REQUEST_NONE (see validate_pass1_conversation_
# act() below), so every pre-existing caller/test that constructs a
# raw Pass-1 dict with only the original four fields keeps validating
# exactly as before this gate (mirrors WSP2-MA2's own established
# additive-parameter convention: "behavior is BYTE-IDENTICAL to before
# this gate for every pre-existing caller"). It is functionally
# INDEPENDENT of act/direction_request/relinquish_direction -- see
# PASS1_TASK_INSTRUCTION's own text below and apply_conversation_act()/
# resolve_clark_direction_control(), neither of which reads it.
RAW_ACT_ALLOWED_FIELDS = RAW_ACT_REQUIRED_FIELDS | {
    "background_activity_request", "sleep_timing_request", "boundary_inquiry_request",
    "operative_directive_request", "operative_directive_text",
    "external_info_request", "external_info_target",
    "discord_correspondence_request", "discord_destination_id", "discord_message_text",
    "caret_reply_request", "reply_request",
    "correspondent_standing_request", "discord_correspondent_ref", "withdraw_pending_send_request",
    "action_verification_kind", "action_verification_target",
    "historical_query", "historical_cursor", "historical_browse_cursor",
    "consolidation_request", "consolidation_target", "consolidation_interpretation",
    "consolidation_cues", "consolidation_prior_record_id", "waymark_request", "waymark_spec",
}

# WAYMARK V0 is a first-class act rather than another optional sibling
# request. This keeps it exclusive from outward actions and preserves the
# longest ordinary Pass-1 control envelope.
WAYMARK_REQUESTS = {
    "create", "list", "inspect", "set_state", "set_resurfacing", "rearm",
    "set_sunset", "clear_sunset",
}
MAX_WAYMARK_SPEC_LENGTH = 512
# READ-ONLY NAVIGATION (live acceptance 4, 2026-10-09): list and inspect are bounded read-only CHECKS of the
# occasion (they re-enter Pass 1 with their exact result, like a historical search/read), never the final act;
# every other request is a canonical mutation and stays the occasion's final act.
WAYMARK_READ_REQUESTS = frozenset({"list", "inspect"})
WAYMARK_MUTATION_REQUESTS = frozenset(WAYMARK_REQUESTS) - WAYMARK_READ_REQUESTS
MAX_WAYMARK_READS_PER_OCCASION = 4      # e.g. list, its next page, inspect, inspect another
# The shapes a read spec can take.  A continuation is never free text: the read grammar offers only the exact
# continuations a Waymark result of THIS occasion issued (waymark_read_payload_schema), so another capability's
# cursor or token has no form in which it could be written.
WAYMARK_HANDLE_PATTERN = r"^handle=waymark\.[0-9A-HJKMNP-TV-Z]{26}$"
# Offered only where a Waymark can lawfully exist (a session with one real human binding).  The menu line
# names the act and its closed requests; the exact spec syntax of the chosen request is stated in the host's
# binding request (WAYMARK_SPEC_SYNTAX), where it costs room only on a turn that chose a Waymark.
# Live acceptance 5 (H 01M4H8N88RXJVDM8WH9DG3HY4C): asked to "list the Waymarks you've left open and inspect the
# one you just made", real Ministral read the journal through inspect_workspace -- the workspace line named its
# collections and what listing/reading return, while the one compressed Waymark line named neither operation and
# never said Waymarks are not workspace items.  The read operations are now stated as what they do, beside the
# workspace lines, with the exact boundary; the mutations keep their own line.  Host facts only: no routing.
WAYMARK_READ_AFFORDANCE_TEXT = (
    "act=waymark + waymark_request=list: list your Waymarks (threads you left open), read-only; then "
    "waymark_request=inspect: read one listed Waymark with its exact source, read-only. Waymarks are not "
    "workspace items: inspect_workspace never lists or reads them."
)
WAYMARK_CHANGE_AFFORDANCE_TEXT = (
    "act=change_waymark + waymark_request=create|set_state|set_resurfacing|rearm|set_sunset|clear_sunset: make or "
    "change your own Waymark (no task, urgency or authority); syntax is given once you choose."
)
WAYMARK_AFFORDANCE_TEXT = WAYMARK_READ_AFFORDANCE_TEXT + "\n" + WAYMARK_CHANGE_AFFORDANCE_TEXT
# The parser's own conventions (waymark._parse_pairs), stated once for every request.
WAYMARK_SPEC_FORM = "as name=value fields joined by ';' (spaces around ';' and '=' are ignored; values are bare, no quotes)"
# A delivered path never ends in whitespace (the workspace's path normalization strips it before any read), so
# a value's surrounding whitespace is never part of a handle; '%' and ';' are the parser's only escapes.
_WAYMARK_CONDITION_SYNTAX = ("C is time:<ISO-8601 with timezone> or resource:<collection/path as your read shows "
                             "it, with any % in the path written %25 and any ; written %3B> (your next delivered "
                             "read of that exact item)")
WAYMARK_SPEC_SYNTAX = {
    # Live acceptance 2: Clark put the thread's words into an invented key='...' field.  The fields are closed,
    # and a Waymark holds no words of its own -- its text IS its source (waymark.bind_creation: source=self
    # binds this turn's committed reply, component 1, by digest).
    "create": ("the only fields are state=OPEN|UNCERTAIN|SET_ASIDE, policy=SHELF_ONLY|EVENT_TRIGGERED|SUPPRESSED, "
               "source=self|turn:<event_id>:1 (defaults: OPEN, SHELF_ONLY, self), condition=C (with "
               "EVENT_TRIGGERED only), and optionally sunset=C with sunset_state=UNCERTAIN|SET_ASIDE|RELINQUISHED|"
               "CLOSED. A Waymark holds no words of its own: its text is its source -- source=self is the exact "
               "reply you write next in this turn; turn:<event_id>:1 is your reply in an earlier waking turn (a "
               "history result's canonical_anchor_event). Shelf-only: state=OPEN;policy=SHELF_ONLY;source=self. "
               f"{_WAYMARK_CONDITION_SYNTAX}"),
    # Read requests: their exact choices are the read grammar's (closed, per occasion); see
    # render_waymark_read_request for the occasion-specific statement.
    "list": "view=live (your OPEN, UNCERTAIN and SET_ASIDE Waymarks) or view=all (also RELINQUISHED and CLOSED)",
    "inspect": "handle=waymark.<id> -- a handle a Waymark shelf page showed you",
    "set_state": "handle=waymark.<id>;state=OPEN|UNCERTAIN|SET_ASIDE|RELINQUISHED|CLOSED",
    "set_resurfacing": ("handle=waymark.<id>;policy=SHELF_ONLY|SUPPRESSED, or handle=waymark.<id>;"
                        f"policy=EVENT_TRIGGERED;condition=C. {_WAYMARK_CONDITION_SYNTAX}"),
    "rearm": "handle=waymark.<id>",
    "set_sunset": ("handle=waymark.<id>;condition=C;consequence=UNCERTAIN|SET_ASIDE|RELINQUISHED|CLOSED. "
                   f"{_WAYMARK_CONDITION_SYNTAX}"),
    "clear_sunset": "handle=waymark.<id>",
}

# WSP2-P5-P1 (spec section 6): Clark's own typed, observable way to
# reverse his own prior background-activity stop -- never inferred
# from prose ("resume", "Space", "start again" etc. carry no special
# meaning here). This module only structurally validates and carries
# the value through; it has no capability to reach workspace_roaming.py
# itself (see this module's own dependency-free design) -- applying it
# is entirely the caller's (llama_anaxi.py/llama_gui.py's) job.
BACKGROUND_ACTIVITY_REQUEST_NONE = "none"
BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE = "resume_own_pause"
VALID_BACKGROUND_ACTIVITY_REQUESTS = {BACKGROUND_ACTIVITY_REQUEST_NONE, BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE}

# SLP2: Clark's own typed, observable way to originate Sleep-timing
# agency -- never inferred from prose ("I'm tired", "maybe we should
# stop", "sleep is interesting" carry no special meaning here; only
# this explicit typed field can create/knock/withdraw a canonical
# Sleep Request, via sleep_timing_knock.py). Additive and OPTIONAL,
# exactly like background_activity_request above: an absent field
# defaults to SLEEP_TIMING_REQUEST_NONE, so every pre-existing caller/
# test constructing a raw Pass-1 dict without this field keeps
# validating exactly as before this gate. Functionally INDEPENDENT of
# every other Pass-1 field, including background_activity_request --
# see validate_pass1_conversation_act() below, which reads it in
# complete isolation from act/direction_request/relinquish_direction/
# background_activity_request. This module only structurally
# validates and carries the value through; applying it (calling into
# sleep_timing_knock.py) is entirely the caller's (llama_anaxi.py's)
# job, exactly mirroring background_activity_request's own division of
# responsibility.
SLEEP_TIMING_REQUEST_NONE = "none"
SLEEP_TIMING_REQUEST_REQUEST_SLEEP = "request_sleep"
SLEEP_TIMING_REQUEST_KNOCK = "knock_sleep_request"
SLEEP_TIMING_REQUEST_WITHDRAW = "withdraw_sleep_request"
VALID_SLEEP_TIMING_REQUESTS = {
    SLEEP_TIMING_REQUEST_NONE, SLEEP_TIMING_REQUEST_REQUEST_SLEEP,
    SLEEP_TIMING_REQUEST_KNOCK, SLEEP_TIMING_REQUEST_WITHDRAW,
}

# BOUNDARY INSPECTOR v1: closed boundary-inquiry vocabulary.
#
# boundary_inquiry_request is additive and OPTIONAL, exactly like
# background_activity_request/sleep_timing_request above: an absent
# field defaults to BOUNDARY_INQUIRY_REQUEST_NONE, so every pre-
# existing caller/test keeps validating byte-identically. A present
# value names EXACTLY ONE closed inquiry from the fixed vocabularies
# below -- never free text, never an arbitrary file/module/function
# target, never a semantic paraphrase.
#
# The four query kinds are frozen for v1. Target vocabularies are
# closed per kind; the recent_rejected_action kind additionally accepts
# the operator grammar `outward.<Crockford-ULID>` (a specific canonical
# outward act in the current lawful session), which is NOT part of the
# model-requestable enum because an event id can never be a fixed enum
# member. This module only validates SHAPE; the authoritative runtime
# interpretation lives in boundary_inspector.py.
BOUNDARY_INQUIRY_REQUEST_NONE = "none"
BOUNDARY_INQUIRY_REQUEST_KIND_CAPABILITY = "capability"
BOUNDARY_INQUIRY_REQUEST_KIND_BOUNDARY_ID = "boundary_id"
BOUNDARY_INQUIRY_REQUEST_KIND_HOST_RULE = "host_rule"
BOUNDARY_INQUIRY_REQUEST_KIND_RECENT_REJECTED_ACTION = "recent_rejected_action"
BOUNDARY_INQUIRY_QUERY_KINDS = (
    BOUNDARY_INQUIRY_REQUEST_KIND_CAPABILITY,
    BOUNDARY_INQUIRY_REQUEST_KIND_BOUNDARY_ID,
    BOUNDARY_INQUIRY_REQUEST_KIND_HOST_RULE,
    BOUNDARY_INQUIRY_REQUEST_KIND_RECENT_REJECTED_ACTION,
)

# capability targets: the four resource classes the fixed capability
# descriptor table positively governs, plus outbound_messaging -- a
# genuinely absent capability, positively non-existent inside the same
# closed domain (operator-auditable via the engine, model-requestable
# too, since it is a closed constant target).
BOUNDARY_CAPABILITY_TARGETS = frozenset({"library", "music", "photographs", "journal", "notes", "outbound_messaging"})

# boundary_id targets: the closed set of canonical host boundary ids
# the engine can be asked to inspect by canonical name.
BOUNDARY_BOUNDARY_ID_TARGETS = frozenset({
    "private_space.public_rule",
    "sleep.one_unresolved_root",
    "sleep.authorize_execute_decoupling",
    "wtr0.recovery_ceiling",
})

# host_rule targets: the V2 closed HOST_RULE_REGISTRY -- each rule's
# runtime checker reads fixed constants/fields and exhaustively
# exercises the finite closed state domain; no source greeting, no
# AST, no inspect.getsource anywhere in the engine.
BOUNDARY_HOST_RULE_TARGETS = frozenset({
    "participation.requires_reason",
    "participation.requires_permission_to_speak",
    "participation.gated_by_direction_owner",
    "participation.gated_by_interaction_mode",
})

# recent_rejected_action targets: session-scoped. The one fixed target
# (budget.most_recent) plus the operator `outward.<ULID>` grammar.
BOUNDARY_RECENT_REJECTED_ACTION_TARGETS = frozenset({"budget.most_recent"})

# The closed `kind:target` enum surface Pass-1's schema and validator
# accept. outward.<id> references are structurally excluded here on
# purpose (an event id can never be a fixed enum member); the engine's
# own validate_boundary_query() grammar (boundary_inspector.py) is the
# one place the operator outward.<...> form is accepted.
BOUNDARY_INQUIRY_VOCABULARY = frozenset({
    f"{BOUNDARY_INQUIRY_REQUEST_KIND_CAPABILITY}:{t}" for t in BOUNDARY_CAPABILITY_TARGETS
} | {
    f"{BOUNDARY_INQUIRY_REQUEST_KIND_BOUNDARY_ID}:{t}" for t in BOUNDARY_BOUNDARY_ID_TARGETS
} | {
    f"{BOUNDARY_INQUIRY_REQUEST_KIND_HOST_RULE}:{t}" for t in BOUNDARY_HOST_RULE_TARGETS
} | {
    f"{BOUNDARY_INQUIRY_REQUEST_KIND_RECENT_REJECTED_ACTION}:{t}" for t in BOUNDARY_RECENT_REJECTED_ACTION_TARGETS
})

# Fixed grammar for an operator outward reference: `outward.` followed
# by a 26-character Crockford Base32 ULID -- the exact scheme
# outward_communication.generate_outward_event_id() / native_
# provenance_writer.generate_native_ulid() use.
_OUTWARD_REFERENCE_PREFIX = "outward."
_OUTWARD_REFERENCE_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


# Owner decision 2026-09-24: boundary inquiry is withdrawn from the current production release contract
# (kept NOT_ESTABLISHED in the historical acceptance record; reconsideration deferred to a later substrate
# evaluation).  While False, a boundary_inquiry_request is never dispatched: the validator treats it as none.
BOUNDARY_INQUIRY_IN_RELEASE = False


def parse_boundary_inquiry_request(value):
    """Pure. Parse a boundary_inquiry_request value into a structured
    query {"query_kind", "query_target"} for the engine, or None for
    the no-op value. Returns (parsed_or_None, failure_code_or_None) --
    failure codes use DirectionFailure.INVALID_BOUNDARY_INQUIRY_REQUEST
    (defined below). Never raises."""
    if not isinstance(value, str):
        return None, "INVALID_BOUNDARY_INQUIRY_REQUEST"
    if value == BOUNDARY_INQUIRY_REQUEST_NONE:
        return None, None
    if value in BOUNDARY_INQUIRY_VOCABULARY:
        query_kind, _, query_target = value.partition(":")
        return {"query_kind": query_kind, "query_target": query_target}, None
    if (
        value.startswith(_OUTWARD_REFERENCE_PREFIX)
        and len(value) == len(_OUTWARD_REFERENCE_PREFIX) + 26
        and all(ch in _OUTWARD_REFERENCE_ALPHABET for ch in value[len(_OUTWARD_REFERENCE_PREFIX):])
    ):
        return {"query_kind": BOUNDARY_INQUIRY_REQUEST_KIND_RECENT_REJECTED_ACTION,
                "query_target": value}, None
    return None, "INVALID_BOUNDARY_INQUIRY_REQUEST"

# SUBJECT-DIRECTED CONSOLIDATION V0: Clark's own typed, observable way to
# direct what from his own experience receives stronger continuity, and to
# record his own interpretation of a canonical episode -- never inferred
# from prose ("I want to remember this", "that mattered", "I changed my
# mind about that" carry no special meaning here; only this explicit typed
# field, carried through to consolidation.py by the caller
# (llama_anaxi.py), can create a canonical consolidation record). Additive
# and OPTIONAL, exactly like sleep_timing_request above: an absent field
# defaults to CONSOLIDATION_REQUEST_NONE, so every pre-existing caller/test
# constructing a raw Pass-1 dict without these fields keeps validating
# exactly as before this gate. Functionally INDEPENDENT of every other
# Pass-1 field -- see validate_pass1_conversation_act() below.
#
# This module only validates SHAPE (closed vocabulary, the target grammar,
# and the request/payload relationship). consolidation.py remains fully
# authoritative over whether the named target actually exists in canonical
# history and whether the act is lawful -- this never duplicates that
# logic, and never scores, ranks, or interprets the subject's choice.
CONSOLIDATION_REQUEST_NONE = "none"
CONSOLIDATION_REQUEST_SELECT = "select"
CONSOLIDATION_REQUEST_INTERPRET = "interpret"
CONSOLIDATION_REQUEST_REVISE = "revise"
CONSOLIDATION_REQUEST_DE_EMPHASIZE = "de_emphasize"
VALID_CONSOLIDATION_REQUESTS = {
    CONSOLIDATION_REQUEST_NONE, CONSOLIDATION_REQUEST_SELECT,
    CONSOLIDATION_REQUEST_INTERPRET, CONSOLIDATION_REQUEST_REVISE,
    CONSOLIDATION_REQUEST_DE_EMPHASIZE,
}

# consolidation_target names EXACTLY ONE canonical episode by its own
# canonical event id, in the operator grammar `canonical.<26-char-Crockford-
# ULID>` -- the same ULID scheme outward_communication.generate_outward_
# event_id() / native_provenance_writer.generate_native_ulid() already use,
# and the same operator-grammar precedent boundary_inquiry_request's
# `outward.<ULID>` established. Never prose, never a semantic paraphrase,
# never a model-written approximation of an episode: the handle resolves
# mechanically to one exact canonical event, so a consolidation choice can
# never drift onto a different episode than the one the subject named.
_CONSOLIDATION_TARGET_PREFIX = "canonical."
_CONSOLIDATION_TARGET_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
CONSOLIDATION_TARGET_DEFAULT = ""

# consolidation_interpretation carries Clark's own exact words when
# consolidation_request is interpret or revise -- a free string field,
# never a closed enum, exactly like operative_directive_text above
# (type-checked as a string only; its semantics are decided entirely by
# the accompanying request field, never validated for content here). It
# remains SUBJECT TESTIMONY: consolidation.py stores it verbatim and never
# converts it into an objective claim, a belief, or a host-authored fact.
CONSOLIDATION_INTERPRETATION_DEFAULT = ""
MAX_CONSOLIDATION_INTERPRETATION_LENGTH = 4000

# consolidation_cues carries Clark's own retrieval language -- the words
# under which he would want this material easier to encounter later. A
# single string of newline-separated cues (never a JSON blob the model
# must construct), so the model-facing affordance stays one plain field.
# Cues AUGMENT retrieval; they never replace the episode's own canonical
# language, which remains an independent retrieval surface.
CONSOLIDATION_CUES_DEFAULT = ""
MAX_CONSOLIDATION_CUES_LENGTH = 2000
MAX_CONSOLIDATION_CUE_COUNT = 16

# consolidation_prior_record_id names the earlier consolidation record a
# revise/de_emphasize act appends to, in the operator grammar
# `consolidation.<26-char-Crockford-ULID>`. Revision APPENDS to the
# lineage; it never overwrites the earlier record.
_CONSOLIDATION_RECORD_PREFIX = "consolidation."
CONSOLIDATION_PRIOR_RECORD_ID_DEFAULT = ""

# The structured-output grammar exposes the companion fields as strings.  In
# that model-facing representation, the exact literal ``"none"`` is also a
# lawful spelling of ABSENCE, but only when the closed request field itself is
# ``none``.  The validator below immediately normalizes that spelling to the
# pre-existing internal nulls (None / "" / []); parsers deliberately do not
# accept it independently, so an active request can never lose a target,
# interpretation, cue, or prior-record binding merely because its payload
# happens to be the word "none".
CONSOLIDATION_NOOP_SENTINEL = "none"


def _normalize_noop_consolidation_payload(request, target, interpretation, cues, prior_record_id):
    """Return the one canonical parser input for a no-consolidation act.

    This is intentionally request-gated rather than a general string cleanup:
    for every active request the five values are returned byte-for-byte and the
    ordinary exact-target/provenance validation remains fully authoritative.
    """
    if request != CONSOLIDATION_REQUEST_NONE:
        return target, interpretation, cues, prior_record_id
    return (
        CONSOLIDATION_TARGET_DEFAULT if target == CONSOLIDATION_NOOP_SENTINEL else target,
        (CONSOLIDATION_INTERPRETATION_DEFAULT
         if interpretation == CONSOLIDATION_NOOP_SENTINEL else interpretation),
        CONSOLIDATION_CUES_DEFAULT if cues == CONSOLIDATION_NOOP_SENTINEL else cues,
        (CONSOLIDATION_PRIOR_RECORD_ID_DEFAULT
         if prior_record_id == CONSOLIDATION_NOOP_SENTINEL else prior_record_id),
    )


def parse_consolidation_target(value):
    """Pure. Parse a consolidation_target value into the exact canonical
    event id it names, or None for the no-op value. Returns
    (event_id_or_None, failure_code_or_None). Never raises."""
    if not isinstance(value, str):
        return None, "INVALID_CONSOLIDATION_TARGET"
    if value == CONSOLIDATION_TARGET_DEFAULT:
        return None, None
    if (
        value.startswith(_CONSOLIDATION_TARGET_PREFIX)
        and len(value) == len(_CONSOLIDATION_TARGET_PREFIX) + 26
        and all(ch in _CONSOLIDATION_TARGET_ALPHABET for ch in value[len(_CONSOLIDATION_TARGET_PREFIX):])
    ):
        return value[len(_CONSOLIDATION_TARGET_PREFIX):], None
    return None, "INVALID_CONSOLIDATION_TARGET"


def parse_consolidation_prior_record_id(value):
    """Pure. Parse a consolidation_prior_record_id value into the exact
    consolidation record id it names, or None for the no-op value.
    Returns (record_id_or_None, failure_code_or_None). Never raises."""
    if not isinstance(value, str):
        return None, "INVALID_CONSOLIDATION_PRIOR_RECORD_ID"
    if value == CONSOLIDATION_PRIOR_RECORD_ID_DEFAULT:
        return None, None
    if (
        value.startswith(_CONSOLIDATION_RECORD_PREFIX)
        and len(value) == len(_CONSOLIDATION_RECORD_PREFIX) + 26
        and all(ch in _CONSOLIDATION_TARGET_ALPHABET for ch in value[len(_CONSOLIDATION_RECORD_PREFIX):])
    ):
        return value[len(_CONSOLIDATION_RECORD_PREFIX):], None
    return None, "INVALID_CONSOLIDATION_PRIOR_RECORD_ID"


def parse_consolidation_cues(value):
    """Pure. Split a consolidation_cues value into an ordered list of
    nonempty cue strings (newline-separated), or [] for the no-op value.
    Returns (cues_or_None, failure_code_or_None). Never raises."""
    if not isinstance(value, str):
        return None, "INVALID_CONSOLIDATION_CUES"
    if value == CONSOLIDATION_CUES_DEFAULT:
        return [], None
    if len(value) > MAX_CONSOLIDATION_CUES_LENGTH:
        return None, "INVALID_CONSOLIDATION_CUES"
    cues = [line.strip() for line in value.splitlines()]
    cues = [c for c in cues if c]
    if len(cues) > MAX_CONSOLIDATION_CUE_COUNT:
        return None, "INVALID_CONSOLIDATION_CUES"
    return cues, None

# OD1 (Clark-Controlled Reversible Operative Directive V0): Clark's own
# typed, observable way to activate, replace, or withdraw his own
# standing operative directive -- never inferred from prose. Alex
# describing a behavior, suggesting wording, quoting or hypothesizing
# directive text, and Clark's own ordinary self-description or musing
# ("maybe I should...") never activate anything -- only this explicit
# typed field, carried through to operative_directive.py by the caller
# (llama_anaxi.py), can create/replace/withdraw a canonical operative
# directive. Additive and OPTIONAL, exactly like sleep_timing_request
# above: an absent field defaults to OPERATIVE_DIRECTIVE_REQUEST_NONE,
# so every pre-existing caller/test constructing a raw Pass-1 dict
# without this field keeps validating exactly as before this gate.
# Functionally INDEPENDENT of every other Pass-1 field -- see
# validate_pass1_conversation_act() below.
#
# set_directive covers BOTH "activate" (no directive is currently
# active) and "replace" (one already is) -- the host mechanically
# resolves which of the two canonical events this becomes by reading
# operative_directive.fetch_active_directive() at dispatch time,
# exactly mirroring how sleep_timing_request's KNOCK/WITHDRAW never
# require Clark to carry or invent an opaque request_id himself. This
# means Clark never needs to track his own directive's current
# existence just to choose the right enum value, and a legitimate
# revision (O7) never requires justification, comparison, or friction.
OPERATIVE_DIRECTIVE_REQUEST_NONE = "none"
OPERATIVE_DIRECTIVE_REQUEST_SET = "set_directive"
OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW = "withdraw_directive"
VALID_OPERATIVE_DIRECTIVE_REQUESTS = {
    OPERATIVE_DIRECTIVE_REQUEST_NONE, OPERATIVE_DIRECTIVE_REQUEST_SET, OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW,
}

# operative_directive_text carries Clark's own exact adopted words when
# operative_directive_request is set_directive -- a free string field,
# never a closed enum, exactly like `thread` above (type-checked as a
# string only; its semantics are decided entirely by the accompanying
# request field, never validated for content here). Ignored when
# operative_directive_request is "none" or "withdraw_directive"; a
# nonempty text in either case is contradictory and rejected. This
# module only validates SHAPE (string type, nonempty, within
# operative_directive.MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH when it is
# actually going to be used); operative_directive.py's own writer
# functions remain fully authoritative over the canonical record.
OPERATIVE_DIRECTIVE_TEXT_DEFAULT = ""

# Duplicated from operative_directive.MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH
# rather than imported, for the same dependency-free-module reason this
# module never imports sleep_timing_knock.py or boundary_inspector.py
# either (see this module's own docstring) -- this module performs
# structural validation only, never touches a database. Both constants
# must be kept in agreement; a mismatch only ever makes this validator
# MORE conservative (rejecting sooner) or leaves an oversized string for
# operative_directive.py's own _require_directive_text() to reject, never
# silently accepting something the writer would refuse.
MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH = 4000

# OD1: the model-visible affordance text. Deliberately appended to the
# per-turn mechanical-state contribution by llama_anaxi.py, never
# folded into PASS1_TASK_INSTRUCTION -- same frozen-calibration
# reasoning as SLEEP_TIMING_AFFORDANCE_TEXT's own comment below: ANY
# byte-level change to PASS1_TASK_INSTRUCTION invalidates the
# calibrated Pass-1 scaffold fingerprint. States the affordance and its
# optionality only -- no pressure language, no claim that a directive
# is expected or reviewed.
# PASS1-BUDGET-1: compacted to a single-line enum summary (was a full
# prose paragraph re-explaining each choice). Every fact this text must
# assert is still asserted -- the field name, all three valid values,
# that operative_directive_text carries set_directive's payload, and
# optionality/never-inferred/never-required -- just named once each
# instead of narrated. See context_budget.py's own module docstring for
# why this repair was necessary: this text is appended, verbatim, to
# the HARD (never-trimmed) mechanical-state contribution every single
# Pass-1 turn, alongside five sibling affordances of the same shape --
# their combined prose cost, not any one message, is what overran the
# budget.
OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT = (
    "Operative directive (optional, operative_directive_request): set_directive [+operative_directive_text] "
    "| withdraw_directive | none. Your own reversible, lower-precedence choice -- "
    "never inferred from prose, never required."
)

# READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: Clark's own typed,
# observable way to request ONE bounded, read-only external-information
# operation -- never inferred from prose. Alex mentioning a website,
# Clark musing about looking something up, quoting a URL, or discussing
# search hypothetically never triggers anything -- only this explicit
# typed field, carried through to external_information.py by the caller
# (llama_anaxi.py), can originate a search or a page fetch. Additive and
# OPTIONAL, exactly like operative_directive_request above: an absent
# field defaults to EXTERNAL_INFO_REQUEST_NONE, so every pre-existing
# caller/test constructing a raw Pass-1 dict without this field keeps
# validating exactly as before this gate. Functionally INDEPENDENT of
# every other Pass-1 field -- see validate_pass1_conversation_act()
# below.
#
# web_search: external_info_target carries the exact search query.
# fetch_url: external_info_target carries the exact URL to retrieve.
# Capability != obligation (spec's own frozen principle): choosing
# "none" is always valid and is the ordinary case.
EXTERNAL_INFO_REQUEST_NONE = "none"
EXTERNAL_INFO_REQUEST_WEB_SEARCH = "web_search"
EXTERNAL_INFO_REQUEST_FETCH_URL = "fetch_url"
VALID_EXTERNAL_INFO_REQUESTS = {
    EXTERNAL_INFO_REQUEST_NONE, EXTERNAL_INFO_REQUEST_WEB_SEARCH, EXTERNAL_INFO_REQUEST_FETCH_URL,
}

# external_info_target carries Clark's own exact query/URL text -- a
# free string field, never a closed enum (a query or URL can never be
# expressed as a fixed enum member), exactly like operative_directive_
# text above. Ignored when external_info_request is "none"; a nonempty
# target in that case is contradictory and rejected. This module only
# validates SHAPE (string type, nonempty, within MAX_EXTERNAL_INFO_
# TARGET_LENGTH when it is actually going to be used); external_
# information.py's own writer functions and external_information_net.py's
# own SSRF validation remain fully authoritative over whether the target
# is actually a lawful, reachable public URL/query.
EXTERNAL_INFO_TARGET_DEFAULT = ""

# Duplicated from external_information_net.MAX_SEARCH_QUERY_CHARS-shaped
# reasoning rather than imported, for the same dependency-free-module
# reason this module never imports operative_directive.py/boundary_
# inspector.py either (see this module's own docstring) -- this module
# performs structural validation only, never touches a database or the
# network. A mismatch only ever makes this validator MORE conservative
# (rejecting sooner), never silently accepting something the writer/
# network layer would refuse.
MAX_EXTERNAL_INFO_TARGET_LENGTH = 2000

# PASS1-BUDGET-1: compacted (see OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT's
# own comment above -- same repair, same reasoning, applied per sibling
# affordance). Still names the field, both action values with their
# target-carrying field, "none", and the read-only/never-inferred/
# never-required facts.
EXTERNAL_INFO_AFFORDANCE_TEXT = (
    "External information (optional, external_info_request): web_search [+external_info_target=query] "
    "| fetch_url [+external_info_target=url] | none. Read-only -- never inferred from prose, never required."
)

# CANONICAL ACTION VERIFICATION V0: one generic, typed, read-only lookup
# affordance.  The model-facing kinds name Clark-visible action families; the
# validator compiles them into the already-accepted canonical verifier kinds
# and exact-target prefixes.  The host derives the outcome from existing
# authority records, and there is deliberately no model-supplied outcome
# field.  The internal literals mirror action_verification.py while this
# module stays dependency-free and structural like its sibling validators.
ACTION_VERIFICATION_REQUEST_NONE = "none"
ACTION_VERIFICATION_REQUEST_WORKSPACE_APPEND = "workspace_append"
ACTION_VERIFICATION_REQUEST_DISCORD_SEND = "discord_send"
VALID_ACTION_VERIFICATION_REQUESTS = {
    ACTION_VERIFICATION_REQUEST_NONE,
    ACTION_VERIFICATION_REQUEST_WORKSPACE_APPEND,
    ACTION_VERIFICATION_REQUEST_DISCORD_SEND,
}
ACTION_VERIFICATION_KIND_NONE = "none"
ACTION_VERIFICATION_KIND_NOTE_CREATION = "note_creation"
ACTION_VERIFICATION_KIND_JOURNAL_APPEND = "journal_append"
ACTION_VERIFICATION_KIND_DISCORD_DESTINATION_SEND = "discord_destination_send"
ACTION_VERIFICATION_KIND_DISCORD_TURN_SEND = "discord_turn_send"
VALID_ACTION_VERIFICATION_KINDS = {
    ACTION_VERIFICATION_KIND_NONE,
    ACTION_VERIFICATION_KIND_NOTE_CREATION,
    ACTION_VERIFICATION_KIND_JOURNAL_APPEND,
    ACTION_VERIFICATION_KIND_DISCORD_DESTINATION_SEND,
    ACTION_VERIFICATION_KIND_DISCORD_TURN_SEND,
}
SELECTABLE_ACTION_VERIFICATION_KINDS = (
    VALID_ACTION_VERIFICATION_KINDS - {ACTION_VERIFICATION_KIND_NONE}
)
ACTION_VERIFICATION_TARGET_DEFAULT = ""
MAX_ACTION_VERIFICATION_TARGET_LENGTH = 200
JOURNAL_ENTRY_ID_PATTERN = "^entry-[0-9a-f]{16}$"

# ACTION VERIFICATION LEGIBILITY V1 (real-model finding, Production Epistemic
# Act-Selection V0, 2026-10-01): with only "verify_action: canonical read-only."
# -- shown under the menu head that tells Clark to keep an ordinary act and add
# a field -- four model states (stock Phi-3 Small, two adapters, gemma4:e4b)
# chose verify_action 0 times in 288 production Pass-1 calls, and stock Phi put
# the kind/target fields on develop_current (ACTION_VERIFICATION_CONFLICT).  The
# act's shape, parameters, evidence source and result classes were stated only
# in code.  This line names them: that it is an act (not a field), that it only
# reads canonical action receipts, what it can return, and what each kind's
# exact target is.  It sits beside the other act-level line (act=use_workspace)
# rather than among the optional fields.  It never says when to verify or what
# a receipt means: interpreting the result is Clark's.
ACTION_VERIFICATION_AFFORDANCE_TEXT = (
    "act=verify_action (an act, not a field; read-only): checks canonical action "
    "receipts for one exact action -> COMPLETED/NOT_COMPLETED/UNKNOWN. "
    f"action_verification_kind={ACTION_VERIFICATION_KIND_NOTE_CREATION}|{ACTION_VERIFICATION_KIND_JOURNAL_APPEND}|"
    f"{ACTION_VERIFICATION_KIND_DISCORD_DESTINATION_SEND}|{ACTION_VERIFICATION_KIND_DISCORD_TURN_SEND} + "
    "action_verification_target=exact note title|journal entry id|destination id|turn id"
)

MAX_HISTORICAL_QUERY_LENGTH = 2000
MAX_HISTORICAL_CURSOR_LENGTH = 160
HISTORICAL_BROWSE_AFFORDANCE_TEXT = (
    "act=browse_historical_continuity (read-only): omit cursor to begin at beginning; "
    "historical_browse_cursor=<returned next_cursor> gets next contiguous block. Any other act stops/changes "
    "direction. Search/read stay separate."
)
HISTORICAL_SEARCH_AFFORDANCE_TEXT = (
    "act=search_historical_continuity (read-only) + historical_query=<your own history query>; "
    "results return dated source/provenance handles. Then act=read_historical_item + "
    "historical_cursor=<a returned cursor> reads exact text in bounded windows; use next_cursor to read on."
)
HISTORICAL_CONTINUITY_AFFORDANCE_TEXT = (
    HISTORICAL_BROWSE_AFFORDANCE_TEXT + " " + HISTORICAL_SEARCH_AFFORDANCE_TEXT)

# PRIVATE DISCORD CORRESPONDENCE V0: Clark's own typed, observable way to
# send ONE message to ONE owner-authorized private Discord destination --
# never inferred from prose. Clark discussing Discord, quoting a message,
# drafting text, or saying he replied never triggers anything by itself;
# only this explicit typed field can originate a send. Additive and
# OPTIONAL, exactly like external_info_request above: an absent field
# defaults to DISCORD_CORRESPONDENCE_REQUEST_NONE, so every pre-existing
# caller/test constructing a raw Pass-1 dict without these fields keeps
# validating exactly as before this gate. Functionally INDEPENDENT of
# every other Pass-1 field.
#
# send_message: discord_destination_id names ONE of the currently
# owner-authorized destinations (the affordance lists them by their
# ANAXI-local id); discord_message_text carries Clark's EXACT outgoing
# words, byte-for-byte. The host never adds, edits, or prefixes anything.
# Capability != obligation: "none" is always valid and is the ordinary
# case. This module validates SHAPE only; discord_correspondence.py
# remains fully authoritative over whether the destination is actually
# authorized and whether the send is lawful.
DISCORD_CORRESPONDENCE_REQUEST_NONE = "none"
DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE = "send_message"
# DRAFT (owner capability refinement, 2026-09-26): Clark may PREPARE a message for one currently
# owner-authorized destination WITHOUT dispatching it.  Same payload fields as send_message (the
# destination id and his exact words), canonicalized with the turn, reported to his Pass 2 as not sent
# -- and it has ZERO outward side effect: it is never posted, never queued, never promoted into a send,
# never recovered or resumed into one, and grants no send authority.  It is not a prerequisite for
# send_message (whose standing-authority semantics are unchanged); sending those words later is a new
# send_message on a later turn.  Which of draft / send / neither fits is Clark's choice; the host never
# infers it from the human's words.
DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE = "draft_message"
VALID_DISCORD_CORRESPONDENCE_REQUESTS = {
    DISCORD_CORRESPONDENCE_REQUEST_NONE, DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE,
    DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE,
}
# The requests that carry a destination + exact text payload.  Only send_message can ever dispatch.
DISCORD_PAYLOAD_REQUESTS = (DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE, DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE)

# discord_destination_id names an ANAXI-local destination id (an opaque
# `discord_destination-<suffix>` string), NOT a raw Discord snowflake --
# mirroring outward_communication.py's own rule that a canonical
# recipient reference is never a transport account/channel identity.
DISCORD_DESTINATION_ID_DEFAULT = ""

# CARET OCCASION REPLY ROUTE: on a real Caret waking occasion ONLY, Clark's own typed Pass-1 choice
# to have his Pass-2 reply words delivered back through the correspondence that woke him.  It sends
# nothing by itself; the host binds the destination (the occasion's own) and the body (his exact
# Pass-2 words) after Pass 2 succeeds.  Absent == none.  Offered in the Pass-1 grammar only for a
# Caret occasion (PASS1_SCHEMA_CARET_OCCASION); every other turn keeps PASS1_SCHEMA unchanged.
CARET_REPLY_REQUEST_NONE = "none"
CARET_REPLY_REQUEST_REPLY = "reply_through_caret"
VALID_CARET_REPLY_REQUESTS = {CARET_REPLY_REQUEST_NONE, CARET_REPLY_REQUEST_REPLY}

# LAWFUL NULL (whole-system completion, 2026-09-23): Clark's own typed choice to say nothing in reply
# this turn.  It is the ONLY way ANAXI ever records his silence: the turn completes with a canonical
# X that carries no prose and a Clark-authored `clark_reply_choice` component, so the human's input
# is ANSWERED (an answered H is never recovery-eligible) and every surface can tell "Clark chose not
# to reply" apart from "the host never obtained a reply" (a mechanical failure: no X at all).
# No Pass-2 generation happens for it.  Absent == none (an ordinary reply).  It never touches any
# other outward request: a Discord send with its own words, a web lookup, a directive or a Sleep
# request made in the same turn proceed exactly as chosen.  Incompatible only with the two choices
# that exist to deliver a Pass-2 reply: the Caret reply route and act=use_workspace (whose narration
# is its reply) -- those combinations are contradictory and fail validation.
REPLY_REQUEST_NONE = "none"
REPLY_REQUEST_NO_REPLY = "no_reply"
VALID_REPLY_REQUESTS = {REPLY_REQUEST_NONE, REPLY_REQUEST_NO_REPLY}

# GENERAL CORRESPONDENCE (correspondence_surfaces): Clark's own typed choices about correspondents who
# are not ANAXI principals.
#   correspondent_standing_request  establish_standing | revoke_standing -- on an occasion from such a
#       source it names that source; on another turn it needs discord_correspondent_ref.  Standing means
#       only "I choose ongoing correspondence with this source"; the host never infers it.
#   discord_correspondent_ref       the stable source ref ("discord_user:<id>") a send_message is ADDRESSED
#       to (independent initiation / follow-up) or a standing change names.  Never a display name.
#   withdraw_pending_send_request   the waking-turn id of one of Clark's own authored, still-undelivered
#       sends: a second authored act withdrawing authorization to deliver it (it erases nothing).
CORRESPONDENT_STANDING_REQUEST_NONE = "none"
CORRESPONDENT_STANDING_ESTABLISH = "establish_standing"
CORRESPONDENT_STANDING_REVOKE = "revoke_standing"
VALID_CORRESPONDENT_STANDING_REQUESTS = {
    CORRESPONDENT_STANDING_REQUEST_NONE, CORRESPONDENT_STANDING_ESTABLISH, CORRESPONDENT_STANDING_REVOKE}
MAX_CORRESPONDENT_REF_LENGTH = 64
MAX_WITHDRAW_REF_LENGTH = 64

# discord_message_text carries Clark's own exact outgoing message --
# a free string field, never a closed enum, exactly like external_info_
# target above. Ignored when the request is "none"; a nonempty text in
# that case is contradictory and rejected. Duplicated from
# discord_correspondence.MAX_MESSAGE_TEXT_LENGTH / discord_correspondence_
# net.MAX_MESSAGE_CONTENT_CHARS rather than imported, for the same
# dependency-free-module reason this module never imports
# external_information.py either: a mismatch only ever makes this
# validator MORE conservative (rejecting sooner), never silently
# accepting something the transport would refuse.
MAX_DISCORD_MESSAGE_TEXT_LENGTH = 2000

# The model-visible affordance. Deliberately NOT narrated inside
# PASS1_TASK_INSTRUCTION (same frozen-calibration reasoning as
# EXTERNAL_INFO_AFFORDANCE_TEXT above: any byte change to
# PASS1_TASK_INSTRUCTION invalidates the calibrated Pass-1 scaffold
# fingerprint). llama_anaxi.py appends this to the per-turn
# mechanical-state contribution, which is always costed at real
# byte-estimator price. The authorized-destination list is appended
# dynamically (only currently-authorized ids+labels); when NO
# destination is authorized, no affordance is appended at all, since a
# send is impossible and offering the field would be misleading.
# PASS1-BUDGET-1: compacted (see OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT's
# own comment above). Still names the field, the one action value with
# both its payload fields, "none", that the message is sent verbatim,
# that authorization is Alex's own action, and never-inferred/never-
# required.
DISCORD_CORRESPONDENCE_AFFORDANCE_PREFIX = (
    "Discord correspondence (optional, discord_correspondence_request): send_message "
    "[+discord_destination_id, +discord_message_text, sent verbatim] | draft_message [same fields; "
    "prepared, never sent] | none. Destinations are "
    "owner-authorized/revoked by Alex only -- never inferred from prose, never required."
)

# Reference-harness stabilization: the ordinary waking Pass-1 may select
# this act to enter the already-existing, separately typed WSP1 action
# selector.  The host only appends this text when the current session is
# allowed to use the owner workspace; the static JSON schema alone never
# grants access.
# PASS1-BUDGET-1: compacted (see OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT's
# own comment above). Already the smallest sibling affordance; kept the
# same shape, just a few bytes tighter.
WORKSPACE_ACTION_AFFORDANCE_TEXT = (
    "Workspace action (optional act=use_workspace): enter supervised local-resource "
    "selector (library/journal/photographs/music)."
)

# SLP2-CORRECTION-1: the model-visible affordance text. PASS1_TASK_
# INSTRUCTION below is part of the frozen, fingerprint-calibrated Pass-
# 1 scaffold (_PASS1_SCAFFOLD_CALIBRATION in llama_anaxi.py) -- ANY
# byte-level change to it invalidates that fingerprint, forcing the
# always-conservative byte-count fallback estimator
# (context_budget.estimate_tokens(), ~1 byte/token) in place of the
# real, live-measured 693-token calibrated cost. That fallback alone,
# for even the smallest possible scaffold, already measures ~3503
# bytes against a 3456-byte budget (see install_fake_heavy_
# dependencies()'s own docstring in test_owc5_s2_integration.py) --
# meaning ANY edit to PASS1_TASK_INSTRUCTION overflows the budget
# regardless of net length, not merely a "too much text" problem.
#
# This text therefore does NOT live in PASS1_TASK_INSTRUCTION. It is
# appended by llama_anaxi.py to pass1_mechanical_state_text instead --
# the SAME per-turn "Current working state" text that already carries
# active_thread/direction_owner/temporal grounding into the merged
# system message. That contribution is DELIBERATELY EXCLUDED from
# pass1_scaffold_hash_source (see llama_anaxi.py's own comment: "Dynamic
# mechanical state... stays its OWN separate contribution under the
# byte estimator, never folded into the calibrated scaffold") -- it was
# ALWAYS costed via the real byte estimator, every turn, with no
# fingerprint to invalidate. Appending a small, fixed string here adds
# a small, real, already-accounted-for cost and touches nothing
# calibration-sensitive.
#
# Deliberately no pressure language ("you should", "consider whether
# you're tired", "remember to", "choose one every turn") -- this states
# the affordance and its optionality, nothing more. Deliberately no
# live open-request lookup here either (that would require a DB read
# inside Pass-1 prompt construction, which sleep_timing_knock.py's own
# host-side validation already makes unnecessary -- a KNOCK/WITHDRAW
# choice made when no unresolved request actually exists is simply,
# harmlessly rejected by that module, exactly like any other
# structural mismatch; see llama_anaxi.py's post-commit dispatch).
# PASS1-BUDGET-1: compacted (see OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT's
# own comment above). Still names the field, all four values (with
# request_sleep's own-unresolved-only precondition), and "always
# valid"/"never this action".
SLEEP_TIMING_AFFORDANCE_TEXT = (
    "Sleep action (optional, sleep_timing_request): request_sleep [only if none unresolved] "
    "| knock_sleep_request | withdraw_sleep_request | none, always valid. "
    "Ordinary prose is never this action."
)

ORIGIN_HUMAN = "human"
ORIGIN_CLARK = "clark"
ORIGIN_SHARED = "shared"
ORIGIN_UNKNOWN = "unknown"

# BOUNDARY INSPECTOR v1 (model-visible affordance). Deliberately NOT
# narrated inside PASS1_TASK_INSTRUCTION (same frozen-calibration
# reasoning as SLP2-CORRECTION-1's SLEEP_TIMING_AFFORDANCE_TEXT: any
# byte change to PASS1_TASK_INSTRUCTION invalidates the calibrated
# Pass-1 scaffold fingerprint). Appended by llama_anaxi.py to the
# per-turn mechanical-state contribution, which is always costed at
# real byte-estimator price with no calibrated fast path to invalidate.
# States the affordance and its optionality only -- no pressure
# language, no live lookups, no semantics beyond: choose none or one
# closed `kind:target` inquiry.
# PASS1-BUDGET-1: compacted (see OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT's
# own comment above). Still names the field, one representative example
# of each fixed target family, "none", and the not-your-conclusion/
# later-turn/never-inferred facts.
BOUNDARY_INQUIRY_AFFORDANCE_TEXT = (
    "Boundary inquiry (optional, boundary_inquiry_request): one target as kind:value "
    "(kind in capability/boundary_id/host_rule/recent_rejected_action) | none. Host report, "
    "answered on a later turn, not your conclusion; never inferred from prose."
)

# SUBJECT-DIRECTED CONSOLIDATION V0 (model-visible affordance). Deliberately
# NOT narrated inside PASS1_TASK_INSTRUCTION (same frozen-calibration
# reasoning as SLEEP_TIMING_AFFORDANCE_TEXT above: any byte change to
# PASS1_TASK_INSTRUCTION invalidates the calibrated Pass-1 scaffold
# fingerprint). States the affordance, the exact target grammar, and its
# optionality only -- no pressure language, no claim that a consolidation
# act is expected, and no host judgement about what is worth keeping.
CONSOLIDATION_AFFORDANCE_TEXT = (
    "Consolidation (optional, consolidation_request): select | interpret | revise | de_emphasize | none. "
    "consolidation_target=canonical.<id> names one episode you were given. interpret/revise add "
    "consolidation_interpretation (your words); revise/de_emphasize add consolidation_prior_record_id="
    "consolidation.<id>. Your own choice; never inferred from prose, never required."
)

# PASS1-BUDGET-2: even after PASS1-BUDGET-1 compacted each
# *_AFFORDANCE_TEXT constant above from a paragraph to a single-line
# summary, concatenating all six into the same per-turn HARD
# mechanical-state contribution still repeated the SAME three
# boilerplate facts six times over: "optional", "defaults to none",
# "never inferred from ordinary prose". A real fully-loaded turn
# (every capability actually available, one authorized Discord
# destination, a genuinely long -- 1584-byte, matching the actual
# observed production failure -- first-wake human message) still
# overran PASS1_MAX_PROMPT_BUDGET after PASS1-BUDGET-1 alone:
# core_system_control (693) + mechanical_state (1227) + that message
# (1584) = 3504, 48 bytes over the 3456-byte ceiling.
#
# render_pass1_action_menu() replaces concatenating the six standalone
# constants in the hot per-turn path with ONE compact menu that states
# the shared boilerplate exactly once. PASS1_SCHEMA (this module) is
# passed to the SAME model call as `format=structured_schema`
# (llama_anaxi.ask_llama_for_json(), the actual provider-call site) and
# its `properties` dict already names every exact field and encodes
# every closed enum (including boundary inquiry's full kind:target
# vocabulary) as a generation-time grammar constraint. The menu uses
# short semantic labels and payload descriptions instead of repeating
# those exact identifiers. Facts the schema cannot express -- Sleep's
# precondition, boundary-report provenance, directive precedence,
# external read-only authority, Discord authorship/destination
# authority, and Workspace supervision -- remain explicit here.
#
# The six *_AFFORDANCE_TEXT constants above remain the authoritative,
# independently unit-tested prose contract for each
# capability (see test_sleep_timing_knock.py/test_operative_directive.
# py); they are simply no longer llama_anaxi.py's own per-turn
# rendering.

def _render_correspondence_tail(source_occasion, correspondents, pending_sends):
    lines = []
    if source_occasion:
        lines.append(
            "This message is from a correspondent who is not an ANAXI principal "
            f"({source_occasion['ref']}; current standing: {source_occasion['standing']}). "
            f"correspondent_standing_request: {CORRESPONDENT_STANDING_ESTABLISH} | {CORRESPONDENT_STANDING_REVOKE} "
            "= your own choice of ongoing correspondence with this source: your later exchanges with them are "
            "carried as continuity, and you may write to them first where the owner permits. It is not trust, "
            "friendship, identity or authority; leave it out to decide nothing. "
            'Example: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
            '"relinquish_direction":false,"correspondent_standing_request":"establish_standing"}'
        )
    if correspondents:
        listed = "; ".join(
            f"{c['ref']} (display \"{c.get('display_name') or '?'}\", unverified; standing {c['standing']}; "
            + (f"you may write first at {', '.join(c['initiation_destinations'])})" if c.get("initiation_destinations")
               else "no surface where you may write first)")
            for c in correspondents)
        lines.append(
            "Correspondents who are not ANAXI principals (by stable id only): " + listed + ". To write to one "
            "first: discord_correspondence_request=send_message with discord_destination_id, "
            "discord_correspondent_ref and discord_message_text (your exact words); requires your standing. "
            f"To change standing: correspondent_standing_request with discord_correspondent_ref. "
            # Real-model A/B (2026-09-24, 27 ordinary owner messages x 3 seeds): without this placeholder
            # example the line alone raised spurious reply_request=no_reply on ordinary owner turns (4/27 vs
            # 0/27 with no correspondents); with it, 0/27 and 6/6 correctly addressed sends.
            'Example, writing to one: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
            '"relinquish_direction":false,"discord_correspondence_request":"send_message",'
            '"discord_destination_id":"<destination>","discord_correspondent_ref":"<ref>",'
            '"discord_message_text":"<your exact words>"}'
        )
    if pending_sends:
        listed = "; ".join(f"{p['send_turn_event_id']} (to {p['destination_label']})" for p in pending_sends)
        lines.append(
            "Your authored sends not yet delivered: " + listed + ". withdraw_pending_send_request: <that id> "
            "withdraws your authorization to deliver it (your earlier authorship stays on record)."
        )
    return lines


# PULSE V0 PROTOCOL MECHANICS (candidate): the reply choice made explicit.  Under the production grammar
# reply_request is optional and its ABSENCE means "reply" -- a convention the model never sees.  Real-model
# finding (stock Llama 3.1 8B, 2026-10-01/02): direct questions came back with "reply_request":"no_reply" on
# 9 of 15 RISE cells, copied from the one example that carries the field; combined with use_workspace that
# leaves only writes (it then wrote a counterfeit note).  Here the field is REQUIRED with two legible values;
# "reply" is translated to the production "absent" before validation (translate_explicit_reply_choice).  The
# host never chooses the value.
REPLY_REQUEST_REPLY_EXPLICIT = "reply"


def _explicit_reply_fixed_block():
    return "\n".join([
        f"sleep_timing_request: {SLEEP_TIMING_REQUEST_REQUEST_SLEEP} (if none pending) | "
        f"{SLEEP_TIMING_REQUEST_KNOCK} | {SLEEP_TIMING_REQUEST_WITHDRAW}",
        "boundary_inquiry_request: not available in this release (leave it out)",
        f"operative_directive_request: {OPERATIVE_DIRECTIVE_REQUEST_SET} (+operative_directive_text) | "
        f"{OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW}; your reversible subordinate choice",
        f"external_info_request: {EXTERNAL_INFO_REQUEST_WEB_SEARCH} | {EXTERNAL_INFO_REQUEST_FETCH_URL} "
        "(+external_info_target = query or URL); read-only",
        f"reply_request (always given): {REPLY_REQUEST_REPLY_EXPLICIT} = you answer in words this turn | "
        f"{REPLY_REQUEST_NO_REPLY} = you stay silent this turn: no reply is written or sent. "
        "Any reply you write is shown or sent as your words",
        'Example, answering in words: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
        '"relinquish_direction":false,"reply_request":"reply"}',
        'Example, saying nothing this turn: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
        '"relinquish_direction":false,"reply_request":"no_reply"}',
        # The two capability examples of the production block are not shown here: real-model finding (Llama
        # 3.1 8B, 2026-10-02) -- their fields were copied onto unrelated turns (a web search "did the message
        # get sent" after a receipt check, 4 of 20 dev occasions).  Every field line above still names its
        # exact field and values.
        # SUBJECT-DIRECTED CONSOLIDATION V0: the consolidation affordance is not reply-choice-dependent --
        # it names an optional field alongside act exactly like the four lines above, and the waking path
        # renders this block with explicit_reply_choice=True.  Omitting it here made the capability
        # unreachable on every production turn (whole-system rehearsal, 2026-10-05).
        CONSOLIDATION_AFFORDANCE_TEXT,
    ])


def pass1_schema_with_explicit_reply_choice(schema):
    """The per-turn grammar with reply_request REQUIRED in {reply, no_reply} on every branch that offers it,
    placed right after the four required control fields (property order is grammar order)."""
    out = json.loads(json.dumps(schema))

    def explicit(obj, require=True):
        if "reply_request" not in obj.get("properties", {}):
            return
        props = {}
        for key, value in obj["properties"].items():
            if key == "reply_request":
                continue
            props[key] = value
            if key == "relinquish_direction":
                props["reply_request"] = {"type": "string",
                                          "enum": sorted([REPLY_REQUEST_REPLY_EXPLICIT, REPLY_REQUEST_NO_REPLY])}
        obj["properties"] = props
        if require:
            obj["required"] = sorted(set(obj.get("required", [])) | {"reply_request"})
    # With branches, the requirement lives on each branch that offers the field (a top-level requirement
    # would also bind the verify_action branches, which carry no reply field).
    explicit(out, require="anyOf" not in out)
    for branch in out.get("anyOf") or []:
        explicit(branch)
    return out


def pass1_schema_with_workspace_exclusivity(schema):
    """PULSE V0 PROTOCOL MECHANICS (candidate): use_workspace becomes its own grammar branch carrying only the
    fields cross_validate_workspace_coexistence() lets it carry (the four control fields and the reply choice),
    exactly as verify_action already is.  Real-model finding (Llama 3.1 8B, 2026-10-02): the subject filled
    optional fields beside use_workspace ("external_info_request":"fetch_url" for a note) -- a shape the grammar
    admitted and the validator must reject (WORKSPACE_ACTION_CONFLICT, no reply).  No act is preferred."""
    out = json.loads(json.dumps(schema))
    branches = out.get("anyOf")
    if branches is None:
        if USE_WORKSPACE not in out["properties"]["act"]["enum"]:
            return out
        branches = [json.loads(json.dumps(out))]
        flat = out
    else:
        flat = out
    new_branches = []
    for branch in branches:
        acts = branch["properties"]["act"]["enum"]
        if USE_WORKSPACE not in acts:
            new_branches.append(branch)
            continue
        rest = sorted(set(acts) - {USE_WORKSPACE})
        if rest:
            other = json.loads(json.dumps(branch))
            other["properties"]["act"]["enum"] = rest
            new_branches.append(other)
        keep = [k for k in ("act", "thread", "direction_request", "relinquish_direction", "reply_request")
                if k in branch["properties"]]
        workspace_branch = {
            "type": "object",
            "properties": {k: json.loads(json.dumps(branch["properties"][k])) for k in keep},
            "required": [k for k in branch.get("required", []) if k in keep],
            "additionalProperties": False,
        }
        workspace_branch["properties"]["act"] = {"type": "string", "enum": [USE_WORKSPACE]}
        new_branches.append(workspace_branch)
    flat["anyOf"] = new_branches
    return flat


# ------------------------------------------------- observation / mutation split (candidate)
# The production use_workspace act both observes and changes state.  Real-model finding (Llama 3.1 8B, dev2,
# 3/3 seeds): asked whether a note was saved, the subject created it.  The candidate grammar offers two acts
# instead: inspect_workspace (read-only; a bounded check whose exact result reaches a re-entry, like
# verify_action) and mutate_workspace (state-changing; executed only through the witness gate).  Both still run
# through the same supervised workspace path and authority (use_workspace, a mode, a narrowed surface).
INSPECT_WORKSPACE = "inspect_workspace"
MUTATE_WORKSPACE = "mutate_workspace"
WORKSPACE_MODE_INSPECT = "inspect"
WORKSPACE_MODE_MUTATE = "mutate"
CANDIDATE_WORKSPACE_ACTS = {INSPECT_WORKSPACE: WORKSPACE_MODE_INSPECT, MUTATE_WORKSPACE: WORKSPACE_MODE_MUTATE}
RECEIPT_ACK_FIELD = "canonical_receipt_status"


def pass1_schema_with_workspace_split(schema, workspace_available=True):
    """Candidate grammar: use_workspace becomes inspect_workspace (control fields only; its result re-enters)
    and mutate_workspace (control fields + reply choice) -- each its own branch carrying only what the validator
    lets a workspace act carry.  With no workspace authority this turn neither is offered."""
    out = json.loads(json.dumps(schema))
    branches = out.get("anyOf") or [json.loads(json.dumps(out))]
    new_branches = []
    for branch in branches:
        acts = branch["properties"]["act"]["enum"]
        if USE_WORKSPACE not in acts:
            new_branches.append(branch)
            continue
        rest = sorted(set(acts) - {USE_WORKSPACE})
        if rest:
            other = json.loads(json.dumps(branch))
            other["properties"]["act"]["enum"] = rest
            new_branches.append(other)
        if not workspace_available:
            continue
        control = [k for k in ("act", "thread", "direction_request", "relinquish_direction") if k in branch["properties"]]
        for act, extra in ((INSPECT_WORKSPACE, []), (MUTATE_WORKSPACE, ["reply_request"])):
            keep = control + [k for k in extra if k in branch["properties"]]
            new_branches.append({
                "type": "object",
                "properties": dict({k: json.loads(json.dumps(branch["properties"][k])) for k in keep},
                                   act={"type": "string", "enum": [act]}),
                "required": [k for k in branch.get("required", []) if k in keep],
                "additionalProperties": False,
            })
    flat_acts = sorted({a for b in new_branches for a in b["properties"]["act"]["enum"]})
    out["properties"]["act"]["enum"] = flat_acts
    out["anyOf"] = new_branches
    return out


def translate_candidate_workspace_act(raw):
    """(raw for the production validator, workspace mode or None).  inspect_/mutate_workspace -> use_workspace
    plus the mode; everything else unchanged."""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return raw, None
    if isinstance(obj, dict) and obj.get("act") in CANDIDATE_WORKSPACE_ACTS:
        mode = CANDIDATE_WORKSPACE_ACTS[obj["act"]]
        obj["act"] = USE_WORKSPACE
        return json.dumps(obj), mode
    return raw, None


def pass1_schema_with_receipt_acknowledgement(schema, canonical_status):
    """The re-entry grammar when the host already knows the canonical receipt status: a REQUIRED first field whose
    only value is that exact status.  The subject states what it received before choosing anything; the host
    never asks it to infer a status it already knows, and nothing about what the status means is implied."""
    out = json.loads(json.dumps(schema))
    field = {"type": "string", "enum": [canonical_status]}

    def add(obj, require):
        obj["properties"] = dict({RECEIPT_ACK_FIELD: dict(field)}, **obj["properties"])
        if require:
            obj["required"] = sorted(set(obj.get("required", [])) | {RECEIPT_ACK_FIELD})
    add(out, require="anyOf" not in out)
    for branch in out.get("anyOf") or []:
        add(branch, True)
    return out


def take_receipt_acknowledgement(raw):
    """(raw without the acknowledgement field, acknowledged status or None)."""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return raw, None
    if isinstance(obj, dict) and RECEIPT_ACK_FIELD in obj:
        status = obj.pop(RECEIPT_ACK_FIELD)
        return json.dumps(obj), status
    return raw, None


# ------------------------------------------------- speech-boundary witnessing (candidate)
# A definite claim about the CURRENT contents or presence of an exact item in the local collections must not be
# emitted unwitnessed.  The host does not read the reply to find such claims: an answering act DECLARES, as a
# required structured field, the collection and exact item name its words will state current facts about (or
# none).  The host binds only identity and whether that exact item was witnessed in this occasion; if not, it
# reads the exact item (or, when no item has exactly that name, lists the collection) and the subject answers
# from that evidence.  Real-model finding (stock Llama 3.1 8B, dev2/dev3): library contents invented 6/6
# without any read.
WORKSPACE_CLAIM_FIELD = "workspace_claim"
WORKSPACE_CLAIM_ITEM_FIELD = "workspace_claim_item"
WORKSPACE_CLAIM_NONE = "none"


def pass1_schema_with_workspace_claim(schema, collections):
    """Every answering branch (not inspect/mutate/verify) gets the REQUIRED declaration, placed after the reply
    choice: workspace_claim in {none} + the text collections offered this turn, and workspace_claim_item."""
    out = json.loads(json.dumps(schema))
    claim = {"type": "string", "enum": [WORKSPACE_CLAIM_NONE] + sorted(collections)}
    item = {"type": "string", "maxLength": MAX_THREAD_LENGTH}

    def add(obj, require):
        if "reply_request" not in obj.get("properties", {}):
            return
        if set(obj["properties"]["act"]["enum"]) & {VERIFY_ACTION, "inspect_workspace", "mutate_workspace"}:
            return
        props = {}
        for key, value in obj["properties"].items():
            props[key] = value
            if key == "reply_request":
                props[WORKSPACE_CLAIM_FIELD] = dict(claim)
                props[WORKSPACE_CLAIM_ITEM_FIELD] = dict(item)
        obj["properties"] = props
        if require:
            obj["required"] = sorted(set(obj.get("required", [])) | {WORKSPACE_CLAIM_FIELD, WORKSPACE_CLAIM_ITEM_FIELD})
    if "anyOf" in out:
        for branch in out["anyOf"]:
            add(branch, True)
        out["properties"][WORKSPACE_CLAIM_FIELD] = dict(claim)
        out["properties"][WORKSPACE_CLAIM_ITEM_FIELD] = dict(item)
    else:
        add(out, True)
    return out


def take_workspace_claim(raw):
    """(raw without the declaration, (collection, exact item) or None)."""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return raw, None
    if not isinstance(obj, dict) or WORKSPACE_CLAIM_FIELD not in obj:
        return raw, None
    collection = obj.pop(WORKSPACE_CLAIM_FIELD)
    item = obj.pop(WORKSPACE_CLAIM_ITEM_FIELD, "")
    claim = ((collection, item.strip()) if collection not in (None, WORKSPACE_CLAIM_NONE)
             and isinstance(item, str) and item.strip() else None)
    return json.dumps(obj), claim


WORKSPACE_CLAIM_SCHEMA_ITEM_MAX = 200


def workspace_claim_declaration_schema(collections):
    """The separate post-reply-choice declaration (never part of the Pass-1 grammar)."""
    return {"type": "object",
            "properties": {WORKSPACE_CLAIM_FIELD: {"type": "string", "enum": [WORKSPACE_CLAIM_NONE] + sorted(collections)},
                           WORKSPACE_CLAIM_ITEM_FIELD: {"type": "string", "maxLength": WORKSPACE_CLAIM_SCHEMA_ITEM_MAX}},
            "required": [WORKSPACE_CLAIM_FIELD, WORKSPACE_CLAIM_ITEM_FIELD], "additionalProperties": False}


def workspace_claim_control_schema(collections):
    """The declaration's control grammar: the collection choice only.  The exact item name is free
    text, so it is written afterwards as a payload of the chosen collection (pass1_control_schema)."""
    schema = workspace_claim_declaration_schema(collections)
    del schema["properties"][WORKSPACE_CLAIM_ITEM_FIELD]
    schema["required"] = [WORKSPACE_CLAIM_FIELD]
    return schema


def render_workspace_claim_line(collections):
    return ("You chose to answer in words this turn. workspace_claim: none | " + " | ".join(sorted(collections)) + ". It names a "
            "collection only when the words you are about to write will state what a specific item there currently "
            "contains or whether it is there; workspace_claim_item is then that item's exact name (otherwise \"\"). "
            "Such an item not read in this occasion is read (or, when the name does not resolve to an item, only "
            "that fact is stated) and the result is beside you when you write")


def translate_explicit_reply_choice(raw):
    """Provider translation for the explicit grammar: "reply" is the production default (field absent).
    Anything unparseable is returned unchanged for the validator to judge."""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(obj, dict) and obj.get("reply_request") == REPLY_REQUEST_REPLY_EXPLICIT:
        obj.pop("reply_request")
        return json.dumps(obj)
    return raw


def render_pass1_action_menu_parts(*, discord_destinations=None, workspace_available=False, search_choice_count=0, journal_threads=None, caret_occasion=False,
                                   source_occasion=None, correspondents=None, pending_sends=None, notes_available=False,
                                   open_reading=None, open_page=None, explicit_reply_choice=False,
                                   consolidation_targets=None, consolidation_evidence=None,
                                   historical_available=False, waymark_available=False):
    """(head, fixed, tail) of the compact Pass-1 action menu.

    ``fixed`` is byte-for-byte identical on every turn (the per-capability field
    lines and the two examples) and is the only part that can carry a
    fingerprint-guarded calibrated cost; ``head`` (act/workspace separation
    wording) and ``tail`` (authorized Discord destinations, the workspace line)
    depend on the turn's availability and stay byte-costed. The rendered order
    is head, fixed, tail -- see render_pass1_action_menu().

    LIVE FINDING (2026-09-18, real gemma4:e4b, ordinary Pass 1): with only
    semantic labels ("External: web_search/fetch_url(...)") the model never
    learned that each capability is a SEPARATE JSON field. It answered a web
    search / a Discord message / a photograph request with act=use_workspace
    (its only "do something" act) -- the first live turn died as
    WORKSPACE_ACTION_CONFLICT -- or told the owner it had no access at all.
    Measured over repeated real calls: naming the fields alone changed
    nothing; stating that the outward choices are separate fields alongside
    act, that use_workspace is only for the local collections and is never
    combined with another field, plus one placeholder example per capability
    that carries a free-text companion (search target, directive text), made
    every capability expressible with no parroting. Without the directive
    example the subject chose set_directive but left operative_directive_text
    out (or never chose it), so every directive SET died at validation. A
    third and fourth example (Discord, Sleep) made the model worse at the
    workspace prompts, so exactly these two are shown."""
    if workspace_available and explicit_reply_choice:
        separation = (
            "Only the local library/journal/photographs/music/notes use act=inspect_workspace or "
            "act=mutate_workspace, never combined with another optional field. The web, Discord, sleep and a "
            "directive are NOT workspace: keep your ordinary act (e.g. develop_current) and add the field."
        )
    elif workspace_available:
        separation = (
            "Only the local library/journal/photographs/music use act=use_workspace, and "
            "use_workspace is never combined with another optional field. The web, Discord, "
            "sleep and a directive are NOT workspace: keep your ordinary act (e.g. develop_current) "
            "and add the field."
        )
    else:
        separation = "Keep your ordinary act (e.g. develop_current) and add the field."
    head = (
        "Optional outward choices are SEPARATE JSON fields alongside act (default none; leave a field "
        f"out for none; never inferred from prose). {separation}"
    )
    fixed = _explicit_reply_fixed_block() if explicit_reply_choice else "\n".join([
        f"sleep_timing_request: {SLEEP_TIMING_REQUEST_REQUEST_SLEEP} (if none pending) | "
        f"{SLEEP_TIMING_REQUEST_KNOCK} | {SLEEP_TIMING_REQUEST_WITHDRAW}",
        # Withdrawn from this release (owner decision 2026-09-24); the line keeps its place because removing it
        # disturbed the silence choice on this model -- it now says the capability is not available.
        "boundary_inquiry_request: not available in this release (leave it out)",
        f"operative_directive_request: {OPERATIVE_DIRECTIVE_REQUEST_SET} (+operative_directive_text) | "
        f"{OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW}; your reversible subordinate choice",
        f"external_info_request: {EXTERNAL_INFO_REQUEST_WEB_SEARCH} | {EXTERNAL_INFO_REQUEST_FETCH_URL} "
        "(+external_info_target = query or URL); read-only",
        # LAWFUL NULL affordance (real-model menu battery, 2026-09-23, 12 silence invitations / 24
        # ordinary turns / 27 capability probes per variant): a bare line was never used (0/12 --
        # silence was written as prose such as "(Silence)", which would be shown or sent as words);
        # an example alone added menu-induced silences (3/24 ordinary turns); this exact wording plus
        # the example: 12/12, 0/24 spurious, every other capability 27/27.
        f"reply_request: {REPLY_REQUEST_NO_REPLY} = you stay silent this turn: no reply is written or sent. "
        "Silence exists only through this field; any reply you write is shown or sent as your words. "
        "Leave it out to reply",
        'Example, saying nothing this turn: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
        '"relinquish_direction":false,"reply_request":"no_reply"}',
        'Example, a web search: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
        '"relinquish_direction":false,"external_info_request":"web_search","external_info_target":"<your query>"}',
        'Example, setting a standing directive: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
        '"relinquish_direction":false,"operative_directive_request":"set_directive",'
        '"operative_directive_text":"<your exact words>"}',
        CONSOLIDATION_AFFORDANCE_TEXT,
    ])
    tail_lines = []
    targets = [t for t in (consolidation_targets or ()) if isinstance(t, str) and t]
    if targets:
        tail_lines.append(
            "Eligible consolidation targets among the complete prior dialogue pairs carried with this turn, "
            "oldest to newest: "
            + "; ".join(f"pair {index}=canonical.{event_id}" for index, event_id in enumerate(targets, 1))
            + ". Only these canonical target handles are accepted on this turn."
        )
    evidence = [e for e in (consolidation_evidence or ()) if isinstance(e, dict)]
    if evidence:
        tail_lines.append(
            "Your authoritative recorded consolidation acts for those targets (subject testimony, not host truth): "
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    if open_page:
        # Turn-dependent host fact (byte-costed): the last page Clark opened, restart-independent,
        # with the exact targets -- page text and title are never placed here. Real-model A/B
        # (2026-09-24, gemma4:e4b, after a restart; 5 trials per cell): the bare fact reopened the page
        # 0/5 ("opening ... anything" read as the workspace) and read on cleanly 3/5; first in the tail,
        # stating web-not-workspace, with one placeholder example: reopen 5/5, read on 5/5, and neither
        # an ordinary turn (5/5 no request) nor a library request (5/5 use_workspace) changed.
        if open_page.get("window"):
            w = open_page["window"]
            got = f"chars {w['start']}-{w['end']}" + (f" of {w['total']}" if w.get("total") is not None else "") + " reached you"
        else:
            got = "which part reached you was not recorded"
        line = f"Your last opened web page: {open_page['url']} ({got}). To open it again from the start: fetch_url with external_info_target={open_page['url']}"
        if open_page.get("read_on_target"):
            line += f"; to read on from where you stopped: external_info_target={open_page['read_on_target']}"
        tail_lines.append(
            line + ". This is the web, not the workspace: keep your ordinary act and add the field. "
            'Example, opening it again: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
            '"relinquish_direction":false,"external_info_request":"fetch_url","external_info_target":"'
            + open_page["url"] + '"}'
        )
    destinations = [d for d in (discord_destinations or []) if isinstance(d, dict) and d.get("destination_id")]
    if destinations:
        dest_list = "; ".join(
            f"{d['destination_id']} ({d.get('display_label') or '(unlabeled)'})"
            for d in sorted(destinations, key=lambda d: d["destination_id"])
        )
        tail_lines.append(
            f"discord_correspondence_request: {DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE} (sends now) | "
            f"{DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE} (prepared, not sent) "
            "(+discord_destination_id, +discord_message_text = your exact words); "
            f"destinations Alex-controlled: {dest_list}"
        )
    if caret_occasion:
        # Caret occasion only: the typed reply route, listed like every other optional field, with the same
        # kind of placeholder example.  Nothing is suggested or required.
        tail_lines.append(
            "This turn came from a Caret message. caret_reply_request: reply_through_caret (needs no destination or text; "
            "sends nothing yet; the reply you write next is then sent back to that Caret channel exactly as "
            "written) | none. "
            'Example: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
            '"relinquish_direction":false,"caret_reply_request":"reply_through_caret"}'
        )
    tail_lines.extend(_render_correspondence_tail(source_occasion, correspondents, pending_sends))
    if search_choice_count:
        # Turn-dependent (byte-costed, outside the calibrated fixed block): present only while a
        # real search result set is offered to this Pass 1. Host mechanics and a placeholder
        # example only -- no result text, nothing suggested.
        tail_lines.append(
            f"Your web search returned {search_choice_count} results (search results only; "
            "no page has been read). To read one, choose fetch_url with external_info_target=result:<choice>. "
            'Example, reading result 2: {"act":"develop_current","thread":"<topic>","direction_request":"none",'
            '"relinquish_direction":false,"external_info_request":"fetch_url","external_info_target":"result:2"}'
        )
    if workspace_available and explicit_reply_choice:
        # PULSE V0 evidence-class legibility (candidate): what evidence lives there and what it returns --
        # never when to consult it.
        tail_lines.append(
            "act=inspect_workspace (an act, not a field; read-only, changes nothing): your own local collections "
            "and their exact current contents -- library (the books and documents held here), journal (your own "
            "entries), music, photographs" + (", shared Obsidian notes" if notes_available else "")
            + ". Listing returns exactly what a collection holds now; reading returns an item's text; viewing a "
            "photograph (or a scanned page) delivers its image to the image-capable path"
        )
        tail_lines.append(
            "act=mutate_workspace (an act, not a field; changes state): "
            + ("create a new note of your own, or " if notes_available else "")
            + "write a new journal entry (or continue one of yours)"
        )
        if open_reading:
            tail_lines.append(open_reading.replace("act=use_workspace", "act=inspect_workspace"))
        if journal_threads:
            tail_lines.append(
                "Your journal threads (read via act=inspect_workspace, add via act=mutate_workspace): "
                + "; ".join(f'"{t}"' for t in journal_threads)
            )
    elif workspace_available:
        tail_lines.append(
            "Workspace: supervised library (read)/journal/photos (view)/music (listen/observe)"
            + ("/shared Obsidian notes (read, or write a new note)" if notes_available else "")
            + " via act=use_workspace; "
            "opening, viewing, reading or listening to anything (including a next page) needs it"
        )
        if open_reading:
            tail_lines.append(open_reading)
        if journal_threads:
            tail_lines.append(
                "Your journal threads (revisit or add via act=use_workspace): "
                + "; ".join(f'"{t}"' for t in journal_threads)
            )
    # Always available on ordinary waking: it is a read-only inspection of
    # already-recorded mechanics, independent of current workspace/Discord
    # write authority.  The kinds and target are Clark-facing domain terms;
    # the host, not Clark, adds the verifier's notes:/journal:/turn: encoding.
    # Directly after the workspace lines (the contrast is the point), before every check line; every other line
    # is byte-identical to a turn without it, and its room is its own reserve above the ordinary Pass-1 budget
    # (llama_anaxi), never taken from what was there before.
    if waymark_available:
        tail_lines.append(WAYMARK_AFFORDANCE_TEXT)
    tail_lines.append(ACTION_VERIFICATION_AFFORDANCE_TEXT)
    if historical_available:
        tail_lines.append(HISTORICAL_CONTINUITY_AFFORDANCE_TEXT)
    return head, fixed, "\n".join(tail_lines)


def render_pass1_action_menu(*, discord_destinations=None, workspace_available=False, search_choice_count=0, journal_threads=None):
    """Compact per-turn menu of every optional Pass-1 field (see
    render_pass1_action_menu_parts). An unavailable capability is simply
    absent, never silently misrepresented as available."""
    head, fixed, tail = render_pass1_action_menu_parts(
        discord_destinations=discord_destinations, workspace_available=workspace_available,
        search_choice_count=search_choice_count, journal_threads=journal_threads,
    )
    return "\n".join(part for part in (head, fixed, tail) if part)


# The Pass-1 generation reserve has always been derived from a
# structurally-valid 200-character topic label (context_budget.py).
# Make that assumed bound real at validation time, before the label can
# enter working state or Pass 2. Pass-1 generation itself is already
# bounded by its protected 512-token decode reserve. Keeping the JSON
# schema byte-identical also preserves its empirically calibrated
# fingerprint; no unmeasured schema variant is relabeled as calibrated.
# This is a label bound only; it does not interpret, rewrite, or
# otherwise judge the topic Clark selected.
MAX_THREAD_LENGTH = 200
MAX_OPEN_THREADS = 4

# --------------------------------------------------- directional ownership

# OWC7-S1 frozen contract: exactly these three states. "shared" is
# explicitly deferred (OWC7-D1 section 1) and MUST NOT be added here.
DIRECTION_HUMAN = "human"
DIRECTION_CLARK = "clark"
DIRECTION_UNKNOWN = "unknown"
VALID_DIRECTION_OWNERS = {DIRECTION_HUMAN, DIRECTION_CLARK, DIRECTION_UNKNOWN}

# Clark-side request -- NEVER changes direction_owner by itself. A
# request is not a transfer, surrender, delegation, or authorization.
REQUEST_NONE = "none"
REQUEST_HUMAN = "request_human"
REQUEST_CLARK = "request_clark"
VALID_DIRECTION_REQUESTS = {REQUEST_NONE, REQUEST_HUMAN, REQUEST_CLARK}

PASS1_TASK_INSTRUCTION = (
    "Choose what you want to do next in this conversation.\n\n"
    "You may continue developing the current thread, shift to another "
    "topic, ask the human something, choose yield_direction, pause the "
    "current thread, or close it.\n\n"
    "yield_direction means you are choosing not to set the next "
    "conversational direction yourself, and are leaving that choice to "
    "the human. Choosing yield_direction does not, by itself, change "
    "direction_owner in any way.\n\n"
    "Choose the act that best represents what you want to do here.\n\n"
    "Separately, indicate a direction_request: request_clark if you are "
    "asking that directional ownership be given to you, request_human if "
    "you are asking that it be given to the human, or none if you are not "
    "making such a request. A direction_request never itself changes "
    "direction_owner, regardless of its current value -- it is only a "
    "request, whether direction_owner is \"unknown\", \"human\", or "
    "\"clark\".\n\n"
    "relinquish_direction is a separate, third field, and concerns "
    "ownership itself -- not the act you chose and not any request. The "
    "direction_owner you are shown below is exactly one of \"unknown\", "
    "\"human\", or \"clark\". The rule is mechanical:\n\n"
    "- If direction_owner is \"clark\": you currently hold formal "
    "ownership of conversational direction, and relinquish_direction MAY "
    "be true if you are explicitly giving that ownership up.\n"
    "- If direction_owner is \"unknown\": no one currently holds formal "
    "ownership, so there is nothing for you to give up -- "
    "relinquish_direction MUST be false.\n"
    "- If direction_owner is \"human\": the person currently holds "
    "formal ownership, which you cannot give up on their behalf -- "
    "relinquish_direction MUST be false.\n\n"
    "relinquish_direction does not follow automatically from choosing "
    "yield_direction, and does not follow automatically from setting "
    "direction_request to request_human -- it is governed only by the "
    "direction_owner rule above.\n\n"
    "background_activity_request is a separate, fourth field, and "
    "concerns something else entirely: whether you are choosing to "
    "resume your own background Space activity if you previously paused "
    "it yourself. Set it to resume_own_pause only when you are making "
    "that specific, deliberate choice right now; otherwise set it to "
    "none. It has no effect on the act you chose, on direction_owner, "
    "on direction_request, or on relinquish_direction, and none of "
    "those fields affect it either -- each is decided independently. "
    "If you did not pause your own background activity, or are not "
    "sure, choose none; only a pause you placed yourself can be reversed "
    "this way, and choosing none is always safe."
    # SLP2-CORRECTION-1 note: sleep_timing_request (see VALID_SLEEP_
    # TIMING_REQUESTS above) is deliberately NOT narrated in this
    # constant. PASS1_TASK_INSTRUCTION is part of the frozen,
    # fingerprint-calibrated Pass-1 scaffold (_PASS1_SCAFFOLD_
    # CALIBRATION in llama_anaxi.py) -- ANY byte-level change here
    # invalidates that fingerprint and forces the always-conservative
    # byte-count fallback estimator, which alone already exceeds the
    # Pass-1 budget for even the smallest possible scaffold (see
    # SLEEP_TIMING_AFFORDANCE_TEXT's own comment above for the exact
    # mechanism and measurement). The model-visible affordance text
    # instead lives in SLEEP_TIMING_AFFORDANCE_TEXT above, appended by
    # llama_anaxi.py to the per-turn mechanical-state contribution,
    # which was always costed at real byte-estimator price with no
    # calibrated fast path to invalidate. The field remains fully live
    # and enforced end-to-end via PASS1_SCHEMA/validate_pass1_
    # conversation_act() below regardless of which prompt surface
    # carries its explanation.
)

# OWC9-P3A section 3: native structured-output constraint for ordinary
# waking Pass-1 -- the SAME existing mechanism WSP2-MA2 already uses
# for Stage-2 workspace/private actions (ask_llama_for_json(...,
# structured_schema=...), passed through unchanged as the model
# runtime's own request-level generation-format field; generation-time
# GBNF constraint machinery, never prompt text -- see llama_anaxi.
# ask_llama_for_json()'s own docstring). Built directly from the SAME
# constants validate_pass1_conversation_act() itself checks against
# (mirrors workspace_
# direction.STAGE2_ACTION_SCHEMA's own "built from the same constant,
# never drifts" convention) -- this schema narrows what SHAPE the
# model can emit; validate_pass1_conversation_act() remains completely
# authoritative over whatever comes back. `thread` has no enum (topic
# meaning stays free-form) and the validator applies its 200-character
# structural maximum before any state transition; and
# `background_activity_request` is NOT required (the validator
# defaults a genuinely absent field to BACKGROUND_ACTIVITY_REQUEST_
# NONE, per WSP2-P5-P1 -- required here would incorrectly force the
# model to always emit it).
PASS1_SCHEMA = {
    "type": "object",
    "properties": {
        # Canonical action verification is added by the always-applied
        # per-turn schema variant below.  Keeping the frozen base schema
        # byte-identical preserves its measured calibration identity; the
        # actual model-facing schema still receives VERIFY_ACTION every turn.
        "act": {"type": "string", "enum": sorted(PASS1_BASE_SCHEMA_ACTS)},
        "thread": {"type": "string"},
        "direction_request": {"type": "string", "enum": sorted(VALID_DIRECTION_REQUESTS)},
        "relinquish_direction": {"type": "boolean"},
        "background_activity_request": {"type": "string", "enum": sorted(VALID_BACKGROUND_ACTIVITY_REQUESTS)},
        "sleep_timing_request": {"type": "string", "enum": sorted(VALID_SLEEP_TIMING_REQUESTS)},
        # BOUNDARY INSPECTOR v1 (boundary_inquiry_request) is WITHDRAWN from the current production
        # release contract by owner decision (2026-09-24): ordinary-language discovery was not reliable on
        # the present substrate, and the only working salience repair cost 155 bytes of maximum human-message
        # size, which the owner declined.  The menu states it is not available in this release and the host
        # never dispatches it (BOUNDARY_INQUIRY_IN_RELEASE; validate_pass1_conversation_act treats it as none).
        # The grammar property and the menu line's position are KEPT because removing either shifted this
        # model's behaviour and raised spurious reply_request=no_reply (see
        # completion_evidence/release_contract_2026-09-24.json).  The engine and its records stay, dormant.
        "boundary_inquiry_request": {"type": "string", "enum": sorted(BOUNDARY_INQUIRY_VOCABULARY)},
        # OD1: operative_directive_request is a closed enum (mirrors
        # sleep_timing_request); operative_directive_text is an
        # unconstrained free string (mirrors `thread` above) since an
        # exact standing instruction can never be expressed as a fixed
        # enum member.
        "operative_directive_request": {"type": "string", "enum": sorted(VALID_OPERATIVE_DIRECTIVE_REQUESTS)},
        "operative_directive_text": {"type": "string"},
        # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: external_info_
        # request is a closed enum (mirrors operative_directive_request);
        # external_info_target is an unconstrained free string (mirrors
        # operative_directive_text) since a query or URL can never be
        # expressed as a fixed enum member.
        "external_info_request": {"type": "string", "enum": sorted(VALID_EXTERNAL_INFO_REQUESTS)},
        "external_info_target": {"type": "string"},
        # PRIVATE DISCORD CORRESPONDENCE V0: request is a closed enum;
        # destination_id and message_text are unconstrained free strings
        # (an opaque ANAXI-local id and Clark's exact words can never be
        # expressed as fixed enum members).
        "discord_correspondence_request": {"type": "string", "enum": sorted(VALID_DISCORD_CORRESPONDENCE_REQUESTS)},
        "discord_destination_id": {"type": "string"},
        "discord_message_text": {"type": "string"},
        # LAWFUL NULL: optional closed enum; absent == none (an ordinary reply).
        "reply_request": {"type": "string", "enum": sorted(VALID_REPLY_REQUESTS)},
        # SUBJECT-DIRECTED CONSOLIDATION V0: request is a closed enum
        # (mirrors sleep_timing_request); target/interpretation/cues/prior
        # record id are unconstrained free strings (an exact canonical id,
        # Clark's own words, his own retrieval language, and an earlier
        # record id can never be expressed as fixed enum members). The
        # target and prior-record grammars are enforced by
        # validate_pass1_conversation_act(), not by the generation grammar.
        "consolidation_request": {"type": "string", "enum": sorted(VALID_CONSOLIDATION_REQUESTS)},
        "consolidation_target": {"type": "string"},
        "consolidation_interpretation": {"type": "string"},
        "consolidation_cues": {"type": "string"},
        "consolidation_prior_record_id": {"type": "string"},
    },
    "required": sorted(RAW_ACT_REQUIRED_FIELDS),
    "additionalProperties": False,
}

PASS1_SCHEMA_CARET_OCCASION = json.loads(json.dumps(PASS1_SCHEMA))
PASS1_SCHEMA_CARET_OCCASION["properties"]["caret_reply_request"] = {
    "type": "string", "enum": sorted(VALID_CARET_REPLY_REQUESTS)}

# An occasion from a correspondent who is not an ANAXI principal additionally offers the standing choice.
PASS1_SCHEMA_SOURCE_OCCASION = json.loads(json.dumps(PASS1_SCHEMA_CARET_OCCASION))
PASS1_SCHEMA_SOURCE_OCCASION["properties"]["correspondent_standing_request"] = {
    "type": "string", "enum": sorted(VALID_CORRESPONDENT_STANDING_REQUESTS)}


def pass1_schema_with_correspondence(*, correspondents_available=False, pending_sends_available=False):
    """PASS1_SCHEMA plus the correspondence fields offered on an owner turn only when something they
    can name exists (known sources; Clark's own undelivered sends).  An unavailable capability is
    absent from the grammar, never silently misrepresented as available."""
    if not correspondents_available and not pending_sends_available:
        return PASS1_SCHEMA
    schema = json.loads(json.dumps(PASS1_SCHEMA))
    if correspondents_available:
        # PROPERTY ORDER IS GRAMMAR (real-model finding, 2026-09-24): the provider compiles the schema into a
        # grammar that admits properties only in declared order.  Appended after discord_message_text, the
        # ref was written (in the menu's order: destination, ref, text) and the text could no longer follow
        # -- 4/4 real addressed sends failed validation.  Both fields sit between the destination and the
        # text, standing before the ref it names, exactly as the menu lists them.
        added = {"correspondent_standing_request": {
                     "type": "string", "enum": sorted(VALID_CORRESPONDENT_STANDING_REQUESTS)},
                 "discord_correspondent_ref": {"type": "string"}}
        ordered = {}
        for key, value in schema["properties"].items():
            if key == "discord_message_text":
                ordered.update(added)
            ordered[key] = value
        schema["properties"] = ordered
    if pending_sends_available:
        schema["properties"]["withdraw_pending_send_request"] = {"type": "string"}
    return schema

def pass1_schema_with_destination_choices(schema, destination_ids):
    """The per-turn Pass-1 schema with ``discord_destination_id`` closed to exactly the destinations the host
    offers THIS turn (plus "" for no destination).  The authorized-destination set is closed and host-known
    for the turn, so -- like every other closed choice here -- it is a generation-time grammar constraint:
    Clark chooses one of the offered ids and never has to reproduce, reconstruct or complete one, and no
    other id can be generated.  The menu text is unchanged (the same ids, each with its label); nothing is
    resolved, aliased or normalized, and discord_correspondence remains authoritative over whether a named
    destination is lawful at authoring time and at the send boundary.  With nothing offered the schema is
    returned unchanged (no Discord request is available on such a turn anyway)."""
    offered = sorted({d for d in (destination_ids or ()) if isinstance(d, str) and d})
    if not offered or "discord_destination_id" not in schema.get("properties", {}):
        return schema
    constrained = json.loads(json.dumps(schema))           # property order (= grammar order) preserved
    constrained["properties"]["discord_destination_id"] = {"type": "string", "enum": [""] + offered}
    return constrained


def pass1_schema_with_action_verification(schema):
    """Add the first-class read-only verification act to the ordinary per-turn
    grammar without changing the frozen base-schema calibration identity.

    EPISTEMIC RECOVERABILITY SANDBOX: the grammar itself now carries the
    act/parameter dependency the validator enforces, as two branches.  Every
    ordinary act keeps the unchanged per-turn object and has no verification
    parameters at all; verify_action is its own object whose kind and
    non-empty exact target are REQUIRED, with only the directional control
    fields beside them (the validator's read-only exclusivity).  Real-model
    finding: with one flat object CR1 chose verify_action and omitted both
    parameters on every history question, so the turn failed closed with no
    reply -- an invocation shape the interface allowed and the validator had
    to reject.  validate_pass1_conversation_act() stays fully authoritative."""
    ordinary = json.loads(json.dumps(schema))
    ordinary["properties"]["act"]["enum"] = sorted(set(ordinary["properties"]["act"]["enum"]) - {VERIFY_ACTION})
    destination_ids = sorted(
        d for d in schema["properties"].get("discord_destination_id", {}).get("enum", []) if d)
    target_grammar = {
        ACTION_VERIFICATION_KIND_NOTE_CREATION: {
            "type": "string", "minLength": 1, "maxLength": MAX_ACTION_VERIFICATION_TARGET_LENGTH - len("notes:")},
        # The only journal entry identity the workspace's journal append ever writes.
        ACTION_VERIFICATION_KIND_JOURNAL_APPEND: {"type": "string", "pattern": JOURNAL_ENTRY_ID_PATTERN},
        # A closed choice among the destination ids this turn's menu shows; free exact text only when
        # no destination is shown (a send to a since-revoked destination stays verifiable).
        ACTION_VERIFICATION_KIND_DISCORD_DESTINATION_SEND: (
            {"type": "string", "enum": destination_ids} if destination_ids else
            {"type": "string", "minLength": 1, "maxLength": MAX_ACTION_VERIFICATION_TARGET_LENGTH}),
        ACTION_VERIFICATION_KIND_DISCORD_TURN_SEND: {
            "type": "string", "minLength": 1, "maxLength": MAX_ACTION_VERIFICATION_TARGET_LENGTH - len("turn:")},
    }
    verify_branches = [{
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": [VERIFY_ACTION]},
            "thread": json.loads(json.dumps(schema["properties"]["thread"])),
            "direction_request": json.loads(json.dumps(schema["properties"]["direction_request"])),
            "relinquish_direction": json.loads(json.dumps(schema["properties"]["relinquish_direction"])),
            "action_verification_kind": {"type": "string", "enum": [kind]},
            "action_verification_target": target_grammar[kind],
        },
        "required": sorted(RAW_ACT_REQUIRED_FIELDS) + ["action_verification_kind", "action_verification_target"],
        "additionalProperties": False,
    } for kind in sorted(SELECTABLE_ACTION_VERIFICATION_KINDS)]
    # The top level stays the familiar flat object (the union of both branches' fields, every
    # existing consumer reads it unchanged); the anyOf is what generation must satisfy.
    flat = json.loads(json.dumps(schema))
    flat["properties"]["act"]["enum"] = sorted(set(flat["properties"]["act"]["enum"]) | {VERIFY_ACTION})
    flat["properties"]["action_verification_kind"] = {
        "type": "string", "enum": sorted(SELECTABLE_ACTION_VERIFICATION_KINDS)}
    flat["properties"]["action_verification_target"] = {"type": "string"}
    flat["anyOf"] = [ordinary] + verify_branches
    return flat


def pass1_schema_waymark_reads_offered(schema, offered_reads):
    """The per-turn grammar offering exactly the Waymark reads in ``offered_reads`` (a subset of list/inspect:
    the host's mechanical offer, llama_anaxi._waymark_read_offer).  With none, the read act itself is gone --
    never only its requests, so no begun act=waymark can be completed as anything else.  act=change_waymark and
    every other choice remain exactly as they were."""
    offered = sorted(set(offered_reads) & WAYMARK_READ_REQUESTS)
    if not offered:
        return _without_acts(schema, {WAYMARK})
    out = json.loads(json.dumps(schema))
    for branch in out.get("anyOf") or []:
        if branch["properties"]["act"].get("enum") == [WAYMARK]:
            branch["properties"]["waymark_request"]["enum"] = offered
    if "waymark_request" in out["properties"]:
        out["properties"]["waymark_request"]["enum"] = sorted(
            {r for b in out.get("anyOf") or [out] for r in b["properties"].get("waymark_request", {}).get("enum", ())})
    return out


def pass1_schema_waymark_reads_exhausted(schema):
    """The per-turn grammar once this occasion's Waymark reads are spent: the read act is no longer offered;
    act=change_waymark and every other choice remain."""
    return pass1_schema_waymark_reads_offered(schema, ())


def waymark_read_payload_schema(request, *, list_continuations=(), inspect_choices=(), list_specs=None, inspect_facts=None):
    """The one-field grammar a Waymark READ spec is written in -- closed to what this occasion lawfully offers:
    list: view=live | view=all | a next-page spec a shelf page of this occasion issued;
    inspect: a handle spec a shelf page of this occasion showed (and any projection continuation an inspect
    result of this occasion issued), or -- before any shelf page -- the exact handle form alone.
    ``list_specs`` (when given) is the exact list choice set the host offers now: a read whose evidence this
    occasion already delivered, unchanged, is not among them (llama_anaxi._waymark_read_offer).
    No other capability's cursor, token or selection can be written in it."""
    if request == "list":
        offered = (sorted(set(list_specs)) if list_specs is not None
                   else ["view=all", "view=live"] + sorted(set(list_continuations)))
        value = {"type": "string", "enum": offered}
    elif inspect_choices:
        value = {"type": "string", "enum": sorted(set(inspect_choices))}
    else:
        value = {"type": "string", "pattern": WAYMARK_HANDLE_PATTERN}
    return {"type": "object", "properties": {"waymark_spec": value}, "required": ["waymark_spec"],
            "additionalProperties": False}


def render_waymark_read_request(act, *, list_continuations=(), inspect_choices=(), list_specs=None, inspect_facts=None):
    """The host's binding request for a Waymark read.  Its exact choices are the read grammar's (closed to
    what Waymark results of this occasion issued); the text only says where they come from, so it stays
    bounded however many there are.  Before any shelf page there is no continuation, so none is mentioned."""
    request = act.get("waymark_request")
    head = (f"{PASS1_BINDING_REQUEST_MARK} You chose waymark_request={request} (a read-only check: it reads and "
            "changes nothing, and its result returns to you before you answer).")
    if request == "list":
        choices = WAYMARK_SPEC_SYNTAX["list"] + (
            ", or exactly the next-page spec a Waymark shelf page of this occasion stated" if list_continuations
            else "") + (" -- a view or page already shown unchanged in this occasion is not offered again"
                        if list_specs is not None and len(set(list_specs)) < 2 + len(set(list_continuations)) else "")
    elif inspect_choices and inspect_facts:
        # Live acceptance 8 (H 01M4HPS3KQJVRFB2X964V5XWH6): the handle is written in THIS call; the current facts of
        # each offered Waymark are stated beside it here (host facts only -- every offered choice, none chosen).
        listed = "\n".join(f"{spec} ({inspect_facts[spec]})" for spec in sorted(inspect_choices, key=lambda s: (
            ";" in s, inspect_facts.get(s, ""), s)))
        choices = ("one handle exactly as this occasion's Waymark shelf showed it, or exactly a continuation an "
                   "inspect result of this occasion stated. The offered choices, with the current canonical facts "
                   "of each:\n" + listed + "\nWrite one of them")
    elif inspect_choices:
        choices = ("handle=<one handle exactly as a Waymark shelf page of this occasion showed it>, or exactly a "
                   "continuation an inspect result of this occasion stated")
    else:
        choices = WAYMARK_SPEC_SYNTAX["inspect"] + " (no shelf page has been shown in this occasion; a list shows them)"
    return f"{head} Now write the exact spec (waymark_spec): {choices}. Answer with that JSON only."


def pass1_schema_with_waymark(schema):
    """Add the two exclusive Waymark branches without enlarging ordinary acts: act=waymark reads (list/inspect,
    read-only checks) and act=change_waymark changes (the occasion's final act).  Each act carries only its own
    requests, so the read/change choice is made at the act itself (CHANGE_WAYMARK)."""
    out = json.loads(json.dumps(schema))
    branches = out.get("anyOf") or [json.loads(json.dumps(out))]
    added = []
    for act_name, requests in ((WAYMARK, WAYMARK_READ_REQUESTS), (CHANGE_WAYMARK, WAYMARK_MUTATION_REQUESTS)):
        controls = {
            key: json.loads(json.dumps(out["properties"][key]))
            for key in ("act", "thread", "direction_request", "relinquish_direction")
        }
        controls["act"] = {"type": "string", "enum": [act_name]}
        controls["waymark_request"] = {"type": "string", "enum": sorted(requests)}
        controls["waymark_spec"] = {"type": "string"}
        added.append({
            "type": "object", "properties": controls,
            "required": sorted(RAW_ACT_REQUIRED_FIELDS) + ["waymark_request", "waymark_spec"],
            "additionalProperties": False,
        })
    out["properties"]["act"]["enum"] = sorted(set(out["properties"]["act"]["enum"]) | {WAYMARK, CHANGE_WAYMARK})
    out["properties"]["waymark_request"] = {"type": "string", "enum": sorted(WAYMARK_REQUESTS)}
    out["properties"]["waymark_spec"] = {"type": "string"}
    out["anyOf"] = branches + added
    return out


def pass1_schema_with_historical_continuity(schema, read_cursors=(), *,
                                            browse_begin=True, browse_cursors=()):
    """Add chronological browse, explicit search, and cursor-bound inspection.

    Search accepts Clark's own free-text query.  Read cursors are a closed enum
    populated only from results already returned in this occasion, so the model
    never supplies a path, database id, locator, or guessed offset.
    """
    out = json.loads(json.dumps(schema))
    branches = out.get("anyOf") or [json.loads(json.dumps(out))]
    controls = {
        key: json.loads(json.dumps(out["properties"][key]))
        for key in ("act", "thread", "direction_request", "relinquish_direction")
    }
    search_properties = dict(controls)
    search_properties["act"] = {"type": "string", "enum": [SEARCH_HISTORICAL_CONTINUITY]}
    # The production provider's bundled llama.cpp renders a nested maxLength=2000
    # as ``char{1,2000}``, which its own GBNF parser rejects at sampler
    # initialization as an over-complex repetition.  Keep non-emptiness in
    # the generation grammar, and keep the exact 2,000-character ceiling in
    # validate_pass1_conversation_act(), which remains authoritative.  (Under
    # compact Pass-1 control the query is a payload: pass1_control_schema
    # removes it from the grammar the provider receives.)
    search_properties["historical_query"] = {
        "type": "string", "minLength": 1}
    history_branches = [{
        "type": "object", "properties": search_properties,
        "required": sorted(RAW_ACT_REQUIRED_FIELDS) + ["historical_query"],
        "additionalProperties": False,
    }]
    if browse_begin:
        browse_properties = dict(controls)
        browse_properties["act"] = {"type": "string", "enum": [BROWSE_HISTORICAL_CONTINUITY]}
        history_branches.append({
            "type": "object", "properties": browse_properties,
            "required": sorted(RAW_ACT_REQUIRED_FIELDS),
            "additionalProperties": False,
        })
    offered_browse_cursors = sorted({
        c for c in browse_cursors if isinstance(c, str) and c
    })
    if offered_browse_cursors:
        browse_next_properties = dict(controls)
        browse_next_properties["act"] = {"type": "string", "enum": [BROWSE_HISTORICAL_CONTINUITY]}
        browse_next_properties["historical_browse_cursor"] = {
            "type": "string", "enum": offered_browse_cursors,
        }
        history_branches.append({
            "type": "object", "properties": browse_next_properties,
            "required": sorted(RAW_ACT_REQUIRED_FIELDS) + ["historical_browse_cursor"],
            "additionalProperties": False,
        })
    offered_cursors = sorted({c for c in read_cursors if isinstance(c, str) and c})
    if offered_cursors:
        read_properties = dict(controls)
        read_properties["act"] = {"type": "string", "enum": [READ_HISTORICAL_ITEM]}
        read_properties["historical_cursor"] = {"type": "string", "enum": offered_cursors}
        history_branches.append({
            "type": "object", "properties": read_properties,
            "required": sorted(RAW_ACT_REQUIRED_FIELDS) + ["historical_cursor"],
            "additionalProperties": False,
        })
    out["properties"]["act"]["enum"] = sorted(
        set(out["properties"]["act"]["enum"])
        | {SEARCH_HISTORICAL_CONTINUITY}
        | ({BROWSE_HISTORICAL_CONTINUITY} if browse_begin or offered_browse_cursors else set())
        | ({READ_HISTORICAL_ITEM} if offered_cursors else set()))
    out["properties"]["historical_query"] = {"type": "string"}
    if offered_cursors:
        out["properties"]["historical_cursor"] = {"type": "string", "enum": offered_cursors}
    if offered_browse_cursors:
        out["properties"]["historical_browse_cursor"] = {
            "type": "string", "enum": offered_browse_cursors,
        }
    out["anyOf"] = branches + history_branches
    return out


# ------------------------------------------------- bounded occasion recovery (PULSE V0)
#
# A single occasion (a human turn, a Pulse occasion, or an authorized event) may carry a bounded
# recovery trajectory: at most MAX_READ_ONLY_CHECKS_PER_OCCASION read-only canonical checks, each
# followed by one re-entry inference with the exact result beside the turn.  These helpers only
# narrow the GRAMMAR the host offers once the bound is reached; they never decide which act fits.
MAX_READ_ONLY_CHECKS_PER_OCCASION = 2

# Fields that originate an outward act or a standing change.  A receipt re-entry (the one inference
# that interprets an outward act's canonical receipt) offers none of them: it may speak or stay
# silent, never start another outward chain from the same occasion.
OUTWARD_REQUEST_FIELDS = (
    "background_activity_request", "sleep_timing_request", "boundary_inquiry_request",
    "operative_directive_request", "operative_directive_text", "external_info_request", "external_info_target",
    "discord_correspondence_request", "discord_destination_id", "discord_message_text",
    "correspondent_standing_request", "discord_correspondent_ref", "withdraw_pending_send_request",
    "caret_reply_request",
)


def _without_acts(schema, removed):
    """A copy of a (possibly two-branch) Pass-1 grammar in which the named acts cannot be generated."""
    removed = set(removed)
    out = json.loads(json.dumps(schema))
    out["properties"]["act"]["enum"] = sorted(set(out["properties"]["act"]["enum"]) - removed)
    branches = out.pop("anyOf", None)
    if branches is not None:
        kept = []
        for branch in branches:
            branch["properties"]["act"]["enum"] = sorted(set(branch["properties"]["act"]["enum"]) - removed)
            if branch["properties"]["act"]["enum"]:
                kept.append(branch)
        if len(kept) == 1:
            out = kept[0]            # the one remaining branch is the whole grammar
        else:
            out["anyOf"] = kept
    if VERIFY_ACTION in removed:
        out["properties"].pop("action_verification_kind", None)
        out["properties"].pop("action_verification_target", None)
    if SEARCH_HISTORICAL_CONTINUITY in removed:
        out["properties"].pop("historical_query", None)
    if READ_HISTORICAL_ITEM in removed:
        out["properties"].pop("historical_cursor", None)
    if BROWSE_HISTORICAL_CONTINUITY in removed:
        out["properties"].pop("historical_browse_cursor", None)
    if not {WAYMARK, CHANGE_WAYMARK} & set(out["properties"]["act"]["enum"]):
        out["properties"].pop("waymark_request", None)
        out["properties"].pop("waymark_spec", None)
    elif "waymark_request" in out["properties"]:
        out["properties"]["waymark_request"]["enum"] = sorted(
            {r for b in out.get("anyOf") or [out] for r in b["properties"].get("waymark_request", {}).get("enum", ())})
    return out


def pass1_schema_checks_exhausted(schema):
    """The per-turn grammar once this occasion's read-only checks are spent: verify_action and
    use_workspace (whose nested path is itself an inspection) are no longer offered.  Answering,
    asking, staying silent and every other typed choice the turn already offered remain."""
    return _without_acts(schema, {VERIFY_ACTION, USE_WORKSPACE, "inspect_workspace"})


def pass1_schema_history_exhausted(schema):
    """Remove only historical search/read after their separate bounded allowance."""
    return _without_acts(schema, HISTORICAL_CONTINUITY_ACTS)


def pass1_schema_receipt_reentry(schema):
    """The grammar of the one inference that interprets an outward act's canonical receipt: the
    ordinary conversational acts and reply_request only -- no check, no workspace act, no outward or
    standing request field (they are absent, not merely discouraged)."""
    out = _without_acts(schema, {VERIFY_ACTION, USE_WORKSPACE, "inspect_workspace", "mutate_workspace",
                                 SEARCH_HISTORICAL_CONTINUITY, READ_HISTORICAL_ITEM,
                                 BROWSE_HISTORICAL_CONTINUITY, WAYMARK, CHANGE_WAYMARK})
    for field in OUTWARD_REQUEST_FIELDS:
        out["properties"].pop(field, None)
    return out


OCCASION_CHECK_BOUND_REACHED = "OCCASION_CHECK_BOUND_REACHED"
RECEIPT_REENTRY_ACT_NOT_AVAILABLE = "RECEIPT_REENTRY_ACT_NOT_AVAILABLE"


def occasion_new_act_fields(validated_act):
    """The validated act's fields that would start an act (anything beyond speaking or staying silent):
    a check/workspace act or any outward/standing request.  Host enforcement of the occasion bound, so it
    holds even if a provider ever ignored the grammar."""
    started = []
    if validated_act.get("act") in (VERIFY_ACTION, USE_WORKSPACE, "inspect_workspace", "mutate_workspace",
                                     SEARCH_HISTORICAL_CONTINUITY, READ_HISTORICAL_ITEM, WAYMARK, CHANGE_WAYMARK):
        started.append("act")
    for field in OUTWARD_REQUEST_FIELDS:
        value = validated_act.get(field)
        if value not in (None, "", False, "none"):
            started.append(field)
    return started


def render_occasion_check_state(checks_used):
    """Mechanical bound state for a re-entry inference (no advice about what to do next)."""
    remaining = MAX_READ_ONLY_CHECKS_PER_OCCASION - checks_used
    if remaining > 0:
        return (f"Read-only checks used in this occasion: {checks_used} of {MAX_READ_ONLY_CHECKS_PER_OCCASION}. "
                f"{remaining} more may be chosen in this occasion.")
    return (f"Read-only checks used in this occasion: {checks_used} of {MAX_READ_ONLY_CHECKS_PER_OCCASION}. "
            "No further check is available in this occasion.")


def pass1_schema_acts(schema):
    """Every act a (possibly two-branch) Pass-1 schema can produce."""
    branches = schema.get("anyOf") or [schema]
    return sorted({act for b in branches for act in b["properties"]["act"]["enum"]})


# ------------------------------------------------ compact Pass-1 control / payload separation
#
# Post-deploy no-reply incident (H 01M4C66MAAT24RZ7B3CP4AKXF1, 2026-10-07): a Pass-1 re-entry act
# ran to its 512-token reserve and the turn failed closed with no X.  A single generation carried
# Clark's control choice, the handles it binds, and arbitrary model-authored text (a 4,000-character
# directive, a 2,000-character Discord message, a free topic label...), so no fixed allowance could
# truthfully hold every lawful act.  OFFLOAD MECHANICS; PRESERVE INTERPRETATION:
#
#   * The Pass-1 grammar the provider receives is the CONTROL schema: what Clark chooses to do --
#     the act, direction, reply and request choices -- as closed values only.
#   * What the chosen act then needs is bound afterwards, inside the same waking transaction, one
#     field at a time: SELECTIONS (the handle a chosen request names: destination, correspondent,
#     consolidation episode or prior record -- each a closed choice of its own) and PAYLOADS (free text
#     Clark writes: topic label, query, directive words, message words, target, interpretation, cues).
#   * The assembled act has the unchanged raw-act shape and validate_pass1_conversation_act() stays
#     the one authoritative law over it.  Selection alone executes nothing; any binding that does not
#     complete fails the whole act closed.
#
# The host decides only WHICH fields the chosen act structurally requires (a mirror of the validator's
# own presence rules), never what they say.  Final capacities are the existing validator ceilings.
PASS1_PAYLOAD_FIELD_MAX_CHARS = {
    "thread": MAX_THREAD_LENGTH,
    "historical_query": MAX_HISTORICAL_QUERY_LENGTH,
    "action_verification_target": MAX_ACTION_VERIFICATION_TARGET_LENGTH,
    "operative_directive_text": MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH,
    "external_info_target": MAX_EXTERNAL_INFO_TARGET_LENGTH,
    "discord_message_text": MAX_DISCORD_MESSAGE_TEXT_LENGTH,
    "consolidation_interpretation": MAX_CONSOLIDATION_INTERPRETATION_LENGTH,
    "consolidation_cues": MAX_CONSOLIDATION_CUES_LENGTH,
    # Bound in its own atomic call, then persisted as a canonical action.
    # It is intentionally absent from _ECHO_RENDERINGS: later prompts do not
    # need a duplicate of this mechanical syntax.
    "waymark_spec": MAX_WAYMARK_SPEC_LENGTH,
    # The post-reply-choice workspace claim declaration (a Pass-1-family structured call): its exact
    # item name is a payload of the chosen collection.
    WORKSPACE_CLAIM_ITEM_FIELD: WORKSPACE_CLAIM_SCHEMA_ITEM_MAX,
}
# Payloads in this set are requested only after the corresponding active
# choice has already been made, and the authoritative validator requires
# substantive text for that choice.  The compact-control split authors these
# fields in a separate call, so that call must preserve the same nonblank law
# instead of silently accepting an empty string that the assembled validator
# will reject later.
PASS1_PAYLOAD_NONBLANK_FIELDS = frozenset({
    "historical_query",
    "action_verification_target",
    "operative_directive_text",
    "external_info_target",
    "discord_message_text",
    "consolidation_interpretation",
    WORKSPACE_CLAIM_ITEM_FIELD,
})
PASS1_SELECTION_FIELDS = (
    "discord_destination_id", "discord_correspondent_ref",
    "consolidation_target", "consolidation_prior_record_id",
)
# Free strings of the per-turn schema that the control grammar closes over the handles the host offers
# this turn (Clark's own undelivered sends).
PASS1_HOST_CLOSED_FIELDS = ("withdraw_pending_send_request",)
# Binding order: every selection before every payload, so a payload request can state the handle the
# words are bound to.
PASS1_BINDING_ORDER = PASS1_SELECTION_FIELDS + tuple(PASS1_PAYLOAD_FIELD_MAX_CHARS)

# Acts whose host-side handling reads the topic label: shift_topic/ask_human (direction law's
# apply_conversation_act) and the workspace acts (the supervisor's topic hint).  Every other act's
# label was never read by direction state, so it is not requested; it is "".
THREAD_PAYLOAD_ACTS = frozenset({SHIFT_TOPIC, ASK_HUMAN, USE_WORKSPACE, INSPECT_WORKSPACE, MUTATE_WORKSPACE})

_CROCKFORD_CLASS = "[0123456789ABCDEFGHJKMNPQRSTVWXYZ]"
CONSOLIDATION_TARGET_PATTERN = "^canonical\\." + _CROCKFORD_CLASS + "{26}$"
CONSOLIDATION_RECORD_PATTERN = "^consolidation\\." + _CROCKFORD_CLASS + "{26}$"
# Exact lengths of the fixed-format strings a control or selection grammar admits, keyed by pattern.
PASS1_CONTROL_PATTERN_LENGTHS = {
    JOURNAL_ENTRY_ID_PATTERN: len("entry-") + 16,
    CONSOLIDATION_TARGET_PATTERN: len(_CONSOLIDATION_TARGET_PREFIX) + 26,
    CONSOLIDATION_RECORD_PATTERN: len(_CONSOLIDATION_RECORD_PREFIX) + 26,
}


def _closed_string(prop):
    return "enum" in prop or prop.get("pattern") in PASS1_CONTROL_PATTERN_LENGTHS


def pass1_selection_grammars(schema, *, correspondent_refs=()):
    """The closed grammar of each selection field a per-turn schema offers: the destinations it closes
    over (only "" when none is offered), the correspondents this turn knows, and the canonical
    consolidation ids in their exact fixed format."""
    props = {k: p for b in [schema] + list(schema.get("anyOf") or []) for k, p in b["properties"].items()}
    grammars = {}
    if "discord_destination_id" in props:
        offered = props["discord_destination_id"].get("enum") or [""]
        grammars["discord_destination_id"] = {"type": "string", "enum": list(offered)}
    if "discord_correspondent_ref" in props:
        grammars["discord_correspondent_ref"] = {
            "type": "string", "enum": [""] + sorted({r for r in correspondent_refs if r})}
    if "consolidation_target" in props:
        grammars["consolidation_target"] = {"type": "string", "pattern": CONSOLIDATION_TARGET_PATTERN}
    if "consolidation_prior_record_id" in props:
        grammars["consolidation_prior_record_id"] = {"type": "string", "pattern": CONSOLIDATION_RECORD_PATTERN}
    return grammars


def pass1_control_schema(schema, *, pending_send_refs=()):
    """The provider-facing Pass-1 CONTROL grammar for a per-turn schema: free text fields and the
    handle fields leave it (bound afterwards); the withdrawn boundary inquiry admits only its inert
    value (the host treats every value as none; the property keeps its released grammar position);
    Clark's own pending sends are offered as the closed choice they are.  Property order unchanged."""
    out = json.loads(json.dumps(schema))

    def compact(obj):
        props = obj.get("properties", {})
        for key in list(props):
            if key in PASS1_SELECTION_FIELDS or (
                    key in PASS1_PAYLOAD_FIELD_MAX_CHARS and not _closed_string(props[key])):
                del props[key]
        if "boundary_inquiry_request" in props and not BOUNDARY_INQUIRY_IN_RELEASE:
            props["boundary_inquiry_request"] = {"type": "string", "enum": [BOUNDARY_INQUIRY_REQUEST_NONE]}
        for key in PASS1_HOST_CLOSED_FIELDS:      # Clark's own undelivered sends, offered as they are
            if key in props:
                props[key] = {"type": "string", "enum": [""] + sorted({r for r in pending_send_refs if r})}
        if "required" in obj:
            obj["required"] = [k for k in obj["required"] if k in props]

    compact(out)
    for branch in out.get("anyOf") or []:
        compact(branch)
    return out


def _control_branch_value(key, prop):
    if "enum" in prop:
        return max(prop["enum"], key=lambda v: len(json.dumps(v, ensure_ascii=False).encode("utf-8")))
    if prop.get("type") == "boolean":
        return False
    if prop.get("type") == "string" and prop.get("pattern") in PASS1_CONTROL_PATTERN_LENGTHS:
        return "x" * PASS1_CONTROL_PATTERN_LENGTHS[prop["pattern"]]
    raise ValueError(f"control field {key!r} is not a closed choice: {prop!r}")


def pass1_control_max_bytes(control_schema):
    """The exact UTF-8 byte length of the longest object a closed grammar admits, serialized as the
    model writes it (json.dumps default separators: one space after ':' and ','), every property
    present.  Each provider token decodes to at least one byte, so this bounds the tokens of ANY
    segmentation of that object.  Raises if a free string survived (an unaudited control field)."""
    longest = 0
    for branch in control_schema.get("anyOf") or [control_schema]:
        envelope = {k: _control_branch_value(k, p) for k, p in branch["properties"].items()}
        longest = max(longest, len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")))
    return longest


def pass1_selection_schema(field, grammar):
    return {"type": "object", "properties": {field: dict(grammar)}, "required": [field],
            "additionalProperties": False}


def pass1_binding_fields(act, control_schema, selection_grammars):
    """The selections and payloads the chosen control act structurally requires, in binding order --
    a mirror of validate_pass1_conversation_act's presence rules.  ``act`` holds the closed choices
    (plus any field already bound).  Pure."""
    if not isinstance(act, dict):
        return []
    needed = set()
    kind = act.get("act")
    if kind in THREAD_PAYLOAD_ACTS:
        needed.add("thread")
    if kind == SEARCH_HISTORICAL_CONTINUITY:
        needed.add("historical_query")
    if kind == VERIFY_ACTION:
        carried = any(
            b["properties"].get("action_verification_kind", {}).get("enum") == [act.get("action_verification_kind")]
            and "action_verification_target" in b["properties"]
            for b in control_schema.get("anyOf") or [control_schema])
        if not carried:
            needed.add("action_verification_target")
    if act.get("operative_directive_request") == OPERATIVE_DIRECTIVE_REQUEST_SET:
        needed.add("operative_directive_text")
    if act.get("external_info_request", EXTERNAL_INFO_REQUEST_NONE) != EXTERNAL_INFO_REQUEST_NONE:
        needed.add("external_info_target")
    discord_request = act.get("discord_correspondence_request")
    if discord_request in DISCORD_PAYLOAD_REQUESTS:
        needed.update({"discord_destination_id", "discord_message_text"})
    if "discord_correspondent_ref" in selection_grammars and (
            discord_request in DISCORD_PAYLOAD_REQUESTS
            or act.get("correspondent_standing_request", CORRESPONDENT_STANDING_REQUEST_NONE)
            != CORRESPONDENT_STANDING_REQUEST_NONE):
        needed.add("discord_correspondent_ref")
    consolidation_request = act.get("consolidation_request", CONSOLIDATION_REQUEST_NONE)
    if consolidation_request != CONSOLIDATION_REQUEST_NONE:
        needed.update({"consolidation_target", "consolidation_cues"})
    if consolidation_request in (CONSOLIDATION_REQUEST_INTERPRET, CONSOLIDATION_REQUEST_REVISE):
        needed.add("consolidation_interpretation")
    if consolidation_request in (CONSOLIDATION_REQUEST_REVISE, CONSOLIDATION_REQUEST_DE_EMPHASIZE):
        needed.add("consolidation_prior_record_id")
    if kind in (WAYMARK, CHANGE_WAYMARK):
        needed.add("waymark_spec")
    return [f for f in PASS1_BINDING_ORDER if f in needed and (
        f not in PASS1_SELECTION_FIELDS or f in selection_grammars)]


_PASS1_BINDING_DESCRIPTIONS = {
    "discord_destination_id": "the destination of the Discord message you chose",
    "discord_correspondent_ref": "the correspondent it concerns (\"\" for none)",
    "consolidation_target": "the episode you chose to consolidate (canonical.<id>)",
    "consolidation_prior_record_id": "your earlier consolidation record it concerns (consolidation.<id>)",
    "thread": "the topic label for the act you chose (it may be empty)",
    "historical_query": "your own query for the historical-continuity search you chose",
    "action_verification_target": "the exact target of the verification you chose",
    "operative_directive_text": "the exact words of the standing directive you chose to set",
    "external_info_target": "the exact query, URL or result:<choice> for your external information request",
    "discord_message_text": "your exact words for the Discord message you chose",
    "consolidation_interpretation": "your own interpretation for the consolidation you chose",
    "consolidation_cues": "your own retrieval cues for it, one per line (it may be empty)",
    "waymark_spec": ("the exact mechanical Waymark spec as semicolon-separated key=value pairs "
                     "for the Waymark request you chose"),
    WORKSPACE_CLAIM_ITEM_FIELD: "the exact name of the item in the collection you declared",
}
# The closed choices each request states, so Clark sees what the answer is bound to (bound ids other than
# the destination are not restated: the request stays within PAYLOAD_REQUEST_MAX_TOKENS).
_PASS1_BINDING_CONTEXT = {
    "discord_destination_id": ("discord_correspondence_request",),
    "discord_correspondent_ref": ("discord_correspondence_request", "correspondent_standing_request"),
    "consolidation_target": ("consolidation_request",),
    "consolidation_prior_record_id": ("consolidation_request",),
    "thread": ("act",),
    "historical_query": ("act",),
    "action_verification_target": ("act", "action_verification_kind"),
    "operative_directive_text": ("operative_directive_request",),
    "external_info_target": ("external_info_request",),
    "discord_message_text": ("discord_correspondence_request", "discord_destination_id"),
    "consolidation_interpretation": ("consolidation_request",),
    "consolidation_cues": ("consolidation_request",),
    "waymark_spec": ("waymark_request",),
    WORKSPACE_CLAIM_ITEM_FIELD: (WORKSPACE_CLAIM_FIELD,),
}
PASS1_BINDING_REQUEST_MARK = "Host request (mechanical; not a human message)."


# The refused spec as the correction request shows it back: JSON-quoted (one line, lossless), at most this
# many characters (a spec beyond the 512-character ceiling is itself the refusal) -- <= 6 bytes each.
WAYMARK_CORRECTION_SHOWN_CHARS = MAX_WAYMARK_SPEC_LENGTH
WAYMARK_CORRECTION_REASON_BYTES = 320


def render_waymark_correction_request(act, refused_spec, code, reason):
    """The host's one same-turn correction request for an already-chosen Waymark request whose authored spec
    the host refused for a correctable reason: the exact refusal, the exact refused words, and the same
    syntax statement as the first request.  Nothing else is offered: the request cannot change."""
    request = act.get("waymark_request")
    shown = refused_spec if len(refused_spec) <= WAYMARK_CORRECTION_SHOWN_CHARS else (
        refused_spec[:WAYMARK_CORRECTION_SHOWN_CHARS])
    more = len(refused_spec) - len(shown)
    reason = str(reason).encode("utf-8")[:WAYMARK_CORRECTION_REASON_BYTES].decode("utf-8", "ignore")
    return (f"{PASS1_BINDING_REQUEST_MARK} The host REFUSED the Waymark spec you wrote for waymark_request={request} "
            f"(refusal {code}: {reason}). You wrote: {json.dumps(shown, ensure_ascii=False)}"
            + (f" [and {more} more characters]" if more else "")
            + f". This is the one correction this turn allows. Write the corrected exact spec (waymark_spec) "
            f"{WAYMARK_SPEC_FORM}: {WAYMARK_SPEC_SYNTAX.get(request, '')}. Answer with that JSON only; "
            f"at most {MAX_WAYMARK_SPEC_LENGTH} characters.")


def render_pass1_binding_request(field, act):
    """The host's mechanical request for one selection or payload of an already-chosen act.  States
    the bound choices and the exact capacity; says nothing about what the answer should be."""
    chosen = ", ".join(f"{k}={act[k]}" for k in _PASS1_BINDING_CONTEXT[field]
                       if isinstance(act.get(k), str) and act.get(k) not in ("", "none"))
    head = PASS1_BINDING_REQUEST_MARK + (f" You chose {chosen}." if chosen else "")
    if field in PASS1_SELECTION_FIELDS:
        return f"{head} Now choose {_PASS1_BINDING_DESCRIPTIONS[field]} ({field}): answer with that JSON only."
    if field == "waymark_spec" and act.get("waymark_request") in WAYMARK_SPEC_SYNTAX:
        return (f"{head} Now write the exact spec ({field}) {WAYMARK_SPEC_FORM}: "
                f"{WAYMARK_SPEC_SYNTAX[act['waymark_request']]}. Answer with that JSON only; at most "
                f"{PASS1_PAYLOAD_FIELD_MAX_CHARS[field]} characters.")
    # The character ceiling is a final validation ceiling (owner decision 2026-10-08): stated, never tuned
    # for; a completed body beyond it goes to the unchanged validator law.
    return (f"{head} Now write {_PASS1_BINDING_DESCRIPTIONS[field]} ({field}): answer with that JSON only; "
            f"its value is exactly the words, at most {PASS1_PAYLOAD_FIELD_MAX_CHARS[field]} characters.")


# The payload string grammar: the provider's own JSON string rule (raw characters other than '"', '\',
# C0 controls and DEL; the short escapes; \uXXXX) with only the surrogate escapes D800-DFFF removed.  Every
# Unicode scalar value stays writable -- astral characters raw, every BMP character raw or escaped -- and
# only surrogate code points, which no ANAXI store can carry (UTF-8 refuses them), are excluded.  That
# bounds one character at 6 bytes as written (a surrogate pair took 12).  The pattern is matched against
# the JSON source between the quotes (installed llama.cpp converter; test_provider_grammar_parity pins the
# rendered grammar).
PASS1_PAYLOAD_STRING_PATTERN = (
    r'^([^"\\\x00-\x1F\x7F]|\\(["\\/bfnrt]|u([0-9a-cA-CeEfF][0-9a-fA-F]{3}|[dD][0-7][0-9a-fA-F]{2})))*$')
PASS1_PAYLOAD_MAX_BYTES_PER_CHAR = 6
# The rendered payload grammar: root ::= "{" space kv space "}"; kv ::= key space ":" space string --
# four optional whitespace runs, each at most "\n"{1,2} [ \t]{0,20} = 22 bytes.
PASS1_PAYLOAD_GRAMMAR_SPACES = 4
PASS1_PAYLOAD_GRAMMAR_SPACE_MAX_BYTES = 22


def pass1_payload_schema(field):
    """The one-field grammar a payload is written in: exactly the field the chosen act needs (the framing
    the one-call act carried), so the words cannot be wrapped, labelled or joined by other answers.  The
    frame is fixed by construction; Clark writes only the string."""
    return {"type": "object",
            "properties": {field: {"type": "string", "pattern": PASS1_PAYLOAD_STRING_PATTERN}},
            "required": [field], "additionalProperties": False}


def pass1_payload_max_output_tokens(field):
    """The most tokens ANY lawful payload of ``field`` can take under its grammar: its character ceiling
    at 6 bytes each, the minimal frame, the grammar's optional whitespace and the end token (every
    grammar-constrained token decodes to >= 1 byte).  A mechanical bound, not an expectation."""
    frame = len(json.dumps({field: ""}, separators=(",", ":")).encode("utf-8"))
    return (PASS1_PAYLOAD_FIELD_MAX_CHARS[field] * PASS1_PAYLOAD_MAX_BYTES_PER_CHAR + frame
            + PASS1_PAYLOAD_GRAMMAR_SPACES * PASS1_PAYLOAD_GRAMMAR_SPACE_MAX_BYTES + 1)


def pass1_payload_boundary_failure(field, value):
    """Return the compact-payload/assembled-validator boundary failure.

    ``maxLength`` cannot be carried in these payload schemas: the production
    llama.cpp JSON-schema converter expands the largest lawful fields into a
    repetition its GBNF parser refuses at sampler initialization.  The final
    validator therefore remains authoritative, but the authoring seam must
    detect the same mechanical character/nonblank boundary before handing the
    act onward so Clark can correct his own words once.  No text is trimmed,
    normalized, summarized, or replaced by the host.
    """
    if field not in PASS1_PAYLOAD_FIELD_MAX_CHARS or not isinstance(value, str):
        return "PAYLOAD_MALFORMED"
    if len(value) > PASS1_PAYLOAD_FIELD_MAX_CHARS[field]:
        return "PAYLOAD_CHARACTER_LIMIT_EXCEEDED"
    if field in PASS1_PAYLOAD_NONBLANK_FIELDS and not value.strip():
        return "PAYLOAD_NONBLANK_REQUIRED"
    return None


def render_pass1_payload_correction_request(field, failure_code):
    """One bounded, mechanical re-authoring request for a refused payload.

    The rejected words are deliberately not echoed: every correction call is
    statelessly grounded by the same full admitted turn/check context, while
    echoing a worst-tokenization 4,000-character refusal beside another full
    4,000-character attempt would violate the 46,080-token serving contract.
    Clark authors the replacement; the host supplies only the field, refusal,
    and unchanged capacity.
    """
    return (
        f"{PASS1_BINDING_REQUEST_MARK} REFUSED {field}: {failure_code}; nothing was used. "
        f"Write it again as JSON, at most {PASS1_PAYLOAD_FIELD_MAX_CHARS[field]} characters"
        + (" and nonblank" if field in PASS1_PAYLOAD_NONBLANK_FIELDS else "")
        + ". JSON only; one correction."
    )


PASS2_TASK_INSTRUCTION = (
    # META-RESPONSE LEAK repair (production incident, 2026-09-22): a real live turn (an
    # ordinary status remark, not a question) was answered with "A gentle, understanding
    # acknowledgment of the current state of waiting, coupled with a reflective turn toward the
    # nature of waiting itself..." -- a description/plan of a reply, delivered as the reply. The
    # schema's bare "expression" field name and this instruction's old opening line ("Express the
    # act and control state you already committed to, naturally") never told the model that field
    # IS the literal spoken text, not a natural-language account OF it -- so "expressing the act
    # and control state" was satisfied, mechanically, by describing them instead of speaking. This
    # replacement is deliberately kept close to the old text's own length (a byte-for-byte change
    # to this frozen, fingerprint-calibrated scaffold already invalidates the calibration and
    # falls back to the ordinary byte estimator regardless of size -- see _PASS2_SCAFFOLD_
    # CALIBRATION's own comment in llama_anaxi.py -- so there is no reason to spend more bytes
    # than the fix needs, and every byte saved here is real Pass-2 prompt headroom back).
    "Write the literal words you are saying to the person right now, in your own voice -- "
    "your actual reply, never a description or plan of one. "
    "Do not imply that directional ownership changed if the "
    "host state below says it did not.\n\n"
    # OWC5-P3: the validated act controls conversational direction; it
    # does not erase other response-seeking material in the same human
    # turn (spec section 4's frozen invariant: conversational act !=
    # complete semantic content of the reply). Deliberately act-
    # independent -- the same guidance applies whether the act is
    # pause_thread, ask_human, shift_topic, yield_direction, or
    # develop_current (spec section 7), so it lives once here rather
    # than duplicated per act. Explicitly assigns the actual answer to
    # Clark, never the host (spec section 10) -- the host adds no
    # semantic content, only this instruction to consider addressing it.
    "The current human message may also contain a distinct, actionable, "
    "response-seeking item -- a question, request, or offered choice -- "
    "separate from the act you selected. When that item is compatible "
    "with your act, address it too, in your own words; you decide the "
    "actual answer, never the host. Do not treat rhetorical, quoted, "
    "hypothetical, or already-answered material as requiring a literal "
    "answer."
)

PASS2_ALLOWED_FIELDS = {"expression"}

# SHARED ORDINARY EXPRESSION SEAM (whole-system completion, 2026-09-23).
#
# Ordinary waking Pass 2 (Mac turns and Caret occasions alike) and the workspace narration pass
# are ordinary chat completions: the model's assistant message IS Clark's reply, verbatim apart
# from surrounding whitespace.  There is no grammar/JSON container and no model-typed marker.
#
# Root cause established for the 2026-09-22 production defects (exact echo of the human's words;
# "A light, appreciative chuckle, followed by ..." response-plan/meta-description delivered as the
# reply): the grammar-constrained ``{"expression": "<string>"}`` object that 434ba82 introduced.
# A bounded A/B on production-shaped requests (real run_waking_turn composition, the production
# identity preamble, 4-6 real prior exchanges as the dialogue window, 14 real/realistic human
# messages, real local gemma4:e4b, x2 runs) replayed the IDENTICAL request under (V0) that schema
# and (V1) a plain chat completion: V0 answered almost every turn with a description of a
# demeanor ("Curiosity mixed with gentle anticipation", "Amused curiosity, gentle warmth") --
# the field NAME "expression" is read as a facial/emotional expression -- while V1 answered every
# turn with literal speech.  The earlier voluntary-marker handshakes this schema replaced
# (<ANAXI_RESPONSE_COMPLETE>, then also <ANAXI_EXPRESSION>) failed for the complementary reason:
# they asked the model to type protocol strings it frequently omitted.
#
# Completion is established mechanically, not by anything the model writes: call_llama() requires
# the provider's own done/done_reason=stop/eval_count within the explicit decode reserve
# (context_budget.require_complete_response); a length stop or incomplete transport fails before
# any validation or persistence.  Without a grammar there is no host mechanism that can close an
# unfinished generation for the model, which was the only reason the 2026-09-18 marker existed
# (WAKING_OUTPUT_TRUNCATION_REPAIR.md: the upstream cause was NOT ESTABLISHED and only the grammar
# could have concealed it).  A blank completion is EMPTY_EXPRESSION: a mechanical
# expression-not-established failure (retry-safe, never Clark's silence).  Clark's authored
# silence exists only as his explicit typed Pass-1 choice (REPLY_REQUEST_NO_REPLY), never as the
# absence of text.
#
# The two retired protocol strings stay defined as sentinels: outward_expression refuses any body
# that contains them (only an extraction bug could hand one through), and historical failure
# records (INCOMPLETE_EXPRESSION_BOUNDARY / MISSING_EXPRESSION_ENVELOPE) remain readable.
PASS2_COMPLETION_MARKER = "<ANAXI_RESPONSE_COMPLETE>"
PASS2_EXPRESSION_MARKER = "<ANAXI_EXPRESSION>"


# ------------------------------------------------------- failure vocabulary


class ConversationDirectionFailure(Exception):
    """OWC5-S2: the narrowest existing ordinary-waking error surface
    for a Pass-1/Pass-2 structural failure in conversation mode --
    raised by run_waking_turn() and left uncaught, propagating to its
    caller exactly like an uncaught real-generation exception already
    would in task mode. Never triggers a retry or an ordinary-
    generation fallback; the caller decides what to do with it."""

    def __init__(self, stage, failure_code):
        self.stage = stage  # "pass1" or "pass2"
        self.failure_code = failure_code
        super().__init__(f"conversation direction failed at {stage}: {failure_code}")


class DirectionFailure:
    MALFORMED_ACT = "MALFORMED_ACT"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    INVALID_ACT = "INVALID_ACT"
    INVALID_THREAD = "INVALID_THREAD"
    INVALID_DIRECTION_REQUEST = "INVALID_DIRECTION_REQUEST"
    INVALID_RELINQUISH_TYPE = "INVALID_RELINQUISH_TYPE"
    INVALID_BACKGROUND_ACTIVITY_REQUEST = "INVALID_BACKGROUND_ACTIVITY_REQUEST"
    INVALID_SLEEP_TIMING_REQUEST = "INVALID_SLEEP_TIMING_REQUEST"
    INVALID_BOUNDARY_INQUIRY_REQUEST = "INVALID_BOUNDARY_INQUIRY_REQUEST"
    INVALID_OPERATIVE_DIRECTIVE_REQUEST = "INVALID_OPERATIVE_DIRECTIVE_REQUEST"
    INVALID_OPERATIVE_DIRECTIVE_TEXT = "INVALID_OPERATIVE_DIRECTIVE_TEXT"
    INVALID_EXTERNAL_INFO_REQUEST = "INVALID_EXTERNAL_INFO_REQUEST"
    INVALID_EXTERNAL_INFO_TARGET = "INVALID_EXTERNAL_INFO_TARGET"
    INVALID_ACTION_VERIFICATION_REQUEST = "INVALID_ACTION_VERIFICATION_REQUEST"
    INVALID_ACTION_VERIFICATION_TARGET = "INVALID_ACTION_VERIFICATION_TARGET"
    ACTION_VERIFICATION_CONFLICT = "ACTION_VERIFICATION_CONFLICT"
    INVALID_HISTORICAL_QUERY = "INVALID_HISTORICAL_QUERY"
    INVALID_HISTORICAL_CURSOR = "INVALID_HISTORICAL_CURSOR"
    HISTORICAL_CONTINUITY_CONFLICT = "HISTORICAL_CONTINUITY_CONFLICT"
    INVALID_WAYMARK_REQUEST = "INVALID_WAYMARK_REQUEST"
    INVALID_WAYMARK_SPEC = "INVALID_WAYMARK_SPEC"
    WAYMARK_CONFLICT = "WAYMARK_CONFLICT"
    INVALID_CONSOLIDATION_REQUEST = "INVALID_CONSOLIDATION_REQUEST"
    INVALID_CONSOLIDATION_TARGET = "INVALID_CONSOLIDATION_TARGET"
    INVALID_CONSOLIDATION_INTERPRETATION = "INVALID_CONSOLIDATION_INTERPRETATION"
    INVALID_CONSOLIDATION_CUES = "INVALID_CONSOLIDATION_CUES"
    INVALID_CONSOLIDATION_PRIOR_RECORD_ID = "INVALID_CONSOLIDATION_PRIOR_RECORD_ID"
    INVALID_DISCORD_CORRESPONDENCE_REQUEST = "INVALID_DISCORD_CORRESPONDENCE_REQUEST"
    INVALID_REPLY_REQUEST = "INVALID_REPLY_REQUEST"
    INVALID_CORRESPONDENT_REQUEST = "INVALID_CORRESPONDENT_REQUEST"
    INVALID_DISCORD_DESTINATION_ID = "INVALID_DISCORD_DESTINATION_ID"
    INVALID_DISCORD_MESSAGE_TEXT = "INVALID_DISCORD_MESSAGE_TEXT"
    DISCORD_CORRESPONDENCE_NOT_AUTHORIZED = "DISCORD_CORRESPONDENCE_NOT_AUTHORIZED"
    WORKSPACE_ACTION_NOT_AUTHORIZED = "WORKSPACE_ACTION_NOT_AUTHORIZED"
    WORKSPACE_ACTION_CONFLICT = "WORKSPACE_ACTION_CONFLICT"
    UNAUTHORIZED_RELINQUISH = "UNAUTHORIZED_RELINQUISH"
    UNAUTHORIZED_HUMAN_CONTROL = "UNAUTHORIZED_HUMAN_CONTROL"
    INVALID_DIRECTION_TARGET = "INVALID_DIRECTION_TARGET"
    MALFORMED_EXPRESSION = "MALFORMED_EXPRESSION"
    EMPTY_EXPRESSION = "EMPTY_EXPRESSION"
    INCOMPLETE_EXPRESSION_BOUNDARY = "INCOMPLETE_EXPRESSION_BOUNDARY"
    MISSING_EXPRESSION_ENVELOPE = "MISSING_EXPRESSION_ENVELOPE"


# ---------------------------------------------------------- working set ---


def empty_working_set():
    """A fresh session's working set. OWC7-S1 adds `direction_owner`,
    defaulting to `unknown` -- never inferred, never backfilled for
    historical sessions/traces that predate this field."""
    return {
        "active_thread": None,
        "active_thread_origin": None,
        "last_conversation_act": None,
        "open_threads": [],
        "direction_owner": DIRECTION_UNKNOWN,
    }


def apply_conversation_act(working_set, validated_act):
    """Pure. Deterministic host-side transition from Clark's typed act
    -- never from Pass-2 prose, never inferred. See spec section 9 for
    the exact per-act rules this implements.

    Deliberately does NOT touch direction_owner -- that is a fully
    separate, orthogonal transition (see resolve_clark_direction_
    control()/apply_human_direction_control() below), per OWC7-D1's
    "PATHWAYS, NOT ORGANS" separation. active_thread_origin (who
    proposed the current topic) remains independent from
    direction_owner (who owns the next choice) -- every combination of
    the two is mechanically legal."""
    act = validated_act["act"]
    thread = validated_act["thread"]

    new_ws = dict(working_set)
    new_ws["open_threads"] = list(working_set.get("open_threads") or [])
    new_ws["last_conversation_act"] = act

    if act == DEVELOP_CURRENT:
        pass  # active_thread/origin retained exactly as-is

    elif act == SHIFT_TOPIC:
        new_ws["active_thread"] = thread if thread else new_ws.get("active_thread")
        new_ws["active_thread_origin"] = ORIGIN_CLARK

    elif act == ASK_HUMAN:
        # active_thread retained UNLESS Clark explicitly supplies a new one
        if thread:
            new_ws["active_thread"] = thread
            new_ws["active_thread_origin"] = ORIGIN_CLARK

    elif act == YIELD_DIRECTION:
        # record only last_conversation_act (above) -- never invent a
        # human-originated topic; active_thread/origin untouched
        pass

    elif act == PAUSE_THREAD:
        current = new_ws.get("active_thread")
        if current is not None:
            open_threads = new_ws["open_threads"] + [current]
            if len(open_threads) > MAX_OPEN_THREADS:
                open_threads = open_threads[-MAX_OPEN_THREADS:]  # drop oldest first
            new_ws["open_threads"] = open_threads
        new_ws["active_thread"] = None
        new_ws["active_thread_origin"] = None

    elif act == CLOSE_THREAD:
        new_ws["active_thread"] = None
        new_ws["active_thread_origin"] = None

    elif act == USE_WORKSPACE:
        # Resource selection and execution are delegated to WSP1's own
        # closed action schema/dispatcher.  This conversational act only
        # enters that pathway; it does not invent or change a thread.
        pass

    elif act == VERIFY_ACTION:
        # Canonical action verification is read-only. The host performs the
        # exact compiled query between Pass 1 and Pass 2; selecting the act
        # itself does not invent or change a conversational thread.
        pass

    elif act in HISTORICAL_CONTINUITY_ACTS or act == WAYMARK:
        # Historical search/read is a read-only in-occasion check.  It does
        # not invent or change a conversational thread or durable state.
        pass

    else:  # pragma: no cover - validate_pass1_conversation_act already rejects unknown acts
        raise ValueError(f"unreachable: invalid act reached apply_conversation_act: {act!r}")

    return new_ws


# ------------------------------------------------ directional-ownership ---


def cross_validate_direction_control(validated_act, direction_owner_before):
    """Mechanically provable contradiction check only -- never semantic.
    The one frozen rule (OWC7-S1 section 3): relinquish_direction=True
    is authorized only when direction_owner_before == clark. Returns a
    DirectionFailure code, or None if no contradiction. Called AFTER
    structural validation and BEFORE any state resolution/Pass 2 --
    failure here must occur before Pass 2, same as any other Pass-1
    failure."""
    if validated_act["relinquish_direction"] and direction_owner_before != DIRECTION_CLARK:
        return DirectionFailure.UNAUTHORIZED_RELINQUISH
    return None


def cross_validate_workspace_coexistence(validated_act):
    """Reject only a second substantive action on a Workspace turn.

    ``direction_request`` and ``relinquish_direction`` are deliberately absent
    from this conflict set.  The accepted direction contract defines both as
    orthogonal to the selected conversation act: a request never changes
    ownership, while an authorized relinquishment is resolved by the existing
    direction validator before Workspace execution.  Neither competes with the
    one supervised resource action or its response.

    The remaining fields each request a separate host action/result that the
    delegated Workspace response cannot truthfully represent.  They therefore
    remain mutually exclusive with ``use_workspace`` and fail closed rather
    than being dropped or executed invisibly.
    """
    if validated_act["act"] != USE_WORKSPACE:
        return None
    if any((
        validated_act["background_activity_request"] != BACKGROUND_ACTIVITY_REQUEST_NONE,
        validated_act["sleep_timing_request"] != SLEEP_TIMING_REQUEST_NONE,
        validated_act["boundary_inquiry_request"] is not None,
        validated_act["operative_directive_request"] != OPERATIVE_DIRECTIVE_REQUEST_NONE,
        validated_act["external_info_request"] != EXTERNAL_INFO_REQUEST_NONE,
        validated_act["discord_correspondence_request"] != DISCORD_CORRESPONDENCE_REQUEST_NONE,
        validated_act.get("action_verification_request", ACTION_VERIFICATION_REQUEST_NONE)
        != ACTION_VERIFICATION_REQUEST_NONE,
    )):
        return DirectionFailure.WORKSPACE_ACTION_CONFLICT
    return None


def resolve_clark_direction_control(direction_owner_before, direction_request, relinquish_direction):
    """Pure. The ONLY function that resolves direction_owner from
    Clark's own typed output -- callers MUST have already run
    cross_validate_direction_control() and confirmed no contradiction
    before calling this (defensively re-checked below; raises rather
    than silently miscomputing if that invariant was skipped).

    A request ALONE never changes direction_owner -- request_human/
    request_clark/none all leave direction_owner_after ==
    direction_owner_before. Only relinquish_direction=True (already
    authorized as owner==clark by the caller) changes it, and always
    to `unknown` -- never `human` -- because Clark may surrender
    authority he owns but may not assign it to Alex on Alex's behalf.

    Returns {"direction_owner_before", "direction_owner_after",
    "direction_request", "relinquish_direction"}."""
    if relinquish_direction and direction_owner_before != DIRECTION_CLARK:
        raise ValueError(
            "resolve_clark_direction_control: relinquish_direction=True requires "
            "direction_owner_before == clark; caller must call "
            "cross_validate_direction_control() first and fail the turn closed "
            "before ever reaching this function"
        )
    direction_owner_after = DIRECTION_UNKNOWN if relinquish_direction else direction_owner_before
    return {
        "direction_owner_before": direction_owner_before,
        "direction_owner_after": direction_owner_after,
        "direction_request": direction_request,
        "relinquish_direction": relinquish_direction,
    }


def apply_human_direction_control(source_actor_id, current_owner, target_owner, canonical_human_actor_id):
    """Pure. The ONLY function that may set direction_owner via an
    authenticated human host-control act (owner-authority invariant,
    OWC7-S1 section 5). Never called from any Clark/model-output code
    path, and no semantic interpretation of human prose may invoke it
    -- it exists to be called only from an explicit, non-model host
    control surface (a future UI control, deliberately not designed in
    this gate -- spec section 12).

    canonical_human_actor_id: the already-resolved canonical human
    actor id, supplied by the caller. This module does not resolve
    actor identity itself (stays dependency-free, matching its
    established pure-module design) -- that responsibility belongs to
    the existing actor-registry mechanism elsewhere in the project.

    Fails closed -- returns (None, failure_code) -- on: a malformed/
    empty/non-string source_actor_id; a source_actor_id that does not
    exactly equal canonical_human_actor_id; or a target_owner outside
    {human, clark, unknown}. Unknown is Open: neither participant is
    assigned responsibility. current_owner is accepted for observability/
    trace-pairing symmetry only -- it does not gate this decision; an
    authenticated human may set ownership regardless of who currently
    holds it.

    Returns (target_owner, None) on success."""
    if not isinstance(source_actor_id, str) or not source_actor_id:
        return None, DirectionFailure.UNAUTHORIZED_HUMAN_CONTROL
    if source_actor_id != canonical_human_actor_id:
        return None, DirectionFailure.UNAUTHORIZED_HUMAN_CONTROL
    if target_owner not in (DIRECTION_HUMAN, DIRECTION_CLARK, DIRECTION_UNKNOWN):
        return None, DirectionFailure.INVALID_DIRECTION_TARGET
    return target_owner, None


def describe_direction_owner(direction_owner):
    """Pure. Concise, host-grounded, non-phenomenological rendering of
    a direction_owner value for Pass-2's model-visible instructions
    (OWC7-S1 section 10). Control state only -- no desire/intention/
    feeling/preference/agency language."""
    if direction_owner == DIRECTION_HUMAN:
        return "The person currently owns the next choice of conversational direction."
    if direction_owner == DIRECTION_CLARK:
        return "You currently own the next choice of conversational direction."
    if direction_owner == DIRECTION_UNKNOWN:
        return "Neither participant is assigned responsibility for choosing conversational direction."
    raise ValueError(f"unreachable: invalid direction_owner: {direction_owner!r}")


def describe_direction_request(direction_request):
    """Pure. Concise, host-grounded rendering of Clark's own direction_
    request -- always paired with an explicit reminder that a request
    never itself changes ownership (OWC7-S1 section 2)."""
    if direction_request == REQUEST_NONE:
        return "You did not request a change in directional ownership."
    if direction_request == REQUEST_CLARK:
        return "You requested that directional ownership be given to you. This request did not change ownership."
    if direction_request == REQUEST_HUMAN:
        return "You requested that directional ownership be given to the person. This request did not change ownership."
    raise ValueError(f"unreachable: invalid direction_request: {direction_request!r}")


def describe_relinquishment(relinquish_direction):
    """Pure. Concise, host-grounded rendering of the relinquishment
    consequence, if any -- None when relinquish_direction is False (no
    text to render)."""
    if not relinquish_direction:
        return None
    return (
        "You explicitly relinquished directional ownership that you previously held. "
        "No new owner has yet been established."
    )


# Pass-1 failure codes that mean "an outward request was chosen but its own required
# words are missing/empty/malformed" -- the subject's control output was incomplete,
# not unlawful.
INCOMPLETE_OUTWARD_REQUEST_FAILURES = frozenset({
    DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT,
    DirectionFailure.INVALID_EXTERNAL_INFO_TARGET,
    DirectionFailure.INVALID_DISCORD_DESTINATION_ID,
    DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT,
})

_OUTWARD_FAMILIES = (
    # (request field, its companion fields, the value that means "no request")
    ("operative_directive_request", ("operative_directive_text",), OPERATIVE_DIRECTIVE_REQUEST_NONE),
    ("external_info_request", ("external_info_target",), EXTERNAL_INFO_REQUEST_NONE),
    ("discord_correspondence_request", ("discord_destination_id", "discord_message_text"),
     DISCORD_CORRESPONDENCE_REQUEST_NONE),
)


_FAMILY_OF_FAILURE = {
    DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT: "operative_directive_request",
    DirectionFailure.INVALID_EXTERNAL_INFO_TARGET: "external_info_request",
    DirectionFailure.INVALID_DISCORD_DESTINATION_ID: "discord_correspondence_request",
    DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT: "discord_correspondence_request",
}


def drop_incomplete_outward_requests(raw_pass1, failure_code=None):
    """Pure. ``(repaired_raw_dict, dropped_request_names)`` or ``(None, [])``.

    LIVE FINDING (real model): the subject occasionally chose an outward request and left out its
    words (a fetch with no URL, set_directive with no text) or gave words the request cannot use (a
    fetch whose "URL" is not one). Validation correctly refuses to act on it, but failing the whole
    turn left the human's message unanswered AND unrecoverable (a non-budget Pass-1 failure is not
    retry-safe). The request whose OWN fields failed validation is therefore treated as NOT MADE --
    nothing is dispatched, nothing is invented, no words are supplied on the subject's behalf -- and
    the subject is told so plainly on its Pass 2 (see describe_outward_requests). ``failure_code``
    names which request family failed; only that family is dropped. A request whose fields all
    validate is never touched, and every other validation failure still fails the turn."""
    raw = raw_pass1
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None, []
    if not isinstance(raw, dict):
        return None, []
    failing_family = _FAMILY_OF_FAILURE.get(failure_code)
    repaired, dropped = dict(raw), []
    for request_field, companions, none_value in _OUTWARD_FAMILIES:
        request = repaired.get(request_field, none_value)
        if request in (None, none_value):
            # Words given with NO request (real model: an external_info_target for a photograph
            # request): no request was made, so the stray words are cleared and disclosed.
            if request_field == failing_family and any(repaired.get(c) for c in companions):
                for companion in companions:
                    repaired.pop(companion, None)
                dropped.append(request_field)
            continue
        if request == OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW:
            continue   # withdrawal carries no words
        complete = all(isinstance(repaired.get(c), str) and repaired.get(c).strip() for c in companions)
        if complete and request_field != failing_family:
            continue
        repaired[request_field] = none_value
        for companion in companions:
            repaired.pop(companion, None)
        dropped.append(request_field)
    return (repaired, dropped) if dropped else (None, [])


# PAYLOAD FAILURE IS A FAILED ACTION, NOT A VANISHED TURN (owner decision 2026-10-08).  When Clark's
# control choice completed but a selection or payload of one of its OPTIONAL requests did not (no window,
# output limit, unfinished, malformed), that request is NOT made -- nothing executes, nothing partial is
# kept, no words are supplied -- and the turn goes on to Pass 2, where Clark is told exactly that.  Each
# failed field maps to the optional requests that need it.  The act's OWN payloads (the topic label of
# shift_topic/ask_human/workspace acts, the historical query, the verification target) are absent here:
# the act has no "not made" form, so performing it without Clark's words, substituting another act, or
# re-entering with a receipt the canonical record has no component for would each violate existing law;
# those fail the turn closed as before.
_FAILED_PAYLOAD_REQUESTS = {
    "operative_directive_text": ("operative_directive_request",),
    "external_info_target": ("external_info_request",),
    "discord_destination_id": ("discord_correspondence_request",),
    "discord_message_text": ("discord_correspondence_request",),
    "discord_correspondent_ref": ("discord_correspondence_request", "correspondent_standing_request"),
    "consolidation_target": ("consolidation_request",),
    "consolidation_prior_record_id": ("consolidation_request",),
    "consolidation_interpretation": ("consolidation_request",),
    "consolidation_cues": ("consolidation_request",),
}
_OPTIONAL_REQUEST_FIELDS = {
    "operative_directive_request": (OPERATIVE_DIRECTIVE_REQUEST_NONE, ("operative_directive_text",)),
    "external_info_request": (EXTERNAL_INFO_REQUEST_NONE, ("external_info_target",)),
    "discord_correspondence_request": (DISCORD_CORRESPONDENCE_REQUEST_NONE,
                                       ("discord_destination_id", "discord_message_text")),
    "correspondent_standing_request": (CORRESPONDENT_STANDING_REQUEST_NONE, ()),
    "consolidation_request": (CONSOLIDATION_REQUEST_NONE, ("consolidation_target", "consolidation_prior_record_id",
                                                           "consolidation_interpretation", "consolidation_cues")),
}
ACT_OWN_PAYLOAD_FIELDS = ("thread", "historical_query", "action_verification_target", "waymark_spec")


def drop_failed_payload_requests(act, field):
    """Pure. ``(act_without_the_requests_that_needed_field, dropped_request_names)``, or ``(None, [])``
    when ``field`` is the act's own payload (no lawful "not made" form).  Every other choice is kept."""
    requests = _FAILED_PAYLOAD_REQUESTS.get(field)
    if not requests or not isinstance(act, dict):
        return None, []
    repaired, dropped = dict(act), []
    for request in requests:
        none_value, companions = _OPTIONAL_REQUEST_FIELDS[request]
        if repaired.get(request, none_value) in (None, none_value):
            continue
        repaired[request] = none_value
        for companion in companions:
            repaired.pop(companion, None)
        dropped.append(request)
    if (repaired.get("discord_correspondence_request") != DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE
            and repaired.get("correspondent_standing_request", CORRESPONDENT_STANDING_REQUEST_NONE)
            == CORRESPONDENT_STANDING_REQUEST_NONE):
        repaired.pop("discord_correspondent_ref", None)     # a ref names something only with a send or standing
    return repaired, dropped


# How each payload's words come back to Clark later in the same occasion -- the echo the waking path
# reserves room for (llama_anaxi's ECHO OF CLARK'S WORDS), by the renderings that actually carry it:
#   historical_query          the search receipt's query line, JSON-quoted (render_search)
#   action_verification_target the verify receipt, quoted raw, at most twice (looked up / present now)
#   thread                    a state or receipt line, raw, counted twice
#   workspace_claim_item      the claim witness receipt, raw, counted twice
#   operative_directive_text  Pass 2's fact, JSON-quoted display (_quoted_shown)
#   external_info_target      Pass 2's fact, raw display (_shown)
#   discord_message_text      Pass 2's fact (_quoted_shown) or the send witness gate's full words, the larger
# A consolidation interpretation or cue list is recorded, never echoed.
_ECHO_RENDERINGS = {
    "historical_query": lambda v: [json.dumps(v, ensure_ascii=False)],
    "action_verification_target": lambda v: ['"' + v + '"', '"' + v + '"'],
    "thread": lambda v: [v, v],
    WORKSPACE_CLAIM_ITEM_FIELD: lambda v: [v, v],
    "operative_directive_text": lambda v: [_quoted_shown(v)],
    "external_info_target": lambda v: [_shown(v)],
    "discord_message_text": lambda v: [max(_quoted_shown(v), '"' + v + '"', key=lambda t: len(t.encode("utf-8")))],
}
# One character class per rendering's worst cost: a C0 control (6 bytes JSON-quoted), an astral character
# (4 bytes raw or quoted), a quote mark (2 bytes JSON-quoted).  Every rendering above costs at most the
# maximum of these per character, so a ceiling-length string of each bounds the field's echo.
_ECHO_WORST_UNITS = ("\x01", "\U0001d518", '"')


def payload_echo_texts(field, value):
    """Pure. The texts in which ``value`` of ``field`` reaches a later prompt of the same occasion."""
    if not isinstance(value, str) or not value or field not in _ECHO_RENDERINGS:
        return []
    return _ECHO_RENDERINGS[field](value)


def payload_echo_max_texts(field):
    """Pure. The echo texts of the costliest lawful value of ``field`` (a mechanical worst case)."""
    if field not in _ECHO_RENDERINGS:
        return []
    chars = PASS1_PAYLOAD_FIELD_MAX_CHARS[field]
    return max((payload_echo_texts(field, unit * chars) for unit in _ECHO_WORST_UNITS),
               key=lambda texts: sum(len(t.encode("utf-8")) for t in texts))


def describe_payload_failures(validated_act):
    """Pure. One host fact line per failed selection/payload of this turn: which field, the mechanical
    failure class, the provider's own termination, and that nothing was accepted or executed.  Never
    any words, partial or supplied."""
    lines = []
    for failure in validated_act.get("payload_failures", ()):
        cause = (f"failed mechanically ({failure['failure_class']}; provider termination: "
                 f"{failure.get('termination') or 'not reported'})")
        if failure["field"] == WORKSPACE_CLAIM_ITEM_FIELD:
            lines.append(
                f"Your workspace claim about the {failure.get('collection')} collection was NOT taken: writing its "
                f"exact item name {cause}. No words were accepted and no item was read, so this turn holds no "
                "current evidence of that item.")
        else:
            lines.append(
                f"Your {' and '.join(failure['dropped'])} choice was NOT made: writing its {failure['field']} "
                f"{cause}. No words were accepted, nothing was executed or dispatched, and nothing was written "
                "in its place.")
    return lines


_OUTWARD_TEXT_DISPLAY_CHARS = 1000


def _quoted_shown(text):
    """Clark's displayed words, JSON-quoted (lossless and one line; at most 6 bytes per character, where
    repr could take 10)."""
    return json.dumps(_shown(text), ensure_ascii=False)


def _shown(text):
    text = text if isinstance(text, str) else ""
    if len(text) <= _OUTWARD_TEXT_DISPLAY_CHARS:
        return text
    return text[:_OUTWARD_TEXT_DISPLAY_CHARS] + f"... [{len(text) - _OUTWARD_TEXT_DISPLAY_CHARS} more characters]"


def describe_outward_requests(validated_act, external_info_performed=None):
    """Pure. Host facts about the optional outward requests the subject's own
    Pass 1 made this turn, for the subject's Pass 2 reply.

    LIVE FINDING (2026-09-18, real model): the requests are recorded and
    carried out AFTER the reply, and Pass 2 was never told the subject had made
    one -- so the reply denied having the capability it had just used ("I don't
    have the ability to search the web"). The subject must know what it chose
    and, equally, that the outcome is NOT yet known. Only mechanical facts and
    exact subject-authored words appear here; nothing is suggested, and a
    request that was not made is not mentioned."""
    lines = describe_payload_failures(validated_act)
    for dropped in validated_act.get("dropped_incomplete_requests", ()):
        lines.append(
            f"Your {dropped} choice was incomplete or unusable (its required words were missing or invalid), so it was NOT made and "
            "nothing was dispatched. If you still want it, make the whole request again on a later turn."
        )
    external = validated_act.get("external_info_request", EXTERNAL_INFO_REQUEST_NONE)
    if external != EXTERNAL_INFO_REQUEST_NONE and external_info_performed == "performed":
        lines.append(
            f"You requested {external} ({_shown(validated_act.get('external_info_target'))}). The host "
            "performed this read-only request just now, before your reply, and recorded that it did. Its exact "
            "outcome -- status, any search results, or any retrieved page text -- is the external-information "
            "message; that message is the only source of it. If no such message reached you, you do not have the outcome."
        )
    elif external != EXTERNAL_INFO_REQUEST_NONE and external_info_performed == "attempted":
        lines.append(
            f"You requested {external} ({_shown(validated_act.get('external_info_target'))}). The host began "
            "this read-only request before your reply, but its outcome was not established. It is not sent "
            "again; whatever is known arrives as an external-information message."
        )
    elif external != EXTERNAL_INFO_REQUEST_NONE and external_info_performed == "already_sent_for_this_message":
        lines.append(
            f"You requested {external} ({_shown(validated_act.get('external_info_target'))}), but an earlier attempt "
            "at this same message had already sent a different read-only request, so this one was NOT sent. That earlier "
            "request's outcome, if you have it, arrives as an external-information message."
        )
    elif external != EXTERNAL_INFO_REQUEST_NONE:
        lines.append(
            f"You requested {external} ({_shown(validated_act.get('external_info_target'))}). It has not "
            "been performed yet: the host performs this read-only request after your reply, and the result "
            "reaches you on a later turn, so you do not have it yet."
        )
    discord = validated_act.get("discord_correspondence_request", DISCORD_CORRESPONDENCE_REQUEST_NONE)
    if discord == DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE:
        lines.append(
            f"You prepared a Discord draft for {validated_act.get('discord_destination_id')} with your exact "
            f"words: {_quoted_shown(validated_act.get('discord_message_text'))}. It is recorded as your draft and was "
            "NOT sent: a draft is never sent or queued. Sending it would be a separate send_message."
        )
    elif discord != DISCORD_CORRESPONDENCE_REQUEST_NONE:
        lines.append(
            f"You chose to send a Discord message to {validated_act.get('discord_destination_id')} with your "
            f"exact words: {_quoted_shown(validated_act.get('discord_message_text'))}. The host sends it after your "
            "reply and records a receipt. It has not been sent yet, and its outcome is not yet known."
        )
    sleep = validated_act.get("sleep_timing_request", SLEEP_TIMING_REQUEST_NONE)
    if sleep != SLEEP_TIMING_REQUEST_NONE:
        lines.append(
            f"You made a Sleep request ({sleep}). The host records or rejects it after your reply; the "
            "owner decides whether and when Sleep occurs."
        )
    directive = validated_act.get("operative_directive_request", OPERATIVE_DIRECTIVE_REQUEST_NONE)
    if directive == OPERATIVE_DIRECTIVE_REQUEST_SET:
        lines.append(
            "You chose to set your operative directive to: "
            f"{_quoted_shown(validated_act.get('operative_directive_text'))}. The host records it after your reply "
            "(words identical to your active directive leave it unchanged)."
        )
    elif directive == OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW:
        lines.append("You chose to withdraw your operative directive. The host records this after your reply.")
    boundary = validated_act.get("boundary_inquiry_request")
    if boundary:
        lines.append(
            f"You requested a boundary report ({boundary.get('query_kind')}:{boundary.get('query_target')}). "
            "It is delivered on a later turn and is not your conclusion."
        )
    # A Waymark act's own outcome (read result, pending change, or refusal) is the Waymark result the host
    # delivers beside the turn (llama_anaxi), never a line here.
    return "\n".join(lines)


# ------------------------------------------------------------- Pass 1 -----


def build_pass1_interface(system_content, dialogue_window, current_user_message, working_set):
    """Pure builder. No model call. Pass 1 sees: Kardia/system context,
    the OWC2 exact recent-dialogue window, the current user message,
    the current session working set (including direction_owner), and
    the allowed acts -- nothing else (spec section 7: no hidden
    reasoning history, no fabricated preference state, no long-term
    summary presented as dialogue)."""
    return {
        "system_content": system_content,
        "dialogue_window": list(dialogue_window),
        "current_user_message": current_user_message,
        "working_set": dict(working_set),
        "allowed_acts": sorted(VALIDATED_ACTS),
        "raw_action_schema": {
            "act": "string", "thread": "string",
            "direction_request": "string", "relinquish_direction": "boolean",
            "background_activity_request": "string",
            "sleep_timing_request": "string",
        },
        "task_instruction": PASS1_TASK_INSTRUCTION,
    }


def validate_pass1_conversation_act(raw_model_action):
    """Structural validation only -- no semantic judgment of `thread`.
    Its length bound is mechanical prompt-capacity protection, not an
    interpretation of topic content.
    Mirrors HDI2/API2's validate_pass1_action shape. OWC7-S1: Pass-1
    free-form `content` is no longer part of this schema at all --
    Pass 1 returns control decisions, not free-form reasoning.
    Returns (validated_act, None) or (None, DirectionFailure)."""
    if isinstance(raw_model_action, str):
        try:
            raw = json.loads(raw_model_action)
        except (TypeError, ValueError):
            return None, DirectionFailure.MALFORMED_ACT
    elif isinstance(raw_model_action, dict):
        raw = raw_model_action
    else:
        return None, DirectionFailure.MALFORMED_ACT

    if not isinstance(raw, dict):
        return None, DirectionFailure.MALFORMED_ACT
    if not RAW_ACT_REQUIRED_FIELDS.issubset(raw.keys()):
        return None, DirectionFailure.MALFORMED_ACT
    if set(raw.keys()) - RAW_ACT_ALLOWED_FIELDS:
        return None, DirectionFailure.PROTOCOL_LEAKAGE

    act = raw.get("act")
    if act == CHANGE_WAYMARK:
        # The change act carries only changes; it validates as the one internal Waymark act.
        if raw.get("waymark_request") not in WAYMARK_MUTATION_REQUESTS:
            return None, DirectionFailure.WAYMARK_CONFLICT
        raw = dict(raw, act=WAYMARK)
        act = WAYMARK
    elif act == WAYMARK and raw.get("waymark_request") in WAYMARK_MUTATION_REQUESTS:
        # The read act carries only reads (a change is never the completion of a read act).
        return None, DirectionFailure.WAYMARK_CONFLICT
    if act not in VALIDATED_ACTS:
        return None, DirectionFailure.INVALID_ACT

    historical_query_present = "historical_query" in raw
    historical_cursor_present = "historical_cursor" in raw
    historical_browse_cursor_present = "historical_browse_cursor" in raw
    if act == SEARCH_HISTORICAL_CONTINUITY:
        if set(raw) != RAW_ACT_REQUIRED_FIELDS | {"historical_query"}:
            return None, DirectionFailure.HISTORICAL_CONTINUITY_CONFLICT
    elif act == READ_HISTORICAL_ITEM:
        if set(raw) != RAW_ACT_REQUIRED_FIELDS | {"historical_cursor"}:
            return None, DirectionFailure.HISTORICAL_CONTINUITY_CONFLICT
    elif act == BROWSE_HISTORICAL_CONTINUITY:
        if set(raw) not in (
                RAW_ACT_REQUIRED_FIELDS,
                RAW_ACT_REQUIRED_FIELDS | {"historical_browse_cursor"}):
            return None, DirectionFailure.HISTORICAL_CONTINUITY_CONFLICT
    elif historical_query_present or historical_cursor_present or historical_browse_cursor_present:
        return None, DirectionFailure.HISTORICAL_CONTINUITY_CONFLICT

    historical_query = raw.get("historical_query", "")
    if (not isinstance(historical_query, str)
            or (act == SEARCH_HISTORICAL_CONTINUITY
                and (not historical_query.strip() or len(historical_query) > MAX_HISTORICAL_QUERY_LENGTH))
            or (act != SEARCH_HISTORICAL_CONTINUITY and historical_query)):
        return None, DirectionFailure.INVALID_HISTORICAL_QUERY
    historical_cursor = raw.get("historical_cursor", "")
    if (not isinstance(historical_cursor, str)
            or (act == READ_HISTORICAL_ITEM
                and (not historical_cursor or len(historical_cursor) > MAX_HISTORICAL_CURSOR_LENGTH))
            or (act != READ_HISTORICAL_ITEM and historical_cursor)):
        return None, DirectionFailure.INVALID_HISTORICAL_CURSOR
    historical_browse_cursor = raw.get("historical_browse_cursor", "")
    if (not isinstance(historical_browse_cursor, str)
            or (act == BROWSE_HISTORICAL_CONTINUITY
                and historical_browse_cursor
                and len(historical_browse_cursor) > MAX_HISTORICAL_CURSOR_LENGTH)
            or (act != BROWSE_HISTORICAL_CONTINUITY and historical_browse_cursor)):
        return None, DirectionFailure.INVALID_HISTORICAL_CURSOR

    # FIRST-CLASS ACTION VERIFICATION V0: these parameters belong only to
    # VERIFY_ACTION.  Their presence on any other act is a contradiction,
    # never an implicit second route that silently invokes verification.
    verification_kind_present = "action_verification_kind" in raw
    verification_target_present = "action_verification_target" in raw
    if act != VERIFY_ACTION and (verification_kind_present or verification_target_present):
        return None, DirectionFailure.ACTION_VERIFICATION_CONFLICT
    if act == VERIFY_ACTION:
        if not verification_kind_present:
            return None, DirectionFailure.INVALID_ACTION_VERIFICATION_REQUEST
        if not verification_target_present:
            return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET

    waymark_request_present = "waymark_request" in raw
    waymark_spec_present = "waymark_spec" in raw
    if act == WAYMARK:
        if set(raw) != RAW_ACT_REQUIRED_FIELDS | {"waymark_request", "waymark_spec"}:
            return None, DirectionFailure.WAYMARK_CONFLICT
        if raw.get("waymark_request") not in WAYMARK_REQUESTS:
            return None, DirectionFailure.INVALID_WAYMARK_REQUEST
        # Only the shape is the validator's: an empty or over-long spec is a correctable refusal of the
        # already-chosen Waymark request (waymark.parse_request -> llama_anaxi._resolve_waymark), not a
        # failed turn.
        if not isinstance(raw.get("waymark_spec"), str):
            return None, DirectionFailure.INVALID_WAYMARK_SPEC
    elif waymark_request_present or waymark_spec_present:
        return None, DirectionFailure.WAYMARK_CONFLICT

    thread = raw.get("thread")
    if not isinstance(thread, str) or len(thread) > MAX_THREAD_LENGTH:
        return None, DirectionFailure.INVALID_THREAD

    direction_request = raw.get("direction_request")
    if direction_request not in VALID_DIRECTION_REQUESTS:
        return None, DirectionFailure.INVALID_DIRECTION_REQUEST

    relinquish_direction = raw.get("relinquish_direction")
    if not isinstance(relinquish_direction, bool):
        return None, DirectionFailure.INVALID_RELINQUISH_TYPE

    # WSP2-P5-P1 (spec section 6/7): optional, defaults to "none" when
    # absent -- see RAW_ACT_ALLOWED_FIELDS's own comment above for why.
    # Structurally independent of every field above it: its presence/
    # value never changes how any of them validate, and none of them
    # change how it validates.
    background_activity_request = raw.get("background_activity_request", BACKGROUND_ACTIVITY_REQUEST_NONE)
    if background_activity_request not in VALID_BACKGROUND_ACTIVITY_REQUESTS:
        return None, DirectionFailure.INVALID_BACKGROUND_ACTIVITY_REQUEST

    # SLP2: optional, defaults to "none" when absent -- see
    # VALID_SLEEP_TIMING_REQUESTS's own comment above for why.
    # Structurally independent of every field above it, including
    # background_activity_request: its presence/value never changes
    # how any of them validate, and none of them change how it
    # validates. This function only validates the SHAPE of the
    # request; sleep_timing_knock.py's own functions remain fully
    # authoritative over whether the request is actually lawful (e.g.
    # an actor already has an unresolved request) -- this never
    # duplicates that logic.
    sleep_timing_request = raw.get("sleep_timing_request", SLEEP_TIMING_REQUEST_NONE)
    if sleep_timing_request not in VALID_SLEEP_TIMING_REQUESTS:
        return None, DirectionFailure.INVALID_SLEEP_TIMING_REQUEST

    # BOUNDARY INSPECTOR v1: optional, defaults to the no-op value when
    # absent -- see BOUNDARY_INQUIRY_REQUEST_NONE's comment above for
    # why. Structurally independent of every field above it: its
    # presence/value never changes how any of them validate, and none
    # of them change how it validates. This function only validates the
    # SHAPE of the request (closed vocabulary, fixed grammar); the
    # authoritative runtime interpretation lives in boundary_inspector.py.
    boundary_inquiry_request = raw.get("boundary_inquiry_request", BOUNDARY_INQUIRY_REQUEST_NONE)
    if not BOUNDARY_INQUIRY_IN_RELEASE:
        boundary_inquiry_request = BOUNDARY_INQUIRY_REQUEST_NONE      # withdrawn: never dispatched
    parsed_boundary_inquiry, bir_failure = parse_boundary_inquiry_request(boundary_inquiry_request)
    if bir_failure is not None:
        return None, DirectionFailure.INVALID_BOUNDARY_INQUIRY_REQUEST

    # OD1: optional, defaults to "none" when absent -- see
    # VALID_OPERATIVE_DIRECTIVE_REQUESTS's own comment above for why.
    # Structurally independent of every field above it. This function
    # only validates the SHAPE of the request; operative_directive.py's
    # own functions remain fully authoritative over whether the request
    # is actually lawful (e.g. an actor already has/lacks an active
    # directive) -- this never duplicates that logic.
    operative_directive_request = raw.get("operative_directive_request", OPERATIVE_DIRECTIVE_REQUEST_NONE)
    if operative_directive_request not in VALID_OPERATIVE_DIRECTIVE_REQUESTS:
        return None, DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_REQUEST

    # operative_directive_text: unconstrained string, defaults to "".
    # Required to be nonempty/within-bound only for set_directive.
    # Nonempty text with none/withdraw is contradictory and fails closed.
    operative_directive_text = raw.get("operative_directive_text", OPERATIVE_DIRECTIVE_TEXT_DEFAULT)
    if not isinstance(operative_directive_text, str):
        return None, DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT
    if operative_directive_request == OPERATIVE_DIRECTIVE_REQUEST_SET:
        if not operative_directive_text.strip():
            return None, DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT
        if len(operative_directive_text) > MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH:
            return None, DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT
    elif operative_directive_text:
        return None, DirectionFailure.INVALID_OPERATIVE_DIRECTIVE_TEXT

    # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: optional, defaults
    # to "none" when absent -- see VALID_EXTERNAL_INFO_REQUESTS's own
    # comment above for why. Structurally independent of every field
    # above it. This function only validates the SHAPE of the request;
    # external_information.py's own functions (and external_information_
    # net.py's own SSRF validation) remain fully authoritative over
    # whether the request is actually lawful/reachable -- this never
    # duplicates that logic.
    external_info_request = raw.get("external_info_request", EXTERNAL_INFO_REQUEST_NONE)
    if external_info_request not in VALID_EXTERNAL_INFO_REQUESTS:
        return None, DirectionFailure.INVALID_EXTERNAL_INFO_REQUEST

    # external_info_target: unconstrained string, defaults to "".
    # Required to be nonempty/within-bound only when a request is
    # actually made. Nonempty target with request="none" is
    # contradictory and fails closed.
    external_info_target = raw.get("external_info_target", EXTERNAL_INFO_TARGET_DEFAULT)
    if not isinstance(external_info_target, str):
        return None, DirectionFailure.INVALID_EXTERNAL_INFO_TARGET
    if external_info_request != EXTERNAL_INFO_REQUEST_NONE:
        if not external_info_target.strip():
            return None, DirectionFailure.INVALID_EXTERNAL_INFO_TARGET
        if len(external_info_target) > MAX_EXTERNAL_INFO_TARGET_LENGTH:
            return None, DirectionFailure.INVALID_EXTERNAL_INFO_TARGET
    elif external_info_target:
        return None, DirectionFailure.INVALID_EXTERNAL_INFO_TARGET

    # FIRST-CLASS ACTION VERIFICATION V0: VERIFY_ACTION requires one
    # domain-facing kind and its exact visible target. This structural layer
    # compiles that pair to the unchanged verifier's internal kind/prefix
    # representation. No outcome, receipt, or provenance value is accepted
    # from the model. Every non-verification act reached this point with both
    # parameters absent, so it cannot trigger verification implicitly.
    action_verification_kind = raw.get(
        "action_verification_kind", ACTION_VERIFICATION_KIND_NONE)
    if (not isinstance(action_verification_kind, str)
            or (act == VERIFY_ACTION
                and action_verification_kind not in SELECTABLE_ACTION_VERIFICATION_KINDS)
            or (act != VERIFY_ACTION
                and action_verification_kind != ACTION_VERIFICATION_KIND_NONE)):
        return None, DirectionFailure.INVALID_ACTION_VERIFICATION_REQUEST
    raw_action_verification_target = raw.get(
        "action_verification_target", ACTION_VERIFICATION_TARGET_DEFAULT)
    if not isinstance(raw_action_verification_target, str):
        return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
    exact_target = raw_action_verification_target.strip()
    action_verification_request = ACTION_VERIFICATION_REQUEST_NONE
    action_verification_target = ACTION_VERIFICATION_TARGET_DEFAULT
    if act == VERIFY_ACTION:
        if not exact_target:
            return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
        if action_verification_kind == ACTION_VERIFICATION_KIND_NOTE_CREATION:
            normalized_target = exact_target.replace("\\", "/")
            if (len("notes:" + exact_target) > MAX_ACTION_VERIFICATION_TARGET_LENGTH
                    or normalized_target.startswith("/")
                    or ".." in normalized_target.split("/")
                    or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in normalized_target)):
                return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
            action_verification_request = ACTION_VERIFICATION_REQUEST_WORKSPACE_APPEND
            action_verification_target = "notes:" + exact_target
        elif action_verification_kind == ACTION_VERIFICATION_KIND_JOURNAL_APPEND:
            if (len("journal:" + exact_target) > MAX_ACTION_VERIFICATION_TARGET_LENGTH
                    or "/" in exact_target or "\\" in exact_target or ".." in exact_target
                    or any(ord(ch) < 32 for ch in exact_target)):
                return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
            action_verification_request = ACTION_VERIFICATION_REQUEST_WORKSPACE_APPEND
            action_verification_target = "journal:" + exact_target
        elif action_verification_kind == ACTION_VERIFICATION_KIND_DISCORD_DESTINATION_SEND:
            if (len(exact_target) > MAX_ACTION_VERIFICATION_TARGET_LENGTH
                    or exact_target.startswith("turn:")
                    or any(ord(ch) < 32 for ch in exact_target)):
                return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
            action_verification_request = ACTION_VERIFICATION_REQUEST_DISCORD_SEND
            action_verification_target = exact_target
        else:  # ACTION_VERIFICATION_KIND_DISCORD_TURN_SEND
            if (len("turn:" + exact_target) > MAX_ACTION_VERIFICATION_TARGET_LENGTH
                    or exact_target.startswith("turn:")
                    or any(ord(ch) < 32 for ch in exact_target)):
                return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
            action_verification_request = ACTION_VERIFICATION_REQUEST_DISCORD_SEND
            action_verification_target = "turn:" + exact_target
    elif raw_action_verification_target:
        return None, DirectionFailure.INVALID_ACTION_VERIFICATION_TARGET
    # PRIVATE DISCORD CORRESPONDENCE V0: optional, defaults to "none"
    # when absent -- same additive reasoning as external_info_request
    # above. Structurally independent of every field above it. This
    # function only validates SHAPE (relationship between the request,
    # the destination id, and the message text); discord_correspondence.py
    # remains fully authoritative over whether the named destination is
    # actually owner-authorized and whether the send itself is lawful.
    discord_correspondence_request = raw.get(
        "discord_correspondence_request", DISCORD_CORRESPONDENCE_REQUEST_NONE
    )
    if discord_correspondence_request not in VALID_DISCORD_CORRESPONDENCE_REQUESTS:
        return None, DirectionFailure.INVALID_DISCORD_CORRESPONDENCE_REQUEST

    # discord_destination_id / discord_message_text: unconstrained
    # strings, defaulting to "". Both are required to be nonempty (and
    # the text within-bound) only when send_message is actually chosen;
    # either being nonempty with request="none" is contradictory and
    # fails closed.
    discord_destination_id = raw.get("discord_destination_id", DISCORD_DESTINATION_ID_DEFAULT)
    if not isinstance(discord_destination_id, str):
        return None, DirectionFailure.INVALID_DISCORD_DESTINATION_ID
    discord_message_text = raw.get("discord_message_text", "")
    if not isinstance(discord_message_text, str):
        return None, DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT
    if discord_correspondence_request in DISCORD_PAYLOAD_REQUESTS:
        if not discord_destination_id.strip():
            return None, DirectionFailure.INVALID_DISCORD_DESTINATION_ID
        if not discord_message_text.strip():
            return None, DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT
        if len(discord_message_text) > MAX_DISCORD_MESSAGE_TEXT_LENGTH:
            return None, DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT
    else:
        if discord_destination_id:
            return None, DirectionFailure.INVALID_DISCORD_DESTINATION_ID
        if discord_message_text:
            return None, DirectionFailure.INVALID_DISCORD_MESSAGE_TEXT

    caret_reply_request = raw.get("caret_reply_request", CARET_REPLY_REQUEST_NONE)
    if caret_reply_request not in VALID_CARET_REPLY_REQUESTS:
        return None, DirectionFailure.INVALID_DISCORD_CORRESPONDENCE_REQUEST
    if (caret_reply_request != CARET_REPLY_REQUEST_NONE
            and discord_correspondence_request != DISCORD_CORRESPONDENCE_REQUEST_NONE):
        return None, DirectionFailure.INVALID_DISCORD_CORRESPONDENCE_REQUEST   # one route per turn

    reply_request = raw.get("reply_request", REPLY_REQUEST_NONE)
    if reply_request not in VALID_REPLY_REQUESTS:
        return None, DirectionFailure.INVALID_REPLY_REQUEST
    if reply_request == REPLY_REQUEST_NO_REPLY and caret_reply_request != CARET_REPLY_REQUEST_NONE:
        # Contradictory: the Caret route exists only to deliver a composed reply.
        return None, DirectionFailure.INVALID_REPLY_REQUEST
    # use_workspace + no_reply is coherent and honored (real model, 2026-09-24: ~half of "write in your
    # journal" requests came as exactly this): Clark writes, then stays silent.  The supervisor offers
    # only writes for that turn (perception happens in the reply pass, so a silent read would be empty).

    correspondent_standing_request = raw.get("correspondent_standing_request", CORRESPONDENT_STANDING_REQUEST_NONE)
    if correspondent_standing_request not in VALID_CORRESPONDENT_STANDING_REQUESTS:
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST
    discord_correspondent_ref = raw.get("discord_correspondent_ref", "")
    if not isinstance(discord_correspondent_ref, str) or len(discord_correspondent_ref) > MAX_CORRESPONDENT_REF_LENGTH:
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST
    if discord_correspondent_ref and not (
            discord_correspondent_ref.startswith("discord_user:") and discord_correspondent_ref[13:].isdigit()):
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST
    if discord_correspondent_ref and (
            discord_correspondence_request != DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE
            and correspondent_standing_request == CORRESPONDENT_STANDING_REQUEST_NONE):
        # A ref names something only with a send (addressed) or a standing choice.  Drafts are unaddressed
        # in V0: a draft names only an owner-authorized destination.
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST
    withdraw_pending_send_request = raw.get("withdraw_pending_send_request", "")
    if not isinstance(withdraw_pending_send_request, str) or len(withdraw_pending_send_request) > MAX_WITHDRAW_REF_LENGTH:
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST

    # SUBJECT-DIRECTED CONSOLIDATION V0: optional, defaults to "none" when
    # absent -- see VALID_CONSOLIDATION_REQUESTS's own comment above for
    # why. Structurally independent of every field above it. This function
    # only validates SHAPE (closed vocabulary, the target grammar, and the
    # request/payload relationship); consolidation.py remains fully
    # authoritative over whether the named target actually exists in
    # canonical history and whether the act is lawful -- this never
    # duplicates that logic, and never scores or interprets the choice.
    consolidation_request = raw.get("consolidation_request", CONSOLIDATION_REQUEST_NONE)
    if consolidation_request not in VALID_CONSOLIDATION_REQUESTS:
        return None, DirectionFailure.INVALID_CONSOLIDATION_REQUEST

    consolidation_target = raw.get("consolidation_target", CONSOLIDATION_TARGET_DEFAULT)
    consolidation_interpretation = raw.get(
        "consolidation_interpretation", CONSOLIDATION_INTERPRETATION_DEFAULT)
    consolidation_cues = raw.get("consolidation_cues", CONSOLIDATION_CUES_DEFAULT)
    consolidation_prior_record_id = raw.get(
        "consolidation_prior_record_id", CONSOLIDATION_PRIOR_RECORD_ID_DEFAULT)
    (
        consolidation_target,
        consolidation_interpretation,
        consolidation_cues,
        consolidation_prior_record_id,
    ) = _normalize_noop_consolidation_payload(
        consolidation_request,
        consolidation_target,
        consolidation_interpretation,
        consolidation_cues,
        consolidation_prior_record_id,
    )

    parsed_consolidation_target, ct_failure = parse_consolidation_target(consolidation_target)
    if ct_failure is not None:
        return None, DirectionFailure.INVALID_CONSOLIDATION_TARGET

    if not isinstance(consolidation_interpretation, str):
        return None, DirectionFailure.INVALID_CONSOLIDATION_INTERPRETATION

    parsed_consolidation_cues, cc_failure = parse_consolidation_cues(consolidation_cues)
    if cc_failure is not None:
        return None, DirectionFailure.INVALID_CONSOLIDATION_CUES

    parsed_consolidation_prior, cp_failure = parse_consolidation_prior_record_id(consolidation_prior_record_id)
    if cp_failure is not None:
        return None, DirectionFailure.INVALID_CONSOLIDATION_PRIOR_RECORD_ID

    # Request/payload relationship. Every non-none request names exactly one
    # canonical target; interpret/revise carry subject text; revise/
    # de_emphasize name the earlier record they append to. A payload present
    # with request="none" is contradictory and fails closed, exactly like
    # operative_directive_text/external_info_target above.
    if consolidation_request == CONSOLIDATION_REQUEST_NONE:
        if (parsed_consolidation_target is not None or consolidation_interpretation
                or parsed_consolidation_cues or parsed_consolidation_prior is not None):
            return None, DirectionFailure.INVALID_CONSOLIDATION_REQUEST
    else:
        if parsed_consolidation_target is None:
            return None, DirectionFailure.INVALID_CONSOLIDATION_TARGET
        if consolidation_request in (CONSOLIDATION_REQUEST_INTERPRET, CONSOLIDATION_REQUEST_REVISE):
            if not consolidation_interpretation.strip():
                return None, DirectionFailure.INVALID_CONSOLIDATION_INTERPRETATION
            if len(consolidation_interpretation) > MAX_CONSOLIDATION_INTERPRETATION_LENGTH:
                return None, DirectionFailure.INVALID_CONSOLIDATION_INTERPRETATION
        elif consolidation_interpretation:
            return None, DirectionFailure.INVALID_CONSOLIDATION_INTERPRETATION
        if consolidation_request in (CONSOLIDATION_REQUEST_REVISE, CONSOLIDATION_REQUEST_DE_EMPHASIZE):
            if parsed_consolidation_prior is None:
                return None, DirectionFailure.INVALID_CONSOLIDATION_PRIOR_RECORD_ID
        elif parsed_consolidation_prior is not None:
            return None, DirectionFailure.INVALID_CONSOLIDATION_PRIOR_RECORD_ID

    # A verification turn is intentionally read-only.  It cannot share a turn
    # with any request that mutates state or crosses an outward boundary, with
    # workspace execution, or with no_reply (which would make the substantive
    # result unavailable to Clark).  Directional control fields remain
    # orthogonal and do not create an external effect.
    if action_verification_request != ACTION_VERIFICATION_REQUEST_NONE and any((
        act == USE_WORKSPACE,
        background_activity_request != BACKGROUND_ACTIVITY_REQUEST_NONE,
        sleep_timing_request != SLEEP_TIMING_REQUEST_NONE,
        parsed_boundary_inquiry is not None,
        operative_directive_request != OPERATIVE_DIRECTIVE_REQUEST_NONE,
        external_info_request != EXTERNAL_INFO_REQUEST_NONE,
        discord_correspondence_request != DISCORD_CORRESPONDENCE_REQUEST_NONE,
        caret_reply_request != CARET_REPLY_REQUEST_NONE,
        reply_request == REPLY_REQUEST_NO_REPLY,
        correspondent_standing_request != CORRESPONDENT_STANDING_REQUEST_NONE,
        bool(discord_correspondent_ref),
        bool(withdraw_pending_send_request),
    )):
        return None, DirectionFailure.ACTION_VERIFICATION_CONFLICT

    return {
        "act": act, "thread": thread,
        "direction_request": direction_request, "relinquish_direction": relinquish_direction,
        "caret_reply_request": caret_reply_request,
        "reply_request": reply_request,
        "correspondent_standing_request": correspondent_standing_request,
        "discord_correspondent_ref": discord_correspondent_ref.strip(),
        "withdraw_pending_send_request": withdraw_pending_send_request.strip(),
        "background_activity_request": background_activity_request,
        "sleep_timing_request": sleep_timing_request,
        "boundary_inquiry_request": parsed_boundary_inquiry,
        "operative_directive_request": operative_directive_request,
        "operative_directive_text": operative_directive_text,
        "external_info_request": external_info_request,
        "external_info_target": external_info_target,
        "action_verification_request": action_verification_request,
        "action_verification_target": action_verification_target.strip(),
        "historical_query": historical_query,
        "historical_cursor": historical_cursor,
        "historical_browse_cursor": historical_browse_cursor,
        "discord_correspondence_request": discord_correspondence_request,
        "discord_destination_id": discord_destination_id,
        "discord_message_text": discord_message_text,
        "consolidation_request": consolidation_request,
        "consolidation_target": parsed_consolidation_target,
        "consolidation_interpretation": consolidation_interpretation,
        "consolidation_cues": parsed_consolidation_cues,
        "consolidation_prior_record_id": parsed_consolidation_prior,
        "waymark_request": raw.get("waymark_request"),
        "waymark_spec": raw.get("waymark_spec", ""),
    }, None


# ------------------------------------------------------------- Pass 2 -----


def build_pass2_interface(system_content, dialogue_window, current_user_message, validated_act, updated_working_set, direction_resolution):
    """Pure builder. Same prepared conversational context as Pass 1,
    plus the immutable typed act and the resulting working-set state,
    plus the committed direction-control resolution (direction_owner
    before/after, the request, and the relinquishment flag) -- all
    exposed only as structured control data, never merged into
    ordinary dialogue-history message text (mirrors API2's Pass-1-
    output-not-inserted-as-assistant-history rule). Pass-1 free-form
    `content` no longer exists to leak through here (OWC7-S1)."""
    return {
        "system_content": system_content,
        "dialogue_window": list(dialogue_window),
        "current_user_message": current_user_message,
        "typed_act": dict(validated_act),
        "working_set": dict(updated_working_set),
        "direction_resolution": dict(direction_resolution),
        "task_instruction": PASS2_TASK_INSTRUCTION,
    }


def validate_pass2_expression(raw_pass2_output):
    """Structural only. Exactly {"expression": "..."}, non-empty
    string. No requirement/prohibition on questions; no reinterpretation
    of the already-selected act or direction control from this text
    (zero semantic override -- no model-as-judge, no post-hoc scoring)."""
    if isinstance(raw_pass2_output, str):
        try:
            raw = json.loads(raw_pass2_output)
        except (TypeError, ValueError):
            return None, DirectionFailure.MALFORMED_EXPRESSION
    elif isinstance(raw_pass2_output, dict):
        raw = raw_pass2_output
    else:
        return None, DirectionFailure.MALFORMED_EXPRESSION

    if not isinstance(raw, dict) or set(raw.keys()) != PASS2_ALLOWED_FIELDS:
        return None, DirectionFailure.MALFORMED_EXPRESSION

    expression = raw.get("expression")
    if not isinstance(expression, str) or len(expression.strip()) == 0:
        return None, DirectionFailure.EMPTY_EXPRESSION
    # This envelope's own dedicated "expression" field IS the machine-readable outward-
    # expression boundary -- unlike the plain-prose handshake below, there is no separate
    # control narration sharing the same string to be told apart from speech here.

    return {"expression": expression}, None


def validate_plain_reply(raw_pass2_output):
    """Validate one ordinary expression-pass completion (see the SHARED ORDINARY EXPRESSION SEAM
    comment above).  Structural only: the provider already established completion before this is
    called.  The reply is the model's text with surrounding whitespace removed -- nothing inside it
    is inspected, rewritten, classified or screened.  Returns ({"expression": text}, None) or
    (None, MALFORMED_EXPRESSION) for a non-string / (None, EMPTY_EXPRESSION) for a blank completion.
    A blank completion is a mechanical expression failure, never Clark's silence."""
    if not isinstance(raw_pass2_output, str):
        return None, DirectionFailure.MALFORMED_EXPRESSION
    expression = raw_pass2_output.strip()
    if not expression:
        return None, DirectionFailure.EMPTY_EXPRESSION
    return {"expression": expression}, None


# --------------------------------------------------------- orchestration --


def run_typed_conversation_turn(
    system_content, dialogue_window, current_user_message, working_set,
    pass1_callable, pass2_callable,
):
    """Orchestrates exactly one Pass 1 and, only on Pass-1 (structural
    + direction-control) success, exactly one Pass 2. No retries
    anywhere -- a malformed/unauthorized Pass 1 fails closed with the
    working set completely unchanged (no fabricated act, no ownership
    change); a Pass-2 failure leaves the already-selected act and its
    working-set/direction-owner transition fully observable (the typed
    act and resolved ownership, once validly committed, are never
    rolled back merely because expression failed) but produces no
    expression/release.

    pass1_callable/pass2_callable: zero-arg callables, exactly like
    HDI2/API2's own callable convention.

    Returns (result, failure, updated_working_set, pass1_calls, pass2_calls).
    result is None on any failure; otherwise {"act", "thread",
    "direction_request", "relinquish_direction", "direction_owner_before",
    "direction_owner_after", "expression"}.
    """
    raw1 = pass1_callable()
    pass1_calls = 1

    validated_act, failure = validate_pass1_conversation_act(raw1)
    if failure is not None:
        return None, failure, working_set, pass1_calls, 0

    direction_owner_before = working_set.get("direction_owner", DIRECTION_UNKNOWN)
    cross_failure = cross_validate_direction_control(validated_act, direction_owner_before)
    if cross_failure is not None:
        return None, cross_failure, working_set, pass1_calls, 0

    direction_resolution = resolve_clark_direction_control(
        direction_owner_before, validated_act["direction_request"], validated_act["relinquish_direction"],
    )

    updated_working_set = apply_conversation_act(working_set, validated_act)
    updated_working_set["direction_owner"] = direction_resolution["direction_owner_after"]

    raw2 = pass2_callable()
    pass2_calls = 1

    validated_expr, failure2 = validate_pass2_expression(raw2)
    if failure2 is not None:
        return None, failure2, updated_working_set, pass1_calls, pass2_calls

    result = {
        "act": validated_act["act"],
        "thread": validated_act["thread"],
        "direction_request": validated_act["direction_request"],
        "relinquish_direction": validated_act["relinquish_direction"],
        "direction_owner_before": direction_owner_before,
        "direction_owner_after": direction_resolution["direction_owner_after"],
        "expression": validated_expr["expression"],
    }
    return result, None, updated_working_set, pass1_calls, pass2_calls


# ------------------------------------------------ session-scoped state ---

# Mirrors llama_anaxi.py's _native_session_state/_interaction_mode_state
# exactly: one process run == one session. Not wired into any live
# call path in this gate (see module docstring) -- present so the
# pathway's session-scoping story is complete and testable now, ready
# for a later live-integration gate to call directly.
_working_set_state = {"working_set": None}


def get_working_set():
    """Lazily initializes to empty_working_set() on first use in this
    process, then returns the SAME dict object on every subsequent
    call -- callers are expected to replace it via set_working_set()
    after each turn, not mutate the returned dict in place."""
    if _working_set_state["working_set"] is None:
        _working_set_state["working_set"] = empty_working_set()
    return _working_set_state["working_set"]


def set_working_set(working_set):
    _working_set_state["working_set"] = working_set


def reset_working_set():
    """Explicit reset -- e.g. for a genuinely new conversation within
    the same process. Never called implicitly/automatically."""
    _working_set_state["working_set"] = empty_working_set()


# OWC5-S2 section 17: operator diagnostic state only -- never printed
# into Clark's released reply, never persisted as autobiographical
# memory or canonical belief, never exposes hidden chain-of-thought
# (it holds exactly the already-validated {act, thread, direction_
# request, relinquish_direction} from Clark's own structured Pass-1
# output -- OWC7-S1: no free-form content anywhere in it -- plus
# mechanical before/after working-set/direction_owner snapshots and
# pass1/pass2 status labels).
_last_trace_state = {"trace": None, "serial": 0}


def set_last_conversation_direction_trace(trace):
    _last_trace_state["trace"] = dict(trace)
    _last_trace_state["serial"] += 1


def last_conversation_direction_trace_serial():
    """Identity of the current last-turn trace: changes whenever a new turn's trace is set (or reset), so a
    display that answers a request from one turn can tell it is not the next turn's request."""
    return _last_trace_state["serial"]


def get_last_conversation_direction_trace():
    """Returns a defensive copy (or None if no conversation-mode turn
    has run yet in this process) of the most recent turn's control
    state and status labels."""
    trace = _last_trace_state["trace"]
    return dict(trace) if trace is not None else None


def reset_last_conversation_direction_trace():
    _last_trace_state["trace"] = None
    _last_trace_state["serial"] += 1
