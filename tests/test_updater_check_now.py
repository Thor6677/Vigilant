"""The "Check now" button on Admin > Updates (T-089).

It runs the SAME check_once() the hourly loop runs, so the announce-once rule
holds: a release that was already announced is not announced again. GitHub and
Discord are replaced by stubs; the database is the temp one from the
test_updater_routes fixture.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.routes.admin as admin_mod
from app.db.models import UpdateStatus
from app.ops import update_check
from tests.test_updater_routes import _run, env  # noqa: F401  (fixture)

TAG = "v9.9.9"


@pytest.fixture
def check(env, monkeypatch):  # noqa: F811
    """check_once() wired to the temp DB, a stub GitHub and a stub Discord."""
    admin_mod._last_manual_check = 0.0
    calls = SimpleNamespace(fetches=0, alerts=[], release={
        "tag_name": TAG, "html_url": "https://example.test/rel", "body": "notes",
        "published_at": "2026-10-30T10:00:00Z"}, fail=None)

    async def fetch(repo):
        calls.fetches += 1
        if calls.fail:
            raise RuntimeError(calls.fail)
        return calls.release

    async def alert(**kw):
        calls.alerts.append(kw["key"])

    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    monkeypatch.setattr(update_check, "send_discord_alert", alert)
    monkeypatch.setattr(update_check, "AsyncSessionLocal", env._sessionmaker)
    monkeypatch.setattr(update_check, "get_settings", lambda: SimpleNamespace(
        version="v1.0.0", update_check_repo="owner/repo", update_check_enabled=True))
    calls.env = env
    return calls


def _row(env):
    async def q():
        async with env._sessionmaker() as db:
            return (await db.execute(select(UpdateStatus))).scalar_one()
    return _run(q())


def _again():
    admin_mod._last_manual_check = 0.0


# ── gating ───────────────────────────────────────────────────────────────────

def test_the_button_is_admin_only(check):
    env = check.env
    assert env.anon().post("/admin/update/check").status_code in (302, 303, 401, 403)
    assert env.plain().post("/admin/update/check").status_code in (302, 303, 401, 403)
    assert check.fetches == 0


def test_the_post_requires_csrf(check):
    c = check.env.admin()
    c.headers.pop("X-CSRF-Token")
    assert c.post("/admin/update/check").status_code == 403
    assert check.fetches == 0


# ── behaviour ────────────────────────────────────────────────────────────────

def test_it_runs_the_check_and_shows_the_result(check):
    r = check.env.admin().post("/admin/update/check")
    assert r.status_code == 200 and check.fetches == 1
    assert TAG in r.text
    assert _row(check.env).latest_tag == TAG


def test_it_works_without_the_updater_sidecar(check):
    """The checker half does not need a heartbeat, so no 404 here."""
    assert check.env.admin().post("/admin/update/check").status_code == 200


def test_a_newer_release_is_announced_once_across_repeated_clicks(check):
    check.env.admin().post("/admin/update/check")
    _again()
    check.env.admin().post("/admin/update/check")
    assert check.alerts == [TAG]
    assert _row(check.env).notified_tag == TAG


def test_an_already_announced_release_is_not_announced_again(check):
    async def seed():
        async with check.env._sessionmaker() as db:
            row = (await db.execute(select(UpdateStatus))).scalar_one()
            row.notified_tag = TAG
            await db.commit()
    _run(seed())
    check.env.admin().post("/admin/update/check")
    assert check.alerts == []


def test_a_fast_second_click_does_not_hit_github_again(check):
    c = check.env.admin()
    c.post("/admin/update/check")
    r = c.post("/admin/update/check")
    assert r.status_code == 200 and check.fetches == 1
    assert "Checked a moment ago" in r.text


def test_a_failed_fetch_shows_the_error_and_keeps_the_page(check):
    check.fail = "boom"
    r = check.env.admin().post("/admin/update/check")
    assert r.status_code == 200
    assert "Last check error" in r.text and "boom" in r.text
    assert _row(check.env).last_error == "boom"


def test_it_refuses_when_the_check_is_turned_off(check, monkeypatch):
    monkeypatch.setattr(admin_mod, "get_settings",
                        lambda: SimpleNamespace(update_check_enabled=False))
    r = check.env.admin().post("/admin/update/check")
    assert r.status_code == 400 and "UPDATE_CHECK_ENABLED" in r.text
    assert check.fetches == 0


def test_the_panel_has_the_button_wired_to_the_panel_swap(check):
    r = check.env.admin().get("/admin/update/status")
    assert 'hx-post="/admin/update/check"' in r.text
    assert 'hx-target="#updater-panel"' in r.text and "Check now" in r.text
