"""Tests for the net-worth tracker (Phase 5 Task 1).

Service tests use the manual-event-loop + temp-file DB idiom
(tests/test_market_history.py / test_sync_field_sessions.py): a real sqlite
file so the `on_conflict_do_update` upsert executes against the actual SQLite
dialect. No ESI is ever hit — `take_snapshots` takes an injected price map, so
valuation + idempotency are exercised with pure fixtures.

Route smoke tests use TestClient against the real app to prove the endpoints
exist and are auth-gated.
"""
import asyncio
import tempfile
from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    Character,
    CharacterAssetCache,
    CharacterDashboardCache,
    NetWorthSnapshot,
)
from app.networth.snapshot import (
    take_snapshots,
    value_assets,
    value_industry_jobs,
    value_orders,
)

import json


CHAR_ID = 90000042
USER_ID = 7


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


def _make_char(cid=CHAR_ID, user_id=USER_ID) -> Character:
    return Character(
        character_id=cid,
        character_name="Networth Pilot",
        access_token="dummy-access",
        refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes="",
        user_id=user_id,
    )


# ── valuation math ──────────────────────────────────────────────────────────

def test_value_assets_sums_qty_times_price():
    assets = [
        {"type_id": 34, "quantity": 1000},   # 1000 * 5.0 = 5000
        {"type_id": 35, "quantity": 10},     # 10 * 12.5   = 125
        {"type_id": 34, "quantity": 2000},   # 2000 * 5.0  = 10000
    ]
    price_map = {34: 5.0, 35: 12.5}
    total, unpriced = value_assets(assets, price_map)
    assert total == 5000 + 125 + 10000
    assert unpriced == 0


def test_value_assets_skips_and_counts_unpriced():
    assets = [
        {"type_id": 34, "quantity": 100},    # priced: 500
        {"type_id": 999999, "quantity": 5},  # unpriced -> skipped + counted
        {"type_id": None, "quantity": 3},    # no type -> skipped + counted
    ]
    price_map = {34: 5.0}
    total, unpriced = value_assets(assets, price_map)
    assert total == 500
    assert unpriced == 2


def test_value_assets_handles_empty_and_missing_qty():
    assert value_assets(None, {}) == (0.0, 0)
    assert value_assets([], {34: 5.0}) == (0.0, 0)
    # Missing quantity defaults to 0 (not 1) so a malformed row can't inflate.
    total, unpriced = value_assets([{"type_id": 34}], {34: 5.0})
    assert total == 0.0 and unpriced == 0


def test_value_orders_buy_escrow_plus_sell_goods():
    orders = [
        # Buy order: only its escrow counts, never price x volume.
        {"type_id": 34, "is_buy_order": True, "price": 4.0,
         "volume_remain": 1000, "escrow": 3500.0},
        # Sell order with a reference price: valued at ref price, not ask.
        {"type_id": 35, "is_buy_order": False, "price": 99.0,
         "volume_remain": 10, "escrow": 0.0},
        # Sell order without a reference price: falls back to its ask price.
        {"type_id": 999999, "is_buy_order": False, "price": 2.5,
         "volume_remain": 4, "escrow": 0.0},
    ]
    total = value_orders(orders, {34: 5.0, 35: 12.5})
    assert total == 3500.0 + 12.5 * 10 + 2.5 * 4
    assert value_orders([], {}) == 0.0


def test_value_industry_jobs_output_valuation():
    jobs = [
        # 3 runs x qty 100/run x 6.0 = 1800
        {"blueprint_type_id": 1001, "product_type_id": 44, "runs": 3},
        # No per-run quantity known -> defaults to 1: 2 x 1 x 50 = 100
        {"blueprint_type_id": 1002, "product_type_id": 45, "runs": 2},
        # Unpriced product contributes nothing.
        {"blueprint_type_id": 1003, "product_type_id": 999999, "runs": 9},
    ]
    total = value_industry_jobs(jobs, {44: 6.0, 45: 50.0}, {1001: 100})
    assert total == 1800.0 + 100.0
    assert value_industry_jobs([], {}, {}) == 0.0


# ── snapshot: wallet + assets, idempotent upsert ────────────────────────────

def _seed(SessionLocal, wallet, assets):
    async def _do():
        async with SessionLocal() as db:
            db.add(_make_char())
            db.add(CharacterDashboardCache(character_id=CHAR_ID, wallet=wallet))
            db.add(CharacterAssetCache(
                character_id=CHAR_ID, assets_json=json.dumps(assets)))
            await db.commit()
    return _do()


def test_snapshot_values_wallet_plus_assets():
    engine, SessionLocal = _temp_engine()
    price_map = {34: 5.0, 35: 100.0}
    assets = [{"type_id": 34, "quantity": 1000},   # 5000
              {"type_id": 35, "quantity": 3},       # 300
              {"type_id": 777, "quantity": 1}]      # unpriced

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal, wallet=1_000_000.0, assets=assets)

        async with SessionLocal() as db:
            chars = list((await db.execute(select(Character))).scalars().all())
            res = await take_snapshots(db, price_map, chars, on_date=date(2026, 7, 4))
        assert res["written"] == 1 and res["skipped"] == 0

        async with SessionLocal() as db:
            row = (await db.execute(select(NetWorthSnapshot))).scalar_one()
        assert row.wallet == 1_000_000.0
        assert row.assets_value == 5000 + 300
        assert row.escrow == 0.0
        assert row.total == 1_000_000.0 + 5300
        assert row.unpriced_count == 1
        assert row.user_id == USER_ID

    _run(scenario())


def test_snapshot_upsert_is_idempotent():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal, wallet=500.0,
                    assets=[{"type_id": 34, "quantity": 100}])  # 100*5 = 500

        d = date(2026, 7, 4)
        # First run.
        async with SessionLocal() as db:
            chars = list((await db.execute(select(Character))).scalars().all())
            await take_snapshots(db, {34: 5.0}, chars, on_date=d)
        # Second run, SAME date, revised wallet + price -> overwrite in place.
        async with SessionLocal() as db:
            chars = list((await db.execute(select(Character))).scalars().all())
            # bump the wallet so we can prove the row was rewritten
            cache = (await db.execute(
                select(CharacterDashboardCache))).scalar_one()
            cache.wallet = 900.0
            await db.commit()
            await take_snapshots(db, {34: 10.0}, chars, on_date=d)

        async with SessionLocal() as db:
            total_rows = (await db.execute(
                select(func.count()).select_from(NetWorthSnapshot))).scalar()
            row = (await db.execute(select(NetWorthSnapshot))).scalar_one()
        assert total_rows == 1                      # no duplicate (char, date)
        assert row.wallet == 900.0                  # overwritten
        assert row.assets_value == 100 * 10.0       # revised price applied
        assert row.total == 900.0 + 1000.0

    _run(scenario())


def test_snapshot_skips_character_with_no_data():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Character exists but has NO wallet and NO asset cache.
        async with SessionLocal() as db:
            db.add(_make_char())
            await db.commit()

        async with SessionLocal() as db:
            chars = list((await db.execute(select(Character))).scalars().all())
            res = await take_snapshots(db, {34: 5.0}, chars, on_date=date(2026, 7, 4))
        assert res["written"] == 0 and res["skipped"] == 1

        async with SessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(NetWorthSnapshot))).scalar()
        assert count == 0

    _run(scenario())


# ── route smoke: auth gating ────────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_networth_page_redirects_when_unauthenticated():
    r = _client().get("/tools/networth", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_networth_data_401_when_unauthenticated():
    r = _client().get("/tools/networth/data.json")
    assert r.status_code == 401


def _authed_client(user_id=USER_ID):
    """TestClient carrying a signed session cookie for `user_id`. Uses an https
    base_url because the session cookie is Secure outside debug mode."""
    import base64
    import json as _json
    import itsdangerous
    import app.main as main

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(_json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def test_networth_page_renders_when_authenticated():
    # Render the real page through base.html: proves the nav-globals push onto
    # the `templates` instance, the csp nonce, and the chart scaffold all work.
    r = _authed_client().get("/tools/networth")
    assert r.status_code == 200
    body = r.text
    assert "Net Worth" in body
    assert "nw-chart" in body          # chart canvas scaffold present
    assert "market-locked value" in body   # valuation footnote surfaced (T-041 item 4)
    assert "nav_groups" not in body    # nav rendered, not left as a literal


def test_networth_data_json_empty_when_no_characters():
    # Authenticated user with no linked characters -> empty, well-formed feed.
    r = _authed_client(user_id=999999).get("/tools/networth/data.json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["dates"] == [] and payload["characters"] == []
    assert payload["total"] == [] and payload["unpriced_count"] == 0


def test_networth_snapshot_post_rejected_when_unauthenticated():
    # A session-less POST is rejected before the handler by the CSRF
    # middleware (403 — no session token to satisfy); the route's own
    # user_id gate would return 401 if CSRF were satisfied. Either way the
    # state-mutating endpoint is never reachable unauthenticated.
    r = _client().post("/tools/networth/snapshot")
    assert r.status_code in (401, 403)
