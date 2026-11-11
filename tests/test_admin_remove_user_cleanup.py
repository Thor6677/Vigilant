"""ISS-058: removing a user must not leave its rows behind with a dangling
user_id.

Two layers:

* A static sweep that checks every (table, column) app.db.user_ids.
  _user_reference_columns() finds against admin.py's USER_OWNED_TABLES,
  NULLABLE_FKS and USER_REFS_HANDLED_ELSEWHERE, so a new user_id column added
  to a model without also being handled in admin_remove_user fails this test
  by name rather than silently leaking rows in prod.
* A behavioural test that drives the real /admin/action/remove-user route
  and checks a seeded target user's rows are gone (or nulled) while a control
  user's identical rows are untouched — catching a WHERE clause that matches
  too much as well as one that matches too little.
"""
import base64
import json
import tempfile

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — populate Base.metadata before the sweep
from app.db.models import (
    Base, MarketScanResult, MarketWatchlist, MarketWatchlistItem, User, get_db,
)
from app.db.user_ids import _user_reference_columns
from app.routes.admin import (
    NULLABLE_FKS, USER_OWNED_CHILD_TABLES, USER_OWNED_TABLES, USER_REFS_HANDLED_ELSEWHERE,
)


# ── Static sweep ─────────────────────────────────────────────────────────────

def test_every_user_reference_column_is_handled_exactly_once():
    owned = {(t, "user_id") for t in USER_OWNED_TABLES}
    nulled = set(NULLABLE_FKS)
    handled = set(USER_REFS_HANDLED_ELSEWHERE)

    # No column should be claimed by more than one list.
    overlaps = (
        (owned & nulled) | (owned & handled) | (nulled & handled)
    )
    assert not overlaps, f"columns claimed by more than one list: {overlaps}"

    all_listed = owned | nulled | handled
    real = _user_reference_columns()

    missing = real - all_listed
    assert not missing, (
        f"columns _user_reference_columns() finds but admin.py does not handle: "
        f"{missing} — add each to USER_OWNED_TABLES (if the row belongs to the "
        f"user and should be deleted), NULLABLE_FKS (if it's an attribution "
        f"column that should be nulled), or USER_REFS_HANDLED_ELSEWHERE (if "
        f"admin_remove_user already handles it some other way)"
    )

    stale = all_listed - real
    assert not stale, (
        f"admin.py lists (table, column) pairs _user_reference_columns() does "
        f"not find: {stale} — this entry is stale or misspelled; fix or remove "
        f"it from USER_OWNED_TABLES / NULLABLE_FKS / USER_REFS_HANDLED_ELSEWHERE"
    )


def test_every_child_of_a_user_owned_table_is_handled():
    """A table with no user_id of its own, owned through a foreign key into a
    USER_OWNED_TABLES table (watchlist items through their watchlist), would
    be orphaned when the parents go. Each must be in USER_OWNED_CHILD_TABLES,
    and each entry there must be a real foreign key into an owned table."""
    owned = set(USER_OWNED_TABLES)
    real = set()
    for table in Base.metadata.tables.values():
        if "user_id" in table.columns:
            continue
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name in owned:
                    real.add((table.name, col.name, fk.column.table.name))
    assert real == set(USER_OWNED_CHILD_TABLES), (
        f"handled: {set(USER_OWNED_CHILD_TABLES)}, found: {real}")
    for _child, _key, parent in USER_OWNED_CHILD_TABLES:
        assert "id" in Base.metadata.tables[parent].columns


def test_every_nullable_fk_column_is_actually_nullable():
    for tbl, col in NULLABLE_FKS:
        table = Base.metadata.tables[tbl]
        column = table.columns[col]
        assert column.nullable, (
            f"{tbl}.{col} is NOT NULL — UPDATE ... SET {col} = NULL in "
            f"admin_remove_user would raise IntegrityError; either make the "
            f"column nullable or move it to USER_OWNED_TABLES instead"
        )


# ── Behavioural test through the real route ─────────────────────────────────

ADMIN_ID = 900
TARGET_ID = 901
CONTROL_ID = 902
CSRF = "test-csrf-token"


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    return signer.sign(data).decode()


@pytest.fixture
def remove_user_client():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=ADMIN_ID, role="admin", is_admin=True))
            db.add(User(id=TARGET_ID, role="user"))
            db.add(User(id=CONTROL_ID, role="user"))
            await db.flush()

            for uid, char_id in ((TARGET_ID, 1001), (CONTROL_ID, 1002)):
                await db.execute(text(
                    "INSERT INTO net_worth_snapshots "
                    "(character_id, date, user_id, wallet, assets_value, escrow, "
                    "industry_value, total, unpriced_count, recorded_at) "
                    "VALUES (:cid, '2026-09-01', :uid, 0, 0, 0, 0, 0, 0, "
                    "'2026-09-01 00:00:00')"
                ), {"cid": char_id, "uid": uid})
                await db.execute(text(
                    "INSERT INTO stockpile_targets "
                    "(user_id, type_id, target_qty, created_at) "
                    "VALUES (:uid, 34, 1000, '2026-09-01 00:00:00')"
                ), {"uid": uid})
                await db.execute(text(
                    "INSERT INTO dscan_results "
                    "(id, paste_data, parsed_json, user_id, created_at, expires_at) "
                    "VALUES (:id, 'paste', '{}', :uid, "
                    "'2026-09-01 00:00:00', '2026-09-08 00:00:00')"
                ), {"id": f"scan{uid}", "uid": uid})
                await db.execute(text(
                    "INSERT INTO kill_alert_events "
                    "(user_id, kind, killmail_id, system_id, triggered_at) "
                    "VALUES (:uid, 'system_watch', :kid, 30000142, "
                    "'2026-09-01 00:00:00')"
                ), {"uid": uid, "kid": 5000 + uid})
                await db.execute(text(
                    "INSERT INTO update_schedule "
                    "(target_tag, run_at, timezone, state, created_by) "
                    "VALUES (:tag, '2026-09-10 04:00:00', 'UTC', 'pending', :uid)"
                ), {"tag": f"v1.0.0-{uid}", "uid": uid})
                await db.execute(text(
                    "INSERT INTO update_run_report "
                    "(created_at, kind, outcome, request_id, acknowledged_by) "
                    "VALUES ('2026-09-01 00:00:00', 'scheduled', 'succeeded', "
                    ":rid, :uid)"
                ), {"rid": f"req-{uid}", "uid": uid})

            # Singletons (id always 1): seed with the target's id so the fix's
            # NULLABLE_FKS pass is exercised on them too.
            await db.execute(text(
                "INSERT INTO update_policy (id, enabled, weekday, local_time, "
                "timezone, patch_only, updated_by) "
                "VALUES (1, 0, 6, '04:00', 'UTC', 1, :uid)"
            ), {"uid": TARGET_ID})
            await db.execute(text(
                "INSERT INTO update_notify_settings (id, discord_policy, "
                "webhook_format, webhook_policy, updated_by) "
                "VALUES (1, 'all', 'json', 'all', :uid)"
            ), {"uid": TARGET_ID})
            await db.commit()
    import asyncio
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    cookie = _cookie(main.settings.secret_key, ADMIN_ID)
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    c.headers.update({"X-CSRF-Token": CSRF})
    c.engine = engine
    c.SessionLocal = SessionLocal
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def _scalar(db_rows, col_idx=0):
    return [r[col_idx] for r in db_rows]


def test_remove_user_deletes_owned_rows_and_clears_attribution(remove_user_client):
    r = remove_user_client.post(f"/admin/action/remove-user/{TARGET_ID}")
    assert r.status_code == 200

    async def _check():
        async with remove_user_client.SessionLocal() as db:
            for tbl in ("net_worth_snapshots", "stockpile_targets",
                       "dscan_results", "kill_alert_events"):
                target_rows = (await db.execute(
                    text(f"SELECT * FROM {tbl} WHERE user_id = :uid"),
                    {"uid": TARGET_ID})).fetchall()
                assert target_rows == [], f"{tbl} still has target rows: {target_rows}"

                control_rows = (await db.execute(
                    text(f"SELECT * FROM {tbl} WHERE user_id = :uid"),
                    {"uid": CONTROL_ID})).fetchall()
                assert len(control_rows) == 1, f"{tbl} lost the control row: {control_rows}"

            # update_schedule / update_run_report: multi-row tables, so both a
            # target and a control attribution can exist side by side.
            sched = (await db.execute(text(
                "SELECT created_by FROM update_schedule ORDER BY id"))).fetchall()
            assert _scalar(sched) == [None, CONTROL_ID], sched

            report = (await db.execute(text(
                "SELECT acknowledged_by FROM update_run_report ORDER BY id"))).fetchall()
            assert _scalar(report) == [None, CONTROL_ID], report

            # Singletons: only the target's id was ever seeded onto them, so
            # this only exercises the NULL-on-match direction.
            policy_by = (await db.execute(text(
                "SELECT updated_by FROM update_policy WHERE id = 1"))).scalar()
            assert policy_by is None, policy_by

            notify_by = (await db.execute(text(
                "SELECT updated_by FROM update_notify_settings WHERE id = 1"))).scalar()
            assert notify_by is None, notify_by

            # The user row itself is gone.
            user = (await db.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": TARGET_ID}
            )).scalar_one_or_none()
            assert user is None

            # The control user is completely untouched.
            control_user = (await db.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": CONTROL_ID}
            )).scalar_one_or_none()
            assert control_user == CONTROL_ID
    import asyncio
    asyncio.run(_check())


def test_remove_user_preserves_singleton_attribution_for_a_different_user(remove_user_client):
    """The control side of the singleton tables: point update_policy /
    update_notify_settings at CONTROL_ID instead, remove TARGET_ID, and check
    the singleton row is left alone. A WHERE-less `SET updated_by = NULL`
    would pass the NULL-on-match test above but fail this one."""
    async def _repoint():
        async with remove_user_client.SessionLocal() as db:
            await db.execute(text(
                "UPDATE update_policy SET updated_by = :uid WHERE id = 1"),
                {"uid": CONTROL_ID})
            await db.execute(text(
                "UPDATE update_notify_settings SET updated_by = :uid WHERE id = 1"),
                {"uid": CONTROL_ID})
            await db.commit()
    import asyncio
    asyncio.run(_repoint())

    r = remove_user_client.post(f"/admin/action/remove-user/{TARGET_ID}")
    assert r.status_code == 200

    async def _check():
        async with remove_user_client.SessionLocal() as db:
            policy_by = (await db.execute(text(
                "SELECT updated_by FROM update_policy WHERE id = 1"))).scalar()
            assert policy_by == CONTROL_ID, policy_by

            notify_by = (await db.execute(text(
                "SELECT updated_by FROM update_notify_settings WHERE id = 1"))).scalar()
            assert notify_by == CONTROL_ID, notify_by
    import asyncio
    asyncio.run(_check())


def test_remove_user_deletes_hub_scan_rows_and_forgets_their_scan(remove_user_client):
    """Watchlists, their items (no user_id of their own) and saved scan
    results go with the account; the control user's stay. The removed user's
    scan job is cancelled and dropped from the in-memory registry."""
    from datetime import datetime

    from app.market import scan_runner, scan_results

    stamp = datetime(2026, 11, 1)

    async def _seed():
        ids = {}
        async with remove_user_client.SessionLocal() as db:
            for uid in (TARGET_ID, CONTROL_ID):
                wl = MarketWatchlist(user_id=uid, name="Sample List", created_at=stamp)
                db.add(wl)
                await db.flush()
                ids[uid] = wl.id
                db.add_all([MarketWatchlistItem(watchlist_id=wl.id, type_id=t, added_at=stamp)
                            for t in (2001, 2002)])
                for kind, ref in (("watchlist", wl.id), ("category", 10)):
                    db.add(MarketScanResult(user_id=uid, list_kind=kind, list_ref=ref,
                                            label="Sample", status="complete",
                                            scanned_at=stamp, opened_at=stamp))
            await db.commit()
        return ids

    import asyncio
    ids = asyncio.run(_seed())

    class _Task:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    settings = scan_results.Settings(5e8, 20, 3.37, 1.5)
    jobs = {}
    for uid in (TARGET_ID, CONTROL_ID):
        job = scan_runner.Job(uid, "watchlist", ids[uid], settings, state="running")
        job.task = _Task()
        scan_runner._jobs[uid] = jobs[uid] = job
    try:
        r = remove_user_client.post(f"/admin/action/remove-user/{TARGET_ID}")
        assert r.status_code == 200
        assert scan_runner.get(TARGET_ID) is None and jobs[TARGET_ID].task.cancelled
        assert scan_runner.get(CONTROL_ID) is jobs[CONTROL_ID]
        assert not jobs[CONTROL_ID].task.cancelled
    finally:
        scan_runner._jobs.pop(TARGET_ID, None)
        scan_runner._jobs.pop(CONTROL_ID, None)

    async def _check():
        async with remove_user_client.SessionLocal() as db:
            def rows(sql, **params):
                return db.execute(text(sql), params)
            for uid, expect in ((TARGET_ID, 0), (CONTROL_ID, 1)):
                lists = (await rows("SELECT COUNT(*) FROM market_watchlist WHERE user_id = :u",
                                    u=uid)).scalar()
                results = (await rows("SELECT COUNT(*) FROM market_scan_result WHERE user_id = :u",
                                      u=uid)).scalar()
                items = (await rows("SELECT COUNT(*) FROM market_watchlist_item "
                                    "WHERE watchlist_id = :w", w=ids[uid])).scalar()
                assert (lists, results, items) == (expect, 2 * expect, 2 * expect), uid
            # No item is left pointing at a list that is gone.
            orphans = (await rows(
                "SELECT COUNT(*) FROM market_watchlist_item WHERE watchlist_id NOT IN "
                "(SELECT id FROM market_watchlist)")).scalar()
            assert orphans == 0
    asyncio.run(_check())
