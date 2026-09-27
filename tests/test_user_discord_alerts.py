"""T-075: a user's own Discord webhook for alerts.

`app.notify.user_discord` is scheduled from `_emit_notification` next to the
instance-wide relay. It stores a credential-bearing URL and makes outbound
requests on a user's behalf, so most of what is pinned down here is the
security contract: the URL shape, masking, encryption at rest, the vetted
connection, mention-proof payloads, one warning line and no traceback on
failure, and a webhook that Discord reports gone being switched off.

No network: the HTTP client is faked at `user_discord.httpx.AsyncClient`, and
address vetting is faked at `user_discord.vetted_addresses` (or, for the one
test that exercises it, one level down at the resolver). Every webhook URL
here is made up. Sync-style event loops, per tests/test_discord_alert_relay.py.
"""
import asyncio
import base64
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as main
from app.db.models import AsyncSessionLocal, UserNotifySettings, engine
from app.notify import user_discord
from app.ops import update_reports as ur
from tests.conftest import ensure_user

HOOK = "https://discord.com/api/webhooks/123456789012345678/abc_DEF-ghi"
HOOK2 = "https://discord.com/api/webhooks/987654321098765432/zyx_WVU-tsr"
TOKEN = "abc_DEF-ghi"
PUBLIC_ADDR = "93.184.215.14"


# ── Harness ───────────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Every test drives its own loop; a pooled connection pinned to this
        # one would be unusable from the next (see conftest).
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Records every POST and every constructor kwarg; per `async with`."""
    posts = []
    kwargs = []
    status = 204
    headers = {}
    raise_on_post = None

    def __init__(self, *args, **kwargs):
        type(self).kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        type(self).posts.append((url, json, headers))
        if type(self).raise_on_post is not None:
            raise type(self).raise_on_post
        return _Resp(type(self).status, type(self).headers)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.kwargs = []
    _FakeClient.status = 204
    _FakeClient.headers = {}
    _FakeClient.raise_on_post = None
    user_discord._reset_state()

    async def vetted(url):
        return None, [PUBLIC_ADDR]

    monkeypatch.setattr(user_discord, "vetted_addresses", vetted)
    monkeypatch.setattr(user_discord.httpx, "AsyncClient", _FakeClient)
    yield
    user_discord._reset_state()


async def _save_row(user_id, url=HOOK, types="structure_attack,pi_expiring", enabled=True):
    async with AsyncSessionLocal() as db:
        row = await db.get(UserNotifySettings, user_id)
        if row is None:
            row = UserNotifySettings(user_id=user_id)
            db.add(row)
        row.discord_webhook_url = url
        row.alert_types = types
        row.enabled = enabled
        row.last_at = row.last_ok = row.last_error = None
        await db.commit()
    user_discord.invalidate(user_id)


async def _row(user_id):
    async with AsyncSessionLocal() as db:
        return await db.get(UserNotifySettings, user_id)


def _send(user_id, title="Structure Under Attack", body="Astrahus in J100001",
          alert_type="structure_attack", key=None, event=None):
    return user_discord.send_user_discord_alert(user_id, title, body, alert_type, key, event=event)


# ── URL validation matrix ─────────────────────────────────────────────────────

ACCEPTED = [
    HOOK,
    "https://discordapp.com/api/webhooks/1/a",
    "https://ptb.discord.com/api/webhooks/1234567890/abc-DEF_123",
    "https://canary.discord.com/api/webhooks/1234567890/abc",
    "https://discord.com:443/api/webhooks/123/abc",
    "https://discord.com/api/webhooks/123/abc?thread_id=456",
    "https://discord.com/api/webhooks/123/abc?wait=true",
    "https://discord.com/api/webhooks/123/abc?thread_id=456&wait=true",
    "https://DISCORD.com/api/webhooks/123/abc",
    "https://discord.com/api/webhooks/" + "9" * 25 + "/" + "a" * 100,
]

REJECTED = [
    ("", "empty"),
    ("http://discord.com/api/webhooks/123/abc", "http"),
    ("ftp://discord.com/api/webhooks/123/abc", "other scheme"),
    ("discord.com/api/webhooks/123/abc", "no scheme"),
    ("https://discord.com.evil.test/api/webhooks/123/abc", "lookalike suffix"),
    ("https://evil.test/discord.com/api/webhooks/123/abc", "host in path"),
    ("https://xn--dscord-9ya.com/api/webhooks/123/abc", "punycode host"),
    ("https://discord.com./api/webhooks/123/abc", "trailing-dot host"),
    ("https://www.discord.com/api/webhooks/123/abc", "unlisted subdomain"),
    ("https://162.159.128.233/api/webhooks/123/abc", "IPv4 literal"),
    ("https://[2606:4700::1]/api/webhooks/123/abc", "IPv6 literal"),
    ("https://!@discord.com/api/webhooks/123/abc", "userinfo"),
    ("https://discord.com@evil.test/api/webhooks/123/abc", "host as userinfo"),
    ("https://user:@discord.com/api/webhooks/123/abc", "user and empty password"),
    ("https://:@discord.com/api/webhooks/123/abc", "empty userinfo"),
    ("https://discord.com:8443/api/webhooks/123/abc", "other port"),
    ("https://discord.com:443x/api/webhooks/123/abc", "invalid port"),
    ("https://discord.com/api/webhook/123/abc", "wrong path"),
    ("https://discord.com/api/webhooks/123", "no token"),
    ("https://discord.com/api/webhooks/abc/abc", "non-numeric id"),
    ("https://discord.com/api/webhooks/123/abc/extra", "extra path"),
    ("https://discord.com/api/webhooks/123/abc/", "trailing slash"),
    ("https://discord.com/api/webhooks/123/ab%2Fc", "encoded token"),
    ("https://discord.com/api/webhooks/123/abc.def", "dot in token"),
    ("https://discord.com/api/webhooks/" + "9" * 26 + "/abc", "id too long"),
    ("https://discord.com/api/webhooks/123/" + "a" * 101, "token too long"),
    ("https://discord.com/api/webhooks/123/abc#frag", "fragment"),
    ("https://discord.com/api/webhooks/123/abc#", "empty fragment"),
    ("https://discord.com/api/webhooks/123/abc?foo=bar", "unknown query"),
    ("https://discord.com/api/webhooks/123/abc?thread_id=abc", "non-numeric thread"),
    ("https://discord.com/api/webhooks/123/abc?wait=false", "wait not true"),
    ("https://discord.com/api/webhooks/123/abc?thread_id=1&thread_id=2", "repeated query"),
    ("https://discord.com/api/webhooks/123/abc?thread_id", "bare query key"),
    ("https://discord.com/api/webhooks/123/abc ", "trailing space"),
    ("https://discord.com/api/webhooks/123/ab c", "inner space"),
    ("https://discord.com/api/webhooks/123/abc\n", "newline"),
    ("https://discord.com/api/webhooks/123/abc\x00", "NUL"),
    ("https://discord.com/api/webhooks/123/abc\t", "tab"),
    ("https://dіscord.com/api/webhooks/123/abc", "cyrillic homograph"),
    ("https://discord.com/api/webhooks/123/abc" + "?wait=true" * 30, "over 300 chars"),
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_accepted_webhook_forms(url):
    assert user_discord.validate_discord_webhook_url(url) == (True, None)


@pytest.mark.parametrize("url,why", REJECTED, ids=[w for _, w in REJECTED])
def test_rejected_webhook_forms(url, why):
    ok, problem = user_discord.validate_discord_webhook_url(url)
    assert not ok, why
    assert problem


# ── Masking and logging ──────────────────────────────────────────────────────

def test_mask_shows_host_and_id_prefix_only():
    masked = user_discord.mask_webhook_url(HOOK)
    assert masked == "discord.com/api/webhooks/1234…/••••"
    assert TOKEN not in masked and "123456789012345678" not in masked
    assert user_discord.mask_webhook_url(None) == ""
    assert user_discord.mask_webhook_url("garbage") == "(unreadable URL)"


def test_log_reference_is_host_and_id_never_token():
    ref = user_discord.webhook_log_ref(HOOK)
    assert ref == "discord.com/123456789012345678"
    assert TOKEN not in ref


# ── Storage ──────────────────────────────────────────────────────────────────

def test_the_url_is_encrypted_at_rest():
    uid = ensure_user(90101)

    async def go():
        await _save_row(uid)
        async with AsyncSessionLocal() as db:
            raw = (await db.execute(text(
                "SELECT discord_webhook_url FROM user_notify_settings WHERE user_id = :u"),
                {"u": uid})).scalar_one()
            row = await db.get(UserNotifySettings, uid)
            return raw, row.discord_webhook_url

    raw, decoded = _run(go())
    assert "discord.com" not in raw and TOKEN not in raw
    assert decoded == HOOK


# ── The relay hook in _emit_notification ─────────────────────────────────────

def test_emit_notification_schedules_the_user_relay_only_when_relaying(monkeypatch):
    from app.routes import dashboard

    user_calls, global_calls = [], []

    async def user_spy(*a, **k):
        user_calls.append((a, k))

    async def global_spy(*a, **k):
        global_calls.append((a, k))

    monkeypatch.setattr(dashboard, "send_user_discord_alert", user_spy)
    monkeypatch.setattr(dashboard, "send_discord_alert", global_spy)

    async def go():
        dashboard._emit_notification(-75, {"type": "auto_update", "title": "t"}, relay=False)
        dashboard._emit_notification(-75, {"type": "pi_expiring", "title": "PI Expiring", "body": "b"})
        await asyncio.sleep(0)

    _run(go())
    dashboard._notification_events.pop(-75, None)
    assert len(global_calls) == 1                      # the global relay, untouched
    assert len(user_calls) == 1
    args, kwargs = user_calls[0]
    assert args == (-75, "PI Expiring", "b", "pi_expiring", None)
    assert kwargs["event"]["type"] == "pi_expiring"


# ── Gating ───────────────────────────────────────────────────────────────────

def test_no_row_no_send():
    uid = ensure_user(90102)
    assert _run(_send(uid)) == user_discord.NOT_SENT_NO_WEBHOOK
    assert _FakeClient.posts == []


def test_only_opted_in_types_are_sent():
    uid = ensure_user(90103)

    async def go():
        await _save_row(uid, types="pi_expiring")
        a = await _send(uid, alert_type="structure_attack")
        b = await _send(uid, "PI Expiring", "extractors expiring", "pi_expiring")
        return a, b

    a, b = _run(go())
    assert a == user_discord.NOT_SENT_TYPE_OFF
    assert b == user_discord.SENT
    assert len(_FakeClient.posts) == 1
    assert _FakeClient.posts[0][0] == HOOK


def test_a_paused_webhook_sends_nothing():
    uid = ensure_user(90104)

    async def go():
        await _save_row(uid, enabled=False)
        return await _send(uid)

    assert _run(go()) == user_discord.NOT_SENT_DISABLED
    assert _FakeClient.posts == []


def test_legacy_structure_alert_follows_structure_attack():
    uid = ensure_user(90105)

    async def go():
        await _save_row(uid, types="structure_attack")
        return await _send(uid, alert_type="structure_alert")

    assert _run(go()) == user_discord.SENT


def test_dedup_is_per_user_so_two_users_both_get_theirs():
    a, b = ensure_user(90106), ensure_user(90107)

    async def go():
        await _save_row(a)
        await _save_row(b, url=HOOK2)
        r1 = await _send(a, key="astrahus-1")
        r2 = await _send(b, key="astrahus-1")
        r3 = await _send(a, key="astrahus-1")
        r4 = await _send(a, key="astrahus-2")
        return r1, r2, r3, r4

    r1, r2, r3, r4 = _run(go())
    assert (r1, r2) == (user_discord.SENT, user_discord.SENT)
    assert r3 == user_discord.NOT_SENT_SUPPRESSED
    assert r4 == user_discord.SENT
    assert [p[0] for p in _FakeClient.posts] == [HOOK, HOOK2, HOOK]


def test_the_per_user_rate_cap_drops_the_excess():
    uid = ensure_user(90108)

    async def go():
        await _save_row(uid, types="skill_complete")
        return [await _send(uid, "Skill Complete", f"skill {i}", "skill_complete", key=f"s{i}")
                for i in range(user_discord._RATE_LIMIT + 5)]

    results = _run(go())
    assert results.count(user_discord.SENT) == user_discord._RATE_LIMIT
    assert results.count(user_discord.NOT_SENT_RATE_CAPPED) == 5
    assert len(_FakeClient.posts) == user_discord._RATE_LIMIT
    assert user_discord.dropped_count(uid) == 5


# ── The request itself ───────────────────────────────────────────────────────

def test_the_client_is_pinned_no_redirects_no_env_proxy_five_seconds():
    uid = ensure_user(90109)

    async def go():
        await _save_row(uid)
        return await _send(uid)

    assert _run(go()) == user_discord.SENT
    [kw] = _FakeClient.kwargs
    assert kw["follow_redirects"] is False
    assert kw["trust_env"] is False
    assert kw["timeout"] == 5.0
    assert isinstance(kw["transport"], ur._PinnedTransport)


def test_mentions_are_neutralised():
    uid = ensure_user(90110)

    async def go():
        await _save_row(uid)
        return await _send(uid, "@everyone look", "ping <@&123> and @here")

    assert _run(go()) == user_discord.SENT
    [(url, payload, headers)] = _FakeClient.posts
    assert payload["allowed_mentions"] == {"parse": []}
    flat = json.dumps(payload)
    assert "@everyone" not in flat and "@here" not in flat and "<@&123>" not in flat
    assert "everyone" in payload["embeds"][0]["title"]
    assert headers["User-Agent"].startswith("Vigilant/")


def test_a_kill_alert_gets_a_readable_message_and_its_own_key():
    uid = ensure_user(90111)
    event = {"type": "kill_alert", "kind": "loss", "killmail_id": 4242, "system_name": "J100001",
             "matched_label": "Watched Corp", "total_value": 2.5e9,
             "zkb_url": "https://zkillboard.com/kill/4242/"}

    async def go():
        await _save_row(uid, types="kill_alert")
        a = await _send(uid, "kill_alert", "", "kill_alert", event=event)
        b = await _send(uid, "kill_alert", "", "kill_alert", event=dict(event, killmail_id=4243))
        return a, b

    assert _run(go()) == (user_discord.SENT, user_discord.SENT)
    embed = _FakeClient.posts[0][1]["embeds"][0]
    assert embed["title"] == "Kill alert"
    assert "Watched Corp" in embed["description"] and "2.50B ISK" in embed["description"]


def test_a_non_public_target_is_never_dialled(monkeypatch):
    """Through the real vetting, with the resolver answering loopback."""
    uid = ensure_user(90112)
    monkeypatch.setattr(user_discord, "vetted_addresses", ur.vetted_addresses)

    async def resolve(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(ur, "_resolve", resolve)

    async def go():
        await _save_row(uid)
        r = await _send(uid)
        return r, await _row(uid)

    result, row = _run(go())
    assert result.startswith("failed: not sent:")
    assert _FakeClient.posts == []
    assert row.last_ok is False and row.last_error.startswith("not sent:")
    assert HOOK not in row.last_error and TOKEN not in row.last_error


# ── Failure handling ─────────────────────────────────────────────────────────

def test_a_failure_is_one_warning_line_without_a_traceback(caplog):
    uid = ensure_user(90113)
    _FakeClient.raise_on_post = ConnectionError("boom " + HOOK)

    async def go():
        await _save_row(uid)
        with caplog.at_level(logging.WARNING, logger="app.notify.user_discord"):
            r = await _send(uid)
        return r, await _row(uid)

    result, row = _run(go())
    assert result == "failed: ConnectionError"
    records = [r for r in caplog.records if r.name == "app.notify.user_discord"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None and records[0].exc_text is None
    assert "Traceback" not in caplog.text
    assert TOKEN not in caplog.text and HOOK not in caplog.text
    assert "discord.com/123456789012345678" in records[0].getMessage()
    assert row.last_ok is False and row.last_error == "ConnectionError" and row.last_at is not None


def test_a_gone_webhook_is_disabled_and_stops_sending():
    uid = ensure_user(90114)
    _FakeClient.status = 404

    async def go():
        await _save_row(uid)
        a = await _send(uid, key="k1")
        row = await _row(uid)
        b = await _send(uid, key="k2")
        return a, row, b

    a, row, b = _run(go())
    assert a == f"failed: {user_discord.GONE_ERROR}"
    assert row.enabled is False and row.last_ok is False
    assert row.last_error == user_discord.GONE_ERROR
    assert b == user_discord.NOT_SENT_DISABLED
    assert len(_FakeClient.posts) == 1


def test_a_401_counts_as_gone_too():
    uid = ensure_user(90115)
    _FakeClient.status = 401

    async def go():
        await _save_row(uid)
        await _send(uid)
        return await _row(uid)

    assert _run(go()).enabled is False


def test_a_429_drops_until_retry_after_without_sleeping(monkeypatch):
    uid = ensure_user(90116)
    _FakeClient.status = 429
    _FakeClient.headers = {"retry-after": "30"}
    clock = [1000.0]
    monkeypatch.setattr(user_discord.time, "monotonic", lambda: clock[0])

    async def go():
        await _save_row(uid)
        a = await _send(uid, key="k1")
        b = await _send(uid, key="k2")
        clock[0] += 31
        _FakeClient.status = 204
        c = await _send(uid, key="k3")
        return a, b, c

    a, b, c = _run(go())
    assert a.startswith("failed: HTTP 429")
    assert b == user_discord.NOT_SENT_MUTED
    assert c == user_discord.SENT
    assert len(_FakeClient.posts) == 2


def test_a_redirect_is_not_followed():
    uid = ensure_user(90117)
    _FakeClient.status = 302

    async def go():
        await _save_row(uid)
        return await _send(uid)

    assert _run(go()) == "failed: HTTP 302: redirect not followed"


def test_the_stored_url_is_revalidated_before_every_send():
    uid = ensure_user(90118)

    async def go():
        await _save_row(uid, url="https://discord.com.evil.test/api/webhooks/1/a")
        r = await _send(uid)
        return r, await _row(uid)

    result, row = _run(go())
    assert _FakeClient.posts == []
    assert result == f"failed: {user_discord.INVALID_STORED_ERROR}"
    assert row.enabled is False


def test_the_never_raise_contract(monkeypatch):
    async def explode(user_id):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(user_discord, "_config_for", explode)
    assert _run(_send(1)) == "failed: RuntimeError"


# ── Account routes ───────────────────────────────────────────────────────────

CSRF = "t075-csrf"


def _cookie(**data) -> str:
    from itsdangerous import TimestampSigner

    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(os.environ["SECRET_KEY"]).sign(payload).decode()


def _client(user_id=None) -> TestClient:
    # https: the session cookie is Secure, so a flash set by one request only
    # comes back on the next over https.
    client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    data = {"csrf_token": CSRF}
    if user_id is not None:
        data["user_id"] = user_id
    client.cookies.set("vigilant_session", _cookie(**data))
    client.headers.update({"X-CSRF-Token": CSRF})
    return client


def _page(client) -> str:
    r = client.get("/account")
    assert r.status_code == 200
    return r.text


def test_save_stores_the_url_and_the_page_shows_only_the_mask():
    uid = ensure_user(90201)
    c = _client(uid)
    r = c.post("/account/notifications/discord",
               data={"discord_webhook_url": HOOK, "alert_types": ["pi_expiring", "structure_fuel", "bogus"]})
    assert r.status_code == 303 and r.headers["location"] == "/account#discord-alerts"

    row = _run(_row(uid))
    assert row.discord_webhook_url == HOOK
    assert row.alert_types == "structure_fuel,pi_expiring"
    assert row.enabled is True

    html = _page(c)
    assert "Discord alert settings saved." in html
    assert "Webhook set · <span class=\"mono\">discord.com/api/webhooks/1234…/••••</span>" in html
    assert TOKEN not in html and "123456789012345678" not in html
    assert 'name="discord_webhook_url" type="password"' in html
    assert 'value="' + HOOK not in html
    assert 'name="alert_types" value="pi_expiring" class="b-check" checked' in html
    assert 'name="alert_types" value="skill_complete" class="b-check" >' in html


def test_the_page_defaults_before_anything_is_saved():
    uid = ensure_user(90202)
    html = _page(_client(uid))
    assert "Not set." in html
    for key in user_discord.DEFAULT_ALERT_TYPES:
        assert f'value="{key}" class="b-check" checked' in html
    assert "Send test message" not in html


def test_a_blank_field_keeps_the_saved_url():
    uid = ensure_user(90203)
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": ["pi_expiring"]})
    r = c.post("/account/notifications/discord",
               data={"discord_webhook_url": "", "alert_types": ["stockpile_low"], "enabled": "on"})
    assert r.status_code == 303
    row = _run(_row(uid))
    assert row.discord_webhook_url == HOOK
    assert row.alert_types == "stockpile_low"


def test_untick_enabled_pauses_and_a_new_url_unpauses():
    uid = ensure_user(90204)
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": ["pi_expiring"]})
    c.post("/account/notifications/discord", data={"discord_webhook_url": "", "alert_types": ["pi_expiring"]})
    assert _run(_row(uid)).enabled is False
    assert "paused" in _page(c)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK2, "alert_types": ["pi_expiring"]})
    row = _run(_row(uid))
    assert row.enabled is True and row.discord_webhook_url == HOOK2


def test_an_invalid_url_is_refused_and_nothing_changes():
    uid = ensure_user(90205)
    c = _client(uid)
    r = c.post("/account/notifications/discord",
               data={"discord_webhook_url": "https://discord.com.evil.test/api/webhooks/1/a",
                     "alert_types": ["pi_expiring"]})
    assert r.status_code == 303
    assert _run(_row(uid)) is None
    html = _page(c)
    assert "Webhook not saved" in html and "evil.test" not in html


def test_a_url_on_a_non_public_host_is_refused_at_save(monkeypatch):
    uid = ensure_user(90206)

    async def vetted(url):
        return "its host is a private, local or reserved address", []

    monkeypatch.setattr(user_discord, "vetted_addresses", vetted)
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": ["pi_expiring"]})
    assert _run(_row(uid)) is None
    assert "private, local or reserved" in _page(c)


def test_the_test_route_sends_one_message_and_records_the_result():
    uid = ensure_user(90207)
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": []})
    r = c.post("/account/notifications/discord/test")
    assert r.status_code == 303
    [(url, payload, _)] = _FakeClient.posts
    assert url == HOOK
    assert payload["embeds"][0]["title"] == "Test message"
    assert payload["allowed_mentions"] == {"parse": []}
    row = _run(_row(uid))
    assert row.last_ok is True and row.last_error is None and row.last_at is not None
    html = _page(c)
    assert "Test message sent" in html
    assert "last delivery" in html and '<span class="is-ok">ok</span>' in html


def test_the_test_route_reports_a_failure_without_the_url():
    uid = ensure_user(90208)
    _FakeClient.status = 404
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": []})
    c.post("/account/notifications/discord/test")
    row = _run(_row(uid))
    assert row.last_ok is False and row.enabled is False
    assert row.last_error == user_discord.GONE_ERROR
    html = _page(c)
    assert "Test message not delivered" in html and "no longer exists" in html
    assert TOKEN not in html


def test_the_test_route_without_a_webhook():
    uid = ensure_user(90209)
    c = _client(uid)
    assert c.post("/account/notifications/discord/test").status_code == 303
    assert _FakeClient.posts == []
    assert "No webhook saved yet" in _page(c)


def test_remove_clears_the_url():
    uid = ensure_user(90210)
    c = _client(uid)
    c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK, "alert_types": ["pi_expiring"]})
    r = c.post("/account/notifications/discord/remove")
    assert r.status_code == 303
    row = _run(_row(uid))
    assert row.discord_webhook_url is None and row.last_error is None
    assert "Not set." in _page(c)
    assert _run(_send(uid)) == user_discord.NOT_SENT_NO_WEBHOOK


def test_the_routes_answer_401_to_an_anonymous_htmx_caller_and_redirect_a_form_post():
    c = _client()
    for path in ("/account/notifications/discord", "/account/notifications/discord/test",
                 "/account/notifications/discord/remove"):
        assert c.post(path, data={}, headers={"HX-Request": "true"}).status_code == 401
        r = c.post(path, data={})
        assert r.status_code == 303 and r.headers["location"] == "/"
    assert _FakeClient.posts == []


def test_the_routes_need_the_csrf_token():
    uid = ensure_user(90211)
    c = _client(uid)
    del c.headers["X-CSRF-Token"]
    assert c.post("/account/notifications/discord", data={"discord_webhook_url": HOOK}).status_code == 403


def test_a_save_invalidates_the_relay_cache():
    uid = ensure_user(90212)

    async def go():
        await _save_row(uid, types="pi_expiring")
        return await _send(uid, alert_type="structure_attack")

    assert _run(go()) == user_discord.NOT_SENT_TYPE_OFF
    _client(uid).post("/account/notifications/discord",
                      data={"discord_webhook_url": "", "alert_types": ["structure_attack"], "enabled": "on"})
    assert _run(_send(uid)) == user_discord.SENT


# ── Registration ─────────────────────────────────────────────────────────────

def test_the_table_is_user_owned_and_never_seeded_on_dev():
    import importlib.util
    import pathlib

    from app.routes.admin import USER_OWNED_TABLES

    assert "user_notify_settings" in USER_OWNED_TABLES
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dev_seed_tables.py"
    spec = importlib.util.spec_from_file_location("dev_seed_tables", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "user_notify_settings" in mod.SKIP


def test_every_emitted_alert_type_is_in_the_allowlist():
    """The types _emit_notification is handed, by call site. auto_update is
    left out on purpose: its emitter passes relay=False."""
    emitted = {
        "skill_complete", "pi_expiring",                                    # dashboard sync
        "structure_attack", "structure_fuel", "structure_change",           # ESI notifications
        "sovereignty", "moonmining", "poco",
        "inventory_low", "inventory_critical",                              # corp inventory thresholds
        "contract_low", "contract_critical",                                # corp contract thresholds
        "kill_alert",                                                       # killmail stream
        "stockpile_low",                                                    # stockpile alerts
    }
    assert emitted == set(user_discord.ALERT_TYPES)
    assert set(user_discord.DEFAULT_ALERT_TYPES) <= emitted
