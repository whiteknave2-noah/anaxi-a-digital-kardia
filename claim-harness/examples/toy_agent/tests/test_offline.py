"""The reference run is offline: the network guard is active."""

import socket

import pytest

from claim_harness import network_guard


def test_network_guard_is_active_for_this_run():
    assert network_guard.is_installed()
    with pytest.raises(network_guard.NetworkBlocked):
        socket.create_connection(("203.0.113.7", 80), timeout=1)
