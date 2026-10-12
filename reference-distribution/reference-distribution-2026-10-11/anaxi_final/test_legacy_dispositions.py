"""Re-verify the explicit disposition of every test module excluded from the
broad pytest collection (completion_evidence/legacy_test_dispositions.json).

Custom-runner suites are gated through a wrapper that requires their own
``N/N pass`` summary.  Unrunnable historical suites are re-run in a subprocess
and must still fail with exactly the recorded signature; if a dependency is
restored or the failure mode changes, this test fails so the disposition is
reconsidered rather than silently going stale.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import network_guard

HERE = Path(__file__).resolve().parent
DISPOSITIONS = json.loads(
    (HERE / "completion_evidence" / "legacy_test_dispositions.json").read_text(encoding="utf-8")
)["modules"]
CHECKPOINT = json.loads(
    (HERE / "completion_evidence" / "offline_regression_checkpoint.json").read_text(encoding="utf-8")
)
ACCOUNTED = (
    CHECKPOINT.get("unrunnable_test_modules_declared", [])
    + CHECKPOINT.get("wrapper_gated_modules", [])
    + CHECKPOINT.get("repaired_modules", [])
)


def _env():
    env = dict(os.environ)
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    return network_guard.guarded_env(env)


def test_every_disposed_module_exists_and_is_collectable_or_explained():
    for name in DISPOSITIONS:
        assert (HERE / name).is_file(), name
    allowed = {
        "REPAIRED_COLLECTION_SAFE", "GATED_BY_WRAPPER",
        "UNRUNNABLE_FROZEN_PROTOTYPE_ABSENT", "UNRUNNABLE_FROZEN_PACKAGES_ABSENT",
        "UNRUNNABLE_LIVE_DATABASE_COPY_REQUIRED",
    }
    for name, entry in DISPOSITIONS.items():
        assert entry["disposition"] in allowed, name


def test_disposition_covers_every_module_previously_excluded_from_broad_collection():
    assert sorted(ACCOUNTED) == sorted(DISPOSITIONS)


@pytest.mark.parametrize(
    "name", [n for n, e in DISPOSITIONS.items() if e["disposition"] == "GATED_BY_WRAPPER"]
)
def test_wrapper_gated_suites_are_pinned_in_the_custom_runner_registry(name):
    registry = json.loads(
        (HERE / "completion_evidence" / "custom_runner_suites.json").read_text(encoding="utf-8")
    )["suites"]
    total = registry[name]["expected_total"]
    assert DISPOSITIONS[name]["expected_summary"] == f"{total}/{total} pass"


@pytest.mark.parametrize(
    "name", [n for n, e in DISPOSITIONS.items() if e["disposition"].startswith("UNRUNNABLE_")]
)
def test_unrunnable_historical_suites_still_fail_for_the_recorded_reason(name):
    entry = DISPOSITIONS[name]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", name, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=HERE, env=_env(), capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode != 0, (
        f"{name} now runs green; retire its UNRUNNABLE disposition and gate it"
    )
    output = completed.stdout + completed.stderr
    assert entry["expected_signature"] in output, output[-2000:]
    assert entry["scope_note"]
