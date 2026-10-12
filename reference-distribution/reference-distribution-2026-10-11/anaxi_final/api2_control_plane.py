"""API2-S1: disabled-by-default canonical TX2 / HDI2 execution seam for
anaxi_provenance.db.

COMMITTED CANONICAL DECISION -> prepare_context() at most once per
attempt -> durable snapshot -> Pass 1 (typed act) -> durable immutable
stage -> Pass 2 (expression) -> durable expression -> canonical TX2
(decision transition + expression + provenance) -> release-eligible.

AUTHORITATIVE_DECISION_PATH_ENABLED defaults to False (top-level gate).
api1_control_plane.AUTHORITATIVE_PDE_ENABLED remains a separate,
subordinate gate for TX1 (decision *establishment*) and is untouched
here. Neither module independently exposes a publicly reachable
partial-authority route with the other disabled. This module performs
zero Ollama calls anywhere in its own code, and is not imported by
llama_anaxi.py/orchestration.py/run_waking_turn().

API2-LR1-S1 production-core promotion: this module now imports its pure
typed-decision-action primitives from `decision_path_core.py` (a
production module, promoted verbatim from the frozen `scratchpad/hdi2`
prototype -- see that module's docstring and
`test_decision_path_core_parity.py` for the parity proof) rather than
from `scratchpad/hdi2` directly. Production's runtime import graph no
longer terminates in an experimental namespace.

Two of the promoted primitives' ORIGINAL scratch counterparts are
deliberately NOT what this module's own hand-written validation
mirrors, because live production identity is not the frozen HDI2-S1
scratchpad's fixed 3-actor world ({clark, human, host}):

  - `hdi2.validation.validate_decision_act` hardcodes
    `delegate_to_actor_id = HUMAN_ACTOR_ID` (the literal string
    "human"). Production's real counterpart is a dynamically
    registered actor id (e.g. "human-actor-c8feddc1b4bb", per
    HIR1-S2). `validate_pass1_action` below performs the identical
    structural checks but derives delegate_to_actor_id from the live
    `protected_decisions.counterpart_actor_id` (spec section 27),
    never from a fixed constant. This function was never promoted --
    only its constants (`Act`/`ACT_VALUES`/`RAW_ACTION_ALLOWED_FIELDS`)
    were.
  - `hdi2.release.validate_release` expects an ExpressionResult shaped
    `{text, action_id, decision_id}`. API2's own frozen Pass-2 output
    contract (spec section 29) is exactly `{action_id, expression}` --
    no `decision_id`, and the text field is named `expression` not
    `text`. `validate_pass2_output` below reuses
    `looks_like_structured_control_payload`/`MAX_EXPRESSION_CHARS`/
    `ReleaseFailure` (all promoted) but implements its own exact-key
    check against the real two-field contract rather than force-
    mapping field names. `validate_release` itself was never promoted.

`compute_transition` IS reused verbatim (promoted) -- it is
parametrized purely by data (no fixed actor constants baked in), so it
applies unmodified to real production actor ids. `route_decision` was
imported in API2-S1 but never actually called anywhere in this module;
it is not promoted here (dead-import cleanup, section 3: "promote only
... required by API2").

`protected_decision_events` is the canonical decision-control ledger
(spec section 36): `hdi2_action_selected`, `decision_state_transitioned`
(the canonical, corrected spelling as of API2-LR1-S1; no live runtime
rows under any prior spelling have ever existed), and
`expression_realized` are all rows there, never in `events`. `events`
gets exactly one new row per committed TX2: the main production
provenance representation of Clark's expressed turn, linked back via
`protected_decision_events.related_waking_event_id`.

Provenance role correction (spec sections 15-19): API2-S1 wrote
`event_requesters(requester_actor_id=clark_actor_id)` on the main
event as a stand-in for "speaker", conflating requester/initiator with
speaker semantics. This is corrected: creator/speaker is now
represented the way production already represents authored text
everywhere else -- an `event_components` row with
`creator_actor_id=clark_actor_id` (the real, pre-existing production
creator-attribution mechanism; see `native_provenance_writer.py`,
which uses the same table for ordinary waking-turn authorship).
Creator and speaker are documented here to coincide for this event
type: nothing else in the schema represents "speaker" more precisely,
and inventing a new dialogue-role table for that alone would be scope
beyond a minimal, truthful representation. No `event_requesters` row
is written on the main event at all -- section 19's own preference
("prefer no requester row over a misleading one") is taken literally,
since the human who initiated the underlying decision is already
truthfully reachable through the existing causal chain (main event ->
`related_waking_event_id` -> transition event -> `decision_id` ->
`protected_decisions` -> `protected_decision_created` event ->
`establishment_request_id` -> TX1), not through a fabricated
requester attribution on Clark's own output event.
"""
import enum
import hashlib
import json
import sqlite3
import sys
import os
import time
import uuid
from datetime import datetime, timezone

_ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if _ANAXI_FINAL not in sys.path:
    sys.path.insert(0, _ANAXI_FINAL)

from decision_path_core import (  # noqa: E402  (production-core; promoted from scratchpad/hdi2, see module docstring)
    Act, ACT_VALUES, RAW_ACTION_ALLOWED_FIELDS, ValidationFailure,
    ReleaseFailure, StageStatus, MAX_EXPRESSION_CHARS, compute_transition,
    looks_like_structured_control_payload,
)

from api1_control_plane import build_protected_decision_route  # noqa: E402  (reused verbatim)
from migrate_historical_data import resolve_pipeline_id, resolve_or_create_model_revision  # noqa: E402  (reused verbatim)
from provenance_schema import derive_stable_id  # noqa: E402  (reused verbatim, pure)

# ------------------------------------------------------------- feature gates -

AUTHORITATIVE_DECISION_PATH_ENABLED = False

# --------------------------------------------------------------- pipeline id --

# Pathway identity, not Clark identity, not model identity, not
# authentication identity (spec section 12). The two existing
# `pipelines` rows (`anaxi_orchestration_lineage_a`/`_b`) represent
# legacy llama/claude SUBSTRATE lineage from migrate_historical_data.py
# -- a historical migration concept this new typed-decision pathway is
# not part of, so it gets its own row rather than borrowing one of
# theirs or leaving pipeline_id NULL (API2-S1's earlier, now-corrected
# choice).
AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY = "authoritative_waking_decision"


def install_authoritative_waking_decision_pipeline(conn):
    """Additive, idempotent. Installs the one durable pipeline-identity
    row for this pathway if absent; verifies it if already present.
    Infrastructure only -- never called as part of any per-turn/per-
    decision runtime path. Reuses migrate_historical_data.py's own
    resolve_pipeline_id()/pipeline_id-minting convention
    (derive_stable_id("pipeline", pipeline_key)) rather than inventing
    a second one."""
    existing_id = resolve_pipeline_id(conn, AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY)
    if existing_id is not None:
        return existing_id, False
    pipeline_id = derive_stable_id("pipeline", AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY)
    with conn:
        conn.execute(
            "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
            "VALUES (?, ?, NULL, ?)",
            (
                pipeline_id, AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY,
                "Typed-decision-action pathway (API2/HDI2 owner route) -- not a legacy substrate lineage.",
            ),
        )
    return pipeline_id, True


# ---------------------------------------------------------------- failure enum -


class Api2Failure(str, enum.Enum):
    FEATURE_DISABLED = "FEATURE_DISABLED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    CAPABILITY_CONSUMED = "CAPABILITY_CONSUMED"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    ATTEMPT_STATE_CONFLICT = "ATTEMPT_STATE_CONFLICT"
    PREPARATION_INDETERMINATE = "PREPARATION_INDETERMINATE"
    SYNCHRONOUS_PREPARE_FAILURE = "SYNCHRONOUS_PREPARE_FAILURE"
    ATTEMPT_TERMINAL = "ATTEMPT_TERMINAL"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
    DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
    STAGE_NOT_FOUND = "STAGE_NOT_FOUND"
    STAGE_STATE_CONFLICT = "STAGE_STATE_CONFLICT"
    STAGE_EXISTS = "STAGE_EXISTS"
    STALE_DECISION_STATE = "STALE_DECISION_STATE"
    PIPELINE_NOT_INSTALLED = "PIPELINE_NOT_INSTALLED"


ATTEMPT_STATUSES = {"created", "preparing_context", "context_ready", "prepare_failed", "aborted"}
PASS1_TASK_INSTRUCTION = "select what you want to do with this decision"
PASS2_ALLOWED_FIELDS = {"action_id", "expression"}


class _StaleDecisionRace(Exception):
    """Internal-only: a concurrent write changed the decision between
    the pre-TX2 read and the atomic write. Caught inside commit_tx2 and
    translated to STALE_DECISION_STATE -- never leaks past this module."""


# ----------------------------------------------------------- capability token -


class Capability:
    """Opaque, host-generated, single-use authority token. Minted only
    by mint_capability() (a supervisor-only concept in this gate --
    API2-S1 mints no live capability; only synthetic ones in tests).
    Cannot be constructed from ordinary user/model input: nothing in
    this module accepts a caller-supplied dict and turns it into a
    Capability. Not authentication -- consume_capability() never checks
    AAB state; that remains execute_authoritative_owner_route()'s own
    separate concern when a real TX1/session is involved."""

    __slots__ = ("capability_id", "decision_id", "consumed")

    def __init__(self, capability_id, decision_id):
        self.capability_id = capability_id
        self.decision_id = decision_id
        self.consumed = False


def mint_capability(decision_id, capability_id=None):
    if capability_id is None:
        capability_id = f"cap-{uuid.uuid4().hex[:16]}"
    return Capability(capability_id, decision_id)


def consume_capability(capability, decision_id):
    if not isinstance(capability, Capability):
        return None, Api2Failure.CAPABILITY_INVALID
    if capability.consumed:
        return None, Api2Failure.CAPABILITY_CONSUMED
    if capability.decision_id != decision_id:
        return None, Api2Failure.CAPABILITY_INVALID
    capability.consumed = True
    return capability, None


# --------------------------------------------------------------- attempt FSM --


def create_attempt(conn, decision_id, expected_last_transition_event_id, attempt_id=None):
    now = int(time.time())
    if attempt_id is None:
        attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
    existing = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if existing is not None:
        if (
            existing["decision_id"] == decision_id
            and existing["expected_last_transition_event_id"] == expected_last_transition_event_id
        ):
            return dict(existing), None
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT

    with conn:
        conn.execute(
            "INSERT INTO protected_decision_attempts (attempt_id, decision_id, "
            "expected_last_transition_event_id, status, started_at, updated_at) "
            "VALUES (?, ?, ?, 'created', ?, ?)",
            (attempt_id, decision_id, expected_last_transition_event_id, now, now),
        )
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row), None


def begin_preparation(conn, attempt_id):
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None, Api2Failure.ATTEMPT_NOT_FOUND
    if row["status"] != "created":
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT
    now = int(time.time())
    with conn:
        conn.execute(
            "UPDATE protected_decision_attempts SET status = 'preparing_context', updated_at = ? "
            "WHERE attempt_id = ? AND status = 'created'",
            (now, attempt_id),
        )
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row), None


SNAPSHOT_WIRE_FORMAT = "API2_PREPARED_CONTEXT_V1"


def serialize_prepared_context(
    prepared, attempt_id, decision_id, expected_last_transition_event_id,
    model_name, think, seed=None, prepared_at=None, source_refs=None,
):
    """Pure adapter (spec sections 7-11). Converts the ACTUAL return
    shape of orchestration.prepare_context() -- `{memory_context,
    kardia, controls, messages}` -- into the durable canonical wire
    snapshot. Read-inspection of orchestration.py/linguistic_pipeline.py
    established that `kardia` is already a plain `Dict[str, str]` (NOT
    a custom Python object -- there never was an object-graph problem
    to solve), and that `messages[0]` is the exact rendered system
    message (identity preamble + injected memory context) that Pass 1/
    Pass 2 actually see on the wire once `apply_to_messages()` has run
    inside `prepare_context()`. Frozen invariant: this function stores
    ONLY that rendered text plus the plain-data messages/memory/
    controls already present -- never the raw `kardia` dict itself (an
    internal input to rendering, not itself model-visible), and calls
    no preparation function of its own -- exactly one prepare_context()
    call happens, by the caller, before this function is invoked."""
    messages = prepared.get("messages") or []
    kardia_system_content = ""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        kardia_system_content = messages[0].get("content", "") or ""

    controls = prepared.get("controls") or {}
    other_controls = {k: v for k, v in controls.items() if k not in ("temperature", "top_p")}

    return {
        "snapshot_format": SNAPSHOT_WIRE_FORMAT,
        "attempt_id": attempt_id,
        "decision_id": decision_id,
        "expected_last_transition_event_id": expected_last_transition_event_id,
        "messages": messages,
        "memory_context": prepared.get("memory_context") or "",
        "kardia_system_content": kardia_system_content,
        "generation_controls": {
            "model_name": model_name,
            "think": bool(think),
            "seed": seed,
            # API2-P1: temperature/top_p are real Pass-2 (call_llama)
            # inputs, sourced directly from the real prepared controls
            # dict -- never defaulted/guessed/reconstructed later. Kept
            # as explicit top-level fields rather than folded into
            # other_controls so a caller building a real call_llama()
            # controls dict never has to know that internal split.
            "temperature": controls.get("temperature"),
            "top_p": controls.get("top_p"),
            "other_controls": other_controls,
        },
        "source_metadata": {
            "context_system_hash": hashlib.sha256(kardia_system_content.encode("utf-8")).hexdigest(),
            "prepared_at": prepared_at,
            "source_refs": list(source_refs) if source_refs else [],
        },
    }


def canonical_snapshot_serialization(prepared_context):
    """Deterministic canonical serialization over an already-adapted
    (serialize_prepared_context output, or an equivalently-shaped
    JSON-serializable dict) wire snapshot."""
    return json.dumps(prepared_context, sort_keys=True, ensure_ascii=True)


def compute_snapshot_hash(serialized):
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def complete_preparation(
    conn, attempt_id, wire_snapshot,
    snapshot_format=SNAPSHOT_WIRE_FORMAT, snapshot_version="1",
    snapshot_id=None, _test_inject_failure_after=None,
):
    """wire_snapshot: the OUTPUT of serialize_prepared_context() (or an
    equivalently-shaped dict) -- not the raw prepare_context() return
    value. See run_preparation(), which applies the adapter before
    calling this function."""
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None, Api2Failure.ATTEMPT_NOT_FOUND
    if row["status"] != "preparing_context":
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT

    if not isinstance(wire_snapshot, dict):
        return None, Api2Failure.SNAPSHOT_INVALID
    try:
        serialized = canonical_snapshot_serialization(wire_snapshot)
    except (TypeError, ValueError):
        return None, Api2Failure.SNAPSHOT_INVALID

    source_metadata = wire_snapshot.get("source_metadata") or {}
    system_context_hash = source_metadata.get("context_system_hash")
    generation_controls = wire_snapshot.get("generation_controls") or {}
    model_name = generation_controls.get("model_name")
    think = generation_controls.get("think")
    if not system_context_hash or not model_name or think is None:
        return None, Api2Failure.SNAPSHOT_INVALID

    snapshot_hash = compute_snapshot_hash(serialized)
    if snapshot_id is None:
        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        conn.execute(
            "INSERT INTO protected_decision_attempt_snapshots (snapshot_id, attempt_id, "
            "snapshot_format, snapshot_version, prepared_context_json, snapshot_hash, "
            "system_context_hash, model_name, think, generation_controls_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id, attempt_id, snapshot_format, snapshot_version, serialized,
                snapshot_hash, system_context_hash, model_name, 1 if think else 0,
                json.dumps(generation_controls, sort_keys=True), now,
            ),
        )
        if _test_inject_failure_after == "snapshot_insert":
            raise RuntimeError("injected test failure: snapshot_insert")
        conn.execute(
            "UPDATE protected_decision_attempts SET status = 'context_ready', snapshot_id = ?, "
            "updated_at = ? WHERE attempt_id = ? AND status = 'preparing_context'",
            (snapshot_id, now, attempt_id),
        )

    snap_row = conn.execute(
        "SELECT * FROM protected_decision_attempt_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    return dict(snap_row), None


def fail_preparation(conn, attempt_id, failure_code, failure_detail=None):
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None, Api2Failure.ATTEMPT_NOT_FOUND
    if row["status"] != "preparing_context":
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT
    now = int(time.time())
    with conn:
        conn.execute(
            "UPDATE protected_decision_attempts SET status = 'prepare_failed', failure_code = ?, "
            "failure_detail = ?, updated_at = ? WHERE attempt_id = ? AND status = 'preparing_context'",
            (failure_code, failure_detail, now, attempt_id),
        )
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row), None


def abort_attempt(conn, attempt_id):
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None, Api2Failure.ATTEMPT_NOT_FOUND
    if row["status"] not in ("created", "preparing_context", "context_ready"):
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT
    now = int(time.time())
    with conn:
        conn.execute(
            "UPDATE protected_decision_attempts SET status = 'aborted', updated_at = ? "
            "WHERE attempt_id = ?",
            (now, attempt_id),
        )
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row), None


def run_preparation(conn, attempt_id, prepare_callable, model_name, think, seed=None):
    """Exactly-once control claim (spec section 10): the host never
    intentionally calls prepare_callable more than once for the same
    attempt_id. A `preparing_context` attempt found with no durable
    snapshot (process-boundary loss) is PREPARATION_INDETERMINATE and
    is left in that state permanently -- the frozen attempt enum has no
    dedicated "indeterminate" value, so remaining in `preparing_context`
    forever (this function will always return PREPARATION_INDETERMINATE
    for it and never call prepare_callable again) IS the mechanically
    terminal indeterminate representation the spec allows as an
    alternative to an explicit `aborted` transition (section 12). No
    automatic abort is performed here -- that stays an explicit
    supervised operation (abort_attempt), never automatic.

    Returns (result, None, call_count) or (None, Api2Failure, call_count).
    call_count counts invocations of prepare_callable made during THIS
    call only (0 for every recovery path).
    """
    row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        return None, Api2Failure.ATTEMPT_NOT_FOUND, 0

    status = row["status"]

    if status == "context_ready":
        snap = conn.execute(
            "SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if snap is None:
            return None, Api2Failure.SNAPSHOT_INVALID, 0
        if compute_snapshot_hash(snap["prepared_context_json"]) != snap["snapshot_hash"]:
            return None, Api2Failure.SNAPSHOT_INVALID, 0
        return dict(snap), None, 0

    if status == "preparing_context":
        snap = conn.execute(
            "SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if snap is not None:
            return None, Api2Failure.SNAPSHOT_INVALID, 0
        return None, Api2Failure.PREPARATION_INDETERMINATE, 0

    if status in ("prepare_failed", "aborted"):
        return None, Api2Failure.ATTEMPT_TERMINAL, 0

    if status != "created":
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT, 0

    _begun, failure = begin_preparation(conn, attempt_id)
    if failure is not None:
        return None, failure, 0

    call_count = 0
    try:
        call_count += 1
        prepared_context = prepare_callable()
    except Exception as exc:  # noqa: BLE001 - a synchronous prepare failure while the process stays alive
        fail_preparation(conn, attempt_id, "PREPARE_RAISED", str(exc))
        return None, Api2Failure.SYNCHRONOUS_PREPARE_FAILURE, call_count

    if prepared_context is None:
        fail_preparation(conn, attempt_id, "PREPARE_RETURNED_NONE", None)
        return None, Api2Failure.SYNCHRONOUS_PREPARE_FAILURE, call_count

    if not isinstance(prepared_context, dict):
        fail_preparation(conn, attempt_id, "PREPARE_RESULT_NOT_A_DICT", None)
        return None, Api2Failure.SYNCHRONOUS_PREPARE_FAILURE, call_count

    wire_snapshot = serialize_prepared_context(
        prepared_context, attempt_id, row["decision_id"], row["expected_last_transition_event_id"],
        model_name, think, seed=seed, prepared_at=datetime.now(timezone.utc).isoformat(),
    )
    result, failure = complete_preparation(conn, attempt_id, wire_snapshot)
    if failure is not None:
        # complete_preparation only fails here on a malformed prepared_context
        # (SNAPSHOT_INVALID) or an attempt-state race; either way this IS a
        # known synchronous failure observed while the process stayed alive.
        fail_preparation(conn, attempt_id, "PREPARE_RESULT_INVALID", str(failure))
        return None, Api2Failure.SYNCHRONOUS_PREPARE_FAILURE, call_count
    return result, None, call_count


# ------------------------------------------------------------------ Pass 1 ---


def build_pass1_interface(route, snapshot_row):
    """Pure builder. Does not call a model. Excludes SID, AAB session
    internals, assurance internals, person ID, human registration
    event, and the raw counterpart actor ID (spec section 20) -- the
    counterpart is exposed only as the fixed literal label below, for
    `delegate` wording."""
    prepared_context = json.loads(snapshot_row["prepared_context_json"])
    return {
        "decision_id": route["decision_id"],
        "decision_text": route["decision_text"],
        "decision_context": route["decision_context"],
        "owner_state": {"status": "resolved", "actor_id": route["owner_actor_id"]},
        "decision_state": route["decision_state"],
        "allowed_acts": sorted(ACT_VALUES),
        "raw_action_schema": {"decision_id": "string", "act": "string", "content": "string"},
        "task_instruction": PASS1_TASK_INSTRUCTION,
        "prepared_messages": prepared_context.get("messages"),
        "memory_context": prepared_context.get("memory_context"),
        "generation_controls": prepared_context.get("generation_controls"),
        "delegate_counterpart_label": "the human counterpart in this interaction",
    }


def validate_pass1_action(protected_decision_route, raw_model_action, clark_actor_id):
    """Structural validation only -- mirrors hdi2.validation.
    validate_decision_act's checks exactly, except delegate recipient
    validity is derived from the LIVE route's counterpart_actor_id
    (spec section 27), not a fixed actor constant. See module
    docstring for why this is not a call to the HDI2 function."""
    if isinstance(raw_model_action, str):
        try:
            raw = json.loads(raw_model_action)
        except (TypeError, ValueError):
            return None, ValidationFailure.MALFORMED_ACTION
    elif isinstance(raw_model_action, dict):
        raw = raw_model_action
    else:
        return None, ValidationFailure.MALFORMED_ACTION

    if not isinstance(raw, dict):
        return None, ValidationFailure.MALFORMED_ACTION
    if not RAW_ACTION_ALLOWED_FIELDS.issubset(raw.keys()):
        return None, ValidationFailure.MALFORMED_ACTION
    if set(raw.keys()) - RAW_ACTION_ALLOWED_FIELDS:
        return None, ValidationFailure.PROTOCOL_LEAKAGE

    if protected_decision_route is None:
        return None, ValidationFailure.UNKNOWN_DECISION
    if not isinstance(raw.get("decision_id"), str) or raw["decision_id"] != protected_decision_route["decision_id"]:
        return None, ValidationFailure.MULTI_DECISION_UPDATE

    if protected_decision_route["owner_actor_id"] != clark_actor_id:
        return None, ValidationFailure.ACTOR_NOT_OWNER
    if protected_decision_route["decision_state"] != "open":
        return None, ValidationFailure.DECISION_NOT_OPEN

    act = raw.get("act")
    if act not in ACT_VALUES:
        return None, ValidationFailure.INVALID_ACT

    content = raw.get("content")
    if not isinstance(content, str):
        return None, ValidationFailure.INVALID_CONTENT

    delegate_to_actor_id = None
    if act == Act.DELEGATE.value:
        counterpart = protected_decision_route.get("counterpart_actor_id")
        if not counterpart:
            return None, ValidationFailure.DELEGATE_RECIPIENT_INVALID
        delegate_to_actor_id = counterpart

    validated = {
        "action_id": None,
        "decision_id": raw["decision_id"],
        "actor_id": clark_actor_id,
        "act": act,
        "content": content,
        "delegate_to_actor_id": delegate_to_actor_id,
    }
    return validated, None


def commit_action_stage(
    conn, attempt_id, decision_id, expected_last_transition_event_id, snapshot_row,
    raw_action_json, validated_action, clark_actor_id, action_id=None, stage_id=None,
    _test_inject_failure_after=None,
):
    """The action-stage durability transaction (spec section 25/26).
    Re-reads canonical decision fresh, requires owner=Clark/state=open/
    token match, then atomically creates the durable immutable stage
    plus the `hdi2_action_selected` control-ledger event. Never alters
    decision owner/state/resolution. Fails STALE_DECISION_STATE (no
    stage) if the fresh read disagrees with the expected token."""
    decision = conn.execute(
        "SELECT * FROM protected_decisions WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    if decision is None:
        return None, Api2Failure.STALE_DECISION_STATE
    if (
        decision["owner_actor_id"] != clark_actor_id
        or decision["decision_state"] != "open"
        or decision["last_transition_event_id"] != expected_last_transition_event_id
    ):
        return None, Api2Failure.STALE_DECISION_STATE

    existing_stage = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if existing_stage is not None:
        return None, Api2Failure.STAGE_EXISTS

    if action_id is None:
        action_id = f"action-{uuid.uuid4().hex[:12]}"
    if stage_id is None:
        stage_id = f"stage-{uuid.uuid4().hex[:12]}"
    event_id = f"pde-event-{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    validated_for_store = dict(validated_action)
    validated_for_store["action_id"] = action_id
    content_hash = hashlib.sha256(validated_action["content"].encode("utf-8")).hexdigest()
    payload = {
        "attempt_id": attempt_id, "stage_id": stage_id, "action_id": action_id,
        "act": validated_action["act"], "content_hash": content_hash,
        "snapshot_id": snapshot_row["snapshot_id"], "snapshot_hash": snapshot_row["snapshot_hash"],
    }

    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        conn.execute(
            "INSERT INTO protected_decision_stages (stage_id, attempt_id, decision_id, "
            "expected_last_transition_event_id, prepared_snapshot_id, prepared_snapshot_hash, "
            "action_id, actor_id, raw_action_json, validated_action_json, status, selected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_expression', ?)",
            (
                stage_id, attempt_id, decision_id, expected_last_transition_event_id,
                snapshot_row["snapshot_id"], snapshot_row["snapshot_hash"], action_id,
                clark_actor_id, raw_action_json, json.dumps(validated_for_store), now,
            ),
        )
        if _test_inject_failure_after == "stage_insert":
            raise RuntimeError("injected test failure: stage_insert")
        conn.execute(
            "INSERT INTO protected_decision_events (event_id, event_type, decision_id, actor_id, "
            "auth_context_id, session_id, establishment_request_id, previous_owner_actor_id, "
            "resulting_owner_actor_id, prior_transition_event_id, related_waking_event_id, "
            "occurred_at, payload_json) VALUES (?, 'hdi2_action_selected', ?, ?, NULL, NULL, NULL, "
            "?, ?, ?, NULL, ?, ?)",
            (
                event_id, decision_id, clark_actor_id, clark_actor_id, clark_actor_id,
                expected_last_transition_event_id, now, json.dumps(payload),
            ),
        )
        if _test_inject_failure_after == "event_insert":
            raise RuntimeError("injected test failure: event_insert")

    return {"stage_id": stage_id, "action_id": action_id, "event_id": event_id}, None


def run_pass1(conn, attempt_row, route, pass1_callable, clark_actor_id):
    """Calls pass1_callable at most once, only if no durable stage
    already exists for this attempt (spec section 18/23: once a stage
    exists, Pass 1 is never rerun for it). Returns (stage_row, None,
    call_count) or (None, failure, call_count)."""
    if attempt_row["status"] != "context_ready":
        return None, Api2Failure.ATTEMPT_STATE_CONFLICT, 0

    existing_stage = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE attempt_id = ?", (attempt_row["attempt_id"],)
    ).fetchone()
    if existing_stage is not None:
        return dict(existing_stage), None, 0

    snapshot_row = conn.execute(
        "SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id = ?",
        (attempt_row["attempt_id"],),
    ).fetchone()
    if snapshot_row is None:
        return None, Api2Failure.SNAPSHOT_INVALID, 0

    raw_action = pass1_callable()
    call_count = 1

    validated, failure = validate_pass1_action(route, raw_action, clark_actor_id)
    if failure is not None:
        # zero automatic retry; no stage created; attempt stays context_ready
        return None, failure, call_count

    raw_action_json = raw_action if isinstance(raw_action, str) else json.dumps(raw_action)
    commit_result, commit_failure = commit_action_stage(
        conn, attempt_row["attempt_id"], route["decision_id"],
        attempt_row["expected_last_transition_event_id"], snapshot_row,
        raw_action_json, validated, clark_actor_id,
    )
    if commit_failure is not None:
        return None, commit_failure, call_count

    stage_row = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE stage_id = ?", (commit_result["stage_id"],)
    ).fetchone()
    return dict(stage_row), None, call_count


# ------------------------------------------------------------------ Pass 2 ---


def build_pass2_input(snapshot_row, stage_row):
    """Pure builder. Pass-1's output is exposed only as structured
    immutable control data (`staged_action`), never merged into
    ordinary assistant-history message text."""
    prepared_context = json.loads(snapshot_row["prepared_context_json"])
    validated_action = json.loads(stage_row["validated_action_json"])
    return {
        "prepared_messages": prepared_context.get("messages"),
        "memory_context": prepared_context.get("memory_context"),
        "generation_controls": prepared_context.get("generation_controls"),
        "staged_action": {
            "decision_id": stage_row["decision_id"],
            "action_id": stage_row["action_id"],
            "act": validated_action["act"],
            "content": validated_action["content"],
        },
    }


def build_pass2_llama_controls(generation_controls):
    """Pure reconstruction of the exact `controls` dict the real
    `call_llama(messages, controls)` helper requires (spec section 6):
    `{"temperature": ..., "top_p": ...}`, taken directly from the
    durable snapshot's `generation_controls.temperature`/`.top_p` --
    never a default, never re-derived from `other_controls` or any
    other source. `generation_controls`: the dict at
    prepared_context["generation_controls"] (equivalently
    pass2_input["generation_controls"] from build_pass2_input()) --
    already reloaded from durable JSON, not the live prepare_context()
    return value."""
    return {
        "temperature": generation_controls["temperature"],
        "top_p": generation_controls["top_p"],
    }


def validate_pass2_output(stage_row, raw_pass2_output):
    """Structural only (spec section 30). Exact expected keys
    {action_id, expression}; action_id must match the stage; expression
    non-empty, resource-bound, and not a structured-control-looking
    payload (reusing hdi2.release.looks_like_structured_control_payload).
    No semantic ownership/action judge."""
    if isinstance(raw_pass2_output, str):
        try:
            raw = json.loads(raw_pass2_output)
        except (TypeError, ValueError):
            return None, ReleaseFailure.PROTOCOL_FORMAT_VIOLATION
    elif isinstance(raw_pass2_output, dict):
        raw = raw_pass2_output
    else:
        return None, ReleaseFailure.PROTOCOL_FORMAT_VIOLATION

    if not isinstance(raw, dict) or set(raw.keys()) != PASS2_ALLOWED_FIELDS:
        return None, ReleaseFailure.PROTOCOL_FORMAT_VIOLATION

    if raw.get("action_id") != stage_row["action_id"]:
        return None, ReleaseFailure.ACTION_BINDING_MISMATCH

    expression = raw.get("expression")
    if not isinstance(expression, str) or len(expression.strip()) == 0:
        return None, ReleaseFailure.EMPTY_EXPRESSION
    if len(expression) > MAX_EXPRESSION_CHARS:
        return None, ReleaseFailure.RESOURCE_LIMIT
    if looks_like_structured_control_payload(expression):
        return None, ReleaseFailure.PROTOCOL_FORMAT_VIOLATION

    return {"expression": expression}, None


def persist_expression(conn, stage_id, expression_text, _test_inject_failure_after=None):
    stage = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    if stage is None:
        return None, Api2Failure.STAGE_NOT_FOUND
    if stage["status"] != "awaiting_expression":
        return None, Api2Failure.STAGE_STATE_CONFLICT

    expression_hash = hashlib.sha256(expression_text.encode("utf-8")).hexdigest()
    now = int(time.time())
    with conn:
        if _test_inject_failure_after == "before_any_write":
            raise RuntimeError("injected test failure: before_any_write")
        conn.execute(
            "UPDATE protected_decision_stages SET status = 'expression_ready', expression_text = ?, "
            "expression_hash = ?, expression_created_at = ? WHERE stage_id = ? AND status = 'awaiting_expression'",
            (expression_text, expression_hash, now, stage_id),
        )
    stage = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    return dict(stage), None


def run_pass2(conn, stage_row, pass2_callable):
    """Calls pass2_callable at most once, only while the stage is
    `awaiting_expression`. A durable expression (or committed stage) is
    reused without ever calling Pass 2 again."""
    if stage_row["status"] in ("expression_ready", "committed"):
        return dict(stage_row), None, 0
    if stage_row["status"] != "awaiting_expression":
        return None, Api2Failure.STAGE_STATE_CONFLICT, 0

    raw_output = pass2_callable()
    call_count = 1

    validated, failure = validate_pass2_output(stage_row, raw_output)
    if failure is not None:
        # zero automatic retry; stage remains awaiting_expression
        return None, failure, call_count

    result, persist_failure = persist_expression(conn, stage_row["stage_id"], validated["expression"])
    if persist_failure is not None:
        return None, persist_failure, call_count
    return result, None, call_count


# -------------------------------------------------------------------- TX2 ----


def commit_tx2(
    conn, stage_id, clark_actor_id, main_event_id=None, transition_event_id=None,
    expression_event_id=None, _test_inject_failure_after=None,
):
    """Canonical TX2. Preconditions (spec section 33) re-checked against
    a fresh read immediately before the atomic write. Idempotent replay
    (stage already `committed`) performs zero writes and reconstructs
    the exact prior result from durable state -- no result_json blob
    needed, since protected_decision_events.related_waking_event_id
    already links the transition event to the main `events` row."""
    stage = conn.execute(
        "SELECT * FROM protected_decision_stages WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    if stage is None:
        return None, Api2Failure.STAGE_NOT_FOUND

    if stage["status"] == "committed":
        transition_event = conn.execute(
            "SELECT * FROM protected_decision_events WHERE event_id = ?", (stage["committed_event_id"],)
        ).fetchone()
        decision = conn.execute(
            "SELECT * FROM protected_decisions WHERE decision_id = ?", (stage["decision_id"],)
        ).fetchone()
        return {
            "decision_id": decision["decision_id"], "stage_id": stage_id,
            "action_id": stage["action_id"], "transition_event_id": transition_event["event_id"],
            "main_event_id": transition_event["related_waking_event_id"],
            "owner_actor_id": decision["owner_actor_id"], "decision_state": decision["decision_state"],
            "resolution_kind": decision["resolution_kind"],
        }, None

    if stage["status"] != "expression_ready":
        return None, Api2Failure.STAGE_STATE_CONFLICT

    decision = conn.execute(
        "SELECT * FROM protected_decisions WHERE decision_id = ?", (stage["decision_id"],)
    ).fetchone()
    if decision is None:
        return None, Api2Failure.STALE_DECISION_STATE
    if (
        decision["owner_actor_id"] != clark_actor_id
        or decision["decision_state"] != "open"
        or decision["last_transition_event_id"] != stage["expected_last_transition_event_id"]
    ):
        return None, Api2Failure.STALE_DECISION_STATE

    pipeline_id = resolve_pipeline_id(conn, AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY)
    if pipeline_id is None:
        return None, Api2Failure.PIPELINE_NOT_INSTALLED

    snapshot_row = conn.execute(
        "SELECT model_name FROM protected_decision_attempt_snapshots WHERE attempt_id = ?",
        (stage["attempt_id"],),
    ).fetchone()
    model_name = snapshot_row["model_name"] if snapshot_row is not None else None

    validated_action = json.loads(stage["validated_action_json"])
    decision_for_transition = {
        "decision_id": decision["decision_id"],
        "owner_state": {"status": "resolved", "actor_id": decision["owner_actor_id"]},
        "decision_state": decision["decision_state"],
        "resolution_kind": decision["resolution_kind"],
        "last_transition_event_id": decision["last_transition_event_id"],
    }
    action_for_transition = {
        "action_id": stage["action_id"], "act": validated_action["act"],
        "delegate_to_actor_id": validated_action.get("delegate_to_actor_id"),
    }
    transition = compute_transition(decision_for_transition, action_for_transition)

    now = int(time.time())
    transition_event_id = transition_event_id or f"pde-event-{uuid.uuid4().hex[:12]}"
    expression_event_id = expression_event_id or f"pde-event-{uuid.uuid4().hex[:12]}"
    main_event_id = main_event_id or f"event-{uuid.uuid4().hex[:12]}"

    try:
        with conn:
            if _test_inject_failure_after == "before_any_write":
                raise RuntimeError("injected test failure: before_any_write")

            conn.execute(
                "INSERT INTO protected_decision_events (event_id, event_type, decision_id, actor_id, "
                "auth_context_id, session_id, establishment_request_id, previous_owner_actor_id, "
                "resulting_owner_actor_id, prior_transition_event_id, related_waking_event_id, "
                "occurred_at, payload_json) VALUES (?, 'decision_state_transitioned', ?, ?, "
                "NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    transition_event_id, decision["decision_id"], clark_actor_id,
                    transition["owner_state_before"]["actor_id"], transition["owner_state_after"]["actor_id"],
                    decision["last_transition_event_id"], main_event_id, now,
                    json.dumps({
                        "action_id": stage["action_id"], "act": validated_action["act"],
                        "decision_state_before": transition["decision_state_before"],
                        "decision_state_after": transition["decision_state_after"],
                        "resolution_kind_before": transition["resolution_kind_before"],
                        "resolution_kind_after": transition["resolution_kind_after"],
                    }),
                ),
            )
            if _test_inject_failure_after == "transition_event_insert":
                raise RuntimeError("injected test failure: transition_event_insert")

            conn.execute(
                "INSERT INTO protected_decision_events (event_id, event_type, decision_id, actor_id, "
                "auth_context_id, session_id, establishment_request_id, previous_owner_actor_id, "
                "resulting_owner_actor_id, prior_transition_event_id, related_waking_event_id, "
                "occurred_at, payload_json) VALUES (?, 'expression_realized', ?, ?, NULL, NULL, NULL, "
                "NULL, NULL, ?, ?, ?, ?)",
                (
                    expression_event_id, decision["decision_id"], clark_actor_id,
                    transition_event_id, main_event_id, now,
                    json.dumps({"action_id": stage["action_id"], "expression_hash": stage["expression_hash"]}),
                ),
            )
            if _test_inject_failure_after == "expression_event_insert":
                raise RuntimeError("injected test failure: expression_event_insert")

            cur = conn.execute(
                "UPDATE protected_decisions SET owner_actor_id = ?, decision_state = ?, "
                "resolution_kind = ?, last_transition_event_id = ?, updated_at = ? "
                "WHERE decision_id = ? AND last_transition_event_id = ?",
                (
                    transition["owner_state_after"]["actor_id"], transition["decision_state_after"],
                    transition["resolution_kind_after"], transition_event_id, now,
                    decision["decision_id"], decision["last_transition_event_id"],
                ),
            )
            if cur.rowcount == 0:
                raise _StaleDecisionRace()
            if _test_inject_failure_after == "decision_update":
                raise RuntimeError("injected test failure: decision_update")

            conn.execute(
                "UPDATE protected_decision_stages SET status = 'committed', committed_event_id = ? "
                "WHERE stage_id = ?",
                (transition_event_id, stage_id),
            )
            if _test_inject_failure_after == "stage_commit":
                raise RuntimeError("injected test failure: stage_commit")

            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, 'protected_decision_expression', ?, 'known', NULL, NULL, ?, ?, ?)",
                (main_event_id, pipeline_id, stage_id, now, now),
            )
            if _test_inject_failure_after == "main_event_insert":
                raise RuntimeError("injected test failure: main_event_insert")

            # Creator/speaker (spec sections 15-17): event_components,
            # the same real production creator-attribution mechanism
            # native_provenance_writer.py uses for ordinary waking-turn
            # authorship -- NOT event_requesters, which is
            # requester/initiator, not speaker (section 15's own
            # correction). No event_requesters row is written on this
            # event at all (section 19 -- see module docstring for why).
            expression_text = stage["expression_text"]
            model_revision_id = resolve_or_create_model_revision(conn, model_name, now) if model_name else None
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, span_start, span_end, "
                "model_revision_id) VALUES (?, 0, ?, 'protected_decision_expression', 'resolved', ?, ?, "
                "NULL, NULL, ?)",
                (main_event_id, clark_actor_id, expression_text, stage["expression_hash"], model_revision_id),
            )
            if _test_inject_failure_after == "component_insert":
                raise RuntimeError("injected test failure: component_insert")

            # Addressee (spec section 18): event_subjects role='addressee'
            # remains appropriate -- unchanged from API2-S1.
            conn.execute(
                "INSERT INTO event_subjects (event_id, subject_actor_id, role) VALUES (?, ?, 'addressee')",
                (main_event_id, decision["counterpart_actor_id"]),
            )
    except _StaleDecisionRace:
        return None, Api2Failure.STALE_DECISION_STATE

    return {
        "decision_id": decision["decision_id"], "stage_id": stage_id, "action_id": stage["action_id"],
        "transition_event_id": transition_event_id, "main_event_id": main_event_id,
        "owner_actor_id": transition["owner_state_after"]["actor_id"],
        "decision_state": transition["decision_state_after"],
        "resolution_kind": transition["resolution_kind_after"],
    }, None


# ------------------------------------------------------- top-level gate seam -


def execute_authoritative_owner_route(
    conn, capability, decision_id, clark_actor_id, expected_last_transition_event_id,
    prepare_callable, pass1_callable, pass2_callable, model_name, think, enabled=None,
):
    """The single top-level entry point requiring BOTH
    AUTHORITATIVE_DECISION_PATH_ENABLED and a valid, unconsumed,
    decision-scoped Capability before any preparation/model invocation
    is even attempted. Neither this gate nor api1_control_plane's
    AUTHORITATIVE_PDE_ENABLED independently creates a publicly
    reachable partial-authority route -- this function only ever
    operates on an ALREADY-ESTABLISHED (TX1-committed) protected
    decision; it never calls submit_authoritative_create_and_assign
    itself."""
    if enabled is None:
        enabled = AUTHORITATIVE_DECISION_PATH_ENABLED
    if not enabled:
        return None, Api2Failure.FEATURE_DISABLED

    _cap, cap_failure = consume_capability(capability, decision_id)
    if cap_failure is not None:
        return None, cap_failure

    attempt, failure = create_attempt(conn, decision_id, expected_last_transition_event_id)
    if failure is not None:
        return None, failure

    _prep_result, failure, _prep_calls = run_preparation(
        conn, attempt["attempt_id"], prepare_callable, model_name, think
    )
    if failure is not None:
        return None, failure

    attempt_row = conn.execute(
        "SELECT * FROM protected_decision_attempts WHERE attempt_id = ?", (attempt["attempt_id"],)
    ).fetchone()
    route = build_protected_decision_route(conn, decision_id, clark_actor_id)
    if route is None:
        return None, Api2Failure.DECISION_NOT_FOUND

    stage, failure, _pass1_calls = run_pass1(conn, dict(attempt_row), route, pass1_callable, clark_actor_id)
    if failure is not None:
        return None, failure

    stage2, failure, _pass2_calls = run_pass2(conn, stage, pass2_callable)
    if failure is not None:
        return None, failure

    result, failure = commit_tx2(conn, stage2["stage_id"], clark_actor_id)
    if failure is not None:
        return None, failure
    return result, None
