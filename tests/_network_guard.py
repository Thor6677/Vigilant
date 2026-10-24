"""Fails any test that reaches the real network.

ISS-117/ISS-132: TestClient lifespans used to start tasks that resolved and
connected to GitHub, CCP, zKillboard and eve-offline, which made the suite
nondeterministic and, intermittently, hung the event loop at teardown.

An audit hook sees every DNS lookup and connect, so it works whatever HTTP
client the app uses. Raising alone is not enough: the app's background
wrappers catch Exception and only log, so the call would be swallowed and the
test would still pass. Every blocked call is therefore also recorded, and the
autouse fixture in conftest.py fails the test that was running at the time.

Allowed: loopback, IP literals (including the numeric forms some SSRF tests use
on purpose) and Unix sockets (asyncio's own self-pipe). A test that really needs
the network opts out with @pytest.mark.allow_network.

Known limits: a reverse lookup of an IP literal and UDP sendto are not hooked;
connect((hostname, port)) resolves inside C before the connect event (the
connect is still blocked); module/session-scoped fixtures and subprocesses run
outside the guard; work leaked past its test is blamed on the next one.
"""
import ipaddress
import socket
import sys
import threading


class NetworkBlockedError(OSError):
    """Raised for a blocked call; httpx surfaces it as a ConnectError."""


_lock = threading.Lock()
_violations: list[tuple[str, str]] = []
_current = {"nodeid": "?", "enabled": False}
_installed = False


def _is_ip_literal(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)  # 0x7f000001, 2130706433
        return True
    except OSError:
        return False


def _lookup_allowed(host) -> bool:
    if host is None or host in ("", b"", "localhost", b"localhost"):
        return True
    return _is_ip_literal(host)


def _connect_allowed(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX path
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _hook(event, args):
    if not _current["enabled"]:
        return
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        if _lookup_allowed(args[0]):
            return
        target = str(args[0])
    elif event == "socket.connect":
        if _connect_allowed(args[1]):
            return
        target = str(args[1])
    else:
        return
    with _lock:
        _violations.append((_current["nodeid"], f"{event} {target}"))
    raise NetworkBlockedError(f"network blocked in tests: {event} {target}")


def install():
    """Install the audit hook once per process (hooks cannot be removed)."""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def start(nodeid: str, enabled: bool = True):
    _current["nodeid"] = nodeid
    _current["enabled"] = enabled


def stop():
    _current["enabled"] = False


def drain() -> list[tuple[str, str]]:
    """Return and clear everything recorded so far."""
    with _lock:
        out = list(_violations)
        _violations.clear()
    return out
