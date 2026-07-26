"""Tests the month-chunked ISK backfill in isolation (in-memory SQLite).

Same pattern as test_ambient_kills.py: sync-style tests, explicit event
loop, extracted functions exercised directly with an injected session
factory (the app module has import-time side effects).
"""
import asyncio
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import Killmail, KillmailDailyAggregate
from app.intel.killmail_isk_backfill import _month_range, backfill_month


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Killmail.__table__.create(c))
            await conn.run_sync(lambda c: KillmailDailyAggregate.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def _km(kid: int, when: datetime, isk: float) -> Killmail:
    return Killmail(
        killmail_id=kid, killmail_hash="deadbeef", killmail_time=when,
        solar_system_id=30000142, victim_ship_type_id=670, total_value=isk,
    )


def test_month_range_boundaries():
    months = list(_month_range(date(2016, 1, 1), date(2016, 3, 15)))
    assert months == [
        (date(2016, 1, 1), date(2016, 2, 1)),
        (date(2016, 2, 1), date(2016, 3, 1)),
        (date(2016, 3, 1), date(2016, 3, 15)),  # partial final month clamps
    ]


def test_backfill_month_aggregates_per_day(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(1, datetime(2020, 5, 1, 10, 0), 100.0))
            s.add(_km(2, datetime(2020, 5, 1, 12, 0), 50.0))
            s.add(_km(3, datetime(2020, 5, 2, 3, 0), 7.0))
            await s.commit()
        n = await backfill_month(session_factory, date(2020, 5, 1), date(2020, 6, 1))
        async with session_factory() as s:
            rows = (await s.execute(
                select(KillmailDailyAggregate).order_by(KillmailDailyAggregate.date)
            )).scalars().all()
        return n, rows
    n, rows = asyncio.get_event_loop().run_until_complete(run())
    assert n == 2
    assert (rows[0].date, rows[0].kill_count, rows[0].total_isk_destroyed) == (date(2020, 5, 1), 2, 150.0)
    assert (rows[1].date, rows[1].kill_count, rows[1].total_isk_destroyed) == (date(2020, 5, 2), 1, 7.0)
    assert all(r.source == "vigilant" for r in rows)


def test_backfill_month_skips_existing_dates(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(1, datetime(2020, 5, 1, 10, 0), 100.0))
            s.add(_km(2, datetime(2020, 5, 2, 10, 0), 30.0))
            # 2020-05-01 already rolled up (rollup owns it — must not change)
            s.add(KillmailDailyAggregate(
                date=date(2020, 5, 1), source="vigilant",
                kill_count=999, total_isk_destroyed=999.0))
            await s.commit()
        n = await backfill_month(session_factory, date(2020, 5, 1), date(2020, 6, 1))
        async with session_factory() as s:
            existing = (await s.execute(
                select(KillmailDailyAggregate).where(KillmailDailyAggregate.date == date(2020, 5, 1))
            )).scalars().one()
        return n, existing
    n, existing = asyncio.get_event_loop().run_until_complete(run())
    assert n == 1                      # only 05-02 inserted
    assert existing.kill_count == 999  # pre-existing row untouched


def test_backfill_month_fast_skip_when_fully_covered(session_factory):
    async def run():
        async with session_factory() as s:
            # every day of Feb 2020 already covered
            for day in range(1, 30):
                s.add(KillmailDailyAggregate(
                    date=date(2020, 2, day), source="vigilant",
                    kill_count=1, total_isk_destroyed=1.0))
            await s.commit()
        return await backfill_month(session_factory, date(2020, 2, 1), date(2020, 3, 1))
    n = asyncio.get_event_loop().run_until_complete(run())
    assert n == -1  # sentinel: fast-skipped, no aggregate query ran
