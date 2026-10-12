"""Keep test processes from writing runtime state into the repository.

``signal_observation_log.record_observation`` binds its default log path
(``signal_observations.jsonl``, relative to the cwd) at import time, so any test
that drives ``run_waking_turn`` appended to a real-looking runtime log in the
repository tree.  Redirect that default to a per-process temporary file.
"""

from __future__ import annotations

import atexit
import os
import tempfile

_TEMP_DIR = None
_WORKSPACE_TEMP_DIR = None


def redirect_signal_observation_log() -> str | None:
    global _TEMP_DIR
    try:
        import signal_observation_log
    except Exception:
        return None
    if _TEMP_DIR is None:
        _TEMP_DIR = tempfile.TemporaryDirectory(prefix="anaxi-test-signal-")
        atexit.register(_TEMP_DIR.cleanup)
    path = os.path.join(_TEMP_DIR.name, "signal_observations.jsonl")
    function = signal_observation_log.record_observation
    defaults = list(function.__defaults__ or ())
    if defaults and defaults[0] != path:
        defaults[0] = path
        function.__defaults__ = tuple(defaults)
    return path


def redirect_workspace_root() -> str:
    """Point every ``production_defaults()`` Workspace root at a per-process
    temporary directory (see runtime_roots). The real Workspace holds the
    owner's journal, collections and Private Space; a test or probe must never
    reach it unless it names that path explicitly."""
    global _WORKSPACE_TEMP_DIR
    import runtime_roots

    if _WORKSPACE_TEMP_DIR is None:
        _WORKSPACE_TEMP_DIR = tempfile.TemporaryDirectory(prefix="anaxi-test-workspace-")
        atexit.register(_WORKSPACE_TEMP_DIR.cleanup)
    os.environ[runtime_roots.TEST_WORKSPACE_ROOT_ENV] = _WORKSPACE_TEMP_DIR.name
    return _WORKSPACE_TEMP_DIR.name
