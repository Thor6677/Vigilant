"""Tests for T-072: the merged nav row and info bar on character pages.

Coverage:
1. Each of the six character-page templates (overview, skills, fittings,
   blueprints, journal, mining) renders exactly one tab strip, with a
   Stats link and an external zKillboard link (target=_blank rel=noopener)
   appended once, and no separate chip row (the old per-page entity_links
   call) alongside it.
2. The overview's info bar renders real values when the data is
   available, and "—" for a field whose scope is missing.

Sub-page routes return before any ESI call when the character lacks the
scope that page needs, so seeding characters with no scopes at all is
enough to exercise their nav row cheaply and hermetically. The overview
always fans out (there is no early return), so its test seeds real data
directly into the hermetic conftest DB — the same engine get_db and the
route's own internal AsyncSessionLocal() calls both already point at —
and stubs the ESI-facing calls instead of hitting the network.
"""
import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient

import app.routes.character_detail as cd
from app.db.models import AsyncSessionLocal, Character, CharacterDashboardCache, User

USER_A = 9301
CHAR_OVERVIEW = 97900001       # has skills scope, not implants
CHAR_NO_SCOPES = 97900002      # nothing shared — every info-bar field "—"
CHAR_SKILLS = 97900003
CHAR_FITTINGS = 97900004
CHAR_BLUEPRINTS = 97900005
CHAR_JOURNAL = 97900006
CHAR_MINING = 97900007

_CSRF = "test-csrf-token-navbar-0123456789"


def _client():
    import app.main as main
    return TestClient(main.app, base_url="https://testserver")


def _authed_client(user_id=USER_A):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = _client()
    client.cookies.set("vigilant_session", cookie)
    return client


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _mk_char(cid, scopes="", security_status=None, birthday=None):
    return Character(
        character_id=cid, character_name=f"Pilot {cid}", user_id=USER_A,
        access_token="x", refresh_token="x",
        token_expiry=datetime(2099, 1, 1), scopes=scopes,
        security_status=security_status, birthday=birthday,
    )


@pytest.fixture(scope="module", autouse=True)
def _seed_characters():
    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(User(id=USER_A, role="user"))
            db.add(_mk_char(CHAR_OVERVIEW, scopes="esi-skills.read_skills.v1",
                             security_status=-1.23, birthday=datetime(2020, 1, 1)))
            db.add(_mk_char(CHAR_NO_SCOPES, scopes="", security_status=None,
                             birthday=datetime(2020, 1, 1)))
            db.add(_mk_char(CHAR_SKILLS))
            db.add(_mk_char(CHAR_FITTINGS))
            db.add(_mk_char(CHAR_BLUEPRINTS))
            db.add(_mk_char(CHAR_JOURNAL))
            db.add(_mk_char(CHAR_MINING))
            db.add(CharacterDashboardCache(
                character_id=CHAR_OVERVIEW, wallet=0.0,
                last_synced=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5),
            ))
            await db.commit()
    _run(_seed())


class _FakeESI:
    """Stands in for ESIClient — only the skills endpoint returns data;
    everything else (implants, corp history, names) answers empty/None so
    the overview route's fan-out has nothing real to reach for.
    """
    def __init__(self, *args, **kwargs):
        self.cache_enabled = False

    async def get(self, path, params=None, bypass_cache=False):
        if path.endswith("/skills/"):
            return {
                "skills": [{"skill_id": 1, "skillpoints_in_skill": 1_000_000}],
                "unallocated_sp": 54321,
                "total_sp": 1_000_000,
            }
        return None

    async def get_public(self, path, bypass_cache=False):
        return []

    async def post_public(self, path, body):
        return []


async def _fake_refresh_token(char, db):
    return "fake-token"


@pytest.fixture(autouse=True)
def _stub_esi(monkeypatch):
    monkeypatch.setattr(cd, "ESIClient", _FakeESI)
    monkeypatch.setattr(cd, "refresh_token", _fake_refresh_token)
    monkeypatch.setattr("app.esi.client.ESIClient", _FakeESI)
    monkeypatch.setattr("app.esi.client.refresh_token", _fake_refresh_token)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _assert_single_nav_row(html: str, cid: int):
    # One desktop tab strip plus (mobile R1) one phone dropdown with the same
    # links. Count inside the strip so a duplicate row is still caught.
    assert html.count('class="b-tab-strip m-tabs-desktop"') == 1
    assert html.count('<details class="m-tabs">') == 1
    strip = html.split('class="b-tab-strip m-tabs-desktop"', 1)[1].split('<details class="m-tabs">', 1)[0]
    assert strip.count(f'href="/intel/entity/character/{cid}"') == 1
    assert len(re.findall(r'href="(https://zkillboard\.com/character/%d/)"' % cid, strip)) == 1
    # Across the page: exactly the strip copy and the dropdown copy.
    assert html.count(f'href="/intel/entity/character/{cid}"') == 2
    assert len(re.findall(r'href="(https://zkillboard\.com/character/%d/)"' % cid, html)) == 2
    # No leftover chip row from the old per-page entity_links() call.
    assert "el-chips" not in html
    # zKillboard opens in a new tab, safely (checked on the strip's link).
    zkb_idx = strip.index("zkillboard.com/character")
    tag_start = strip.rfind("<a ", 0, zkb_idx)
    tag_end = strip.index(">", zkb_idx)
    tag = strip[tag_start:tag_end]
    assert 'target="_blank"' in tag
    assert 'rel="noopener"' in tag


# ── 1. Nav row — one per page, on every character page ──────────────────────

def test_overview_nav_row():
    r = _authed_client().get(f"/character/{CHAR_OVERVIEW}")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_OVERVIEW)


def test_skills_nav_row():
    r = _authed_client().get(f"/character/{CHAR_SKILLS}/skills")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_SKILLS)


def test_fittings_nav_row():
    r = _authed_client().get(f"/character/{CHAR_FITTINGS}/fittings")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_FITTINGS)


def test_blueprints_nav_row():
    r = _authed_client().get(f"/character/{CHAR_BLUEPRINTS}/blueprints")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_BLUEPRINTS)


def test_journal_nav_row():
    r = _authed_client().get(f"/character/{CHAR_JOURNAL}/journal")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_JOURNAL)


def test_mining_nav_row():
    r = _authed_client().get(f"/character/{CHAR_MINING}/mining")
    assert r.status_code == 200
    _assert_single_nav_row(r.text, CHAR_MINING)


# ── 2. Info bar ───────────────────────────────────────────────────────────────

def test_info_bar_renders_available_values():
    r = _authed_client().get(f"/character/{CHAR_OVERVIEW}")
    assert r.status_code == 200
    html = r.text
    assert "1,000,000" in html          # Total SP
    assert "54,321" in html             # Unallocated SP
    assert "-1.23" in html              # Security status
    assert "Total SP" in html and "Unallocated SP" in html and "Last Synced" in html


def test_info_bar_shows_dash_when_scope_missing():
    r = _authed_client().get(f"/character/{CHAR_NO_SCOPES}")
    assert r.status_code == 200
    html = r.text
    # Isolate the info bar block so this doesn't accidentally match a dash
    # rendered somewhere else on a data-heavy page.
    start = html.index("Total SP")
    bar = html[start:start + 800]
    assert bar.count("—") >= 3   # Total SP, Unallocated SP, Implants at least
