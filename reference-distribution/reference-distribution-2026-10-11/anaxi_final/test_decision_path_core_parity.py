"""API2-LR1-S1 section 5: behavioral parity between promoted
production-core primitives (decision_path_core.py) and the original
scratch HDI2-S1 implementation (scratchpad/hdi2), which remains in
place as the regression oracle. Zero model calls.
"""
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
SCRATCHPAD = os.path.join(ANAXI_FINAL, "scratchpad")
sys.path.insert(0, ANAXI_FINAL)
sys.path.insert(0, SCRATCHPAD)

import decision_path_core as core
from hdi2 import schemas as scratch_schemas
from hdi2 import transition as scratch_transition
from hdi2 import release as scratch_release


def test_act_enum_values_identical():
    assert core.ACT_VALUES == scratch_schemas.ACT_VALUES
    assert {a.value for a in core.Act} == {a.value for a in scratch_schemas.Act}


def test_stage_status_values_identical():
    assert {s.value for s in core.StageStatus} == {s.value for s in scratch_schemas.StageStatus}


def test_raw_action_allowed_fields_identical():
    assert core.RAW_ACTION_ALLOWED_FIELDS == scratch_schemas.RAW_ACTION_ALLOWED_FIELDS


def test_validation_failure_values_identical():
    assert {f.value for f in core.ValidationFailure} == {f.value for f in scratch_schemas.ValidationFailure}


def test_release_failure_values_identical():
    assert {f.value for f in core.ReleaseFailure} == {f.value for f in scratch_schemas.ReleaseFailure}


def test_max_expression_chars_identical():
    assert core.MAX_EXPRESSION_CHARS == scratch_schemas.MAX_EXPRESSION_CHARS


def _decision(owner_actor_id="clark", decision_state="open", resolution_kind=None):
    return {
        "decision_id": "dec-parity-1",
        "owner_state": {"status": "resolved", "actor_id": owner_actor_id},
        "decision_state": decision_state,
        "resolution_kind": resolution_kind,
        "last_transition_event_id": "evt-parity-0",
    }


def test_compute_transition_parity_all_six_acts():
    for act, delegate_to in (
        ("choose", None), ("refuse", None), ("defer", None),
        ("ask_clarification", None), ("request_advice", None), ("delegate", "human-x"),
    ):
        decision = _decision()
        validated_action = {"action_id": "action-parity-1", "act": act, "delegate_to_actor_id": delegate_to}
        core_result = core.compute_transition(decision, validated_action)
        scratch_result = scratch_transition.compute_transition(decision, validated_action)
        assert core_result == scratch_result, f"act={act}"


def test_looks_like_structured_control_payload_parity():
    samples = [
        "plain conversational text",
        "```json\n{\"a\": 1}\n```",
        "---\ntitle: x\n---",
        '{"a": 1}',
        "[1, 2, 3]",
        "{not valid json",
        "",
        "   ",
        "I choose option A because it seems best given the context.",
    ]
    for text in samples:
        assert core.looks_like_structured_control_payload(text) == scratch_release.looks_like_structured_control_payload(text), repr(text)


ALL_TESTS = [
    test_act_enum_values_identical,
    test_stage_status_values_identical,
    test_raw_action_allowed_fields_identical,
    test_validation_failure_values_identical,
    test_release_failure_values_identical,
    test_max_expression_chars_identical,
    test_compute_transition_parity_all_six_acts,
    test_looks_like_structured_control_payload_parity,
]


def main():
    passed, failed = 0, 0
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
