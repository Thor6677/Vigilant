"""httpx's own request log line must never carry a webhook credential.

httpx logs `HTTP Request: POST <url> "<version> <status> <reason>"` at INFO
for every completed request, and the app logs httpx at INFO. Each of the
three webhook senders wraps its POST in `redact_request_urls()`, and the
filter in app/notify/log_redaction.py cuts the URL down to its origin while
that context is active. These tests drive each sender through a real
httpx.AsyncClient (on a MockTransport, so httpx formats the line exactly as
it does in production) and read what reached the log.

The other half of the contract: an ordinary ESI-shaped request, made outside
the context, is logged unchanged, including its full path.
"""
import asyncio
import contextvars
import functools
import logging

import httpcore
import httpx
import pytest

from app.notify import discord as discord_notify
from app.notify import log_redaction
from app.notify import user_discord
from app.ops import update_reports as ur

TOKEN = "abc_DEF-ghi_jklMNOP0123456789"
DISCORD_HOOK = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"
NTFY_TOPIC = "some-topic-nobody-should-see"
NTFY_HOOK = f"https://ntfy.example.test/{NTFY_TOPIC}"
ESI_URL = "https://esi.evetech.net/latest/characters/123/skills/?datasource=tranquility"
PUBLIC_ADDR = "93.184.215.14"

HTTP_LOGGERS = ("httpx", "httpcore")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _messages(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records
            if rec.name.split(".")[0] in HTTP_LOGGERS]


def _capture(caplog):
    for name in HTTP_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)


def _mock_transport(seen: list, status: int = 204) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)
    return httpx.MockTransport(handler)


class Report:
    kind, outcome, to_tag, from_tag, detail = "automatic", "succeeded", "v1.2.3", "v1.2.2", "fine"
    created_at = None


class _NoRow:
    """A db whose settings lookup finds no row, so record_outcome returns early."""

    async def execute(self, _stmt):
        return self

    def scalar_one_or_none(self):
        return None


# ── The three senders ────────────────────────────────────────────────────────

def _assert_redacted(messages: list[str], secret: str, host: str, origin_line: str):
    assert messages, "httpx logged nothing: the send never went through a real client"
    assert not any(secret in m for m in messages), messages
    assert any(origin_line in m for m in messages), messages
    assert all(host in m for m in messages if "HTTP Request" in m)


def test_user_discord_deliver_never_logs_the_token(monkeypatch, caplog):
    seen = []

    async def vetted(url):
        return None, [PUBLIC_ADDR]

    monkeypatch.setattr(user_discord, "vetted_addresses", vetted)
    monkeypatch.setattr(user_discord, "_PinnedTransport", lambda addrs: _mock_transport(seen))
    _capture(caplog)

    outcome = _run(user_discord.deliver(DISCORD_HOOK, "Title", "Body", "structure_attack"))

    assert outcome.ok
    assert str(seen[0].url) == DISCORD_HOOK   # httpx itself still got the real URL
    _assert_redacted(_messages(caplog), TOKEN, "discord.com",
                     'HTTP Request: POST https://discord.com/… "HTTP/1.1 204 No Content"')


def test_user_discord_test_message_never_logs_the_token(monkeypatch, caplog):
    seen = []

    async def vetted(url):
        return None, [PUBLIC_ADDR]

    monkeypatch.setattr(user_discord, "vetted_addresses", vetted)
    monkeypatch.setattr(user_discord, "_PinnedTransport", lambda addrs: _mock_transport(seen))
    _capture(caplog)

    outcome = _run(user_discord.send_test_message(_NoRow(), 7, DISCORD_HOOK))

    assert outcome.ok
    messages = _messages(caplog)
    _assert_redacted(messages, TOKEN, "discord.com", "POST https://discord.com/…")
    assert not any("/api/webhooks/123456789012345678" in m for m in messages)


def test_instance_relay_never_logs_the_webhook_url(monkeypatch, caplog):
    seen = []

    class Settings:
        discord_webhook_url = DISCORD_HOOK
        discord_alert_types = "structure_attack,update_available"

    monkeypatch.setattr(discord_notify, "get_settings", lambda: Settings())
    monkeypatch.setattr(discord_notify, "_last_sent", {})
    monkeypatch.setattr(discord_notify.httpx, "AsyncClient",
                        functools.partial(httpx.AsyncClient, transport=_mock_transport(seen)))
    _capture(caplog)

    result = _run(discord_notify.send_discord_alert("Under attack", "Astrahus", "structure_attack"))
    announce = _run(discord_notify.send_discord_alert("Update available", "v9.9.9", "update_available"))

    assert result == discord_notify.SENT and announce == discord_notify.SENT
    assert len(seen) == 2
    _assert_redacted(_messages(caplog), TOKEN, "discord.com",
                     'HTTP Request: POST https://discord.com/… "HTTP/1.1 204 No Content"')


@pytest.mark.parametrize("fmt", [ur.FORMAT_NTFY, ur.FORMAT_JSON])
def test_update_report_webhook_never_logs_the_topic(monkeypatch, caplog, fmt):
    seen = []

    async def vetted(url):
        return None, [PUBLIC_ADDR]

    monkeypatch.setattr(ur, "vetted_addresses", vetted)
    monkeypatch.setattr(ur, "_PinnedTransport", lambda addrs: _mock_transport(seen, 200))
    _capture(caplog)

    result = _run(ur.post_webhook(NTFY_HOOK, fmt, Report()))

    assert result["state"] == ur.SENT
    assert str(seen[0].url) == NTFY_HOOK
    _assert_redacted(_messages(caplog), NTFY_TOPIC, "ntfy.example.test",
                     'HTTP Request: POST https://ntfy.example.test/… "HTTP/1.1 200 OK"')


def test_httpcore_debug_lines_carry_no_secret_through_the_pinned_transport(monkeypatch, caplog):
    """The real httpcore pool at DEBUG, on a faked socket: the connect and
    request traces name the host, never the path."""
    class FakeStream(httpcore.AsyncNetworkStream):
        reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

        async def read(self, max_bytes, timeout=None):
            out, self.reply = self.reply[:max_bytes], self.reply[max_bytes:]
            return out

        async def write(self, buffer, timeout=None):
            pass

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return self

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return FakeStream()

    async def resolve(host, port):
        return [PUBLIC_ADDR]

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    monkeypatch.setattr(ur, "_resolve", resolve)
    _capture(caplog)

    result = _run(ur.post_webhook(NTFY_HOOK, ur.FORMAT_NTFY, Report()))

    assert result["state"] == ur.SENT
    messages = _messages(caplog)
    assert any(rec.name.startswith("httpcore") for rec in caplog.records), "httpcore logged nothing"
    assert not any(NTFY_TOPIC in m for m in messages), messages
    assert any("ntfy.example.test" in m for m in messages)


# ── Ordinary requests are untouched ──────────────────────────────────────────

def test_an_esi_request_is_logged_in_full(caplog):
    _capture(caplog)

    async def go():
        async with httpx.AsyncClient(transport=_mock_transport([], 200)) as client:
            await client.get(ESI_URL)

    _run(go())

    assert any(f'HTTP Request: GET {ESI_URL} "HTTP/1.1 200 OK"' == m for m in _messages(caplog))


def test_the_context_is_task_local(caplog):
    """A record logged from a context captured before the redaction started,
    the way a concurrently running ESI task would be, keeps its URL."""
    _capture(caplog)
    outside = contextvars.copy_context()
    log = logging.getLogger("httpx")

    with log_redaction.redact_request_urls():
        assert log_redaction.redacting()
        outside.run(log.info, "HTTP Request: %s %s", "GET", httpx.URL(ESI_URL))
        log.info("HTTP Request: %s %s", "GET", httpx.URL(ESI_URL))
    assert not log_redaction.redacting()

    messages = _messages(caplog)
    assert messages == [f"HTTP Request: GET {ESI_URL}",
                        "HTTP Request: GET https://esi.evetech.net/…"]


# ── Belt and braces ──────────────────────────────────────────────────────────

def test_a_discord_webhook_path_is_redacted_even_without_the_context(caplog):
    _capture(caplog)
    assert not log_redaction.redacting()
    log = logging.getLogger("httpx")

    log.info('HTTP Request: %s %s "%s %d %s"', "POST", httpx.URL(DISCORD_HOOK),
             "HTTP/1.1", 204, "No Content")
    log.info("plain string " + DISCORD_HOOK + "?wait=true and again " + DISCORD_HOOK)
    logging.getLogger("httpcore.http11").debug("target=%r", "/api/webhooks/42/" + TOKEN)

    messages = _messages(caplog)
    assert not any(TOKEN in m for m in messages), messages
    assert messages[0] == 'HTTP Request: POST https://discord.com/api/webhooks/… "HTTP/1.1 204 No Content"'
    assert messages[1] == ("plain string https://discord.com/api/webhooks/… and again "
                           "https://discord.com/api/webhooks/…")
    assert messages[2] == "target='/api/webhooks/…'"


def test_the_filter_never_raises_on_odd_records():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    flt = log_redaction.RedactRequestUrlFilter()

    def record(msg, args):
        rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, None, None)
        rec.args = args   # past LogRecord's own normalisation, so the odd shape stays
        return rec

    odd = [
        record(None, None),
        record(42, None),
        record({"not": "a string"}, None),
        record("%(url)s", {"url": DISCORD_HOOK, "n": 1}),
        record("%s %s", (Unprintable(), None)),
        record("%s", Unprintable()),
        record(Unprintable(), (b"bytes", 1.5, True)),
    ]
    with log_redaction.redact_request_urls():
        for rec in odd:
            assert flt.filter(rec) is True
    for rec in odd:
        assert flt.filter(rec) is True

    assert odd[3].args == {"url": "https://discord.com/…", "n": 1}
    assert odd[3].getMessage() == "https://discord.com/…"
    assert odd[6].args == (b"bytes", 1.5, True)   # plain values keep their types


def test_install_is_idempotent_and_covers_every_httpcore_logger():
    log_redaction.install()
    log_redaction.install()
    for name in log_redaction.LOGGER_NAMES:
        mine = [f for f in logging.getLogger(name).filters
                if isinstance(f, log_redaction.RedactRequestUrlFilter)]
        assert len(mine) == 1, name
    assert {"httpx", "httpcore.connection", "httpcore.http11"} <= set(log_redaction.LOGGER_NAMES)


# ── The test message's footer ────────────────────────────────────────────────

def test_test_message_footer_reads_as_a_label():
    payload = user_discord.build_payload("Test message", "hello", user_discord.TEST_TYPE)
    assert payload["embeds"][0]["footer"]["text"] == "Vigilant · Test message"
    # An opted-in type keeps its own label; an unknown one falls back to its name.
    assert user_discord.build_payload("t", "b", "structure_attack")["embeds"][0]["footer"]["text"] \
        == "Vigilant · " + user_discord.ALERT_LABELS["structure_attack"]
    assert user_discord.build_payload("t", "b", "mystery")["embeds"][0]["footer"]["text"] \
        == "Vigilant · mystery"
