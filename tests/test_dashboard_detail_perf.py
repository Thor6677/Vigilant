"""T-080: load_wallet_sparkline_points() must downsample in SQL, not fetch
every snapshot in the 7-day window and group it in Python; and
load_latest_networth() must seek NetWorthSnapshot's composite PK per
character, never rank the whole table with a ROW_NUMBER() window (found
while auditing this release's other wallet_snapshots/net_worth_snapshots
queries — see the walletdelta perf test's module docstring for the same
audit on load_wallet_baselines).

A real install writes a WalletSnapshot roughly every 2 minutes, so the old
"every row in the window" sparkline query measured ~126k rows for 24 pilots
on a Detailed/Table dashboard load. The fix buckets the window into 3-hour
buckets in SQL and keeps only the latest balance per (character_id,
bucket) — at most ~56 rows per pilot come back.

Three things below: an EXPLAIN QUERY PLAN check for each query, and a perf
regression test against a ~300k-row wallet_snapshots table (schema built
via Base.metadata.create_all, so the real ix_wallet_snapshots_character_id
index is there too) bulk-loaded with executemany. Correctness (one
statement, empty-list, no-history, "last balance in bucket wins") is
covered in tests/test_dashboard_detail_io.py — this file is timing and
query-plan only.
"""
import asyncio
import calendar
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard.detail import (
    SPARKLINE_BUCKET,
    _build_latest_networth_stmt,
    _build_wallet_sparkline_stmt,
    load_latest_networth,
    load_wallet_sparkline_points,
)
from app.db.models import Base

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CUTOFF = (NOW - timedelta(days=7)).replace(tzinfo=None)


def _explain(sql: str) -> list[str]:
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


def test_sparkline_query_range_scans_composite_index():
    stmt = _build_wallet_sparkline_stmt(list(range(1, 25)), CUTOFF)
    sql = str(stmt.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}))
    lines = _explain(sql)
    plan = "\n".join(lines)
    assert "SEARCH wallet_snapshots USING INDEX ix_wallet_snapshots_char_recorded" in plan, plan
    # Bounded range on recorded_at, not just an equality on character_id —
    # proves the cutoff folds into the same index seek.
    assert "recorded_at>" in plan
    assert "SCAN wallet_snapshots" not in plan


def test_networth_query_seeks_the_composite_primary_key():
    stmt = _build_latest_networth_stmt([1, 2, 3, 24])
    sql = str(stmt.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}))
    lines = _explain(sql)
    plan = "\n".join(lines)
    assert "SEARCH net_worth_snapshots USING INDEX sqlite_autoindex_net_worth_snapshots_1" in plan, plan
    assert not any("B-TREE" in line.upper() for line in lines), plan
    assert "SCAN net_worth_snapshots" not in plan


# ── perf regression: ~300k rows / 24 characters, bulk-loaded ────────────────

N_CHARACTERS = 24
ROWS_PER_CHARACTER = 12_500  # ~300k total
STEP = timedelta(minutes=2)
START_BALANCE = 1_000_000.0
T0 = NOW.replace(tzinfo=None) - ROWS_PER_CHARACTER * STEP
BUCKET_SECONDS = int(SPARKLINE_BUCKET.total_seconds())


def _utc_epoch(dt: datetime) -> int:
    """Seconds since the Unix epoch, treating the naive `dt` as UTC — the
    same interpretation SQLite's strftime('%s', recorded_at) gives the
    stored naive-UTC datetimes. datetime.timestamp() would apply the local
    timezone instead and silently disagree with SQL by however far local
    time is offset from UTC."""
    return calendar.timegm(dt.timetuple())


def _expected_points(cid: int) -> list[tuple[datetime, float]]:
    """Row i (0-indexed) for character cid has recorded_at = T0 + i*STEP and
    balance = START_BALANCE + cid + (i + 1) — see _bulk_seed. Bucket by
    BUCKET_SECONDS-wide windows of UTC epoch time and keep the row with the
    highest recorded_at (== highest balance, since balance only increases)
    in each bucket, restricted to recorded_at >= CUTOFF."""
    i_start = max(0, -((CUTOFF - T0) // -STEP))  # ceil division via double negation
    buckets: dict[int, tuple[datetime, float]] = {}
    for i in range(i_start, ROWS_PER_CHARACTER):
        t = T0 + i * STEP
        if t < CUTOFF:
            continue
        balance = START_BALANCE + cid + (i + 1)
        bucket = _utc_epoch(t) // BUCKET_SECONDS
        existing = buckets.get(bucket)
        if existing is None or t > existing[0]:
            buckets[bucket] = (t, balance)
    return [buckets[b] for b in sorted(buckets)]


def _bulk_seed(db_path: str) -> None:
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
    path = tmp_path_factory.mktemp("detail_perf") / "wallet.db"
    _bulk_seed(str(path))
    return str(path)


def test_load_wallet_sparkline_points_perf_and_correctness(seeded_db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{seeded_db_path}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    character_ids = list(range(1, N_CHARACTERS + 1))

    async def _run():
        async with SessionLocal() as db:
            await load_wallet_sparkline_points(db, character_ids, now=NOW)  # warm

            t0 = time.perf_counter()
            for _ in range(10):
                result = await load_wallet_sparkline_points(db, character_ids, now=NOW)
            elapsed_ms = (time.perf_counter() - t0) / 10 * 1000
            return result, elapsed_ms

    result, elapsed_ms = asyncio.run(_run())
    asyncio.run(engine.dispose())

    # This threshold is a regression tripwire, not a target: measured ~40ms
    # on this fixture, and the every-row-in-Python query it replaced
    # measured ~149ms here. 1000ms only needs to catch an order-of-magnitude
    # regression reliably on a CI runner slower than a laptop — it isn't
    # meant to hold the query near its actual ~40ms cost.
    assert elapsed_ms < 1000, f"load_wallet_sparkline_points took {elapsed_ms:.2f}ms, expected well under 1000ms"
    assert set(result.keys()) == set(character_ids)
    for cid in character_ids:
        assert result[cid] == _expected_points(cid), cid
