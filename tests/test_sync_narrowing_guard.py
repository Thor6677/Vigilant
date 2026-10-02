"""ISS-079: work already running when a user narrows a character's permissions
writes nothing it fetched under the withdrawn ones.

A sync (or a page that stores what it reads) holds the access token it read
when it started. After a narrowing that token stays usable at ESI for up to
20 minutes, so whatever it fetched may come from a permission the user has
just withdrawn, and the narrowing has already cleared live state and, if
asked, purged history. Every such writer re-reads the character's stored
scopes inside its own write transaction and drops its results when a scope it
depends on is gone. Widening, or no change, never drops anything.

The narrowing is simulated at a seam inside each writer (a fetcher, the price
lookup, the valuation step): the stored scopes change in another session and
commit while the writer is between its read and its write.
"""
import asyncio
import json
import logging
import tempfile
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 (populate Base.metadata)
import app.db.sde_models  # noqa: F401
from app.auth import purge
from app.auth import scopes as cat
from app.db.models import (
    Base, Character, CharacterAssetCache, CharacterDashboardCache, IndustryJobHistory,
    MiningLedgerEntry, NetWorthSnapshot, WalletSnapshot, WalletTransaction,
)

CID = 515151
USER = 41

NARROWED = "narrowed"
WIDENED = "widened"
UNCHANGED = "unchanged"


def _run(coro):
    return asyncio.run(coro)


async def _engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_pilot(SessionLocal, scopes, cid=CID):
    async with SessionLocal() as db:
        db.add(Character(character_id=cid, character_name="Test Alt", user_id=USER,
                         access_token="x", refresh_token="y", token_expiry=datetime(2099, 1, 1),
                         scopes=cat.join_scopes(scopes)))
        await db.commit()


async def _change_scopes(SessionLocal, scopes, *, clear=(), cid=CID):
    """What a narrowing (or widening) commits: the new stored scopes, then
    the live-state clear, in two commits as the SSO callback does."""
    async with SessionLocal() as db:
        c = (await db.execute(select(Character).where(Character.character_id == cid))).scalar_one()
        c.scopes = cat.join_scopes(scopes)
        await db.commit()
        if clear:
            await purge.clear_live_state(db, cid, clear)
            await db.commit()


def _after(change, base, *, withdrawn, added=cat.SKILLS):
    """The stored scopes once `change` lands on a pilot that had `base`."""
    if change == NARROWED:
        return [s for s in base if s != withdrawn]
    if change == WIDENED:
        return list(base) + [added]
    return list(base)


async def _scalar(SessionLocal, sql):
    async with SessionLocal() as db:
        return (await db.execute(text(sql))).scalar()


# ── _sync_fields: the dashboard sync's final commit ─────────────────────────

def _sync_with(monkeypatch, SessionLocal, fetchers):
    """Point _sync_fields at the given fake fetchers, each due now and
    ungated, on the test engine."""
    import app.routes.dashboard as dash
    monkeypatch.setattr(dash, "FIELD_CACHE_SECONDS", {f: 0 for f in fetchers})
    monkeypatch.setattr(dash, "FIELD_SCOPES", {f: None for f in fetchers})
    monkeypatch.setattr(dash, "_FIELD_FETCHERS", fetchers)
    monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
    return dash._sync_fields


async def _run_sync(SessionLocal, sync_fields):
    async with SessionLocal() as sync_db:
        char = (await sync_db.execute(select(Character).where(
            Character.character_id == CID))).scalar_one()
        cache = await sync_db.get(CharacterDashboardCache, CID)
        asset_cache = (await sync_db.execute(select(CharacterAssetCache).where(
            CharacterAssetCache.character_id == CID))).scalar_one()
        await sync_fields(CID, char, cache, asset_cache, sync_db)


@pytest.mark.parametrize("change", [NARROWED, WIDENED, UNCHANGED])
def test_a_sync_running_when_permissions_change(monkeypatch, change):
    """The narrowing commits while the sync's fetchers run: the sync writes
    nothing (no balance, no snapshot, no bookkeeping). Widening, or no
    change, never stops it."""
    base = [cat.WALLET, cat.ASSETS]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID))
                db.add(CharacterAssetCache(character_id=CID))
                await db.commit()

            async def wallet(chars, db):
                await _change_scopes(SessionLocal, _after(change, base, withdrawn=cat.WALLET),
                                     clear=["wallet"] if change == NARROWED else ())
                return {chars[0].character_id: (123.0, None)}

            await _run_sync(SessionLocal, _sync_with(monkeypatch, SessionLocal, {"wallet": wallet}))
            async with SessionLocal() as db:
                cache = await db.get(CharacterDashboardCache, CID)
                snaps = (await db.execute(text("SELECT count(*) FROM wallet_snapshots"))).scalar()
            return cache.wallet, snaps, cache.field_synced_json
        finally:
            await engine.dispose()

    wallet, snaps, synced = _run(go())
    if change == NARROWED:
        assert (wallet, snaps, synced) == (None, 0, None)
    else:
        assert (wallet, snaps) == (123.0, 1)
        assert "wallet" in json.loads(synced)


def test_a_sync_survives_a_narrowing_that_deleted_a_row_it_updates(monkeypatch):
    """Withdrawing assets deletes the asset-cache row the running sync is
    about to update. Flushing that UPDATE would raise StaleDataError; the
    sync must notice the narrowing first and drop quietly instead."""
    base = [cat.WALLET, cat.ASSETS]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID))
                db.add(CharacterAssetCache(character_id=CID, assets_json="[]"))
                await db.commit()

            async def wallet(chars, db):
                return {chars[0].character_id: (123.0, None)}

            async def assets(chars, db):
                await _change_scopes(SessionLocal, [cat.WALLET], clear=["assets"])
                return {chars[0].character_id: ([{"type_id": 34, "quantity": 5}], None)}

            await _run_sync(SessionLocal, _sync_with(
                monkeypatch, SessionLocal, {"wallet": wallet, "assets": assets}))
            async with SessionLocal() as db:
                return (await db.execute(text(
                    "SELECT (SELECT count(*) FROM character_asset_cache),"
                    " (SELECT count(*) FROM wallet_snapshots),"
                    " (SELECT wallet FROM character_dashboard_cache)"))).one()
        finally:
            await engine.dispose()

    assert tuple(_run(go())) == (0, 0, None)


def test_a_narrowing_between_the_check_and_the_commit_still_stops_the_sync(monkeypatch):
    """The check before the flush passes; the narrowing commits right after
    it. The check is asked again once the sync's flush holds SQLite's write
    lock, so the sync still writes nothing."""
    base = [cat.WALLET, cat.ASSETS]
    real = getattr(purge, "scopes_withdrawn", None)
    calls = []

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID))
                db.add(CharacterAssetCache(character_id=CID))
                await db.commit()

            async def racing(db, character_id, scopes):
                answer = await real(db, character_id, scopes)
                calls.append(answer)
                if len(calls) == 1:
                    await _change_scopes(SessionLocal, [cat.ASSETS], clear=["wallet"])
                return answer
            monkeypatch.setattr(purge, "scopes_withdrawn", racing, raising=False)

            async def wallet(chars, db):
                return {chars[0].character_id: (123.0, None)}

            await _run_sync(SessionLocal, _sync_with(monkeypatch, SessionLocal, {"wallet": wallet}))
            return (await _scalar(SessionLocal, "SELECT count(*) FROM wallet_snapshots"),
                    await _scalar(SessionLocal, "SELECT wallet FROM character_dashboard_cache"))
        finally:
            await engine.dispose()

    assert _run(go()) == (0, None)
    assert calls == [False, True]


# ── Fetchers that commit as they go ─────────────────────────────────────────

class _Client:
    cache_enabled = True


@pytest.mark.parametrize("change", [NARROWED, WIDENED, UNCHANGED])
def test_wallet_transactions_fetched_before_a_narrowing_are_not_stored(monkeypatch, change):
    """The transactions fetcher commits each page on its own session, long
    before the sync's final check, so it checks at each page."""
    import app.routes.dashboard as dash
    base = [cat.WALLET, cat.ASSETS]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)

            async def client_for(char):
                return _Client(), None

            pages = []

            async def page(client, character_id, from_id):
                pages.append(from_id)
                if len(pages) > 1:
                    return []
                await _change_scopes(SessionLocal, _after(change, base, withdrawn=cat.WALLET))
                return [{"transaction_id": 9001 + i, "date": "2026-09-01T12:00:00Z", "type_id": 34,
                         "quantity": 1, "unit_price": 5.0, "is_buy": True} for i in range(2)]
            monkeypatch.setattr(dash, "_client_for", client_for)
            monkeypatch.setattr(dash.esi_char, "get_wallet_transactions", page)

            async with SessionLocal() as db:
                char = (await db.execute(select(Character).where(
                    Character.character_id == CID))).scalar_one()
                await dash.fetch_wallet_transactions_data([char], db)
            return await _scalar(SessionLocal, "SELECT count(*) FROM wallet_transactions")
        finally:
            await engine.dispose()

    assert _run(go()) == (0 if change == NARROWED else 2)


@pytest.mark.parametrize("change", [NARROWED, WIDENED, UNCHANGED])
def test_completed_jobs_fetched_before_a_narrowing_are_not_stored(monkeypatch, change):
    """The industry fetcher stores delivered jobs on its own session too. The
    active jobs it returns still reach the sync, whose final check decides
    about those."""
    import app.esi.industry as esi_industry
    import app.market.lp as market_lp
    import app.routes.dashboard as dash
    base = [cat.JOBS, cat.BLUEPRINTS]
    jobs = [
        {"job_id": 777001, "status": "delivered", "activity_id": 1, "blueprint_type_id": 1001,
         "product_type_id": 1002, "runs": 2, "cost": 10.0,
         "start_date": "2026-09-01T00:00:00Z", "completed_date": "2026-09-02T00:00:00Z"},
        {"job_id": 777002, "status": "active", "activity_id": 1, "blueprint_type_id": 1001,
         "product_type_id": 1002, "runs": 1, "end_date": "2026-09-09T00:00:00Z"},
    ]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)

            async def client_for(char):
                return _Client(), None

            async def fetch(client, character_id, include_completed=False):
                await _change_scopes(SessionLocal, _after(change, base, withdrawn=cat.JOBS))
                return jobs

            async def prices(db):
                return {}
            monkeypatch.setattr(dash, "_client_for", client_for)
            monkeypatch.setattr(esi_industry, "get_character_jobs", fetch)
            monkeypatch.setattr(market_lp, "get_price_map", prices)

            async with SessionLocal() as db:
                char = (await db.execute(select(Character).where(
                    Character.character_id == CID))).scalar_one()
                result = await dash.fetch_industry_jobs_data([char], db)
            return result, await _scalar(SessionLocal, "SELECT count(*) FROM industry_job_history")
        finally:
            await engine.dispose()

    result, stored = _run(go())
    active, warning = result[CID]
    assert [j["job_id"] for j in active] == [777002] and warning is None
    assert stored == (0 if change == NARROWED else 1)


# ── One call, several pilots: a drop for one leaves the others alone ───────

OTHER_CID = 525252


def _no_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "app.routes.dashboard" and r.levelno >= logging.WARNING]


def _hold_until_dropped(monkeypatch):
    """An event set once the narrowed pilot's write has been refused, so the
    other pilot's fetch can be made to finish only after that drop."""
    dropped = asyncio.Event()
    real = purge.scopes_withdrawn

    async def watching(db, character_id, scopes):
        answer = await real(db, character_id, scopes)
        if answer:
            dropped.set()
        return answer
    monkeypatch.setattr(purge, "scopes_withdrawn", watching)

    async def wait():
        await dropped.wait()
        for _ in range(20):          # and let its rollback finish
            await asyncio.sleep(0.01)
    return wait


def test_a_transactions_drop_for_one_pilot_leaves_the_others_written(monkeypatch, caplog):
    """Both pilots share the call and its session. Dropping the narrowed
    pilot's page must not roll back, or expire, anything of the other's."""
    import app.routes.dashboard as dash
    caplog.set_level(logging.INFO)

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, [cat.WALLET])
            await _add_pilot(SessionLocal, [cat.WALLET], cid=OTHER_CID)

            async def client_for(char):
                return _Client(), None

            pages: dict[int, int] = {}
            after_the_drop = _hold_until_dropped(monkeypatch)

            async def page(client, character_id, from_id):
                pages[character_id] = pages.get(character_id, 0) + 1
                if pages[character_id] > 1:
                    return []
                if character_id == CID:
                    await _change_scopes(SessionLocal, [])
                else:
                    await after_the_drop()
                return [{"transaction_id": character_id * 10 + i, "date": "2026-09-01T12:00:00Z",
                         "type_id": 34, "quantity": 1, "unit_price": 5.0, "is_buy": True}
                        for i in range(2)]
            monkeypatch.setattr(dash, "_client_for", client_for)
            monkeypatch.setattr(dash.esi_char, "get_wallet_transactions", page)

            async with SessionLocal() as db:
                chars = list((await db.execute(select(Character).order_by(
                    Character.character_id))).scalars())
                result = await dash.fetch_wallet_transactions_data(chars, db)
            async with SessionLocal() as db:
                stored = dict((await db.execute(text(
                    "SELECT character_id, count(*) FROM wallet_transactions GROUP BY character_id"
                ))).all())
            return result, stored
        finally:
            await engine.dispose()

    result, stored = _run(go())
    assert stored == {OTHER_CID: 2}
    assert result == {CID: (0, None), OTHER_CID: (2, None)}
    assert _no_warnings(caplog) == []


def test_a_completed_jobs_drop_for_one_pilot_leaves_the_others_written(monkeypatch, caplog):
    import app.esi.industry as esi_industry
    import app.market.lp as market_lp
    import app.routes.dashboard as dash
    caplog.set_level(logging.INFO)

    def jobs_for(cid):
        return [{"job_id": cid * 10, "status": "delivered", "activity_id": 1,
                 "blueprint_type_id": 1001, "product_type_id": 1002, "runs": 1, "cost": 1.0,
                 "start_date": "2026-09-01T00:00:00Z", "completed_date": "2026-09-02T00:00:00Z"},
                {"job_id": cid * 10 + 1, "status": "active", "activity_id": 1,
                 "blueprint_type_id": 1001, "product_type_id": 1002, "runs": 1,
                 "end_date": "2026-09-09T00:00:00Z"}]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, [cat.JOBS])
            await _add_pilot(SessionLocal, [cat.JOBS], cid=OTHER_CID)

            async def client_for(char):
                return _Client(), None

            after_the_drop = _hold_until_dropped(monkeypatch)

            async def fetch(client, character_id, include_completed=False):
                if character_id == CID:
                    await _change_scopes(SessionLocal, [])
                else:
                    await after_the_drop()
                return jobs_for(character_id)

            async def prices(db):
                return {}
            monkeypatch.setattr(dash, "_client_for", client_for)
            monkeypatch.setattr(esi_industry, "get_character_jobs", fetch)
            monkeypatch.setattr(market_lp, "get_price_map", prices)

            async with SessionLocal() as db:
                chars = list((await db.execute(select(Character).order_by(
                    Character.character_id))).scalars())
                result = await dash.fetch_industry_jobs_data(chars, db)
            async with SessionLocal() as db:
                stored = dict((await db.execute(text(
                    "SELECT character_id, count(*) FROM industry_job_history GROUP BY character_id"
                ))).all())
            return result, stored
        finally:
            await engine.dispose()

    result, stored = _run(go())
    assert stored == {OTHER_CID: 1}
    assert {cid: ([j["job_id"] for j in active], warn) for cid, (active, warn) in result.items()} \
        == {CID: ([CID * 10 + 1], None), OTHER_CID: ([OTHER_CID * 10 + 1], None)}
    assert _no_warnings(caplog) == []


# ── The mining ledger: stored by the page that reads it ─────────────────────

@pytest.mark.parametrize("change", [NARROWED, WIDENED, UNCHANGED])
def test_mining_entries_fetched_before_a_narrowing_are_not_stored(monkeypatch, change):
    """_sync_and_fetch_mining serves the character page, the corp page and
    the mining-ledger page alike."""
    import app.routes.mining as mining
    base = [cat.MINING, cat.WALLET]

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)
            async with SessionLocal() as db:
                db.add(MiningLedgerEntry(character_id=CID, date="2026-08-31", type_id=1230,
                                         solar_system_id=30000142, quantity=10))
                await db.commit()

            async def fetch(client, character_id):
                await _change_scopes(SessionLocal, _after(change, base, withdrawn=cat.MINING))
                return [
                    {"date": "2026-09-01", "type_id": 1230, "solar_system_id": 30000142, "quantity": 100},
                    # Already stored; its quantity grew since.
                    {"date": "2026-08-31", "type_id": 1230, "solar_system_id": 30000142, "quantity": 25},
                ]
            monkeypatch.setattr(mining, "_fetch_all_mining", fetch)

            async with SessionLocal() as db:
                await mining._sync_and_fetch_mining(object(), CID, db)
            async with SessionLocal() as db:
                return sorted((r.date, r.quantity) for r in (await db.execute(
                    select(MiningLedgerEntry))).scalars())
        finally:
            await engine.dispose()

    rows = _run(go())
    if change == NARROWED:
        assert rows == [("2026-08-31", 10)]
    else:
        assert rows == [("2026-08-31", 25), ("2026-09-01", 100)]


# ── Net worth: valued from live state, written once a day ───────────────────

@pytest.mark.parametrize("change", [NARROWED, WIDENED, UNCHANGED])
def test_net_worth_never_values_a_component_withdrawn_while_it_ran(monkeypatch, change):
    """The snapshot read the wallet before the narrowing cleared it. The row
    still gets written (a missing day would dip the summed chart), but
    without the withdrawn component."""
    import app.networth.snapshot as snapshot
    base = [cat.WALLET, cat.ASSETS]
    day = date(2026, 9, 2)

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, base)
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID, wallet=1000.0))
                db.add(CharacterAssetCache(character_id=CID,
                                           assets_json=json.dumps([{"type_id": 34, "quantity": 10}])))
                await db.commit()

            async with SessionLocal() as db:
                chars = list((await db.execute(select(Character))).scalars())
                read = db.execute
                fired = []

                async def execute(stmt, *a, **k):
                    res = await read(stmt, *a, **k)
                    # The narrowing commits just after the snapshot read the caches.
                    if not fired and "character_asset_cache" in str(stmt):
                        fired.append(stmt)
                        await _change_scopes(SessionLocal, _after(change, base, withdrawn=cat.WALLET),
                                             clear=["wallet"] if change == NARROWED else ())
                    return res
                monkeypatch.setattr(db, "execute", execute)
                await snapshot.take_snapshots(db, {34: 5.0}, chars, on_date=day)
                assert fired
            async with SessionLocal() as db:
                row = (await db.execute(select(NetWorthSnapshot))).scalar_one()
            return row.wallet, row.assets_value, row.total
        finally:
            await engine.dispose()

    wallet, assets_value, total = _run(go())
    assert assets_value == 50.0
    if change == NARROWED:
        assert (wallet, total) == (0.0, 50.0)
    else:
        assert (wallet, total) == (1000.0, 1050.0)


def test_snapshot_now_reads_the_scopes_it_compares_against(monkeypatch):
    """The "Snapshot now" button hands take_snapshots the characters its
    route loaded; reading their scopes there must not need a lazy load."""
    import app.networth.snapshot as snapshot
    from app.routes.networth import _user_characters

    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add_pilot(SessionLocal, [cat.WALLET])
            async with SessionLocal() as db:
                db.add(CharacterDashboardCache(character_id=CID, wallet=1000.0))
                await db.commit()

            async def prices(db):
                return {}
            monkeypatch.setattr(snapshot, "get_price_map", prices)
            async with SessionLocal() as db:
                result = await snapshot.snapshot_for_characters(db, await _user_characters(db, USER))
            return result["written"], await _scalar(SessionLocal, "SELECT wallet FROM net_worth_snapshots")
        finally:
            await engine.dispose()

    assert _run(go()) == (1, 1000.0)


# ── The lookup every guard runs ─────────────────────────────────────────────

def test_the_scope_lookup_uses_an_index():
    async def go():
        engine, _ = await _engine()
        try:
            async with engine.connect() as conn:
                return " | ".join(r[3] for r in (await conn.execute(text(
                    "EXPLAIN QUERY PLAN SELECT scopes FROM characters WHERE character_id = 1"
                ))).fetchall())
        finally:
            await engine.dispose()
    plan = _run(go())
    assert "INDEX" in plan and "SCAN" not in plan, plan
