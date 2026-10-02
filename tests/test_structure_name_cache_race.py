"""ISS-085: two lookups of the same structure at once both succeed.

In one sync the location fetcher and the asset resolver can resolve the same
player structure at the same moment, each on its own session. Both find it
missing from structure_name_cache, both ask ESI, and both store the name. The
store must not fail for whichever comes second: before the fix the loser hit
the table's UNIQUE key and its whole field failed for that sync.
"""
import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 (populate Base.metadata)
from app.db.models import Base, StructureNameCache, ensure_wal
from app.esi import universe

STRUCTURE, SYSTEM, OTHER_SYSTEM = 1032000000001, 30000142, 30000144


class _Client:
    async def get(self, path, *a, **k):
        assert path == f"/universe/structures/{STRUCTURE}/"
        await asyncio.sleep(0.01)
        return {"name": "Test Citadel", "solar_system_id": SYSTEM}


async def _engine(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'structures.db'}"
    ensure_wal(url)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _rows(SessionLocal):
    async with SessionLocal() as db:
        return [(r.structure_id, r.name, r.solar_system_id)
                for r in (await db.execute(select(StructureNameCache))).scalars()]


def test_two_concurrent_lookups_of_one_structure_both_succeed(tmp_path):
    async def go():
        engine, SessionLocal = await _engine(tmp_path)
        try:
            # Hold each lookup after every read of the cache table until the
            # other has made the same read: both see the structure missing
            # before either stores it, which is the race.
            both_read = asyncio.Barrier(2)

            async def lookup():
                async with SessionLocal() as db:
                    real = db.execute

                    async def execute(stmt, *a, **k):
                        result = await real(stmt, *a, **k)
                        if getattr(stmt, "is_select", False) and "structure_name_cache" in str(stmt):
                            await asyncio.wait_for(both_read.wait(), 5)
                        return result
                    db.execute = execute
                    return await universe.get_structure(_Client(), STRUCTURE, db=db)

            results = await asyncio.gather(lookup(), lookup(), return_exceptions=True)
            return results, await _rows(SessionLocal)
        finally:
            await engine.dispose()

    results, rows = asyncio.run(go())
    assert results == [{"name": "Test Citadel", "solar_system_id": SYSTEM}] * 2
    assert rows == [(STRUCTURE, "Test Citadel", SYSTEM)]


def test_storing_a_known_structure_updates_its_name_and_keeps_a_known_system(tmp_path):
    """A later store renames the structure. It replaces the system only when
    it brings one, as before."""
    async def go():
        engine, SessionLocal = await _engine(tmp_path)
        try:
            async with SessionLocal() as db:
                await universe.cache_structure_name(db, STRUCTURE, "Old Name", SYSTEM)
            first = await _rows(SessionLocal)
            async with SessionLocal() as db:
                stamped = (await db.execute(select(StructureNameCache.updated_at))).scalar_one()
                await db.execute(StructureNameCache.__table__.update().values(
                    updated_at=datetime(2000, 1, 1)))
                await db.commit()
            async with SessionLocal() as db:
                await universe.cache_structure_name(db, STRUCTURE, "New Name")
            renamed = await _rows(SessionLocal)
            async with SessionLocal() as db:
                restamped = (await db.execute(select(StructureNameCache.updated_at))).scalar_one()
                await universe.cache_structure_name(db, STRUCTURE, "New Name", OTHER_SYSTEM)
            moved = await _rows(SessionLocal)
            return first, stamped, renamed, restamped, moved
        finally:
            await engine.dispose()

    first, stamped, renamed, restamped, moved = asyncio.run(go())
    assert first == [(STRUCTURE, "Old Name", SYSTEM)]
    assert stamped is not None
    assert renamed == [(STRUCTURE, "New Name", SYSTEM)]
    assert restamped > datetime(2000, 1, 1)
    assert moved == [(STRUCTURE, "New Name", OTHER_SYSTEM)]
