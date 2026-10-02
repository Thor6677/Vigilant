"""ISS-083: a dashboard sync never holds SQLite's write lock across an ESI call.

SQLite has one writer. The sync's own session opens its write transaction at
the first statement after the results loop starts changing rows (an
autoflush), and keeps it until the final commit. A network call made in
between holds every other writer in the app (the scheduler, page requests,
the rate-limit log) behind it, for as long as ESI takes: a 502 retry is over a
second, and a 429's retry-after can outlast their 10 s busy timeout.

Each test runs a real sync (the real fetchers and ESI client) against a
throwaway WAL database, with httpx's send replaced by a fake ESI. At every
outbound call the fake opens its own stdlib sqlite3 connection to the file and
tries BEGIN IMMEDIATE with no busy timeout: the write lock must be free.

The fetchers commit small rows of their own (the ESI response cache, a
structure name) on their own sessions while the sync runs, and one of those
commits can be in flight at the instant another fetcher sends. It lands within
a few loop turns, so the check retries briefly before it calls the lock held.
A lock held by the task that is sending can't be released while that task
waits on the send, so it is still caught. The public-info calls, which run
when no fetcher is left, must find the lock free at the first try.

The lock check never reads sqlite3.Connection.in_transaction on the event
loop: while an aiosqlite worker waits on a lock, that read blocks the loop.
"""
import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.cache  # noqa: F401 (registers esi_cache on Base, as at startup)
import app.db.models as models
import app.db.sde_models  # noqa: F401
import app.esi.client as esi_client
import app.routes.dashboard as dash
from app.auth import scopes as perms
from app.db.models import (
    Base, Character, CharacterCorpRoles, CharacterDashboardCache, ensure_wal,
)

CORP_OLD, CORP_NEW = 98100001, 98100002
ALLY_OLD, ALLY_NEW = 99100001, 99100002
STRUCTURE, SYSTEM = 1031000000001, 30000142

STEADY = {"wallet", "skillqueue", "location", "roles"}   # the hourly roll-over


def _token(cid: int) -> str:
    """An unsigned token whose scp claim the client's scope guard reads."""
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return ".".join([part({"alg": "none"}),
                     part({"sub": f"CHARACTER:EVE:{cid}", "scp": list(perms.ALL_SCOPES)}),
                     "unsigned"])


def _write_lock_free(path: str) -> bool:
    con = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            return False
        raise
    finally:
        con.close()


class FakeESI:
    """Answers the calls one sync makes, and checks the write lock at each."""

    def __init__(self, db_path, cid, *, public_info, corp_status=200, public_5xx=0):
        self.db_path = db_path
        self.cid = cid
        self.public_info = public_info
        self.corp_status = corp_status
        self.public_5xx = public_5xx
        self.sends = []          # (path, is_public, attempts needed; None = never free)
        self.unrouted = []

    async def send(self, client, request, **kw):
        path = request.url.path
        if path.startswith("/latest"):
            path = path[len("/latest"):]
        attempts = None
        for n in range(1, 51):
            if _write_lock_free(self.db_path):
                attempts = n
                break
            await asyncio.sleep(0.01)
        self.sends.append((path, "authorization" not in request.headers, attempts))
        await asyncio.sleep(0.02)    # the round trip
        return self.route(request, path)

    def route(self, request, path):
        def answer(data, status=200):
            return httpx.Response(status, json=data, request=request)
        c = f"/characters/{self.cid}"
        if path == f"{c}/":
            if self.public_5xx:
                self.public_5xx -= 1
                return answer({"error": "bad gateway"}, 502)
            return answer(self.public_info)
        if path.startswith("/corporations/"):
            if self.corp_status != 200:
                return answer({"error": "not found"}, self.corp_status)
            return answer({"name": "Test Corp B"})
        if path.startswith("/alliances/"):
            return answer({"name": "Test Alliance B"})
        table = {
            f"{c}/wallet/": 1234.5,
            f"{c}/location/": {"solar_system_id": SYSTEM, "structure_id": STRUCTURE},
            f"{c}/ship/": {"ship_type_id": 587, "ship_name": "Test Ship", "ship_item_id": 1},
            f"{c}/online/": {"online": True},
            f"{c}/skillqueue/": [],
            f"{c}/roles/": {"roles": ["Director"]},
            f"/universe/structures/{STRUCTURE}/": {"name": "Test Citadel", "solar_system_id": SYSTEM},
        }
        if path in table:
            return answer(table[path])
        self.unrouted.append(path)
        return answer({"error": "not found"}, 404)


def _run_sync(monkeypatch, tmp_path, cid, fake_kwargs, *, stale=STEADY):
    """One full sync of a pilot in CORP_OLD / ALLY_OLD whose `stale` fields
    are due. Returns the fake ESI and what the sync left in the database."""
    db_path = str(tmp_path / "sync.db")
    url = f"sqlite+aiosqlite:///{db_path}"
    ensure_wal(url)
    fake = FakeESI(db_path, cid, **fake_kwargs)

    async def send(client, request, **kw):
        return await fake.send(client, request, **kw)

    async def go():
        engine = create_async_engine(url)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(models, "AsyncSessionLocal", SessionLocal)
        monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
        # The lock registries are module dicts of asyncio.Lock; start each
        # test on fresh ones so none is bound to an earlier test's loop.
        monkeypatch.setattr(dash, "_token_locks", {})
        monkeypatch.setattr(dash, "_char_sync_locks", {})
        monkeypatch.setattr(esi_client, "_refresh_locks", {})
        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            now = datetime.now(timezone.utc)
            synced = {f: (now - timedelta(days=1) if f in stale else now).isoformat()
                      for f in dash.FIELD_CACHE_SECONDS}
            async with SessionLocal() as db:
                db.add(Character(
                    character_id=cid, character_name="Pilot A", user_id=None,
                    corporation_id=CORP_OLD, corporation_name="Test Corp A",
                    alliance_id=ALLY_OLD, alliance_name="Test Alliance A",
                    access_token=_token(cid), refresh_token="unused",
                    token_expiry=datetime(2099, 1, 1), scopes=perms.join_scopes(perms.ALL_SCOPES)))
                db.add(CharacterDashboardCache(character_id=cid, field_synced_json=json.dumps(synced)))
                await db.commit()

            await dash._sync_task_inner(cid)

            async with SessionLocal() as db:
                char = (await db.execute(select(Character).where(
                    Character.character_id == cid))).scalar_one()
                cache = (await db.execute(select(CharacterDashboardCache).where(
                    CharacterDashboardCache.character_id == cid))).scalar_one()
                roles = (await db.execute(select(CharacterCorpRoles).where(
                    CharacterCorpRoles.character_id == cid))).scalar_one_or_none()
                snaps = (await db.execute(text("SELECT count(*) FROM wallet_snapshots"))).scalar()
            return {
                "corp": (char.corporation_id, char.corporation_name),
                "alliance": (char.alliance_id, char.alliance_name),
                "status": cache.sync_status, "warnings": cache.sync_warnings_json,
                "wallet": cache.wallet, "snapshots": snaps,
                "roles": json.loads(roles.roles_json) if roles else None,
            }
        finally:
            await engine.dispose()

    return fake, asyncio.run(go())


def _assert_lock_free_at_every_send(fake):
    assert fake.unrouted == []
    held = [path for path, _, attempts in fake.sends if attempts is None]
    assert held == [], f"write lock held during ESI calls: {held}"
    public_retries = [(path, n) for path, public, n in fake.sends if public and n != 1]
    assert public_retries == [], public_retries


def _assert_sync_wrote(result):
    """The sync really reached its write path: the roles row, the balance and
    a snapshot, with no field failing quietly."""
    assert result["status"] == "idle" and result["warnings"] is None
    assert result["roles"] == ["Director"]
    assert (result["wallet"], result["snapshots"]) == (1234.5, 1)


def _public_paths(fake):
    return [path for path, public, _ in fake.sends if public]


def test_steady_state_sync_holds_no_lock_across_public_info(monkeypatch, tmp_path):
    cid = 93100001
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_OLD, "alliance_id": ALLY_OLD}})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == [f"/characters/{cid}/"]
    assert result["corp"] == (CORP_OLD, "Test Corp A")
    assert result["alliance"] == (ALLY_OLD, "Test Alliance A")


def test_corp_and_alliance_change_holds_no_lock_across_their_lookups(monkeypatch, tmp_path):
    cid = 93100002
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_NEW, "alliance_id": ALLY_NEW}})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == [f"/characters/{cid}/", f"/corporations/{CORP_NEW}/",
                                   f"/alliances/{ALLY_NEW}/"]
    assert result["corp"] == (CORP_NEW, "Test Corp B")
    assert result["alliance"] == (ALLY_NEW, "Test Alliance B")


def test_a_public_info_retry_holds_no_lock(monkeypatch, tmp_path):
    """A 502 makes the client back off for a second or more and ask again."""
    cid = 93100003
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_OLD, "alliance_id": ALLY_OLD}, "public_5xx": 1})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == [f"/characters/{cid}/", f"/characters/{cid}/"]
    assert result["corp"] == (CORP_OLD, "Test Corp A")


def test_a_failed_corp_lookup_still_records_the_new_corp(monkeypatch, tmp_path):
    cid = 93100004
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_NEW, "alliance_id": ALLY_OLD}, "corp_status": 404})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == [f"/characters/{cid}/", f"/corporations/{CORP_NEW}/"]
    assert result["corp"] == (CORP_NEW, None)
    assert result["alliance"] == (ALLY_OLD, "Test Alliance A")


def test_leaving_an_alliance_clears_it(monkeypatch, tmp_path):
    cid = 93100005
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_OLD}})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == [f"/characters/{cid}/"]
    assert result["corp"] == (CORP_OLD, "Test Corp A")
    assert result["alliance"] == (None, None)


def test_location_not_due_makes_no_public_call(monkeypatch, tmp_path):
    cid = 93100006
    fake, result = _run_sync(monkeypatch, tmp_path, cid, {
        "public_info": {"corporation_id": CORP_NEW, "alliance_id": ALLY_NEW}},
        stale={"wallet", "roles"})
    _assert_lock_free_at_every_send(fake)
    _assert_sync_wrote(result)
    assert _public_paths(fake) == []
    assert result["corp"] == (CORP_OLD, "Test Corp A")
