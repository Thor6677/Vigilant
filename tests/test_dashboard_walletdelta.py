"""app/dashboard/walletdelta.py (T-076): the 7-day wallet-change arrow.

`load_wallet_baselines` must be ONE statement for every pilot (never one per
pilot) — proven by counting statements against the sync engine, the same
pattern tests/test_canfly_engine.py uses. `build_wallet_deltas` is pure and
tested directly against crafted baselines/current-wallet dicts.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard.walletdelta import build_wallet_deltas, load_wallet_baselines
from app.db.models import Base, WalletSnapshot

CID_UP = 1
CID_DOWN = 2
CID_FLAT = 3
CID_NO_HISTORY = 4
CID_NO_CURRENT_WALLET = 5

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            naive_now = NOW.replace(tzinfo=None)
            # CID_UP: an 8-day-old snapshot (the real baseline) and a
            # 2-day-old one (must NOT be picked — proves "at or before
            # cutoff", not "the very oldest").
            db.add(WalletSnapshot(character_id=CID_UP, balance=100.0, recorded_at=naive_now - timedelta(days=8)))
            db.add(WalletSnapshot(character_id=CID_UP, balance=900.0, recorded_at=naive_now - timedelta(days=2)))
            db.add(WalletSnapshot(character_id=CID_DOWN, balance=500.0, recorded_at=naive_now - timedelta(days=7, hours=1)))
            db.add(WalletSnapshot(character_id=CID_FLAT, balance=300.0, recorded_at=naive_now - timedelta(days=10)))
            # CID_NO_HISTORY: only a snapshot inside the 7-day window — no
            # baseline old enough to diff against.
            db.add(WalletSnapshot(character_id=CID_NO_HISTORY, balance=50.0, recorded_at=naive_now - timedelta(days=1)))
            # CID_NO_CURRENT_WALLET has an old-enough snapshot but no current
            # wallet value (never synced since) — build_wallet_deltas must
            # still drop it.
            db.add(WalletSnapshot(character_id=CID_NO_CURRENT_WALLET, balance=10.0, recorded_at=naive_now - timedelta(days=9)))
            await db.commit()

    asyncio.run(_init())
    yield async_sessionmaker(engine, expire_on_commit=False), engine
    asyncio.run(engine.dispose())


def test_load_wallet_baselines_is_one_statement(db_factory):
    SessionLocal, engine = db_factory
    statements = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if "wallet_snapshots" in statement.lower():
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _listener)
    try:
        async def _run():
            async with SessionLocal() as db:
                return await load_wallet_baselines(
                    db, [CID_UP, CID_DOWN, CID_FLAT, CID_NO_HISTORY, CID_NO_CURRENT_WALLET], now=NOW,
                )
        result = asyncio.run(_run())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listener)

    assert len(statements) == 1, f"expected exactly one wallet_snapshots statement, got {len(statements)}"
    assert result[CID_UP] == 100.0
    assert result[CID_DOWN] == 500.0
    assert result[CID_FLAT] == 300.0
    assert CID_NO_HISTORY not in result
    assert result[CID_NO_CURRENT_WALLET] == 10.0


def test_load_wallet_baselines_chunks_past_the_union_all_limit(db_factory):
    """SQLite caps a compound SELECT at 500 terms by default. 450 character
    ids must still come back correct — chunked into
    ceil(450/_MAX_IDS_PER_STATEMENT) statements (3, at 200/chunk) rather
    than one 450-branch UNION ALL."""
    SessionLocal, engine = db_factory
    naive_now = NOW.replace(tzinfo=None)
    n_ids = 450
    character_ids = list(range(1001, 1001 + n_ids))

    async def _seed():
        async with SessionLocal() as db:
            for cid in character_ids:
                db.add(WalletSnapshot(character_id=cid, balance=float(cid), recorded_at=naive_now - timedelta(days=8)))
            await db.commit()
    asyncio.run(_seed())

    statements = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if "wallet_snapshots" in statement.lower():
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _listener)
    try:
        async def _run():
            async with SessionLocal() as db:
                return await load_wallet_baselines(db, character_ids, now=NOW)
        result = asyncio.run(_run())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listener)

    assert len(statements) == 3, f"expected 3 chunked statements for {n_ids} ids, got {len(statements)}"
    assert len(result) == n_ids
    for cid in character_ids:
        assert result[cid] == float(cid)


def test_load_wallet_baselines_empty_ids_returns_empty_without_a_query(db_factory):
    SessionLocal, engine = db_factory

    async def _run():
        async with SessionLocal() as db:
            return await load_wallet_baselines(db, [], now=NOW)
    assert asyncio.run(_run()) == {}


# ── build_wallet_deltas: pure ────────────────────────────────────────────────

def test_build_wallet_deltas_up_down_flat():
    current = {CID_UP: 900.0, CID_DOWN: 100.0, CID_FLAT: 300.0}
    baselines = {CID_UP: 100.0, CID_DOWN: 500.0, CID_FLAT: 300.0}
    out = build_wallet_deltas(current, baselines)
    assert out[CID_UP] == {"direction": "up", "amount": 800.0}
    assert out[CID_DOWN] == {"direction": "down", "amount": 400.0}
    assert out[CID_FLAT] == {"direction": "flat", "amount": 0.0}


def test_build_wallet_deltas_no_baseline_shows_nothing():
    out = build_wallet_deltas({CID_NO_HISTORY: 50.0}, {})
    assert CID_NO_HISTORY not in out


def test_build_wallet_deltas_no_current_wallet_shows_nothing():
    out = build_wallet_deltas({}, {CID_NO_CURRENT_WALLET: 10.0})
    assert CID_NO_CURRENT_WALLET not in out


def test_build_wallet_deltas_current_wallet_none_shows_nothing():
    out = build_wallet_deltas({CID_UP: None}, {CID_UP: 5.0})
    assert CID_UP not in out
