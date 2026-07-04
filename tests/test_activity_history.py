# tests/test_activity_history.py
"""Tests _build_history_payload in isolation (in-memory SQLite),
sync-style per tests/test_ambient_kills.py."""
import asyncio
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import KillmailDailyAggregate, PlayerCountDailyAggregate
from app.routes.player_stats import _build_history_payload, _FIRST_PCU


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: PlayerCountDailyAggregate.__table__.create(c))
            await conn.run_sync(lambda c: KillmailDailyAggregate.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def _run(session_factory):
    async def run():
        async with session_factory() as s:
            return await _build_history_payload(s)
    return asyncio.get_event_loop().run_until_complete(run())


def test_arrays_parallel_and_span_full_timeline(session_factory):
    p = _run(session_factory)
    n = len(p["dates"])
    # UTC "today", NOT date.today() — local-tz today diverges from the
    # builder's UTC date every US evening and would flake this test.
    from datetime import datetime, timezone
    utc_today = datetime.now(timezone.utc).date()
    assert n == (utc_today - _FIRST_PCU.date()).days + 1
    assert all(len(p[k]) == n for k in ("pcu_avg", "pcu_peak", "kills", "isk"))
    assert p["dates"][0] == "2003-05-28"


def test_values_land_on_their_dates(session_factory):
    async def seed():
        async with session_factory() as s:
            s.add(PlayerCountDailyAggregate(
                date=date(2010, 6, 15), source="eve-offline-net",
                avg_pc=41000, peak_pc=52000, sample_count=24))
            s.add(KillmailDailyAggregate(
                date=date(2010, 6, 15), source="zkb-totals",
                kill_count=18500, total_isk_destroyed=None))
            s.add(KillmailDailyAggregate(
                date=date(2020, 3, 3), source="vigilant",
                kill_count=22000, total_isk_destroyed=5.5e12))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(seed())
    p = _run(session_factory)
    i2010 = p["dates"].index("2010-06-15")
    i2020 = p["dates"].index("2020-03-03")
    assert p["pcu_avg"][i2010] == 41000 and p["pcu_peak"][i2010] == 52000
    assert p["kills"][i2010] == 18500 and p["isk"][i2010] is None
    assert p["kills"][i2020] == 22000 and p["isk"][i2020] == 5.5e12


def test_vigilant_beats_zkb_on_same_date(session_factory):
    async def seed():
        async with session_factory() as s:
            s.add(KillmailDailyAggregate(
                date=date(2026, 4, 1), source="zkb-totals",
                kill_count=100, total_isk_destroyed=None))
            s.add(KillmailDailyAggregate(
                date=date(2026, 4, 1), source="vigilant",
                kill_count=105, total_isk_destroyed=1.0e12))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(seed())
    p = _run(session_factory)
    i = p["dates"].index("2026-04-01")
    assert p["kills"][i] == 105 and p["isk"][i] == 1.0e12
