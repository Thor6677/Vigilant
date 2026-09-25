"""Give an existing `users` table AUTOINCREMENT, so a deleted id is never reused.

Without AUTOINCREMENT, SQLite hands out max(id)+1, so removing the newest
account frees its id for the next signup — which then inherits anything still
keyed to that id. The model declares `sqlite_autoincrement`, which covers fresh
installs through create_all; this rebuilds the table once on installs that
predate it. SQLite cannot add AUTOINCREMENT in place, so this follows its
documented table-rebuild procedure: create the new table, copy, drop the old
one, rename. Foreign keys are not enforced on this app's connections, so the
drop does not cascade, and other tables' `REFERENCES users(id)` resolve to the
renamed table afterwards.

Ids that were already freed before this runs can still be handed out once:
sqlite_sequence starts from the highest id present at copy time.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from app.db.models import User

log = logging.getLogger(__name__)

_TMP = "users_autoincrement_rebuild"


async def ensure_users_autoincrement(db: AsyncSession) -> bool:
    """Rebuild `users` with AUTOINCREMENT if it lacks it. Returns True if it
    rebuilt. Idempotent: a table that already has it is left alone."""
    row = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"))).first()
    if row is None or "AUTOINCREMENT" in (row[0] or "").upper():
        return False

    live = [r[1] for r in (await db.execute(text("PRAGMA table_info(users)"))).fetchall()]
    known = set(User.__table__.c.keys())
    unknown = [c for c in live if c not in known]
    if unknown:
        # Copying would drop these columns. Leave the table as it is.
        log.warning("users table not rebuilt for AUTOINCREMENT: unknown columns %s", unknown)
        return False
    cols = ", ".join(c for c in live)

    ddl = str(CreateTable(User.__table__).compile(dialect=sqlite.dialect())).strip()
    ddl = ddl.replace("CREATE TABLE users ", f"CREATE TABLE {_TMP} ", 1)
    assert ddl.startswith(f"CREATE TABLE {_TMP} ") and "AUTOINCREMENT" in ddl

    await db.commit()   # nothing of ours may be pending when the explicit BEGIN runs
    try:
        # Explicit: the sqlite driver only opens a transaction before DML, so
        # without this the CREATE would run outside it.
        await db.execute(text("BEGIN IMMEDIATE"))
        await db.execute(text(f"DROP TABLE IF EXISTS {_TMP}"))
        await db.execute(text(ddl))
        await db.execute(text(f"INSERT INTO {_TMP} ({cols}) SELECT {cols} FROM users"))
        await db.execute(text("DROP TABLE users"))
        await db.execute(text(f"ALTER TABLE {_TMP} RENAME TO users"))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    log.info("users table rebuilt with AUTOINCREMENT")
    return True
