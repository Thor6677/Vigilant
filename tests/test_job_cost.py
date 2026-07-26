"""Job build-cost valuation (T-041 item 2). Pure math + history lookup.

DB tests follow tests/test_networth.py's pattern: temp SQLite file +
create_all + AsyncSessionLocal-style sessionmaker, run via a private loop.
"""
import asyncio
import tempfile
from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketHistory
from app.industry.job_cost import (
    ME_MANUFACTURING, job_build_cost, prices_at_bulk,
)


def test_job_build_cost_me_adjusted():
    # 100 base qty, 3 runs, ME 10, neutral facility → calc_material applies
    # ceil/floor rules from app.industry.manufacturing — mirror them here:
    from app.industry.manufacturing import calc_material
    mats = [{"type_id": 34, "quantity": 100}]
    expected_qty = calc_material(100, 3, 10, 1.0, 0.0, 1.0)
    cost, basis = job_build_cost(
        mats, runs=3, me=10, install_cost=500.0,
        price_fn=lambda tid: (5.0, "history"))
    assert cost == pytest.approx(expected_qty * 5.0 + 500.0)
    assert basis == "history"


def test_job_build_cost_unpriced_material_is_none():
    cost, basis = job_build_cost(
        [{"type_id": 34, "quantity": 10}], runs=1, me=0, install_cost=0.0,
        price_fn=lambda tid: (None, None))
    assert cost is None and basis is None


def test_job_build_cost_mixed_basis_reports_reference():
    # If ANY material fell back to reference pricing, the whole job's basis
    # is "reference" (the weaker claim wins).
    prices = {34: (5.0, "history"), 35: (2.0, "reference")}
    cost, basis = job_build_cost(
        [{"type_id": 34, "quantity": 1}, {"type_id": 35, "quantity": 1}],
        runs=1, me=0, install_cost=0.0,
        price_fn=lambda tid: prices[tid])
    assert cost is not None and basis == "reference"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_prices_at_bulk_nearest_prior_within_7d_and_fallback():
    async def _t():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(MarketHistory(region_id=10000002, type_id=34,
                                 date=date(2026, 7, 1), average=5.5,
                                 highest=6.0, lowest=5.0, volume=1000,
                                 order_count=10))
            db.add(MarketHistory(region_id=10000002, type_id=34,
                                 date=date(2026, 6, 20), average=4.0,
                                 highest=5.0, lowest=3.0, volume=1000,
                                 order_count=10))
            await db.commit()
            out = await prices_at_bulk(
                db, [34, 35], date(2026, 7, 3), reference_map={35: 9.0})
        assert out[34] == (5.5, "history")      # nearest ≤ 2026-07-03 within 7d
        assert out[35] == (9.0, "reference")    # no history → reference
    _run(_t())


def test_prices_at_bulk_stale_history_outside_window_loses_to_reference():
    # A type whose ONLY history row is older than the 7-day window must fall
    # back to the reference map — the stale row must NOT win. This isolates
    # the window exclusion: no newer row exists to mask it via ordering.
    async def _t():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(MarketHistory(region_id=10000002, type_id=34,
                                 date=date(2026, 6, 20), average=4.0,
                                 highest=5.0, lowest=3.0, volume=1000,
                                 order_count=10))
            await db.commit()
            out = await prices_at_bulk(
                db, [34], date(2026, 7, 3), reference_map={34: 7.0})
        # 2026-06-20 < cutoff 2026-06-26 → stale, reference price wins.
        assert out[34] == (7.0, "reference")
    _run(_t())
