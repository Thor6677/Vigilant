"""T-080: load_wallet_baselines() must resolve every pilot's 7-day-ago
balance via per-character index seeks, never a table-wide window/sort.

The old ROW_NUMBER() OVER (PARTITION BY character_id ...) window measured
8.27s for 24 pilots against a real install's ~1.9M-row wallet_snapshots
table, because SQLite has to rank the entire before-cutoff slice before it
can pick row 1 of each partition. The fix (see app/dashboard/walletdelta.py)
is a UNION ALL of one independent, per-character scalar subquery per id —
each one answered by a single index seek on
ix_wallet_snapshots_char_recorded.

Two things below: an EXPLAIN QUERY PLAN check that the generated SQL
actually seeks the composite index and never builds a temp B-tree/window
over wallet_snapshots, and a perf regression test against a ~300k-row table
(schema built the same way production does, via Base.metadata.create_all,
so the real ix_wallet_snapshots_character_id index is there too and the
planner has both to choose from) bulk-loaded with executemany.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard.walletdelta import _build_wallet_baselines_stmt, load_wallet_baselines
from app.db.models import Base

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CUTOFF = (NOW - timedelta(days=7)).replace(tzinfo=None)


def _explain_lines(character_ids: list[int]) -> list[str]:
    stmt = _build_wallet_baselines_stmt(character_ids, CUTOFF)
    sql = str(stmt.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}))

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                result = await conn.execute(text("EXPLAIN QUERY PLAN " + sql))
                return [row[3] for row in result.all()]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_baselines_query_seeks_composite_index_not_a_window():
    lines = _explain_lines([1, 2, 3, 24])
    plan = "\n".join(lines)
    assert (
        "USING INDEX ix_wallet_snapshots_char_recorded" in plan
        or "USING COVERING INDEX ix_wallet_snapshots_char_recorded" in plan
    ), plan
    # No window/sort machinery built over wallet_snapshots at all: the old
    # ROW_NUMBER() plan needed a temp B-tree to rank the whole filtered set.
    assert not any("B-TREE" in line.upper() for line in lines), plan
    # Every character gets its own SEARCH against the composite index —
    # never a single full-table SCAN.
    assert "SCAN wallet_snapshots" not in plan
    assert plan.count("ix_wallet_snapshots_char_recorded") >= 4


# ── perf regression: ~300k rows / 24 characters, bulk-loaded ────────────────

N_CHARACTERS = 24
ROWS_PER_CHARACTER = 12_500  # ~300k total
STEP = timedelta(minutes=2)
START_BALANCE = 1_000_000.0
T0 = NOW.replace(tzinfo=None) - ROWS_PER_CHARACTER * STEP


def _expected_baseline(cid: int) -> float | None:
    """Row i (0-indexed) for character cid has recorded_at = T0 + i*STEP and
    balance = START_BALANCE + cid + (i + 1) (balance starts at
    START_BALANCE + cid and is incremented by 1.0 before each insert — see
    _bulk_seed). The baseline is the balance of the last row at or before
    CUTOFF."""
    i_cutoff = (CUTOFF - T0) // STEP  # exact integer floor division of two timedeltas
    if i_cutoff < 0:
        return None
    i_cutoff = min(i_cutoff, ROWS_PER_CHARACTER - 1)
    return START_BALANCE + cid + (i_cutoff + 1)


def _bulk_seed(db_path: str) -> None:
    """Real schema (Base.metadata.create_all via a sync engine — the same
    indexes production has, including the single-column
    ix_wallet_snapshots_character_id the planner could otherwise pick),
    then a fast bulk insert via sqlite3 executemany in one transaction."""
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        rows = []
        for cid in range(1, N_CHARACTERS + 1):
            t = T0
            balance = START_BALANCE + cid
            for _ in range(ROWS_PER_CHARACTER):
                balance += 1.0
                rows.append((cid, balance, t.isoformat(sep=" ")))
                t += STEP
        conn.executemany(
            "INSERT INTO wallet_snapshots (character_id, balance, recorded_at) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("walletdelta_perf") / "wallet.db"
    _bulk_seed(str(path))
    return str(path)


def test_load_wallet_baselines_perf_and_correctness(seeded_db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{seeded_db_path}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    character_ids = list(range(1, N_CHARACTERS + 1))

    async def _run():
        async with SessionLocal() as db:
            # Warm the OS/page cache once so the timed calls measure the
            # query plan, not cold disk I/O.
            await load_wallet_baselines(db, character_ids, now=NOW)

            import time
            t0 = time.perf_counter()
            for _ in range(10):
                result = await load_wallet_baselines(db, character_ids, now=NOW)
            elapsed_ms = (time.perf_counter() - t0) / 10 * 1000
            return result, elapsed_ms

    result, elapsed_ms = asyncio.run(_run())
    asyncio.run(engine.dispose())

    assert elapsed_ms < 100, f"load_wallet_baselines took {elapsed_ms:.2f}ms, expected well under 100ms"
    expected = {cid: _expected_baseline(cid) for cid in character_ids}
    expected = {cid: bal for cid, bal in expected.items() if bal is not None}
    assert result == pytest.approx(expected)
