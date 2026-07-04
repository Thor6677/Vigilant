"""Tests for the Discord alert relay helper (Phase 3 Task 1).

`app.notify.discord.send_discord_alert` is the fire-and-forget POST used by
`app.routes.dashboard._emit_notification` — the single choke point every
alert-emitting call site (dashboard sync, corp inventory/contract
thresholds, killmail stream) funnels through. These tests exercise the
helper directly (not through `_emit_notification`) since it owns all the
interesting logic: settings-driven enable/disable, per-(type,key)
suppression, and swallow-never-raise on failure.

Sync-style (no pytest-asyncio): a single manually-managed event loop, per
tests/test_sync_field_sessions.py.
"""
import asyncio

import app.notify.discord as discord_notify
from app.notify.discord import send_discord_alert


class _FakeSettings:
    def __init__(self, webhook_url="https://discord.example/webhook", alert_types="structure_attack,structure_fuel"):
        self.discord_webhook_url = webhook_url
        self.discord_alert_types = alert_types


class _FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code


class _FakeAsyncClient:
    """Records every POST made through it; instantiated per `async with`."""

    calls = []
    raise_on_post = None
    response_status = 204

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).calls.append((url, json))
        if type(self).raise_on_post is not None:
            raise type(self).raise_on_post
        return _FakeResponse(type(self).response_status)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _reset_fake_client():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.raise_on_post = None
    _FakeAsyncClient.response_status = 204


def _patch(monkeypatch, settings):
    monkeypatch.setattr(discord_notify, "get_settings", lambda: settings)
    monkeypatch.setattr(discord_notify.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(discord_notify, "_last_sent", {})
    _reset_fake_client()


def test_sent_when_configured_and_type_enabled(monkeypatch):
    _patch(monkeypatch, _FakeSettings())

    _run(send_discord_alert("Structure Under Attack", "Astrahus in J123456", "structure_attack"))

    assert len(_FakeAsyncClient.calls) == 1
    url, payload = _FakeAsyncClient.calls[0]
    assert url == "https://discord.example/webhook"
    assert "Structure Under Attack" in payload["content"]
    assert "Astrahus in J123456" in payload["content"]


def test_duplicate_within_window_is_suppressed(monkeypatch):
    _patch(monkeypatch, _FakeSettings())

    _run(send_discord_alert("Fuel Alert", "Tower X low on fuel", "structure_fuel"))
    _run(send_discord_alert("Fuel Alert", "Tower X low on fuel", "structure_fuel"))

    assert len(_FakeAsyncClient.calls) == 1


def test_unset_webhook_is_a_noop(monkeypatch):
    _patch(monkeypatch, _FakeSettings(webhook_url=""))

    _run(send_discord_alert("Structure Under Attack", "Astrahus in J123456", "structure_attack"))

    assert _FakeAsyncClient.calls == []


def test_type_not_enabled_is_a_noop(monkeypatch):
    _patch(monkeypatch, _FakeSettings(alert_types="structure_attack"))

    _run(send_discord_alert("Inventory Low", "Fuel blocks at Office", "inventory_low"))

    assert _FakeAsyncClient.calls == []


def test_send_failure_is_swallowed_and_logged(monkeypatch, caplog):
    _patch(monkeypatch, _FakeSettings())
    _FakeAsyncClient.raise_on_post = RuntimeError("connection refused")

    with caplog.at_level("WARNING"):
        # Must not raise.
        _run(send_discord_alert("Structure Under Attack", "Astrahus in J123456", "structure_attack"))

    assert len(_FakeAsyncClient.calls) == 1
    assert any("discord alert relay" in rec.message for rec in caplog.records)


def test_different_keys_are_not_suppressed(monkeypatch):
    _patch(monkeypatch, _FakeSettings())

    _run(send_discord_alert("Structure Under Attack", "Astrahus", "structure_attack", key="astrahus-1"))
    _run(send_discord_alert("Structure Under Attack", "Fortizar", "structure_attack", key="fortizar-1"))

    assert len(_FakeAsyncClient.calls) == 2
