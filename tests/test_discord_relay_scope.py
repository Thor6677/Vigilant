"""ISS-063: the instance-wide Discord relay carries only admin and manager
accounts' alerts unless DISCORD_RELAY_SCOPE=all.

`_emit_notification` schedules `relay_user_alert` (instance channel, role
checked) and `send_user_discord_alert` (the user's own webhook, unchanged).
No network: the HTTP client is faked at `discord_notify.httpx.AsyncClient`.
"""
import asyncio
import logging

import pytest
from sqlalchemy import text

import app.notify.discord as discord_notify
from app.db.models import AsyncSessionLocal, User, engine
from app.routes import dashboard

ADMIN, MANAGER, PLAIN, PLAIN2 = 9101, 9102, 9103, 9104


class _Settings:
    def __init__(self, scope="admins"):
        self.discord_webhook_url = "https://discord.example/webhook"
        self.discord_alert_types = "structure_attack,structure_fuel,auto_update"
        self.discord_relay_scope = scope


class _Resp:
    status_code = 204


class _Client:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).calls.append(json["content"])
        return _Resp()


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


async def _seed_users():
    async with AsyncSessionLocal() as db:
        for uid, role in ((ADMIN, "admin"), (MANAGER, "manager"),
                          (PLAIN, "user"), (PLAIN2, "user")):
            await db.execute(text("DELETE FROM users WHERE id=:i"), {"i": uid})
            db.add(User(id=uid, role=role))
        await db.commit()


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _Client.calls = []
    monkeypatch.setattr(discord_notify, "_last_sent", {})
    monkeypatch.setattr(discord_notify, "_warned_scopes", set())
    monkeypatch.setattr(discord_notify.httpx, "AsyncClient", _Client)
    _run(_seed_users())
    yield
    for uid in (ADMIN, MANAGER, PLAIN, PLAIN2):
        dashboard._notification_events.pop(uid, None)

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM users WHERE id IN (9101,9102,9103,9104)"))
            await db.commit()
    _run(cleanup())


def _scope(monkeypatch, scope):
    monkeypatch.setattr(discord_notify, "get_settings", lambda: _Settings(scope))


def _emit(user_id, title="Structure Under Attack", alert_type="structure_attack"):
    """Drive _emit_notification the way a sync would, with a fake own-webhook
    relay, and return the own-relay calls."""
    own = []

    async def own_spy(uid, *a, **k):
        own.append(uid)

    async def go():
        dashboard._emit_notification(user_id, {"type": alert_type, "title": title, "body": "b"})
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.gather(*list(dashboard._discord_relay_tasks))
    return own, own_spy, go


def _fire(monkeypatch, user_id, **kw):
    own, own_spy, go = _emit(user_id, **kw)
    monkeypatch.setattr(dashboard, "send_user_discord_alert", own_spy)
    _run(go())
    return own


def test_non_admin_reaches_own_relay_but_not_instance_channel(monkeypatch):
    _scope(monkeypatch, "admins")
    own = _fire(monkeypatch, PLAIN)
    assert own == [PLAIN]
    assert _Client.calls == []


@pytest.mark.parametrize("uid", [ADMIN, MANAGER])
def test_admin_and_manager_reach_both(monkeypatch, uid):
    _scope(monkeypatch, "admins")
    own = _fire(monkeypatch, uid)
    assert own == [uid]
    assert len(_Client.calls) == 1


def test_all_scope_restores_the_old_behaviour(monkeypatch):
    _scope(monkeypatch, "all")
    own = _fire(monkeypatch, PLAIN)
    assert own == [PLAIN]
    assert len(_Client.calls) == 1


def test_unknown_scope_falls_back_to_admins_and_logs_once(monkeypatch, caplog):
    _scope(monkeypatch, "everyone")
    with caplog.at_level(logging.WARNING, logger=discord_notify.logger.name):
        _fire(monkeypatch, PLAIN)
        _fire(monkeypatch, ADMIN, title="Other")
        _fire(monkeypatch, PLAIN, title="Third")
    assert len(_Client.calls) == 1                      # only the admin's
    assert sum("DISCORD_RELAY_SCOPE" in r.message for r in caplog.records) == 1


def test_unknown_user_id_is_not_relayed(monkeypatch):
    _scope(monkeypatch, "admins")
    _fire(monkeypatch, 987654)
    assert _Client.calls == []


def test_dedup_is_per_user(monkeypatch):
    _scope(monkeypatch, "all")
    _fire(monkeypatch, PLAIN)
    _fire(monkeypatch, PLAIN2)                          # same type and title
    assert len(_Client.calls) == 2
    _fire(monkeypatch, PLAIN)                           # exact repeat: collapsed
    assert len(_Client.calls) == 2


def test_update_reports_path_is_unaffected(monkeypatch):
    """update_reports calls send_discord_alert directly: no role lookup, and a
    'user' scope never applies to it."""
    _scope(monkeypatch, "admins")
    result = _run(discord_notify.send_discord_alert("Update ok", "b", "auto_update"))
    assert result == discord_notify.SENT
    assert len(_Client.calls) == 1


def test_role_lookup_failure_fails_closed(monkeypatch):
    _scope(monkeypatch, "admins")

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.db.models.AsyncSessionLocal", boom)
    result = _run(discord_notify.relay_user_alert(ADMIN, "t", "b", "structure_attack"))
    assert result.startswith("failed:")
    assert _Client.calls == []
