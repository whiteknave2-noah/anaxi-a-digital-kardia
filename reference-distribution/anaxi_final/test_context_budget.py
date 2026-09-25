"""OWC9 acceptance tests. Pure unit tests against context_budget.py's
own compositor/estimator -- no model call, no llama_anaxi involvement
(that integration is covered separately in test_owc5_s2_integration.py).
"""
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import context_budget as cb


# ------------------------------------------------------- A: token estimator


def test_estimate_tokens_empty_and_none():
    assert cb.estimate_tokens("") == 0
    assert cb.estimate_tokens(None) == 0


def test_estimate_tokens_is_utf8_byte_count():
    assert cb.estimate_tokens("abc") == 3
    # multi-byte UTF-8 characters cost more than one "token unit" per
    # character -- the conservative upper bound, never chars/4.
    multi_byte = "日本語"  # 3 chars, 9 UTF-8 bytes
    assert cb.estimate_tokens(multi_byte) == len(multi_byte.encode("utf-8"))
    assert cb.estimate_tokens(multi_byte) > len(multi_byte)


def test_estimate_tokens_never_undercounts_relative_to_chars_over_4():
    # Section 4's own forbidden comparison: chars/4 must never exceed
    # this conservative estimate for ordinary ASCII text (it does not
    # -- 1 byte per char for ASCII means chars/4 is always well under
    # the byte count).
    text = "The quick brown fox jumps over the lazy dog." * 20
    casual_chars_over_4 = len(text) / 4
    assert cb.estimate_tokens(text) > casual_chars_over_4


# --------------------------------------------------- B: budget constants


def test_budget_constants_arithmetic():
    assert cb.PASS1_MAX_PROMPT_BUDGET == cb.CONTEXT_CEILING - cb.PASS1_GENERATION_RESERVE - cb.SAFETY_MARGIN
    assert cb.PASS2_MAX_PROMPT_BUDGET == cb.CONTEXT_CEILING - cb.PASS2_GENERATION_RESERVE - cb.SAFETY_MARGIN
    assert cb.PASS1_MAX_PROMPT_BUDGET > 0
    assert cb.PASS2_MAX_PROMPT_BUDGET > 0


def test_pass1_reserve_covers_worst_case_structural_envelope():
    import json
    worst_case = {
        "act": "yield_direction", "thread": "x" * 200,
        "direction_request": "request_human", "relinquish_direction": False,
        "background_activity_request": "resume_own_pause",
    }
    worst_case_cost = cb.estimate_tokens(json.dumps(worst_case))
    assert worst_case_cost < cb.PASS1_GENERATION_RESERVE


# ------------------------------------------------ C: Contribution/compose


def test_contribution_rejects_unknown_kind():
    raised = False
    try:
        cb.Contribution("not_a_real_kind", "x", hard=True)
    except ValueError:
        raised = True
    assert raised


def test_contribution_droppable_units_requires_render_fn():
    raised = False
    try:
        cb.Contribution(cb.RECENT_DIALOGUE, "x", hard=False, droppable_units=[1, 2])
    except ValueError:
        raised = True
    assert raised


def test_compose_fits_without_trimming_when_everything_fits():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 100, hard=True)
    soft = cb.Contribution(cb.RECENT_DIALOGUE, "y" * 100, hard=False,
                            droppable_units=[1, 2, 3], render_fn=lambda u: "y" * (len(u) * 33))
    result = cb.compose_within_budget([hard, soft], 1000)
    assert result.fits is True
    assert result.dropped_kinds == []
    assert result.trimmed_kinds == []
    assert len(result.included) == 2


def test_compose_hard_only_overflow_fails_closed():
    huge_hard = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "z" * 5000, hard=True)
    result = cb.compose_within_budget([huge_hard], 1000)
    assert result.fits is False
    assert result.final_prompt_cost == 5000
    assert result.included == [huge_hard]  # diagnostic visibility, but caller must not proceed


def test_compose_trims_droppable_units_oldest_first():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 100, hard=True)
    units = ["a", "b", "c", "d"]  # oldest first, chronological
    soft = cb.Contribution(
        cb.RECENT_DIALOGUE, "".join(units) * 100, hard=False,
        droppable_units=list(units), render_fn=lambda u: "".join(u) * 100,
    )
    # budget only large enough for hard + ~2 units
    result = cb.compose_within_budget([hard, soft], 100 + 250)
    assert result.fits is True
    survivor = result.included_kind(cb.RECENT_DIALOGUE)
    assert survivor is not None
    # newest units (from the END of the original list) survive
    assert survivor.droppable_units == units[-len(survivor.droppable_units):]
    assert cb.RECENT_DIALOGUE in result.trimmed_kinds


def test_compose_drops_atomic_soft_contribution_whole():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 100, hard=True)
    atomic_soft = cb.Contribution(cb.EPISODE_CONTEXT, "e" * 2000, hard=False)
    result = cb.compose_within_budget([hard, atomic_soft], 150)
    assert result.fits is True
    assert result.included_kind(cb.EPISODE_CONTEXT) is None
    assert cb.EPISODE_CONTEXT in result.dropped_kinds


def test_compose_follows_fixed_trim_order_not_semantic():
    # RETRIEVED_HISTORY trims before EPISODE_CONTEXT before
    # ACTIVE_WORKSPACE_CONTINUITY before RECENT_DIALOGUE (spec section
    # 9's own SOFT_TRIM_ORDER) -- proven directly: with just enough
    # budget for hard + ONE soft family, the LAST-in-trim-order family
    # (recent_dialogue) survives, the earlier ones are dropped first.
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 10, hard=True)
    retrieved = cb.Contribution(cb.RETRIEVED_HISTORY, "r" * 500, hard=False)
    episode = cb.Contribution(cb.EPISODE_CONTEXT, "e" * 500, hard=False)
    dialogue_units = ["p1"]
    dialogue = cb.Contribution(
        cb.RECENT_DIALOGUE, "p1" * 1, hard=False,
        droppable_units=dialogue_units, render_fn=lambda u: "".join(u),
    )
    result = cb.compose_within_budget([hard, retrieved, episode, dialogue], 15)
    assert result.fits is True
    assert cb.RETRIEVED_HISTORY in result.dropped_kinds
    assert cb.EPISODE_CONTEXT in result.dropped_kinds
    assert result.included_kind(cb.RECENT_DIALOGUE) is not None  # last in trim order -- survives


def test_compose_absent_kind_never_touched():
    # A kind not present in `contributions` at all is simply never seen
    # by the trim loop -- confirmed by NOT appearing in dropped_kinds
    # (dropped_kinds only ever lists a kind that was actually present
    # and actually removed).
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 5000, hard=True)
    result = cb.compose_within_budget([hard], 10)  # hard alone overflows
    assert result.fits is False
    assert cb.EPISODE_CONTEXT not in result.dropped_kinds
    assert cb.RECENT_DIALOGUE not in result.dropped_kinds


def test_compose_never_tears_a_single_unit():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "", hard=True)
    units = ["short", "a-much-longer-unit-of-text-here"]
    soft = cb.Contribution(
        cb.RECENT_DIALOGUE, "".join(units), hard=False,
        droppable_units=list(units), render_fn=lambda u: "".join(u),
    )
    # budget tight enough that only the newest, larger unit could fit
    # -- proves the survivor is the COMPLETE unit, never a substring.
    budget = len(units[-1]) + 5
    result = cb.compose_within_budget([hard, soft], budget)
    survivor = result.included_kind(cb.RECENT_DIALOGUE)
    if survivor is not None and survivor.droppable_units:
        assert survivor.droppable_units[-1] == units[-1]  # complete, not sliced


def test_delivered_source_ids_empty_when_dropped():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 10, hard=True)
    dropped_soft = cb.Contribution(
        cb.ACTIVE_WORKSPACE_CONTINUITY, "a" * 5000, hard=False, source_ids=["evt-1", "evt-2"],
    )
    result = cb.compose_within_budget([hard, dropped_soft], 20)
    assert result.fits is True
    assert result.delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY) == []


def test_delivered_source_ids_present_when_included():
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 10, hard=True)
    included_soft = cb.Contribution(
        cb.ACTIVE_WORKSPACE_CONTINUITY, "a" * 10, hard=False, source_ids=["evt-1", "evt-2"],
    )
    result = cb.compose_within_budget([hard, included_soft], 1000)
    assert result.fits is True
    assert result.delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY) == ["evt-1", "evt-2"]


def test_delivered_source_ids_partial_after_unit_trimming():
    # Section 18: droppable-units trimming must shrink source_ids to
    # match what actually survives, not the original full list.
    hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x" * 10, hard=True)
    events = [{"event_id": "evt-1", "rendered": "aaaaaaaaaa"}, {"event_id": "evt-2", "rendered": "bbbbbbbbbb"}]

    def render(evts):
        return "".join(e["rendered"] for e in evts)

    contrib = cb.Contribution(
        cb.ACTIVE_WORKSPACE_CONTINUITY, render(events), hard=False,
        droppable_units=list(events), render_fn=render,
        source_ids=[e["event_id"] for e in events],
    )
    # This test's own source_ids were fixed at construction time (the
    # ORIGINAL full list) -- proving the wiring-layer responsibility:
    # a real caller (llama_anaxi.py) must derive delivered source_ids
    # from the SURVIVING droppable_units after composition, not from
    # the Contribution's own static source_ids field. Confirmed here
    # via the units that actually remain:
    budget = 10 + len(events[-1]["rendered"]) + 2
    result = cb.compose_within_budget([hard, contrib], budget)
    survivor = result.included_kind(cb.ACTIVE_WORKSPACE_CONTINUITY)
    assert survivor is not None
    surviving_ids = [e["event_id"] for e in survivor.droppable_units]
    assert surviving_ids == ["evt-2"]  # newest survives, oldest dropped


# ------------------------------------------- D: observed failure regression


def test_owc9_reproduces_and_repairs_observed_live_failure_geometry():
    # Section 16: synthetic reproduction of the measured production
    # failure (prompt_eval_count=4092 against a 4096 ceiling, 4 tokens
    # of generation room, done_reason="length"). No model call is made
    # anywhere in this test -- purely arithmetic over the same
    # constants/algorithm the real call sites use.
    hard_core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "s" * 2000, hard=True)
    hard_human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "h" * 972, hard=True)  # matches observed incoming_message_char_count
    dialogue_units = list(range(6))
    soft_dialogue = cb.Contribution(
        cb.RECENT_DIALOGUE, "d" * (len(dialogue_units) * 180), hard=False,
        droppable_units=dialogue_units, render_fn=lambda u: "d" * (len(u) * 180),
    )
    soft_awc = cb.Contribution(cb.ACTIVE_WORKSPACE_CONTINUITY, "w" * 300, hard=False)

    pretrim_total = hard_core.cost + hard_human.cost + soft_dialogue.cost + soft_awc.cost
    assert pretrim_total >= cb.CONTEXT_CEILING - 10  # reproduces "near/over the ceiling"

    result = cb.compose_within_budget(
        [hard_core, hard_human, soft_dialogue, soft_awc], cb.PASS1_MAX_PROMPT_BUDGET,
    )
    assert result.fits is True
    remaining_for_generation = cb.CONTEXT_CEILING - result.final_prompt_cost
    # Before this gate: 4 tokens remained. After: comfortably protected.
    assert remaining_for_generation >= cb.PASS1_GENERATION_RESERVE + cb.SAFETY_MARGIN
    assert remaining_for_generation > 4


def test_owc9_hard_context_overflow_case_fails_closed_deterministically():
    # Section 16's second required case: mandatory control/system +
    # current message + minimal mechanical state cannot fit even with
    # every soft contributor dropped -> deterministic pre-call
    # BUDGET_EXCEEDED, no model call (proven at the wiring layer in
    # test_owc5_s2_integration.py; this proves the compositor's own
    # honest refusal).
    hard_core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "s" * 3000, hard=True)
    hard_human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "h" * 3000, hard=True)
    result = cb.compose_within_budget([hard_core, hard_human], cb.PASS1_MAX_PROMPT_BUDGET)
    assert result.fits is False
    assert result.final_prompt_cost == 6000


def test_image_admission_cost_covers_measured_envelope_worst_case():
    # CAP2A-C: standing regression for the envelope-wide vision
    # calibration -- IMAGE_ADMISSION_TOKEN_COST must stay strictly
    # above the actual measured worst-case prompt_eval_count observed
    # anywhere in the accepted image envelope (512x512 up to
    # 2048x2048, plus extreme aspect ratios -- see context_budget.py's
    # own module comment for the full measurement table), so a future
    # edit to either constant can never silently drift the safety
    # margin to zero or negative without this test catching it.
    assert cb.IMAGE_ADMISSION_TOKEN_COST > cb.MEASURED_WORST_CASE_IMAGE_PROMPT_EVAL_COUNT_CAP2A
    assert cb.MEASURED_WORST_CASE_IMAGE_PROMPT_EVAL_COUNT_CAP2A == 270
    assert cb.IMAGE_ADMISSION_TOKEN_COST == 512


def test_build_image_contribution_uses_calibrated_cost_not_text_estimate():
    contrib = cb.build_image_contribution()
    assert contrib.hard is True
    assert contrib.cost == cb.IMAGE_ADMISSION_TOKEN_COST
    # The placeholder rendered_text is short -- if .cost were derived
    # from it via estimate_tokens(), it would be nowhere near the real
    # calibrated constant, proving .cost is genuinely an override, not
    # a byte-count coincidence.
    assert cb.estimate_tokens(contrib.rendered_text) != cb.IMAGE_ADMISSION_TOKEN_COST


def test_image_admission_hard_overflow_fails_closed_zero_inference():
    # Mirrors test_owc9_hard_context_overflow_case_fails_closed_
    # deterministically -- an admitted image's HARD cost, alongside
    # other HARD contributions, must fail the whole composition closed
    # rather than ever being silently trimmed or partially sent.
    hard_core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "s" * 3000, hard=True)
    hard_human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "h" * 3000, hard=True)
    image_contrib = cb.build_image_contribution()
    result = cb.compose_within_budget([hard_core, hard_human, image_contrib], cb.PASS1_MAX_PROMPT_BUDGET)
    assert result.fits is False
    assert result.final_prompt_cost == 6000 + cb.IMAGE_ADMISSION_TOKEN_COST


# ========================================================= CAP2E: qwen3-vl:4b


def test_qwen_image_admission_cost_flat_plateau():
    # CAP2E measured: 512x512 and 1024x1024 both plateau at the same
    # cost (both areas <= QWEN_IMAGE_PLATEAU_AREA_PX).
    assert cb.qwen_image_admission_cost(512, 512) == cb.QWEN_IMAGE_PLATEAU_TOKEN_COST
    assert cb.qwen_image_admission_cost(1024, 1024) == cb.QWEN_IMAGE_PLATEAU_TOKEN_COST
    assert cb.qwen_image_admission_cost(2048, 16) == cb.QWEN_IMAGE_PLATEAU_TOKEN_COST
    assert cb.qwen_image_admission_cost(16, 2048) == cb.QWEN_IMAGE_PLATEAU_TOKEN_COST


def test_qwen_image_admission_cost_scales_conservatively_above_plateau():
    # CAP2E measured 1536x1536 -> 2317 real tokens; the formula must
    # NEVER undercount that real measurement.
    predicted_1536 = cb.qwen_image_admission_cost(1536, 1536)
    assert predicted_1536 >= 2317
    # CAP2E measured 2048x2048 exceeded the entire 4096-token context
    # (server rejected the request outright) -- the formula must place
    # this image's cost far beyond QWEN_VISION_MAX_PROMPT_BUDGET, so
    # compose_within_budget() fails it closed before any inference is
    # attempted, never relying on the server's own 400 error as the
    # containment mechanism.
    predicted_2048 = cb.qwen_image_admission_cost(2048, 2048)
    assert predicted_2048 > cb.QWEN_VISION_MAX_PROMPT_BUDGET
    assert predicted_2048 > predicted_1536 > cb.QWEN_IMAGE_PLATEAU_TOKEN_COST


def test_qwen_image_admission_cost_monotonic_in_area():
    sizes = [(512, 512), (1024, 1024), (1536, 1536), (2048, 2048)]
    costs = [cb.qwen_image_admission_cost(w, h) for w, h in sizes]
    assert costs == sorted(costs)


def test_build_qwen_image_contribution_uses_formula_not_text_estimate():
    contrib = cb.build_qwen_image_contribution(512, 512)
    assert contrib.hard is True
    assert contrib.cost == cb.QWEN_IMAGE_PLATEAU_TOKEN_COST
    assert cb.estimate_tokens(contrib.rendered_text) != contrib.cost


def test_qwen_budget_profile_is_independent_of_gemma():
    # CAP2E: distinct constants, not gemma4's own reused by analogy.
    assert cb.QWEN_VISION_GENERATION_RESERVE != cb.PASS2_GENERATION_RESERVE
    assert cb.QWEN_VISION_MAX_PROMPT_BUDGET == (
        cb.QWEN_VISION_CONTEXT_CEILING - cb.QWEN_VISION_GENERATION_RESERVE - cb.QWEN_VISION_SAFETY_MARGIN
    )


def test_qwen_large_image_hard_overflow_fails_closed_zero_inference():
    hard_core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "s" * 500, hard=True)
    hard_human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "h" * 500, hard=True)
    image_contrib = cb.build_qwen_image_contribution(2048, 2048)
    result = cb.compose_within_budget([hard_core, hard_human, image_contrib], cb.QWEN_VISION_MAX_PROMPT_BUDGET)
    assert result.fits is False


ALL_TESTS = [
    test_estimate_tokens_empty_and_none,
    test_estimate_tokens_is_utf8_byte_count,
    test_estimate_tokens_never_undercounts_relative_to_chars_over_4,
    test_budget_constants_arithmetic,
    test_pass1_reserve_covers_worst_case_structural_envelope,
    test_contribution_rejects_unknown_kind,
    test_contribution_droppable_units_requires_render_fn,
    test_compose_fits_without_trimming_when_everything_fits,
    test_compose_hard_only_overflow_fails_closed,
    test_compose_trims_droppable_units_oldest_first,
    test_compose_drops_atomic_soft_contribution_whole,
    test_compose_follows_fixed_trim_order_not_semantic,
    test_compose_absent_kind_never_touched,
    test_compose_never_tears_a_single_unit,
    test_delivered_source_ids_empty_when_dropped,
    test_delivered_source_ids_present_when_included,
    test_delivered_source_ids_partial_after_unit_trimming,
    test_owc9_reproduces_and_repairs_observed_live_failure_geometry,
    test_owc9_hard_context_overflow_case_fails_closed_deterministically,
    test_image_admission_cost_covers_measured_envelope_worst_case,
    test_build_image_contribution_uses_calibrated_cost_not_text_estimate,
    test_image_admission_hard_overflow_fails_closed_zero_inference,
    test_qwen_image_admission_cost_flat_plateau,
    test_qwen_image_admission_cost_scales_conservatively_above_plateau,
    test_qwen_image_admission_cost_monotonic_in_area,
    test_build_qwen_image_contribution_uses_formula_not_text_estimate,
    test_qwen_budget_profile_is_independent_of_gemma,
    test_qwen_large_image_hard_overflow_fails_closed_zero_inference,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
