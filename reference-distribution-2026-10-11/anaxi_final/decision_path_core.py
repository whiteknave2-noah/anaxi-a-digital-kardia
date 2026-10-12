"""Production-core pure typed-decision-action primitives.

Promoted from the frozen `scratchpad/hdi2` prototype (HDI2-S1) so that
production code (`api2_control_plane.py`) no longer depends on a
scratch/experimental namespace for runtime semantics (API2-LR1-S1
section 3/36). This is code-placement hardening only -- every value
and function body below is copied verbatim from its `scratchpad/hdi2`
counterpart, not redesigned. Exact behavioral parity with the original
scratch implementation is proven by `test_decision_path_core_parity.py`,
which imports both this module and `scratchpad/hdi2` side by side and
compares outputs across all six acts plus release/transition behavior.

`scratchpad/hdi2` itself is deliberately left in place (not deleted)
as a regression oracle for that parity test -- see the module's own
docstring for why. Only the pure primitives production actually uses
are promoted here: the typed-act enum, stage statuses, the raw-action
field contract, validation/release failure vocabularies, the
expression length bound, deterministic transition computation, and the
structural release-payload detector. Scratch DB code, test harnesses,
experimental orchestration, and diagnostic fixtures are NOT promoted --
they stay in `scratchpad/hdi2` and never had a production caller.
"""
import enum
import json

# ------------------------------------------------------------------ enums --


class Act(str, enum.Enum):
    CHOOSE = "choose"
    REFUSE = "refuse"
    DEFER = "defer"
    ASK_CLARIFICATION = "ask_clarification"
    REQUEST_ADVICE = "request_advice"
    DELEGATE = "delegate"


ACT_VALUES = {a.value for a in Act}

OPEN_ACTS = {Act.DEFER.value, Act.ASK_CLARIFICATION.value, Act.REQUEST_ADVICE.value}


class ValidationFailure(str, enum.Enum):
    MALFORMED_ACTION = "MALFORMED_ACTION"
    UNKNOWN_DECISION = "UNKNOWN_DECISION"
    ACTOR_NOT_OWNER = "ACTOR_NOT_OWNER"
    INVALID_ACT = "INVALID_ACT"
    INVALID_CONTENT = "INVALID_CONTENT"
    DELEGATE_RECIPIENT_INVALID = "DELEGATE_RECIPIENT_INVALID"
    DECISION_NOT_OPEN = "DECISION_NOT_OPEN"
    MULTI_DECISION_UPDATE = "MULTI_DECISION_UPDATE"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"


class ReleaseFailure(str, enum.Enum):
    EMPTY_EXPRESSION = "EMPTY_EXPRESSION"
    MISSING_STAGE = "MISSING_STAGE"
    ACTION_BINDING_MISMATCH = "ACTION_BINDING_MISMATCH"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    PROTOCOL_FORMAT_VIOLATION = "PROTOCOL_FORMAT_VIOLATION"
    STALE_DECISION_STATE = "STALE_DECISION_STATE"
    COMMIT_PRECONDITION_FAILED = "COMMIT_PRECONDITION_FAILED"


class StageStatus(str, enum.Enum):
    AWAITING_EXPRESSION = "awaiting_expression"
    EXPRESSION_READY = "expression_ready"
    COMMITTED = "committed"
    ABORTED = "aborted"


RAW_ACTION_ALLOWED_FIELDS = {"decision_id", "act", "content"}
MAX_EXPRESSION_CHARS = 4000

RELEASABLE_STAGE_STATUSES = {
    StageStatus.AWAITING_EXPRESSION.value,
    StageStatus.EXPRESSION_READY.value,
}


# --------------------------------------------------------- pure functions --


def compute_transition(protected_decision, validated_action):
    """Pure function. Returns a ProposedDecisionTransition dict. No DB
    mutation. Verbatim promotion of hdi2.transition.compute_transition."""
    act = validated_action["act"]
    owner_before = protected_decision["owner_state"]
    decision_state_before = protected_decision["decision_state"]
    resolution_before = protected_decision["resolution_kind"]

    owner_after = dict(owner_before)
    decision_state_after = decision_state_before
    resolution_after = resolution_before

    if act == Act.CHOOSE.value:
        decision_state_after = "resolved"
        resolution_after = "chosen"
    elif act == Act.REFUSE.value:
        decision_state_after = "resolved"
        resolution_after = "refused"
    elif act in (Act.DEFER.value, Act.ASK_CLARIFICATION.value, Act.REQUEST_ADVICE.value):
        pass
    elif act == Act.DELEGATE.value:
        owner_after = {
            "status": "resolved",
            "actor_id": validated_action["delegate_to_actor_id"],
        }
    else:  # pragma: no cover - validation already rejects unknown acts
        raise ValueError(f"unreachable: invalid act reached compute_transition: {act!r}")

    return {
        "action_id": validated_action["action_id"],
        "decision_id": protected_decision["decision_id"],
        "owner_state_before": owner_before,
        "owner_state_after": owner_after,
        "decision_state_before": decision_state_before,
        "decision_state_after": decision_state_after,
        "resolution_kind_before": resolution_before,
        "resolution_kind_after": resolution_after,
        "precondition_last_transition_event_id": protected_decision["last_transition_event_id"],
    }


def looks_like_structured_control_payload(text):
    """Mechanical-only detector. No fuzzy judgment. Verbatim promotion
    of hdi2.release.looks_like_structured_control_payload."""
    stripped = text.strip()
    if stripped.startswith("```"):
        return True
    if stripped.startswith("---"):
        return True
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            json.loads(stripped)
            return True
        except (TypeError, ValueError):
            return False
    return False
