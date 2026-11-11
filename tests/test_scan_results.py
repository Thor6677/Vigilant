"""Hub Scan results: computing rows, saving, loading, deleting, cleanup."""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketScanResult, MarketScanSummary, User
from app.esi.market import APPRAISAL_HUBS
from app.market import hub_cost, scan_results
from app.market.scan_results import Settings

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
SETTINGS = Settings(budget_isk=5e8, share_pct=10.0, sales_tax_pct=3.37, broker_fee_pct=1.5)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    fac = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with fac() as db:
            db.add_all([User(id=91), User(id=92)])
            await db.commit()

    _run(setup())
    yield fac
    _run(engine.dispose())


def _region(hub):
    return APPRAISAL_HUBS[hub]["region_id"]


def _summary(region, type_id, lowest, units, volume, *, history_at=NOW, orders_at=NOW):
    """30 days of history ending yesterday (relative to NOW) plus a one-level ladder."""
    days = [[(NOW.date() - timedelta(days=n)).isoformat(), volume, 100.0, 110.0]
            for n in range(30, 0, -1)]
    return MarketScanSummary(
        region_id=region, type_id=type_id, days_json=json.dumps(days),
        history_fetched_at=history_at.replace(tzinfo=None),
        ladder_json=json.dumps([[lowest, units]]), ladder_truncated=False,
        orders_fetched_at=orders_at.replace(tzinfo=None))


async def _compute(factory, type_ids, failed, bench):
    async with factory() as db:
        db.add(_summary(_region("jita"), 2001, 100.0, 1000, 5))
        db.add(_summary(_region("rens"), 2001, 160.0, 5, 5))
        await db.commit()
    async with factory() as db:
        summaries = await scan_results.load_summaries(db, type_ids)
    return scan_results.compute_rows(summaries, type_ids, {2001: "Sample Hull"},
                                     bench, SETTINGS, failed, NOW)


def test_rows_follow_the_rules_and_keep_the_order(factory, monkeypatch):
    calls = []
    real = scan_results.scan_rules.best_for_item

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(scan_results.scan_rules, "best_for_item", spy)
    settings = Settings(budget_isk=5e8, share_pct=20.0, sales_tax_pct=3.37, broker_fee_pct=1.5)
    monkeypatch.setattr(sys.modules[__name__], "SETTINGS", settings)
    rows = _run(_compute(factory, [2001, 2002, 2003], failed={2003}, bench={}))
    assert calls
    for _figs, _cost, budget, share, tax, broker in calls:
        assert budget == pytest.approx(5e8) and share == pytest.approx(0.20)
        assert tax == pytest.approx(0.0337) and broker == pytest.approx(0.015)
    assert [r["type_id"] for r in rows] == [2001, 2002, 2003]
    assert rows[0]["status"] == "ok" and (rows[0]["hub"], rows[0]["source"]) == ("rens", "haul")
    assert rows[0]["name"] == "Sample Hull" and rows[0]["copy_only"] is False
    assert rows[1]["status"] == "no_sales"
    assert rows[2] == {"type_id": 2003, "name": "2003", "status": "unavailable"}


def test_copy_only_benchmark_marks_the_row(factory):
    bench = {2001: hub_cost.Benchmark(50.0, 10, None, True)}
    rows = _run(_compute(factory, [2001], failed=set(), bench=bench))
    assert rows[0]["source"] == "build" and rows[0]["copy_only"] is True


def test_hubs_without_a_summary_are_none_not_missing(factory, monkeypatch):
    seen = {}
    real = scan_results.scan_rules.best_for_item

    def spy(figs, *a):
        seen.update(figs)
        return real(figs, *a)

    monkeypatch.setattr(scan_results.scan_rules, "best_for_item", spy)
    _run(_compute(factory, [2001], failed=set(), bench={}))
    assert set(seen) == set(APPRAISAL_HUBS)
    assert seen["jita"] is not None and seen["rens"] is not None
    assert seen["amarr"] is None and seen["dodixie"] is None and seen["hek"] is None


async def _save(db, user, label="New label", rows=None, kind="category", ref=10, settings=SETTINGS):
    await scan_results.save(
        db, user, kind, ref, label=label, status="complete", item_total=1, item_scanned=1,
        settings=settings, notes=["n"], routes={}, now=NOW,
        rows=rows if rows is not None else [{"type_id": 1, "status": "no_sales", "name": "x"}])


async def _save_twice_and_load(factory):
    async with factory() as db:
        await _save(db, 91, label="Old label", rows=[{"type_id": 9, "status": "no_cost", "name": "y"}])
    async with factory() as db:
        await _save(db, 91)
    async with factory() as db:
        mine = await scan_results.load(db, 91, "category", 10, NOW)
    async with factory() as db:
        other = await scan_results.load(db, 92, "category", 10, NOW)
    async with factory() as db:
        count = len((await db.execute(select(MarketScanResult))).scalars().all())
    async with factory() as db:
        other_kind = await scan_results.load(db, 91, "watchlist", 10, NOW)
        other_ref = await scan_results.load(db, 91, "category", 11, NOW)
    return mine, other, count, other_kind, other_ref


def test_save_replaces_and_load_is_per_user(factory):
    first, other, count, other_kind, other_ref = _run(_save_twice_and_load(factory))
    assert first["label"] == "New label"
    assert other is None and count == 1
    assert other_kind is None and other_ref is None
    assert first["rows"] == [{"type_id": 1, "status": "no_sales", "name": "x"}]
    assert first["settings"]["budget_isk"] == 5e8
    assert first["scanned_at"] == NOW and first["notes"] == ["n"]


def test_save_refuses_nan_and_infinity(factory):
    async def go(bad):
        async with factory() as db:
            await _save(db, 91, rows=[{"type_id": 1, "x": bad}])
        async with factory() as db:
            return await scan_results.load(db, 91, "category", 10)

    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _run(go(bad))
    # nothing half-written
    assert _run(_none_saved(factory))


async def _none_saved(factory):
    async with factory() as db:
        return (await db.execute(select(MarketScanResult))).scalars().first() is None


def test_load_stamps_opened_at(factory):
    later = NOW + timedelta(days=3)

    async def go():
        async with factory() as db:
            await _save(db, 91)
        async with factory() as db:
            await scan_results.load(db, 91, "category", 10, later)
        async with factory() as db:
            return (await db.execute(select(MarketScanResult))).scalar_one()

    row = _run(go())
    assert row.opened_at == later.replace(tzinfo=None)
    assert row.scanned_at == NOW.replace(tzinfo=None)


def test_load_stamp_is_skipped_within_the_hour_and_due_after(factory):
    async def opened():
        async with factory() as db:
            return (await db.execute(select(MarketScanResult))).scalar_one().opened_at

    async def go():
        async with factory() as db:
            await _save(db, 91)
        async with factory() as db:
            await scan_results.load(db, 91, "category", 10, NOW + timedelta(minutes=59))
        within = await opened()
        async with factory() as db:
            await scan_results.load(db, 91, "category", 10, NOW + timedelta(hours=2))
        return within, await opened()

    within, after = _run(go())
    assert within == NOW.replace(tzinfo=None)
    assert after == (NOW + timedelta(hours=2)).replace(tzinfo=None)


def test_failed_stamp_does_not_fail_the_read(factory, monkeypatch):
    async def go():
        async with factory() as db:
            await _save(db, 91)
        async with factory() as db:
            async def boom():
                raise RuntimeError("database is locked")

            real_commit = db.commit
            monkeypatch.setattr(db, "commit", boom)
            res = await scan_results.load(db, 91, "category", 10, NOW + timedelta(hours=3))
            monkeypatch.setattr(db, "commit", real_commit)
            again = (await db.execute(select(MarketScanResult))).scalar_one()
            return res, again.opened_at

    res, opened = _run(go())
    assert res is not None and res["label"] == "New label"
    assert opened == NOW.replace(tzinfo=None)


def test_save_is_an_upsert_even_when_the_row_was_never_loaded(factory):
    async def go():
        async with factory() as a, factory() as b:
            # Neither session has read the row: both would have taken the insert path.
            await _save(a, 91, label="First")
            await _save(b, 91, label="Second")
        async with factory() as db:
            return (await db.execute(select(MarketScanResult))).scalars().all()

    rows = _run(go())
    assert len(rows) == 1 and rows[0].label == "Second"


def test_naive_now_is_accepted_by_save(factory):
    async def go():
        async with factory() as db:
            await scan_results.save(
                db, 91, "category", 10, label="L", status="complete", item_total=1,
                item_scanned=1, settings=SETTINGS, notes=[], routes={}, rows=[],
                now=NOW.replace(tzinfo=None))
        async with factory() as db:
            return await scan_results.load(db, 91, "category", 10)

    assert _run(go())["scanned_at"] == NOW


def test_delete_for_removes_only_that_list(factory):
    async def go():
        async with factory() as db:
            await _save(db, 91, ref=10)
            await _save(db, 91, ref=11)
            await _save(db, 92, ref=10)
        async with factory() as db:
            await scan_results.delete_for(db, 91, "category", 10)
        out = []
        for user, ref in ((91, 10), (91, 11), (92, 10)):
            async with factory() as db:
                out.append(await scan_results.load(db, user, "category", ref) is not None)
        return out

    assert _run(go()) == [False, True, True]


async def _cleanup_counts(factory):
    old, hour = NOW - timedelta(days=3), NOW - timedelta(hours=1)
    async with factory() as db:
        db.add_all([
            _summary(1, 1, 100.0, 5, 5, history_at=old, orders_at=old),     # A: goes
            _summary(1, 2, 100.0, 5, 5, history_at=old, orders_at=hour),    # B: stays
            _summary(1, 3, 100.0, 5, 5, history_at=hour, orders_at=old),    # C: stays
        ])
        await db.commit()
    for kind, ref, idle in (("category", 10, 31), ("category", 11, 5), ("watchlist", 12, 90), ("blueprints", 13, 90)):
        async with factory() as db:
            await _save(db, 91, kind=kind, ref=ref)
        async with factory() as db:
            row = (await db.execute(select(MarketScanResult).where(
                MarketScanResult.list_ref == ref))).scalar_one()
            row.opened_at = (NOW - timedelta(days=idle)).replace(tzinfo=None)
            await db.commit()
    async with factory() as db:
        counts = await scan_results.cleanup(db, NOW)
    async with factory() as db:
        left = sorted(r.type_id for r in (await db.execute(select(MarketScanSummary))).scalars())
        refs = sorted(r.list_ref for r in (await db.execute(select(MarketScanResult))).scalars())
    assert left == [2, 3] and refs == [11, 12, 13]
    return counts


def test_cleanup_removes_only_old_summaries_and_idle_category_results(factory):
    assert _run(_cleanup_counts(factory)) == (1, 1)
