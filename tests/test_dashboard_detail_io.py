"""app/dashboard/detail.py (T-080): correctness for the two async I/O
functions — load_latest_networth and load_wallet_sparkline_points. Both
must still be ONE statement (proven by counting statements against the sync
engine, same pattern as tests/test_dashboard_walletdelta.py), and
load_wallet_sparkline_points' SQL-side bucketing must pick the LATEST
balance in each bucket, not just any row in it.

Perf and EXPLAIN QUERY PLAN coverage for both lives in
tests/test_dashboard_detail_perf.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard.detail import load_latest_networth, load_wallet_sparkline_points
from app.db.models import Base, NetWorthSnapshot, WalletSnapshot

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield async_sessionmaker(engine, expire_on_commit=False), engine
    asyncio.run(engine.dispose())


def _count_statements(engine, table_name: str, coro):
    statements = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if table_name in statement.lower():
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _listener)
    try:
        result = asyncio.run(coro)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listener)
    return result, statements


# ── load_latest_networth ─────────────────────────────────────────────────────

CID_A = 1
CID_B = 2
CID_NO_HISTORY = 3


def test_load_latest_networth_is_one_statement_and_picks_the_newest_row(db_factory):
    SessionLocal, engine = db_factory

    async def _seed():
        async with SessionLocal() as db:
            db.add(NetWorthSnapshot(character_id=CID_A, date=NOW.date() - timedelta(days=2), total=100.0))
            db.add(NetWorthSnapshot(character_id=CID_A, date=NOW.date(), total=500.0))
            db.add(NetWorthSnapshot(character_id=CID_B, date=NOW.date() - timedelta(days=1), total=250.0))
            await db.commit()
    asyncio.run(_seed())

    async def _run():
        async with SessionLocal() as db:
            return await load_latest_networth(db, [CID_A, CID_B, CID_NO_HISTORY])

    result, statements = _count_statements(engine, "net_worth_snapshots", _run())
    assert len(statements) == 1, f"expected exactly one net_worth_snapshots statement, got {len(statements)}"
    assert result[CID_A] == 500.0
    assert result[CID_B] == 250.0
    assert CID_NO_HISTORY not in result


def test_load_latest_networth_empty_ids_returns_empty_without_a_query(db_factory):
    SessionLocal, _ = db_factory

    async def _run():
        async with SessionLocal() as db:
            return await load_latest_networth(db, [])
    assert asyncio.run(_run()) == {}


# ── load_wallet_sparkline_points ─────────────────────────────────────────────

def test_load_wallet_sparkline_points_is_one_statement(db_factory):
    SessionLocal, engine = db_factory
    naive_now = NOW.replace(tzinfo=None)

    async def _seed():
        async with SessionLocal() as db:
            db.add(WalletSnapshot(character_id=CID_A, balance=100.0, recorded_at=naive_now - timedelta(days=1)))
            db.add(WalletSnapshot(character_id=CID_A, balance=200.0, recorded_at=naive_now))
            await db.commit()
    asyncio.run(_seed())

    async def _run():
        async with SessionLocal() as db:
            return await load_wallet_sparkline_points(db, [CID_A], now=NOW)

    result, statements = _count_statements(engine, "wallet_snapshots", _run())
    assert len(statements) == 1, f"expected exactly one wallet_snapshots statement, got {len(statements)}"
    assert result[CID_A] == [
        (naive_now - timedelta(days=1), 100.0),
        (naive_now, 200.0),
    ]


def test_load_wallet_sparkline_points_empty_ids_returns_empty_without_a_query(db_factory):
    SessionLocal, _ = db_factory

    async def _run():
        async with SessionLocal() as db:
            return await load_wallet_sparkline_points(db, [], now=NOW)
    assert asyncio.run(_run()) == {}


def test_load_wallet_sparkline_points_no_history_is_absent(db_factory):
    SessionLocal, _ = db_factory

    async def _run():
        async with SessionLocal() as db:
            return await load_wallet_sparkline_points(db, [CID_NO_HISTORY], now=NOW)
    assert asyncio.run(_run()) == {}


def test_load_wallet_sparkline_points_keeps_the_latest_balance_per_bucket(db_factory):
    """SPARKLINE_BUCKET is 3h. Three snapshots inside the [00:00, 03:00)
    bucket must collapse to ONE point carrying the LAST (highest
    recorded_at) balance in that bucket, not the first or an arbitrary one;
    a fourth snapshot in the next bucket must survive as its own point."""
    SessionLocal, _ = db_factory
    base = NOW.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(days=1)

    async def _seed():
        async with SessionLocal() as db:
            db.add(WalletSnapshot(character_id=CID_A, balance=1.0, recorded_at=base))
            db.add(WalletSnapshot(character_id=CID_A, balance=55.0, recorded_at=base + timedelta(minutes=30)))
            db.add(WalletSnapshot(character_id=CID_A, balance=123.0, recorded_at=base + timedelta(hours=2, minutes=59)))
            db.add(WalletSnapshot(character_id=CID_A, balance=999.0, recorded_at=base + timedelta(hours=3, minutes=5)))
            await db.commit()
    asyncio.run(_seed())

    async def _run():
        async with SessionLocal() as db:
            return await load_wallet_sparkline_points(db, [CID_A], now=NOW)
    result = asyncio.run(_run())

    assert result[CID_A] == [
        (base + timedelta(hours=2, minutes=59), 123.0),
        (base + timedelta(hours=3, minutes=5), 999.0),
    ]
