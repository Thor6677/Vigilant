"""Outcome reports for scheduled and automatic updates, and their delivery.

Driven through the scheduler's real tick against the hermetic database, with a
tmpdir standing in for /control. The HTTP boundary is faked at the client, and
the fake records how it was built as well as what it sent, so "redirects are not
followed" and "five seconds" are asserted rather than assumed.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.models import (AdminAuditLog, AsyncSessionLocal, UpdateNotifySettings,
                           UpdatePolicy, UpdateRunReport, UpdateSchedule, UpdateStatus,
                           User)
from app.ops import update_reports as ur
from app.ops import update_schedule as us
from tests.test_update_schedule_tick import (SUNDAY_0430, _beat, _finish, _latest,
                                             _policy, _request, _run, _schedule,
                                             _set_policy, control)  # noqa: F401

UTC = timezone.utc
HOOK = "https://ntfy.example/vigilant-secret-topic-abc123"


# ── Fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def quiet_discord(monkeypatch):
    """Discord is off unless a test turns it on: the relay reports "no webhook"
    exactly as the real one does with DISCORD_WEBHOOK_URL unset."""
    from app.notify import discord

    async def off(**kwargs):
        return discord.NOT_SENT_NO_WEBHOOK
    monkeypatch.setattr("app.notify.discord.send_discord_alert", off)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    """Stands in for httpx.AsyncClient. Class-level so a test can inspect it
    after the `async with` has closed."""

    built: list = []
    posts: list = []
    status = 200
    raise_with: Exception | None = None

    def __init__(self, *args, **kwargs):
        type(self).built.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        type(self).posts.append((url, kwargs))
        if type(self).raise_with is not None:
            raise type(self).raise_with
        return FakeResponse(type(self).status)


@pytest.fixture
def http(monkeypatch):
    FakeClient.built, FakeClient.posts = [], []
    FakeClient.status, FakeClient.raise_with = 200, None
    monkeypatch.setattr(ur.httpx, "AsyncClient", FakeClient)
    return FakeClient


def _notify(**kw):
    async def go():
        async with AsyncSessionLocal() as db:
            row = await ur.get_notify_settings(db)
            for k, v in kw.items():
                setattr(row, k, v)
            await db.commit()
    _run(go())


def _notify_row():
    async def go():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(UpdateNotifySettings))).scalars().first()
    return _run(go())


def _reports():
    async def go():
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(UpdateRunReport).order_by(UpdateRunReport.id))).scalars().all()
    return _run(go())


def _audit(prefix):
    async def go():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(AdminAuditLog))).scalars().all()
            return [(r.event_type, r.detail) for r in rows if r.event_type.startswith(prefix)]
    return _run(go())


def _clear_audit():
    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AdminAuditLog))
            await db.commit()
    _run(go())


def _fire_policy_and_finish(control, state, reverted_to=None):
    """One automatic run, from fire to terminal status, reconciled."""
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "fired policy"
    rid = _request(control)["id"]
    (control / "request.json").unlink()                     # sidecar claimed it
    _finish(control, rid, state, reverted_to=reverted_to)
    _run(us.tick(SUNDAY_0430 + timedelta(minutes=5)))
    return rid


def _fire_schedule_and_finish(control, state, reverted_to=None):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=5)
    assert _run(us.tick()) == "fired schedule"
    rid = _request(control)["id"]
    (control / "request.json").unlink()
    _finish(control, rid, state, reverted_to=reverted_to)
    _run(us.tick())
    return rid


# ── Every unattended run gets exactly one report ─────────────────────────────

@pytest.mark.parametrize("state,reverted_to,outcome", [
    ("success", None, "succeeded"),
    ("failed", None, "failed"),
    ("failed", "v1.2.0", "reverted"),
])
def test_a_scheduled_run_is_reported(control, state, reverted_to, outcome):
    """One-shot schedules used to get no report at all."""
    rid = _fire_schedule_and_finish(control, state, reverted_to)
    reports = _reports()
    assert len(reports) == 1
    r = reports[0]
    assert (r.kind, r.outcome, r.request_id) == ("scheduled", outcome, rid)
    assert r.to_tag == "v1.3.0"
    assert any(e == f"scheduled_update_{outcome}" for e, _ in _audit("scheduled_update_"))


@pytest.mark.parametrize("state,reverted_to,outcome", [
    ("success", None, "succeeded"),
    ("failed", None, "failed"),
    ("failed", "v1.2.0", "reverted"),
])
def test_an_automatic_run_is_reported(control, state, reverted_to, outcome):
    rid = _fire_policy_and_finish(control, state, reverted_to)
    [r] = _reports()
    assert (r.kind, r.outcome, r.request_id) == ("automatic", outcome, rid)
    if outcome != "succeeded":
        assert "paused" in r.detail
        assert _policy().enabled is False


def test_a_run_is_reported_once_however_many_ticks_see_it(control):
    _fire_schedule_and_finish(control, "success")
    for minutes in range(1, 4):
        _run(us.tick(datetime.now(UTC) + timedelta(minutes=minutes)))
    assert len(_reports()) == 1


def test_a_manual_run_is_not_reported(control):
    """Somebody pressed Update: they were watching, and it is not ours."""
    _beat(control, current_tag="v1.2.0")
    _finish(control, "someone-elses-run", "failed")
    _run(us.tick())
    assert _reports() == []


# ── Skipped: once per schedule, once per window, never per tick ──────────────

def test_a_schedule_held_past_its_grace_is_reported_skipped_once(control):
    """The updater stayed busy upgrading itself for the whole grace."""
    handoff = {"state": "handed_off", "target": "v1.2.0", "error": None, "at": "x"}
    _beat(control, current_tag="v1.2.0", self_update=handoff)
    base = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
    _schedule("v1.3.0", minutes_ago=0, base=base)

    for minutes in (1, 30, 90):          # due, held, held
        assert _run(us.tick(base + timedelta(minutes=minutes))) == "updater is upgrading itself"
    assert _reports() == []

    for minutes in (125, 126, 200):      # grace over: reported on the first only
        _run(us.tick(base + timedelta(minutes=minutes)))
    [r] = _reports()
    assert (r.kind, r.outcome, r.request_id) == ("scheduled", "skipped", None)
    assert "upgrading itself" in r.detail
    assert _request(control) is None


def test_a_schedule_missed_while_down_is_reported_skipped(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=60 * 6)
    assert _run(us.tick()) == "schedule missed its window"
    [r] = _reports()
    assert r.outcome == "skipped" and "not running" in r.detail
    assert any(e == "scheduled_update_skipped" for e, _ in _audit("scheduled_update_"))


def test_a_stale_heartbeat_holds_rather_than_ignores(control):
    """A crashed sidecar is exactly the missed window an admin must hear about,
    so a stale heartbeat is a hold reason, not "no updater on this install"."""
    _beat(control, current_tag="v1.2.0")
    past = time.time() - 3600
    os.utime(control / "updater.json", (past, past))
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "no updater"
    p = _policy()
    assert (p.held_window, p.held_reason) == ("2026-09-13", "no updater")
    assert _request(control) is None


def test_a_policy_window_held_to_the_end_is_reported_skipped_once(control):
    _beat(control, current_tag="v1.2.0",
          checks={"socket": "ok", "remote": "FAIL: could not reach origin"})
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)).startswith("updater checks not passing")
    assert _reports() == []

    after = datetime(2026, 9, 13, 6, 30, tzinfo=UTC)         # grace over
    assert _run(us.tick(after)) == "window skipped"
    assert _run(us.tick(after + timedelta(minutes=1))) == "outside the window"
    [r] = _reports()
    assert (r.kind, r.outcome) == ("automatic", "skipped")
    assert "remote" in r.detail and "2026-09-13" in r.detail
    assert _policy().last_skipped_window == "2026-09-13"


def test_a_window_nothing_wanted_is_not_reported(control):
    """Up to date all window long: nothing was skipped."""
    _beat(control, current_tag="v1.3.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    _run(us.tick(datetime(2026, 9, 13, 6, 30, tzinfo=UTC)))
    assert _reports() == []


def _published(when):
    async def go():
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(UpdateStatus))).scalars().first()
            row.latest_published_at = when.replace(tzinfo=None)
            await db.commit()
    _run(go())


def test_a_window_passed_while_vigilant_was_down_is_reported(control, monkeypatch):
    """No tick ran in the window; the release existed before it closed."""
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _published(datetime(2026, 9, 10, tzinfo=UTC))
    monkeypatch.setattr(us, "STARTED_AT", datetime(2026, 9, 13, 9, 0, tzinfo=UTC))
    assert _run(us.tick(datetime(2026, 9, 13, 9, 1, tzinfo=UTC))) == "window skipped"
    [r] = _reports()
    assert "not running" in r.detail


def test_a_release_published_after_the_window_is_not_a_skip(control, monkeypatch):
    """A false "skipped" is a sticky banner someone has to acknowledge."""
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _published(datetime(2026, 9, 13, 8, 0, tzinfo=UTC))       # after 06:00 close
    monkeypatch.setattr(us, "STARTED_AT", datetime(2026, 9, 13, 9, 0, tzinfo=UTC))
    _run(us.tick(datetime(2026, 9, 13, 9, 1, tzinfo=UTC)))
    assert _reports() == []


def test_a_window_while_running_with_no_hold_is_not_a_skip(control, monkeypatch):
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _published(datetime(2026, 9, 10, tzinfo=UTC))
    monkeypatch.setattr(us, "STARTED_AT", datetime(2026, 9, 13, 3, 0, tzinfo=UTC))
    _run(us.tick(datetime(2026, 9, 13, 9, 1, tzinfo=UTC)))
    assert _reports() == []


# ── The audit row comes first and cannot be lost to a channel ────────────────

def test_the_audit_row_is_written_even_when_every_channel_fails(control, monkeypatch, http):
    async def boom(**kwargs):
        raise RuntimeError("discord is down")
    monkeypatch.setattr("app.notify.discord.send_discord_alert", boom)
    http.raise_with = RuntimeError("connection refused")
    _notify(webhook_url=HOOK, webhook_format="json")
    _clear_audit()

    _fire_policy_and_finish(control, "failed", reverted_to="v1.2.0")

    assert [e for e, _ in _audit("auto_update_reverted")], "no audit row for the outcome"
    [r] = _reports()
    d = ur.deliveries_of(r)
    assert d["discord"]["state"] == "failed"
    assert d["webhook"]["state"] == "failed"
    assert _notify_row().webhook_last_ok is False


# ── Discord: once, through the report, never through the bell's relay ────────

@pytest.fixture
def admin_user():
    async def add():
        async with AsyncSessionLocal() as db:
            u = User(role="admin", is_admin=True)
            db.add(u)
            await db.commit()
            return u.id
    uid = _run(add())
    yield uid

    async def remove():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()
    _run(remove())


def test_discord_gets_one_message_and_the_bell_gets_the_event(control, monkeypatch, admin_user):
    from app.notify import discord
    from app.routes import dashboard

    calls = []

    async def spy(*args, **kwargs):
        calls.append(kwargs or args)
        return discord.SENT
    # Both references: the relay inside _emit_notification binds the name at
    # import, so patching only the module attribute would miss a double send.
    monkeypatch.setattr("app.notify.discord.send_discord_alert", spy)
    monkeypatch.setattr(dashboard, "send_discord_alert", spy)
    dashboard._notification_events.pop(admin_user, None)

    # tick() runs inside an event loop, so _emit_notification's relay WOULD
    # schedule a Discord task here if the dispatcher let it.
    _fire_policy_and_finish(control, "success")

    assert len(calls) == 1, calls
    assert calls[0]["alert_type"] == "auto_update"
    events = dashboard._notification_events.pop(admin_user, [])
    assert [e["type"] for e in events] == ["auto_update"]
    [r] = _reports()
    assert ur.deliveries_of(r)["discord"]["state"] == "sent"


def test_emit_notification_without_relay_never_schedules_discord(monkeypatch):
    from app.routes import dashboard

    scheduled = []

    async def spy(*a, **k):
        scheduled.append(a or k)

    monkeypatch.setattr(dashboard, "send_discord_alert", spy)

    async def go():
        dashboard._emit_notification(-7, {"type": "auto_update", "title": "t"}, relay=False)
        dashboard._emit_notification(-7, {"type": "structure_attack", "title": "t"})
        await asyncio.sleep(0)
    _run(go())
    dashboard._notification_events.pop(-7, None)
    assert len(scheduled) == 1                      # only the relayed one


def test_an_unconfigured_discord_is_left_out_of_the_record(control):
    _fire_policy_and_finish(control, "success")
    [r] = _reports()
    assert "discord" not in ur.deliveries_of(r)


def test_discord_problems_only_filters_a_success(control, monkeypatch):
    from app.notify import discord

    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return discord.SENT
    monkeypatch.setattr("app.notify.discord.send_discord_alert", spy)
    monkeypatch.setattr(ur, "_discord_configured", lambda: True)
    _notify(discord_policy="problems")
    _fire_policy_and_finish(control, "success")
    assert calls == []
    [r] = _reports()
    assert ur.deliveries_of(r)["discord"]["state"] == "filtered"


# ── The generic webhook ──────────────────────────────────────────────────────

def test_json_webhook_shape(control, http):
    _notify(webhook_url=HOOK, webhook_format="json")
    rid = _fire_policy_and_finish(control, "success")
    [(url, kwargs)] = http.posts
    assert url == HOOK
    body = kwargs["json"]
    assert body == {
        "event": "vigilant.update", "kind": "automatic", "outcome": "succeeded",
        "from_tag": "v1.2.0", "to_tag": "v1.3.0", "at": body["at"],
        "detail": body["detail"],
    }
    assert body["at"].endswith("Z") and "v1.3.0" in body["detail"]
    assert ur.deliveries_of(_reports()[0])["webhook"]["state"] == "sent"
    assert _notify_row().webhook_last_ok is True
    assert rid


def test_the_client_never_follows_redirects_and_gives_up_in_five_seconds(control, http):
    _notify(webhook_url=HOOK, webhook_format="json")
    http.status = 302
    _fire_policy_and_finish(control, "success")
    assert http.built and all(b.get("follow_redirects") is False for b in http.built)
    assert all(b.get("timeout") == 5.0 for b in http.built)
    d = ur.deliveries_of(_reports()[0])["webhook"]
    assert d["state"] == "failed" and "redirect not followed" in d["error"]


@pytest.mark.parametrize("state,reverted_to,priority", [
    ("success", None, "default"),
    ("failed", "v1.2.0", "high"),
])
def test_ntfy_shape(control, http, state, reverted_to, priority):
    _notify(webhook_url=HOOK, webhook_format="ntfy")
    _fire_policy_and_finish(control, state, reverted_to)
    [(url, kwargs)] = http.posts
    assert url == HOOK
    assert isinstance(kwargs["content"], bytes)
    assert b"v1.3.0" in kwargs["content"]
    headers = kwargs["headers"]
    assert headers["Priority"] == priority
    assert headers["Title"].startswith("Vigilant: automatic update to v1.3.0")
    assert "vigilant" in headers["Tags"]
    for name in ("Title", "Priority", "Tags"):
        headers[name].encode("latin-1")             # httpx would refuse otherwise


def test_problems_only_webhook_skips_a_success_and_sends_a_failure(control, http):
    _notify(webhook_url=HOOK, webhook_format="json", webhook_policy="problems")
    _fire_schedule_and_finish(control, "success")
    assert http.posts == []
    assert ur.deliveries_of(_reports()[0])["webhook"]["state"] == "filtered"
    assert _notify_row().webhook_last_at is None     # a filter is not an attempt

    _fire_policy_and_finish(control, "failed")
    assert len(http.posts) == 1


def test_the_webhook_url_never_reaches_an_error_or_the_log(control, http, caplog):
    _notify(webhook_url=HOOK, webhook_format="json")
    http.raise_with = RuntimeError(f"could not connect to {HOOK}")
    with caplog.at_level(logging.WARNING):
        _fire_policy_and_finish(control, "success")
    d = ur.deliveries_of(_reports()[0])["webhook"]
    assert "secret-topic" not in d["error"]
    assert "secret-topic" not in caplog.text
    assert "secret-topic" not in (_notify_row().webhook_last_error or "")
    assert all("secret-topic" not in (detail or "") for _, detail in _audit(""))


# ── Test notifications ───────────────────────────────────────────────────────

def test_a_test_notification_ignores_problems_only_and_records_the_attempt(control, http):
    _notify(webhook_url=HOOK, webhook_format="ntfy", webhook_policy="problems")

    async def go():
        async with AsyncSessionLocal() as db:
            return await ur.send_test(db, "webhook")
    result = _run(go())
    assert result["state"] == "sent"
    [(url, kwargs)] = http.posts
    assert kwargs["headers"]["Title"] == "Vigilant: test notification"
    assert _notify_row().webhook_last_ok is True


def test_a_discord_test_uses_a_fresh_key_each_time(monkeypatch):
    from app.notify import discord

    keys = []

    async def spy(**kwargs):
        keys.append(kwargs["key"])
        return discord.SENT
    monkeypatch.setattr("app.notify.discord.send_discord_alert", spy)
    monkeypatch.setattr(ur, "_discord_configured", lambda: True)

    async def go():
        async with AsyncSessionLocal() as db:
            await ur.send_test(db, "discord")
            await asyncio.sleep(0.01)
            await ur.send_test(db, "discord")
    _run(go())
    assert len(keys) == 2 and keys[0] != keys[1]


# ── Pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,ok", [
    ("https://ntfy.sh/abc", True),
    ("http://hooks.example:8080/hook", True),
    ("ftp://example.com/x", False),
    ("javascript:alert(1)", False),
    ("https:///nohost", False),
    ("https://exa mple.com/x", False),
    ("", False),
    (None, False),
    ("https://example.com/" + "a" * 600, False),
])
def test_webhook_url_validation(raw, ok):
    url, error = ur.validate_webhook_url(raw)
    assert (url is not None) is ok
    assert (error is None) is ok


def test_redaction_keeps_the_host_and_hides_the_topic():
    shown = ur.redact_url(HOOK)
    assert shown.startswith("https://ntfy.example/")
    assert "secret" not in shown and "topic" not in shown


@pytest.mark.parametrize("policy,outcome,expected", [
    ("all", "succeeded", True), ("problems", "succeeded", False),
    ("problems", "failed", True), ("problems", "reverted", True),
    ("problems", "skipped", True),
])
def test_report_policies(policy, outcome, expected):
    assert ur.wants(policy, outcome) is expected


# ── Docs ─────────────────────────────────────────────────────────────────────

def test_the_docs_describe_the_channels_as_built():
    """The JSON shape is a contract with whatever receives it; the README is
    where an operator reads it, so pin it to the code's own keys."""
    from pathlib import Path
    from types import SimpleNamespace

    readme = Path("README.md").read_text()
    keys = ur.json_payload(SimpleNamespace(
        kind="automatic", outcome="failed", from_tag=None, to_tag=None,
        created_at=None, detail=None)).keys()
    for key in keys:
        assert f'"{key}"' in readme, key
    flat = " ".join(readme.split())
    for needle in ("ntfy", "auto_update", "problems only", "Send test notification"):
        assert needle in flat, needle
    env = Path(".env.example").read_text()
    assert "auto_update" in env and "updater panel" in env


# ── A false "skipped" is worse than a missing one ────────────────────────────

def test_a_held_window_satisfied_some_other_way_is_not_a_skip(control):
    """Held all morning behind an admin's own run, which applied the release."""
    _beat(control, current_tag="v1.2.0")
    started = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    (control / "status.json").write_text(json.dumps({"state": "running", "started_at": started}))
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "busy"
    assert _policy().held_window == "2026-09-13"

    (control / "status.json").unlink()
    _beat(control, current_tag="v1.3.0")                    # someone else deployed it
    _run(us.tick(datetime(2026, 9, 13, 6, 30, tzinfo=UTC)))
    assert _reports() == []
    assert _policy().held_window is None


def test_a_missed_schedule_whose_tag_is_now_running_is_not_a_skip(control):
    handoff = {"state": "pulling", "target": "v1.2.0", "error": None, "at": "x"}
    _beat(control, current_tag="v1.2.0", self_update=handoff)
    base = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
    _schedule("v1.3.0", minutes_ago=0, base=base)
    assert _run(us.tick(base + timedelta(minutes=5))) == "updater is upgrading itself"

    _beat(control, current_tag="v1.3.0")                    # deployed from the CLI
    assert _run(us.tick(base + timedelta(hours=3))) == "scheduled tag already running"
    assert _reports() == []


def test_a_restart_after_a_window_vigilant_saw_is_not_a_skip(control, monkeypatch):
    """Up to date through Sunday's window; rolled back on Wednesday, which
    restarts the app. The window was not missed while down."""
    _beat(control, current_tag="v1.3.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _published(datetime(2026, 9, 10, tzinfo=UTC))
    assert _run(us.tick(SUNDAY_0430)) == "already up to date"
    assert _policy().observed_window == "2026-09-13"

    _beat(control, current_tag="v1.2.9")                    # the rollback
    monkeypatch.setattr(us, "STARTED_AT", datetime(2026, 9, 16, 12, 0, tzinfo=UTC))
    _run(us.tick(datetime(2026, 9, 16, 12, 1, tzinfo=UTC)))
    assert _reports() == []


# ── A request nothing claimed is withdrawn, not left to run whenever ─────────

def _age_request(control, hours):
    path = control / "request.json"
    req = json.loads(path.read_text())
    req["requested_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    path.write_text(json.dumps(req))
    return req["id"]


def test_an_unclaimed_request_of_ours_is_withdrawn_after_the_grace(control):
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "fired policy"
    rid = _age_request(control, hours=3)          # the sidecar died before claiming

    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=1))) == "stale request withdrawn"
    assert _request(control) is None
    assert json.loads((control / "request.json.withdrawn").read_text())["id"] == rid
    [r] = _reports()
    assert (r.kind, r.outcome, r.request_id) == ("automatic", "skipped", rid)
    assert "withdrawn" in r.detail
    p = _policy()
    assert p.awaiting_request_id is None and p.enabled is True


def test_a_withdrawn_schedule_is_marked_and_reported(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=5)
    assert _run(us.tick()) == "fired schedule"
    _age_request(control, hours=3)
    assert _run(us.tick()) == "stale request withdrawn"
    [r] = _reports()
    assert (r.kind, r.outcome) == ("scheduled", "skipped")

    async def states():
        async with AsyncSessionLocal() as db:
            return [s.state for s in (await db.execute(select(UpdateSchedule))).scalars()]
    assert _run(states()) == ["withdrawn"]


def test_a_request_within_the_grace_is_left_for_the_sidecar(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=5)
    _run(us.tick())
    _age_request(control, hours=1)
    assert _run(us.tick()) != "stale request withdrawn"
    assert _request(control) is not None
    assert _reports() == []


def test_a_stale_request_someone_else_wrote_is_left_alone(control):
    """An admin's own click was watched; it is theirs to deal with."""
    _beat(control, current_tag="v1.2.0")
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat(timespec="seconds")
    (control / "request.json").write_text(json.dumps(
        {"id": "manual-1", "action": "update", "tag": "v1.3.0",
         "requested_by": 7, "requested_at": old.replace("+00:00", "Z")}))
    _run(us.tick())
    assert _request(control)["id"] == "manual-1"
    assert _reports() == []
