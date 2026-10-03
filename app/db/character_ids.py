"""Give an existing `characters` table AUTOINCREMENT, so a removed character's
id is never handed to the next character added (ISS-074).

Without AUTOINCREMENT, SQLite gives a new row max(id)+1, so removing the newest
character frees its id. Two writers update a character BY that id: the token
refresh in app/esi/client.py commits a Character object it loaded before the
SSO round trip, and the startup token-encryption pass in app/db/encryption.py
runs `UPDATE characters ... WHERE id = :id`. Had the id been reused in between,
either write would land on the other character's row, putting one pilot's
tokens and scopes over another's. With AUTOINCREMENT the stale write matches no
row instead (the ORM raises StaleDataError).

The model declares `sqlite_autoincrement`, which covers fresh installs through
create_all. This rebuilds the table once on installs that predate it, along
the lines of `ensure_users_autoincrement` (app/db/user_ids.py), with more
care, because this table holds every account's encrypted EVE tokens:

  - All of it runs in ONE transaction, taken with BEGIN IMMEDIATE. The decision
    to rebuild is re-made inside it, and any error rolls everything back, so
    the original table either survives untouched or is fully replaced.
  - Rows are copied with a plain INSERT ... SELECT. Nothing passes through the
    ORM, so the EncryptedText ciphertext is copied byte for byte.
  - Every index the live table had is replayed from its saved SQL after the
    rename, including any the model does not declare. DROP TABLE removes them,
    and losing the UNIQUE index on character_id would allow duplicate rows.
  - DROP TABLE also deletes the table's planner statistics (sqlite_stat1 and
    sqlite_stat4 rows, from ANALYZE or PRAGMA optimize). They are saved and
    written back, since the rows and index names they describe are unchanged.
  - Before COMMIT it checks that the copy holds exactly the same rows, the
    index list matches the old one, and AUTOINCREMENT and sqlite_sequence are
    in place. Any mismatch raises, which rolls back.
  - It refuses, with a warning and no change, if it can't be sure the result
    is complete: unknown columns (copying would drop them), an automatic
    index from an inline constraint (no SQL to replay, and the model's DDL
    would not recreate it), a trigger or view that mentions the table (the
    drop and rename would break or lose it), or foreign-key enforcement
    switched on (the drop would cascade or fail).

Foreign keys are not enforced on this app's connections (SQLite's default,
which the connect hook in app/db/models.py leaves alone), so dropping the old
table does not cascade. Other tables' `REFERENCES characters(character_id)` are
stored as text naming `characters`, and they resolve again to the renamed table.

No sequence floor is needed (unlike `ensure_users_sequence_floor`).
sqlite_sequence starts at max(id) at copy time, so an id freed BEFORE the
rebuild, and above that max, can still be handed out once afterwards. That is
harmless here because nothing keeps a characters.id once its row is gone:
  - every other table refers to a character by its EVE `character_id`;
  - the app never stores `characters.id` anywhere else;
  - the only holders of a by-id write are in-memory ORM objects and the
    encryption pass, and neither survives the restart this rebuild runs in.
    It runs in the blocking startup pass, before any background task starts
    or any request is served.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from app.db.models import Character

log = logging.getLogger(__name__)

_TABLE = "characters"
_TMP = "characters_autoincrement_rebuild"
_STAT_TABLES = ("sqlite_stat1", "sqlite_stat4")


def _new_table_ddl() -> str:
    """The model's CREATE TABLE, under the scratch name."""
    ddl = str(CreateTable(Character.__table__).compile(dialect=sqlite.dialect())).strip()
    prefix = f"CREATE TABLE {_TABLE} "
    if not ddl.startswith(prefix) or "AUTOINCREMENT" not in ddl.upper():
        # Not an assert: those vanish under python -O.
        raise RuntimeError(f"unexpected DDL for {_TABLE}: {ddl[:80]!r}")
    return f"CREATE TABLE {_TMP} " + ddl[len(prefix):]


async def _table_sql(db: AsyncSession) -> str | None:
    row = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"),
        {"n": _TABLE})).first()
    return None if row is None else (row[0] or "")


async def _index_signature(db: AsyncSession) -> list[tuple]:
    """(name, unique, origin, partial, columns) for every index on the table,
    sorted by name."""
    out = []
    for _seq, name, unique, origin, partial in (await db.execute(
            text(f"PRAGMA index_list({_TABLE})"))).all():
        cols = tuple(r[2] for r in (await db.execute(
            text(f'PRAGMA index_info("{name}")'))).all())
        out.append((name, unique, origin, partial, cols))
    return sorted(out)


async def _planner_stats(db: AsyncSession) -> dict[str, list[tuple]]:
    """This table's rows in each planner-statistics table that exists, in
    rowid order (which a write-back in the same order reproduces)."""
    present = {r[0] for r in (await db.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('sqlite_stat1', 'sqlite_stat4')"))).all()}
    return {
        t: [tuple(r) for r in (await db.execute(text(
            f"SELECT * FROM {t} WHERE tbl = :n ORDER BY rowid"), {"n": _TABLE})).all()]
        for t in _STAT_TABLES if t in present
    }


async def _restore_planner_stats(db: AsyncSession, stats: dict[str, list[tuple]]) -> None:
    for t, rows in stats.items():
        for row in rows:
            marks = ", ".join(f":p{i}" for i in range(len(row)))
            await db.execute(text(f"INSERT INTO {t} VALUES ({marks})"),
                             {f"p{i}": v for i, v in enumerate(row)})


async def _refusal(db: AsyncSession) -> str | None:
    """Why the live table can't be rebuilt safely, or None if it can."""
    if (await db.execute(text("PRAGMA foreign_keys"))).scalar():
        return "foreign key enforcement is on, so dropping the table would cascade"

    live = [r[1] for r in (await db.execute(text(f"PRAGMA table_info({_TABLE})"))).all()]
    known = set(Character.__table__.c.keys())
    unknown = [c for c in live if c not in known]
    if unknown:
        return f"unknown columns {unknown}; copying would drop them"

    auto = [r[0] for r in (await db.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :n "
        "AND sql IS NULL"), {"n": _TABLE})).all()]
    if auto:
        return (f"automatic index {auto} from an inline constraint has no SQL "
                "to replay, and the model's DDL would not recreate it")

    # A trigger on the table goes with DROP TABLE, and a view or trigger
    # elsewhere that names it would break the rename. The app creates
    # neither; refuse rather than guess at rewriting one.
    for kind, name in (await db.execute(text(
            "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view') "
            "AND (tbl_name = :n OR lower(sql) LIKE '%' || :n || '%')"), {"n": _TABLE})).all():
        return f"{kind} {name!r} refers to the table"
    return None


async def ensure_characters_autoincrement(db: AsyncSession) -> bool:
    """Rebuild `characters` with AUTOINCREMENT if it lacks it. Returns True if
    it rebuilt. Idempotent: a table that already has it, or no table at all,
    is left alone. Raises (after rolling back, original table intact) if
    anything goes wrong part-way."""
    sql = await _table_sql(db)
    if sql is None or "AUTOINCREMENT" in sql.upper():
        return False   # the usual case, decided without taking the write lock

    ddl = _new_table_ddl()
    started = time.perf_counter()
    await db.commit()   # nothing of ours may be pending when the explicit BEGIN runs
    try:
        # Explicit: the sqlite driver only opens a transaction before DML, so
        # without this the DDL below would run outside it. IMMEDIATE takes the
        # write lock now, so nothing changes between the checks and the swap.
        await db.execute(text("BEGIN IMMEDIATE"))

        # Decide again inside the transaction: this is the state it acts on.
        sql = await _table_sql(db)
        if sql is None or "AUTOINCREMENT" in sql.upper():
            await db.rollback()
            return False
        why = await _refusal(db)
        if why:
            await db.rollback()
            log.warning("characters table not rebuilt for AUTOINCREMENT, left unchanged: %s", why)
            return False

        cols = ", ".join(
            f'"{r[1]}"' for r in (await db.execute(text(f"PRAGMA table_info({_TABLE})"))).all())
        index_sql = [r[0] for r in (await db.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :n "
            "AND sql IS NOT NULL ORDER BY name"), {"n": _TABLE})).all()]
        indexes_before = await _index_signature(db)
        stats_before = await _planner_stats(db)
        rows, max_id = (await db.execute(text(f"SELECT count(*), max(id) FROM {_TABLE}"))).one()

        await db.execute(text(f"DROP TABLE IF EXISTS {_TMP}"))
        await db.execute(text(ddl))
        await db.execute(text(f"INSERT INTO {_TMP} ({cols}) SELECT {cols} FROM {_TABLE}"))

        # Same rows, value for value, before the original goes. id is unique,
        # so equal counts plus an empty difference mean identical contents.
        copied = (await db.execute(text(f"SELECT count(*) FROM {_TMP}"))).scalar()
        missing = (await db.execute(text(
            f"SELECT count(*) FROM (SELECT {cols} FROM {_TABLE} "
            f"EXCEPT SELECT {cols} FROM {_TMP})"))).scalar()
        if copied != rows or missing:
            raise RuntimeError(f"copy mismatch: {rows} rows, {copied} copied, {missing} differ")

        # The old table never had AUTOINCREMENT, so SQLite kept no sequence
        # row under its name; clear any stray one so the rename can't leave two.
        await db.execute(text("DELETE FROM sqlite_sequence WHERE name = :n"), {"n": _TABLE})
        await db.execute(text(f"DROP TABLE {_TABLE}"))
        await db.execute(text(f"ALTER TABLE {_TMP} RENAME TO {_TABLE}"))
        for stmt in index_sql:
            await db.execute(text(stmt))
        await _restore_planner_stats(db, stats_before)

        sql_after = await _table_sql(db)
        if sql_after is None or "AUTOINCREMENT" not in sql_after.upper():
            raise RuntimeError("rebuilt table lacks AUTOINCREMENT")
        indexes_after = await _index_signature(db)
        if indexes_after != indexes_before:
            raise RuntimeError(f"index mismatch: had {indexes_before}, now {indexes_after}")
        if await _planner_stats(db) != stats_before:
            raise RuntimeError("planner statistics were not restored")
        if (await db.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar() != rows:
            raise RuntimeError("row count changed across the rename")
        seq = (await db.execute(text(
            "SELECT seq FROM sqlite_sequence WHERE name = :n"), {"n": _TABLE})).all()
        if seq != ([] if max_id is None else [(max_id,)]):
            raise RuntimeError(f"sqlite_sequence is {seq}, expected max id {max_id}")

        await db.commit()
    except Exception:
        await db.rollback()
        raise
    log.info("characters table rebuilt with AUTOINCREMENT: %d rows, %d indexes, %.3fs",
             rows, len(index_sql), time.perf_counter() - started)
    return True
