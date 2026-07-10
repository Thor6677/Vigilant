"""Tests for Trading P&L — FIFO transaction matching (Phase 5 Task 5).

Three layers:
  * The pure `app.market.pnl` engine, exercised exhaustively against
    hand-computed numbers (single/partial/multi-lot fills, out-of-order input,
    unmatched sells, fee math).
  * The `wallet_transactions` sync fetcher on a temp-file DB — proves
    INSERT-OR-IGNORE idempotency (re-run = no dupes) and, critically, that a
    transactions sync does NOT clobber `cache.wallet`.
  * Route auth gating + empty-state via TestClient.
"""
import asyncio
import json
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routes.dashboard as dashboard
from app.db.models import (
    Base,
    Character,
    CharacterAssetCache,
    CharacterDashboardCache,
    WalletTransaction,
)
from app.market import pnl as engine
from app.market.pnl import (
    aggregate_by_type,
    aggregate_monthly,
    match_fifo,
    totals,
)

BROKER = engine.BROKER_FEE_RATE      # 0.015
TAX = engine.SALES_TAX_RATE          # 0.0337
SELL_FACTOR = 1 - TAX - BROKER       # 0.9513
BUY_FACTOR = 1 + BROKER              # 1.015

CHAR_ID = 90000055
USER_ID = 11


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
    e = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    return e, async_sessionmaker(e, expire_on_commit=False)


def _dt(s):
    return datetime.fromisoformat(s)


def _tx(tid, date, type_id, qty, price, is_buy):
    return {"transaction_id": tid, "date": _dt(date), "type_id": type_id,
            "quantity": qty, "unit_price": price, "is_buy": is_buy}


# ── FIFO engine: fee math pinned to hand-computed numbers ────────────────────

def test_single_lot_exact_fill():
    txns = [
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
        _tx(2, "2026-01-02", 34, 100, 15.0, False),
    ]
    res = engine.match_fifo(txns)
    bucket = res[34]
    assert bucket["lots"] == []              # buy fully consumed
    assert bucket["unmatched_sell_qty"] == 0
    assert len(bucket["realized"]) == 1
    m = bucket["realized"][0]
    # cost = 100 * 10 * 1.015 = 1015 ; proceeds = 100 * 15 * 0.9513 = 1426.95
    assert m["cost_basis"] == pytest.approx(1015.0)
    assert m["proceeds"] == pytest.approx(1426.95)
    assert m["profit"] == pytest.approx(411.95)


def test_partial_fill_splits_lot():
    txns = [
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
        _tx(2, "2026-01-02", 34, 40, 15.0, False),
    ]
    res = engine.match_fifo(txns)
    bucket = res[34]
    # 60 units remain in the lot at the original buy price.
    assert bucket["lots"] == [{"qty": 60, "unit_price": 10.0, "date": _dt("2026-01-01")}]
    assert bucket["unmatched_sell_qty"] == 0
    m = bucket["realized"][0]
    assert m["qty"] == 40
    assert m["profit"] == pytest.approx(40 * (15 * SELL_FACTOR - 10 * BUY_FACTOR))


def test_multi_lot_spanning_sell():
    txns = [
        _tx(1, "2026-01-01", 34, 50, 10.0, True),
        _tx(2, "2026-01-02", 34, 50, 20.0, True),
        _tx(3, "2026-01-03", 34, 80, 30.0, False),
    ]
    res = engine.match_fifo(txns)
    bucket = res[34]
    # Sell consumes all of lot1 (50@10) then 30 of lot2 (@20); 20 remain @20.
    assert bucket["lots"] == [{"qty": 20, "unit_price": 20.0, "date": _dt("2026-01-02")}]
    assert len(bucket["realized"]) == 2
    m1, m2 = bucket["realized"]
    assert m1["qty"] == 50 and m1["buy_price"] == 10.0
    assert m2["qty"] == 30 and m2["buy_price"] == 20.0
    assert m1["profit"] == pytest.approx(50 * (30 * SELL_FACTOR - 10 * BUY_FACTOR))
    assert m2["profit"] == pytest.approx(30 * (30 * SELL_FACTOR - 20 * BUY_FACTOR))


def test_out_of_order_input_sorted_internally():
    # Sell listed BEFORE its buy, but its date is later — engine sorts by date.
    txns = [
        _tx(2, "2026-01-05", 34, 100, 15.0, False),
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
    ]
    res = engine.match_fifo(txns)
    bucket = res[34]
    assert bucket["unmatched_sell_qty"] == 0
    assert len(bucket["realized"]) == 1
    assert bucket["realized"][0]["profit"] == pytest.approx(411.95)


def test_same_timestamp_tiebreak_by_transaction_id():
    # Buy and sell share a timestamp; the buy (lower id) must be processed first.
    txns = [
        _tx(2, "2026-01-01", 34, 10, 20.0, False),
        _tx(1, "2026-01-01", 34, 10, 10.0, True),
    ]
    res = engine.match_fifo(txns)
    assert res[34]["unmatched_sell_qty"] == 0
    assert len(res[34]["realized"]) == 1


def test_unmatched_sell_excluded_and_counted():
    txns = [_tx(1, "2026-01-01", 34, 10, 5.0, False)]  # sell with no prior buy
    res = engine.match_fifo(txns)
    bucket = res[34]
    assert bucket["realized"] == []
    assert bucket["unmatched_sell_qty"] == 10
    # Excluded from every rollup.
    assert engine.aggregate_by_type(res)[0]["realized_isk"] == 0
    assert engine.totals(res)["realized_isk"] == 0
    assert engine.totals(res)["unmatched_sell_qty"] == 10


def test_partial_unmatched_after_lots_exhausted():
    txns = [
        _tx(1, "2026-01-01", 34, 30, 10.0, True),
        _tx(2, "2026-01-02", 34, 50, 15.0, False),  # 30 matched, 20 unmatched
    ]
    res = engine.match_fifo(txns)
    bucket = res[34]
    assert bucket["realized"][0]["qty"] == 30
    assert bucket["unmatched_sell_qty"] == 20
    assert bucket["lots"] == []


def test_configurable_rates_zero_fees():
    txns = [
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
        _tx(2, "2026-01-02", 34, 100, 15.0, False),
    ]
    res = engine.match_fifo(txns, broker_fee=0.0, sales_tax=0.0)
    # No fees: pure (15-10)*100 = 500.
    assert res[34]["realized"][0]["profit"] == pytest.approx(500.0)


def test_aggregate_by_type_sorted_and_weighted_margin():
    txns = [
        # Type 34: profit 411.95 on cost 1015 -> ~40.59% margin
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
        _tx(2, "2026-01-02", 34, 100, 15.0, False),
        # Type 35: a small loss
        _tx(3, "2026-01-01", 35, 10, 100.0, True),
        _tx(4, "2026-01-02", 35, 10, 90.0, False),
    ]
    res = engine.match_fifo(txns)
    rows = engine.aggregate_by_type(res)
    assert [r["type_id"] for r in rows] == [34, 35]           # profit desc
    assert rows[0]["margin_pct"] == pytest.approx(411.95 / 1015 * 100)
    assert rows[1]["realized_isk"] < 0


def test_aggregate_monthly_buckets_on_sell_month():
    txns = [
        _tx(1, "2026-01-01", 34, 100, 10.0, True),
        _tx(2, "2026-01-15", 34, 50, 15.0, False),   # Jan
        _tx(3, "2026-02-10", 34, 50, 15.0, False),   # Feb
    ]
    res = engine.match_fifo(txns)
    monthly = engine.aggregate_monthly(res)
    assert [m["month"] for m in monthly] == ["2026-01", "2026-02"]
    assert monthly[0]["qty_flipped"] == 50 and monthly[1]["qty_flipped"] == 50


# ── Fetcher: storage idempotency + wallet not clobbered ──────────────────────

_ESI_BATCH = [
    {"transaction_id": 1001, "date": "2026-01-01T10:00:00Z", "type_id": 34,
     "quantity": 100, "unit_price": 10.0, "is_buy": True,
     "client_id": 500, "location_id": 60003760},
    {"transaction_id": 1002, "date": "2026-01-02T10:00:00Z", "type_id": 34,
     "quantity": 100, "unit_price": 15.0, "is_buy": False,
     "client_id": 501, "location_id": 60003760},
]


def _patch_esi(monkeypatch):
    async def fake_client_for(char):
        return type("C", (), {"cache_enabled": True})(), None

    async def fake_get_tx(client, character_id, from_id=None):
        # One page of history; paging back (from_id set) returns nothing.
        return _ESI_BATCH if from_id is None else []

    monkeypatch.setattr(dashboard, "_client_for", fake_client_for)
    monkeypatch.setattr(dashboard.esi_char, "get_wallet_transactions", fake_get_tx)


def _make_char(scopes="esi-wallet.read_character_wallet.v1"):
    return Character(
        character_id=CHAR_ID, character_name="Trader",
        access_token="x", refresh_token="y",
        token_expiry=datetime(2099, 1, 1), scopes=scopes, user_id=USER_ID,
    )


def test_fetcher_stores_rows_and_is_idempotent(monkeypatch):
    _patch_esi(monkeypatch)
    e, SessionLocal = _temp_engine()

    async def scenario():
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char())
            await db.commit()

        async with SessionLocal() as db:
            char = (await db.execute(select(Character))).scalar_one()
            res = await dashboard.fetch_wallet_transactions_data([char], db)
        assert res[CHAR_ID] == (2, None)          # 2 new rows inserted

        async with SessionLocal() as db:
            n = (await db.execute(select(func.count()).select_from(WalletTransaction))).scalar()
        assert n == 2

        # Re-run: same batch, INSERT OR IGNORE -> zero new rows, no dupes.
        async with SessionLocal() as db:
            char = (await db.execute(select(Character))).scalar_one()
            res2 = await dashboard.fetch_wallet_transactions_data([char], db)
        assert res2[CHAR_ID] == (0, None)

        async with SessionLocal() as db:
            n2 = (await db.execute(select(func.count()).select_from(WalletTransaction))).scalar()
        assert n2 == 2

    _run(scenario())


def test_fetcher_missing_scope_marker(monkeypatch):
    _patch_esi(monkeypatch)
    e, SessionLocal = _temp_engine()

    async def scenario():
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char(scopes=""))
            await db.commit()
        async with SessionLocal() as db:
            char = (await db.execute(select(Character))).scalar_one()
            res = await dashboard.fetch_wallet_transactions_data([char], db)
        assert res[CHAR_ID] == (0, "missing_scope")

    _run(scenario())


def test_transactions_sync_does_not_clobber_wallet(monkeypatch):
    """The transactions branch in _sync_fields MUST NOT let its int marker fall
    through to the `col is None` wallet-writing block (which would overwrite the
    balance with a row count and poison net-worth snapshots)."""
    _patch_esi(monkeypatch)
    e, SessionLocal = _temp_engine()
    # _run_fetcher opens its own AsyncSessionLocal() — point it at the temp DB.
    monkeypatch.setattr(dashboard, "AsyncSessionLocal", SessionLocal)

    async def scenario():
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        now = datetime.now(timezone.utc)
        # Wallet + zkill marked fresh so ONLY `transactions` is stale.
        # (zkill has no scope -> always considered unless recently synced.)
        fresh = {"wallet": now.isoformat(), "zkill": now.isoformat()}
        async with SessionLocal() as db:
            db.add(_make_char())
            db.add(CharacterDashboardCache(
                character_id=CHAR_ID, wallet=999.0,
                field_synced_json=json.dumps(fresh)))
            db.add(CharacterAssetCache(character_id=CHAR_ID))
            await db.commit()

        async with SessionLocal() as db:
            char = (await db.execute(select(Character))).scalar_one()
            cache = (await db.execute(
                select(CharacterDashboardCache))).scalar_one()
            asset_cache = (await db.execute(
                select(CharacterAssetCache))).scalar_one()
            await dashboard._sync_fields(CHAR_ID, char, cache, asset_cache, db)

        async with SessionLocal() as db:
            cache = (await db.execute(
                select(CharacterDashboardCache))).scalar_one()
            n = (await db.execute(
                select(func.count()).select_from(WalletTransaction))).scalar()
        assert cache.wallet == 999.0            # NOT clobbered by the row count
        assert n == 2                            # transactions still persisted
        synced = json.loads(cache.field_synced_json)
        assert "transactions" in synced          # field marked synced

    _run(scenario())


# ── Route gating + empty state ───────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def _authed_client(user_id=USER_ID):
    import base64
    import itsdangerous
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def test_pnl_page_redirects_when_unauthenticated():
    r = _client().get("/market/pnl", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_pnl_page_empty_state_when_no_data():
    # Authenticated user with no characters/transactions -> friendly empty state.
    r = _authed_client(user_id=987654).get("/market/pnl")
    assert r.status_code == 200
    body = r.text
    assert "Trading P&L" in body or "Trading P&amp;L" in body
    assert "accumulates" in body               # empty-state copy
    assert "nav_groups" not in body            # nav rendered, not a literal


# ── Industry P&L: build lots (T-041 item 2) ─────────────────────────────────

def test_build_lot_no_buy_broker_fee():
    """A build lot's cost basis is raw build cost — no acquisition broker fee."""
    txs = [
        {"type_id": 1, "quantity": 10, "unit_price": 100.0, "is_buy": True,
         "date": "2026-01-01", "source": "build"},
        {"type_id": 1, "quantity": 10, "unit_price": 200.0, "is_buy": False,
         "date": "2026-01-02"},
    ]
    r = match_fifo(txs, broker_fee=0.01, sales_tax=0.02)
    m = r[1]["realized"][0]
    assert m["lot_source"] == "build"
    assert m["cost_basis"] == pytest.approx(100.0 * 10)          # raw, no 1.01x
    assert m["proceeds"] == pytest.approx(200.0 * (1 - 0.02 - 0.01) * 10)


def test_trade_and_build_lots_interleave_fifo_order():
    """Sells consume oldest lots first regardless of source; rows are tagged."""
    txs = [
        {"type_id": 1, "quantity": 5, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},                                   # trade lot
        {"type_id": 1, "quantity": 5, "unit_price": 7.0, "is_buy": True,
         "date": "2026-01-02", "source": "build"},                # build lot
        {"type_id": 1, "quantity": 8, "unit_price": 20.0, "is_buy": False,
         "date": "2026-01-03"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    rows = r[1]["realized"]
    assert [(m["qty"], m["lot_source"]) for m in rows] == [(5, "trade"), (3, "build")]


def test_per_source_aggregation_splits():
    txs = [
        {"type_id": 1, "quantity": 1, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},
        {"type_id": 1, "quantity": 1, "unit_price": 5.0, "is_buy": True,
         "date": "2026-01-02", "source": "build"},
        {"type_id": 1, "quantity": 2, "unit_price": 20.0, "is_buy": False,
         "date": "2026-02-01"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    by_type = aggregate_by_type(r)[0]
    assert by_type["trade_profit"] == pytest.approx(10.0)   # 20-10
    assert by_type["build_profit"] == pytest.approx(15.0)   # 20-5
    t = totals(r)
    assert t["trade_profit"] == pytest.approx(10.0)
    assert t["build_profit"] == pytest.approx(15.0)
    monthly = aggregate_monthly(r)
    assert monthly[0]["trade_profit"] == pytest.approx(10.0)
    assert monthly[0]["build_profit"] == pytest.approx(15.0)


def test_default_source_is_trade_and_legacy_shape_unchanged():
    txs = [
        {"type_id": 1, "quantity": 1, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},
        {"type_id": 1, "quantity": 1, "unit_price": 20.0, "is_buy": False,
         "date": "2026-01-02"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    assert r[1]["realized"][0]["lot_source"] == "trade"
