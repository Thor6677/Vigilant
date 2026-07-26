"""Daily PCU aggregator.

Rolls up per-sample rows from player_count_snapshots into one row per
(source, date) in player_count_daily_aggregates. Mirrors the pattern of
killmail_daily_rollup.

Why: the snapshot table has ~10M rows at full archive coverage. Long
activity-chart windows (1y/5y/all) GROUP BY on it scan the whole table
per request. The daily rollup reduces that to ~thousands of rows per
source.

Idempotent: re-runs upsert the same (source, date). Raw snapshots are
NEVER deleted by this job — the rollup is purely a derived cache and
the snapshot table remains the source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import (
    AsyncSessionLocal,
    PlayerCountDailyAggregate,
    PlayerCountSnapshot,
)

log = logging.getLogger(__name__)


async def rollup_pcu(days: int | None = 7) -> dict:
    """Aggregate player_count_snapshots into PlayerCountDailyAggregate.

    days=None  → full backfill (no cutoff). Use for first-time build.
    days=N     → trailing N days only. Use for daily ticks.
    """
    if days is None:
        cutoff = None
    else:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    async with AsyncSessionLocal() as db:
        q = select(
            func.date(PlayerCountSnapshot.recorded_at).label("d"),
            PlayerCountSnapshot.source.label("src"),
            func.avg(PlayerCountSnapshot.player_count).label("avg_pc"),
            func.max(PlayerCountSnapshot.player_count).label("peak_pc"),
            func.count().label("n"),
        ).group_by("d", "src")
        if cutoff is not None:
            q = q.where(PlayerCountSnapshot.recorded_at >= cutoff)

        rows = (await db.execute(q)).all()

        upserted = 0
        for d_str, src, avg_pc, peak_pc, n in rows:
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            stmt = sqlite_insert(PlayerCountDailyAggregate).values(
                date=d,
                source=src,
                avg_pc=float(avg_pc or 0.0),
                peak_pc=int(peak_pc or 0),
                sample_count=int(n or 0),
                rolled_up_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["source", "date"],
                set_={
                    "avg_pc": stmt.excluded.avg_pc,
                    "peak_pc": stmt.excluded.peak_pc,
                    "sample_count": stmt.excluded.sample_count,
                    "rolled_up_at": stmt.excluded.rolled_up_at,
                },
            )
            await db.execute(stmt)
            upserted += 1
        await db.commit()
    log.info("pcu rollup: %d (source,date) rows upserted (cutoff=%s)",
             upserted, cutoff.isoformat() if cutoff else "ALL")
    return {"rows_upserted": upserted, "cutoff": cutoff.isoformat() if cutoff else None}


async def auto_backfill_if_empty() -> dict:
    """One-shot full rebuild if the aggregate table is empty. Cheap to run
    every startup — the count check skips it once populated."""
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(
                select(func.count()).select_from(PlayerCountDailyAggregate)
            )
        ).scalar() or 0
    if existing > 0:
        return {"skipped": True, "existing_rows": existing}
    log.info("pcu daily aggregate empty — running full backfill from snapshots")
    return await rollup_pcu(days=None)
