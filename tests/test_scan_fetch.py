"""Hub Scan's compact market summaries: fetching, freshness, figures."""
import asyncio
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketHistory, MarketScanSummary
from app.market import history, scan_fetch
from app.market.hub_analysis import market_figures

REGION = 10000030
STATION = 60004588
TYPE = 2001
NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)


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

    _run(setup())
    yield fac
    _run(engine.dispose())


def raw_history(days=30, volume=5, average=330.0, highest=340.0):
    return [{"date": (NOW.date() - timedelta(days=i + 1)).isoformat(), "average": average,
             "highest": highest, "lowest": 320.0, "volume": volume, "order_count": 3}
            for i in range(days)]


def raw_orders():
    return [
        {"is_buy_order": False, "price": 331.0, "volume_remain": 14, "location_id": STATION},
        {"is_buy_order": False, "price": 331.0, "volume_remain": 6, "location_id": STATION},
        {"is_buy_order": False, "price": 400.0, "volume_remain": 9, "location_id": STATION},
        {"is_buy_order": False, "price": 300.0, "volume_remain": 5, "location_id": 60000001},
        {"is_buy_order": True, "price": 284.0, "volume_remain": 6, "location_id": STATION},
        {"is_buy_order": False, "price": 331.0, "volume_remain": 0, "location_id": STATION},
    ]


@pytest.fixture
def stubs(monkeypatch):
    calls = []

    async def fake_history(region_id, type_id):
        calls.append("history")
        return raw_history()

    async def fake_orders(region_id, type_id):
        calls.append("orders")
        return raw_orders()

    monkeypatch.setattr(scan_fetch.market_history, "_fetch_history_esi", fake_history)
    monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", fake_orders)
    monkeypatch.setattr(scan_fetch.market_history, "_now", lambda: NOW)
    return calls


def test_ladder_merges_prices_and_ignores_other_stations_buys_and_empties():
    levels, truncated = scan_fetch.ladder_from_orders(raw_orders(), STATION)
    assert levels == [[331.0, 20], [400.0, 9]] and truncated is False


def test_ladder_is_capped():
    many = [{"is_buy_order": False, "price": 100.0 + i, "volume_remain": 1,
             "location_id": STATION} for i in range(scan_fetch.MAX_LEVELS + 5)]
    levels, truncated = scan_fetch.ladder_from_orders(many, STATION)
    assert len(levels) == scan_fetch.MAX_LEVELS and truncated is True
    assert levels[0][0] == 100.0


def test_compact_days_keeps_the_newest():
    out = scan_fetch.compact_days(raw_history(days=60))
    assert len(out) == scan_fetch.KEEP_DAYS
    assert out[-1][0] == (NOW.date() - timedelta(days=1)).isoformat()


def test_first_fetch_makes_two_requests_then_none(factory, stubs):
    async def go():
        pacer = scan_fetch.Pacer(pause=0)
        async with factory() as db:
            first = await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
            second = await scan_fetch.ensure_summary(db, REGION, STATION, TYPE,
                                                     NOW + timedelta(minutes=5), pacer)
            return first, second
    assert _run(go()) == (2, 0)
    assert stubs == ["history", "orders"]


def test_orders_refresh_alone_after_fifteen_minutes(factory, stubs):
    async def go():
        pacer = scan_fetch.Pacer(pause=0)
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
            return await scan_fetch.ensure_summary(db, REGION, STATION, TYPE,
                                                   NOW + timedelta(minutes=16), pacer)
    assert _run(go()) == 1
    assert stubs == ["history", "orders", "orders"]


def test_history_refreshes_alone_after_a_day_from_a_new_session(factory, stubs):
    """Stamps come back naive from SQLite; the TTL maths must cope."""
    async def go():
        pacer = scan_fetch.Pacer(pause=0)
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
        async with factory() as db:
            return await scan_fetch.ensure_summary(db, REGION, STATION, TYPE,
                                                   NOW + timedelta(hours=25), pacer)
    assert _run(go()) == 2
    assert stubs == ["history", "orders", "history", "orders"]


def test_scan_never_writes_market_history(factory, stubs):
    async def go():
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW,
                                            scan_fetch.Pacer(pause=0))
            return (await db.execute(select(func.count()).select_from(MarketHistory))).scalar()
    assert _run(go()) == 0


def test_fresh_market_history_is_reused(factory, stubs):
    async def go():
        async with factory() as db:
            await history.get_history(REGION, TYPE, db)      # the item page viewed it
            stubs.clear()
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW,
                                            scan_fetch.Pacer(pause=0))
        async with factory() as db:
            row = await db.get(MarketScanSummary, (REGION, TYPE))
            return scan_fetch.figures(row, STATION, NOW)
    fig = _run(go())
    assert stubs == ["orders"]
    assert fig is not None and fig["demand_30"] == pytest.approx(5.0)


def test_figures_agree_with_phase_one(factory, stubs):
    async def go():
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW,
                                            scan_fetch.Pacer(pause=0))
        async with factory() as db:     # fresh session: naive stamps from SQLite
            row = await db.get(MarketScanSummary, (REGION, TYPE))
            return scan_fetch.figures(row, STATION, NOW), row, scan_fetch.figures(
                row, STATION, NOW.replace(tzinfo=None))
    fig, row, naive = _run(go())
    assert naive == fig
    days = [{"date": date.fromisoformat(d["date"]), "volume": d["volume"],
             "average": d["average"]} for d in raw_history()]
    # At 14:00 UTC the game has published through yesterday, the history covers
    # through yesterday, so the window ends (exclusive) today.
    expected = market_figures(days, raw_orders(), STATION, 1, NOW.date())
    for key in ("lowest_sell", "demand_30", "demand_7", "trade_days", "reference",
                "stock", "days_of_stock", "nothing_listed", "outliers"):
        assert fig[key] == expected[key], key
    assert fig["high_30"] == 340.0
    assert fig["ladder"] == [(331.0, 20)]          # 400 is outside the 10% band
    assert fig["behind"] is False
    assert fig["ladder_truncated"] is False
    # A naive `now` is read as UTC, same as ensure_summary.


def test_figures_none_when_a_part_is_missing_or_history_is_stale(factory, stubs):
    async def go():
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW,
                                            scan_fetch.Pacer(pause=0))
        async with factory() as db:
            row = await db.get(MarketScanSummary, (REGION, TYPE))
            late = scan_fetch.figures(row, STATION, NOW + timedelta(days=10))
            row.ladder_json = None
            return late, scan_fetch.figures(row, STATION, NOW)
    assert _run(go()) == (None, None)
    assert scan_fetch.figures(None, STATION, NOW) is None


def test_a_failed_orders_fetch_keeps_the_history_part(factory, stubs, monkeypatch):
    async def boom(region_id, type_id):
        raise RuntimeError("down")
    real = scan_fetch.market_orders._fetch_orders_esi
    monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", boom)

    async def go():
        pacer = scan_fetch.Pacer(pause=0)
        async with factory() as db:
            with pytest.raises(RuntimeError):
                await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
            await db.rollback()
        async with factory() as db:
            row = await db.get(MarketScanSummary, (REGION, TYPE))
            state = (row is not None and row.history_fetched_at is not None
                     and row.days_json is not None and row.ladder_json is None
                     and row.orders_fetched_at is None,
                     scan_fetch.figures(row, STATION, NOW))
            stubs.clear()
            monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", real)
            n = await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
            return state, n
    state, n = _run(go())
    assert state == (True, None)
    assert n == 1 and stubs == ["orders"]


def test_figures_reports_truncation(factory, stubs, monkeypatch):
    async def many(region_id, type_id):
        return [{"is_buy_order": False, "price": 331.0 + i, "volume_remain": 1,
                 "location_id": STATION} for i in range(scan_fetch.MAX_LEVELS + 5)]
    monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", many)

    async def go():
        async with factory() as db:
            await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW,
                                            scan_fetch.Pacer(pause=0))
        async with factory() as db:
            row = await db.get(MarketScanSummary, (REGION, TYPE))
            return scan_fetch.figures(row, STATION, NOW)
    assert _run(go())["ladder_truncated"] is True


def test_concurrent_callers_for_one_new_key_fetch_once(factory, stubs, monkeypatch):
    async def slow_history(region_id, type_id):
        stubs.append("history")
        await asyncio.sleep(0.02)
        return raw_history()

    async def slow_orders(region_id, type_id):
        stubs.append("orders")
        await asyncio.sleep(0.02)
        return raw_orders()
    monkeypatch.setattr(scan_fetch.market_history, "_fetch_history_esi", slow_history)
    monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", slow_orders)

    async def go():
        pacer = scan_fetch.Pacer(pause=0)

        async def one():
            async with factory() as db:
                return await scan_fetch.ensure_summary(db, REGION, STATION, TYPE, NOW, pacer)
        results = await asyncio.gather(one(), one())
        async with factory() as db:
            count = (await db.execute(
                select(func.count()).select_from(MarketScanSummary))).scalar()
        return results, count
    results, count = _run(go())
    assert sorted(results) == [0, 2] and count == 1
    assert stubs.count("history") == 1 and stubs.count("orders") == 1
    assert scan_fetch._locks == {}


def test_pacer_cancelled_during_the_pause_releases_its_slot():
    async def go():
        pacer = scan_fetch.Pacer(concurrent=1, batch=1, pause=5)

        async def enter():
            async with pacer:
                pass
        task = asyncio.ensure_future(enter())
        await asyncio.sleep(0.05)           # inside the pause sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pacer._pause = 0

        async def again():
            async with pacer:
                pass
        await asyncio.wait_for(again(), timeout=1)
    _run(go())
