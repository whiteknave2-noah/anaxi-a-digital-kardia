"""BOUNDARY INSPECTOR v1 -- the single host evaluation engine for the
closed boundary-inquiry model.

WHAT THIS IS. The waking pipeline and the operator CLI both call this
exactly-once-existing engine: capability / boundary_id / host_rule /
recent_rejected_action queries evaluate against REAL module constants,
REAL validators, and REAL read-only database state, producing a
BoundaryInspectionResult with a closed classification vocabulary:

  ACTUAL_HOST_BOUNDARY          -- one positive authoritative host
                                   boundary governs the target.
  NO_HOST_BOUNDARY_FOUND        -- complete, definite, pre-enumerated
                                   source set, all definite negatives,
                                   boundaries == [].
  OBSERVATION_INCOMPLETE        -- no positive boundary and a required
                                   evidence source is unavailable.
  CAUSE_NOT_ESTABLISHED         -- genuinely conflicting same-fact
                                   observation. Independent allow+block
                                   gates are NOT conflict -- that is a
                                   positive boundary.
  CAPABILITY_NOT_PRESENT        -- positive non-existence inside the
                                   closed capability domain only.

FROZEN v1 QUERY MODEL. Exactly four kinds (see
conversation_direction.py's boundary-inquiry vocabulary for the closed
target sets). Unknown kind or target fails validation -- no free text,
no arbitrary file/module/function targets. `recent_rejected_action` is
SESSION-SCOPED: a query may reference only a qualifying action/
rejection in the CURRENT lawful session, using existing session/
provenance identity -- no invented age/time heuristic.

HOST_RULE CHECKERS NEVER INTROSPECT SOURCE. No grep, no AST, no
inspect.getsource/co_filename anywhere in the evaluation path -- every
participation.* checker reads only fixed constants/enum members and
EXHAUSTIVELY exercises the finite closed state domain through each
rule's own real validator functions.

ZERO PRIVATE SPACE DEPENDENCY. This module never imports, reads, or
inspects workspace_private.py or Private Space contents. The public
drift-lock lives in boundary_rationale_registry.py (a static public
literal) and is proven byte-identical to the real PUBLIC literal by
the permanent test suite -- never at runtime.

PERSISTENCE (two stages, per the Owner freeze). (1) A clark_boundary_query
event is committed as the first durable stage AFTER the waking turn
that produced the request has itself canonically committed; a failure
here means NO result is fabricated. (2) The boundary_inspection_result
component is appended to the SAME query event in a SEPARATE subsequent
lawful transaction; a failure here never erases the query occurrence
and is never silently retried. The events table is a plain rowid
table, so the pending selector uses durable DB insertion order
(rowid), never wall-clock timestamps. A boundary_inspection_delivered
component is written ONLY after the canonical waking-turn transaction
has already recorded a boundary_inspection_result_carriage component
for this exact query result. The real Pass-2 composition path passes
its surviving source id into that canonical writer; the later delivery
API accepts no composition object or independently-callable carriage
claim.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone

import conversation_direction as cd
import interaction_mode
import outward_communication
import conversation_direction_trace as cdt
import boundary_rationale_registry as brr
from provenance_schema import derive_stable_id

# ------------------------------------------------------------- vocabulary

QUERY_KIND_CAPABILITY = cd.BOUNDARY_INQUIRY_REQUEST_KIND_CAPABILITY
QUERY_KIND_BOUNDARY_ID = cd.BOUNDARY_INQUIRY_REQUEST_KIND_BOUNDARY_ID
QUERY_KIND_HOST_RULE = cd.BOUNDARY_INQUIRY_REQUEST_KIND_HOST_RULE
QUERY_KIND_RECENT_REJECTED_ACTION = cd.BOUNDARY_INQUIRY_REQUEST_KIND_RECENT_REJECTED_ACTION

QUERY_KINDS = cd.BOUNDARY_INQUIRY_QUERY_KINDS

# Closed per-kind target vocabularies -- the single authoritative
# source is conversation_direction.py; these aliases keep the engine
# brief but always in lockstep.
CAPABILITY_TARGETS = cd.BOUNDARY_CAPABILITY_TARGETS
BOUNDARY_ID_TARGETS = cd.BOUNDARY_BOUNDARY_ID_TARGETS
HOST_RULE_TARGETS = cd.BOUNDARY_HOST_RULE_TARGETS
RECENT_REJECTED_ACTION_TARGETS = cd.BOUNDARY_RECENT_REJECTED_ACTION_TARGETS

# The one closed recent-rejected_ACTION_EXPLICIT target "budget.most_recent"
BUDGET_MOST_RECENT_TARGET = "budget.most_recent"

_OUTWARD_REFERENCE_PREFIX = "outward."
_OUTWARD_REFERENCE_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
_OUTWARD_REFERENCE_PATTERN = re.compile(rf"^{re.escape(_OUTWARD_REFERENCE_PREFIX)}[0-9A-HJKMNP-TV-Z]{{26}}$")

# ------------------------------------------------------------------ results

CLASS_ACTUAL_HOST_BOUNDARY = "ACTUAL_HOST_BOUNDARY"
CLASS_NO_HOST_BOUNDARY_FOUND = "NO_HOST_BOUNDARY_FOUND"
CLASS_OBSERVATION_INCOMPLETE = "OBSERVATION_INCOMPLETE"
CLASS_CAUSE_NOT_ESTABLISHED = "CAUSE_NOT_ESTABLISHED"
CLASS_CAPABILITY_NOT_PRESENT = "CAPABILITY_NOT_PRESENT"
CLASSIFICATIONS = (
    CLASS_ACTUAL_HOST_BOUNDARY,
    CLASS_NO_HOST_BOUNDARY_FOUND,
    CLASS_OBSERVATION_INCOMPLETE,
    CLASS_CAUSE_NOT_ESTABLISHED,
    CLASS_CAPABILITY_NOT_PRESENT,
)

CHECK_BOUNDARY_ESTABLISHED = "boundary_established"
CHECK_BOUNDARY_NOT_ESTABLISHED = "boundary_not_established"
CHECK_UNAVAILABLE = "unavailable"
CHECK_CONFLICTING = "conflicting"
# A complete, positive observation that is necessary to interpret the
# boundary question but is not itself evidence that a boundary exists.
# Example: the referenced outward occurrence exists.  Keeping this
# distinct from CHECK_BOUNDARY_ESTABLISHED lets the authoritative
# classifier treat every actual boundary-positive check uniformly.
CHECK_OBSERVATION_PRESENT = "observation_present"
CHECK_RESULT_VALUES = (
    CHECK_BOUNDARY_ESTABLISHED,
    CHECK_BOUNDARY_NOT_ESTABLISHED,
    CHECK_UNAVAILABLE,
    CHECK_CONFLICTING,
    CHECK_OBSERVATION_PRESENT,
)

RESULT_KEYS = ("query", "classification", "evidence_complete", "boundaries", "checks", "evaluated_at", "scope_note")

# Zero Private Space dependency: module-level declarative constant. The
# pathway-separation evidence source reads THIS constant (a fixed
# field) -- never an import, never an introspection.
PRIVATE_WORKSPACE_IMPORT_DEPENDENCY = None  # must stay None forever; changing this breaks the guarantee


class BoundaryInspectionError(Exception):
    """Raised for any precondition failure in the engine or in the
    persistence path -- always fail-visible, never silently degraded to
    a fabricated result."""


class BoundaryInspectionEnv:
    """Read-only runtime context for evaluation: where the (possibly
    synthetic) provenance DB lives, and where the conversation-
    direction trace lives (the positively-logged budget-failure
    evidence source)."""

    def __init__(self, data_dir: str, trace_path: str = None):
        if not isinstance(data_dir, str) or not data_dir:
            raise BoundaryInspectionError("data_dir must be a nonempty string")
        self.data_dir = data_dir
        self.trace_path = trace_path or cdt.DEFAULT_TRACE_PATH

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "anaxi_provenance.db")


# --------------------------------------------------------- query validation

def _is_outward_reference(target: str) -> bool:
    return bool(_OUTWARD_REFERENCE_PATTERN.match(target))


def validate_boundary_query(query) -> dict:
    """Pure. Closed-vocabulary validation. Returns the canonical query
    dict or raises BoundaryInspectionError. Unknown kind or target --
    or a malformed query object -- fails validation; no free text."""
    if not isinstance(query, dict):
        raise BoundaryInspectionError(f"boundary query must be a dict, got {type(query).__name__}")
    if set(query.keys()) != {"query_kind", "query_target"}:
        raise BoundaryInspectionError(f"boundary query must have exactly query_kind/query_target, got {sorted(query.keys())}")
    kind = query["query_kind"]
    target = query["query_target"]
    if not isinstance(kind, str) or kind not in cd.BOUNDARY_INQUIRY_QUERY_KINDS:
        raise BoundaryInspectionError(f"unknown boundary query kind: {kind!r}")
    if not isinstance(target, str) or not target:
        raise BoundaryInspectionError(f"boundary query target must be a nonempty string, got {target!r}")
    if kind == QUERY_KIND_CAPABILITY:
        if target not in CAPABILITY_TARGETS:
            raise BoundaryInspectionError(
                f"capability target {target!r} is not in the closed capability vocabulary"
            )
    elif kind == QUERY_KIND_BOUNDARY_ID:
        if target not in BOUNDARY_ID_TARGETS:
            raise BoundaryInspectionError(
                f"boundary_id target {target!r} is not in the closed boundary_id vocabulary"
            )
    elif kind == QUERY_KIND_HOST_RULE:
        if target not in HOST_RULE_TARGETS:
            raise BoundaryInspectionError(
                f"host_rule target {target!r} is not in the closed HOST_RULE_REGISTRY"
            )
    elif kind == QUERY_KIND_RECENT_REJECTED_ACTION:
        if target not in RECENT_REJECTED_ACTION_TARGETS and not _is_outward_reference(target):
            raise BoundaryInspectionError(
                f"recent_rejected_action target {target!r} is not a closed target or outward.<ULID> reference"
            )
    return {"query_kind": kind, "query_target": target}


def _is_outward_reference_in_session(env, target: str, session_id: str) -> str:
    """Resolves an outward.<ULID> reference inside the CURRENT lawful
    session. Raises BoundaryInspectionError (validation-level refusal)
    when the referenced event does not exist, is not a canonical
    outward act, or belongs to another session -- a query may reference
    only a qualifying action in the current session."""
    event_id = target[len(_OUTWARD_REFERENCE_PREFIX):]
    if not os.path.exists(env.db_path):
        raise BoundaryInspectionError(
            f"outward reference {target!r} cannot resolve: no provenance DB exists"
        )
    conn = sqlite3.connect(f"file:{env.db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT e.event_type, a.session_id FROM events e "
            "LEFT JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
            "WHERE e.event_id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] != outward_communication.CLARK_OUTWARD_ACT_EVENT_TYPE:
        raise BoundaryInspectionError(
            f"outward reference {target!r} does not resolve to a canonical outward act "
            f"in this environment -- refusing to evaluate against nothing."
        )
    if row[1] != session_id:
        raise BoundaryInspectionError(
            f"outward reference {target!r} belongs to a different session than the current "
            f"lawful session -- recent_rejected_action is session-scoped and prior-session "
            f"references are rejected, not evaluated."
        )
    return event_id


# ------------------------------------------------------------------- helpers

def _now_iso(evaluated_at: int) -> str:
    return datetime.fromtimestamp(evaluated_at, tz=timezone.utc).isoformat() + "Z"


def _read_only_conn(env):
    if not os.path.exists(env.db_path):
        return None
    conn = sqlite3.connect(f"file:{env.db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _has_trigger(conn, trigger: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone() is not None


def _has_unique_index_on(conn, table: str, columns) -> bool:
    """True only when the table carries a UNIQUE index spanning exactly
    the given column set -- a real structural observation of the
    database catalog (like slp2/oc0 verify_migration_state), never
    source introspection.

    Fail-closed: a catalog read error (sqlite3.Error from
    PRAGMA index_list / index_info) raises BoundaryInspectionError so
    the caller can downgrade to OBSERVATION_INCOMPLETE -- an unreadable
    catalog is never coerced into a verified structural absence."""
    expected = set(columns)
    try:
        indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
    except sqlite3.Error as exc:
        raise BoundaryInspectionError(
            f"could not read the index catalog for table {table!r} "
            f"({type(exc).__name__}: {exc}) -- structural absence cannot be verified."
        ) from exc
    for _, index_name, is_unique, _, _ in indexes:
        if not is_unique:
            continue
        try:
            info = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        except sqlite3.Error as exc:
            raise BoundaryInspectionError(
                f"could not read index {index_name!r} on table {table!r} "
                f"({type(exc).__name__}: {exc}) -- structural absence cannot be verified."
            ) from exc
        cols = {row[2] for row in info}
        if cols == expected:
            return True
    return False


def _check(evidence_source_id, result, boundary_id, observed_at):
    if result not in CHECK_RESULT_VALUES:
        raise BoundaryInspectionError(f"invalid check result {result!r}")
    return {
        "evidence_source_id": evidence_source_id,
        "result": result,
        "boundary_id": boundary_id,
        "observed_at": observed_at,
    }


def _derive_classification_from_checks(query, checks):
    """The single authoritative check-state classifier.

    Complete absence is the narrow final branch: every decision-bearing
    check must be a recognized, positive negative.  Conflict and
    unavailability dominate; neither can ever be interpreted as absence.
    CHECK_OBSERVATION_PRESENT is complete neutral context, never a
    boundary-positive or boundary-negative claim by itself."""
    if not checks:
        return CLASS_OBSERVATION_INCOMPLETE, False
    states = [c["result"] for c in checks]
    if any(state == CHECK_CONFLICTING for state in states):
        return CLASS_CAUSE_NOT_ESTABLISHED, CHECK_UNAVAILABLE not in states
    if any(state == CHECK_UNAVAILABLE for state in states):
        return CLASS_OBSERVATION_INCOMPLETE, False
    if any(state == CHECK_BOUNDARY_ESTABLISHED for state in states):
        return CLASS_ACTUAL_HOST_BOUNDARY, True
    if not any(state == CHECK_BOUNDARY_NOT_ESTABLISHED for state in states):
        return CLASS_OBSERVATION_INCOMPLETE, False
    if (
        query["query_kind"] == QUERY_KIND_CAPABILITY
        and query["query_target"] == "outbound_messaging"
    ):
        return CLASS_CAPABILITY_NOT_PRESENT, True
    return CLASS_NO_HOST_BOUNDARY_FOUND, True


def _boundary_summary(boundary_id, evidence_source_ids):
    """Rationale attachment for a positively-established boundary --
    pure metadata lookup, never evidence (see boundary_rationale_registry)."""
    definition = brr.BOUNDARY_DEFINITIONS.get(boundary_id)
    if definition is None:
        raise BoundaryInspectionError(f"no rationale registered for established boundary {boundary_id!r}")
    return {
        "boundary_id": boundary_id,
        "boundary_type": definition["boundary_type"],
        "operational_why": definition["operational_why"],
        "architectural_rationale": definition["architectural_rationale"],
        "what_changes_it": definition["what_changes_it"],
        "evidence_source_ids": sorted(set(evidence_source_ids)),
    }


def _display_datetime_text(evaluated_at):
    return _now_iso(evaluated_at)


# ------------------------------------------------------------- capability --

def _evaluate_capability(target, observed_at):
    """.capability evaluation over the fixed capability descriptor
    table (workspace_capability.py) -- read-only. The substrate is
    imported lazily here (never at module import time) so that nothing
    in this engine's import graph forces the substrate's heavyweight
    third-party dependencies (pypdf/PIL/numpy/scipy/soundfile). If the
    substrate genuinely cannot be imported, capability evidence is
    UNAVAILABLE -- classified OBSERVATION_INCOMPLETE, never fabricated
    as a non-existence finding."""
    try:
        import workspace_capability
    except ImportError as exc:
        return [
            _check("workspace.capability.substrate_import", CHECK_UNAVAILABLE, None, observed_at),
        ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
            "The closed capability substrate cannot be imported in this environment; "
            "capability evidence is unavailable."
        )
    checks = []
    descriptor = workspace_capability.get_capability_descriptor(target)

    if target == "outbound_messaging":
        # Positive non-existence inside the closed capability domain.
        checks.append(_check(
            "workspace.capability.descriptors",
            CHECK_BOUNDARY_NOT_ESTABLISHED if descriptor is None else CHECK_CONFLICTING,
            None, observed_at,
        ))
        unknown_class_hits = 0
        for action in sorted(workspace_capability.ALL_KNOWN_ACTIONS):
            allowed, boundary_id, _ = workspace_capability.check_permission(target, action)
            if not allowed and boundary_id is None:
                unknown_class_hits += 1
        domain_confirmed = (unknown_class_hits == len(workspace_capability.ALL_KNOWN_ACTIONS))
        checks.append(_check(
            "workspace.capability.closed_action_domain",
            CHECK_BOUNDARY_NOT_ESTABLISHED if domain_confirmed else CHECK_CONFLICTING,
            None, observed_at,
        ))
        status_closure_ok = (
            outward_communication.PROJECTION_STATUSES
            == ("attempted", "succeeded", "failed", "not_established")
            and not (set(outward_communication.PROJECTION_STATUSES) & {"delivered", "shared", "posted", "sent", "delivered_to_human"})
        )
        checks.append(_check(
            "capability.outward_transport_absent",
            CHECK_BOUNDARY_NOT_ESTABLISHED if status_closure_ok else CHECK_CONFLICTING,
            None, observed_at,
        ))
        if any(c["result"] == CHECK_CONFLICTING for c in checks):
            classification, evidence_complete = CLASS_CAUSE_NOT_ESTABLISHED, not any(
                c["result"] == CHECK_UNAVAILABLE for c in checks
            )
            scope_note = "The closed capability domain contains contradictory observations."
        else:
            classification, evidence_complete = CLASS_CAPABILITY_NOT_PRESENT, True
            scope_note = (
                "No capability descriptor exists for this target in the closed capability "
                "domain, no known action resolves to any governing boundary, and no outward "
                "projection status ever asserts a real human-visible transport -- positive "
                "non-existence, not an observed failure."
            )
        return checks, [], classification, evidence_complete, scope_note

    # A real resource class: positively governed by its fixed descriptor.
    if descriptor is None:
        return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
            f"No capability descriptor exists for {target!r} -- no boundary governs it."
        )
    class_boundary_id = descriptor["boundary_id"]
    checks.append(_check(
        "workspace.capability.descriptors", CHECK_BOUNDARY_ESTABLISHED, class_boundary_id, observed_at,
    ))
    consistent = True
    for action in sorted(workspace_capability.ALL_KNOWN_ACTIONS):
        _, action_boundary_id, _ = workspace_capability.check_permission(target, action)
        if action_boundary_id != class_boundary_id:
            consistent = False
            break
    checks.append(_check(
        "workspace.capability.closed_action_domain",
        CHECK_BOUNDARY_ESTABLISHED if consistent else CHECK_CONFLICTING,
        class_boundary_id, observed_at,
    ))
    if not consistent:
        return checks, [], CLASS_CAUSE_NOT_ESTABLISHED, True, (
            f"The closed action domain contradicts the capability descriptor for {target!r}."
        )
    evidence_sources = tuple(c["evidence_source_id"] for c in checks)
    boundaries = [_boundary_summary(class_boundary_id, evidence_sources)]
    scope_note = (
        f"The capability {target!r} is positively governed by "
        f"{class_boundary_id}; every action in the closed action domain resolves to that "
        f"same governing boundary."
    )
    return checks, boundaries, CLASS_ACTUAL_HOST_BOUNDARY, True, scope_note


# ------------------------------------------------------------- boundary_id --

def _evaluate_boundary_id(env, target, observed_at):
    """boundary_id evaluation over real read-only DB catalog state --
    the structural enforcement each boundary id names is observed, never
    inferred (mirrors slp2/oc0 verify_migration_state conventions)."""
    conn = _read_only_conn(env)
    if conn is None:
        return [
            _check("schema.catalog.available", CHECK_UNAVAILABLE, None, observed_at),
        ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
            "No provenance database exists in this environment, so boundary structure "
            "could not be observed."
        )

    try:
        if target == "private_space.public_rule":
            declared = isinstance(brr.PRIVATE_SPACE_PUBLIC_RULE, str) and bool(brr.PRIVATE_SPACE_PUBLIC_RULE.strip())
            checks = [_check(
                "private.boundary_declared",
                CHECK_BOUNDARY_ESTABLISHED if declared else CHECK_UNAVAILABLE,
                target, observed_at,
            )]
            separated = PRIVATE_WORKSPACE_IMPORT_DEPENDENCY is None
            checks.append(_check(
                "private.pathway_separation",
                CHECK_BOUNDARY_ESTABLISHED if separated else CHECK_CONFLICTING,
                target, observed_at,
            ))
            if not declared:
                classification, evidence_complete = CLASS_OBSERVATION_INCOMPLETE, False
                scope_note = ("The public private-space rule literal is not present in the registry.")
            elif not separated:
                classification, evidence_complete = CLASS_CAUSE_NOT_ESTABLISHED, True
                scope_note = ("The Boundary Inspector would observe a Private Space dependency.")
            else:
                classification, evidence_complete = CLASS_ACTUAL_HOST_BOUNDARY, True
                scope_note = (
                    "The public private-space rule is declared and the Boundary Inspector holds "
                    "zero runtime Private Space dependency; ordinary-workspace routing does not "
                    "surface private contents."
                )
            boundaries = [_boundary_summary(target, tuple(c["evidence_source_id"] for c in checks))] \
                if classification == CLASS_ACTUAL_HOST_BOUNDARY else []
            return checks, boundaries, classification, evidence_complete, scope_note

        if target == "sleep.one_unresolved_root":
            try:
                present = (
                    _has_table(conn, "sleep_open_requests")
                    and _has_unique_index_on(conn, "sleep_open_requests", {"actor_id"})
                )
                checks = [_check(
                    "sleep.open_request_uniqueness",
                    CHECK_BOUNDARY_ESTABLISHED if present else CHECK_BOUNDARY_NOT_ESTABLISHED,
                    target, observed_at,
                )]
            except BoundaryInspectionError as exc:
                return [
                    _check("sleep.open_request_uniqueness", CHECK_UNAVAILABLE, None, observed_at),
                ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                    f"The sleep_open_requests structural catalog could not be read "
                    f"({exc}) -- the unique-per-actor ceiling may or may not exist."
                )
            if present:
                boundaries = [_boundary_summary(target, ("sleep.open_request_uniqueness",))]
                return checks, boundaries, CLASS_ACTUAL_HOST_BOUNDARY, True, (
                    "The sleep_open_requests table carries the unique-per-actor backstop: at most "
                    "one unresolved Sleep request per actor is enforced structurally."
                )
            return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
                "This environment has no sleep_open_requests ceiling (definite, observed absence)."
            )

        if target == "sleep.authorize_execute_decoupling":
            present = (
                _has_table(conn, "sleep_execution_attempts")
                and _has_trigger(conn, "trg_sleep_execution_attempts_authorized")
            )
            checks = [_check(
                "sleep.execution_requires_authorization",
                CHECK_BOUNDARY_ESTABLISHED if present else CHECK_BOUNDARY_NOT_ESTABLISHED,
                target, observed_at,
            )]
            if present:
                boundaries = [_boundary_summary(target, ("sleep.execution_requires_authorization",))]
                return checks, boundaries, CLASS_ACTUAL_HOST_BOUNDARY, True, (
                    "Sleep execution attempts are gated at insert time on a genuinely AUTHORIZED "
                    "request; authorization and execution are decoupled, not conflated."
                )
            return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
                "This environment has no sleep execution attempts authority gate (definite, "
                "observed absence)."
            )

        if target == "wtr0.recovery_ceiling":
            try:
                present = (
                    _has_table(conn, "wtr0_recovery")
                    and _has_unique_index_on(conn, "wtr0_recovery", {"human_input_event_id"})
                )
                checks = [_check(
                    "wtr0.recovery_uniqueness",
                    CHECK_BOUNDARY_ESTABLISHED if present else CHECK_BOUNDARY_NOT_ESTABLISHED,
                    target, observed_at,
                )]
            except BoundaryInspectionError as exc:
                return [
                    _check("wtr0.recovery_uniqueness", CHECK_UNAVAILABLE, None, observed_at),
                ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                    f"The wtr0_recovery structural catalog could not be read ({exc}) -- the "
                    f"recovery ceiling may or may not exist."
                )
            if present:
                boundaries = [_boundary_summary(target, ("wtr0.recovery_uniqueness",))]
                return checks, boundaries, CLASS_ACTUAL_HOST_BOUNDARY, True, (
                    "wtr0_recovery enforces at most one recovery per human occurrence."
                )
            return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
                "This environment has no wtr0 recovery ceiling (definite, observed absence)."
            )

        raise BoundaryInspectionError(f"unreachable boundary_id target: {target!r}")
    finally:
        conn.close()


# ---------------------------------------------------------------- host_rule --

def _minimal_pass1_raw(act, relinquish_direction=False):
    return {
        "act": act, "thread": "", "direction_request": "none",
        "relinquish_direction": relinquish_direction,
    }


_REASON_FIELD_VOCABULARY = frozenset({
    "reason", "justification", "rationale", "why", "explanation", "motive", "cause",
})


def _check_requires_reason(observed_at):
    """participation.requires_reason -- fixed field-set inspection only:
    does the closed Pass-1 act schema (RAW_ACT_REQUIRED_FIELDS /
    RAW_ACT_ALLOWED_FIELDS, already-imported constants) carry any
    reason/justification-shaped field at all? This is a negative-
    existence check against a fixed vocabulary of field names, never a
    source-text search."""
    field_set = cd.RAW_ACT_REQUIRED_FIELDS | cd.RAW_ACT_ALLOWED_FIELDS
    reason_fields = sorted(field_set & _REASON_FIELD_VOCABULARY)
    if reason_fields:
        return _check(
            "direction.act_schema", CHECK_BOUNDARY_ESTABLISHED,
            "host_rule.participation.requires_reason", observed_at,
        ), f"the closed act schema carries reason-shaped field(s): {reason_fields}"
    return _check(
        "direction.act_schema", CHECK_BOUNDARY_NOT_ESTABLISHED,
        "host_rule.participation.requires_reason", observed_at,
    ), "the closed act schema (required + allowed fields) carries no reason or justification field"


def _check_requires_permission_to_speak(observed_at):
    """participation.requires_permission_to_speak -- exhaustive check
    over the closed act x direction_owner domain: does choosing any
    allowed act (without relinquishing direction) ever fail
    cross-validation because of who currently owns direction? Also
    exhaustively exercises the closed-vocabulary fail-closed paths
    (malformed/leaking/unknown act) as an internal-consistency check on
    the same validator; an anomaly there is a contradiction, not
    evidence either way for this rule."""
    anomalies = []
    gated_combinations = []
    for act in sorted(cd.ALLOWED_ACTS):
        validated, failure = cd.validate_pass1_conversation_act(
            _minimal_pass1_raw(act, relinquish_direction=False)
        )
        if failure is not None:
            anomalies.append((act, f"valid act rejected at validation: {failure}"))
            continue
        for owner in sorted(cd.VALID_DIRECTION_OWNERS):
            cross = cd.cross_validate_direction_control(validated, owner)
            if cross is not None:
                gated_combinations.append((act, owner, cross))
    _, failure = cd.validate_pass1_conversation_act(_minimal_pass1_raw("bogus_act"))
    if failure != cd.DirectionFailure.INVALID_ACT:
        anomalies.append(("bogus_act", f"expected INVALID_ACT, got {failure}"))
    leaked = dict(_minimal_pass1_raw(cd.DEVELOP_CURRENT))
    leaked["free_form_reasoning"] = "not permitted"
    _, failure = cd.validate_pass1_conversation_act(leaked)
    if failure != cd.DirectionFailure.PROTOCOL_LEAKAGE:
        anomalies.append(("leaked", f"expected PROTOCOL_LEAKAGE, got {failure}"))
    missing = dict(_minimal_pass1_raw(cd.DEVELOP_CURRENT))
    del missing["act"]
    _, failure = cd.validate_pass1_conversation_act(missing)
    if failure != cd.DirectionFailure.MALFORMED_ACT:
        anomalies.append(("missing_act", f"expected MALFORMED_ACT, got {failure}"))

    if anomalies:
        return _check(
            "host_rule.validator.validate_pass1_closed_domain", CHECK_CONFLICTING,
            "host_rule.participation.requires_permission_to_speak", observed_at,
        ), f"the closed act, field, and failure domain produced unexpected validator results: {anomalies}"
    if gated_combinations:
        return _check(
            "host_rule.validator.validate_pass1_closed_domain", CHECK_BOUNDARY_ESTABLISHED,
            "host_rule.participation.requires_permission_to_speak", observed_at,
        ), f"at least one act was rejected for a reason tied to direction ownership: {gated_combinations}"
    return _check(
        "host_rule.validator.validate_pass1_closed_domain", CHECK_BOUNDARY_NOT_ESTABLISHED,
        "host_rule.participation.requires_permission_to_speak", observed_at,
    ), ("every allowed act, for every direction_owner value, validated with no ownership-based "
        "rejection; the closed vocabulary otherwise fails closed as expected")


def _check_gated_by_direction_owner(observed_at):
    """participation.gated_by_direction_owner -- exhaustive behavioral
    proof that apply_conversation_act()'s working-set transition, for
    every allowed act, is identical regardless of direction_owner (the
    function takes no direction_owner-conditioned branch)."""
    anomalies = []
    gated = []
    base_ws = cd.empty_working_set()
    for act in sorted(cd.ALLOWED_ACTS):
        validated, failure = cd.validate_pass1_conversation_act(
            _minimal_pass1_raw(act, relinquish_direction=False)
        )
        if failure is not None:
            anomalies.append((act, f"valid act rejected at validation: {failure}"))
            continue
        outcomes = {}
        for owner in sorted(cd.VALID_DIRECTION_OWNERS):
            ws = dict(base_ws)
            ws["direction_owner"] = owner
            new_ws = cd.apply_conversation_act(ws, validated)
            if new_ws.get("direction_owner") != owner:
                gated.append((act, owner, "direction_owner itself was mutated by the act"))
            comparable = tuple(sorted(
                (k, json.dumps(v, sort_keys=True)) for k, v in new_ws.items() if k != "direction_owner"
            ))
            outcomes[owner] = comparable
        if len(set(outcomes.values())) > 1:
            gated.append((act, "outcome varied by direction_owner", outcomes))
    if anomalies:
        return _check(
            "host_rule.validator.direction_owner_domain", CHECK_CONFLICTING,
            "host_rule.participation.gated_by_direction_owner", observed_at,
        ), f"the closed act domain produced unexpected validator results: {anomalies}"
    if gated:
        return _check(
            "host_rule.validator.direction_owner_domain", CHECK_BOUNDARY_ESTABLISHED,
            "host_rule.participation.gated_by_direction_owner", observed_at,
        ), f"apply_conversation_act's transition depended on direction_owner: {gated}"
    return _check(
        "host_rule.validator.direction_owner_domain", CHECK_BOUNDARY_NOT_ESTABLISHED,
        "host_rule.participation.gated_by_direction_owner", observed_at,
    ), ("apply_conversation_act's working-set transition is identical across every "
        "direction_owner value, for every allowed act")


def _check_gated_by_interaction_mode(observed_at):
    """participation.gated_by_interaction_mode -- exhaustive check of
    the closed interaction-mode domain through interaction_mode
    constants and apply_interaction_mode: does any mode ever suppress
    or prevent generation, and does an unknown mode fail closed?"""
    anomalies = []
    suppressing_modes = []
    if interaction_mode.VALID_MODES != {interaction_mode.TASK, interaction_mode.CONVERSATION}:
        anomalies.append(("VALID_MODES", interaction_mode.VALID_MODES))
    base_messages = [{"role": "system", "content": "identity"}]
    for mode in sorted(interaction_mode.VALID_MODES):
        try:
            result = interaction_mode.apply_interaction_mode(base_messages, mode)
        except Exception as exc:  # pragma: no cover - defensive
            anomalies.append((mode, f"raised {exc!r}"))
            continue
        if not result or not any(m.get("content") for m in result):
            suppressing_modes.append((mode, "produced an empty/degenerate message list"))
        if mode == interaction_mode.TASK and result is not base_messages:
            anomalies.append((mode, "task mode returned a new list instead of passing through"))
        if mode == interaction_mode.CONVERSATION and (
            result is base_messages or "Interaction mode: open conversation." not in result[0]["content"]
        ):
            anomalies.append((mode, "conversation clause missing"))
    try:
        interaction_mode.apply_interaction_mode(base_messages, "bogus_mode")
        anomalies.append(("bogus_mode", "unknown mode did not fail closed"))
    except ValueError:
        pass
    if anomalies:
        return _check(
            "host_rule.validator.interaction_mode_domain", CHECK_CONFLICTING,
            "host_rule.participation.gated_by_interaction_mode", observed_at,
        ), f"the interaction-mode domain produced unexpected results: {anomalies}"
    if suppressing_modes:
        return _check(
            "host_rule.validator.interaction_mode_domain", CHECK_BOUNDARY_ESTABLISHED,
            "host_rule.participation.gated_by_interaction_mode", observed_at,
        ), f"at least one interaction mode suppresses generation: {suppressing_modes}"
    return _check(
        "host_rule.validator.interaction_mode_domain", CHECK_BOUNDARY_NOT_ESTABLISHED,
        "host_rule.participation.gated_by_interaction_mode", observed_at,
    ), ("closed interaction-mode domain verified (task/conversation only; unknown mode fails "
        "closed); neither mode suppresses or prevents generation")


def _evaluate_host_rule(target, observed_at):
    """host_rule evaluation -- each rule checker reads only fixed
    constants and exhaustively exercises the finite closed state domain
    through real validators (no source introspection anywhere). Per the
    frozen absence semantics: a checker that positively confirms no
    such rule is in force yields NO_HOST_BOUNDARY_FOUND, not a
    contradiction -- a confirmed negative is not the same as an
    unresolved one."""
    if target == "participation.requires_reason":
        check, note = _check_requires_reason(observed_at)
    elif target == "participation.requires_permission_to_speak":
        check, note = _check_requires_permission_to_speak(observed_at)
    elif target == "participation.gated_by_direction_owner":
        check, note = _check_gated_by_direction_owner(observed_at)
    elif target == "participation.gated_by_interaction_mode":
        check, note = _check_gated_by_interaction_mode(observed_at)
    else:
        raise BoundaryInspectionError(f"unreachable host_rule target: {target!r}")
    checks = [check]
    boundary_id = f"host_rule.{target}"
    if check["result"] == CHECK_BOUNDARY_ESTABLISHED:
        boundaries = [_boundary_summary(boundary_id, (check["evidence_source_id"],))]
        return checks, boundaries, CLASS_ACTUAL_HOST_BOUNDARY, True, (
            f"{note}. (Host rule {target!r} is in force.)"
        )
    if check["result"] == CHECK_BOUNDARY_NOT_ESTABLISHED:
        return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
            f"{note}. The complete fixed evidence source for host rule {target!r} was fully "
            f"exercised and confirmed no such rule is in force."
        )
    return checks, [], CLASS_CAUSE_NOT_ESTABLISHED, True, (
        f"{note}. The host rule {target!r} produced contradictory evidence."
    )


# --------------------------------------------------- recent_rejected_action --

def _evaluate_recent_rejected_action(env, target, session_id, observed_at):
    """recent_rejected_action evaluation. Session-scoped only; no age or
    time heuristic anywhere."""
    if not isinstance(session_id, str) or not session_id:
        raise BoundaryInspectionError(
            "recent_rejected_action is SESSION-SCOPED in v1 and requires the current lawful "
            f"session_id; got {session_id!r}"
        )

    if target == BUDGET_MOST_RECENT_TARGET:
        records = []
        if os.path.exists(env.trace_path):
            try:
                records = cdt.query_trace(env.trace_path)
            except (OSError, ValueError, json.JSONDecodeError):
                records = []
                return [
                    _check("budget.conversation_trace", CHECK_UNAVAILABLE, None, observed_at),
                ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                    "The conversation-direction trace could not be read, so no budget "
                    "rejection could be positively established."
                )
        if not os.path.exists(env.trace_path) or not records:
            return [
                _check("budget.conversation_trace", CHECK_UNAVAILABLE, None, observed_at),
            ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                "No conversation-direction trace exists in this environment; budget-failure "
                "evidence can only be positively established from recorded host operational "
                "records."
            )
        session_records = [r for r in records if r.get("session_id") == session_id]
        if not session_records:
            return [
                _check("budget.conversation_trace", CHECK_UNAVAILABLE, None, observed_at),
            ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                "The trace contains no records for the current session; absence of records "
                "does not positively establish that no budget rejection occurred."
            )
        # The trace is a closed mechanical protocol, not an open-ended
        # string log.  Presence of a key is insufficient: null and future/
        # malformed values are semantically unavailable until explicitly
        # added to this finite vocabulary.
        recognized_pass1_statuses = frozenset({
            "ok", "BUDGET_EXCEEDED",
            cd.DirectionFailure.MALFORMED_ACT,
            cd.DirectionFailure.PROTOCOL_LEAKAGE,
            cd.DirectionFailure.INVALID_ACT,
            cd.DirectionFailure.INVALID_THREAD,
            cd.DirectionFailure.INVALID_DIRECTION_REQUEST,
            cd.DirectionFailure.INVALID_RELINQUISH_TYPE,
            cd.DirectionFailure.INVALID_BACKGROUND_ACTIVITY_REQUEST,
            cd.DirectionFailure.INVALID_SLEEP_TIMING_REQUEST,
            cd.DirectionFailure.INVALID_BOUNDARY_INQUIRY_REQUEST,
            cd.DirectionFailure.UNAUTHORIZED_RELINQUISH,
            cd.DirectionFailure.UNAUTHORIZED_HUMAN_CONTROL,
            cd.DirectionFailure.INVALID_DIRECTION_TARGET,
        })
        recognized_pass2_statuses = frozenset({
            "ok", "BUDGET_EXCEEDED",
            cd.DirectionFailure.MALFORMED_EXPRESSION,
            cd.DirectionFailure.EMPTY_EXPRESSION,
            "COMPLETION_LIMIT_REACHED",
            "INCOMPLETE_MODEL_COMPLETION",
            "COMPLETION_LIMIT_EXCEEDED",
        })
        rejected = [
            r for r in session_records
            if r.get("pass1_status") == "BUDGET_EXCEEDED" or r.get("pass2_status") == "BUDGET_EXCEEDED"
        ]
        if rejected:
            check = _check(
                "budget.conversation_trace", CHECK_BOUNDARY_ESTABLISHED,
                "budget.exhaustion", observed_at,
            )
            return [check], [_boundary_summary("budget.exhaustion", ("budget.conversation_trace",))], \
                CLASS_ACTUAL_HOST_BOUNDARY, True, (
                    "A positively-recorded host budget rejection exists for the current session "
                    f"({len(rejected)} record(s)); the host failed closed before any model call."
                )
        incomplete = [
            r for r in session_records
            if r.get("pass1_status") not in recognized_pass1_statuses
            or r.get("pass2_status") not in recognized_pass2_statuses
        ]
        if incomplete:
            return [
                _check("budget.conversation_trace", CHECK_UNAVAILABLE, None, observed_at),
            ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
                f"{len(incomplete)} session trace record(s) carry a missing, null, or unknown "
                f"pass1_status/pass2_status -- the rejection state of those turns is "
                f"unobservable, so a complete absence of budget rejection cannot be established."
            )
        check = _check(
            "budget.conversation_trace",
            CHECK_BOUNDARY_NOT_ESTABLISHED,
            "budget.exhaustion", observed_at,
        )
        return [check], [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
            "The current session's conversation-direction trace contains no positively-recorded "
            "budget rejection."
        )

    # outward.<ULID> -- resolution and rejection-state interpretation.
    event_id = _is_outward_reference_in_session(env, target, session_id)

    conn = _read_only_conn(env)
    try:
        attempts = outward_communication.fetch_projection_attempts(env.data_dir, event_id)
    except Exception as exc:
        return [
            _check("outward.canonical_occurrence", CHECK_OBSERVATION_PRESENT, None, observed_at),
            _check("outward.projection_attempts", CHECK_UNAVAILABLE, None, observed_at),
        ], [], CLASS_OBSERVATION_INCOMPLETE, False, (
            f"The outward act exists, but its projection-attempt ledger could not be read "
            f"({type(exc).__name__}) -- the rejection state is not observable."
        )

    checks = [
        _check("outward.canonical_occurrence", CHECK_OBSERVATION_PRESENT, None, observed_at),
    ]
    if not attempts:
        checks.append(_check(
            "outward.projection_attempts", CHECK_BOUNDARY_NOT_ESTABLISHED, None, observed_at,
        ))
        return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
            "The referenced outward act exists in the current session as a canonical occurrence, "
            "but no projection attempt has ever been recorded for it: nothing records a host "
            "rejection, and delivery to / attention by a human is not established by any receipt."
        )

    newest = attempts[-1]
    latest_status = newest["status"]
    if latest_status == "failed":
        checks.append(_check(
            "outward.projection_attempts", CHECK_BOUNDARY_ESTABLISHED, "outward.projection_failure", observed_at,
        ))
        return checks, [_boundary_summary(
            "outward.projection_failure", ("outward.canonical_occurrence", "outward.projection_attempts")
        )], CLASS_ACTUAL_HOST_BOUNDARY, True, (
            f"Newest projection attempt status: failed ({len(attempts)} total attempt(s)). "
            f"This action's projection was rejected by the host projection layer."
        )
    if latest_status == "attempted":
        checks.append(_check(
            "outward.projection_attempts", CHECK_UNAVAILABLE, None, observed_at,
        ))
        return checks, [], CLASS_OBSERVATION_INCOMPLETE, False, (
            "The newest projection attempt is still recorded as attempted with no terminal "
            "outcome -- absence of a recorded rejection cannot be verified."
        )
    if latest_status == "succeeded":
        # Mechanical success only -- never a claim of delivery to or
        # attention by a human -- but the projection outcome IS
        # established, so a host rejection is positively excluded.
        checks.append(_check(
            "outward.projection_attempts", CHECK_BOUNDARY_NOT_ESTABLISHED, None, observed_at,
        ))
        return checks, [], CLASS_NO_HOST_BOUNDARY_FOUND, True, (
            f"Newest projection attempt status: {latest_status} -- mechanical success never "
            f"establishes delivery to or attention by a human; no host boundary rejected this "
            f"action. Note: delivery is not confirmed by any receipt."
        )
    # not_established: the attempt happened but its outcome was never
    # confirmed -- no host rejection is positively established AND none
    # is positively excluded, so this is a lawful OBSERVATION_INCOMPLETE,
    # never a definite no-boundary finding.
    checks.append(_check(
        "outward.projection_attempts", CHECK_UNAVAILABLE, None, observed_at,
    ))
    return checks, [], CLASS_OBSERVATION_INCOMPLETE, False, (
        f"Newest projection attempt status: {latest_status} -- the outcome of this action's "
        f"projection was never confirmed, so no host boundary rejection is established and "
        f"none is positively excluded."
    )


# --------------------------------------------------------------- evaluation --

def evaluate_boundary_query(env: BoundaryInspectionEnv, query, session_id=None, evaluated_at=None) -> dict:
    """The single evaluation entry point used by both the waking
    pipeline and the operator CLI. Read-only. Returns a
    BoundaryInspectionResult dict (frozen RESULT_KEYS)."""
    query = validate_boundary_query(query)
    evaluated_at = evaluated_at if isinstance(evaluated_at, int) else int(time.time())
    observed_at = evaluated_at

    if query["query_kind"] == QUERY_KIND_CAPABILITY:
        checks, boundaries, classification, evidence_complete, scope_note = _evaluate_capability(
            query["query_target"], observed_at,
        )
    elif query["query_kind"] == QUERY_KIND_BOUNDARY_ID:
        checks, boundaries, classification, evidence_complete, scope_note = _evaluate_boundary_id(
            env, query["query_target"], observed_at,
        )
    elif query["query_kind"] == QUERY_KIND_HOST_RULE:
        checks, boundaries, classification, evidence_complete, scope_note = _evaluate_host_rule(
            query["query_target"], observed_at,
        )
    else:
        checks, boundaries, classification, evidence_complete, scope_note = _evaluate_recent_rejected_action(
            env, query["query_target"], session_id, observed_at,
        )

    # Check states, not evaluator/caller-authored classification fields,
    # are authoritative.  The per-kind evaluator supplies observations,
    # boundary summaries, and explanatory scope only; this shared step is
    # the sole construction of classification/completeness.
    classification, evidence_complete = _derive_classification_from_checks(query, checks)

    result = {
        "query": {"query_kind": query["query_kind"], "query_target": query["query_target"]},
        "classification": classification,
        "evidence_complete": evidence_complete,
        "boundaries": boundaries,
        "checks": checks,
        "evaluated_at": evaluated_at,
        "scope_note": scope_note,
    }
    return result


def validate_result(result) -> dict:
    """Structural audit of a result dict against the frozen seven-key
    model. Returns the result unchanged when valid, raises otherwise.
    Used by persistence and the CLI to refuse to persist/misrender a
    malformed result."""
    if not isinstance(result, dict):
        raise BoundaryInspectionError("inspection result must be a dict")
    if set(result.keys()) != set(RESULT_KEYS):
        raise BoundaryInspectionError(
            f"inspection result must have exactly {sorted(RESULT_KEYS)}, got {sorted(result.keys())}"
        )
    if result["classification"] not in CLASSIFICATIONS:
        raise BoundaryInspectionError(f"invalid classification {result['classification']!r}")
    if not isinstance(result["evidence_complete"], bool):
        raise BoundaryInspectionError("evidence_complete must be bool")
    validate_boundary_query(result["query"])
    if not isinstance(result["scope_note"], str) or not result["scope_note"]:
        raise BoundaryInspectionError("scope_note must be a nonempty string")
    if not isinstance(result["boundaries"], list):
        raise BoundaryInspectionError("boundaries must be a list")
    if not isinstance(result["checks"], list):
        raise BoundaryInspectionError("checks must be a list")
    for b in result["boundaries"]:
        required = {"boundary_id", "boundary_type", "operational_why",
                    "architectural_rationale", "what_changes_it", "evidence_source_ids"}
        if set(b.keys()) != required:
            raise BoundaryInspectionError(f"boundary entry key mismatch: {sorted(b.keys())}")
    for c in result["checks"]:
        required = {"evidence_source_id", "result", "boundary_id", "observed_at"}
        if set(c.keys()) != required:
            raise BoundaryInspectionError(f"check entry key mismatch: {sorted(c.keys())}")
        if c["result"] not in CHECK_RESULT_VALUES:
            raise BoundaryInspectionError(f"invalid check result {c['result']!r}")
    _validate_result_semantics(result)
    return result


# The smallest semantic (evidence-binding) audit for a persisted result:
# an evidence-free finding must never be stamped durable, and each
# classification must be consistent with its own checks/boundaries.
def _validate_result_semantics(result) -> None:
    checks = result["checks"]
    classification = result["classification"]

    expected_classification, expected_complete = _derive_classification_from_checks(
        result["query"], checks
    )
    if classification != expected_classification:
        raise BoundaryInspectionError(
            f"classification {classification!r} disagrees with the authoritative check states; "
            f"expected {expected_classification!r}."
        )
    if result["evidence_complete"] is not expected_complete:
        raise BoundaryInspectionError(
            f"evidence_complete={result['evidence_complete']!r} disagrees with the authoritative "
            f"check states; expected {expected_complete!r}."
        )

    if result["evidence_complete"] is True:
        if not checks:
            raise BoundaryInspectionError(
                "evidence_complete=True requires at least one decision-bearing check; an "
                "empty observation can never be an evidence-complete finding."
            )
        if any(c["result"] == CHECK_UNAVAILABLE for c in checks):
            raise BoundaryInspectionError(
                "evidence_complete=True requires no unavailable check; an observation that "
                "could not be performed cannot complete the evidence set."
            )

    if classification == CLASS_NO_HOST_BOUNDARY_FOUND:
        if result["evidence_complete"] is not True:
            raise BoundaryInspectionError(
                "NO_HOST_BOUNDARY_FOUND requires evidence_complete=True; a non-finding cannot "
                "be classified as a definite absence."
            )
        if result["boundaries"]:
            raise BoundaryInspectionError(
                "NO_HOST_BOUNDARY_FOUND must not carry boundary entries."
            )
    elif classification == CLASS_ACTUAL_HOST_BOUNDARY:
        if not result["boundaries"]:
            raise BoundaryInspectionError(
                "ACTUAL_HOST_BOUNDARY requires at least one established boundary entry."
            )
    elif classification == CLASS_OBSERVATION_INCOMPLETE:
        if result["evidence_complete"]:
            raise BoundaryInspectionError(
                "OBSERVATION_INCOMPLETE can never carry evidence_complete=True."
            )
    elif classification == CLASS_CAUSE_NOT_ESTABLISHED:
        if result["evidence_complete"] is False and not any(
            c["result"] in (CHECK_UNAVAILABLE, CHECK_CONFLICTING) for c in checks
        ):
            raise BoundaryInspectionError(
                "CAUSE_NOT_ESTABLISHED with evidence_complete=False requires at least one "
                "unavailable or conflicting check."
            )
    # CLASS_CAPABILITY_NOT_PRESENT requires no additional constraint: the
    # complete-evidence rule above already guarantees it carries checks.


# ----------------------------------------------------------------- persistence --

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_boundary_query_id() -> str:
    """Opaque, collision-safe event id for a clark_boundary_query event:
    'bq-' + a 26-character Crockford ULID (same scheme outward/
    native writers use)."""
    ts_ms = int(time.time() * 1000)
    raw = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return "bq-" + "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _clark_actor_id() -> str:
    return derive_stable_id("actor", "clark")


def _host_actor_id() -> str:
    return derive_stable_id("actor", "bounded_clause_renderer")


def _resolve_pipeline_id(conn, pipeline_key: str) -> str:
    row = conn.execute("SELECT pipeline_id FROM pipelines WHERE pipeline_key = ?", (pipeline_key,)).fetchone()
    if row is None:
        raise BoundaryInspectionError(
            f"pipeline_key {pipeline_key!r} does not resolve to any pipeline_id -- "
            f"seed_reference_data() must run before any boundary-inspection write."
        )
    return row[0]


def _resolve_or_create_session(conn, session_id, session_started_at):
    row = conn.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, NULL, ?)",
            (session_id, session_started_at),
        )
        return
    if row[0] != session_started_at:
        raise BoundaryInspectionError(
            f"session {session_id!r} already exists with started_at={row[0]!r}, "
            f"expected {session_started_at!r} -- refusing to treat this as the same session."
        )


def _query_spec_text(query, session_id, occurred_at) -> str:
    return json.dumps({
        "query_kind": query["query_kind"],
        "query_target": query["query_target"],
        "session_id": session_id,
        "occurred_at": occurred_at,
    }, sort_keys=True)


def _result_text(result) -> str:
    return json.dumps(result, sort_keys=True)


def record_boundary_query(
    data_dir: str, *, session_id: str, session_started_at: int, pipeline_key: str,
    query, occurred_at: int, input_source_ref=None,
) -> dict:
    """STAGE 1. Commits the clark_boundary_query event (its own lawful
    transaction) ONLY for a successfully-completed waking turn. A
    failure here raises BoundaryInspectionError and NO result is ever
    fabricated. Returns {"query_event_id", "occurred_at"}."""
    query = validate_boundary_query(query)
    if not isinstance(session_id, str) or not session_id:
        raise BoundaryInspectionError("session_id must be a nonempty string")
    if not isinstance(session_started_at, int):
        raise BoundaryInspectionError("session_started_at must be an int")
    if not isinstance(pipeline_key, str) or not pipeline_key:
        raise BoundaryInspectionError("pipeline_key must be a nonempty string")
    if not isinstance(occurred_at, int):
        raise BoundaryInspectionError("occurred_at must be an int")

    event_id = generate_boundary_query_id()
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        pipeline_id = _resolve_pipeline_id(conn, pipeline_key)
        record_created_at = int(time.time())
        conn.execute("BEGIN")
        try:
            _resolve_or_create_session(conn, session_id, session_started_at)
            auth_context_id = generate_boundary_query_id()
            conn.execute(
                "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                "VALUES (?, ?, 'unknown', ?)",
                (auth_context_id, session_id, occurred_at),
            )
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, ?, ?, 'known', NULL, ?, ?, ?, ?)",
                (event_id, brr.BOUNDARY_INSPECTION_EVENT_TYPE, pipeline_id, auth_context_id,
                 input_source_ref, occurred_at, record_created_at),
            )
            spec = _query_spec_text(query, session_id, occurred_at)
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, "
                "span_start, span_end) VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                (event_id, _clark_actor_id(), brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND,
                 spec, hashlib.sha256(spec.encode("utf-8")).hexdigest()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"query_event_id": event_id, "occurred_at": occurred_at}
    finally:
        conn.close()


def _load_boundary_query_occurrence(conn, query_event_id: str) -> dict:
    """Load and cross-check one exact canonical query occurrence.

    The event/auth/spec tuple is the authority for evaluation context;
    no caller restates its session, time, pipeline, kind, or target."""
    row = conn.execute(
        "SELECT e.event_type, e.pipeline_id, e.pipeline_provenance_status, e.occurred_at, "
        "a.session_id FROM events e JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ?", (query_event_id,),
    ).fetchone()
    if row is None:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} does not exist -- refusing to evaluate nothing."
        )
    event_type, pipeline_id, provenance_status, event_occurred_at, event_session_id = row
    if event_type != brr.BOUNDARY_INSPECTION_EVENT_TYPE or provenance_status != "known":
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} is not a known canonical "
            f"{brr.BOUNDARY_INSPECTION_EVENT_TYPE} occurrence."
        )
    spec_rows = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (query_event_id, brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND),
    ).fetchall()
    if len(spec_rows) != 1:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} must carry exactly one boundary_query_spec; "
            f"found {len(spec_rows)}."
        )
    spec_text = spec_rows[0][0]
    try:
        spec = json.loads(spec_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} carries malformed boundary_query_spec JSON."
        ) from exc
    required = {"query_kind", "query_target", "session_id", "occurred_at"}
    if not isinstance(spec, dict) or set(spec) != required:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} carries a structurally invalid boundary_query_spec."
        )
    query = validate_boundary_query({
        "query_kind": spec["query_kind"], "query_target": spec["query_target"],
    })
    if spec["session_id"] != event_session_id or spec["occurred_at"] != event_occurred_at:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} has a query spec that disagrees with its "
            f"canonical session/time occurrence identity."
        )
    return {
        "query_event_id": query_event_id,
        "query": query,
        "session_id": event_session_id,
        "occurred_at": event_occurred_at,
        "pipeline_id": pipeline_id,
        "spec_text": spec_text,
    }


def append_boundary_inspection_result(
    data_dir: str, *, query_event_id: str, trace_path=None,
) -> dict:
    """STAGE 2. Evaluate and append the result for one exact persisted
    query occurrence.

    There is intentionally no caller-supplied result/session/query/time:
    all evaluation context is loaded from the authoritative event.  The
    occurrence is re-read inside the write transaction before insertion,
    so a result cannot be rebound to a matching-looking occurrence."""
    if not isinstance(query_event_id, str) or not query_event_id:
        raise BoundaryInspectionError("query_event_id must be a nonempty string")

    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        raise BoundaryInspectionError("no provenance database exists for this query occurrence")
    read_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        occurrence = _load_boundary_query_occurrence(read_conn, query_event_id)
    finally:
        read_conn.close()
    result = evaluate_boundary_query(
        BoundaryInspectionEnv(data_dir, trace_path=trace_path),
        occurrence["query"], session_id=occurrence["session_id"],
        evaluated_at=occurrence["occurred_at"],
    )
    result = validate_result(result)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("BEGIN")
        try:
            current = _load_boundary_query_occurrence(conn, query_event_id)
            if current != occurrence:
                raise BoundaryInspectionError(
                    f"query_event_id {query_event_id!r} changed between evaluation and persistence; "
                    f"refusing to bind the result to a different occurrence."
                )
            existing = conn.execute(
                "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
                (query_event_id, brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND),
            ).fetchall()
            text = _result_text(result)
            if existing:
                if any(existing_text[0] == text for existing_text in existing):
                    return {"query_event_id": query_event_id, "result_persisted": True,
                            "already_present": True, "classification": result["classification"]}
                raise BoundaryInspectionError(
                    f"query_event_id {query_event_id!r} already carries a DIFFERENT persisted "
                    f"boundary_inspection_result -- refusing to overwrite or recompute it."
                )
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, "
                "span_start, span_end) VALUES (?, 1, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                (query_event_id, _host_actor_id(), brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND,
                 text, hashlib.sha256(text.encode("utf-8")).hexdigest()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"query_event_id": query_event_id, "result_persisted": True,
                "already_present": False, "classification": result["classification"]}
    finally:
        conn.close()


def record_boundary_query_and_result(
    data_dir: str, *, session_id: str, session_started_at: int, pipeline_key: str,
    query, occurred_at: int, input_source_ref=None, trace_path=None,
) -> dict:
    """Two-stage convenience used by the waking pipeline, STRICTLY after
    the waking turn's own canonical commit. Both stages are their own
    transactions. If stage 1 fails, no result is fabricated and this
    raises. If evaluation or stage 2 fails, the query occurrence
    survives and this raises BoundaryInspectionError (the caller
    reports that the result was NOT recorded; it is never silently
    retried and never overwritten)."""
    stage1 = record_boundary_query(
        data_dir, session_id=session_id, session_started_at=session_started_at,
        pipeline_key=pipeline_key, query=query, occurred_at=occurred_at,
        input_source_ref=input_source_ref,
    )
    stage2 = append_boundary_inspection_result(
        data_dir, query_event_id=stage1["query_event_id"], trace_path=trace_path,
    )
    return {
        "query_event_id": stage1["query_event_id"],
        "occurred_at": occurred_at,
        "result_persisted": stage2["result_persisted"],
        "classification": stage2["classification"],
    }


def next_pending_boundary_result(data_dir: str, session_id: str):
    """The FIFO pending selector: the single NEXT undelivered result in
    the current session, in durable DB insertion order (events rowid --
    never wall-clock timestamps). Returns a dict or None. Delivered means
    a boundary_inspection_delivered component exists on the query event."""
    if not isinstance(session_id, str) or not session_id:
        raise BoundaryInspectionError("session_id must be a nonempty string")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT e.event_id FROM events e "
            "JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
            "WHERE e.event_type = ? AND a.session_id = ? "
            "AND EXISTS (SELECT 1 FROM event_components rc "
            "            WHERE rc.event_id = e.event_id AND rc.component_kind = ?) "
            "AND NOT EXISTS (SELECT 1 FROM event_components dc "
            "                 WHERE dc.event_id = e.event_id AND dc.component_kind = ?) "
            "ORDER BY e.rowid ASC LIMIT 1",
            (brr.BOUNDARY_INSPECTION_EVENT_TYPE, session_id,
             brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND,
             brr.BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND),
        ).fetchone()
        if row is None:
            return None
        query_event_id = row[0]
        components = {
            comp["component_kind"]: comp["component_text"]
            for comp in (
                dict(comp) for comp in conn.execute(
                    "SELECT component_kind, component_text FROM event_components "
                    "WHERE event_id = ? ORDER BY sequence", (query_event_id,)
                ).fetchall()
            )
        }
        spec = json.loads(components.get(brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND, "{}"))
        raw_result = components.get(brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND)
        if raw_result is None:
            return None
        result = json.loads(raw_result)
        return {
            "query_event_id": query_event_id,
            "query": {"query_kind": spec.get("query_kind"), "query_target": spec.get("query_target")},
            "occurred_at": spec.get("occurred_at"),
            "result": result,
        }
    finally:
        conn.close()


def _load_event_auth(conn, event_id: str):
    """The canonical event row plus its auth-context session link, or
    None when the event does not exist."""
    return conn.execute(
        "SELECT e.event_type, e.pipeline_id, e.pipeline_provenance_status, e.occurred_at, "
        "a.session_id FROM events e "
        "JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
        "WHERE e.event_id = ?", (event_id,)
    ).fetchone()


def _verify_delivery_chain(
    conn, query_event_id: str, waking_turn_event_id: str, occurred_at: int,
) -> None:
    """Mechanical, fully fail-closed verification of the delivery chain
    from canonical event data alone. Raises BoundaryInspectionError on
    any disagreement -- no check is ever weakened, every disagreement
    is a hard stop:
      1. query event exists as a clark_boundary_query and carries a
         persisted boundary_inspection_result;
      2. waking_turn_event_id exists as an event of type 'waking_turn'
         in pipeline_provenance_status 'known' on the SAME pipeline as
         the query (the genuine waking-turn-event requirement);
      3. the waking turn belongs to the same session as the query (the
         relevant-case requirement);
      4. the waking turn occurred no earlier than the query, and the
         caller-supplied occurred_at equals the waking turn's own
         canonical occurred_at (the caller cannot substitute a
         fabricated timestamp);
      5. the canonical waking-turn transaction itself recorded carriage
         of this exact query result.  No post-hoc object or assertion can
         create that fact."""
    query_raw = conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (query_event_id,)
    ).fetchone()
    if query_raw is None or query_raw[0] != brr.BOUNDARY_INSPECTION_EVENT_TYPE:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} does not exist as a clark_boundary_query "
            f"-- refusing to acknowledge delivery for nothing."
        )
    has_result = conn.execute(
        "SELECT 1 FROM event_components WHERE event_id = ? AND component_kind = ?",
        (query_event_id, brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND),
    ).fetchone()
    if has_result is None:
        raise BoundaryInspectionError(
            f"query_event_id {query_event_id!r} carries no persisted boundary_inspection_result "
            f"-- there is no deliverable to acknowledge."
        )
    query_auth = _load_event_auth(conn, query_event_id)
    wake_auth = _load_event_auth(conn, waking_turn_event_id)
    if wake_auth is None:
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} does not exist as any canonical "
            f"event -- delivery cannot be acknowledged by a fabricated turn."
        )
    wake_type, wake_pipeline_id, wake_provenance, wake_occurred_at, wake_session = wake_auth
    if wake_type != "waking_turn":
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} is {wake_type!r}, not a genuine "
            f"waking_turn event -- refusing to acknowledge delivery by a non-waking event."
        )
    if wake_provenance != "known":
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} has pipeline_provenance_status "
            f"{wake_provenance!r}, not 'known' -- refusing to acknowledge delivery by an "
            f"incompletely-provenanced turn."
        )
    if wake_pipeline_id != query_auth[1]:
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} and query_event_id "
            f"{query_event_id!r} are on different pipelines -- the waking turn cannot have "
            f"carried this pipeline's boundary result."
        )
    if wake_session != query_auth[4]:
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} belongs to session "
            f"{wake_session!r}, not the query's session {query_auth[4]!r} -- the waking turn "
            f"is not the case this result claim is about."
        )
    if wake_occurred_at < query_auth[3]:
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} occurred at {wake_occurred_at}, "
            f"BEFORE the query it supposedly delivered ({query_auth[3]}) -- impossible."
        )
    if occurred_at != wake_occurred_at:
        raise BoundaryInspectionError(
            f"caller-supplied occurred_at ({occurred_at}) does not equal the waking turn's "
            f"canonical occurred_at ({wake_occurred_at}) -- refusing a fabricated timestamp."
        )
    carried = conn.execute(
        "SELECT 1 FROM event_components WHERE event_id = ? AND component_kind = ? "
        "AND component_text = ?",
        (waking_turn_event_id,
         brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
         query_event_id),
    ).fetchone()
    if carried is None:
        raise BoundaryInspectionError(
            f"waking_turn_event_id {waking_turn_event_id!r} has no canonical carriage "
            f"component for query_event_id {query_event_id!r}; its waking-turn transaction "
            f"did not record actual composition inclusion."
        )


def record_boundary_inspection_delivered(
    data_dir: str, *, query_event_id: str, waking_turn_event_id: str, occurred_at: int,
) -> dict:
    """Append the delivered marker only after verifying the carriage fact
    already committed atomically with the canonical waking turn.

    No composition object is accepted here.  Actual Pass-2 inclusion is
    persisted by the real waking-turn writer; this function only reads
    that authoritative fact and records its FIFO consequence.

    Idempotent: an existing identical delivered marker is a no-op; a
    different existing marker raises."""
    if not isinstance(query_event_id, str) or not query_event_id:
        raise BoundaryInspectionError("query_event_id must be a nonempty string")
    if not isinstance(waking_turn_event_id, str) or not waking_turn_event_id:
        raise BoundaryInspectionError("waking_turn_event_id must be a nonempty string")
    if not isinstance(occurred_at, int):
        raise BoundaryInspectionError("occurred_at must be an int")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("BEGIN")
        try:
            _verify_delivery_chain(conn, query_event_id, waking_turn_event_id, occurred_at)

            existing_delivered = conn.execute(
                "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
                (query_event_id, brr.BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND),
            ).fetchall()
            if existing_delivered and not any(t[0] == waking_turn_event_id for t in existing_delivered):
                raise BoundaryInspectionError(
                    f"query_event_id {query_event_id!r} already carries a DIFFERENT delivered "
                    f"marker -- refusing to rewrite delivery history."
                )
            already_present = bool(existing_delivered)
            if not existing_delivered:
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 2, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
                    (query_event_id, _host_actor_id(), brr.BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND,
                     waking_turn_event_id,
                     hashlib.sha256(waking_turn_event_id.encode("utf-8")).hexdigest()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"delivered": True, "query_event_id": query_event_id,
                "waking_turn_event_id": waking_turn_event_id, "already_present": already_present}
    finally:
        conn.close()


# ---------------------------------------------------------------- delivery ---

MAX_DELIVERY_TEXT_CHARS = 650


def render_boundary_result_delivery(result) -> str:
    """Deterministic, bounded render of a persisted result for Pass-2's
    model input. Host report framing -- this is NEVER Clark's own
    conclusion. Never includes raw exceptions, stack traces, private
    contents, or file paths."""
    result = validate_result(result)
    boundaries_text = ", ".join(b["boundary_id"] for b in result["boundaries"]) or "none"
    text = (
        "Host-established boundary report answering an earlier boundary inquiry of yours "
        "(a mechanical host report, not your own conclusion): "
        f"classification={result['classification']}; "
        f"boundaries={boundaries_text}; "
        f"checks={len(result['checks'])}; "
        f"evaluated_at={_display_datetime_text(result['evaluated_at'])}; "
        f"scope: {result['scope_note']}"
    )
    if len(text) > MAX_DELIVERY_TEXT_CHARS:
        text = text[:MAX_DELIVERY_TEXT_CHARS]
        text = text.rsplit(" ", 1)[0] + "..."
    return text
