"""Execute the real ``Launch Anaxi.command`` control flow (not a text check).

The script is copied into a temporary deployment layout whose project-local
interpreter is a recording stub, so the actual bash logic runs: authenticated
human binding gate, project-venv-only interpreter, offline embedding preflight,
bounded PDF renderer preparation, public-resource sync, exactly one
``llama_launch.py --conversation`` start, exit-status propagation (clean
shutdown), and a second launch (restart).  Nothing is started for real and no
model, network or production path is touched.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import network_guard

HERE = Path(__file__).resolve().parent
SCRIPT_NAME = "Launch Anaxi.command"
BOUND = "human-actor-c8feddc1b4bb"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS launcher")

STUB = """#!/bin/bash
# recording stub for the project-local interpreter
echo "$(basename "$1") ${@:2}" >> "$ANAXI_TEST_CALL_LOG"
case "$(basename "$1")" in
  prepare_embedding_model.py) exit "${ANAXI_TEST_EMBED_STATUS:-0}";;
  public_resource_ingest.py) exit "${ANAXI_TEST_INGEST_STATUS:-0}";;
  llama_launch.py) exit "${ANAXI_TEST_LAUNCH_STATUS:-0}";;
esac
exit 0
"""


def _deployment(tmp_path, *, with_venv=True):
    root = tmp_path / "deploy"
    root.mkdir()
    shutil.copy(HERE / SCRIPT_NAME, root / SCRIPT_NAME)
    (root / "pdf_page_renderer.swift").write_text("// stub source", encoding="utf-8")
    if with_venv:
        bindir = root / ".venv-macos" / "bin"
        bindir.mkdir(parents=True)
        python = bindir / "python"
        python.write_text(STUB, encoding="utf-8")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        renderer = bindir / "anaxi-pdf-page-renderer"  # newer than the swift source -> not recompiled
        renderer.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        renderer.chmod(renderer.stat().st_mode | stat.S_IXUSR)
        future = renderer.stat().st_mtime + 60
        os.utime(renderer, (future, future))
    return root


def _run(root, tmp_path, *, bound=BOUND, **status):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "ANAXI_TEST_CALL_LOG": str(tmp_path / "calls.log")}
    if bound is not None:
        env["ANAXI_BOUND_HUMAN_ACTOR_ID"] = bound
    env.update({f"ANAXI_TEST_{k.upper()}_STATUS": str(v) for k, v in status.items()})
    env = network_guard.guarded_env(env)
    completed = subprocess.run(
        ["/bin/bash", str(root / SCRIPT_NAME)], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    log = tmp_path / "calls.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return completed, calls


def test_ordinary_launch_runs_preflight_renderer_sync_then_exactly_one_conversation_start(tmp_path):
    root = _deployment(tmp_path)
    completed, calls = _run(root, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert calls == [
        "prepare_embedding_model.py --check-only",
        "public_resource_ingest.py sync",
        "llama_launch.py --conversation",
    ]
    assert "exited with status 0" in completed.stdout


def test_launcher_never_bootstraps_installs_or_uses_a_path_interpreter(tmp_path):
    root = _deployment(tmp_path)
    _completed, calls = _run(root, tmp_path)
    joined = " ".join(calls)
    for forbidden in ("pip", "bootstrap", "install", "--prepare", "download"):
        assert forbidden not in joined
    script = (HERE / SCRIPT_NAME).read_text(encoding="utf-8")
    assert '"$SCRIPT_DIR/.venv-macos/bin/python"' in script
    code_lines = [line.strip() for line in script.splitlines() if not line.strip().startswith("#")]
    for line in code_lines:
        assert not line.startswith(("python ", "python3 ", "pip ", "pip3 ")), line


def test_missing_authenticated_human_binding_starts_nothing(tmp_path):
    root = _deployment(tmp_path)
    for bound in (None, "someone-else"):
        completed, calls = _run(root, tmp_path, bound=bound)
        assert completed.returncode == 1
        assert calls == []
        assert "ANAXI was not started" in completed.stdout


def test_missing_project_venv_starts_nothing(tmp_path):
    root = _deployment(tmp_path, with_venv=False)
    completed, calls = _run(root, tmp_path)
    assert completed.returncode == 1 and calls == []
    assert "Run bootstrap_macos.sh first" in completed.stdout


def test_absent_or_invalid_embedding_artifact_stops_before_resource_state_or_backend(tmp_path):
    root = _deployment(tmp_path)
    completed, calls = _run(root, tmp_path, embed=7)
    assert completed.returncode == 7
    assert calls == ["prepare_embedding_model.py --check-only"]
    assert "embedding model is absent or invalid" in completed.stdout


def test_ingest_failure_never_starts_the_backend(tmp_path):
    root = _deployment(tmp_path)
    completed, calls = _run(root, tmp_path, ingest=3)
    assert completed.returncode == 3
    assert calls == ["prepare_embedding_model.py --check-only", "public_resource_ingest.py sync"]
    assert "did not complete safely" in completed.stdout


def test_backend_exit_status_propagates_and_a_second_launch_restarts_cleanly(tmp_path):
    root = _deployment(tmp_path)
    first, calls_first = _run(root, tmp_path, launch=0)
    (tmp_path / "calls.log").unlink()
    second, calls_second = _run(root, tmp_path, launch=5)
    assert first.returncode == 0 and calls_first[-1] == "llama_launch.py --conversation"
    assert second.returncode == 5, "a non-clean backend exit must be surfaced, not masked"
    assert calls_second == calls_first
    assert "exited with status 5" in second.stdout
