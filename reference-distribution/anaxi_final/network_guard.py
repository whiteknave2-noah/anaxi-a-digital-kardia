"""Fail-closed guard: no test process may open a non-loopback network connection.

The offline evidence suite must be unable to message real people, call
external services, or reach the production Discord/search/model endpoints.
Any attempt to connect to a non-loopback address raises immediately and is
recorded in ``BLOCKED_ATTEMPTS`` so a test can prove the guard is live.
"""

from __future__ import annotations

import ipaddress
import socket

BLOCKED_ATTEMPTS: list[str] = []
_INSTALLED = False
_ORIGINAL: dict = {}


class ExternalNetworkBlocked(ConnectionError):
    """Raised for any non-loopback connection attempt while the guard is on."""


def _is_local(address) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    host = address[0] if address else ""
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host in ("", "localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname we did not resolve is not proven local


def _refuse(address):
    BLOCKED_ATTEMPTS.append(repr(address))
    raise ExternalNetworkBlocked(f"external network connection blocked by test guard: {address!r}")


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _ORIGINAL["connect"] = socket.socket.connect
    _ORIGINAL["connect_ex"] = socket.socket.connect_ex

    def connect(self, address):
        if not _is_local(address):
            _refuse(address)
        return _ORIGINAL["connect"](self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            _refuse(address)
        return _ORIGINAL["connect_ex"](self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    _INSTALLED = True


def is_installed() -> bool:
    return _INSTALLED


def guarded_env(env: dict) -> dict:
    """Environment for a test subprocess that must also be network-guarded."""
    import os
    site = os.path.join(os.path.dirname(os.path.abspath(__file__)), "network_guard_site")
    guarded = dict(env)
    existing = guarded.get("PYTHONPATH", "")
    guarded["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    guarded["ANAXI_NETWORK_GUARD"] = "1"
    return guarded
