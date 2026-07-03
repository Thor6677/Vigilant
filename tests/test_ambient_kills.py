"""Tests the recent-kill query window in isolation (in-memory SQLite).

The app has import-time side effects (DB/SDE init), so we test the
extracted query function, not the FastAPI route object.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import Base, Killmail
from app.routes.ambient import _recent_kill_systems


@pytest.fixture()
def session_factory():
    # Python 3.14 removed the implicit auto-created event loop that
    # asyncio.get_event_loop() used to lazily vivify per-thread. Create one
    # explicitly and register it as "current" so the get_event_loop() calls
    # in the test bodies below (kept sync-style, no pytest-asyncio marks)
    # resolve to this same loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Killmail.__table__.create(c))

    loop.run_until_complete(_init())
    return async_sessionmaker(engine, expire_on_commit=False)


def _km(system_id: int, age_s: int) -> Killmail:
    return Killmail(
        killmail_id=hash((system_id, age_s)) % 10**9,
        killmail_hash="deadbeef",
        killmail_time=datetime.utcnow() - timedelta(seconds=age_s),
        solar_system_id=system_id,
        victim_ship_type_id=670,
    )


def test_empty_table_returns_empty(session_factory):
    async def run():
        async with session_factory() as s:
            return await _recent_kill_systems(s, window_s=120)
    assert asyncio.get_event_loop().run_until_complete(run()) == []


def test_recent_kill_included_stale_excluded(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(30000142, age_s=30))    # Jita, fresh
            s.add(_km(30002187, age_s=600))   # Amarr, stale
            await s.commit()
            return await _recent_kill_systems(s, window_s=120)
    result = asyncio.get_event_loop().run_until_complete(run())
    assert result == [30000142]


def test_distinct_systems(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(30000142, age_s=10))
            s.add(_km(30000142, age_s=20))
            await s.commit()
            return await _recent_kill_systems(s, window_s=120)
    result = asyncio.get_event_loop().run_until_complete(run())
    assert result == [30000142]
