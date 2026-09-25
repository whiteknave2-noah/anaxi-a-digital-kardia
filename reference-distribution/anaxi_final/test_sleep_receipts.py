"""
Tests for sleep_receipts.py and its wiring into llama_sleep.py's
run_sleep(). Covers the five properties specified: one receipt per
cycle, expected fields present, failure represented explicitly rather
than as false success, receipt generation cannot mutate Kardia or
proposals, and multiple cycles produce distinct receipts.

Separate from test_anaxi_protocol.py and test_relational_history.py --
this is the new observability layer's own coverage, not a change to
what those already establish.

Setup: save alongside sleep_receipts.py, llama_sleep.py.
    pip install pytest

Run:
    pytest test_sleep_receipts.py -v
"""

import inspect
import json

from sleep_receipts import SleepReceiptStore
from orchestration import AnaxiOrchestrator


EXPECTED_FIELDS = {
    "cycle_id", "user_id", "sleep_start_timestamp",
    "sleep_completion_timestamp", "cycle_end_timestamp",
    "sleep_success", "identity_success",
    "memories_considered", "memories_retained", "memories_discarded",
    "kardia_reflections_generated", "proposals_created", "proposals_pending",
    "errors",
}


def make_receipt(store, cycle_id=None, **overrides):
    defaults = dict(
        cycle_id=cycle_id or store.new_cycle_id(),
        user_id="nate",
        sleep_start_timestamp=1000,
        sleep_success=True,
        identity_success=True,
        memories_considered=2,
        memories_retained=1,
        memories_discarded=1,
        kardia_reflections_generated=1,
        proposals_created=1,
        proposals_pending=1,
        errors=[],
        sleep_completion_timestamp=1010,
        cycle_end_timestamp=1012,
    )
    defaults.update(overrides)
    store.record_receipt(**defaults)
    return defaults["cycle_id"]


# ---------------------------------------------------------------------
# 1 & 2. A cycle produces exactly one receipt, with the expected fields.
# ---------------------------------------------------------------------

def test_successful_cycle_produces_exactly_one_receipt_with_expected_fields(tmp_path):
    store = SleepReceiptStore(str(tmp_path / "receipts.db"))
    cycle_id = make_receipt(store)

    all_receipts = store.receipts_for_user("nate")
    assert len(all_receipts) == 1

    receipt = store.get_receipt(cycle_id)
    store.close()

    assert receipt is not None
    assert set(receipt.keys()) == EXPECTED_FIELDS
    assert receipt["sleep_success"] is True
    assert receipt["identity_success"] is True
    assert receipt["memories_considered"] == 2
    assert receipt["errors"] == []


# ---------------------------------------------------------------------
# 3. Failure is represented explicitly, not as false success.
# ---------------------------------------------------------------------

def test_failed_cycle_is_recorded_as_failed_not_success(tmp_path):
    store = SleepReceiptStore(str(tmp_path / "receipts.db"))
    cycle_id = make_receipt(
        store,
        sleep_success=False,
        identity_success=False,
        memories_considered=0,
        memories_retained=0,
        memories_discarded=0,
        kardia_reflections_generated=0,
        proposals_created=0,
        proposals_pending=0,
        errors=["REM proposal step: JSONDecodeError: Expecting value"],
    )

    receipt = store.get_receipt(cycle_id)
    store.close()

    assert receipt["sleep_success"] is False
    assert receipt["identity_success"] is False
    assert receipt["errors"] != []
    assert "JSONDecodeError" in receipt["errors"][0]


# ---------------------------------------------------------------------
# SLP1-E: llama_sleep.run_sleep() -- the actual legacy REM/reflection/
# governance/Kardia semantic entrypoint, not merely its CLI wrapper --
# is now mechanically disabled. It fails closed immediately (prints
# LEGACY_SLEEP_DISABLED_MESSAGE and raises RuntimeError) before writing
# any receipt, calling the model, or touching AnaxiOrchestrator's
# governance. The two tests that used to exercise run_sleep()'s own
# REM-failure/consolidation-failure receipt behavior are gone: that
# behavior is now permanently unreachable by construction, and the
# narrower claim they cared about (failure is recorded explicitly, not
# as false success) is already independently covered above by
# test_failed_cycle_is_recorded_as_failed_not_success, which exercises
# the receipt store directly and needs no legacy Sleep execution at all.
# ---------------------------------------------------------------------

def test_run_sleep_itself_fails_closed_before_any_legacy_semantic_work(tmp_path, monkeypatch):
    """Disabling only main()'s CLI dispatch would leave run_sleep()
    directly callable by any Python caller -- the actual semantic
    entrypoint must fail closed itself. Proves: raises before writing a
    receipt, before calling the model, and before touching
    AnaxiOrchestrator's governance at all."""
    import llama_sleep

    def poison_ask_llama_for_json(messages):
        raise AssertionError("ask_llama_for_json must never be called -- run_sleep() is disabled")

    monkeypatch.setattr(llama_sleep, "ask_llama_for_json", poison_ask_llama_for_json)
    monkeypatch.setattr(llama_sleep, "LOG_FILE", str(tmp_path / "log.jsonl"))

    orch = AnaxiOrchestrator(str(tmp_path / "mind.db"))
    receipts = SleepReceiptStore(str(tmp_path / "receipts.db"))

    raised = False
    try:
        llama_sleep.run_sleep(orch, receipts)
    except RuntimeError as e:
        raised = True
        assert "LEGACY_SLEEP_DISABLED" in str(e)

    all_receipts = receipts.receipts_for_user("nate")
    orch.close()
    receipts.close()

    assert raised, "run_sleep() must fail closed, not silently no-op or succeed"
    assert all_receipts == [], "no receipt may be written for a call that never ran"


# ---------------------------------------------------------------------
# OWC9-P4 section 5's own aggregate REM/reflection budget-preflight
# claim, preserved but now exercised directly against the non-semantic
# pieces (_compose_rem_budget/_compose_reflection_budget, the shared
# ask_llama_for_json transport wrapper, and AnaxiOrchestrator's plain
# prompt-template builders build_rem_prompt/build_reflection_prompt --
# none of which mutate Kardia or run governance) rather than through
# the now-disabled run_sleep() semantic entrypoint. Same regression
# claim as before: an oversized conversation log fails BOTH budget
# checks closed with zero model calls; an ordinary one does not.
# ---------------------------------------------------------------------

def test_owc9p4_oversized_conversation_budget_exceeded_no_model_call(tmp_path, monkeypatch):
    import llama_sleep

    def exploding_ask_llama_for_json(messages):
        raise AssertionError("ask_llama_for_json must never be called when the aggregate budget check fails")

    monkeypatch.setattr(llama_sleep, "ask_llama_for_json", exploding_ask_llama_for_json)

    orch = AnaxiOrchestrator(str(tmp_path / "mind.db"))
    big_conversation = "User: " + ("x" * 4000) + "\nLlama: ok"
    rem_messages = orch.build_rem_prompt(big_conversation, "(none yet)")
    reflection_messages = orch.build_reflection_prompt(
        conversation_summary=big_conversation, identity_signals="(none noted)", user_id="nate",
    )
    orch.close()

    rem_budget = llama_sleep._compose_rem_budget(rem_messages)
    reflection_budget = llama_sleep._compose_reflection_budget(reflection_messages)

    # Exactly the preflight run_sleep() itself used to apply, before
    # ever calling ask_llama_for_json() -- proven here without going
    # through the now-disabled run_sleep() at all.
    assert not rem_budget.fits
    assert not reflection_budget.fits


def test_owc9p4_normal_conversation_budget_fits_and_model_is_callable(tmp_path, monkeypatch):
    import llama_sleep

    calls = {"n": 0}

    def counting_ask_llama_for_json(messages):
        calls["n"] += 1
        return json.dumps({"ok": True})

    monkeypatch.setattr(llama_sleep, "ask_llama_for_json", counting_ask_llama_for_json)

    orch = AnaxiOrchestrator(str(tmp_path / "mind.db"))
    conversation = "User: hello\nLlama: hi there"
    rem_messages = orch.build_rem_prompt(conversation, "(none yet)")
    reflection_messages = orch.build_reflection_prompt(
        conversation_summary=conversation, identity_signals="(none noted)", user_id="nate",
    )
    orch.close()

    rem_budget = llama_sleep._compose_rem_budget(rem_messages)
    reflection_budget = llama_sleep._compose_reflection_budget(reflection_messages)
    assert rem_budget.fits
    assert reflection_budget.fits

    llama_sleep.ask_llama_for_json(rem_messages)
    llama_sleep.ask_llama_for_json(reflection_messages)
    assert calls["n"] == 2


# ---------------------------------------------------------------------
# 4. Receipt generation cannot mutate Kardia or proposals.
# ---------------------------------------------------------------------

def test_receipt_store_has_no_reference_to_kardia_or_proposals():
    """Structural check: the class that writes receipts has no
    dependency on ConstitutionalMind or AnaxiOrchestrator at all --
    it cannot reach Kardia or proposals because nothing in its own
    source imports or references them."""
    source = inspect.getsource(SleepReceiptStore)
    for forbidden in ("ConstitutionalMind", "AnaxiOrchestrator", "active_kardia", "resolve_proposal"):
        assert forbidden not in source


def test_recording_a_receipt_does_not_change_kardia_or_proposals(tmp_path):
    orch = AnaxiOrchestrator(str(tmp_path / "mind.db"))
    kardia_before = dict(orch.mind.get_current_kardia("nate"))
    proposals_before = orch.list_pending_proposals("nate")

    store = SleepReceiptStore(str(tmp_path / "receipts.db"))
    make_receipt(store)
    store.close()

    kardia_after = dict(orch.mind.get_current_kardia("nate"))
    proposals_after = orch.list_pending_proposals("nate")
    orch.close()

    assert kardia_before == kardia_after
    assert proposals_before == proposals_after


# ---------------------------------------------------------------------
# 5. Multiple cycles produce distinct receipts.
# ---------------------------------------------------------------------

def test_multiple_cycles_produce_distinct_receipts(tmp_path):
    store = SleepReceiptStore(str(tmp_path / "receipts.db"))
    id1 = make_receipt(store, sleep_start_timestamp=1000, memories_considered=2)
    id2 = make_receipt(store, sleep_start_timestamp=2000, memories_considered=5)
    id3 = make_receipt(store, sleep_start_timestamp=3000, memories_considered=0)

    assert len({id1, id2, id3}) == 3, "cycle IDs must be unique"

    all_receipts = store.receipts_for_user("nate")
    store.close()

    assert len(all_receipts) == 3
    considered_values = sorted(r["memories_considered"] for r in all_receipts)
    assert considered_values == [0, 2, 5]


# ---------------------------------------------------------------------
# Bonus: append-only, same standard already applied to relational_history.py.
# ---------------------------------------------------------------------

def test_receipt_store_has_no_public_mutation_method():
    public_methods = {m for m in dir(SleepReceiptStore) if not m.startswith("_")}
    forbidden = {"update_receipt", "edit_receipt", "delete_receipt", "modify_receipt"}
    assert not (public_methods & forbidden)
