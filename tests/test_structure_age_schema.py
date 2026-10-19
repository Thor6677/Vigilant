"""Structure age schema (user anchor points): calibration rows default to
source 'triff', structure_age_submissions allows one pending row per
structure ID, and the startup migration upgrades the pre-release table.
Ids are invented."""
import asyncio
import inspect
import tempfile

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base, _create_missing_indexes

MIGRATION = ("ALTER TABLE structure_age_calibration "
             "ADD COLUMN source VARCHAR(8) NOT NULL DEFAULT 'triff'")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blank_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")


async def _engine():
    eng = _blank_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return eng


def test_calibration_rows_default_to_triff():
    async def go():
        eng = await _engine()
        try:
            async with eng.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO structure_age_calibration "
                    "(structure_id, anchor_mid, anchor_low, anchor_high, days_wide, fetched_at) "
                    "VALUES (1050000000000, '2026-01-01 00:00:00', '2025-12-29 00:00:00', "
                    "'2026-01-04 00:00:00', 6.0, '2026-01-05 00:00:00')"))
                return (await conn.execute(text(
                    "SELECT source FROM structure_age_calibration"))).scalar()
        finally:
            await eng.dispose()
    assert _run(go()) == "triff"


def test_one_pending_submission_per_structure_id():
    insert = text(
        "INSERT INTO structure_age_submissions "
        "(structure_id, anchor_date, certainty, status, submitted_at) "
        "VALUES (:sid, '2026-09-30', 'day', :status, '2026-10-01 00:00:00')")

    async def go():
        eng = await _engine()
        try:
            async with eng.begin() as conn:
                # A rejected row doesn't block a new pending one for the same ID.
                await conn.execute(insert, {"sid": 1055400000000, "status": "rejected"})
                await conn.execute(insert, {"sid": 1055400000000, "status": "pending"})
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                async with eng.begin() as conn:
                    await conn.execute(insert, {"sid": 1055400000000, "status": "pending"})
            async with eng.begin() as conn:
                await conn.execute(insert, {"sid": 1055400000001, "status": "pending"})
                return (await conn.execute(text(
                    "SELECT count(*) FROM structure_age_submissions "
                    "WHERE status = 'pending'"))).scalar()
        finally:
            await eng.dispose()
    assert _run(go()) == 2


def test_startup_migration_upgrades_the_pre_release_table():
    """Prod's table before this release has no `source`. The statement
    app/main.py runs at startup must apply, and old rows read 'triff'.

    Replays init_db's order (create_all -> _create_missing_indexes -> ALTER),
    so an index declared on `source` would crash here as it would at boot."""
    import app.main as main
    assert MIGRATION in inspect.getsource(main)

    async def go():
        eng = _blank_engine()
        try:
            async with eng.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE structure_age_calibration (structure_id BIGINT PRIMARY KEY, "
                    "anchor_mid DATETIME NOT NULL, anchor_low DATETIME NOT NULL, "
                    "anchor_high DATETIME NOT NULL, days_wide FLOAT NOT NULL, "
                    "fetched_at DATETIME NOT NULL)"))
                await conn.execute(text(
                    "INSERT INTO structure_age_calibration VALUES (1050000000000, "
                    "'2026-01-01 00:00:00', '2025-12-29 00:00:00', '2026-01-04 00:00:00', "
                    "6.0, '2026-01-05 00:00:00')"))
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(_create_missing_indexes)
                await conn.execute(text(MIGRATION))
                source = (await conn.execute(text(
                    "SELECT source FROM structure_age_calibration"))).scalar()
                indexes = {r[0] for r in await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index'"))}
                return source, indexes
        finally:
            await eng.dispose()
    source, indexes = _run(go())
    assert source == "triff"
    assert "ix_structure_age_calibration_anchor_mid" in indexes
