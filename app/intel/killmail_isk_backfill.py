"""One-time, resumable backfill of daily kill_count + total_isk_destroyed
(T-040, spec docs/superpowers/specs/2026-07-04-activity-history-browser-design.md).

The EVERef import filled killmails back to ~2016 with total_value, but
killmail_daily_aggregates only has vigilant (ISK-bearing) rows since
2026-03-21 — so /tools/activity's 5y/all ISK reads raw-scanned all 60M
rows and OOM-killed the container when pre-warmed (2026-07-04 incident).

Design constraints:
- MONTH CHUNKS ONLY. One month ≈ 600k killmail rows per GROUP BY —
  bounded memory. Never widen the window.
- Insert-only, per-date skip: the daily rollup (killmail_daily_rollup)
  owns recent dates; we never update an existing vigilant row. The
  uq_kda_source_date unique constraint backstops races.
- Resumable: runs on EVERY startup. Fully-covered months fast-skip on a
  cheap date-count probe without touching the killmails table, so a
  finished backfill costs ~120 tiny queries per boot and a half-finished
  one resumes exactly at its gap. (Fast-skip assumes every day in a month
  has >= 1 kill — true for all of 2016-2026 EVE; a hypothetical zero-kill
  day would just make its month re-scan each boot, harmlessly.)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import func, select

from app.db.models import AsyncSessionLocal, Killmail, KillmailDailyAggregate

log = logging.getLogger(__name__)

# EVERef ISK coverage starts ~2016; vigilant rollup coverage starts
# 2026-03-21 (exclusive end — that date already has a rollup row).
BACKFILL_START = date(2016, 1, 1)
BACKFILL_END = date(2026, 3, 21)

_running = False  # single-flight across duplicate startup calls


def _month_range(start: date, end: date):
    """Yield (m0, m1) month windows covering [start, end), m1 exclusive.
    The final window clamps to `end`."""
    cur = date(start.year, start.month, 1)
    while cur < end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        yield cur, min(nxt, end)
        cur = nxt


async def backfill_month(session_factory, m0: date, m1: date) -> int:
    """Aggregate killmails in [m0, m1) into per-day vigilant rows.

    Returns rows inserted, or -1 if the month was already fully covered
    (fast-skip — the killmail aggregate query never ran).
    """
    async with session_factory() as db:
        existing = set((await db.execute(
            select(KillmailDailyAggregate.date).where(
                KillmailDailyAggregate.source == "vigilant",
                KillmailDailyAggregate.date >= m0,
                KillmailDailyAggregate.date < m1,
            )
        )).scalars().all())
        if len(existing) >= (m1 - m0).days:
            return -1

        day_expr = func.date(Killmail.killmail_time)
        rows = (await db.execute(
            select(day_expr, func.count(), func.sum(Killmail.total_value))
            .where(
                Killmail.killmail_time >= datetime(m0.year, m0.month, m0.day),
                Killmail.killmail_time < datetime(m1.year, m1.month, m1.day),
            )
            .group_by(day_expr)
        )).all()

        inserted = 0
        for d_str, kc, isk in rows:
            d = date.fromisoformat(d_str)
            if d in existing:
                continue
            db.add(KillmailDailyAggregate(
                date=d, source="vigilant",
                kill_count=int(kc or 0),
                total_isk_destroyed=float(isk or 0.0),
            ))
            inserted += 1
        if inserted:
            await db.commit()
        return inserted


async def run_backfill() -> None:
    """Startup entry point (fire-and-forget from main.py).

    Waits out the boot rush (pre-warm, consumers), then walks the months.
    A failing month logs and moves on — the next boot's resume check
    finds the gap and retries it.
    """
    global _running
    if _running:
        return
    _running = True
    try:
        await asyncio.sleep(120)
        total = 0
        for m0, m1 in _month_range(BACKFILL_START, BACKFILL_END):
            try:
                n = await backfill_month(AsyncSessionLocal, m0, m1)
            except Exception:
                log.exception("isk-backfill: month %s failed; retrying next boot", m0)
                n = 0
            if n == -1:
                continue  # fast-skip: no sleep needed, nothing was scanned
            total += n
            log.info("isk-backfill: %s +%d day rows", m0.strftime("%Y-%m"), n)
            await asyncio.sleep(2)
        log.info("isk-backfill: pass complete, %d rows inserted", total)
    finally:
        _running = False
