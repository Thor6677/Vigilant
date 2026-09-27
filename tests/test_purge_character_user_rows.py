"""purge_character_user_rows: one hook every character-removal path calls, so a
feature that stores per-character rows for a user only has to register its
table in PER_CHARACTER_USER_TABLES (app/auth/purge.py).
"""
import asyncio
import inspect

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — populate Base.metadata
from app.auth import purge
from app.db.models import Base


def test_every_registered_table_exists_with_a_character_id_column():
    for table in purge.PER_CHARACTER_USER_TABLES:
        assert table in Base.metadata.tables, f"{table} is not a known table"
        assert "character_id" in Base.metadata.tables[table].columns, (
            f"{table} has no character_id column, so purge_character_user_rows can't clear it")


def test_every_removal_path_calls_the_hook():
    from app.auth.routes import _release_transferred, remove_character
    from app.routes.admin import admin_remove_character, admin_remove_user
    for fn in (remove_character, _release_transferred, admin_remove_character, admin_remove_user):
        assert "purge_character_user_rows(" in inspect.getsource(fn), (
            f"{fn.__name__} takes a character off an account but doesn't call purge_character_user_rows")


def test_deletes_only_the_given_characters_rows(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(purge, "PER_CHARACTER_USER_TABLES", ("t_one", "t_two"))

    async def run():
        async with SessionLocal() as db:
            for t in ("t_one", "t_two"):
                await db.execute(text(f"CREATE TABLE {t} (user_id INTEGER, character_id INTEGER)"))
                await db.execute(text(f"INSERT INTO {t} VALUES (1, 100), (1, 200), (2, 100)"))
            await db.commit()
            deleted = await purge.purge_character_user_rows(db, 100)
            await db.commit()
            left = {
                t: (await db.execute(text(f"SELECT character_id FROM {t}"))).scalars().all()
                for t in ("t_one", "t_two")
            }
        await engine.dispose()
        return deleted, left

    deleted, left = asyncio.run(run())
    assert deleted == 4
    assert left == {"t_one": [200], "t_two": [200]}
