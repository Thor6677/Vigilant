"""The wormhole kill partial is login-only and bounds its ESI work (ISS-167).

GET /wormholes/system/<name>/kills makes one zKillboard call and then one
ESI killmail fetch per kill zKillboard lists (up to 200), from the same ESI
budget and throttle every signed-in sync uses. It answered without a
session, although only login-only pages embed it; it fetched every listed
kill before dropping the ones outside the 30/60/90-day range; nothing was
reused between requests; and each request ran its own five fetches at once,
so a few requests together could take every ESI slot the gate check needs.

zKillboard, ESI and the SDE lookups are stubbed. Every system and kill is
invented.
"""
import asyncio
import base64
import json
import re
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import httpx
import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User, get_db
from app.routes import wormholes as wh_mod

USER_ID = 9421
_CSRF = "test-csrf-token-wh-kills-0123456789"
SYSTEMS = {"J100001": 31000001, "J100002": 31000002, "J100003": 31000003}
STUBS_PER_SYSTEM = 200
IN_RANGE = 12            # how many of each system's newest kills are recent


def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _cookie(**data):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


class _Upstream:
    """zKillboard and ESI stand-ins that count what they are asked for."""

    def __init__(self):
        self.zkb_paths: list[str] = []
        self.fetched: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.now = datetime.now(timezone.utc)
        self.in_range = IN_RANGE

    @staticmethod
    def stub_ids(system_id):
        base = (system_id - 31000000) * 10_000
        return [base + i for i in range(STUBS_PER_SYSTEM, 0, -1)]   # newest first

    async def zkb_get(self, path):
        self.zkb_paths.append(path)
        system_id = int(re.search(r"/systemID/(\d+)/", path).group(1))
        return [{"killmail_id": k, "zkb": {"hash": f"h{k}", "npc": False, "totalValue": 1.0}}
                for k in self.stub_ids(system_id)]

    async def fetch_killmail(self, killmail_id, km_hash):
        self.fetched.append(killmail_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.001)
        finally:
            self.in_flight -= 1
        age = STUBS_PER_SYSTEM - killmail_id % 10_000      # 0 for the newest
        when = (self.now - timedelta(hours=1 + age)) if age < self.in_range \
            else (self.now - timedelta(days=200 + age))
        return {"killmail_id": killmail_id,
                "killmail_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "victim": {"corporation_id": 98000001, "ship_type_id": 587},
                "attackers": []}


class _NoNames:
    def __init__(self, *a, **kw):
        self.cache_enabled = False

    async def post_public(self, path, ids):
        return []


@pytest.fixture
def upstream(monkeypatch):
    up = _Upstream()

    async def _system(db, name):
        sid = SYSTEMS.get(name.upper())
        return {"system_id": sid, "system_name": name.upper()} if sid else None

    async def _names(db, ids):
        return {}

    monkeypatch.setattr(wh_mod, "zkb_get", up.zkb_get)
    monkeypatch.setattr(wh_mod, "fetch_killmail", up.fetch_killmail)
    monkeypatch.setattr(wh_mod, "ESIClient", _NoNames)
    monkeypatch.setattr(wh_mod.sde, "get_wormhole_system_detail", _system)
    monkeypatch.setattr(wh_mod.sde, "type_ids_to_names", _names)
    # Module-level state; raising=False so the test reaches its assertions
    # on code that has none of it.
    monkeypatch.setattr(wh_mod, "_kills_cache", OrderedDict(), raising=False)
    monkeypatch.setattr(wh_mod, "_kills_building", {}, raising=False)
    monkeypatch.setattr(wh_mod, "_kills_loop", None, raising=False)
    return up


@pytest.fixture
def app_db():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield main.app
    finally:
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)


def _client(app, signed_in=True):
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    session = {"csrf_token": _CSRF, **({"user_id": USER_ID} if signed_in else {})}
    client.cookies.set("vigilant_session", _cookie(**session))
    return client


def test_kill_activity_needs_a_session(app_db, upstream):
    r = _client(app_db, signed_in=False).get("/wormholes/system/J100001/kills")
    assert r.status_code == 401 and r.text == ""
    assert upstream.zkb_paths == [] and upstream.fetched == []


def test_kill_fetches_stop_once_kills_are_older_than_the_range(app_db, upstream):
    r = _client(app_db).get("/wormholes/system/J100001/kills?days=30")
    assert r.status_code == 200
    assert f">{IN_RANGE}</span>" in r.text
    newest = upstream.stub_ids(SYSTEMS["J100001"])[:IN_RANGE]
    assert set(newest) <= set(upstream.fetched), "a kill inside the range was not fetched"
    # Everything past the in-range kills is older still; only the rest of
    # the batch that found the cutoff may be fetched beyond them.
    assert len(upstream.fetched) <= 2 * IN_RANGE, (
        f"fetched {len(upstream.fetched)} of {STUBS_PER_SYSTEM} kills for {IN_RANGE} in range")


def test_kill_activity_is_reused_per_system_and_range(app_db, upstream):
    client = _client(app_db)
    first = client.get("/wormholes/system/J100001/kills?days=30")
    calls = (len(upstream.zkb_paths), len(upstream.fetched))
    again = client.get("/wormholes/system/j100001/kills?days=30")
    assert (len(upstream.zkb_paths), len(upstream.fetched)) == calls
    assert f">{IN_RANGE}</span>" in first.text and f">{IN_RANGE}</span>" in again.text
    # The range buttons keep the name as this request spelled it.
    assert "/wormholes/system/j100001/kills?days=60" in again.text

    client.get("/wormholes/system/J100001/kills?days=60")
    client.get("/wormholes/system/J100002/kills?days=30")
    assert len(upstream.zkb_paths) == calls[0] + 2


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="https://testserver")
    client.cookies.set("vigilant_session", _cookie(csrf_token=_CSRF, user_id=USER_ID))
    return client


def test_concurrent_requests_for_one_system_build_it_once(app_db, upstream):
    async def _go():
        async with _async_client(app_db) as client:
            return await asyncio.gather(*[
                client.get("/wormholes/system/J100001/kills?days=30") for _ in range(4)])

    responses = asyncio.run(_go())
    assert all(r.status_code == 200 for r in responses)
    assert len(upstream.zkb_paths) == 1


def test_concurrent_builds_share_one_fetch_ceiling(app_db, upstream):
    upstream.in_range = STUBS_PER_SYSTEM   # so each build fetches its whole list

    async def _go():
        async with _async_client(app_db) as client:
            return await asyncio.gather(*[
                client.get(f"/wormholes/system/{name}/kills?days=90") for name in SYSTEMS])

    responses = asyncio.run(_go())
    assert all(r.status_code == 200 for r in responses)
    assert len(upstream.fetched) == len(SYSTEMS) * STUBS_PER_SYSTEM
    # app.intel.safety lets ESI calls run ten at a time process-wide, shared
    # with the gate check; the kill partial must leave it room.
    assert upstream.max_in_flight <= 5, (
        f"{upstream.max_in_flight} killmail fetches in flight across {len(SYSTEMS)} requests")
