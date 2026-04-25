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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail, PlayerCountSnapshot, get_db

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

    # ISK destroyed — sum per bin. Killmails table only retains discovery-
    # scope kills for 30 days, so longer windows naturally trail off.
    isk_buckets = [0.0] * num_bins
    isk_rows = (await db.execute(
        select(Killmail.killmail_time, Killmail.total_value)
        .where(Killmail.killmail_time >= cutoff)
    )).all()
    for kt, val in isk_rows:
        if kt is None or val is None:
            continue
        idx = int((kt - cutoff).total_seconds() // bin_seconds)
        if 0 <= idx < num_bins:
            isk_buckets[idx] += float(val)
    total_isk = sum(isk_buckets)

    # PCU — average per bin across ALL sources. ESI samples and daily
    # backfill rows live in the same table; their timestamps don't collide,
    # so a flat average across rows in a bin is fine.
    pcu_sums = [0.0] * num_bins
    pcu_counts = [0] * num_bins
    pcu_rows = (await db.execute(
        select(PlayerCountSnapshot.recorded_at, PlayerCountSnapshot.player_count)
        .where(PlayerCountSnapshot.recorded_at >= cutoff)
    )).all()
    for rt, pc in pcu_rows:
        if rt is None or pc is None:
            continue
        idx = int((rt - cutoff).total_seconds() // bin_seconds)
        if 0 <= idx < num_bins:
            pcu_sums[idx] += float(pc)
            pcu_counts[idx] += 1
    pcu_values = [
        round(pcu_sums[i] / pcu_counts[i]) if pcu_counts[i] else None
        for i in range(num_bins)
    ]
    peak_pcu = max((v for v in pcu_values if v is not None), default=0)
    nonempty = [v for v in pcu_values if v is not None]
    mean_pcu = round(sum(nonempty) / len(nonempty)) if nonempty else 0

    # Source coverage breakdown for the attribution footer
    src_counts = {}
    for src, in (await db.execute(
        select(PlayerCountSnapshot.source).where(PlayerCountSnapshot.recorded_at >= cutoff)
    )).all():
        src_counts[src] = src_counts.get(src, 0) + 1

    return templates.TemplateResponse(
        "tools_activity.html",
        {
            "request": request,
            "window": window,
            "window_label": label,
            "labels": labels,
            "isk_values": isk_buckets,
            "pcu_values": pcu_values,
            "total_isk": total_isk,
            "peak_pcu": peak_pcu,
            "mean_pcu": mean_pcu,
            "source_counts": src_counts,
            "window_options": [(k, v[0]) for k, v in _WINDOWS.items()],
        },
    )
