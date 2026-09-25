"""Run every custom-runner suite (``python test_x.py``) under pytest.

These modules define ``run_test_suite()``/``main()`` style checks that pytest
collects zero tests from, so a plain ``pytest`` run never executed them.  The
registry pins each suite's exact check count; a suite must exit 0 and print an
``N/N`` summary equal to that pin, so neither a failing nor a silently
shrinking suite can pass.  Discovery is also checked: any ``test_*.py`` that
pytest collects nothing from and that is neither registered nor explicitly
dispositioned as unrunnable fails the completeness test.
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
EVIDENCE = HERE / "completion_evidence"
SUITES = json.loads((EVIDENCE / "custom_runner_suites.json").read_text(encoding="utf-8"))["suites"]
DISPOSITIONS = json.loads(
    (EVIDENCE / "legacy_test_dispositions.json").read_text(encoding="utf-8")
)["modules"]

_SUMMARY = re.compile(r"(\d+)\s*/\s*(\d+)\s*(?:pass|checks|tests)?", re.IGNORECASE)


def _env():
    env = dict(os.environ)
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    return network_guard.guarded_env(env)


@pytest.mark.parametrize("script", sorted(SUITES))
def test_custom_runner_suite_passes_every_pinned_check(script):
    completed = subprocess.run(
        [sys.executable, script], cwd=HERE, env=_env(),
        capture_output=True, text=True, timeout=900,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-3000:]
    summaries = _SUMMARY.findall(output)
    assert summaries, "suite printed no N/N summary"
    passed, total = map(int, summaries[-1])
    expected = SUITES[script]["expected_total"]
    assert (passed, total) == (expected, expected), (
        f"{script}: {passed}/{total} reported, pinned expected_total={expected}"
    )


def test_every_test_module_is_collected_registered_or_dispositioned():
    """No test module may silently contribute zero executed checks."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=HERE, env=_env(), capture_output=True, text=True, timeout=600,
    )
    collected = {
        line.split("::", 1)[0] for line in completed.stdout.splitlines() if "::" in line
    }
    unaccounted = []
    for path in sorted(HERE.glob("test_*.py")):
        name = path.name
        if name in collected or name in SUITES:
            continue
        entry = DISPOSITIONS.get(name)
        if entry and entry["disposition"].startswith("UNRUNNABLE_"):
            continue
        unaccounted.append(name)
    assert not unaccounted, f"test modules executing zero checks without disposition: {unaccounted}"
