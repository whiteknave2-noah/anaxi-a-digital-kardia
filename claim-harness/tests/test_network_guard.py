import socket

import pytest

from claim_harness import network_guard


@pytest.fixture
def guard():
    was = network_guard.is_installed()
    network_guard.install()
    yield
    if not was:
        network_guard.uninstall()


def test_external_connect_blocked_and_recorded(guard):
    with pytest.raises(network_guard.NetworkBlocked):
        socket.create_connection(("198.51.100.20", 443), timeout=1)
    assert any("198.51.100.20" in a for a in network_guard.ATTEMPTS)


def test_external_name_resolution_blocked(guard):
    with pytest.raises(network_guard.NetworkBlocked):
        socket.getaddrinfo("example.com", 80)


def test_udp_sendto_blocked(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(network_guard.NetworkBlocked):
        s.sendto(b"x", ("192.0.2.1", 53))


def test_loopback_still_works(guard):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)  # the kernel completes the handshake; no accept needed
        with socket.create_connection(("localhost", server.getsockname()[1]), timeout=2):
            pass


def test_blocked_context_restores_previous_state():
    if network_guard.is_installed():
        pytest.skip("guard is installed for the whole session (-p claim_harness.network_guard)")
    original = socket.socket.connect
    with network_guard.blocked():
        assert network_guard.is_installed()
    assert not network_guard.is_installed() and socket.socket.connect is original
