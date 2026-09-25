"""Offline guard: make a test run fail if code reaches for a non-loopback network.

Use it as a pytest plugin::

    python -m pytest -p claim_harness.network_guard ...

or directly with ``install()`` / ``uninstall()`` / ``with blocked(): ...``.

While installed, connecting or sending to a non-loopback address, or resolving
a non-local host name, raises ``NetworkBlocked`` and is recorded in
``ATTEMPTS``.  Loopback and Unix-domain sockets stay usable.  When used as a
plugin it also writes ``claim_harness.network_guard=active`` into the JUnit
suite properties, so the ledger records that the evidence ran guarded.

This guards against accidents in cooperating code; it is not a sandbox.
Subprocesses and native extensions that bypass Python's ``socket`` module are
not covered.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket

ATTEMPTS: list = []
_ORIGINAL: dict = {}
_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost"}


class NetworkBlocked(ConnectionError):
    """A non-loopback network operation was attempted while the guard was installed."""


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).lower().split("%", 1)[0]
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # an unresolved name is not proven local


def _is_local_address(address) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    return _is_local_host(address[0] if address else None)


def _refuse(what: str, target) -> None:
    ATTEMPTS.append(f"{what} {target!r}")
    raise NetworkBlocked(f"network access blocked by claim_harness.network_guard: {what} {target!r}")


def install() -> None:
    if _ORIGINAL:
        return
    _ORIGINAL.update(connect=socket.socket.connect, connect_ex=socket.socket.connect_ex,
                     sendto=socket.socket.sendto, getaddrinfo=socket.getaddrinfo)

    def connect(self, address):
        if not _is_local_address(address):
            _refuse("connect", address)
        return _ORIGINAL["connect"](self, address)

    def connect_ex(self, address):
        if not _is_local_address(address):
            _refuse("connect", address)
        return _ORIGINAL["connect_ex"](self, address)

    def sendto(self, data, *args):
        address = args[-1]
        if not _is_local_address(address):
            _refuse("sendto", address)
        return _ORIGINAL["sendto"](self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            _refuse("resolve", host)
        return _ORIGINAL["getaddrinfo"](host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo


def uninstall() -> None:
    if not _ORIGINAL:
        return
    socket.socket.connect = _ORIGINAL["connect"]
    socket.socket.connect_ex = _ORIGINAL["connect_ex"]
    socket.socket.sendto = _ORIGINAL["sendto"]
    socket.getaddrinfo = _ORIGINAL["getaddrinfo"]
    _ORIGINAL.clear()


def is_installed() -> bool:
    return bool(_ORIGINAL)


@contextlib.contextmanager
def blocked():
    already = is_installed()
    install()
    try:
        yield
    finally:
        if not already:
            uninstall()


# --- pytest plugin hooks (active only when loaded with ``-p claim_harness.network_guard``) ---

def pytest_configure(config):
    install()


def pytest_unconfigure(config):
    uninstall()


def pytest_report_header(config):
    return "claim_harness.network_guard: non-loopback network access is blocked"


try:  # the fixture is only defined when pytest is importable
    import pytest as _pytest

    @_pytest.fixture(scope="session", autouse=True)
    def _claim_harness_network_guard_property(record_testsuite_property):
        record_testsuite_property("claim_harness.network_guard", "active" if is_installed() else "inactive")
except ImportError:  # pragma: no cover - the guard itself does not need pytest
    pass
