"""Hub comparison preferences: defaults, save/load, validation."""
import asyncio
import tempfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User
from app.market import hub_prefs

USER_ID = 91


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def session_factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run(setup())
    yield factory
    _run(engine.dispose())


def test_defaults_when_no_row(session_factory):
    async def go():
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    prefs = _run(go())
    assert prefs.sales_tax_pct == pytest.approx(3.37)
    assert prefs.broker_fee_pct == pytest.approx(1.5)
    assert prefs.origin_system_id is None


def test_save_then_load_and_update_in_place(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 4.5, 1.0, 30000142)
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 2.0, 0.5, None)
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    prefs = _run(go())
    assert (prefs.sales_tax_pct, prefs.broker_fee_pct, prefs.origin_system_id) == (2.0, 0.5, None)


@pytest.mark.parametrize("raw, default, expected", [
    ("4.5", 3.37, 4.5), (150, 3.37, 100.0), (-2, 3.37, 0.0),
    ("abc", 3.37, 3.37), (None, 1.5, 1.5), ("nan", 1.5, 1.5), ("", 1.5, 1.5),
])
def test_clamp_pct(raw, default, expected):
    assert hub_prefs.clamp_pct(raw, default) == pytest.approx(expected)


def test_scan_defaults_without_a_row(session_factory):
    async def go():
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    p = _run(go())
    assert p.scan_budget_isk == 500_000_000
    assert p.market_share_pct == 20.0


def test_save_scan_prefs_clamps_and_keeps_fees(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 8, 2, 30099001)
            await hub_prefs.save_scan_prefs(db, USER_ID, 5, 250)
            low = await hub_prefs.get_prefs(db, USER_ID)
            await hub_prefs.save_scan_prefs(db, USER_ID, 9e12, 0)
            high = await hub_prefs.get_prefs(db, USER_ID)
            return low, high
    low, high = _run(go())
    assert (low.scan_budget_isk, low.market_share_pct) == (1_000_000, 100.0)
    assert (high.scan_budget_isk, high.market_share_pct) == (100_000_000_000, 1.0)
    assert (high.sales_tax_pct, high.broker_fee_pct, high.origin_system_id) == (8, 2, 30099001)


def test_save_prefs_keeps_scan_settings(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_scan_prefs(db, USER_ID, 2e9, 35)
            await hub_prefs.save_prefs(db, USER_ID, 4, 1, None)
            return await hub_prefs.get_prefs(db, USER_ID)
    p = _run(go())
    assert (p.scan_budget_isk, p.market_share_pct) == (2_000_000_000, 35.0)
