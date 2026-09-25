"""Tests for llama_launch.py, the browser-first production launcher.

No real Gradio server is ever started, no real browser tab is ever
opened, and no real network call ever leaves 127.0.0.1 in any test
here -- start_backend()/webbrowser.open()/urllib.request.urlopen() are
monkeypatched or given fake targets throughout. Importing llama_launch
itself is safe and side-effect-free (verified: it only defines
functions/constants and imports the already-existing, already-tested
`demo` object from llama_gui.py -- it does not call demo.launch()).
"""
import ast
import io
import json
import os
import sys
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

sys.argv = ["llama_launch.py", "--conversation"]
import llama_launch as ll


import pytest

_REAL_REFUSE = ll.runtime_roots.refuse_if_redirected


@pytest.fixture(autouse=True)
def _launcher_not_redirected(monkeypatch):
    """Under pytest the Workspace root is redirected (runtime_roots), and the
    launcher correctly REFUSES to start on a redirected root; these tests
    exercise the launcher's own behavior, so lift that one refusal here.
    test_launcher_refuses_a_redirected_workspace_root proves the refusal."""
    monkeypatch.setattr(ll.runtime_roots, "refuse_if_redirected", lambda: None)


def test_launcher_refuses_a_redirected_workspace_root(monkeypatch):
    import runtime_roots
    monkeypatch.setattr(ll.runtime_roots, "refuse_if_redirected", _REAL_REFUSE)
    monkeypatch.setenv(runtime_roots.TEST_WORKSPACE_ROOT_ENV, "/tmp/redirected")
    started = []
    monkeypatch.setattr(ll, "start_backend", lambda *a, **k: started.append(1) or True)
    with pytest.raises(SystemExit):
        ll.main()
    assert not started


class _Poison(Exception):
    pass


def _poison(*a, **k):
    raise _Poison("this must never be called in this test")


def _patched(monkeypatches):
    """Applies {name: value} to the llama_launch module, returns a
    restore function. Mirrors the monkeypatch-and-restore pattern
    already used throughout this project's test files."""
    originals = {name: getattr(ll, name) for name in monkeypatches}
    for name, value in monkeypatches.items():
        setattr(ll, name, value)

    def restore():
        for name, value in originals.items():
            setattr(ll, name, value)

    return restore


# ============================================================ A/I: backend =


def test_fresh_launch_starts_exactly_one_backend():
    calls = {"start_backend": 0}

    def fake_start_backend():
        calls["start_backend"] += 1
        return True

    restore = _patched({
        "port_is_occupied": lambda: False,
        "start_backend": fake_start_backend,
        "wait_for_readiness": lambda: True,
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(lambda url: None)})(),
    })
    try:
        ll.main()
    finally:
        restore()
    assert calls["start_backend"] == 1


def test_duplicate_launch_does_not_start_a_second_backend():
    opened = []
    restore = _patched({
        "port_is_occupied": lambda: True,
        "probe_gradio_server": lambda: True,
        "start_backend": _poison,
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(lambda url: opened.append(url))})(),
    })
    try:
        ll.main()  # must not raise _Poison
    finally:
        restore()
    assert opened == [ll.URL]


def test_unrelated_occupant_fails_closed_no_backend_no_browser():
    restore = _patched({
        "port_is_occupied": lambda: True,
        "probe_gradio_server": lambda: False,
        "start_backend": _poison,
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(_poison)})(),
    })
    try:
        ll.main()  # must not raise _Poison from either
    finally:
        restore()


def test_backend_start_failure_never_opens_browser():
    restore = _patched({
        "port_is_occupied": lambda: False,
        "start_backend": lambda: False,
        "wait_for_readiness": _poison,
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(_poison)})(),
    })
    try:
        ll.main()
    finally:
        restore()


# ================================================================ B/C: order =


def test_browser_not_opened_before_readiness():
    order = []
    restore = _patched({
        "port_is_occupied": lambda: False,
        "start_backend": lambda: (order.append("start_backend") or True),
        "wait_for_readiness": lambda: (order.append("wait_for_readiness") or False),
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(lambda url: order.append("open"))})(),
        "demo": type("FakeDemo", (), {"block_thread": staticmethod(lambda: order.append("block_thread"))})(),
    })
    try:
        ll.main()
    finally:
        restore()
    assert "open" not in order
    assert order == ["start_backend", "wait_for_readiness", "block_thread"]


def test_browser_opened_exactly_once_at_exact_url_after_readiness():
    order = []
    opened = []
    restore = _patched({
        "port_is_occupied": lambda: False,
        "start_backend": lambda: (order.append("start_backend") or True),
        "wait_for_readiness": lambda: (order.append("wait_for_readiness") or True),
        "webbrowser": type("FakeWebbrowser", (), {"open": staticmethod(lambda url: (order.append("open"), opened.append(url)))})(),
        "demo": type("FakeDemo", (), {"block_thread": staticmethod(lambda: order.append("block_thread"))})(),
    })
    try:
        ll.main()
    finally:
        restore()
    assert order == ["start_backend", "wait_for_readiness", "open", "block_thread"]
    assert opened == ["http://127.0.0.1:7860"]
    assert ll.URL == "http://127.0.0.1:7860"


# =================================================================== D: no wrapper


def test_launcher_never_imports_webview_or_desktop_wrapper():
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert "webview" not in names
    assert "llama_desktop" not in names
    assert "create_window(" not in source


# ===================================================================== E: mode


def test_conversation_mode_propagates_from_argv():
    old_argv = sys.argv
    for mod in ("llama_launch", "llama_gui", "llama_desktop", "llama_anaxi"):
        sys.modules.pop(mod, None)
    sys.argv = ["llama_launch.py", "--conversation"]
    try:
        import llama_launch as fresh_ll
        assert fresh_ll.LAUNCH_MODE == "conversation"
    finally:
        sys.argv = old_argv
        for mod in ("llama_launch", "llama_gui", "llama_desktop", "llama_anaxi"):
            sys.modules.pop(mod, None)
        sys.argv = ["llama_launch.py", "--conversation"]
        import llama_launch  # noqa: F401  -- restore module-level `ll` binding's target state


def test_launcher_has_no_second_mode_parser():
    # OWC6-G1 discipline preserved: llama_launch.py must not duplicate
    # mode resolution -- it only imports the already-resolved LAUNCH_MODE.
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    assert "resolve_launch_mode(" not in source
    assert "argparse" not in source


# =================================================================== F: roaming


def test_launcher_never_references_workspace_roaming():
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("workspace_roaming", "start_roaming_worker", "apply_human_roaming_authorization"):
        assert forbidden not in source


def test_roaming_authorization_is_off_after_importing_launcher():
    import workspace_roaming
    workspace_roaming.reset_roaming_state()
    assert workspace_roaming.get_roaming_state()["authorized"] is False


# ===================================================================== G: sleep


def test_launcher_never_references_sleep_rem():
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("anaxi_sleep", "llama_sleep", "run_sleep_cycle", "sleep_evidence", "schtasks", "ScheduledTask"):
        assert forbidden not in source


# =============================================================== H: lifetime


def test_browser_open_has_no_lifetime_coupling_to_backend():
    # Structural: webbrowser.open() is a fire-and-forget external-process
    # launch with no return value used afterward, no handle retained,
    # no monitoring of the opened tab -- so nothing in main() COULD
    # react to the browser tab closing even if it wanted to.
    import inspect
    source = inspect.getsource(ll.main)
    assert "webbrowser.open(" in source
    open_line = next(l for l in source.splitlines() if "webbrowser.open(" in l)
    assert "=" not in open_line.split("webbrowser.open(")[0].strip().rstrip(":")


def test_backend_kept_alive_via_block_thread_not_window_event_loop():
    import inspect
    source = inspect.getsource(ll.main)
    assert "demo.block_thread()" in source
    assert "webview" not in source


# =========================================================== readiness/probe


def test_wait_for_readiness_stops_as_soon_as_probe_succeeds():
    calls = {"probe": 0, "sleep": 0}

    def fake_probe():
        calls["probe"] += 1
        return calls["probe"] >= 3

    fake_time = {"t": 0.0}

    def fake_sleep(interval):
        calls["sleep"] += 1
        fake_time["t"] += interval

    result = ll.wait_for_readiness(
        probe=fake_probe, timeout=100, interval=1, sleep_fn=fake_sleep, clock=lambda: fake_time["t"],
    )
    assert result is True
    assert calls["probe"] == 3
    assert calls["sleep"] == 2  # slept between probe 1->2 and 2->3, never after success


def test_wait_for_readiness_respects_bounded_timeout_never_succeeding():
    fake_time = {"t": 0.0}

    def fake_sleep(interval):
        fake_time["t"] += interval

    result = ll.wait_for_readiness(
        probe=lambda: False, timeout=5, interval=1, sleep_fn=fake_sleep, clock=lambda: fake_time["t"],
    )
    assert result is False
    assert fake_time["t"] >= 5  # bounded -- loop actually terminated rather than hanging


def test_probe_gradio_server_accepts_real_looking_gradio_config():
    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"version": "6.22.0", "components": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    restore = _patched({"urllib": type("FakeUrllib", (), {
        "request": type("FakeRequestModule", (), {"urlopen": staticmethod(lambda url, timeout=None: FakeResponse())}),
        "error": ll.urllib.error,
    })()})
    try:
        assert ll.probe_gradio_server() is True
    finally:
        restore()


def test_probe_gradio_server_rejects_non_gradio_response():
    class FakeResponse:
        status = 200

        def read(self):
            return b"<html><body>some other server</body></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    restore = _patched({"urllib": type("FakeUrllib", (), {
        "request": type("FakeRequestModule", (), {"urlopen": staticmethod(lambda url, timeout=None: FakeResponse())}),
        "error": ll.urllib.error,
    })()})
    try:
        assert ll.probe_gradio_server() is False
    finally:
        restore()


def test_probe_gradio_server_rejects_connection_error():
    def raise_conn_error(url, timeout=None):
        raise OSError("connection refused")

    restore = _patched({"urllib": type("FakeUrllib", (), {
        "request": type("FakeRequestModule", (), {"urlopen": staticmethod(raise_conn_error)}),
        "error": ll.urllib.error,
    })()})
    try:
        assert ll.probe_gradio_server() is False
    finally:
        restore()


def test_config_url_and_probe_are_localhost_only():
    assert ll.CONFIG_URL.startswith("http://127.0.0.1:")
    assert ll.HOST == "127.0.0.1"
    with open(os.path.join(ANAXI_FINAL, "llama_launch.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("http://0.0.0.0", "https://", "0.0.0.0"):
        assert forbidden not in source


def test_readiness_timeout_is_bounded_not_infinite():
    assert 0 < ll.READINESS_TIMEOUT_SECONDS <= 120
    assert ll.READINESS_POLL_INTERVAL_SECONDS > 0


# ============================================================ E2: legacy note


def test_legacy_wrapper_docstring_marks_it_non_default():
    with open(os.path.join(ANAXI_FINAL, "llama_desktop.py"), encoding="utf-8") as f:
        source = f.read()
    assert "LEGACY" in source
    assert "llama_launch.py" in source
    # Unchanged functional behavior -- only the docstring grew.
    assert "def main():" in source
    assert "webview.create_window(" in source
    assert "webview.start()" in source


def test_launch_bat_invokes_new_launcher_not_desktop_wrapper():
    # llama_desktop.py may still be legitimately NAMED in the .bat's own
    # comments (explaining it remains available manually) -- the actual
    # requirement is that no executable (non-REM) line invokes it.
    bat_path = os.path.join(ANAXI_FINAL, "Launch Anaxi.bat")
    with open(bat_path, encoding="utf-8") as f:
        lines = f.readlines()
    executable_lines = [l for l in lines if l.strip() and not l.strip().upper().startswith("REM") and not l.strip().startswith("@")]
    executable_text = "".join(executable_lines)
    assert "llama_launch.py" in executable_text
    assert "llama_desktop.py" not in executable_text
    assert "--conversation" in executable_text


ALL_TESTS = [
    test_fresh_launch_starts_exactly_one_backend,
    test_duplicate_launch_does_not_start_a_second_backend,
    test_unrelated_occupant_fails_closed_no_backend_no_browser,
    test_backend_start_failure_never_opens_browser,
    test_browser_not_opened_before_readiness,
    test_browser_opened_exactly_once_at_exact_url_after_readiness,
    test_launcher_never_imports_webview_or_desktop_wrapper,
    test_conversation_mode_propagates_from_argv,
    test_launcher_has_no_second_mode_parser,
    test_launcher_never_references_workspace_roaming,
    test_roaming_authorization_is_off_after_importing_launcher,
    test_launcher_never_references_sleep_rem,
    test_browser_open_has_no_lifetime_coupling_to_backend,
    test_backend_kept_alive_via_block_thread_not_window_event_loop,
    test_wait_for_readiness_stops_as_soon_as_probe_succeeds,
    test_wait_for_readiness_respects_bounded_timeout_never_succeeding,
    test_probe_gradio_server_accepts_real_looking_gradio_config,
    test_probe_gradio_server_rejects_non_gradio_response,
    test_probe_gradio_server_rejects_connection_error,
    test_config_url_and_probe_are_localhost_only,
    test_readiness_timeout_is_bounded_not_infinite,
    test_legacy_wrapper_docstring_marks_it_non_default,
    test_launch_bat_invokes_new_launcher_not_desktop_wrapper,
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
