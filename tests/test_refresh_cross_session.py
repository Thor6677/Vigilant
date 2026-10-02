"""ISS-081: page code that loads a Character in one session and refreshes its
token through a different one used to raise "not persistent within this
Session" when the token was due, and the pilot silently dropped out of the
page. Each such call site now refreshes the row as loaded by the session it
passes.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 (populate Base.metadata)
import app.db.sde_models  # noqa: F401
import app.esi.client as esi_client
import app.routes.industry_jobs as industry_jobs
import app.routes.mining_ledger as mining_ledger
from app.db.models import Base, Character

CID = 616161


def _run(coro):
    return asyncio.run(coro)


async def _engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_due_pilot(SessionLocal, cid=CID):
    async with SessionLocal() as db:
        db.add(Character(character_id=cid, character_name="Test Alt", user_id=7,
                         access_token="old-access", refresh_token="old-refresh",
                         token_expiry=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
                         scopes="esi-industry.read_character_jobs.v1 "
                                "esi-industry.read_character_mining.v1 "
                                "esi-contracts.read_corporation_contracts.v1"))
        await db.commit()


def _mock_sso(monkeypatch):
    """Route the refresh POST to a canned SSO answer; no real network."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "new-access",
                                         "refresh_token": "new-refresh", "expires_in": 1200})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(esi_client, "get_http_client", lambda: client)
    return calls


async def _loaded_elsewhere(SessionLocal, cid=CID) -> Character:
    """A Character the way a request session leaves it: loaded in a session
    that is not the one the call site opens."""
    async with SessionLocal() as other:
        return (await other.execute(select(Character).where(Character.character_id == cid))).scalar_one()


async def _stored(SessionLocal, cid=CID) -> Character:
    async with SessionLocal() as db:
        return (await db.execute(select(Character).where(Character.character_id == cid))).scalar_one()


def test_industry_jobs_refreshes_a_due_token_loaded_in_another_session(monkeypatch):
    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_due_pilot(SessionLocal)
            calls = _mock_sso(monkeypatch)
            seen = {}

            async def get_character_jobs(client, character_id, include_completed=False):
                seen["token"] = client.token
                return [{"job_id": 1}]

            monkeypatch.setattr(industry_jobs, "AsyncSessionLocal", SessionLocal)
            monkeypatch.setattr(industry_jobs.esi_industry, "get_character_jobs", get_character_jobs)
            char = await _loaded_elsewhere(SessionLocal)
            _, jobs, err = await industry_jobs._fetch_character_jobs(char, False)
            assert err is None
            assert jobs == [{"job_id": 1}]
            assert seen["token"] == "new-access"
            assert len(calls) == 1
            row = await _stored(SessionLocal)
            assert row.access_token == "new-access"
            assert row.refresh_token == "new-refresh"
        finally:
            await engine.dispose()

    _run(go())


def test_mining_ledger_refreshes_a_due_token_loaded_in_another_session(monkeypatch):
    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_due_pilot(SessionLocal)
            _mock_sso(monkeypatch)

            async def sync_and_fetch(client, character_id, db):
                return [{"type_id": 1, "solar_system_id": 2, "quantity": 5, "date": "2026-09-01"}]

            async def names(db, ids):
                return {i: f"n{i}" for i in ids}

            async def prices(db, ids):
                return {i: 1.0 for i in ids}

            monkeypatch.setattr(mining_ledger, "AsyncSessionLocal", SessionLocal)
            monkeypatch.setattr(mining_ledger, "_sync_and_fetch_mining", sync_and_fetch)
            monkeypatch.setattr(mining_ledger.sde, "type_ids_to_names", names)
            monkeypatch.setattr(mining_ledger.sde, "system_ids_to_names", names)
            monkeypatch.setattr(mining_ledger, "_get_price_map", prices)
            char = await _loaded_elsewhere(SessionLocal)
            async with SessionLocal() as db:
                result = await mining_ledger._fetch_chars_mining([char], db)
            all_raw, per_char, names_used = result[0], result[1], result[2]
            assert names_used == ["Test Alt"]
            assert len(all_raw) == 1
            assert per_char[CID]["quantity"] == 5
            assert (await _stored(SessionLocal)).access_token == "new-access"
        finally:
            await engine.dispose()

    _run(go())


def test_a_vanished_pilot_is_refused_not_written_elsewhere(monkeypatch):
    """If the row is gone by the time the call site loads it, the fetch fails
    for that pilot and nothing is written (ISS-074 property)."""
    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_due_pilot(SessionLocal)
            calls = _mock_sso(monkeypatch)
            monkeypatch.setattr(industry_jobs, "AsyncSessionLocal", SessionLocal)
            char = await _loaded_elsewhere(SessionLocal)
            async with SessionLocal() as db:
                row = (await db.execute(select(Character))).scalar_one()
                await db.delete(row)
                await db.commit()
            _, jobs, err = await industry_jobs._fetch_character_jobs(char, False)
            assert jobs == [] and err and err.startswith("esi_error")
            assert calls == []
            async with SessionLocal() as db:
                assert (await db.execute(select(Character))).scalars().all() == []
        finally:
            await engine.dispose()

    _run(go())

