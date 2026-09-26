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

The rebuild alone is not the whole guarantee: sqlite_sequence starts from the
highest id present in `users` at copy time, not the highest id ever handed
out. An account deleted before the rebuild ran, whose id was above that copy-
time max, left that id free — the next signup after the rebuild would get it
once, and inherit anything still keyed to it (characters, caches, settings,
audit attribution). `ensure_users_sequence_floor` closes that gap by raising
sqlite_sequence to the highest id any scanned column still references, so a
freed id above that floor is never handed out while something could still
inherit from it. It says nothing about an id that survives only inside a
free-text field (an audit log `detail` string naming "user 42", say) rather
than a column value — that id could still come back once, but there is
nothing left in the schema for it to inherit, since it was never in a column
this floor scans in the first place.
"""
from __future__ import annotations

import logging

from sqlalchemy import Integer, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from app.db.models import Base, User

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


# Columns that hold a users.id but aren't discoverable from Base.metadata as
# a ForeignKey to users.id, and aren't named `user_id` either (that bare-name
# case is handled directly in _user_reference_columns). Each is written from
# `admin.id` in app/routes/admin.py: update_schedule.created_by at the
# schedule-request handler (~line 1464), update_policy.updated_by at the
# policy-save handler (~line 1545), update_run_report.acknowledged_by at the
# acknowledge handler (~line 1665), update_notify_settings.updated_by at the
# notify-settings save handler (~line 1609).
_EXTRA_USER_REF_COLUMNS = (
    ("update_schedule", "created_by"),
    ("update_policy", "updated_by"),
    ("update_run_report", "acknowledged_by"),
    ("update_notify_settings", "updated_by"),
)


def _user_reference_columns() -> set[tuple[str, str]]:
    """Every (table, column) that can hold a users.id — what "referenced
    anywhere" in the module docstring actually means, in code.

    Three sources:
      - `users.id` itself.
      - Every column declared with `ForeignKey("users.id")` anywhere in
        Base.metadata — characters, caches, per-user settings, the audit
        log, and so on.
      - Any integer column literally named `user_id` that has no declared
        FK. A handful of tables (net worth snapshots, stockpile targets,
        d-scan results, hosted images) carry a plain `user_id` for
        denormalization or optional attribution rather than a relationship,
        so they predate — or deliberately skip — the FK.
      - `_EXTRA_USER_REF_COLUMNS`, a short explicit list of admin-attribution
        columns under some other name (see its comment).
    The app has no tables outside Base.metadata: no raw CREATE TABLE and no
    Core `Table(...)` definitions other than this module's own rebuild
    scratch table (app/db/user_ids.py's `_TMP`), which is renamed to `users`
    before this ever runs, not left standing under its scratch name.
    """
    found = {("users", "id")}
    for table in Base.metadata.tables.values():
        for col in table.columns:
            has_users_fk = any(
                fk.column.table.name == "users" and fk.column.name == "id"
                for fk in col.foreign_keys
            )
            if has_users_fk:
                found.add((table.name, col.name))
            elif col.name == "user_id" and isinstance(col.type, Integer):
                found.add((table.name, col.name))
    found.update(_EXTRA_USER_REF_COLUMNS)
    return found


async def ensure_users_sequence_floor(db: AsyncSession) -> bool:
    """Raise `users`' sqlite_sequence value to at least the highest user id
    referenced anywhere. Returns True if it raised the value.

    ensure_users_autoincrement stops SQLite from reusing an id going
    forward, but sqlite_sequence starts from max(users.id) at copy time —
    an id that was freed by a deletion *before* the rebuild ran, and was
    above that copy-time max, is still handed out once. This scans every
    column in `_user_reference_columns` for the true high-water mark and
    writes it into sqlite_sequence when that's higher than what's there.

    Idempotent, and safe to call on any database:
      - `users` without AUTOINCREMENT is left alone. A fresh install already
        has it via `sqlite_autoincrement` on the model; the only way to lack
        it here is a rebuild that bailed on unknown columns (see
        `ensure_users_autoincrement`) — and a table that was never rebuilt
        was never given the guarantee this floor exists to protect, so there
        is nothing to raise.
      - A table or column this DB doesn't have yet is skipped.
      - The sequence is never lowered; a call that finds nothing higher than
        what's already recorded does no write and logs nothing.
    """
    row = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"))).first()
    if row is None or "AUTOINCREMENT" not in (row[0] or "").upper():
        return False

    live_tables = {r[0] for r in (await db.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'table'"))).fetchall()}

    highest = 0
    for table_name, col_name in _user_reference_columns():
        if table_name not in live_tables:
            continue
        live_cols = {r[1] for r in (await db.execute(
            text(f"PRAGMA table_info({table_name})"))).fetchall()}
        if col_name not in live_cols:
            continue
        value = (await db.execute(text(
            f"SELECT MAX({col_name}) FROM {table_name}"))).scalar()
        if value is not None and value > highest:
            highest = value

    if highest <= 0:
        return False

    current = (await db.execute(text(
        "SELECT seq FROM sqlite_sequence WHERE name = 'users'"))).scalar()
    if current is None:
        await db.execute(text(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('users', :v)"), {"v": highest})
        await db.commit()
        log.info("users sqlite_sequence initialized to %d", highest)
        return True
    if highest > current:
        await db.execute(text(
            "UPDATE sqlite_sequence SET seq = :v WHERE name = 'users'"), {"v": highest})
        await db.commit()
        log.info("users sqlite_sequence raised from %d to %d", current, highest)
        return True
    return False
