"""SLP1-S1 acceptance tests proving anaxi_sleep.py (the legacy
Claude-substrate Sleep pathway) is mechanically disabled. No real
Anthropic API call anywhere in this file -- ask_claude_for_json is
monkeypatched to a poison function that raises if ever called, for
every invocation shape.
"""
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import anaxi_sleep


class _PoisonCalled(Exception):
    pass


def _poison_ask_claude_for_json(messages):
    raise _PoisonCalled("ask_claude_for_json must never be called -- anaxi_sleep.py is disabled")


def _run_main_with_argv(argv):
    old_argv = sys.argv
    old_ask = anaxi_sleep.ask_claude_for_json
    sys.argv = ["anaxi_sleep.py"] + argv
    anaxi_sleep.ask_claude_for_json = _poison_ask_claude_for_json
    exit_code = None
    try:
        try:
            anaxi_sleep.main()
        except SystemExit as e:
            exit_code = e.code
    finally:
        sys.argv = old_argv
        anaxi_sleep.ask_claude_for_json = old_ask
    return exit_code


def test_no_args_invocation_fails_closed():
    exit_code = _run_main_with_argv([])
    assert exit_code == 1


def test_list_invocation_fails_closed():
    exit_code = _run_main_with_argv(["--list"])
    assert exit_code == 1


def test_resolve_invocation_fails_closed():
    exit_code = _run_main_with_argv(["--resolve", "3", "accept"])
    assert exit_code == 1


def test_unknown_argument_invocation_fails_closed():
    exit_code = _run_main_with_argv(["--something-else"])
    assert exit_code == 1


def test_every_invocation_shape_produces_zero_model_calls():
    # If ask_claude_for_json were ever reached, _run_main_with_argv
    # itself would have let a _PoisonCalled exception propagate out of
    # main() uncaught (main() has no try/except around its body), which
    # would surface here as a raised exception rather than a clean
    # SystemExit -- proving no test above accidentally reached it.
    for argv in ([], ["--list"], ["--resolve", "1", "reject"], ["--anything"]):
        _run_main_with_argv(argv)  # raises _PoisonCalled if disabled guard is ever bypassed


def test_disable_guard_precedes_orchestrator_construction():
    # Structural proof, not just behavioral: the guard in main() must
    # be reachable before AnaxiOrchestrator(DB_PATH) is ever
    # constructed, so a missing/locked/absent anaxi_mind.db can never
    # even matter. Verified by source order: the disable print/exit
    # appears in main()'s source before any reference to
    # AnaxiOrchestrator within the function body.
    # Functional check (actual construction call), not a bare
    # "AnaxiOrchestrator" substring scan -- main()'s own explanatory
    # comment legitimately names AnaxiOrchestrator in prose above the
    # disable guard.
    import inspect
    source = inspect.getsource(anaxi_sleep.main)
    disable_pos = source.index("print(LEGACY_SLEEP_DISABLED_MESSAGE)")
    orchestrator_call_pos = source.find("AnaxiOrchestrator(")
    assert orchestrator_call_pos == -1 or disable_pos < orchestrator_call_pos


def test_run_sleep_list_resolve_functions_remain_defined_on_disk():
    # "Preserve historical source on disk unless a minimal safe guard
    # requires restructuring" -- the underlying functions still exist
    # (archaeology preserved), they are simply unreachable from main().
    assert callable(anaxi_sleep.run_sleep)
    assert callable(anaxi_sleep.list_proposals)
    assert callable(anaxi_sleep.resolve)
    assert callable(anaxi_sleep.ask_claude_for_json)


ALL_TESTS = [
    test_no_args_invocation_fails_closed,
    test_list_invocation_fails_closed,
    test_resolve_invocation_fails_closed,
    test_unknown_argument_invocation_fails_closed,
    test_every_invocation_shape_produces_zero_model_calls,
    test_disable_guard_precedes_orchestrator_construction,
    test_run_sleep_list_resolve_functions_remain_defined_on_disk,
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

    # Proves the disabled message is actually printed, via plain
    # stdout capture (this file's runner is not pytest, so no capsys).
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _run_main_with_argv([])
        assert "LEGACY_SLEEP_DISABLED" in buf.getvalue()
        passed += 1
        print("PASS test_disabled_message_is_printed")
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"FAIL test_disabled_message_is_printed: {exc}")

    total = len(ALL_TESTS) + 1
    print()
    print(f"TOTAL={total} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
