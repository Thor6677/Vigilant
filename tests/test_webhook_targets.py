"""Update-report webhooks go only to public addresses.

The webhook is sent from inside the deployment, so a URL whose host is the
machine itself, the Docker network, the LAN or a cloud metadata endpoint would
have Vigilant make requests there — and the status or error it reports back
would say what answered. Every address the host resolves to must be globally
reachable, checked when the URL is saved and again before each send.

The send then connects to the addresses that check vetted, not to the name:
a name whose answer changed between the check and the connect would otherwise
be resolved a second time by the HTTP client and slip through. The socket
layer is faked one level below the client, at the backend every transport
dials through, so it sees exactly what would have been connected to.

Non-public test addresses are built from integers rather than written out.
"""
import asyncio
import ipaddress

import httpcore
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


# ── The send dials the vetted addresses, never the name ──────────────────────

HOOK = "https://hooks.example.org:8443/topic/secret"
V6 = "2606:2800:220:1:248:1893:25c8:1946"


class Report:
    kind, outcome, to_tag, from_tag, detail = "automatic", "succeeded", "v1.2.3", "v1.2.2", "fine"
    created_at = None


class FakeStream(httpcore.AsyncNetworkStream):
    """One accepted connection: takes the request bytes, answers 200."""

    def __init__(self, dial):
        self.dial = dial
        self.sent = b""
        self.reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

    async def read(self, max_bytes, timeout=None):
        out, self.reply = self.reply[:max_bytes], self.reply[max_bytes:]
        return out

    async def write(self, buffer, timeout=None):
        self.sent += buffer

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.dial["tls"] = {"server_hostname": server_hostname,
                            "check_hostname": ssl_context.check_hostname}
        return self


@pytest.fixture
def dials(monkeypatch):
    """Every TCP connect any httpcore pool would open, in order, faked.

    Patched on the stock backend class rather than on anything of ours, so a
    client that bypassed the pin and dialled the name would be caught too.
    Addresses listed in `refuse` fail as a refused connection.
    """
    class Dials(list):
        def __init__(self):
            super().__init__()
            self.refuse = set()

    log = Dials()
    refuse = log.refuse

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        dial = {"host": host, "port": port, "timeout": timeout, "tls": None}
        log.append(dial)
        if host in refuse:
            raise httpcore.ConnectError("[Errno 61] Connection refused")
        dial["stream"] = FakeStream(dial)
        return dial["stream"]

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    return log


def _resolver_sequence(monkeypatch, *answers):
    """Each resolution gets the next answer; the last one repeats."""
    calls = []

    async def fake(host, port):
        calls.append((host, port))
        return answers[min(len(calls), len(answers)) - 1]
    monkeypatch.setattr(ur, "_resolve", fake)
    return calls


def _request_lines(dial) -> list[str]:
    head = dial["stream"].sent.split(b"\r\n\r\n", 1)[0]
    return head.decode().split("\r\n")


def test_a_rebinding_name_cannot_redirect_the_send(monkeypatch, dials):
    """The check saw a public address; a lookup made at connect time would
    have seen a private one. Only the vetted address may be dialled, and the
    name is resolved once per send, so there is no second lookup to rebind."""
    calls = _resolver_sequence(monkeypatch, [PUBLIC[0]], [_v4(10, 0, 0, 5)])

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert result["state"] == ur.SENT
    assert len(calls) == 1
    assert [d["host"] for d in dials] == [PUBLIC[0]]
    assert dials[0]["port"] == 8443


def test_a_send_that_resolves_to_loopback_the_second_time_is_refused(monkeypatch, dials):
    """The rebinding, seen across two sends: the second one's own resolution
    is what it is judged on, and it never reaches the socket."""
    _resolver_sequence(monkeypatch, [PUBLIC[0]], ["127.0.0.1"])

    first = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))
    second = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert first["state"] == ur.SENT
    assert second["state"] == ur.DELIVERY_FAILED
    assert second["error"] == "not sent: its host is a private, local or reserved address"
    assert [d["host"] for d in dials] == [PUBLIC[0]]


def test_tls_and_the_host_header_still_carry_the_original_name(monkeypatch, dials):
    """Pinning the socket to an address must not turn the request into one
    for that address: the certificate is checked against the name, the name
    goes out as the SNI, and the Host header is what the URL said."""
    _resolver_sequence(monkeypatch, [PUBLIC[0]])

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_NTFY, Report()))

    assert result["state"] == ur.SENT
    [dial] = dials
    assert dial["host"] == PUBLIC[0]
    assert dial["tls"] == {"server_hostname": "hooks.example.org", "check_hostname": True}
    lines = _request_lines(dial)
    assert lines[0] == "POST /topic/secret HTTP/1.1"
    assert "Host: hooks.example.org:8443" in lines
    assert any(line.startswith("User-Agent: Vigilant") for line in lines)
    assert any(line.startswith("Title: Vigilant: automatic update to v1.2.3") for line in lines)


def test_a_vetted_ipv6_address_is_dialled_as_itself(monkeypatch, dials):
    _resolver_sequence(monkeypatch, [V6])

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert result["state"] == ur.SENT
    [dial] = dials
    assert dial["host"] == V6
    assert dial["tls"]["server_hostname"] == "hooks.example.org"
    assert "Host: hooks.example.org:8443" in _request_lines(dial)


def test_a_non_public_answer_is_refused_before_anything_is_dialled(monkeypatch, dials):
    _resolver_sequence(monkeypatch, [PUBLIC[0], _v4(172, 17, 0, 2)])

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert dials == []
    assert result["state"] == ur.DELIVERY_FAILED
    assert result["error"] == "not sent: its host is a private, local or reserved address"


def test_the_next_vetted_address_is_tried_when_one_refuses(monkeypatch, dials):
    """A dual-stack host whose IPv6 is unreachable from the deployment still
    gets its IPv4 tried, and the connect budget is shared between them
    rather than spent whole on the first."""
    _resolver_sequence(monkeypatch, [V6, PUBLIC[0]])
    dials.refuse.add(V6)

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert result["state"] == ur.SENT
    assert [d["host"] for d in dials] == [V6, PUBLIC[0]]
    assert all(d["timeout"] == ur.WEBHOOK_TIMEOUT_SECONDS / 2 for d in dials)


def test_a_refusal_from_every_address_is_reported_as_a_failure(monkeypatch, dials):
    _resolver_sequence(monkeypatch, [PUBLIC[0]])
    dials.refuse.add(PUBLIC[0])

    result = _run(ur.post_webhook(HOOK, ur.FORMAT_JSON, Report()))

    assert result["state"] == ur.DELIVERY_FAILED
    assert result["error"].startswith("ConnectError:")
    assert "secret" not in result["error"]
