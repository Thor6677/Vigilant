"""The network guard (tests/_network_guard.py) blocks and records real calls."""
import asyncio
import socket

import pytest

from tests import _network_guard
from tests._network_guard import NetworkBlockedError


def test_guard_blocks_a_real_lookup_and_records_it():
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("example.invalid", 443)
    hits = _network_guard.drain()  # clear it so this test's own fixture passes
    assert len(hits) == 1 and "example.invalid" in hits[0][1]


def test_guard_allows_loopback_literals_and_unix_sockets():
    socket.getaddrinfo("localhost", 80)
    socket.getaddrinfo("127.0.0.1", 80)
    socket.getaddrinfo("0x7f000001", 80)
    socket.getaddrinfo("2130706433", 80)
    socket.getaddrinfo("93.184.216.34", 80)  # lookup of a literal makes no DNS query
    a, b = socket.socketpair()
    a.close()
    b.close()
    server = socket.create_server(("127.0.0.1", 0))
    try:
        client = socket.create_connection(server.getsockname())
        client.close()
    finally:
        server.close()
    assert _network_guard.drain() == []


def test_connect_allowed_decisions():
    allowed = _network_guard._connect_allowed
    assert allowed(("127.0.0.1", 1))
    assert allowed(("::1", 1, 0, 0))
    assert allowed("/tmp/x.sock")
    assert not allowed(("192.0.2.1", 1))


def test_guard_blocks_a_non_loopback_connect():
    s = socket.socket()
    s.settimeout(1)  # a broken guard must not hang CI
    try:
        with pytest.raises(NetworkBlockedError):
            s.connect(("192.0.2.1", 9))
    finally:
        s.close()
    assert len(_network_guard.drain()) == 1


def test_guard_records_a_call_that_a_background_task_swallows():
    async def _swallowing_task():
        try:
            # Runs on the loop's executor thread, like httpx's DNS lookups.
            await asyncio.get_running_loop().getaddrinfo("example.invalid", 443)
        except Exception:
            pass  # what the app's check_once/_kick_* wrappers do

    asyncio.run(_swallowing_task())
    # Nothing was raised to the test, yet the fixture would now fail it.
    hits = _network_guard.drain()
    assert len(hits) == 1


@pytest.mark.allow_network
def test_allow_network_marker_disables_the_guard():
    assert _network_guard._current["enabled"] is False
