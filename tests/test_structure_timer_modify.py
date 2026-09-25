"""Who may edit or delete a structure timer.

The creator and app admins/managers always may. An in-game Director or CEO may
too, but only for timers they can see: a timer restricted to an ACL group they
are not in stays out of their reach.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.esi.client as esi_client
from app.db.models import (
    Base, Character, StructureTimer, TimerACLEntry, TimerACLGroup, User,
)
from app.routes.structure_timers import _can_modify_timer

CREATOR, DIRECTOR, MANAGER = 1, 2, 3
DIRECTOR_CHAR, DIRECTOR_CORP = 91000002, 98000002


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as s:
            s.add_all([User(id=CREATOR), User(id=DIRECTOR), User(id=MANAGER, role="manager")])
            s.add(Character(
                character_id=DIRECTOR_CHAR, character_name="Director", user_id=DIRECTOR,
                corporation_id=DIRECTOR_CORP, access_token="a", refresh_token="r",
                token_expiry=datetime.utcnow() + timedelta(days=1),
                scopes="esi-characters.read_corporation_roles.v1"))
            s.add(TimerACLGroup(id=10, name="Their corp only", created_by=CREATOR))
            s.add(TimerACLEntry(group_id=10, entry_type="corporation", eve_id=98000999))
            s.add(TimerACLGroup(id=11, name="Includes the director's corp", created_by=CREATOR))
            s.add(TimerACLEntry(group_id=11, entry_type="corporation", eve_id=DIRECTOR_CORP))
            await s.commit()
    _run(seed())

    # The character holds Director in game.
    async def fake_refresh(char, db):
        return "token"

    class FakeClient:
        def __init__(self, token, db=None):
            pass

        async def get(self, path):
            return {"roles": ["Director"]}

    monkeypatch.setattr(esi_client, "refresh_token", fake_refresh)
    monkeypatch.setattr(esi_client, "ESIClient", FakeClient)
    yield SessionLocal
    _run(engine.dispose())


def _timer(acl_group_id):
    return StructureTimer(
        id=1, structure_name="X", structure_type="astrahus", system_name="Jita",
        owner_name="Someone", disposition="hostile", timer_phase="armor",
        timer_expires=datetime.utcnow() + timedelta(hours=1),
        acl_group_id=acl_group_id, created_by=CREATOR)


def _can(SessionLocal, user_id, timer):
    async def go():
        async with SessionLocal() as s:
            return await _can_modify_timer(s, user_id, timer)
    return _run(go())


def test_director_cannot_modify_a_timer_hidden_from_them(db):
    assert _can(db, DIRECTOR, _timer(acl_group_id=10)) is False


def test_director_can_modify_a_timer_their_group_sees(db):
    assert _can(db, DIRECTOR, _timer(acl_group_id=11)) is True


def test_director_can_modify_an_unrestricted_timer(db):
    assert _can(db, DIRECTOR, _timer(acl_group_id=None)) is True


def test_creator_and_manager_keep_their_access_to_restricted_timers(db):
    assert _can(db, CREATOR, _timer(acl_group_id=10)) is True
    assert _can(db, MANAGER, _timer(acl_group_id=10)) is True
