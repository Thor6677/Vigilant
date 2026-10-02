"""ISS-060: every way a character leaves an account cleans up the same way.

Four paths (self-removal, admin remove-character, admin remove-user, an EVE
owner change) share app/auth/purge.py:remove_character_from_account. The
small live state goes in the request; the ESI response cache and, when asked,
the history go in a batched background purge that survives a restart, stops
when the character is linked again, and waits out any sync that was already
running. Every table with a character_id column is classified, and the sweep
below fails by table name on one that isn't.
"""
import asyncio
import inspect
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — populate Base.metadata
import app.db.sde_models  # noqa: F401
from app.auth import purge
from app.db.cache import ESICache
from app.db.models import (
    AdminAuditLog, Base, Character, CharacterAssetCache, CharacterCorpRoles,
    CharacterDashboardCache, CharacterKillIngest, CharacterPurge, CharacterTag,
    DashboardAttentionDismissal, IndustryJobHistory, MiningLedgerEntry, NetWorthSnapshot,
    SkillFarmPilot, User, WalletSnapshot, WalletTransaction,
)
from tests.test_character_owner_change import _login, _set_owner, _sso
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    ALT_ID, CSRF, MAIN_ID, OTHER_ID, STRANGER_ID, USER_ID, env,
)

_NOW = datetime(2026, 9, 1, 12, 0, 0)
_LATER = timedelta(seconds=1)


# ── The registry ─────────────────────────────────────────────────────────────

def _character_columns(table) -> list[str]:
    return [c.name for c in table.columns
            if c.name == "character_id" or c.name.endswith("_character_id")]


def _groups() -> dict[str, set[str]]:
    return {
        "LIVE_STATE_TABLES": set(purge.LIVE_STATE_TABLES),
        "HISTORY_TABLES": set(purge.HISTORY_TABLES),
        "PER_CHARACTER_USER_TABLES": set(purge.PER_CHARACTER_USER_TABLES),
        "KEPT_TABLES": set(purge.KEPT_TABLES),
        "REMOVAL_TABLES": set(purge.REMOVAL_TABLES),
    }


def test_every_character_id_table_is_classified():
    classified = set().union(*_groups().values())
    missing = sorted(t.name for t in Base.metadata.tables.values()
                     if _character_columns(t) and t.name not in classified)
    assert not missing, (
        f"table(s) with a character_id column that removal doesn't know about: {missing}. "
        "Add each to one group in app/auth/purge.py: LIVE_STATE_TABLES (current values, "
        "cleared on removal), HISTORY_TABLES (accumulated, purged in the background), "
        "PER_CHARACTER_USER_TABLES (a user's own rows about the character) or KEPT_TABLES "
        "(shared/public data that must stay, with the reason).")


def test_each_table_is_in_one_group_and_exists():
    groups = _groups()
    names = list(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not (groups[a] & groups[b]), f"in both {a} and {b}: {groups[a] & groups[b]}"
    for group, tables in groups.items():
        for t in tables:
            assert t in Base.metadata.tables, f"{group} lists {t}, which no model defines"
            assert _character_columns(Base.metadata.tables[t]), f"{t} has no character_id column"


def test_tables_removal_deletes_from_have_a_character_id_column():
    for t in purge.LIVE_STATE_TABLES + purge.HISTORY_TABLES + purge.PER_CHARACTER_USER_TABLES:
        assert "character_id" in Base.metadata.tables[t].columns, t


def test_purge_waits_out_a_running_sync():
    from app.routes.dashboard import _SYNC_TIMEOUT
    assert purge.PURGE_DELAY.total_seconds() >= _SYNC_TIMEOUT + 60


def test_every_removal_path_uses_the_one_helper():
    from app.auth.routes import _release_transferred, remove_character
    from app.routes.admin import admin_remove_character, admin_remove_user
    for fn in (remove_character, _release_transferred, admin_remove_character, admin_remove_user):
        src = inspect.getsource(fn)
        assert "remove_character_from_account(" in src, fn.__name__
        # The inline full-table LIKE on esi_cache must not come back.
        assert "clear_live_state(" not in src and "clear_esi_cache(" not in src, fn.__name__


def test_the_scheduler_runs_the_purge():
    from app.routes.dashboard import _background_scheduler
    assert "run_due_purges()" in inspect.getsource(_background_scheduler)


def test_batch_deletes_use_an_index():
    async def go():
        engine, _ = await _fresh_engine()
        try:
            async with engine.connect() as conn:
                for t in purge.HISTORY_TABLES:
                    plan = " | ".join(r[3] for r in (await conn.execute(text(
                        f"EXPLAIN QUERY PLAN SELECT rowid FROM {t} WHERE character_id = 1 LIMIT 5000"
                    ))).fetchall())
                    assert "INDEX" in plan and "SCAN" not in plan, f"{t}: {plan}"
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_narrowing_purge_batches_only_tables_the_index_check_covers():
    """ISS-072: purge_history (the narrowing's "also delete" box) runs the
    same batched DELETE, so its tables must be ones checked above."""
    tables = {t for ts in purge._HISTORY_ROWS.values() for t in ts}
    assert tables and tables <= set(purge.HISTORY_TABLES)


# ── Seeding helpers ──────────────────────────────────────────────────────────

async def _fresh_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db, cid: int, user_id: int, *, snapshots: int = 1) -> None:
    """One row per classified table (more wallet snapshots if asked) plus an
    ESI cache entry, as a linked character would have accumulated."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(CharacterDashboardCache(character_id=cid, wallet=1.0))
    db.add(CharacterAssetCache(character_id=cid, assets_json="[]"))
    db.add(CharacterCorpRoles(character_id=cid, roles_json="[]"))
    db.add(CharacterKillIngest(character_id=cid))
    for i in range(snapshots):
        db.add(WalletSnapshot(character_id=cid, balance=float(i), recorded_at=now))
    db.add(WalletTransaction(transaction_id=cid * 10 + 1, character_id=cid, date=now,
                             type_id=34, quantity=1, unit_price=1.0, is_buy=True))
    db.add(IndustryJobHistory(job_id=cid * 10 + 1, character_id=cid, activity_id=1,
                              blueprint_type_id=1, runs=1, output_qty=1, completed_date=now))
    db.add(MiningLedgerEntry(character_id=cid, date="2026-09-01", type_id=1230,
                             solar_system_id=30000142, quantity=10))
    db.add(NetWorthSnapshot(character_id=cid, date=date(2026, 9, 1), user_id=user_id, total=1.0))
    db.add(CharacterTag(user_id=user_id, character_id=cid, tags_json='["Test"]'))
    db.add(DashboardAttentionDismissal(user_id=user_id, character_id=cid,
                                       item_key=f"test:{cid}", fingerprint="f"))
    db.add(SkillFarmPilot(user_id=user_id, character_id=cid))
    db.add(ESICache(key=f"h:@CHARACTER:EVE:{cid}|/characters/{cid}/wallet/", data="1",
                    expires_at=now + timedelta(hours=1)))
    await db.commit()


async def _counts(db, cid: int) -> dict[str, int]:
    out = {}
    for t in purge.LIVE_STATE_TABLES + purge.HISTORY_TABLES + purge.PER_CHARACTER_USER_TABLES:
        out[t] = (await db.execute(text(f"SELECT count(*) FROM {t} WHERE character_id = :c"),
                                   {"c": cid})).scalar()
    out["esi_cache"] = (await db.execute(text("SELECT count(*) FROM esi_cache WHERE key LIKE :m"),
                                         {"m": f"%:@CHARACTER:EVE:{cid}|%"})).scalar()
    return out


def _only(counts: dict, tables) -> dict:
    return {t: counts[t] for t in tables}


def _zero(tables) -> dict:
    return {t: 0 for t in tables}


def _one(tables) -> dict:
    return {t: 1 for t in tables}


async def _job(db, cid):
    return (await db.execute(select(CharacterPurge).where(
        CharacterPurge.character_id == cid))).scalar_one_or_none()


async def _audit(db, cid):
    return [(r.event_type, r.detail) for r in (await db.execute(
        select(AdminAuditLog).where(AdminAuditLog.character_id == cid,
                                    AdminAuditLog.event_type.like("character_purge%"))
    )).scalars().all()]


def _run_due(env):
    async def go(db):
        later = datetime.now(timezone.utc) + purge.PURGE_DELAY + _LATER
        return await purge.run_due_purges(
            now=later, session_factory=async_sessionmaker(db.bind, expire_on_commit=False))
    return env.q(go)


@pytest.fixture
def no_pause(monkeypatch):
    async def instant():
        return None
    monkeypatch.setattr(purge, "_between_batches", instant)


@pytest.fixture
def seeded(env, no_pause):
    """ALT (to remove) and MAIN (control) for USER_ID, STRANGER for OTHER_ID."""
    async def go(db):
        await _seed(db, ALT_ID, USER_ID)
        await _seed(db, MAIN_ID, USER_ID)
        await _seed(db, STRANGER_ID, OTHER_ID)
    env.q(go)
    return env


def _all(tables_group):
    return list(tables_group)


LIVE = purge.LIVE_STATE_TABLES
HISTORY = purge.HISTORY_TABLES
USER_ROWS = purge.PER_CHARACTER_USER_TABLES
EVERYTHING = LIVE + HISTORY + USER_ROWS + ("esi_cache",)
# What the background purge itself deletes (the user's own rows about the
# character go in the removal request, which the tests below skip).
PURGED = LIVE + HISTORY + ("esi_cache",)


def _assert_untouched(env, cid):
    assert env.q(lambda db: _counts(db, cid)) == _one(EVERYTHING), cid


# ── Self-removal ─────────────────────────────────────────────────────────────

def test_self_remove_keeps_history_by_default(seeded):
    env = seeded
    r = env.user().post(f"/auth/remove/{ALT_ID}", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert env.char(ALT_ID) is None
    assert env.calls["revoked"] == ["old-refresh"]

    now = env.q(lambda db: _counts(db, ALT_ID))
    assert _only(now, LIVE + USER_ROWS) == _zero(LIVE + USER_ROWS)
    assert _only(now, HISTORY) == _one(HISTORY)
    assert now["esi_cache"] == 1          # the background purge's job
    job = env.q(lambda db: _job(db, ALT_ID))
    assert (job.reason, job.delete_history) == ("self", False)

    [result] = _run_due(env)
    assert result["outcome"] == "done"
    after = env.q(lambda db: _counts(db, ALT_ID))
    assert after["esi_cache"] == 0
    assert _only(after, HISTORY) == _one(HISTORY)   # kept, as asked
    assert env.q(lambda db: _job(db, ALT_ID)) is None
    assert [e for e, _ in env.q(lambda db: _audit(db, ALT_ID))] == ["character_purge_done"]
    _assert_untouched(env, MAIN_ID)
    _assert_untouched(env, STRANGER_ID)


def test_self_remove_with_the_box_deletes_history_in_the_background(seeded):
    env = seeded
    r = env.user().post(f"/auth/remove/{ALT_ID}", data={"csrf_token": CSRF, "delete_history": "1"})
    assert r.status_code == 303
    # Nothing heavy in the request: the history is still there...
    assert _only(env.q(lambda db: _counts(db, ALT_ID)), HISTORY) == _one(HISTORY)
    job = env.q(lambda db: _job(db, ALT_ID))
    assert (job.reason, job.delete_history) == ("self", True)

    # ...and the purge waits out a sync that may still be running.
    async def too_early(db):
        return await purge.run_due_purges(
            now=datetime.now(timezone.utc),
            session_factory=async_sessionmaker(db.bind, expire_on_commit=False))
    assert env.q(too_early) == []
    assert env.q(lambda db: _job(db, ALT_ID)) is not None

    [result] = _run_due(env)
    assert result["outcome"] == "done"
    assert env.q(lambda db: _counts(db, ALT_ID)) == _zero(EVERYTHING)
    _assert_untouched(env, MAIN_ID)
    _assert_untouched(env, STRANGER_ID)


def test_self_remove_cannot_touch_another_users_character(seeded):
    env = seeded
    r = env.user().post(f"/auth/remove/{STRANGER_ID}",
                        data={"csrf_token": CSRF, "delete_history": "1"})
    assert r.status_code == 303
    assert env.char(STRANGER_ID) is not None
    assert env.q(lambda db: _job(db, STRANGER_ID)) is None
    _assert_untouched(env, STRANGER_ID)


def test_account_page_offers_the_history_box_unticked(env):
    r = env.user().get("/account")
    assert r.status_code == 200
    # One removable (non-main) character on this account, one box, unticked.
    assert r.text.count('name="delete_history" value="1">') == 1
    assert 'name="delete_history" value="1" checked' not in r.text
    assert "Its history is deleted only if you ticked the box." in r.text


# ── Admin removal ────────────────────────────────────────────────────────────

def _make_admin(env):
    async def go(db):
        u = (await db.execute(select(User).where(User.id == USER_ID))).scalar_one()
        u.role, u.is_admin = "admin", True
        await db.commit()
    env.q(go)


def test_admin_remove_character_always_deletes_history(seeded):
    env = seeded
    _make_admin(env)
    r = env.user().post(f"/admin/action/remove-character/{STRANGER_ID}")
    assert r.status_code == 200
    assert env.char(STRANGER_ID) is None
    now = env.q(lambda db: _counts(db, STRANGER_ID))
    assert _only(now, LIVE + USER_ROWS) == _zero(LIVE + USER_ROWS)
    job = env.q(lambda db: _job(db, STRANGER_ID))
    assert (job.reason, job.delete_history) == ("admin_character", True)

    _run_due(env)
    assert env.q(lambda db: _counts(db, STRANGER_ID)) == _zero(EVERYTHING)
    _assert_untouched(env, ALT_ID)
    _assert_untouched(env, MAIN_ID)


def test_admin_remove_user_queues_a_purge_for_each_character(seeded):
    env = seeded
    _make_admin(env)
    r = env.user().post(f"/admin/action/remove-user/{OTHER_ID}")
    assert r.status_code == 200
    assert env.char(STRANGER_ID) is None
    job = env.q(lambda db: _job(db, STRANGER_ID))
    assert (job.reason, job.delete_history) == ("admin_user", True)

    _run_due(env)
    assert env.q(lambda db: _counts(db, STRANGER_ID)) == _zero(EVERYTHING)
    _assert_untouched(env, ALT_ID)
    _assert_untouched(env, MAIN_ID)


# ── EVE owner change ─────────────────────────────────────────────────────────

def test_transfer_deletes_history_in_the_request_and_spares_the_new_owner(seeded, monkeypatch):
    env = seeded
    _set_owner(env, ALT_ID, "owner-A")
    _sso(monkeypatch, ALT_ID, "Alt Pilot", owner="owner-B")
    r = _login(env)
    assert r.status_code == 303
    new_user = env.session_of(r)["user_id"]
    assert env.char(ALT_ID).user_id == new_user

    # The new owner is linked in the same request, so the old owner's history
    # must already be gone: a background purge would find the character
    # "re-added" and keep it for them.
    now = env.q(lambda db: _counts(db, ALT_ID))
    assert _only(now, LIVE + HISTORY + USER_ROWS) == _zero(LIVE + HISTORY + USER_ROWS)
    assert now["esi_cache"] == 1
    job = env.q(lambda db: _job(db, ALT_ID))
    assert (job.reason, job.delete_history) == ("transfer", False)

    # The new owner's first sync writes before the purge runs.
    async def new_owner_sync(db):
        db.add(CharacterDashboardCache(character_id=ALT_ID, wallet=9.0))
        db.add(WalletSnapshot(character_id=ALT_ID, balance=9.0,
                              recorded_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        await db.commit()
    env.q(new_owner_sync)

    [result] = _run_due(env)
    assert result["outcome"] == "done"
    after = env.q(lambda db: _counts(db, ALT_ID))
    assert after["esi_cache"] == 0
    assert after["character_dashboard_cache"] == 1
    assert after["wallet_snapshots"] == 1
    assert env.char(ALT_ID).user_id == new_user
    _assert_untouched(env, MAIN_ID)


# ── The background purge itself ──────────────────────────────────────────────

CID = 424242
CONTROL = 434343


async def _queue(SessionLocal, cid, *, delete_history=True, reason="admin_character"):
    """What the removal request leaves behind: no character row, a job."""
    async with SessionLocal() as db:
        await purge._record_purge(db, cid, reason=reason, delete_history=delete_history)
        await db.commit()


def _run(coro):
    return asyncio.run(coro)


def _due(SessionLocal):
    return purge.run_due_purges(now=datetime.now(timezone.utc) + purge.PURGE_DELAY + _LATER,
                                session_factory=SessionLocal)


def test_history_goes_in_batches_and_other_characters_stay(monkeypatch):
    pauses = []

    async def count_pause():
        pauses.append(1)
    monkeypatch.setattr(purge, "_between_batches", count_pause)
    monkeypatch.setattr(purge, "PURGE_BATCH_ROWS", 3)

    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            async with SessionLocal() as db:
                await _seed(db, CID, 1, snapshots=10)
                await _seed(db, CONTROL, 1, snapshots=10)
            await _queue(SessionLocal, CID)
            [result] = await _due(SessionLocal)
            async with SessionLocal() as db:
                return result, await _counts(db, CID), await _counts(db, CONTROL)
        finally:
            await engine.dispose()

    result, gone, control = _run(go())
    assert result["counts"]["wallet_snapshots"] == 10
    assert _only(gone, PURGED) == _zero(PURGED)
    assert control["wallet_snapshots"] == 10
    assert _only(control, LIVE + USER_ROWS) == _one(LIVE + USER_ROWS)
    # 10 snapshots in batches of 3: three full batches, each followed by a
    # pause, then a short one that ends the loop; plus one pause after the
    # single esi_cache key batch.
    assert len(pauses) == 3 + 1


def test_a_readd_between_batches_stops_the_purge_and_keeps_the_new_rows(monkeypatch):
    monkeypatch.setattr(purge, "PURGE_BATCH_ROWS", 3)
    state = {"SessionLocal": None, "readded": False}

    async def readd_after_first_batch():
        if state["readded"]:
            return
        state["readded"] = True
        async with state["SessionLocal"]() as db:
            db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                             access_token="x", refresh_token="y", token_expiry=_NOW))
            db.add(WalletSnapshot(character_id=CID, balance=99.0, recorded_at=_NOW))
            await db.commit()
    monkeypatch.setattr(purge, "_between_batches", readd_after_first_batch)

    async def go():
        engine, SessionLocal = await _fresh_engine()
        state["SessionLocal"] = SessionLocal
        try:
            async with SessionLocal() as db:
                await _seed(db, CID, 1, snapshots=10)
                # The esi_cache step pauses once too; give it no keys so the
                # first pause comes after the first history batch.
                await db.execute(text("DELETE FROM esi_cache"))
                await db.commit()
            await _queue(SessionLocal, CID)
            [result] = await _due(SessionLocal)
            async with SessionLocal() as db:
                return (result, await _counts(db, CID), await _job(db, CID),
                        await _audit(db, CID))
        finally:
            await engine.dispose()

    result, left, job, audit = _run(go())
    assert result["outcome"] == "abandoned"
    # 3 deleted before the re-add; the other 7 and the new owner's row stay.
    assert left["wallet_snapshots"] == 7 + 1
    # History tables after the one it stopped in are left alone.
    assert left["net_worth_snapshots"] == 1
    assert job is None
    assert [e for e, _ in audit] == ["character_purge_abandoned"]


def test_a_character_linked_again_keeps_its_rows_but_loses_its_esi_cache(no_pause):
    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            async with SessionLocal() as db:
                await _seed(db, CID, 1)
            await _queue(SessionLocal, CID)
            async with SessionLocal() as db:
                db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                await db.commit()
            [result] = await _due(SessionLocal)
            async with SessionLocal() as db:
                return result, await _counts(db, CID), await _job(db, CID)
        finally:
            await engine.dispose()

    result, left, job = _run(go())
    assert result["outcome"] == "abandoned"
    assert left["esi_cache"] == 0
    assert _only(left, LIVE + HISTORY + USER_ROWS) == _one(LIVE + HISTORY + USER_ROWS)
    assert job is None


def test_an_interrupted_purge_resumes_on_the_next_run(monkeypatch, no_pause):
    real = purge._delete_in_batches

    async def dies_on_industry(db, table, cid, *, only_if_gone):
        if table == "industry_job_history":
            raise RuntimeError("process restarted")
        return await real(db, table, cid, only_if_gone=only_if_gone)

    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            async with SessionLocal() as db:
                await _seed(db, CID, 1, snapshots=5)
            await _queue(SessionLocal, CID)
            monkeypatch.setattr(purge, "_delete_in_batches", dies_on_industry)
            first = await _due(SessionLocal)
            async with SessionLocal() as db:
                mid, mid_job = await _counts(db, CID), await _job(db, CID)
            monkeypatch.setattr(purge, "_delete_in_batches", real)
            second = await _due(SessionLocal)
            async with SessionLocal() as db:
                return first, mid, mid_job, second, await _counts(db, CID), await _job(db, CID)
        finally:
            await engine.dispose()

    first, mid, mid_job, second, end, end_job = _run(go())
    assert first == []                       # failed, logged, left queued
    assert mid_job is not None
    assert mid["wallet_snapshots"] == 0      # what it did before dying stays done
    assert mid["industry_job_history"] == 1
    assert [r["outcome"] for r in second] == ["done"]
    assert _only(end, PURGED) == _zero(PURGED)
    assert end_job is None


def test_removing_again_moves_the_job_on_and_keeps_the_history_choice():
    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            await _queue(SessionLocal, CID, delete_history=True, reason="self")
            async with SessionLocal() as db:
                first = await _job(db, CID)
            await asyncio.sleep(0.01)
            await _queue(SessionLocal, CID, delete_history=False, reason="self")
            async with SessionLocal() as db:
                return first, await _job(db, CID), (await db.execute(
                    text("SELECT count(*) FROM character_purges"))).scalar()
        finally:
            await engine.dispose()

    first, second, rows = _run(go())
    assert rows == 1
    assert second.delete_history is True
    assert second.removed_at > first.removed_at


def test_a_removal_recorded_mid_purge_stays_queued(monkeypatch):
    state = {}

    async def remove_again():
        if state.get("done"):
            return
        state["done"] = True
        await _queue(state["SessionLocal"], CID, delete_history=False, reason="self")
    monkeypatch.setattr(purge, "_between_batches", remove_again)

    async def go():
        engine, SessionLocal = await _fresh_engine()
        state["SessionLocal"] = SessionLocal
        try:
            async with SessionLocal() as db:
                await _seed(db, CID, 1)
            await _queue(SessionLocal, CID)
            await _due(SessionLocal)
            async with SessionLocal() as db:
                return await _job(db, CID)
        finally:
            await engine.dispose()

    job = _run(go())
    # The newer removal still has to wait out its own syncs, so its row stays.
    assert job is not None
    assert job.delete_history is True


# ── A sync that was running when the character was removed ─────────────────

def test_sync_must_not_write_after_a_removal():
    started = datetime.now(timezone.utc)

    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            async with SessionLocal() as db:
                gone = await purge.sync_must_not_write(db, CID, started)
                db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                await db.commit()
                linked = await purge.sync_must_not_write(db, CID, started)
                # A transfer: removed and linked to the new owner in one request.
                await purge._record_purge(db, CID, reason="transfer", delete_history=False)
                await db.commit()
                transferred = await purge.sync_must_not_write(db, CID, started)
                # A removal long before this sync began doesn't concern it.
                later_sync = await purge.sync_must_not_write(
                    db, CID, started + timedelta(minutes=10))
            return gone, linked, transferred, later_sync
        finally:
            await engine.dispose()

    assert _run(go()) == (True, False, True, False)


def _sync_one_wallet_field(monkeypatch, SessionLocal):
    """Point _sync_fields at a single wallet field whose fetcher answers
    without ESI, on the test engine."""
    import app.routes.dashboard as dash

    async def fake_wallet(chars, db):
        return {chars[0].character_id: (123.0, None)}
    monkeypatch.setattr(dash, "FIELD_CACHE_SECONDS", {"wallet": 0})
    monkeypatch.setattr(dash, "FIELD_SCOPES", {"wallet": None})
    monkeypatch.setattr(dash, "_FIELD_FETCHERS", {"wallet": fake_wallet})
    monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
    return dash._sync_fields


@pytest.mark.parametrize("linked_again", [False, True], ids=["removed", "transferred"])
def test_a_running_sync_writes_nothing_after_the_removal(monkeypatch, linked_again):
    async def go():
        engine, SessionLocal = await _fresh_engine()
        sync_fields = _sync_one_wallet_field(monkeypatch, SessionLocal)
        try:
            async with SessionLocal() as db:
                db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                db.add(CharacterDashboardCache(character_id=CID))
                db.add(CharacterAssetCache(character_id=CID))
                await db.commit()

            async with SessionLocal() as sync_db:
                char = (await sync_db.execute(select(Character).where(
                    Character.character_id == CID))).scalar_one()
                cache = await sync_db.get(CharacterDashboardCache, CID)
                asset_cache = await sync_db.get(CharacterAssetCache, CID)

                # The removal lands while the sync is running.
                async with SessionLocal() as db:
                    victim = (await db.execute(select(Character).where(
                        Character.character_id == CID))).scalar_one()
                    await purge.remove_character_from_account(
                        db, victim, reason="self", delete_history=False)
                    await db.commit()
                    if linked_again:
                        db.add(Character(character_id=CID, character_name="Test Alt",
                                         user_id=2, access_token="x2", refresh_token="y2",
                                         token_expiry=_NOW))
                        await db.commit()

                await sync_fields(CID, char, cache, asset_cache, sync_db)

            async with SessionLocal() as db:
                return (await db.execute(text(
                    "SELECT (SELECT count(*) FROM wallet_snapshots),"
                    " (SELECT count(*) FROM character_dashboard_cache)"))).one()
        finally:
            await engine.dispose()

    snapshots, caches = _run(go())
    assert snapshots == 0
    assert caches == 0


def test_a_sync_on_a_linked_character_still_writes(monkeypatch):
    async def go():
        engine, SessionLocal = await _fresh_engine()
        sync_fields = _sync_one_wallet_field(monkeypatch, SessionLocal)
        try:
            async with SessionLocal() as db:
                db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                db.add(CharacterDashboardCache(character_id=CID))
                db.add(CharacterAssetCache(character_id=CID))
                await db.commit()
            async with SessionLocal() as sync_db:
                char = (await sync_db.execute(select(Character).where(
                    Character.character_id == CID))).scalar_one()
                await sync_fields(CID, char, await sync_db.get(CharacterDashboardCache, CID),
                                  await sync_db.get(CharacterAssetCache, CID), sync_db)
            async with SessionLocal() as db:
                return (await db.execute(text("SELECT wallet FROM character_dashboard_cache"))).scalar(), \
                    (await db.execute(text("SELECT count(*) FROM wallet_snapshots"))).scalar()
        finally:
            await engine.dispose()

    assert _run(go()) == (123.0, 1)


def test_a_sync_that_recreated_the_cache_row_writes_nothing_and_the_purge_clears_it(
        monkeypatch, no_pause):
    """The removal lands after the sync read the character but before it
    created the cache rows (_sync_task_inner), so an empty cache row exists
    again for a character that doesn't. The sync's fetchers can no longer load
    the character, and the final check keeps the sync from writing its
    bookkeeping into that row; the purge's live-state sweep then deletes it."""
    async def go():
        engine, SessionLocal = await _fresh_engine()
        sync_fields = _sync_one_wallet_field(monkeypatch, SessionLocal)
        try:
            async with SessionLocal() as db:
                db.add(Character(character_id=CID, character_name="Test Alt", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                await db.commit()
            async with SessionLocal() as sync_db:
                char = (await sync_db.execute(select(Character).where(
                    Character.character_id == CID))).scalar_one()

                async with SessionLocal() as db:
                    victim = (await db.execute(select(Character).where(
                        Character.character_id == CID))).scalar_one()
                    await purge.remove_character_from_account(
                        db, victim, reason="self", delete_history=True)
                    await db.commit()

                cache = CharacterDashboardCache(character_id=CID, sync_status="syncing")
                asset_cache = CharacterAssetCache(character_id=CID)
                sync_db.add_all([cache, asset_cache])
                await sync_db.commit()
                await sync_fields(CID, char, cache, asset_cache, sync_db)

            async with SessionLocal() as db:
                before = (await db.execute(text(
                    "SELECT (SELECT count(*) FROM wallet_snapshots),"
                    " (SELECT count(*) FROM character_dashboard_cache"
                    "   WHERE field_synced_json IS NOT NULL OR wallet IS NOT NULL),"
                    " (SELECT count(*) FROM character_dashboard_cache)"))).one()
            await _due(SessionLocal)
            async with SessionLocal() as db:
                after = await _counts(db, CID)
            return tuple(before), after
        finally:
            await engine.dispose()

    before, after = _run(go())
    assert before == (0, 0, 1)      # nothing written into it; an empty row left
    assert _only(after, PURGED) == _zero(PURGED)
