"""A session ends when the account behind it changes.

Session cookies are signed but stateless, so on their own they outlive the
account they name. Every request with a user_id is checked against that user's
row: a missing user, or an epoch that is not the row's current one, clears the
session (app/auth/session_guard.py). Rotating the epoch — on "sign out
everywhere" and on a role change — ends every session the account has, and
removing an account ends them by removing the row.

Uses the SSO-stubbed environment from test_permissions_flow.py, whose users
start with no epoch, like accounts from before epochs existed.
"""
import asyncio
import os
import tempfile

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AdminAuditLog, Base, User
from app.db.user_ids import ensure_users_autoincrement
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    MAIN_ID, OTHER_ID, USER_ID, _pending, _scalar, env,
)


def _set_epoch(env, user_id, epoch):
    async def go(db):
        u = await _scalar(db, select(User).where(User.id == user_id))
        u.session_epoch = epoch
        await db.commit()
    env.q(go)


def _epoch(env, user_id):
    return env.q(lambda db: _scalar(db, select(User.session_epoch).where(User.id == user_id)))


def _signed_in(client) -> bool:
    return client.get("/account").status_code == 200


# ── The check itself ────────────────────────────────────────────────────────

def test_matching_epoch_keeps_the_session(env):
    _set_epoch(env, USER_ID, "e1")
    assert _signed_in(env.client({"user_id": USER_ID, "session_epoch": "e1"}))


def test_stale_epoch_ends_the_session(env):
    _set_epoch(env, USER_ID, "e2")
    c = env.client({"user_id": USER_ID, "session_epoch": "e1"})
    assert not _signed_in(c)


def test_a_cookie_without_an_epoch_ends_once_the_account_has_one(env):
    _set_epoch(env, USER_ID, "e1")
    assert not _signed_in(env.client({"user_id": USER_ID}))


def test_a_session_for_a_missing_user_ends(env):
    c = env.client({"user_id": 999_999})
    r = c.get("/account")
    assert r.status_code == 303 and r.headers["location"] == "/"
    # The emptied session is sent back as an expired cookie.
    assert "vigilant_session=null" in r.headers.get("set-cookie", "")


# ── Sign-in stores the epoch ────────────────────────────────────────────────

def test_sign_in_gives_the_account_an_epoch_and_the_cookie_carries_it(env):
    assert _epoch(env, USER_ID) is None
    env.sso_returns(MAIN_ID, "Main Pilot", [])
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    session = env.session_of(r)
    epoch = _epoch(env, USER_ID)
    assert epoch and session["session_epoch"] == epoch
    # A second sign-in keeps it, so other browsers stay signed in.
    env.sso_returns(MAIN_ID, "Main Pilot", [])
    r2 = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    assert env.session_of(r2)["session_epoch"] == epoch


def test_a_new_account_starts_with_an_epoch(env):
    env.sso_returns(90000077, "Brand New", [])
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    session = env.session_of(r)
    assert session["session_epoch"] == _epoch(env, session["user_id"])


# ── What ends every session ─────────────────────────────────────────────────

def test_sign_out_everywhere_ends_other_sessions(env):
    _set_epoch(env, USER_ID, "e1")
    other_browser = env.client({"user_id": USER_ID, "session_epoch": "e1"})
    this_browser = env.client({"user_id": USER_ID, "session_epoch": "e1"})

    r = this_browser.post("/auth/logout-everywhere")

    assert r.status_code == 303
    assert _epoch(env, USER_ID) not in (None, "e1")
    assert not _signed_in(other_browser)
    events = env.q(lambda db: _all(db, select(AdminAuditLog).where(
        AdminAuditLog.event_type == "user_logout_everywhere")))
    assert [e.user_id for e in events] == [USER_ID]


def test_a_role_change_ends_that_users_sessions(env):
    _set_epoch(env, USER_ID, "admin-epoch")
    _set_epoch(env, OTHER_ID, "e1")

    async def make_admin(db):
        u = await _scalar(db, select(User).where(User.id == USER_ID))
        u.role, u.is_admin = "admin", True
        await db.commit()
    env.q(make_admin)
    admin = env.client({"user_id": USER_ID, "session_epoch": "admin-epoch"})
    target = env.client({"user_id": OTHER_ID, "session_epoch": "e1"})

    r = admin.post(f"/admin/action/set-role/{OTHER_ID}/manager")

    assert r.status_code == 200
    assert _epoch(env, OTHER_ID) not in (None, "e1")
    assert not _signed_in(target)
    assert _signed_in(admin)


def test_a_removed_users_cookie_does_not_reach_the_next_signup(env):
    _set_epoch(env, OTHER_ID, "e1")
    stale = {"user_id": OTHER_ID, "session_epoch": "e1"}

    async def remove(db):
        await db.execute(text("DELETE FROM characters WHERE user_id = :u"), {"u": OTHER_ID})
        await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": OTHER_ID})
        await db.commit()
    env.q(remove)
    assert not _signed_in(env.client(stale))

    # Even if the next account were handed the same id, it has its own epoch.
    async def reuse(db):
        db.add(User(id=OTHER_ID, role="user", session_epoch="someone-else"))
        await db.commit()
    env.q(reuse)
    assert not _signed_in(env.client(stale))


async def _all(db, stmt):
    return (await db.execute(stmt)).scalars().all()


# ── Ids are not reused ──────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def old_db():
    """A users table as installs before AUTOINCREMENT created it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE users (id INTEGER NOT NULL, created_at DATETIME, "
                "last_login DATETIME, is_admin BOOLEAN, role VARCHAR(16), PRIMARY KEY (id))"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN session_epoch VARCHAR(32)"))
            await conn.execute(text(
                "CREATE TABLE characters (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id))"))
            await conn.execute(text(
                "INSERT INTO users (id, role, is_admin, session_epoch) VALUES "
                "(1, 'admin', 1, 'a'), (2, 'user', 0, 'b'), (3, 'user', 0, 'c')"))
            await conn.execute(text("INSERT INTO characters (id, user_id) VALUES (10, 3)"))
    _run(setup())
    yield engine, SessionLocal
    _run(engine.dispose())
    os.unlink(path)


def _q(SessionLocal, fn):
    async def go():
        async with SessionLocal() as db:
            return await fn(db)
    return _run(go())


def test_fresh_schema_does_not_reuse_a_removed_id(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add_all([User(), User()])
            await db.commit()
            await db.execute(text("DELETE FROM users WHERE id = 2"))
            await db.commit()
            u = User()
            db.add(u)
            await db.commit()
            return u.id
    try:
        assert _run(go()) == 3
    finally:
        _run(engine.dispose())


def test_existing_table_is_rebuilt_with_rows_intact(old_db):
    engine, SessionLocal = old_db
    assert _q(SessionLocal, ensure_users_autoincrement) is True

    sql = _q(SessionLocal, lambda db: _scalar(db, text(
        "SELECT sql FROM sqlite_master WHERE name = 'users'")))
    assert "AUTOINCREMENT" in sql.upper()
    rows = _q(SessionLocal, lambda db: _rows(db, "SELECT id, role, is_admin, session_epoch FROM users ORDER BY id"))
    assert rows == [(1, "admin", 1, "a"), (2, "user", 0, "b"), (3, "user", 0, "c")]

    async def remove_top_then_add(db):
        await db.execute(text("DELETE FROM users WHERE id = 3"))
        await db.execute(text("INSERT INTO users (role) VALUES ('user')"))
        await db.commit()
        return (await db.execute(text("SELECT max(id) FROM users"))).scalar()
    assert _q(SessionLocal, remove_top_then_add) == 4
    # Other tables still point at users.
    fk = _q(SessionLocal, lambda db: _rows(db, "PRAGMA foreign_key_list(characters)"))
    assert fk and fk[0][2] == "users"


def test_rebuild_runs_once(old_db):
    _, SessionLocal = old_db
    assert _q(SessionLocal, ensure_users_autoincrement) is True
    assert _q(SessionLocal, ensure_users_autoincrement) is False


def test_rebuild_refuses_a_table_with_columns_it_does_not_know(old_db):
    engine, SessionLocal = old_db

    async def add_col(db):
        await db.execute(text("ALTER TABLE users ADD COLUMN something_local TEXT"))
        await db.commit()
    _q(SessionLocal, add_col)
    assert _q(SessionLocal, ensure_users_autoincrement) is False
    sql = _q(SessionLocal, lambda db: _scalar(db, text(
        "SELECT sql FROM sqlite_master WHERE name = 'users'")))
    assert "AUTOINCREMENT" not in sql.upper()


async def _rows(db, sql):
    return [tuple(r) for r in (await db.execute(text(sql))).fetchall()]
