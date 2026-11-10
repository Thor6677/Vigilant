"""ISS-175: no message the app posts to Discord can ping anyone.

Alert text can carry what a user typed into a form: a corp inventory
threshold's item and location names, a contract threshold's keyword. The
instance relay posted that as `content` with no `allowed_mentions`, so an
@everyone in a threshold name pinged the operator's channel. Every Discord
body is now built by `app.notify.discord.webhook_payload`, which always sends
`allowed_mentions: {"parse": []}`. These tests send through each path with an
@everyone-bearing name and read the body that went out, and the last one holds
the rest of app/ to the helper.

No network: the HTTP client is faked at `httpx.AsyncClient`, as in
tests/test_discord_alert_relay.py. No database: the two paths that read one
are handed stand-ins.
"""
import ast
import asyncio
import pathlib
from types import SimpleNamespace

import pytest

import app.notify.discord as discord_notify
from app.intel import everef_ingest
from app.notify import user_discord
from app.ops import update_reports

NO_MENTIONS = {"parse": []}
NAME = "@everyone Fuel Blocks"
WHERE = "@here <@&123456789> Jita IV - Moon 4"


class _Settings:
    discord_webhook_url = "https://discord.example/webhook"
    discord_webhook = "https://discord.example/ops-webhook"
    discord_alert_types = "inventory_low,contract_low,auto_update,update_available"
    discord_relay_scope = "all"


class _Resp:
    status_code = 204
    headers = {}


class _Client:
    """Records every POST; instantiated per `async with`."""
    posts = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        type(self).posts.append((url, json))
        return _Resp()


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _Client.posts = []
    monkeypatch.setattr(discord_notify, "get_settings", lambda: _Settings())
    monkeypatch.setattr(discord_notify, "_last_sent", {})
    monkeypatch.setattr(discord_notify.httpx, "AsyncClient", _Client)


def _sent() -> dict:
    [(_, payload)] = _Client.posts
    return payload


# ── Every send path ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("alert_type,title,body", [
    # The bodies corporations.py builds from a threshold's form fields.
    ("inventory_low", "Inventory Low", f"{NAME} at {WHERE} — 3 left (low: 10)"),
    ("contract_low", "Contracts Low", f'Title: "{NAME}" — 1 outstanding (low: 5)'),
], ids=["inventory", "contract"])
def test_the_instance_relay_pings_nobody_with_a_typed_name(alert_type, title, body):
    assert _run(discord_notify.relay_user_alert(42, title, body, alert_type)) == discord_notify.SENT
    payload = _sent()
    assert "@everyone" in payload["content"]          # the text goes out as typed ...
    assert payload["allowed_mentions"] == NO_MENTIONS  # ... and pings nobody


def test_the_update_check_announcement_pings_nobody():
    """app/ops/update_check.py's call, with a release tag carrying a mention."""
    result = _run(discord_notify.send_discord_alert(
        title="Vigilant v9.9.9 @everyone is available",
        body="Running v1.0.0.\nhttps://example.invalid/releases/v9.9.9",
        alert_type="update_available", key="v9.9.9"))
    assert result == discord_notify.SENT
    assert _sent()["allowed_mentions"] == NO_MENTIONS


def test_an_update_report_pings_nobody():
    delivery = _run(update_reports._send_discord(f"Vigilant: updated {NAME}", WHERE, key="r1"))
    assert delivery["state"] == update_reports.SENT
    assert _sent()["allowed_mentions"] == NO_MENTIONS


def test_the_admin_discord_test_notification_pings_nobody(monkeypatch):
    settings = SimpleNamespace()

    async def notify_settings(db):
        return settings

    class _Db:
        async def commit(self):
            pass

    monkeypatch.setattr(update_reports, "get_notify_settings", notify_settings)
    delivery = _run(update_reports.send_test(_Db(), "discord"))
    assert delivery["state"] == update_reports.SENT
    assert _sent()["allowed_mentions"] == NO_MENTIONS


def test_the_everef_backfill_ping_pings_nobody(monkeypatch):
    class _Rows:
        def first(self):
            return (12, 3400)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Rows()

    monkeypatch.setattr("app.config.get_settings", lambda: _Settings())
    monkeypatch.setattr(everef_ingest, "AsyncSessionLocal", _Session)
    _run(everef_ingest._notify_discord_complete())
    [(url, payload)] = _Client.posts
    assert url == _Settings.discord_webhook
    assert "EVERef backfill complete" in payload["content"]
    assert payload["allowed_mentions"] == NO_MENTIONS


def test_a_users_own_webhook_pings_nobody():
    payload = user_discord.build_payload(NAME, WHERE, "inventory_low")
    assert payload["allowed_mentions"] == NO_MENTIONS


def test_nothing_passed_in_can_switch_mentions_back_on():
    payload = discord_notify.webhook_payload(content="@everyone", embeds=[], username="x")
    assert payload["allowed_mentions"] == NO_MENTIONS
    assert list(payload)[-1] == "allowed_mentions"


# ── One builder ──────────────────────────────────────────────────────────────

_DISCORD_KEYS = {"content", "embeds", "allowed_mentions"}


def _keys(node) -> list:
    """String keys a node puts into a dict, if it builds or fills one."""
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return [kw.arg for kw in node.keywords]
    if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)):
        return [node.slice.value]
    return []


def test_no_discord_body_is_built_outside_webhook_payload():
    """A Discord message body has `content` or `embeds`. Nothing in app/ may
    put either (or `allowed_mentions`) into a dict except webhook_payload, so
    a new sender cannot build its own body and forget to switch mentions off.
    If this fails on a dict that has nothing to do with Discord, rename the
    key or exempt that one place here."""
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    helper = root / "notify" / "discord.py"
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt: set[int] = set()
        if path == helper:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "webhook_payload":
                    exempt = {id(n) for n in ast.walk(node)}
            assert exempt, "webhook_payload is missing from app/notify/discord.py"
        for node in ast.walk(tree):
            if id(node) in exempt:
                continue
            hits = _DISCORD_KEYS.intersection(k for k in _keys(node) if isinstance(k, str))
            if hits:
                found.append(f"{path.relative_to(root.parent)}:{node.lineno} {sorted(hits)}")
    assert found == [], "build Discord bodies with app.notify.discord.webhook_payload: " + "; ".join(found)
