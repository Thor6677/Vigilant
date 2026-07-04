"""Tests for the on-demand market history service + routes (Phase 4 Task 1).

Service tests use the manual-event-loop + temp-file DB idiom
(tests/test_sync_field_sessions.py): a real sqlite file so upsert +
`on_conflict_do_update` execute against the actual SQLite dialect, with a
fresh `async_sessionmaker` whose sessions we hand to `get_history`. The ESI
fetch is monkeypatched — the network is never hit — and a `_now` clock seam
drives the 24h TTL.

Route smoke tests use TestClient against the real app to prove the endpoints
exist and are auth-gated (the nav dead-link test separately proves `/market`
is a registered route).
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.market.history as history
from app.db.models import Base, MarketHistory, MarketHistoryMeta

REGION = history.DEFAULT_REGION_ID
TYPE_ID = 34  # Tritanium


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


def _sample_rows():
    return [
        {"date": "2026-06-01", "average": 5.10, "highest": 5.40,
         "lowest": 4.90, "volume": 12_000_000_000, "order_count": 900},
        {"date": "2026-06-02", "average": 5.20, "highest": 5.55,
         "lowest": 5.00, "volume": 13_500_000_000, "order_count": 950},
        {"date": "2026-06-03", "average": 5.05, "highest": 5.30,
         "lowest": 4.80, "volume": 11_000_000_000, "order_count": 880},
    ]


# ── fetch-once-then-cached ─────────────────────────────────────────────────

def test_fetch_once_then_served_from_cache(monkeypatch):
    engine, SessionLocal = _temp_engine()
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        assert region_id == REGION and type_id == TYPE_ID
        return _sample_rows()

    monkeypatch.setattr(history, "_fetch_history_esi", fake_fetch)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            rows1 = await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 1
        assert len(rows1) == 3
        # ordered ascending by date, values round-tripped through SQLite
        assert [r.date.isoformat() for r in rows1] == [
            "2026-06-01", "2026-06-02", "2026-06-03"]
        assert rows1[0].volume == 12_000_000_000  # BigInteger, no overflow

        # Second call inside the TTL must NOT refetch.
        async with SessionLocal() as db:
            rows2 = await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 1
        assert len(rows2) == 3

    _run(scenario())


# ── stale refetch ──────────────────────────────────────────────────────────

def test_stale_meta_triggers_refetch(monkeypatch):
    engine, SessionLocal = _temp_engine()
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        return _sample_rows()

    monkeypatch.setattr(history, "_fetch_history_esi", fake_fetch)

    t0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    monkeypatch.setattr(history, "_now", lambda: clock["t"])

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 1

        # Within TTL → no refetch.
        clock["t"] = t0 + history.HISTORY_TTL - timedelta(hours=1)
        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 1

        # Past TTL → refetch.
        clock["t"] = t0 + history.HISTORY_TTL + timedelta(hours=1)
        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 2

    _run(scenario())


# ── upsert idempotency ─────────────────────────────────────────────────────

def test_upsert_is_idempotent(monkeypatch):
    engine, SessionLocal = _temp_engine()

    # Second fetch returns the same dates with revised values — upsert should
    # overwrite in place, never duplicate the (region, type, date) rows.
    revised = [
        {"date": "2026-06-01", "average": 6.00, "highest": 6.40,
         "lowest": 5.90, "volume": 1_000, "order_count": 10},
        {"date": "2026-06-02", "average": 5.20, "highest": 5.55,
         "lowest": 5.00, "volume": 13_500_000_000, "order_count": 950},
        {"date": "2026-06-03", "average": 5.05, "highest": 5.30,
         "lowest": 4.80, "volume": 11_000_000_000, "order_count": 880},
    ]
    payloads = [_sample_rows(), revised]

    async def fake_fetch(region_id, type_id):
        return payloads.pop(0)

    monkeypatch.setattr(history, "_fetch_history_esi", fake_fetch)

    # Force both calls to actually fetch by keeping meta perpetually stale.
    monkeypatch.setattr(history, "HISTORY_TTL", timedelta(seconds=0))

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)
        async with SessionLocal() as db:
            rows = await history.get_history(REGION, TYPE_ID, db)

        assert len(rows) == 3  # no duplicates
        by_date = {r.date.isoformat(): r for r in rows}
        assert by_date["2026-06-01"].average == 6.00  # overwritten in place

        async with SessionLocal() as db:
            total = (await db.execute(
                select(func.count()).select_from(MarketHistory)
            )).scalar()
        assert total == 3

    _run(scenario())


# ── empty history still stamps meta ────────────────────────────────────────

def test_empty_history_stamps_meta_and_does_not_refetch(monkeypatch):
    engine, SessionLocal = _temp_engine()
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        return []

    monkeypatch.setattr(history, "_fetch_history_esi", fake_fetch)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            rows = await history.get_history(REGION, TYPE_ID, db)
        assert rows == []
        assert calls["n"] == 1

        # Meta was stamped despite zero rows → no refetch on the next view.
        async with SessionLocal() as db:
            meta = (await db.execute(
                select(MarketHistoryMeta).where(
                    MarketHistoryMeta.region_id == REGION,
                    MarketHistoryMeta.type_id == TYPE_ID,
                )
            )).scalar_one_or_none()
        assert meta is not None

        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)
        assert calls["n"] == 1

    _run(scenario())


# ── stale-on-error ─────────────────────────────────────────────────────────

def test_fetch_error_returns_existing_rows(monkeypatch):
    engine, SessionLocal = _temp_engine()
    state = {"fail": False}

    async def fake_fetch(region_id, type_id):
        if state["fail"]:
            raise RuntimeError("ESI down")
        return _sample_rows()

    monkeypatch.setattr(history, "_fetch_history_esi", fake_fetch)
    monkeypatch.setattr(history, "HISTORY_TTL", timedelta(seconds=0))  # always stale

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            await history.get_history(REGION, TYPE_ID, db)

        # Now the fetch fails; we must still return the previously stored rows.
        state["fail"] = True
        async with SessionLocal() as db:
            rows = await history.get_history(REGION, TYPE_ID, db)
        assert len(rows) == 3

    _run(scenario())


# ── route smoke: auth gating ───────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_market_browser_redirects_when_unauthenticated():
    client = _client()
    r = client.get("/market", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_market_type_page_redirects_when_unauthenticated():
    client = _client()
    r = client.get(f"/market/type/{TYPE_ID}", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_history_json_401_when_unauthenticated():
    client = _client()
    r = client.get(f"/market/type/{TYPE_ID}/history.json")
    assert r.status_code == 401


def test_market_search_401_when_unauthenticated():
    client = _client()
    r = client.get("/market/search?q=trit")
    assert r.status_code == 401
