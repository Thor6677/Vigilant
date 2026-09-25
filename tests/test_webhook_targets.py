"""Update-report webhooks go only to public addresses.

The webhook is sent from inside the deployment, so a URL whose host is the
machine itself, the Docker network, the LAN or a cloud metadata endpoint would
have Vigilant make requests there — and the status or error it reports back
would say what answered. Every address the host resolves to must be globally
reachable, checked when the URL is saved and again before each send.

Non-public test addresses are built from integers rather than written out.
"""
import asyncio
import ipaddress

import pytest

from app.ops import update_reports as ur


def _v4(a, b, c, d):
    return str(ipaddress.IPv4Address((a << 24) | (b << 16) | (c << 8) | d))


NOT_PUBLIC = [
    "127.0.0.1",                 # loopback
    _v4(10, 1, 2, 3),            # private
    _v4(172, 17, 0, 2),          # private (Docker's default bridge)
    _v4(192, 168, 1, 10),        # private
    _v4(100, 64, 0, 1),          # shared address space
    "169.254.169.254",           # link-local / metadata
    "0.0.0.0",                   # unspecified
    "224.0.0.1",                 # multicast
    "240.0.0.1",                 # reserved
    "::1",                       # IPv6 loopback
    "fe80::1%lo0",               # IPv6 link-local with a zone
    "fd00::1",                   # IPv6 unique local
    "::ffff:127.0.0.1",          # IPv4-mapped loopback
    "::",                        # IPv6 unspecified
]
PUBLIC = ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize("addr", NOT_PUBLIC)
def test_non_public_addresses_are_refused(addr):
    assert ur.address_problem(addr)


@pytest.mark.parametrize("addr", PUBLIC)
def test_public_addresses_are_allowed(addr):
    assert ur.address_problem(addr) is None


def _resolver(monkeypatch, answer):
    async def fake(host, port):
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(ur, "_resolve", fake)


def test_every_resolved_address_must_be_public(monkeypatch):
    _resolver(monkeypatch, [PUBLIC[0], "127.0.0.1"])
    assert _run(ur.webhook_target_problem("https://hooks.example/x"))


def test_a_public_host_passes(monkeypatch):
    _resolver(monkeypatch, PUBLIC)
    assert _run(ur.webhook_target_problem("https://hooks.example/x")) is None


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    import socket
    _resolver(monkeypatch, socket.gaierror("nope"))
    assert "does not resolve" in _run(ur.webhook_target_problem("https://nowhere.example/x"))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/",
    "http://[::1]/hook",
    "http://0x7f000001/",
    "http://2130706433/",
])
def test_literal_and_numeric_hosts_are_refused(url):
    """Real resolution: these need no DNS, and each is loopback in disguise."""
    assert _run(ur.webhook_target_problem(url))


def test_a_send_to_a_non_public_host_never_leaves(monkeypatch):
    posted = []

    class Recorder:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            posted.append(url)

    monkeypatch.setattr(ur.httpx, "AsyncClient", Recorder)
    _resolver(monkeypatch, ["169.254.169.254"])

    class Report:
        kind, outcome, to_tag, from_tag, detail = "update", "success", "v1.2.3", "v1.2.2", ""

    result = _run(ur.post_webhook("http://hooks.example/x", ur.FORMAT_JSON, Report()))
    assert posted == []
    assert result["state"] == ur.DELIVERY_FAILED
    assert result["error"].startswith("not sent:")
