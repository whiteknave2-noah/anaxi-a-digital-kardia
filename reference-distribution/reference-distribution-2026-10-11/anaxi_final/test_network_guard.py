"""The offline evidence run must be physically unable to reach the outside world.

This is the machine evidence behind "no surprise engineering-time message to a
real person": every test process (in-process via conftest, subprocess suites via
sitecustomize) refuses non-loopback connections, so no Discord, search, model or
web endpoint can be contacted while evidence is produced.
"""

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import network_guard

HERE = Path(__file__).resolve().parent


def test_guard_is_installed_in_every_pytest_process():
    assert network_guard.is_installed()


@pytest.mark.parametrize("address", [
    ("93.184.216.34", 80), ("discord.com", 443), ("api.search.example", 443), ("8.8.8.8", 53),
    ("2606:4700:4700::1111", 443),
])
def test_non_loopback_connections_are_refused_and_recorded(address):
    before = len(network_guard.BLOCKED_ATTEMPTS)
    with socket.socket(socket.AF_INET6 if ":" in address[0] else socket.AF_INET) as sock:
        with pytest.raises(network_guard.ExternalNetworkBlocked):
            sock.connect(address)
        with pytest.raises(network_guard.ExternalNetworkBlocked):
            sock.connect_ex(address)
    assert len(network_guard.BLOCKED_ATTEMPTS) == before + 2


def test_loopback_still_works_for_local_servers():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
    thread.start()
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass
    thread.join(5)
    server.close()
    assert accepted


def test_guard_is_active_in_subprocess_suites():
    code = (
        "import socket, network_guard\n"
        "assert network_guard.is_installed()\n"
        "s = socket.socket()\n"
        "try:\n"
        "    s.connect(('93.184.216.34', 80))\n"
        "except network_guard.ExternalNetworkBlocked:\n"
        "    print('BLOCKED')\n"
    )
    import os
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=HERE, env=network_guard.guarded_env(dict(os.environ)),
        capture_output=True, text=True, timeout=60,
    )
    assert completed.stdout.strip() == "BLOCKED", completed.stdout + completed.stderr


def test_tests_never_write_the_repository_signal_observation_log(tmp_path):
    import os
    import signal_observation_log as sol
    default_path = sol.record_observation.__defaults__[0]
    assert os.path.isabs(default_path)
    assert not default_path.startswith(str(HERE)), "test runs must not write runtime logs into the repo"
    # A legitimate production log may already exist in this checkout, so the
    # invariant is NON-MUTATION (size and mtime unchanged; contents are never
    # read), not absence.
    repo_log = HERE / "signal_observations.jsonl"

    def state():
        try:
            info = repo_log.stat()
        except FileNotFoundError:
            return None
        return (info.st_size, info.st_mtime_ns)

    before = state()
    sol.record_observation("hello there")
    assert state() == before
