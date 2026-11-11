"""Market routes check request-chosen ids against the SDE before asking ESI (ISS-178).

history.json, the order book and the LP offers partial used to send any
integer to ESI. Each failed lookup spends the ESI error budget the whole
instance shares (every character sync included), and nothing remembered the
failure, so a repeated bad id cost an error every time.

Now:
  * history.json and the order book ask ESI only for a published SDE type
    with a market group; anything else gets the route's normal empty answer.
  * LP offers asks ESI only for an NPC corporation the app knows (the SDE's
    `sde_npc_corps`, or the in-memory roster the corp tree was built from),
    and a 404 ("this corp runs no LP store") is cached for the offers TTL.

The ESI fetch helpers are stubbed and count their calls; the network is
never reached. Ids and names are invented.
"""
import asyncio
import base64
import json
import tempfile

import httpx
import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
import app.market.history as history
import app.market.lp as lp
import app.market.orders as orders
from app.db.models import Base, User, get_db

USER = 9178
_CSRF = "test-csrf-token-esi-ids-0123456789"

MARKET_TYPE = 34          # published, has a market group
UNLISTED_TYPE = 99178001  # published, no market group
UNKNOWN_TYPE = 99178999   # not in the SDE at all

SDE_CORP = 1000125        # in sde_npc_corps
ROSTER_ONLY_CORP = 1000999  # only in the ESI roster held in memory
PLAYER_CORP = 98000001    # in neither


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def env(monkeypatch):
    """Seeded DB behind get_db, a signed-in client, and counting ESI stubs."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER))
            db.add(sm.SDEType(type_id=MARKET_TYPE, type_name="Sample Mineral",
                              published=True, market_group_id=1857))
            db.add(sm.SDEType(type_id=UNLISTED_TYPE, type_name="Sample Unlisted Module",
                              published=True, market_group_id=None))
            db.add(sm.SDENpcCorp(corporation_id=SDE_CORP, faction_id=500001))
            await db.commit()
    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    calls = {"history": [], "orders": [], "offers": [], "prices": 0}

    async def fake_history(region_id, type_id):
        calls["history"].append(type_id)
        return [{"date": "2026-06-01", "average": 5.0, "highest": 5.5,
                 "lowest": 4.5, "volume": 1000, "order_count": 10}]

    async def fake_orders(region_id, type_id):
        calls["orders"].append(type_id)
        return [{"is_buy_order": False, "price": 100.0, "volume_remain": 5,
                 "location_id": 60003760}]

    async def fake_offers(corporation_id):
        calls["offers"].append(corporation_id)
        return [{"offer_id": 1, "type_id": MARKET_TYPE, "quantity": 1,
                 "lp_cost": 100, "isk_cost": 0, "required_items": []}]

    async def fake_prices(db):
        calls["prices"] += 1
        return {MARKET_TYPE: 1000.0}

    monkeypatch.setattr(history, "_fetch_history_esi", fake_history)
    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_orders)
    monkeypatch.setattr(orders, "_cache", {})
    monkeypatch.setattr(orders, "_locks", {})
    monkeypatch.setattr(orders, "_failed", {})
    monkeypatch.setattr(history, "_failed", {})
    monkeypatch.setattr(lp, "_fetch_offers_esi", fake_offers)
    monkeypatch.setattr(lp, "get_price_map", fake_prices)
    monkeypatch.setattr(lp, "_offers_cache", {})
    monkeypatch.setattr(lp, "_offers_locks", {})
    monkeypatch.setattr(lp, "_npc_corp_ids", None)

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)

    yield client, calls

    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


# ── history.json ──────────────────────────────────────────────────────

@pytest.mark.parametrize("type_id", [UNKNOWN_TYPE, UNLISTED_TYPE, 0, -5, 2 ** 31, 2 ** 63 + 5])
def test_history_for_a_non_market_type_makes_no_esi_call(env, type_id):
    client, calls = env
    for _ in range(2):
        r = client.get(f"/market/type/{type_id}/history.json")
        assert r.status_code == 200
        body = r.json()
        assert body["type_id"] == type_id
        assert body["dates"] == [] and body["average"] == [] and body["volume"] == []
    assert calls["history"] == []


def test_history_for_a_market_type_still_fetches(env):
    client, calls = env
    r = client.get(f"/market/type/{MARKET_TYPE}/history.json?range=all")
    assert r.status_code == 200
    assert r.json()["dates"] == ["2026-06-01"]
    assert calls["history"] == [MARKET_TYPE]


# ── order book ────────────────────────────────────────────────────────

@pytest.mark.parametrize("type_id", [UNKNOWN_TYPE, UNLISTED_TYPE, 0, 2 ** 63 + 5])
def test_orders_for_a_non_market_type_make_no_esi_call(env, type_id):
    client, calls = env
    for _ in range(2):
        r = client.get(f"/market/type/{type_id}/orders")
        assert r.status_code == 200
        assert "Best sell" in r.text   # the normal (empty) book partial
        assert "100.00" not in r.text
    assert calls["orders"] == []


def test_orders_for_a_market_type_still_fetch(env):
    client, calls = env
    r = client.get(f"/market/type/{MARKET_TYPE}/orders")
    assert r.status_code == 200
    assert "100.00" in r.text
    assert calls["orders"] == [MARKET_TYPE]


# ── LP offers: validation ─────────────────────────────────────────────

@pytest.mark.parametrize("corp_id", [PLAYER_CORP, -1, 2 ** 63 + 5])
def test_offers_for_an_unknown_corp_make_no_esi_call(env, corp_id):
    client, calls = env
    for _ in range(2):
        r = client.get(f"/market/lp/offers?corporation_id={corp_id}")
        assert r.status_code == 200
        assert "lp-head" not in r.text   # the empty-catalog answer
    assert calls["offers"] == []
    assert calls["prices"] == 0


def test_offers_for_an_sde_npc_corp_still_fetch(env):
    client, calls = env
    r = client.get(f"/market/lp/offers?corporation_id={SDE_CORP}")
    assert r.status_code == 200
    assert "Sample Mineral" in r.text
    assert calls["offers"] == [SDE_CORP]


def test_offers_for_a_corp_in_the_loaded_roster_still_fetch(env, monkeypatch):
    """A corp the tree shows (ESI roster in memory) is accepted even when the
    SDE table doesn't list it, e.g. before an SDE reimport."""
    client, calls = env
    monkeypatch.setattr(lp, "_npc_corp_ids", [ROSTER_ONLY_CORP])
    r = client.get(f"/market/lp/offers?corporation_id={ROSTER_ONLY_CORP}")
    assert r.status_code == 200
    assert calls["offers"] == [ROSTER_ONLY_CORP]


def test_is_npc_corp_never_fetches_the_roster(env, monkeypatch):
    """Validation reads only what is already loaded; it must not trigger the
    roster's own ESI fetch."""
    async def no_roster_fetch():
        raise AssertionError("validation fetched the NPC roster")

    monkeypatch.setattr(lp, "_fetch_npc_corp_ids_esi", no_roster_fetch)
    client, calls = env
    assert client.get(f"/market/lp/offers?corporation_id={PLAYER_CORP}").status_code == 200
    assert client.get(f"/market/lp/offers?corporation_id={SDE_CORP}").status_code == 200


# ── LP offers: "no store" is remembered ───────────────────────────────

def _status_error(status):
    req = httpx.Request("GET", "https://esi.invalid/loyalty/stores/1/offers/")
    return httpx.HTTPStatusError(str(status), request=req,
                                 response=httpx.Response(status, request=req))


def test_offers_404_is_cached_as_no_store(env, monkeypatch):
    client, calls = env
    seen = []

    async def no_store(corporation_id):
        seen.append(corporation_id)
        raise _status_error(404)

    monkeypatch.setattr(lp, "_fetch_offers_esi", no_store)
    for _ in range(3):
        r = client.get(f"/market/lp/offers?corporation_id={SDE_CORP}")
        assert r.status_code == 200
        assert "lp-head" not in r.text
    assert seen == [SDE_CORP]


def test_offers_404_expires_with_the_offers_ttl(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(lp, "_offers_cache", {})
    monkeypatch.setattr(lp, "_offers_locks", {})
    seen = []

    async def no_store(corporation_id):
        seen.append(corporation_id)
        raise _status_error(404)

    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(lp, "_fetch_offers_esi", no_store)
    monkeypatch.setattr(lp, "_now", lambda: now)
    assert _run(lp.get_offers(SDE_CORP)) == []
    assert _run(lp.get_offers(SDE_CORP)) == []
    assert seen == [SDE_CORP]

    later = now + lp.OFFERS_TTL + timedelta(minutes=1)
    monkeypatch.setattr(lp, "_now", lambda: later)
    assert _run(lp.get_offers(SDE_CORP)) == []
    assert seen == [SDE_CORP, SDE_CORP]


@pytest.mark.parametrize("failure", [
    _status_error(503), _status_error(500), _status_error(420), _status_error(429),
    _status_error(403), httpx.ReadTimeout("timed out"), RuntimeError("down")])
def test_offers_other_failures_are_not_cached(monkeypatch, failure):
    """Only ESI's 404 for that corp means "no store"; a throttle, a 5xx or a
    timeout is never kept, and the next request finds the real catalog."""
    monkeypatch.setattr(lp, "_offers_cache", {})
    monkeypatch.setattr(lp, "_offers_locks", {})
    seen = []

    async def failing(corporation_id):
        seen.append(corporation_id)
        raise failure

    monkeypatch.setattr(lp, "_fetch_offers_esi", failing)
    assert _run(lp.get_offers(SDE_CORP)) == []
    assert _run(lp.get_offers(SDE_CORP)) == []
    assert seen == [SDE_CORP, SDE_CORP]
    assert SDE_CORP not in lp._offers_cache

    async def store(corporation_id):
        seen.append(corporation_id)
        return [{"offer_id": 7}]

    monkeypatch.setattr(lp, "_fetch_offers_esi", store)
    assert _run(lp.get_offers(SDE_CORP)) == [{"offer_id": 7}]
