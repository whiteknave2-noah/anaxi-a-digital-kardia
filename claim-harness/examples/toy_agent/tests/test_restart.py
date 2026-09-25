"""Persistence across reopen, and recovery of accepted-but-unprocessed input."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from claim_harness.checks import check_recovered_once, check_survives_reopen
from toy_agent import Channel, FakeTransport, SimulatedCrash, ToyAgent, item, open_demo


def test_notes_survive_reopen(agent):
    agent.submit(item("i1", "!note keep me"))
    reopened = check_survives_reopen(agent, lambda a: a.notes("alpha"))
    assert reopened.notes("alpha") == ["keep me"]


def test_grants_and_revocations_survive_reopen(agent):
    agent.revoke("guest", "note")
    agent.revoke_destination("ops-desk")
    check_survives_reopen(agent, lambda a: (a.allows("guest", "note"), a.allows("guest", "recall"),
                                            a.can_receive("ops-desk"), a.can_receive("flaky-relay")))


def test_accepted_input_is_recovered_once_after_crash(tmp_path):
    transport, channel = FakeTransport(), Channel()
    crashing = open_demo(tmp_path / "state", transport, channel, crash_at="after_accept")
    lost = item("i1", "!note written before the crash")
    with pytest.raises(SimulatedCrash):
        crashing.submit(lost)
    recovered = ToyAgent(tmp_path / "state", transport, channel)
    check_recovered_once(recovered, lost, fields=("scope", "sender", "text"))
    assert recovered.notes("alpha") == ["written before the crash"]
    # the client retrying the same input afterwards changes nothing
    recovered.submit(lost)
    check_recovered_once(recovered, lost, fields=("scope", "sender", "text"))


@pytest.mark.slow
def test_notes_survive_process_kill(tmp_path):
    here = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {str(here)!r})
        from toy_agent import item, open_demo
        agent = open_demo({str(tmp_path / 'state')!r})
        agent.submit(item("i1", "!note survives kill -9"))
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    proc = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": ""})
    assert proc.returncode == -signal.SIGKILL
    assert open_demo(tmp_path / "state").notes("alpha") == ["survives kill -9"]
