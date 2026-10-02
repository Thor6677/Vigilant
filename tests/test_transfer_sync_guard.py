"""ISS-082: work already running when a character changes EVE owner (or is
taken off its account) writes nothing it fetched for the old link.

An owner change takes the character off the old account with its history
(remove_character_from_account, REASON_TRANSFER) and the new owner's sign-in
links it again as a NEW characters row (characters.id is never reused). A
sync or page that started before holds the old owner's token, and the new
owner's stored scopes usually still cover what it fetched, so the narrowing
check alone (ISS-079) lets its rows through: the old owner's last wallet
transactions, completed jobs, mining entries or net-worth row would survive
under the new owner. Every writer that commits outside _sync_fields' final
check (which sync_must_not_write already covers) now also refuses when the
character row it started with is gone or replaced.

The transfer, or plain removal, is committed at a seam inside each writer:
between its fetch and its write.
"""
import asyncio
import json
import tempfile
from datetime import date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 (populate Base.metadata)
import app.db.sde_models  # noqa: F401
from app.auth import purge
from app.auth import scopes as cat
from app.db.models import (
    Base, Character, CharacterAssetCache, CharacterDashboardCache, NetWorthSnapshot,
)

CID = 535353
OLD_USER = 51
NEW_USER = 52

TRANSFER = "transfer"
REMOVAL = "removal"
NOTHING = "nothing"


def _run(coro):
    return asyncio.run(coro)


async def _engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _link(SessionLocal, user_id, owner, scopes):
    async with SessionLocal() as db:
        db.add(Character(character_id=CID, character_name="Test Alt", user_id=user_id,
                         owner_hash=owner, access_token=f"token-{owner}",
                         refresh_token=f"refresh-{owner}", token_expiry=datetime(2099, 1, 1),
                         scopes=cat.join_scopes(scopes)))
        await db.commit()


async def _event(SessionLocal, event, scopes):
    """Commit what the event commits, from another session."""
    if event == NOTHING:
        return
    async with SessionLocal() as db:
        c = (await db.execute(select(Character).where(Character.character_id == CID))).scalar_one()
        if event == TRANSFER:
            # _release_transferred: off the old account, history deleted now.
            await purge.remove_character_from_account(
                db, c, reason=purge.REASON_TRANSFER, delete_history=True)
        else:
            await purge.remove_character_from_account(
                db, c, reason=purge.REASON_SELF, delete_history=False)
        await db.commit()
    if event == TRANSFER:
        # The new owner's sign-in links it again: a new row, same scopes.
        await _link(SessionLocal, NEW_USER, "owner-B", scopes)


async def _pilot(SessionLocal):
    async with SessionLocal() as db:
        return (await db.execute(select(Character).where(Character.character_id == CID))).scalar_one()


async def _count(SessionLocal, table):
    async with SessionLocal() as db:
        return (await db.execute(text(f"SELECT count(*) FROM {table} WHERE character_id = :c"),
                                 {"c": CID})).scalar()


class _Client:
    cache_enabled = True


async def _client_for(char):
    return _Client(), None


# ── Wallet transactions: committed page by page by the sync's fetcher ───────

async def _fetch_transactions(monkeypatch, SessionLocal, event, scopes, first_id):
    import app.routes.dashboard as dash
    calls = []

    async def page(client, character_id, from_id):
        calls.append(from_id)
        if len(calls) > 1:
            return []
        await _event(SessionLocal, event, scopes)
        return [{"transaction_id": first_id + i, "date": "2026-09-01T12:00:00Z", "type_id": 34,
                 "quantity": 1, "unit_price": 5.0, "is_buy": True} for i in range(2)]
    monkeypatch.setattr(dash, "_client_for", _client_for)
    monkeypatch.setattr(dash.esi_char, "get_wallet_transactions", page)
    async with SessionLocal() as db:
        char = (await db.execute(select(Character).where(Character.character_id == CID))).scalar_one()
        return await dash.fetch_wallet_transactions_data([char], db)


@pytest.mark.parametrize("event", [TRANSFER, REMOVAL, NOTHING])
def test_wallet_transactions_fetched_for_the_old_link_are_not_stored(monkeypatch, event):
    scopes = [cat.WALLET]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)
            result = await _fetch_transactions(monkeypatch, SessionLocal, event, scopes, 7100)
            return result, await _count(SessionLocal, "wallet_transactions")
        finally:
            await engine.dispose()

    result, stored = _run(go())
    assert stored == (2 if event == NOTHING else 0)
    assert result[CID][1] is None                    # dropped quietly, not an error


def test_the_new_owners_own_sync_then_stores_transactions(monkeypatch):
    scopes = [cat.WALLET]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)
            await _fetch_transactions(monkeypatch, SessionLocal, TRANSFER, scopes, 7100)
            # The next sync starts from the new owner's row and token.
            await _fetch_transactions(monkeypatch, SessionLocal, NOTHING, scopes, 7200)
            return await _count(SessionLocal, "wallet_transactions"), (await _pilot(SessionLocal)).user_id
        finally:
            await engine.dispose()

    assert _run(go()) == (2, NEW_USER)


# ── Completed industry jobs: persisted by the sync's industry fetcher ───────

async def _fetch_jobs(monkeypatch, SessionLocal, event, scopes, job_id):
    import app.esi.industry as esi_industry
    import app.market.lp as market_lp
    import app.routes.dashboard as dash

    async def fetch(client, character_id, include_completed=False):
        await _event(SessionLocal, event, scopes)
        return [{"job_id": job_id, "status": "delivered", "activity_id": 1,
                 "blueprint_type_id": 1001, "product_type_id": 1002, "runs": 1, "cost": 1.0,
                 "start_date": "2026-09-01T00:00:00Z", "completed_date": "2026-09-02T00:00:00Z"}]

    async def prices(db):
        return {}
    monkeypatch.setattr(dash, "_client_for", _client_for)
    monkeypatch.setattr(esi_industry, "get_character_jobs", fetch)
    monkeypatch.setattr(market_lp, "get_price_map", prices)
    async with SessionLocal() as db:
        char = (await db.execute(select(Character).where(Character.character_id == CID))).scalar_one()
        return await dash.fetch_industry_jobs_data([char], db)


@pytest.mark.parametrize("event", [TRANSFER, REMOVAL, NOTHING])
def test_completed_jobs_fetched_for_the_old_link_are_not_stored(monkeypatch, event):
    scopes = [cat.JOBS]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)
            result = await _fetch_jobs(monkeypatch, SessionLocal, event, scopes, 880001)
            return result, await _count(SessionLocal, "industry_job_history")
        finally:
            await engine.dispose()

    result, stored = _run(go())
    assert stored == (1 if event == NOTHING else 0)
    assert result[CID][1] is None


def test_the_new_owners_own_sync_then_stores_completed_jobs(monkeypatch):
    scopes = [cat.JOBS]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)
            await _fetch_jobs(monkeypatch, SessionLocal, TRANSFER, scopes, 880001)
            await _fetch_jobs(monkeypatch, SessionLocal, NOTHING, scopes, 880002)
            return await _count(SessionLocal, "industry_job_history")
        finally:
            await engine.dispose()

    assert _run(go()) == 1


# ── The mining ledger: stored by the pages that read it ─────────────────────

@pytest.mark.parametrize("event", [TRANSFER, REMOVAL, NOTHING])
def test_mining_entries_fetched_for_the_old_link_are_not_stored(monkeypatch, event):
    """Through the mining-ledger page's helper, which reads each pilot's
    ledger in a session of its own (the corp page does the same)."""
    import app.routes.mining as mining
    import app.routes.mining_ledger as ledger
    scopes = [cat.MINING]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)

            async def fetch(client, character_id):
                await _event(SessionLocal, event, scopes)
                return [{"date": "2026-09-01", "type_id": 1230, "solar_system_id": 30000142,
                         "quantity": 100}]

            async def prices(db, type_ids):
                return {}
            monkeypatch.setattr(mining, "_fetch_all_mining", fetch)
            monkeypatch.setattr(ledger, "_get_price_map", prices)
            monkeypatch.setattr(ledger, "AsyncSessionLocal", SessionLocal)
            async with SessionLocal() as db:
                c = (await db.execute(select(Character).where(Character.character_id == CID))).scalar_one()
                await ledger._fetch_chars_mining([c], db)
            return await _count(SessionLocal, "mining_ledger_entries")
        finally:
            await engine.dispose()

    assert _run(go()) == (1 if event == NOTHING else 0)


# ── Net worth: one row per character per day, from live state ───────────────

@pytest.mark.parametrize("event", [TRANSFER, REMOVAL, NOTHING])
def test_a_net_worth_run_leaves_no_row_for_the_old_link(monkeypatch, event):
    """The run read the old owner's live state; the owner changed before it
    wrote. Its row would be filed under the character the new owner now
    has, so it doesn't stay."""
    import app.networth.snapshot as snapshot
    scopes = [cat.WALLET]
    day = date(2026, 9, 2)

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _link(SessionLocal, OLD_USER, "owner-A", scopes)
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID, wallet=1000.0))
                db.add(CharacterAssetCache(character_id=CID,
                                           assets_json=json.dumps([{"type_id": 34, "quantity": 1}])))
                await db.commit()

            async with SessionLocal() as db:
                chars = list((await db.execute(select(Character))).scalars())
                read = db.execute
                fired = []

                async def execute(stmt, *a, **k):
                    res = await read(stmt, *a, **k)
                    # The event commits just after the run read the caches.
                    if not fired and "character_asset_cache" in str(stmt):
                        fired.append(stmt)
                        await _event(SessionLocal, event, scopes)
                    return res
                monkeypatch.setattr(db, "execute", execute)
                result = await snapshot.take_snapshots(db, {34: 5.0}, chars, on_date=day)
                assert fired
            async with SessionLocal() as db:
                rows = [(r.user_id, r.wallet) for r in (await db.execute(select(NetWorthSnapshot))).scalars()]
            return result, rows
        finally:
            await engine.dispose()

    result, rows = _run(go())
    assert result["written"] == 1
    assert rows == ([] if event != NOTHING else [(OLD_USER, 1000.0)])
