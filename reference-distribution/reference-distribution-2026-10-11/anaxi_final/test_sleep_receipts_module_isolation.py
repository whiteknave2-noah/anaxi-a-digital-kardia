"""SLP0: regression test for the sys.modules["sleep_receipts"]
test-isolation leak.

Root cause (see SLP0 final report): several test helper functions
(install_fake_heavy_dependencies() in test_conversation_direction_
trace.py, test_gemma_substrate_switch.py, test_llama_gui_conversation_
mode.py, test_owc5_s2_integration.py, and test_workspace_direction.py)
unconditionally replaced sys.modules["sleep_receipts"] with a bare
fake module, with no restoration afterward -- even though
sleep_receipts.py is a pure-stdlib module (json/sqlite3/uuid) that
llama_anaxi.py never actually imports, directly or transitively. The
substitution outlived the test that created it, corrupting any later
same-process consumer that performs a dynamic sys.modules-keyed lookup
against the real module -- concretely, inspect.getsource() does
exactly this internally (module = sys.modules.get(cls.__module__)),
so test_sleep_receipts.py::test_receipt_store_has_no_reference_to_
kardia_or_proposals failed with "TypeError: ... is a built-in class"
whenever it ran, in the same pytest session, after any test using one
of those helpers.

The fix removed the unnecessary fake injection entirely (the smallest
correct repair, once the injection was shown to be genuinely
unneeded) rather than adding save/restore machinery around a
mutation that should never have happened.

This test proves the isolation boundary holds: running the real
contaminating helper leaves sys.modules["sleep_receipts"] byte-for-
byte identical (same object) to what it was before, and a dynamic
lookup against it (inspect.getsource(), the exact mechanism that
failed) still succeeds afterward -- repeated twice, to prove this
isn't merely a first-call coincidence."""
import inspect
import os
import sys

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from sleep_receipts import SleepReceiptStore
import test_conversation_direction_trace as tcdt


def _run_contaminating_helper():
    """The exact real helper every affected test file's own tests call
    -- not a reimplementation or a simplified stand-in."""
    tcdt.fresh_llama_anaxi(
        pass1_value={"act": "develop_current", "thread": "isolation-probe",
                     "direction_request": "none", "relinquish_direction": False},
        pass2_value={"expression": "isolation probe reply"},
    )


def test_sleep_receipts_module_identity_survives_contaminating_helper():
    real_module_before = sys.modules["sleep_receipts"]
    _run_contaminating_helper()
    real_module_after = sys.modules["sleep_receipts"]
    assert real_module_after is real_module_before
    assert real_module_after is sys.modules.get(SleepReceiptStore.__module__)


def test_dynamic_module_lookup_against_sleep_receipts_still_succeeds_after_helper():
    """The exact failure mode: inspect.getsource() internally does
    sys.modules.get(cls.__module__) -- this must keep working after the
    contaminating helper has run, not just before it."""
    _run_contaminating_helper()
    source = inspect.getsource(SleepReceiptStore)
    assert "class SleepReceiptStore" in source


def test_repeated_calls_never_leak_a_substituted_module():
    """Order/repetition-sensitivity check (spec item 6): calling the
    helper multiple times in the same process must never leave a
    substituted module behind, on the first call or any later one."""
    real_module = sys.modules["sleep_receipts"]
    for _ in range(3):
        _run_contaminating_helper()
        assert sys.modules["sleep_receipts"] is real_module
        inspect.getsource(SleepReceiptStore)  # must not raise


ALL_TESTS = [
    test_sleep_receipts_module_identity_survives_contaminating_helper,
    test_dynamic_module_lookup_against_sleep_receipts_still_succeeds_after_helper,
    test_repeated_calls_never_leak_a_substituted_module,
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
    print(f"\nTOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
