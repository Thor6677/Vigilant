"""Tests for completed-industry-job ingest (Industry P&L, T-041 item 2).

Pattern follows tests/test_networth.py: a real temp SQLite file (so the
`INSERT OR IGNORE` idempotency path executes against the actual SQLite
dialect) + create_all + async_sessionmaker, driven by a private event loop.
`_persist_completed_jobs` is exercised directly with fixture ESI job dicts
(no ESI/scope plumbing) per the plan's Task 3 Step 1 scenario.
"""
import asyncio
import tempfile
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, IndustryJobHistory, MarketHistory
from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial
from app.industry.manufacturing import calc_material
from app.routes.dashboard import _persist_completed_jobs

CHAR_ID = 90000042
JITA = 10000002


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _temp_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _job(job_id, blueprint_type_id, product_type_id, runs, activity_id=1,
          cost=0.0, completed_date="2026-07-01T12:00:00Z",
          start_date="2026-06-30T12:00:00Z", status="delivered"):
    return {
        "job_id": job_id,
        "activity_id": activity_id,
        "blueprint_type_id": blueprint_type_id,
        "product_type_id": product_type_id,
        "runs": runs,
        "cost": cost,
        "status": status,
        "completed_date": completed_date,
        "start_date": start_date,
    }


@pytest.fixture(autouse=True)
def _no_esi_price_map(monkeypatch):
    """`_persist_completed_jobs` falls back to the global reference-price map
    for materials with no history — patch it to a fixed dict so tests never
    touch the network. Individual tests override via monkeypatch again."""
    async def _fake(db):
        return {}
    monkeypatch.setattr("app.market.lp.get_price_map", _fake)
    return _fake


def test_persist_computes_output_qty_and_build_cost():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=999, product_type_id=44,
                                     product_quantity=10))
            db.add(SDEBlueprintMaterial(blueprint_type_id=999, activity_id=1,
                                         material_type_id=34, quantity=100))
            db.add(MarketHistory(region_id=JITA, type_id=34, date=date(2026, 7, 1),
                                  average=5.0, highest=6.0, lowest=4.0,
                                  volume=1000, order_count=10))
            await db.commit()

        async with SessionLocal() as db:
            job = _job(job_id=1, blueprint_type_id=999, product_type_id=44, runs=3)
            inserted = await _persist_completed_jobs(db, CHAR_ID, [job])
        assert inserted == 1

        async with SessionLocal() as db:
            row = (await db.execute(select(IndustryJobHistory))).scalar_one()
        assert row.job_id == 1
        assert row.output_qty == 30   # runs(3) x product_quantity(10)
        expected_qty = calc_material(100, 3, 10, 1.0, 0.0, 1.0)
        expected_cost = expected_qty * 5.0 + 0.0  # install_cost from job "cost"=0.0
        assert row.build_cost == pytest.approx(expected_cost)
        assert row.cost_basis == "history"

    _run(scenario())


def test_persist_is_idempotent_on_job_id():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=999, product_type_id=44,
                                     product_quantity=10))
            db.add(SDEBlueprintMaterial(blueprint_type_id=999, activity_id=1,
                                         material_type_id=34, quantity=100))
            db.add(MarketHistory(region_id=JITA, type_id=34, date=date(2026, 7, 1),
                                  average=5.0, highest=6.0, lowest=4.0,
                                  volume=1000, order_count=10))
            await db.commit()

        job = _job(job_id=1, blueprint_type_id=999, product_type_id=44, runs=3)
        async with SessionLocal() as db:
            first = await _persist_completed_jobs(db, CHAR_ID, [job])
        async with SessionLocal() as db:
            second = await _persist_completed_jobs(db, CHAR_ID, [job])
        assert first == 1
        assert second == 0

        async with SessionLocal() as db:
            count = len((await db.execute(select(IndustryJobHistory))).scalars().all())
        assert count == 1

    _run(scenario())


def test_unpriceable_material_stores_null_then_revalues_on_later_sync():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=998, product_type_id=46,
                                     product_quantity=1))
            db.add(SDEBlueprintMaterial(blueprint_type_id=998, activity_id=1,
                                         material_type_id=35, quantity=50))
            await db.commit()

        job = _job(job_id=2, blueprint_type_id=998, product_type_id=46, runs=1,
                    completed_date="2026-07-02T00:00:00Z",
                    start_date="2026-07-01T00:00:00Z")

        # First sync: no market history and no reference price for type 35
        # (the autouse fixture patches get_price_map -> {}) -> NULL cost.
        async with SessionLocal() as db:
            inserted = await _persist_completed_jobs(db, CHAR_ID, [job])
        assert inserted == 1
        async with SessionLocal() as db:
            row = (await db.execute(select(IndustryJobHistory))).scalar_one()
        assert row.build_cost is None
        assert row.cost_basis is None

        # Price history for type 35 arrives before the next sync tick.
        async with SessionLocal() as db:
            db.add(MarketHistory(region_id=JITA, type_id=35, date=date(2026, 7, 2),
                                  average=2.0, highest=3.0, lowest=1.0,
                                  volume=1000, order_count=10))
            await db.commit()

        # Next sync brings no NEW jobs, but must retry the NULL row.
        async with SessionLocal() as db:
            inserted_again = await _persist_completed_jobs(db, CHAR_ID, [])
        assert inserted_again == 0

        async with SessionLocal() as db:
            row = (await db.execute(select(IndustryJobHistory))).scalar_one()
        expected_qty = calc_material(50, 1, 10, 1.0, 0.0, 1.0)
        assert row.build_cost == pytest.approx(expected_qty * 2.0)
        assert row.cost_basis == "history"

    _run(scenario())


def test_materials_scoped_to_job_activity_id():
    """A blueprint_type_id can carry SDEBlueprintMaterial rows for multiple
    SDE activities under the same id (e.g. activity_id=1 manufacturing
    materials AND activity_id=8 invention datacores). Valuation must only
    sum the materials for the JOB's own activity — an unscoped lookup would
    fold unrelated (and here, unpriceable) datacore cost into a
    manufacturing job's build_cost, or make it NULL entirely."""
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=997, product_type_id=47,
                                     product_quantity=1))
            # Manufacturing materials (activity_id=1) — priced.
            db.add(SDEBlueprintMaterial(blueprint_type_id=997, activity_id=1,
                                         material_type_id=34, quantity=10))
            # Invention datacore under the SAME blueprint_type_id, different
            # activity, deliberately unpriced (no history, no reference).
            db.add(SDEBlueprintMaterial(blueprint_type_id=997, activity_id=8,
                                         material_type_id=9999, quantity=5))
            db.add(MarketHistory(region_id=JITA, type_id=34, date=date(2026, 7, 1),
                                  average=5.0, highest=6.0, lowest=4.0,
                                  volume=1000, order_count=10))
            await db.commit()

        job = _job(job_id=3, blueprint_type_id=997, product_type_id=47, runs=1,
                    activity_id=1)
        async with SessionLocal() as db:
            inserted = await _persist_completed_jobs(db, CHAR_ID, [job])
        assert inserted == 1

        async with SessionLocal() as db:
            row = (await db.execute(select(IndustryJobHistory))).scalar_one()
        expected_qty = calc_material(10, 1, 10, 1.0, 0.0, 1.0)
        assert row.build_cost == pytest.approx(expected_qty * 5.0)
        assert row.cost_basis == "history"

    _run(scenario())


def test_non_delivered_and_wrong_activity_jobs_are_ignored():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        jobs = [
            _job(job_id=10, blueprint_type_id=999, product_type_id=44, runs=1,
                 status="active"),
            _job(job_id=11, blueprint_type_id=999, product_type_id=44, runs=1,
                 activity_id=5, status="delivered"),  # copying, not tracked
        ]
        async with SessionLocal() as db:
            inserted = await _persist_completed_jobs(db, CHAR_ID, jobs)
        assert inserted == 0
        async with SessionLocal() as db:
            count = len((await db.execute(select(IndustryJobHistory))).scalars().all())
        assert count == 0

    _run(scenario())
