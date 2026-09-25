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

ALLOWED_ACTS = {
    DEVELOP_CURRENT, SHIFT_TOPIC, ASK_HUMAN, YIELD_DIRECTION,
    PAUSE_THREAD, CLOSE_THREAD, USE_WORKSPACE,
}

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
VALID_DISCORD_CORRESPONDENCE_REQUESTS = {
    DISCORD_CORRESPONDENCE_REQUEST_NONE, DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE,
}

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
    "[+discord_destination_id, +discord_message_text, sent verbatim] | none. Destinations are "
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


def render_pass1_action_menu_parts(*, discord_destinations=None, workspace_available=False, search_choice_count=0, journal_threads=None, caret_occasion=False,
                                   source_occasion=None, correspondents=None, pending_sends=None, notes_available=False,
                                   open_reading=None, open_page=None):
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
    if workspace_available:
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
    fixed = "\n".join([
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
    ])
    tail_lines = []
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
            f"discord_correspondence_request: {DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE} "
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
    if workspace_available:
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
        "act": {"type": "string", "enum": sorted(ALLOWED_ACTS)},
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


_OUTWARD_TEXT_DISPLAY_CHARS = 1000


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
    lines = []
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
    if discord != DISCORD_CORRESPONDENCE_REQUEST_NONE:
        lines.append(
            f"You chose to send a Discord message to {validated_act.get('discord_destination_id')} with your "
            f"exact words: {_shown(validated_act.get('discord_message_text'))!r}. The host sends it after your "
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
            f"{_shown(validated_act.get('operative_directive_text'))!r}. The host records it after your reply "
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
        "allowed_acts": sorted(ALLOWED_ACTS),
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
    if act not in ALLOWED_ACTS:
        return None, DirectionFailure.INVALID_ACT

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
    if discord_correspondence_request == DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE:
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
            discord_correspondence_request == DISCORD_CORRESPONDENCE_REQUEST_NONE
            and correspondent_standing_request == CORRESPONDENT_STANDING_REQUEST_NONE):
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST     # a ref names something only with a request
    withdraw_pending_send_request = raw.get("withdraw_pending_send_request", "")
    if not isinstance(withdraw_pending_send_request, str) or len(withdraw_pending_send_request) > MAX_WITHDRAW_REF_LENGTH:
        return None, DirectionFailure.INVALID_CORRESPONDENT_REQUEST

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
        "discord_correspondence_request": discord_correspondence_request,
        "discord_destination_id": discord_destination_id,
        "discord_message_text": discord_message_text,
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
