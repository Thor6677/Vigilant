"""T-080: ix_wallet_snapshots_char_recorded must never be built inline at
startup against an already-existing wallet_snapshots table — building it
against a real install's table (1.9M rows) measured 5.35s, and the deploy
health check only retries for ~20s, so a blocking build risks a failed
deploy and an automatic revert.

Two behaviours under test:
  1. `_create_missing_indexes` (the blocking startup pass) skips this named
     index — an "existing table missing it" is exactly what an upgrade from
     an older release looks like.
  2. `create_wallet_snapshot_index_background` (the one-shot task started
     from app.main's startup handler) builds it, is idempotent, and never
     raises even if the build fails outright.

A FRESH install is covered by the existing dashboard/walletdelta and
dashboard/detail EXPLAIN tests, which run Base.metadata.create_all (the
fresh-install path) and see the index in the plan immediately — this file
is only about the deferred-on-upgrade path.
"""
import asyncio
import tempfile

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models as models_mod
from app.db.models import Base, _create_missing_indexes, create_wallet_snapshot_index_background

INDEX_NAME = "ix_wallet_snapshots_char_recorded"


async def _index_exists(engine, name: str) -> bool:
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name},
        )
        return res.first() is not None


def _temp_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")


def test_create_missing_indexes_skips_the_deferred_wallet_index():
    async def _run():
        engine = _temp_engine()
        try:
            async with engine.begin() as conn:
                # Fresh schema (as create_all would build it), then drop the
                # composite index to simulate an old-shape table from before
                # T-076/T-080 — exactly what _create_missing_indexes exists
                # to fix up for every OTHER index.
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text(f"DROP INDEX {INDEX_NAME}"))
            assert not await _index_exists(engine, INDEX_NAME)

            async with engine.begin() as conn:
                await conn.run_sync(_create_missing_indexes)

            # Still absent: the blocking startup pass must not have rebuilt it.
            assert not await _index_exists(engine, INDEX_NAME)
            # But an ordinary index (never deferred) IS restored by the same
            # pass, proving _create_missing_indexes still does its job for
            # everything else.
            assert await _index_exists(engine, "ix_wallet_snapshots_character_id")
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_background_index_creator_builds_it_and_is_idempotent(monkeypatch):
    async def _run():
        engine = _temp_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text(f"DROP INDEX {INDEX_NAME}"))
            assert not await _index_exists(engine, INDEX_NAME)

            monkeypatch.setattr(models_mod, "engine", engine)
            await create_wallet_snapshot_index_background(delay=0)
            assert await _index_exists(engine, INDEX_NAME)

            # Idempotent: CREATE INDEX IF NOT EXISTS, called again, is a no-op.
            await create_wallet_snapshot_index_background(delay=0)
            assert await _index_exists(engine, INDEX_NAME)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_background_index_creator_never_raises_on_failure(monkeypatch, caplog):
    async def _run():
        # An engine with NO schema at all: CREATE INDEX ... ON wallet_snapshots
        # fails with "no such table" — the function must swallow it.
        engine = _temp_engine()
        try:
            monkeypatch.setattr(models_mod, "engine", engine)
            with caplog.at_level("WARNING"):
                await create_wallet_snapshot_index_background(delay=0)  # must not raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
    assert any(
        "ix_wallet_snapshots_char_recorded" in r.message and "failed" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_background_index_creator_logs_elapsed_seconds_on_success(monkeypatch, caplog):
    async def _run():
        engine = _temp_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text(f"DROP INDEX {INDEX_NAME}"))
            monkeypatch.setattr(models_mod, "engine", engine)
            with caplog.at_level("INFO"):
                await create_wallet_snapshot_index_background(delay=0)
        finally:
            await engine.dispose()

    asyncio.run(_run())
    assert any(
        "Built ix_wallet_snapshots_char_recorded" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]
