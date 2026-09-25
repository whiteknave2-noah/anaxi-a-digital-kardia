"""The reference harness is inside the review boundary (owner directive section 21) and must not be
bypassable by omission: it ran outside pytest and had silently gone red on production master (R2: a
real GUI-projection ordering defect; R8/R9: fixtures predating the author-mapping law).  This binds it:
a red harness is a red suite.  Run in its own process -- the harness stubs heavy modules globally."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def test_the_reference_harness_runs_green_in_its_own_process():
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    proc = subprocess.run([sys.executable, os.path.join(HERE, "reference_harness_v0.py")], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=600)
    tail = "\n".join(proc.stdout.splitlines()[-20:])
    assert proc.returncode == 0, tail + "\n" + proc.stderr[-2000:]
    assert "0 FAIL, 0 SKIP" in proc.stdout, tail
