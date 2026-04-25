"""Daily ISK + kill-count aggregator.

Rolls up per-kill rows from the killmails table into one row per day in
killmail_daily_aggregates with source='vigilant'. Designed to run daily
BEFORE gc_discovery_killmails fires, so the pre-GC universe-wide ISK
data is preserved at daily granularity even after the per-kill rows are
deleted.

Idempotent: re-runs simply overwrite the same (source, date) row via
upsert. Default scope is the trailing 35 days — wider than the 30-day GC
window so day boundaries don't slip through.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import (
    AsyncSessionLocal,
    Killmail,
    KillmailDailyAggregate,
)
from app.intel.zkb_totals_scraper import fetch_zkb_totals

log = logging.getLogger(__name__)


async def rollup_recent_days(days: int = 35) -> dict:
    """Aggregate the trailing N days of killmails into KillmailDailyAggregate
    (source='vigilant'). Upserts so re-runs just refresh."""
    cutoff_date = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).date()
    async with AsyncSessionLocal() as db:
        # Group-by-date in SQL, summing total_value
        # SQLite has no DATE() cast on a DateTime that returns Date directly
        # in SQLAlchemy core; use func.date which yields a string. Then
        # parse back in Python.
        rows = (
            await db.execute(
                select(
                    func.date(Killmail.killmail_time).label("d"),
                    func.count().label("n"),
                    func.sum(Killmail.total_value).label("isk"),
                )
                .where(Killmail.killmail_time >= cutoff_date)
                .group_by(func.date(Killmail.killmail_time))
            )
        ).all()

        upserted = 0
        for d_str, n, isk in rows:
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            stmt = sqlite_insert(KillmailDailyAggregate).values(
                date=d,
                source="vigilant",
                kill_count=int(n or 0),
                total_isk_destroyed=float(isk or 0.0) or None,
                rolled_up_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["source", "date"],
                set_={
                    "kill_count": stmt.excluded.kill_count,
                    "total_isk_destroyed": stmt.excluded.total_isk_destroyed,
                    "rolled_up_at": stmt.excluded.rolled_up_at,
                },
            )
            await db.execute(stmt)
            upserted += 1
        await db.commit()
    log.info("vigilant rollup: %d day rows upserted (cutoff %s)", upserted, cutoff_date)
    return {"days_covered": upserted, "cutoff_date": cutoff_date.isoformat()}


async def auto_zkb_totals_if_needed(min_rows_threshold: int = 5000) -> dict:
    """One-shot ingest of zKB totals.json. Skips if we already have enough
    'zkb-totals' rows. Mirrors the player-count auto-backfill pattern."""
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(
                select(func.count())
                .select_from(KillmailDailyAggregate)
                .where(KillmailDailyAggregate.source == "zkb-totals")
            )
        ).scalar() or 0
    if existing >= min_rows_threshold:
        log.info("zkb-totals auto-ingest: skipped (%d rows already)", existing)
        return {"skipped": True, "existing_rows": existing}

    log.info("zkb-totals auto-ingest: fetching (have %d rows)", existing)
    rows = await fetch_zkb_totals()
    if not rows:
        return {"skipped": False, "fetched": 0, "inserted": 0}

    inserted = 0
    async with AsyncSessionLocal() as db:
        # Chunked insert for SQLite param limits
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            stmt = sqlite_insert(KillmailDailyAggregate).values(chunk)
            stmt = stmt.on_conflict_do_nothing(index_elements=["source", "date"])
            await db.execute(stmt)
        await db.commit()
        # Count diff
        new_total = (
            await db.execute(
                select(func.count())
                .select_from(KillmailDailyAggregate)
                .where(KillmailDailyAggregate.source == "zkb-totals")
            )
        ).scalar() or 0
        inserted = new_total - existing
    log.info("zkb-totals auto-ingest: fetched=%d new_inserts=%d", len(rows), inserted)
    return {"skipped": False, "fetched": len(rows), "inserted": inserted}
