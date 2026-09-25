"""Pytest isolation for suites that replace production modules in ``sys.modules``.

Several older integration harnesses intentionally install synthetic Ollama and
database modules before importing the real waking driver.  Those replacements
are valid inside one test, but leaving them process-global makes later tests
exercise a fake dependency by accident.  The full-suite result then depends on
collection/execution order.  Restore the pre-test module identities and the
few commonly rebound local module namespaces after every test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _unrunnable_modules():
    """Modules that cannot be collected as ordinary pytest tests: historical
    suites whose absent frozen oracle/live-DB dependency makes collection error,
    and custom-runner suites (``def test_x(check)``) that are executed instead by
    test_custom_runner_suites.py.  The list is the explicit, machine-verified
    disposition (test_legacy_dispositions.py), not an ad-hoc exclusion."""
    path = Path(__file__).with_name("completion_evidence") / "legacy_test_dispositions.json"
    try:
        modules = json.loads(path.read_text(encoding="utf-8"))["modules"]
    except (OSError, ValueError, KeyError):
        return []
    return sorted(
        name for name, entry in modules.items()
        if str(entry.get("disposition", "")).startswith(("UNRUNNABLE_", "GATED_BY_WRAPPER"))
    )


collect_ignore = _unrunnable_modules()

import network_guard  # noqa: E402

network_guard.install()  # no test may reach a non-loopback host, ever

import runtime_isolation  # noqa: E402

runtime_isolation.redirect_signal_observation_log()  # tests never write repo runtime logs
runtime_isolation.redirect_workspace_root()  # ...nor the live Workspace (journal, collections, Private Space)


_MISSING = object()
_TRACKED_MODULES = (
    "ollama",
    "orchestration",
    "anaxi_protocol",
    "anaxi_protocol_sqlite",
    "relational_history",
    "native_provenance_writer",
    "native_turn_staging",
    "conversation_direction",
    "conversation_direction_trace",
    "llama_anaxi",
    "workspace_supervisor",
    "workspace_capability",
    "workspace_audio",
    "workspace_roaming",
    "workspace_episode_provenance",
    "human_session_binding",
    "migrate_historical_data",
    "signal_observation_log",
    "context_budget",
    "sleep_receipts",
    "sentence_transformers",
)
_RESTORE_NAMESPACES = {
    "llama_anaxi",
    "workspace_supervisor",
    "workspace_roaming",
    "conversation_direction",
    "context_budget",
}


@pytest.fixture(scope="session", autouse=True)
def _tests_run_outside_the_repository_tree(tmp_path_factory):
    """Many modules bind cwd-relative default paths (roaming trace, signal log,
    legacy databases).  Running the whole session from a scratch directory keeps
    every such default from writing runtime state into the repository."""
    import os
    original = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("cwd"))
    try:
        yield
    finally:
        os.chdir(original)


@pytest.fixture(autouse=True)
def _restore_replaced_production_modules():
    before = {name: sys.modules.get(name, _MISSING) for name in _TRACKED_MODULES}
    namespaces = {
        name: dict(module.__dict__)
        for name, module in before.items()
        if name in _RESTORE_NAMESPACES and module is not _MISSING
    }
    yield
    for name, original in before.items():
        if original is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
            if name in namespaces:
                original.__dict__.clear()
                original.__dict__.update(namespaces[name])
