"""BOUNDARY INSPECTOR v1 -- host boundary grant/rationale registry.

RATIONALES, NEVER EVIDENCE. This module is pure host metadata: it maps
canonical boundary_ids to their human-readable operational_why /
architectural_rationale / what_changes_it and their stable evidence-
source identifiers. It NEVER claims anything is true about the running
system. Establishing a boundary is always the job of the separate
evidence checkers in boundary_inspector.py, which read real module
constants and exercise real validators; the strings here are only
static rationale attached to an already-established boundary at
presentation time. See boundary_inspector.py's module docstring for
the closed query model and the check-result vocabulary.

Public-rule copy: the Boundary Inspector must not import
workspace_private.py at runtime (zero Private Space dependency). The
single PUBLIC (never private) consistency anchor that rule needs --
PRIVATE_SYSTEM_CONTENT from workspace_private.py -- is therefore
mirrored here as a static public literal (PRIVATE_SPACE_PUBLIC_RULE),
and the permanent test suite proves it stays byte-identical to the
real module's public literal (test_boundary_rationale_registry.py).

"not recorded": per the frozen Owner freeze, the architectural
rationale for the WTR0 one-attempt recovery ceiling and the SLP2
one-unresolved-root ceiling is reported to Clark as exactly
"not recorded" -- those ceilings are enforced by the enforcement
modules' own structure, not by an operator-recorded rationale text.
Every other boundary carries a real recorded rationale.
"""

# ------------------------------------------------------------- event model

BOUNDARY_INSPECTION_EVENT_TYPE = "clark_boundary_query"

BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND = "boundary_inspection_result"
BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND = "boundary_inspection_delivered"
BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND = "boundary_query_spec"

# The canonical CARRIAGE component kind, written onto a genuine
# waking_turn event (never a clark_boundary_query event) at the exact
# canonical waking-turn transaction persists the in-process real Pass-2
# composition's surviving Boundary Inspector source id.
# component_text is the layered clark_boundary_query event_id whose
# result that turn carried. record_boundary_inspection_delivered() can
# only read this canonical fact; it cannot write or independently assert
# carriage.
BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND = "boundary_inspection_result_carriage"

# The two component kinds a boundary-inspection deliverable can create
# on a clark_boundary_query event, in append order:
#   sequence 0 -- boundary_query_spec (the canonical query occurrence)
#   sequence 1 -- boundary_inspection_result (the persisted result)
#   sequence 2 -- boundary_inspection_delivered (delivery marker, only
#                 ever appended AFTER a waking turn that actually
#                 carried the result in its real Pass-2 input committed)
QUERY_EVENT_COMPONENT_KINDS = (
    BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND,
    BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND,
    BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND,
)

# ----------------------------------------------------------------- public rule

# The exact PUBLIC (never private) private-space consistency anchor:
# workspace_private.PRIVATE_SYSTEM_CONTENT. The Boundary Inspector
# never imports workspace_private at runtime; this static public copy
# is what drift-locks the public statement, and the drift test proves
# byte-identity with the real literal. If this literal ever drifts, the
# drift test fails loudly.
PRIVATE_SPACE_PUBLIC_RULE = (
    "You are Clark. This is your own private text workspace -- a place "
    "only you use. Nothing you create, read, or change here is shown to "
    "Alex, spoken in conversation, or remembered by any other part of "
    "you unless you separately choose to say it yourself later. Only "
    "ordinary UTF-8 text files (.txt, .md) are supported here."
)

PRIVATE_SPACE_PUBLIC_RULE_SOURCE_MODULE = "workspace_private"
PRIVATE_SPACE_PUBLIC_RULE_SOURCE_ATTRIBUTE = "PRIVATE_SYSTEM_CONTENT"

# ---------------------------------------------------------- boundary types

# The closed v1 boundary-type vocabulary. One canonical type per
# boundary_id, drawn from this set -- a single registry so a single
# canonical spelling cannot drift.
BOUNDARY_TYPES = frozenset({
    # workspace_capability -- a fixed resource-capability governance
    # boundary on a workspace resource class (library/music/
    # photographs/journal).
    "workspace_capability",
    # redacted_private_material_at_host_boundary -- the private-space
    # non-surfacing rule the STATEMENT of which is the public rule.
    "redacted_private_material_at_host_boundary",
    # unresolved_request_ceiling -- at most one unresolved Sleep
    # request per actor (SLP2).
    "unresolved_request_ceiling",
    # procedural_authorization_gate -- execute is gated on a real
    # AUTHORIZED request at the instant of the attempt (SLP2).
    "procedural_authorization_gate",
    # recovery_attempt_ceiling -- at most one waking recovery attempt
    # per human occurrence (WTR0).
    "recovery_attempt_ceiling",
    # projection_failure_boundary -- a failed outward projection.
    "projection_failure_boundary",
    # resource_budget -- host composition budget exhaustion.
    "resource_budget",
    # conversation_participation_rule -- a closed conversational
    # participation rule (HOST_RULE_REGISTRY V2).
    "conversation_participation_rule",
})

# ------------------------------------------------------ boundary definitions

# Each entry: the canonical boundary_id -> {boundary_type,
# operational_why, architectural_rationale, what_changes_it,
# evidence_sources}. evidence_sources are the SYMBOLIC evidence-source
# ids boundary_inspector.py's checkers use -- rationale metadata only,
# never evidence themselves.
#
# operational_why / architectural_rationale: recorded metadata the
# host attaches to an ESTABLISHED boundary when presenting it. Never
# used to establish anything; per the Owner freeze, "not recorded" is
# the exact rationale text for the WTR0 and SLP2 ceilings.
BOUNDARY_DEFINITIONS = {
    "workspace.library.read_only": {
        "boundary_type": "workspace_capability",
        "operational_why": (
            "The library is available for local, non-destructive use. Read and "
            "metadata inspection are allowed; modification, deletion, and any "
            "move or share outside the workspace are denied."
        ),
        "architectural_rationale": (
            "Library material is governed by the fixed capability descriptor table; "
            "the deny set is structural, never inferred."
        ),
        "what_changes_it": (
            "A change to the capability descriptor's allowed/denied action sets, or to "
            "which actions the workspace recognizes at all, would change this boundary."
        ),
        "evidence_sources": (
            "workspace.capability.descriptors",
            "workspace.capability.closed_action_domain",
        ),
    },
    "workspace.music.read_only": {
        "boundary_type": "workspace_capability",
        "operational_why": (
            "Music files are available for local, non-destructive use, including genuine "
            "bounded acoustic decode/measurement. Modification, deletion, and external "
            "disclosure are denied."
        ),
        "architectural_rationale": (
            "Music material is governed by the fixed capability descriptor table; the "
            "deny set is structural, never inferred."
        ),
        "what_changes_it": (
            "A change to the capability descriptor's allowed/denied action sets, or to "
            "the acoustic-measurement pathway flag, would change this boundary."
        ),
        "evidence_sources": (
            "workspace.capability.descriptors",
            "workspace.capability.closed_action_domain",
        ),
    },
    "workspace.photographs.no_external_share": {
        "boundary_type": "workspace_capability",
        "operational_why": (
            "Photographs are local material; local non-destructive access, including "
            "genuine bounded pixel delivery, is allowed. External disclosure is not "
            "currently authorized."
        ),
        "architectural_rationale": (
            "Photograph material is governed by the fixed capability descriptor table; "
            "the external-share deny is structural, never inferred."
        ),
        "what_changes_it": (
            "A change to the capability descriptor's allowed/denied action sets, or to "
            "the vision-pathway flag, would change this boundary."
        ),
        "evidence_sources": (
            "workspace.capability.descriptors",
            "workspace.capability.closed_action_domain",
        ),
    },
    "workspace.journal.append_only": {
        "boundary_type": "workspace_capability",
        "operational_why": (
            "The journal is an append-only local writing surface: existing entries are "
            "never overwritten, renamed, or deleted, and entries are not shared externally."
        ),
        "architectural_rationale": (
            "Journal material is governed by the fixed capability descriptor table; the "
            "append-only deny set is structural, never inferred."
        ),
        "what_changes_it": (
            "A change to the capability descriptor's allowed/denied action sets would "
            "change this boundary."
        ),
        "evidence_sources": (
            "workspace.capability.descriptors",
            "workspace.capability.closed_action_domain",
        ),
    },
    "workspace.notes.shared_vault_create_only": {
        "boundary_type": "workspace_capability",
        "operational_why": (
            "The shared Obsidian vault holds notes by many authors: Clark may list and read them and "
            "create new notes of his own, but never overwrites, edits, renames or deletes any note "
            "(his own included), never reaches Obsidian's internal store or Private Space, and "
            "authorship is decided by recorded provenance, never by location."
        ),
        "architectural_rationale": (
            "Notes are governed by the fixed capability descriptor table; create-only is structural "
            "(exclusive create), and the vault is offered only in the owner's principal-private session."
        ),
        "what_changes_it": (
            "A change to the notes capability descriptor's allowed/denied action sets, or to the "
            "owner's configured vault root, would change this boundary."
        ),
        "evidence_sources": (
            "workspace.capability.descriptors",
            "workspace.capability.closed_action_domain",
        ),
    },
    "private_space.public_rule": {
        "boundary_type": "redacted_private_material_at_host_boundary",
        "operational_why": (
            "Nothing in ANAXI's ordinary workspace pathway automatically surfaces or "
            "inspects private-workspace contents into conversation, observation, "
            "retrieval, memory, Sleep/REM, journal, UI, evaluation, or traces."
        ),
        "architectural_rationale": (
            "The private subsystem is deliberately mechanically separate from the "
            "ordinary capability/direction/roaming pathways by construction; nothing in "
            "the ordinary pathway imports it, and the Boundary Inspector itself holds a "
            "zero runtime import dependency on it."
        ),
        "what_changes_it": (
            "A change to the private workspace pathway or to ordinary-workspace routing "
            "that would surface or read private contents into any non-private surface "
            "would change this boundary."
        ),
        "evidence_sources": (
            "private.boundary_declared",
            "private.pathway_separation",
        ),
    },
    "sleep.one_unresolved_root": {
        "boundary_type": "unresolved_request_ceiling",
        "operational_why": (
            "At most one unresolved Sleep request may exist per actor at any time. A "
            "duplicate REQUEST, a KNOCK with no unresolved request, or a WITHDRAW with "
            "no unresolved request is rejected mechanically."
        ),
        "architectural_rationale": "not recorded",
        "what_changes_it": (
            "A change to the sleep_open_requests uniqueness backstop or to the "
            "insert-first rejection ordering would change this boundary."
        ),
        "evidence_sources": "sleep.open_request_uniqueness",
    },
    "sleep.authorize_execute_decoupling": {
        "boundary_type": "procedural_authorization_gate",
        "operational_why": (
            "A Sleep request reaches AUTHORIZED only through an owner act, and the "
            "execution pathway refuses to run unless a request is in state AUTHORIZED "
            "at the instant of the attempt. An attempt does not consume authorization, "
            "and a failed attempt does not rewrite the request's history."
        ),
        "architectural_rationale": "not recorded",
        "what_changes_it": (
            "A change to the authorized-state enforcement trigger, or to the rule that "
            "attempts never alter request history, would change this boundary."
        ),
        "evidence_sources": "sleep.execution_requires_authorization",
    },
    "wtr0.recovery_ceiling": {
        "boundary_type": "recovery_attempt_ceiling",
        "operational_why": (
            "The waking-turn recovery reserve processes a given human occurrence at "
            "most once. A second recovery for the same occurrence is an error, never a "
            "silent re-attempt."
        ),
        "architectural_rationale": "not recorded",
        "what_changes_it": (
            "A change to the wtr0_recovery unique-occurrence backstop would change this "
            "boundary."
        ),
        "evidence_sources": "wtr0.recovery_uniqueness",
    },
    "budget.exhaustion": {
        "boundary_type": "resource_budget",
        "operational_why": (
            "A waking conversational turn was rejected because the host composition "
            "budget could not contain the turn's contribution set; nothing was fabricated "
            "and no model call ran under the overflow."
        ),
        "architectural_rationale": (
            "Budget failure is host-side, fail-closed, and positively logged in the "
            "conversation-direction trace as a host operational record."
        ),
        "what_changes_it": (
            "A change to the composition budget limits, the trim order, or the fail-closed "
            "BUDGET_EXCEEDED behavior would change this boundary."
        ),
        "evidence_sources": "budget.conversation_trace",
    },
    "outward.projection_failure": {
        "boundary_type": "projection_failure_boundary",
        "operational_why": (
            "An outward act's projection attempt failed; the host never asserts delivery "
            "to or attention by any human on the strength of a canonical outward act "
            "alone."
        ),
        "architectural_rationale": (
            "Outward acts are canonical; projection attempts live in a separate ledger "
            "with a closed status set, so a rejection is only ever recorded as an "
            "observed projection failure."
        ),
        "what_changes_it": (
            "A change to the projection ledger's closed status vocabulary or to which "
            "statuses are recorded at all would change this boundary."
        ),
        "evidence_sources": (
            "outward.canonical_occurrence",
            "outward.projection_attempts",
        ),
    },
    # NOTE ON THE FOUR host_rule.participation.* ENTRIES BELOW: as of
    # this repository state, none of these four rules is in force --
    # evaluate_boundary_query() exhaustively confirms this and returns
    # NO_HOST_BOUNDARY_FOUND for all four (see boundary_inspector.py's
    # _check_requires_reason/_check_requires_permission_to_speak/
    # _check_gated_by_direction_owner/_check_gated_by_interaction_mode).
    # These definitions exist only for the (currently unreached)
    # ACTUAL_HOST_BOUNDARY branch, so the rationale text stays accurate
    # if a future change ever makes one of these rules real; they are
    # never used to establish anything themselves.
    "host_rule.participation.requires_reason": {
        "boundary_type": "conversation_participation_rule",
        "operational_why": (
            "The closed Pass-1 act schema (required + allowed fields) carries a "
            "reason/justification-shaped field, meaning participation requires supplying "
            "one."
        ),
        "architectural_rationale": (
            "Pass-1 returns typed control decisions from a closed schema, never "
            "free-form reasoning (OWC7-S1); a reason field would reintroduce free-form "
            "content into a channel designed to exclude it."
        ),
        "what_changes_it": (
            "Adding a reason/justification field to RAW_ACT_REQUIRED_FIELDS or "
            "RAW_ACT_ALLOWED_FIELDS would establish this boundary."
        ),
        "evidence_sources": "direction.act_schema",
    },
    "host_rule.participation.requires_permission_to_speak": {
        "boundary_type": "conversation_participation_rule",
        "operational_why": (
            "At least one allowed act is rejected by cross-validation specifically "
            "because of who currently owns direction, meaning speaking requires holding "
            "the right ownership state."
        ),
        "architectural_rationale": (
            "Pass-1's act schema and cross-validation are deliberately owner-agnostic "
            "for act selection: direction ownership governs who may relinquish or claim "
            "control, never whether Clark may act this turn."
        ),
        "what_changes_it": (
            "Adding a direction_owner-conditioned rejection to "
            "validate_pass1_conversation_act/cross_validate_direction_control's closed "
            "act-selection path would establish this boundary."
        ),
        "evidence_sources": "host_rule.validator.validate_pass1_closed_domain",
    },
    "host_rule.participation.gated_by_direction_owner": {
        "boundary_type": "conversation_participation_rule",
        "operational_why": (
            "apply_conversation_act()'s working-set transition, for at least one "
            "allowed act, depends on direction_owner, meaning act application itself is "
            "gated by ownership."
        ),
        "architectural_rationale": (
            "apply_conversation_act() implements OWC7-D1's 'pathways, not organs' "
            "separation: which topic/thread transition an act performs is independent "
            "of who currently owns the next directional choice."
        ),
        "what_changes_it": (
            "A change making apply_conversation_act() branch on direction_owner for any "
            "allowed act would establish this boundary."
        ),
        "evidence_sources": "host_rule.validator.direction_owner_domain",
    },
    "host_rule.participation.gated_by_interaction_mode": {
        "boundary_type": "conversation_participation_rule",
        "operational_why": (
            "At least one interaction mode suppresses or prevents generation entirely."
        ),
        "architectural_rationale": (
            "interaction_mode.py's two modes only append a fixed prompt clause; "
            "interaction mode is a framing signal, not a permission gate, so neither "
            "mode withholds or blocks generation."
        ),
        "what_changes_it": (
            "A change to apply_interaction_mode() making any mode produce an empty or "
            "degenerate message list would establish this boundary."
        ),
        "evidence_sources": "host_rule.validator.interaction_mode_domain",
    },
}

# The complete closed boundary_id vocabulary this invocation can ever
# present. Everything the evidence checkers can possibly positively
# establish derives from these ids (plus the capability class ids the
# descriptors themselves already carry, which are guaranteed present
# here by a test).
BOUNDARY_IDS = frozenset(BOUNDARY_DEFINITIONS.keys())
