"""Route-level tests for /tools/skill-farm (T-073) — auth gating and the IDOR
boundary through the real HTTP endpoints (tests/test_skill_farm_pilots.py
covers the same boundary at the service-function level). Follows the
env-fixture idiom from tests/test_permissions_flow.py: a dependency-overridden
temp-file DB and a forged, signed session cookie carrying a CSRF token.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Character, SkillFarmPilot, User, get_db

USER_A = 701
USER_B = 702
CHAR_A = 90000301   # belongs to USER_A
CHAR_B = 90000302   # belongs to USER_B
CSRF = "test-csrf-token"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char(cid, user_id, name):
    return Character(
        character_id=cid, character_name=name, user_id=user_id,
        access_token="a", refresh_token="r", token_expiry=datetime(2099, 1, 1),
        scopes="",
    )


@pytest.fixture
def env():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A, role="user"))
            db.add(User(id=USER_B, role="user"))
            db.add(_char(CHAR_A, USER_A, "Alpha Pilot"))
            db.add(_char(CHAR_B, USER_B, "Bravo Pilot"))
            await db.commit()

    _run(seed())

    async def _override():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = _override

    # Prices call real market functions through ESIClient("") -- avoid real
    # network in tests by making every price lookup resolve to None (a
    # missing-price page render, which the templates handle explicitly).
    import app.skillfarm.prices as farm_prices

    async def _no_price(*a, **k):
        return None

    farm_prices.clear_price_cache()
    orig_get_hub_price = farm_prices.get_hub_price
    orig_get_plex_price = farm_prices.get_plex_price
    farm_prices.get_hub_price = _no_price
    farm_prices.get_plex_price = _no_price

    def client(session: dict | None = None):
        c = TestClient(main.app, base_url="https://testserver",
                       raise_server_exceptions=False, follow_redirects=False)
        if session is not None:
            signer = itsdangerous.TimestampSigner(main.settings.secret_key)
            data = base64.b64encode(json.dumps({"csrf_token": CSRF, **session}).encode())
            c.cookies.set("vigilant_session", signer.sign(data).decode())
            c.headers.update({"X-CSRF-Token": CSRF})
        return c

    def q(fn):
        async def go():
            async with SessionLocal() as db:
                return await fn(db)
        return _run(go())

    class Env:
        pass

    async def _all_pilots(db):
        rows = (await db.execute(select(SkillFarmPilot))).scalars().all()
        return list(rows)

    async def _pilot_for_character(db, character_id):
        row = (await db.execute(select(SkillFarmPilot).where(
            SkillFarmPilot.character_id == character_id
        ))).scalars().first()
        return row

    async def _pilot_by_id(db, pilot_id):
        row = (await db.execute(select(SkillFarmPilot).where(
            SkillFarmPilot.id == pilot_id
        ))).scalars().first()
        return row

    async def _settings_for(db, user_id):
        from app.db.models import SkillFarmSettings
        row = (await db.execute(select(SkillFarmSettings).where(
            SkillFarmSettings.user_id == user_id
        ))).scalars().first()
        return row

    e = Env()
    e.client, e.q = client, q
    e.user_a = lambda: client({"user_id": USER_A})
    e.user_b = lambda: client({"user_id": USER_B})
    e.all_pilots = lambda: q(_all_pilots)
    e.pilot_for_character = lambda cid: q(lambda db: _pilot_for_character(db, cid))
    e.pilot_by_id = lambda pid: q(lambda db: _pilot_by_id(db, pid))
    e.settings_for = lambda uid: q(lambda db: _settings_for(db, uid))
    yield e

    main.app.dependency_overrides.pop(get_db, None)
    farm_prices.get_hub_price = orig_get_hub_price
    farm_prices.get_plex_price = orig_get_plex_price
    farm_prices.clear_price_cache()
    _run(engine.dispose())


# ── Auth gating ──────────────────────────────────────────────────────────────

def test_page_redirects_anonymous_visitors(env):
    r = env.client().get("/tools/skill-farm")
    assert r.status_code in (302, 303, 307)


def test_page_opens_for_a_session(env):
    r = env.user_a().get("/tools/skill-farm")
    assert r.status_code == 200
    assert "Skill Farm" in r.text


def test_mutations_refuse_a_fully_anonymous_caller(env):
    """No session at all -> the CSRF middleware itself refuses first (403),
    same as every other htmx mutation in the app — this module's own
    user_id check never even gets a chance to answer."""
    anon = env.client()
    assert anon.post("/tools/skill-farm/settings",
                     data={"sales_tax_pct": 8, "plex_per_month": 500, "price_source": "sell"}
                     ).status_code == 403
    assert anon.post("/tools/skill-farm/pilots",
                     data={"character_id": CHAR_A}).status_code == 403
    assert anon.post("/tools/skill-farm/pilots/1/base-sp",
                     data={"base_sp": 6_000_000}).status_code == 403
    assert anon.delete("/tools/skill-farm/pilots/1").status_code == 403


def test_mutations_refuse_a_session_with_no_user(env):
    """Past CSRF (a session that carries a token but no user_id) so the
    handler's own gate is what actually answers — the T-047 idiom used by
    tests/test_route_auth_gating.py's _LOGIN_ONLY_POSTS."""
    no_user = env.client({})
    assert no_user.post("/tools/skill-farm/settings",
                        data={"sales_tax_pct": 8, "plex_per_month": 500, "price_source": "sell"}
                        ).status_code == 401
    assert no_user.post("/tools/skill-farm/pilots",
                        data={"character_id": CHAR_A}).status_code == 401
    assert no_user.post("/tools/skill-farm/pilots/1/base-sp",
                        data={"base_sp": 6_000_000}).status_code == 401
    assert no_user.delete("/tools/skill-farm/pilots/1").status_code == 401


# ── Happy path ───────────────────────────────────────────────────────────────

def test_add_pilot_for_own_character_then_remove(env):
    client = env.user_a()
    r = client.post("/tools/skill-farm/pilots", data={"character_id": CHAR_A, "base_sp": 5_500_000})
    assert r.status_code == 200
    assert "Alpha Pilot" in r.text
    assert "needs the skills permission" in r.text.lower()  # no scope shared yet

    pilot = env.pilot_for_character(CHAR_A)
    assert pilot is not None
    assert pilot.base_sp == 5_500_000

    r2 = client.delete(f"/tools/skill-farm/pilots/{pilot.id}")
    assert r2.status_code == 200
    # Back to no pilots (the character now shows up again in the "add a farm
    # pilot" select instead, which legitimately still names it).
    assert "No farm pilots added yet" in r2.text
    assert env.pilot_for_character(CHAR_A) is None


def test_settings_form_saves(env):
    client = env.user_a()
    r = client.post("/tools/skill-farm/settings",
                    data={"sales_tax_pct": 4.5, "plex_per_month": 400, "price_source": "buy"})
    assert r.status_code == 200
    row = env.settings_for(USER_A)
    assert row.sales_tax_pct == 4.5
    assert row.plex_per_month == 400
    assert row.price_source == "buy"


def test_settings_form_rejects_out_of_range_tax(env):
    client = env.user_a()
    r = client.post("/tools/skill-farm/settings",
                    data={"sales_tax_pct": 250, "plex_per_month": 500, "price_source": "sell"})
    assert r.status_code == 200
    assert "must be between" in r.text.lower()


# ── IDOR ─────────────────────────────────────────────────────────────────────

def test_cannot_add_someone_elses_character_as_a_pilot(env):
    """IDOR fails closed with 404 (the v1.7.0 ownership convention) rather
    than a 200 + message — htmx never swaps a non-2xx, and this path is only
    ever reached by a crafted request, never the real UI."""
    r = env.user_a().post("/tools/skill-farm/pilots", data={"character_id": CHAR_B})
    assert r.status_code == 404
    assert env.all_pilots() == []


def test_cannot_edit_someone_elses_pilot_row(env):
    env.user_a().post("/tools/skill-farm/pilots", data={"character_id": CHAR_A})
    pilot = env.pilot_for_character(CHAR_A)

    r = env.user_b().post(f"/tools/skill-farm/pilots/{pilot.id}/base-sp",
                          data={"base_sp": 999_000_000})
    assert r.status_code == 404

    reread = env.pilot_by_id(pilot.id)
    assert reread.base_sp != 999_000_000


def test_cannot_remove_someone_elses_pilot_row(env):
    env.user_a().post("/tools/skill-farm/pilots", data={"character_id": CHAR_A})
    pilot = env.pilot_for_character(CHAR_A)

    r = env.user_b().delete(f"/tools/skill-farm/pilots/{pilot.id}")
    assert r.status_code == 404

    still_there = env.pilot_by_id(pilot.id)
    assert still_there is not None


# ── Full "ok" row: synced skills + skillqueue + real prices ────────────────

def test_a_fully_synced_training_pilot_renders_every_numeric_column(env, monkeypatch):
    """The riskiest render path: every template field that only appears for
    status == "ok" (total/unallocated/base SP, ready-now + ISK, SP/hour,
    ETA, profit — including a NEGATIVE profit's styling branch) actually
    renders without a Jinja error, against real numbers end to end."""
    import json as _json
    from datetime import timedelta, timezone as _tz
    from app.auth import scopes as perms
    from app.db.models import CharacterDashboardCache
    import app.skillfarm.prices as farm_prices

    now = datetime.now(_tz.utc)
    start = now - timedelta(hours=1)
    finish = now + timedelta(hours=1)
    skillqueue = [{
        "skill_id": 3300, "finished_level": 4,
        "start_date": start.isoformat().replace("+00:00", "Z"),
        "finish_date": finish.isoformat().replace("+00:00", "Z"),
        "training_start_sp": 0, "level_end_sp": 200_000,
    }]
    skills_payload = {"total_sp": 6_000_000, "unallocated_sp": 500_000, "levels": {"3300": 4}}

    async def seed(db):
        char = (await db.execute(select(Character).where(
            Character.character_id == CHAR_A
        ))).scalar_one()
        char.scopes = " ".join([perms.SKILLS, perms.SKILLQUEUE])
        db.add(CharacterDashboardCache(
            character_id=CHAR_A,
            skills_json=_json.dumps(skills_payload),
            skillqueue_json=_json.dumps(skillqueue),
        ))
        await db.commit()

    env.q(seed)
    env.user_a().post("/tools/skill-farm/pilots", data={"character_id": CHAR_A, "base_sp": 5_000_000})

    # Make PLEX expensive enough to force a NEGATIVE monthly profit — the
    # branch that must not crash the sf-loss/sf-profit class expression.
    async def fake_hub_price(client, hub_key, type_id, source):
        from app.skillfarm.constants import LARGE_SKILL_INJECTOR_TYPE_ID
        return 800_000_000.0 if type_id == LARGE_SKILL_INJECTOR_TYPE_ID else 600_000_000.0

    async def fake_plex_price(client, source):
        return 500_000_000.0  # absurdly expensive PLEX -> guaranteed loss

    monkeypatch.setattr(farm_prices, "get_hub_price", fake_hub_price)
    monkeypatch.setattr(farm_prices, "get_plex_price", fake_plex_price)

    r = env.user_a().get("/tools/skill-farm")
    assert r.status_code == 200
    assert "6,000,000" in r.text        # total SP
    assert "500,000" in r.text          # unallocated SP
    assert "sf-loss" in r.text          # negative profit styling actually rendered
    assert "not currently training" not in r.text.lower()
