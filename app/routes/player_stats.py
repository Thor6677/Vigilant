"""Tools → Activity. Full-page PCU + ISK destroyed view with extended
windows (1d / 7d / 30d / 90d / 1y / 5y / all).

The dashboard panel at /dashboard/activity covers short windows (1H/24H/W/M)
backed only by live ESI samples + the killmail stream. This page extends to
years using the third-party historical archives loaded into
player_count_snapshots via the eve-offline.net + eve-offline.com scrapers.
ISK destroyed is bounded by the killmails table's 30-day discovery
retention, so longer windows show ISK as zero on bins predating that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail, KillmailDailyAggregate, PlayerCountSnapshot, get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# Window → (cutoff_delta, bin_seconds, label_fmt). For all-time we use a
# fixed cutoff at 2003-05-28 (first PCU data point per Chribba's archive).
_FIRST_PCU = datetime(2003, 5, 28)
_WINDOWS = {
    "1d":   ("Last 24 hours",     timedelta(days=1),    1 * 3600,        "%H:%M"),
    "7d":   ("Last 7 days",       timedelta(days=7),    6 * 3600,        "%b %d %H:00"),
    "30d":  ("Last 30 days",      timedelta(days=30),   24 * 3600,       "%b %d"),
    "90d":  ("Last 90 days",      timedelta(days=90),   24 * 3600,       "%b %d"),
    "1y":   ("Last year",         timedelta(days=365),  7 * 24 * 3600,   "%Y-%m-%d"),
    "5y":   ("Last 5 years",      timedelta(days=365 * 5),  30 * 24 * 3600,  "%Y-%m"),
    "all":  ("All time (2003–)",  None,                 90 * 24 * 3600,  "%Y-%m"),
}


@router.get("/tools/activity", response_class=HTMLResponse)
async def tools_activity(
    request: Request,
    window: str = "30d",
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return RedirectResponse("/")

    if window not in _WINDOWS:
        window = "30d"
    label, delta, bin_seconds, label_fmt = _WINDOWS[window]

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = _FIRST_PCU if delta is None else (now - delta)
    total_seconds = int((now - cutoff).total_seconds())
    num_bins = max(1, total_seconds // bin_seconds)
    bin_starts = [
        cutoff + timedelta(seconds=i * bin_seconds) for i in range(num_bins)
    ]
    labels = [bs.strftime(label_fmt) for bs in bin_starts]

    # CRITICAL: aggregate IN SQL, not Python. PlayerCountSnapshot has ~10M
    # rows at full archive coverage; loading them all into Python and binning
    # client-side OOMs the 2.5GB container instantly. SQLite GROUP BY on a
    # cast-to-int bin index returns ~num_bins rows per query.
    def _bin_expr(time_col):
        # julianday is in days; *86400 → seconds; / bin_seconds → bin index.
        return func.cast(
            (func.julianday(time_col) - func.julianday(cutoff))
            * 86400.0 / float(bin_seconds),
            Integer,
        )

    # ── PCU: average per bin (concurrent count, not throughput) ──
    pcu_q = (
        select(
            _bin_expr(PlayerCountSnapshot.recorded_at).label("b"),
            func.avg(PlayerCountSnapshot.player_count).label("avg_pc"),
        )
        .where(PlayerCountSnapshot.recorded_at >= cutoff)
        .group_by("b")
    )
    pcu_values: list[int | None] = [None] * num_bins
    for b, avg_pc in (await db.execute(pcu_q)).all():
        if b is None or avg_pc is None:
            continue
        i = int(b)
        if 0 <= i < num_bins:
            pcu_values[i] = round(float(avg_pc))
    peak_pcu = max((v for v in pcu_values if v is not None), default=0)
    nonempty = [v for v in pcu_values if v is not None]
    mean_pcu = round(sum(nonempty) / len(nonempty)) if nonempty else 0

    # ── ISK destroyed: sum per bin ──
    isk_q = (
        select(
            _bin_expr(Killmail.killmail_time).label("b"),
            func.sum(Killmail.total_value).label("isk"),
        )
        .where(Killmail.killmail_time >= cutoff)
        .group_by("b")
    )
    isk_buckets: list[float] = [0.0] * num_bins
    for b, isk in (await db.execute(isk_q)).all():
        if b is None:
            continue
        i = int(b)
        if 0 <= i < num_bins:
            isk_buckets[i] = float(isk or 0.0)
    total_isk = sum(isk_buckets)

    # Daily kill counts — pulled from killmail_daily_aggregates.
    # Prefer source='vigilant' on overlapping dates (more recent + has ISK);
    # fall back to source='zkb-totals' (deep history, kill_count only).
    kills_buckets = [0] * num_bins
    kills_counts = [0] * num_bins
    kda_rows = (await db.execute(
        select(KillmailDailyAggregate.date, KillmailDailyAggregate.kill_count, KillmailDailyAggregate.source)
        .where(KillmailDailyAggregate.date >= cutoff.date())
    )).all()
    # Build per-date best-source view (vigilant beats zkb-totals)
    by_date: dict = {}
    for d, kc, src in kda_rows:
        cur = by_date.get(d)
        if cur is None or (cur[1] == "zkb-totals" and src == "vigilant"):
            by_date[d] = (int(kc), src)
    for d, (kc, _src) in by_date.items():
        d_dt = datetime(d.year, d.month, d.day)
        idx = int((d_dt - cutoff).total_seconds() // bin_seconds)
        if 0 <= idx < num_bins:
            kills_buckets[idx] += kc
            kills_counts[idx] += 1
    # Average per bin (sum kills/day across days inside the bin) — but here
    # we want the SUM not the average, since each day-row is independent.
    # Already summed above. If bin spans multiple days, kills_buckets[i] is
    # the cluster total — appropriate for "kills in this bin".
    total_kills = sum(kills_buckets)

    # Source coverage breakdown — aggregate in SQL, again. Loading every
    # row's source column for a 10M-row table would OOM.
    src_counts = {}
    for src, n in (await db.execute(
        select(PlayerCountSnapshot.source, func.count())
        .where(PlayerCountSnapshot.recorded_at >= cutoff)
        .group_by(PlayerCountSnapshot.source)
    )).all():
        src_counts[src] = src_counts.get(src, 0) + int(n)
    for src, n in (await db.execute(
        select(KillmailDailyAggregate.source, func.count())
        .where(KillmailDailyAggregate.date >= cutoff.date())
        .group_by(KillmailDailyAggregate.source)
    )).all():
        src_counts[src] = src_counts.get(src, 0) + int(n)

    return templates.TemplateResponse(
        "tools_activity.html",
        {
            "request": request,
            "window": window,
            "window_label": label,
            "labels": labels,
            "isk_values": isk_buckets,
            "pcu_values": pcu_values,
            "kills_values": kills_buckets,
            "total_isk": total_isk,
            "peak_pcu": peak_pcu,
            "mean_pcu": mean_pcu,
            "total_kills": total_kills,
            "source_counts": src_counts,
            "window_options": [(k, v[0]) for k, v in _WINDOWS.items()],
        },
    )
