"""ISS-074: a removed character's `characters.id` is never handed to the next
character added.

Without AUTOINCREMENT, SQLite gives a new row max(rowid)+1, so removing the
newest character frees its id for whichever character is added next. Two
writers update a character BY that id:

  - the token refresh in app/esi/client.py, which holds a Character ORM object
    across the SSO round trip and commits it afterwards
    (UPDATE characters SET access_token=..., refresh_token=..., ... WHERE id=?);
  - the startup token-encryption pass in app/db/encryption.py, a raw
    UPDATE characters SET access_token=:at, refresh_token=:rt WHERE id=:id.

If the id were reused while one of them still held the removed character, its
write would land on the other character's row: one pilot's tokens and scopes
silently written over another's. With AUTOINCREMENT the new character gets a
fresh id, so the stale write matches no row (StaleDataError for the ORM, a
rowcount of 0 for raw SQL) and the other character is untouched.

These tests build the table from the model, as a fresh install does. The
one-time rebuild of an existing install's table is covered in
tests/test_character_ids_rebuild.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError

from app.db.models import Character, User
from app.esi import client as esi_client

# Fake EVE character ids.
OLDER_EVE_ID = 91_000_001
A_EVE_ID = 91_000_002
B_EVE_ID = 91_000_003


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char(eve_id, name, at, rt, *, user_id, scopes="", expiry=None):
    return Character(
        character_id=eve_id, character_name=name,
        access_token=at, refresh_token=rt,
        token_expiry=expiry or datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=scopes, user_id=user_id,
    )


async def _seed(Session, *, a_expiry=None):
    """One account with two characters; "Pilot A" is the newest row."""
    async with Session() as db:
        user = User(role="user")
        db.add(user)
        await db.commit()
        db.add(_char(OLDER_EVE_ID, "Pilot Older", "at-older", "rt-older", user_id=user.id))
        await db.commit()
        db.add(_char(A_EVE_ID, "Pilot A", "at-A", "rt-A", user_id=user.id,
                     scopes="scope.a", expiry=a_expiry))
        await db.commit()
        return user.id


async def _remove_a_and_add_b(Session, user_id):
    """The account removes Pilot A and adds Pilot B straight after. Returns
    B's characters.id."""
    async with Session() as db:
        await db.execute(delete(Character).where(Character.character_id == A_EVE_ID))
        await db.commit()
        b = _char(B_EVE_ID, "Pilot B", "at-B", "rt-B", user_id=user_id, scopes="scope.b")
        db.add(b)
        await db.commit()
        return b.id


async def _b_as_stored(Session):
    async with Session() as db:
        b = (await db.execute(
            select(Character).where(Character.character_id == B_EVE_ID))).scalar_one()
        return b.access_token, b.refresh_token, b.scopes


def _scenario(tmp_path, body, *, a_expiry=None):
    """Fresh-install schema (create from the model), seeded, then `body`."""
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chars.db'}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: User.__table__.create(c))
                await conn.run_sync(lambda c: Character.__table__.create(c))
            user_id = await _seed(Session, a_expiry=a_expiry)
            return await body(Session, user_id)
        finally:
            await engine.dispose()
    return _run(go())


def test_a_stale_commit_cannot_land_on_the_character_added_after_it(tmp_path):
    """The core reuse: A is loaded in one session, removed and replaced by B
    in another, then the first session commits A."""
    async def body(Session, user_id):
        async with Session() as s1:
            a = (await s1.execute(
                select(Character).where(Character.character_id == A_EVE_ID))).scalar_one()
            a_id = a.id
            b_id = await _remove_a_and_add_b(Session, user_id)

            a.access_token = "at-A-new"
            a.refresh_token = "rt-A-new"
            a.scopes = "scope.a.new"
            err = None
            try:
                await s1.commit()
            except StaleDataError as e:
                err = e
                await s1.rollback()
        return a_id, b_id, err, await _b_as_stored(Session)

    a_id, b_id, err, b_stored = _scenario(tmp_path, body)
    # B keeps its own tokens and scopes: A's write did not land on B's row.
    assert b_stored == ("at-B", "rt-B", "scope.b")
    assert b_id != a_id
    # The stale write matched no row, and the ORM says so.
    assert isinstance(err, StaleDataError)


def test_a_token_refresh_in_flight_cannot_write_onto_the_replacement(tmp_path, monkeypatch):
    """The same reuse through the real refresh path: A's token is refreshed,
    and A is removed and B added while the SSO request is in flight. The
    refresh's commit must not write A's new tokens onto B."""
    state = {}

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"access_token": "at-A-refreshed", "refresh_token": "rt-A-refreshed",
                    "expires_in": 1200}

    class HC:
        async def post(self, *a, **k):
            state["b_id"] = await _remove_a_and_add_b(state["Session"], state["user_id"])
            return Resp()

    monkeypatch.setattr(esi_client, "get_http_client", lambda: HC())

    async def body(Session, user_id):
        state.update(Session=Session, user_id=user_id)
        async with Session() as s1:
            a = (await s1.execute(
                select(Character).where(Character.character_id == A_EVE_ID))).scalar_one()
            state["a_id"] = a.id
            err = None
            try:
                await esi_client.refresh_token(a, s1)
            except StaleDataError as e:
                err = e
                await s1.rollback()
        return err, await _b_as_stored(Session)

    # A's token has expired, so refresh_token() goes to SSO.
    err, b_stored = _scenario(
        tmp_path, body, a_expiry=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert b_stored == ("at-B", "rt-B", "scope.b")
    assert state["b_id"] != state["a_id"]
    assert isinstance(err, StaleDataError)


def test_a_refresh_of_a_removed_character_does_not_load_its_replacement(tmp_path, monkeypatch):
    """refresh_token() re-reads the character by id before deciding whether
    to refresh. If A's id had been reused, that re-read would load B's row
    into A's object and hand B's access token to A's caller."""
    def no_sso():
        raise AssertionError("SSO must not be called")
    monkeypatch.setattr(esi_client, "get_http_client", no_sso)

    async def body(Session, user_id):
        async with Session() as s1:
            a = (await s1.execute(
                select(Character).where(Character.character_id == A_EVE_ID))).scalar_one()
            await _remove_a_and_add_b(Session, user_id)
            token, err = None, None
            try:
                token = await esi_client.refresh_token(a, s1)
            except InvalidRequestError as e:
                err = e
            # Whatever A's object holds now, read without a lazy load.
            return token, err, inspect(a).dict.get("character_id")

    # Close to expiry, so refresh_token() takes the lock and re-reads the row.
    token, err, a_eve_id = _scenario(
        tmp_path, body, a_expiry=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert token != "at-B"
    assert a_eve_id != B_EVE_ID
    assert isinstance(err, InvalidRequestError)


def test_a_raw_update_by_id_matches_nothing_after_a_removal(tmp_path):
    """The startup encryption pass's statement, keyed by a removed id."""
    async def body(Session, user_id):
        async with Session() as db:
            a_id = (await db.execute(
                select(Character.id).where(Character.character_id == A_EVE_ID))).scalar_one()
        await _remove_a_and_add_b(Session, user_id)
        async with Session() as db:
            res = await db.execute(
                text("UPDATE characters SET access_token = :at, refresh_token = :rt WHERE id = :id"),
                {"at": "raw-at", "rt": "raw-rt", "id": a_id})
            await db.commit()
        return res.rowcount, await _b_as_stored(Session)

    rowcount, b_stored = _scenario(tmp_path, body)
    assert b_stored == ("at-B", "rt-B", "scope.b")
    assert rowcount == 0


def test_a_fresh_install_creates_characters_with_autoincrement(tmp_path):
    async def body(Session, user_id):
        async with Session() as db:
            return (await db.execute(text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'characters'"))).scalar()
    assert "AUTOINCREMENT" in _scenario(tmp_path, body).upper()
