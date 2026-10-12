"""The interpreter running the suite must be the exact pinned production
environment, the offline embedding artifact must preflight with no network, and
every native stack ANAXI's capabilities rely on must import and function.

Fail-closed by design: run under a different interpreter and this fails.
"""

import importlib
import importlib.metadata as metadata
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import network_guard

HERE = Path(__file__).resolve().parent


def _pinned():
    pins = {}
    for raw in (HERE / "requirements-macos.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        assert match, f"unpinned or unparseable requirement: {line!r}"
        pins[match.group(1)] = match.group(2)
    return pins


def test_manifest_is_fully_pinned_and_nonempty():
    pins = _pinned()
    assert len(pins) >= 50


def test_every_pinned_distribution_is_installed_at_exactly_its_pinned_version():
    mismatches = []
    for name, version in _pinned().items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            mismatches.append((name, version, None))
            continue
        if installed != version:
            mismatches.append((name, version, installed))
    assert not mismatches, f"environment differs from the pinned production manifest: {mismatches}"


@pytest.mark.parametrize("module", [
    "pypdf", "PIL", "numpy", "sentence_transformers", "gradio", "faiss", "psutil", "ollama",
])
def test_capability_stack_modules_import(module):
    assert importlib.import_module(module) is not None


def test_audio_stack_can_actually_encode_and_decode_every_owner_format(tmp_path):
    """libsndfile must round-trip WAV, FLAC and MP3 (the production audio decoder)."""
    import numpy as np
    import soundfile as sf
    available = sf.available_formats()
    for fmt in ("WAV", "FLAC", "MP3"):
        assert fmt in available, f"audio stack lacks {fmt}"
    samples = (0.3 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype("float32")
    for extension, fmt in ((".wav", "WAV"), (".flac", "FLAC"), (".mp3", "MP3")):
        path = tmp_path / f"tone{extension}"
        sf.write(path, samples, 8000, format=fmt)
        decoded, rate = sf.read(path)
        assert rate == 8000 and len(decoded) >= 7000, extension
    import workspace_audio  # the production module imports the same stack
    assert workspace_audio is not None


def test_offline_embedding_artifact_preflight_passes_with_no_network():
    env = network_guard.guarded_env(dict(os.environ))
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    completed = subprocess.run(
        [sys.executable, "prepare_embedding_model.py", "--check-only"],
        cwd=HERE, env=env, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "verified in the local cache" in completed.stdout
    assert "384 dimensions" in completed.stdout


def test_offline_embedding_preflight_fails_closed_when_the_cache_is_absent(tmp_path):
    env = network_guard.guarded_env(dict(os.environ))
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_HOME": str(tmp_path / "empty-hf"), "SENTENCE_TRANSFORMERS_HOME": str(tmp_path / "empty-st"),
                "XDG_CACHE_HOME": str(tmp_path / "empty-cache"), "HOME": str(tmp_path)})
    completed = subprocess.run(
        [sys.executable, "prepare_embedding_model.py", "--check-only"],
        cwd=HERE, env=env, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode != 0
    assert "verified in the local cache" not in completed.stdout
