"""ISS-046: the character page's mail list is read live from ESI.

The list used to come from CharacterDashboardCache.mail_json, which nothing
wrote any more, so most characters showed mail frozen for months. The route
now asks ESI on every load and renders every failure inside the panel.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.routes.character_detail as cd
from app.db.cache import TTL, _ttl_for_path
from app.db.models import CharacterDashboardCache
from app.esi.client import TokenRevoked
from app.esi.scope_guard import ScopeNotGranted
from tests.test_permissions_flow import ALT_ID, MAIN_ID, STRANGER_ID, env  # noqa: F401

ALICE, BOB, NOBODY = 7001, 7002, 7003


def _hdr(mail_id, sender, subject, is_read=True, ts="2026-09-25T12:34:56Z"):
    return {"mail_id": mail_id, "from": sender, "subject": subject, "timestamp": ts,
            "is_read": is_read, "labels": [1], "recipients": []}


def _status_error(code):
    req = httpx.Request("POST", "https://esi.invalid/universe/names/")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


class FakeESI:
    """Stands in for the ESIClient get_client_safe() returns."""

    def __init__(self, headers=(), names=None, raises=None, names_raise=None, names_delay=0):
        self.headers, self.names = list(headers), names or {}
        self.raises, self.names_raise, self.names_delay = raises, names_raise, names_delay
        self.cache_enabled = False
        self.gets, self.name_calls = [], []

    async def get(self, path, params=None, bypass_cache=False):
        self.gets.append(path)
        if self.raises:
            raise self.raises
        return self.headers

    async def post_public(self, path, body):
        self.name_calls.append(list(body))
        if self.names_delay:
            await asyncio.sleep(self.names_delay)
        if self.names_raise:
            raise self.names_raise
        if any(i not in self.names for i in body):   # ESI: one bad id fails the batch
            raise _status_error(404)
        return [{"id": i, "name": self.names[i], "category": "character"} for i in body]


@pytest.fixture
def esi(monkeypatch):
    cd._unresolvable_senders.clear()

    def install(fake=None, client_raises=None):
        async def fake_get_client_safe(char):
            if client_raises:
                raise client_raises
            return fake
        monkeypatch.setattr(cd, "get_client_safe", fake_get_client_safe)
        return fake

    yield install
    cd._unresolvable_senders.clear()


def _panel(env, cid=MAIN_ID):
    return env.user().get(f"/character/{cid}/mail-partial")


# ── Who may see what ────────────────────────────────────────────────────────

def test_someone_elses_mail_is_404_and_never_fetched(env, esi):
    fake = esi(FakeESI([_hdr(1, ALICE, "Secret")]))
    r = _panel(env, STRANGER_ID)
    assert r.status_code == 404
    assert fake.gets == []


def test_declined_mail_shows_the_permission_notice_and_is_never_fetched(env, esi):
    fake = esi(FakeESI([_hdr(1, ALICE, "Secret")]))
    r = _panel(env, ALT_ID)
    assert r.status_code == 200
    assert "perm-notice" in r.text and "Secret" not in r.text
    assert fake.gets == []


# ── The list itself ─────────────────────────────────────────────────────────

def test_mail_is_read_live_with_sender_names(env, esi):
    fake = esi(FakeESI([_hdr(2, ALICE, "Fleet tonight", is_read=False), _hdr(1, BOB, "Contract")],
                       names={ALICE: "Alice Pilot", BOB: "Bob Hauler"}))
    r = _panel(env)
    assert r.status_code == 200
    assert fake.gets == [f"/characters/{MAIN_ID}/mail/"]
    assert fake.cache_enabled is True            # goes through the ESI response cache
    assert fake.name_calls == [[ALICE, BOB]]     # one bulk lookup
    for text in ("Fleet tonight", "Contract", "Alice Pilot", "Bob Hauler", "2026-09-25 12:34"):
        assert text in r.text
    assert r.text.count(">NEW<") == 1


def test_leftover_synced_mail_is_ignored(env, esi):
    async def seed(db):
        db.add(CharacterDashboardCache(character_id=MAIN_ID, mail_json=json.dumps(
            {"headers": [_hdr(9, ALICE, "Frozen in April", is_read=False)], "unread_count": 1})))
        await db.commit()
    env.q(seed)
    esi(FakeESI([_hdr(10, ALICE, "Fresh today")], names={ALICE: "Alice Pilot"}))
    r = _panel(env)
    assert "Fresh today" in r.text and "Frozen in April" not in r.text


def test_empty_mailbox(env, esi):
    esi(FakeESI([]))
    assert "No recent mail" in _panel(env).text


def test_mail_without_is_read_is_not_badged_new(env, esi):
    sent = _hdr(1, ALICE, "Sent by me")
    del sent["is_read"]
    esi(FakeESI([sent], names={ALICE: "Alice Pilot"}))
    assert ">NEW<" not in _panel(env).text


def test_subjects_and_sender_names_are_escaped(env, esi):
    esi(FakeESI([_hdr(1, ALICE, "<script>alert(1)</script>")], names={ALICE: "<b>Alice</b>"}))
    r = _panel(env)
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
    assert "<b>Alice</b>" not in r.text


# ── Sender names are best effort ────────────────────────────────────────────

def test_one_unnameable_sender_does_not_hide_the_others(env, esi):
    fake = esi(FakeESI([_hdr(2, ALICE, "Named"), _hdr(1, NOBODY, "From a gone account")],
                       names={ALICE: "Alice Pilot"}))
    r = _panel(env)
    assert "Alice Pilot" in r.text and "From a gone account" in r.text
    assert cd._unresolvable_senders == {NOBODY}
    # Next load leaves the known-bad id out, so the bulk call succeeds.
    fake.name_calls.clear()
    _panel(env)
    assert fake.name_calls == [[ALICE]]


def test_names_outage_still_lists_mail_without_retrying_each_id(env, esi):
    fake = esi(FakeESI([_hdr(1, ALICE, "Still listed")], names_raise=httpx.ConnectError("down")))
    r = _panel(env)
    assert "Still listed" in r.text
    assert len(fake.name_calls) == 1
    assert cd._unresolvable_senders == set()     # an outage is not "unknown id"


def test_slow_name_lookup_does_not_hold_up_the_list(env, esi, monkeypatch):
    monkeypatch.setattr(cd, "_MAIL_NAMES_TIMEOUT", 0.05)
    esi(FakeESI([_hdr(1, ALICE, "On time")], names={ALICE: "Alice Pilot"}, names_delay=2))
    r = _panel(env)
    assert "On time" in r.text and "Alice Pilot" not in r.text


# ── Failures render inside the panel with a 200 ─────────────────────────────

def test_dead_token_says_renew(env, esi):
    esi(client_raises=TokenRevoked("SSO returned 400"))
    r = _panel(env)
    assert r.status_code == 200
    assert "no longer accepts" in r.text and f'href="/account/permissions/{MAIN_ID}"' in r.text


def test_token_without_the_mail_scope_says_review(env, esi):
    esi(FakeESI(raises=ScopeNotGranted("GET", "/characters/1/mail/", "esi-mail.read_mail.v1")))
    r = _panel(env)
    assert r.status_code == 200
    assert f"/account/permissions/{MAIN_ID}?add=mail" in r.text


def test_esi_failure_offers_a_retry_into_the_panel(env, esi):
    esi(FakeESI(raises=httpx.ReadTimeout("slow")))
    r = _panel(env)
    assert r.status_code == 200
    assert "Couldn&#39;t load mail" in r.text or "Couldn't load mail" in r.text
    assert f'hx-get="/character/{MAIN_ID}/mail-partial"' in r.text
    assert 'hx-target="#mail-panel"' in r.text
    page = Path("app/templates/character_detail.html").read_text()
    assert 'id="mail-panel"' in page             # the retry's target exists


# ── Cache lifetime ──────────────────────────────────────────────────────────

def test_mail_list_is_cached_for_esis_own_max_age():
    assert _ttl_for_path("/characters/123/mail/") == TTL["character_mail"] == 30
    assert _ttl_for_path("/characters/123/mail/456/") == 300          # bodies keep the default
    assert _ttl_for_path("/characters/123/killmails/recent/") == TTL["killmail"]
