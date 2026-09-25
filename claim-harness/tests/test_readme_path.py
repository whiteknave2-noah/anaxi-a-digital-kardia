"""The README's quick start, executed command by command in a clean copy of the package.

Environment setup (venv, pip install) is the caller's; every other command in
the README's ``bash`` blocks is run exactly as written, with ``python`` bound
to the interpreter running these tests.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SETUP = ("python3 -m venv", "source ", "python -m pip install", "python -m pytest tests")
IGNORE = shutil.ignore_patterns(".venv", "out", "build", "*.egg-info", "__pycache__", ".pytest_cache")


def readme_commands():
    text = (PACKAGE / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    return [line for block in blocks for line in block.splitlines() if line.strip()]


def expected_exit(command):
    if "deliberate_failure -p" in command:
        return 1  # pytest: the planted defect's test fails
    if "bindings_invalid.json" in command:
        return 2  # accounting refused
    if "deliberate_failure/bindings.json" in command:
        return 1  # ledger: DIGEST-1 FAILs
    return 0


def statuses(path):
    return {c["id"]: c["status"] for c in json.loads(Path(path).read_text())["claims"]}


@pytest.mark.slow
def test_readme_quick_start_runs_as_documented(tmp_path):
    copy = tmp_path / "claim-harness"
    shutil.copytree(PACKAGE, copy, ignore=IGNORE)
    commands = [c for c in readme_commands() if not c.startswith(SETUP)]
    assert commands[0].startswith("python -m pytest examples/toy_agent/tests")
    outputs = {}
    first_ledger = None
    for command in commands:
        runnable = re.sub(r"^python ", f"{sys.executable} ", command)
        proc = subprocess.run(runnable, shell=True, cwd=copy, capture_output=True, text=True, timeout=300)
        assert proc.returncode == expected_exit(command), (command, proc.stdout, proc.stderr)
        outputs.setdefault(command, []).append(proc.stdout + proc.stderr)
        if command.endswith("--output out/toy-ledger.json") and first_ledger is None:
            first_ledger = statuses(copy / "out" / "toy-ledger.json")

    toy_run = outputs[commands[1]][0]
    assert "summary: PASS=14  FAIL=0  NOT_ATTEMPTED=1  LIVE_ONLY=1  DISPOSITION=1  WITHDRAWN=1" in toy_run
    assert "network guard during evidence run: active" in toy_run
    assert first_ledger["PERSISTENCE-1"] == "NOT_ATTEMPTED"
    full = next(out for cmd, outs in outputs.items() if "toy-full-ledger" in cmd for out in outs)
    assert "PASS=15  FAIL=0  NOT_ATTEMPTED=0" in full
    shown = next(out for cmd, outs in outputs.items() if cmd.endswith("show out/failing-ledger.json") for out in outs)
    assert "DIGEST-1  FAIL" in shown and "not delivered" in shown
    invalid = next(out for cmd, outs in outputs.items() if "bindings_invalid" in cmd for out in outs)
    assert "ACCOUNTING INVALID" in invalid and not (copy / "out" / "invalid-ledger.json").exists()
    # after `rm -rf out` the rerun reproduces the same statuses
    assert statuses(copy / "out" / "toy-ledger.json") == first_ledger
    assert not (copy / "out" / "failing-ledger.json").exists()
