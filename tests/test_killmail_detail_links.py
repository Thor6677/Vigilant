"""The killmail detail panel links only the viewer's own characters inward.

An attacker who is one of the viewer's characters links to its Vigilant
character page. Every other attacker links to zKillboard — including characters
registered on this instance by someone else, whose registration is not the
viewer's to learn.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.intel_kills as intel_kills
from app.db.models import Base, Character, Killmail, KillmailAttacker, User, get_db
from tests.test_route_auth_gating import _client

VIEWER, OTHER = 7101, 7102
MINE, THEIRS, STRANGER = 93000001, 93000002, 93000003
KM = 555001


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char(cid, user_id):
    return Character(character_id=cid, character_name=f"Pilot {cid}", user_id=user_id,
                     access_token="a", refresh_token="r",
                     token_expiry=datetime.utcnow() + timedelta(days=1), scopes="")


@pytest.fixture
def detail(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add_all([User(id=VIEWER), User(id=OTHER), _char(MINE, VIEWER), _char(THEIRS, OTHER)])
            db.add(Killmail(killmail_id=KM, killmail_hash="h", killmail_time=datetime.utcnow(),
                            solar_system_id=30000142, victim_ship_type_id=587))
            for i, cid in enumerate((MINE, THEIRS, STRANGER)):
                db.add(KillmailAttacker(killmail_id=KM, character_id=cid, ship_type_id=587,
                                        damage_done=100 - i, final_blow=(i == 0)))
            await db.commit()
    _run(seed())

    async def override_get_db():
        async with SessionLocal() as s:
            yield s

    async def names(ids):
        return {i: f"Name{i}" for i in ids}

    monkeypatch.setattr(intel_kills, "resolve_entity_names", names)
    main.app.dependency_overrides[get_db] = override_get_db
    yield
    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


def test_only_the_viewers_own_characters_link_inward(detail):
    r = _client(user_id=VIEWER).get(f"/intel/kills/{KM}/detail")
    assert r.status_code == 200
    assert f'href="/characters/{MINE}"' in r.text
    assert f'href="/characters/{THEIRS}"' not in r.text
    assert f"zkillboard.com/character/{THEIRS}/" in r.text
    assert f"zkillboard.com/character/{STRANGER}/" in r.text


def test_the_panel_is_not_shared_between_viewers(detail):
    r = _client(user_id=VIEWER).get(f"/intel/kills/{KM}/detail")
    assert "private" in r.headers["cache-control"]
