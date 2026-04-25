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
from sqlalchemy import Integer, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Killmail,
    KillmailDailyAggregate,
    PlayerCountDailyAggregate,
    PlayerCountSnapshot,
    get_db,
)

# Source priority when multiple sources have the same date — prefer live
# ESI samples, fall back to historical archives. Used by the daily-aggregate
# read path so each date contributes exactly one avg_pc to the bin.
_PCU_SOURCE_PRIORITY = {"esi": 0, "eve-offline-net": 1, "eve-offline-com": 2}

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

# Slow windows benefit from a short TTL cache — they don't change minute to
# minute and most of the data is historical. Keyed by window. Values are
# (expires_at, payload_dict).
_SLOW_WINDOWS = {"1y", "5y", "all"}
_SLOW_TTL_SECONDS = 3600
_payload_cache: dict[str, tuple[datetime, dict]] = {}


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

    # Slow-window cache: payload is mostly historical, fine to serve a
    # 1-hour-old version. Skip for short windows where data turns over fast.
    cached = _payload_cache.get(window) if window in _SLOW_WINDOWS else None
    if cached is not None:
        expires_at, payload = cached
        if datetime.now(timezone.utc).replace(tzinfo=None) < expires_at:
            return templates.TemplateResponse(
                "tools_activity.html", {"request": request, **payload}
            )

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
    # For sub-day bins (1d/7d) we GROUP BY on the raw snapshot table — small
    # window so the scan is cheap. For day+ bins (30d/90d/1y/5y/all) we read
    # the pre-aggregated PlayerCountDailyAggregate table to avoid a 10M-row
    # scan per request. Raw snapshots are preserved either way.
    pcu_values: list[int | None] = [None] * num_bins
    pcu_peaks: list[int | None] = [None] * num_bins
    if bin_seconds < 86400:
        pcu_q = (
            select(
                _bin_expr(PlayerCountSnapshot.recorded_at).label("b"),
                func.avg(PlayerCountSnapshot.player_count).label("avg_pc"),
                func.max(PlayerCountSnapshot.player_count).label("peak_pc"),
            )
            .where(PlayerCountSnapshot.recorded_at >= cutoff)
            .group_by("b")
        )
        for b, avg_pc, peak_pc in (await db.execute(pcu_q)).all():
            if b is None or avg_pc is None:
                continue
            i = int(b)
            if 0 <= i < num_bins:
                pcu_values[i] = round(float(avg_pc))
                pcu_peaks[i] = int(peak_pc) if peak_pc is not None else None
    else:
        # Read pre-aggregated daily rows. Multiple sources may share a date;
        # pick the highest-priority source per date so we don't double-count.
        # The daily rollup runs once per 24h, so today's row would be stale
        # for hours — supplement with a small live query for today's raw
        # snapshots so the rightmost bin updates as new samples land.
        today = now.date()
        kda_q = select(
            PlayerCountDailyAggregate.date,
            PlayerCountDailyAggregate.source,
            PlayerCountDailyAggregate.avg_pc,
            PlayerCountDailyAggregate.peak_pc,
        ).where(
            PlayerCountDailyAggregate.date >= cutoff.date(),
            PlayerCountDailyAggregate.date < today,
        )
        per_date: dict = {}
        for d, src, avg_pc, peak_pc in (await db.execute(kda_q)).all():
            cur = per_date.get(d)
            new_pri = _PCU_SOURCE_PRIORITY.get(src, 99)
            if cur is None or new_pri < cur[0]:
                per_date[d] = (new_pri, float(avg_pc or 0.0), int(peak_pc or 0))
        # Live today bin from raw snapshots (single day, ~1440 rows for ESI).
        today_start = datetime(today.year, today.month, today.day)
        live_q = select(
            func.avg(PlayerCountSnapshot.player_count),
            func.max(PlayerCountSnapshot.player_count),
        ).where(PlayerCountSnapshot.recorded_at >= today_start)
        row = (await db.execute(live_q)).first()
        if row and row[0] is not None:
            per_date[today] = (0, float(row[0]), int(row[1] or 0))
        # Bin: average avg_pc across the days in each bin (each day weight=1);
        # peak = max across days.
        bin_sum = [0.0] * num_bins
        bin_n = [0] * num_bins
        for d, (_pri, avg_pc, peak_pc) in per_date.items():
            d_dt = datetime(d.year, d.month, d.day)
            idx = int((d_dt - cutoff).total_seconds() // bin_seconds)
            if 0 <= idx < num_bins:
                bin_sum[idx] += avg_pc
                bin_n[idx] += 1
                if pcu_peaks[idx] is None or peak_pc > pcu_peaks[idx]:
                    pcu_peaks[idx] = peak_pc
        for i in range(num_bins):
            if bin_n[i] > 0:
                pcu_values[i] = round(bin_sum[i] / bin_n[i])
    peak_pcu = max((v for v in pcu_peaks if v is not None), default=0)
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

    # ── Breakdown charts (only on windows ≤ 30d) ──
    # Killmail rows are GC'd at 30d, so attacker-count + is_npc breakdowns
    # only make sense on the trailing 30 days. Hide them for longer windows
    # rather than showing partial data.
    breakdowns_available = window in ("1d", "7d", "30d")
    solo_fleet_series: dict[str, list[int]] = {}
    npc_player_series: dict[str, list[int]] = {}
    if breakdowns_available:
        # Solo / small / medium / large bucketing on attacker_count.
        bucket_expr = case(
            (Killmail.attacker_count <= 1, "solo"),
            (Killmail.attacker_count <= 10, "small"),
            (Killmail.attacker_count <= 50, "medium"),
            else_="large",
        ).label("bucket")
        sf_q = (
            select(
                _bin_expr(Killmail.killmail_time).label("b"),
                bucket_expr,
                func.count().label("n"),
            )
            .where(Killmail.killmail_time >= cutoff)
            .group_by("b", "bucket")
        )
        for name in ("solo", "small", "medium", "large"):
            solo_fleet_series[name] = [0] * num_bins
        for b, bucket, n in (await db.execute(sf_q)).all():
            if b is None or bucket not in solo_fleet_series:
                continue
            i = int(b)
            if 0 <= i < num_bins:
                solo_fleet_series[bucket][i] = int(n or 0)

        # NPC vs player kills — uses is_npc flag.
        np_q = (
            select(
                _bin_expr(Killmail.killmail_time).label("b"),
                Killmail.is_npc.label("npc"),
                func.count().label("n"),
            )
            .where(Killmail.killmail_time >= cutoff)
            .group_by("b", "npc")
        )
        npc_player_series["npc"] = [0] * num_bins
        npc_player_series["player"] = [0] * num_bins
        for b, is_npc, n in (await db.execute(np_q)).all():
            if b is None:
                continue
            i = int(b)
            if 0 <= i < num_bins:
                key = "npc" if is_npc else "player"
                npc_player_series[key][i] = int(n or 0)

    # ── Hour-of-day × day-of-week PCU heatmap (always trailing 90d) ──
    # Independent of the selected chart window — it answers a different
    # question ("when is EVE busiest?") and only makes sense at hourly
    # resolution. Filtered to source='esi' so we don't double-count where
    # historical archives overlap with live sampling. Cell value is the
    # avg PCU at that (weekday, hour). SQLite strftime('%w') is 0=Sunday.
    heatmap_cutoff = now - timedelta(days=90)
    heatmap_rows = (await db.execute(
        select(
            func.strftime("%w", PlayerCountSnapshot.recorded_at).label("dow"),
            func.strftime("%H", PlayerCountSnapshot.recorded_at).label("hr"),
            func.avg(PlayerCountSnapshot.player_count).label("avg_pc"),
        )
        .where(
            PlayerCountSnapshot.recorded_at >= heatmap_cutoff,
            PlayerCountSnapshot.source == "esi",
        )
        .group_by("dow", "hr")
    )).all()
    # Build 7×24 grid; rows ordered Mon…Sun (rotate from SQLite's Sun=0).
    pcu_heatmap: list[list[int | None]] = [[None] * 24 for _ in range(7)]
    for dow_str, hr_str, avg_pc in heatmap_rows:
        if dow_str is None or hr_str is None or avg_pc is None:
            continue
        # SQLite: Sun=0 Mon=1 … Sat=6. Rotate so Mon=0 … Sun=6.
        dow = (int(dow_str) + 6) % 7
        hr = int(hr_str)
        if 0 <= dow < 7 and 0 <= hr < 24:
            pcu_heatmap[dow][hr] = round(float(avg_pc))
    has_heatmap_data = any(v is not None for row in pcu_heatmap for v in row)

    # Source coverage breakdown. For day+ bins we sum sample_count from the
    # daily aggregate (cheap). For sub-day windows we GROUP BY on the
    # snapshot table — small window, still cheap.
    src_counts = {}
    if bin_seconds < 86400:
        src_q = (
            select(PlayerCountSnapshot.source, func.count())
            .where(PlayerCountSnapshot.recorded_at >= cutoff)
            .group_by(PlayerCountSnapshot.source)
        )
    else:
        src_q = (
            select(PlayerCountDailyAggregate.source,
                   func.sum(PlayerCountDailyAggregate.sample_count))
            .where(PlayerCountDailyAggregate.date >= cutoff.date())
            .group_by(PlayerCountDailyAggregate.source)
        )
    for src, n in (await db.execute(src_q)).all():
        src_counts[src] = src_counts.get(src, 0) + int(n or 0)
    for src, n in (await db.execute(
        select(KillmailDailyAggregate.source, func.count())
        .where(KillmailDailyAggregate.date >= cutoff.date())
        .group_by(KillmailDailyAggregate.source)
    )).all():
        src_counts[src] = src_counts.get(src, 0) + int(n)

    payload = {
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
        "breakdowns_available": breakdowns_available,
        "solo_fleet_series": solo_fleet_series,
        "npc_player_series": npc_player_series,
        "pcu_heatmap": pcu_heatmap,
        "has_heatmap_data": has_heatmap_data,
    }
    if window in _SLOW_WINDOWS:
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=_SLOW_TTL_SECONDS)
        _payload_cache[window] = (expires_at, payload)
    return templates.TemplateResponse(
        "tools_activity.html", {"request": request, **payload}
    )
