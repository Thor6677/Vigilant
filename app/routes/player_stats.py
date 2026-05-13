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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Killmail,
    KillmailDailyAggregate,
    KillmailZoneDailyAggregate,
    PlayerCountDailyAggregate,
    PlayerCountSnapshot,
    get_db,
)
from app.db.sde_models import SDESystem

_ZONES = ("highsec", "lowsec", "nullsec", "wormhole")

# Source priority when multiple sources have the same date — prefer live
# ESI samples, fall back to historical archives. Used by the daily-aggregate
# read path so each date contributes exactly one avg_pc to the bin.
_PCU_SOURCE_PRIORITY = {"esi": 0, "eve-offline-net": 1, "eve-offline-com": 2}

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# Window → (cutoff_delta, bin_seconds, label_fmt). For all-time we use a
# fixed cutoff at 2003-05-28 (first PCU data point per Chribba's archive).
_FIRST_PCU = datetime(2003, 5, 28)
# Bin sizes target ~150-300 points per window so each chart has similar
# visual density. Sub-day windows (≤90d) hit raw player_count_snapshots
# via ix_recorded_at — cheap because the time filter narrows the scan.
# Longer windows (1y/5y/all) keep day+ bins so the PCU read stays on
# PlayerCountDailyAggregate; raw scans over years would be expensive.
_WINDOWS = {
    "1h":   ("Last hour",         timedelta(hours=1),       60,              "%H:%M"),
    "1d":   ("Last 24 hours",     timedelta(days=1),        5 * 60,          "%H:%M"),
    "36h":  ("Last 36 hours",     timedelta(hours=36),      10 * 60,         "%a %H:%M"),
    "7d":   ("Last 7 days",       timedelta(days=7),        30 * 60,         "%b %d %H:%M"),
    "30d":  ("Last 30 days",      timedelta(days=30),       3 * 3600,        "%b %d %H:00"),
    "90d":  ("Last 90 days",      timedelta(days=90),       12 * 3600,       "%b %d"),
    "1y":   ("Last year",         timedelta(days=365),      24 * 3600,       "%Y-%m-%d"),
    "5y":   ("Last 5 years",      timedelta(days=365 * 5),  7 * 24 * 3600,   "%Y-%m-%d"),
    "all":  ("All time (2003–)",  None,                     30 * 24 * 3600,  "%Y-%m"),
}

# Slow windows benefit from a short TTL cache — they don't change minute to
# minute and most of the data is historical. Keyed by window. Values are
# (expires_at, payload_dict).
_SLOW_WINDOWS = {"1y", "5y", "all"}
_SLOW_TTL_SECONDS = 3600
_payload_cache: dict[str, tuple[datetime, dict]] = {}

# Heatmap is a 90-day aggregate, window-independent, and runs ~1.5s via a
# row_number() over 200k+ rows. Cache the result for all windows to share.
_HEATMAP_TTL_SECONDS = 1800
_heatmap_cache: tuple[datetime, list[list[int | None]], bool] | None = None


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

    # Standalone daily-kills series — one point per actual day. Used by a
    # dedicated chart so the value doesn't collapse into a single spike when
    # the main chart's bins are sub-day (1d/7d). Always trailing 30 days
    # (the killmail aggregate's effective horizon for kill_count); for short
    # windows this gives recent context, for long windows it complements the
    # binned main chart with day-resolution detail.
    dk_cutoff = (now - timedelta(days=30)).date()
    dk_rows = (await db.execute(
        select(KillmailDailyAggregate.date, KillmailDailyAggregate.kill_count,
               KillmailDailyAggregate.source)
        .where(KillmailDailyAggregate.date >= dk_cutoff)
    )).all()
    dk_by_date: dict = {}
    for d, kc, src in dk_rows:
        cur = dk_by_date.get(d)
        if cur is None or (cur[1] == "zkb-totals" and src == "vigilant"):
            dk_by_date[d] = (int(kc), src)
    daily_kills_dates = sorted(dk_by_date.keys())
    daily_kills_labels = [d.strftime("%b %d") for d in daily_kills_dates]
    daily_kills_counts = [dk_by_date[d][0] for d in daily_kills_dates]

    # ── Breakdown charts (only on windows ≤ 30d) ──
    # Killmail rows are GC'd at 30d, so attacker-count + is_npc breakdowns
    # only make sense on the trailing 30 days. Hide them for longer windows
    # rather than showing partial data.
    breakdowns_available = window in ("1h", "1d", "36h", "7d", "30d")
    has_breakdown_data = False
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
        has_breakdown_data = (
            any(sum(v) > 0 for v in solo_fleet_series.values())
            or any(sum(v) > 0 for v in npc_player_series.values())
        )

    # ── Security-zone split (kills + ISK by HS/LS/NS/WH) ──
    # Two paths: sub-day bins (1d/7d) GROUP BY directly on Killmail joined
    # to sde_systems — small windows, scan is cheap. Day+ bins read the
    # pre-aggregated KillmailZoneDailyAggregate so we don't scan all of
    # killmails on the longer windows. The aggregate is populated by
    # killmail_daily_rollup; pre-rollup bins simply show as 0.
    zone_available = True
    zone_series: dict[str, list[int]] = {z: [0] * num_bins for z in _ZONES}
    zone_isk_series: dict[str, list[float]] = {z: [0.0] * num_bins for z in _ZONES}
    if bin_seconds < 86400:
        zone_expr = case(
            (Killmail.solar_system_id >= 31000000, "wormhole"),
            (SDESystem.security.is_(None), "unknown"),
            (func.round(SDESystem.security, 1) >= 0.5, "highsec"),
            (func.round(SDESystem.security, 1) > 0.0, "lowsec"),
            else_="nullsec",
        ).label("zone")
        zrows = (await db.execute(
            select(
                _bin_expr(Killmail.killmail_time).label("b"),
                zone_expr,
                func.count().label("n"),
                func.sum(Killmail.total_value).label("isk"),
            )
            .select_from(Killmail)
            .join(SDESystem, SDESystem.system_id == Killmail.solar_system_id, isouter=True)
            .where(Killmail.killmail_time >= cutoff)
            .group_by("b", "zone")
        )).all()
        for b, z, n, isk in zrows:
            if b is None or z not in zone_series:
                continue
            i = int(b)
            if 0 <= i < num_bins:
                zone_series[z][i] += int(n or 0)
                zone_isk_series[z][i] += float(isk or 0.0)
    else:
        zrows = (await db.execute(
            select(
                KillmailZoneDailyAggregate.date,
                KillmailZoneDailyAggregate.zone,
                KillmailZoneDailyAggregate.kill_count,
                KillmailZoneDailyAggregate.total_isk_destroyed,
            ).where(KillmailZoneDailyAggregate.date >= cutoff.date())
        )).all()
        for d, z, kc, isk in zrows:
            if z not in zone_series:
                continue
            d_dt = datetime(d.year, d.month, d.day)
            idx = int((d_dt - cutoff).total_seconds() // bin_seconds)
            if 0 <= idx < num_bins:
                zone_series[z][idx] += int(kc or 0)
                zone_isk_series[z][idx] += float(isk or 0.0)
    has_zone_data = any(sum(s) > 0 for s in zone_series.values())

    # ── Hour-of-day × day-of-week PCU heatmap (always trailing 90d) ──
    # Independent of the selected chart window — it answers a different
    # question ("when is EVE busiest?") and only makes sense at hourly
    # resolution. Source preference: ESI when present (our own live
    # samples), fall back to Chribba's eve-offline-net, then Adminor's
    # eve-offline-com. Implemented via row_number() window function —
    # picks one row per recorded_at minute by source priority. Coarse
    # granularities (daily/weekly archive rollups) excluded so they
    # don't blur the hourly buckets. SQLite strftime('%w') is 0=Sunday.
    global _heatmap_cache
    pcu_heatmap: list[list[int | None]]
    if _heatmap_cache is not None and now < _heatmap_cache[0]:
        pcu_heatmap = _heatmap_cache[1]
        has_heatmap_data = _heatmap_cache[2]
    else:
        heatmap_cutoff = now - timedelta(days=90)
        src_rank = case(
            (PlayerCountSnapshot.source == "esi", 0),
            (PlayerCountSnapshot.source == "eve-offline-net", 1),
            (PlayerCountSnapshot.source == "eve-offline-com", 2),
            else_=3,
        )
        ranked = (
            select(
                PlayerCountSnapshot.recorded_at,
                PlayerCountSnapshot.player_count,
                func.row_number().over(
                    partition_by=PlayerCountSnapshot.recorded_at,
                    order_by=src_rank,
                ).label("rn"),
            )
            .where(
                PlayerCountSnapshot.recorded_at >= heatmap_cutoff,
                PlayerCountSnapshot.granularity.in_(("60s", "minute", "hourly")),
            )
            .subquery()
        )
        heatmap_rows = (await db.execute(
            select(
                func.strftime("%w", ranked.c.recorded_at).label("dow"),
                func.strftime("%H", ranked.c.recorded_at).label("hr"),
                func.avg(ranked.c.player_count).label("avg_pc"),
            )
            .where(ranked.c.rn == 1)
            .group_by("dow", "hr")
        )).all()
        # Build 7×24 grid; rows ordered Mon…Sun (rotate from SQLite's Sun=0).
        pcu_heatmap = [[None] * 24 for _ in range(7)]
        for dow_str, hr_str, avg_pc in heatmap_rows:
            if dow_str is None or hr_str is None or avg_pc is None:
                continue
            # SQLite: Sun=0 Mon=1 … Sat=6. Rotate so Mon=0 … Sun=6.
            dow = (int(dow_str) + 6) % 7
            hr = int(hr_str)
            if 0 <= dow < 7 and 0 <= hr < 24:
                pcu_heatmap[dow][hr] = round(float(avg_pc))
        has_heatmap_data = any(v is not None for row in pcu_heatmap for v in row)
        _heatmap_cache = (
            now + timedelta(seconds=_HEATMAP_TTL_SECONDS),
            pcu_heatmap,
            has_heatmap_data,
        )

    # Source coverage breakdown. For day+ bins we sum sample_count from the
    # daily aggregate (cheap). For sub-day windows we GROUP BY on the
    # snapshot table — use `GROUP BY +source` so SQLite picks the
    # ix_recorded_at index to filter to ~1440 rows BEFORE grouping. The
    # plain `GROUP BY source` form makes the planner choose the
    # (source, recorded_at) unique covering index and full-scan all 10M
    # rows (~13s). The unary `+` defeats that index match. A subquery
    # rewrite does NOT help — SQLite flattens it back to the same plan.
    src_counts = {}
    if bin_seconds < 86400:
        src_q = (
            select(PlayerCountSnapshot.source, func.count())
            .where(PlayerCountSnapshot.recorded_at >= cutoff)
            .group_by(literal_column("+source"))
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
        "has_breakdown_data": has_breakdown_data,
        "solo_fleet_series": solo_fleet_series,
        "npc_player_series": npc_player_series,
        "pcu_heatmap": pcu_heatmap,
        "has_heatmap_data": has_heatmap_data,
        "zone_available": zone_available,
        "has_zone_data": has_zone_data,
        "zone_series": zone_series,
        "zone_isk_series": zone_isk_series,
        "daily_kills_labels": daily_kills_labels,
        "daily_kills_counts": daily_kills_counts,
    }
    if window in _SLOW_WINDOWS:
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=_SLOW_TTL_SECONDS)
        _payload_cache[window] = (expires_at, payload)
    return templates.TemplateResponse(
        "tools_activity.html", {"request": request, **payload}
    )


# Lazy-loaded prior-period overlay. Lives on its own endpoint so the main
# /tools/activity render isn't slowed by a second pass over the data, and
# so the slow-window response cache stays small. JS only fetches this when
# the user clicks the "Compare to prior period" toggle.
_compare_cache: dict[str, tuple[datetime, dict]] = {}


@router.get("/tools/activity/compare.json")
async def tools_activity_compare(
    request: Request,
    window: str = "30d",
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "auth"}, status_code=401)
    if window not in _WINDOWS or window == "all":
        return JSONResponse({"error": "no prior period for this window"}, status_code=400)

    cached = _compare_cache.get(window) if window in _SLOW_WINDOWS else None
    if cached is not None:
        expires_at, body = cached
        if datetime.now(timezone.utc).replace(tzinfo=None) < expires_at:
            return JSONResponse(body)

    _label, delta, bin_seconds, label_fmt = _WINDOWS[window]
    # delta is non-None for every non-'all' window.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    current_cutoff = now_naive - delta
    prior_cutoff = current_cutoff - delta
    total_seconds = int((current_cutoff - prior_cutoff).total_seconds())
    num_bins = max(1, total_seconds // bin_seconds)
    bin_starts = [prior_cutoff + timedelta(seconds=i * bin_seconds) for i in range(num_bins)]
    labels = [bs.strftime(label_fmt) for bs in bin_starts]

    def _bin_expr(time_col):
        return func.cast(
            (func.julianday(time_col) - func.julianday(prior_cutoff))
            * 86400.0 / float(bin_seconds),
            Integer,
        )

    # PCU — daily aggregate path for day+ bins, raw for sub-day.
    pcu_values: list[int | None] = [None] * num_bins
    if bin_seconds < 86400:
        for b, avg_pc in (await db.execute(
            select(_bin_expr(PlayerCountSnapshot.recorded_at).label("b"),
                   func.avg(PlayerCountSnapshot.player_count))
            .where(PlayerCountSnapshot.recorded_at >= prior_cutoff,
                   PlayerCountSnapshot.recorded_at < current_cutoff)
            .group_by("b")
        )).all():
            if b is None or avg_pc is None:
                continue
            i = int(b)
            if 0 <= i < num_bins:
                pcu_values[i] = round(float(avg_pc))
    else:
        per_date: dict = {}
        for d, src, avg_pc in (await db.execute(
            select(PlayerCountDailyAggregate.date, PlayerCountDailyAggregate.source,
                   PlayerCountDailyAggregate.avg_pc)
            .where(PlayerCountDailyAggregate.date >= prior_cutoff.date(),
                   PlayerCountDailyAggregate.date < current_cutoff.date())
        )).all():
            cur = per_date.get(d)
            new_pri = _PCU_SOURCE_PRIORITY.get(src, 99)
            if cur is None or new_pri < cur[0]:
                per_date[d] = (new_pri, float(avg_pc or 0.0))
        bin_sum = [0.0] * num_bins
        bin_n = [0] * num_bins
        for d, (_pri, avg_pc) in per_date.items():
            d_dt = datetime(d.year, d.month, d.day)
            idx = int((d_dt - prior_cutoff).total_seconds() // bin_seconds)
            if 0 <= idx < num_bins:
                bin_sum[idx] += avg_pc
                bin_n[idx] += 1
        for i in range(num_bins):
            if bin_n[i] > 0:
                pcu_values[i] = round(bin_sum[i] / bin_n[i])

    # ISK — only available for the trailing 30d of Killmail rows. Older bins
    # return 0; that's honest given retention.
    isk_values: list[float] = [0.0] * num_bins
    for b, isk in (await db.execute(
        select(_bin_expr(Killmail.killmail_time).label("b"),
               func.sum(Killmail.total_value))
        .where(Killmail.killmail_time >= prior_cutoff,
               Killmail.killmail_time < current_cutoff)
        .group_by("b")
    )).all():
        if b is None:
            continue
        i = int(b)
        if 0 <= i < num_bins:
            isk_values[i] = float(isk or 0.0)

    # Kills — from KillmailDailyAggregate (multi-source).
    kills_values = [0] * num_bins
    by_date: dict = {}
    for d, kc, src in (await db.execute(
        select(KillmailDailyAggregate.date, KillmailDailyAggregate.kill_count,
               KillmailDailyAggregate.source)
        .where(KillmailDailyAggregate.date >= prior_cutoff.date(),
               KillmailDailyAggregate.date < current_cutoff.date())
    )).all():
        cur = by_date.get(d)
        if cur is None or (cur[1] == "zkb-totals" and src == "vigilant"):
            by_date[d] = (int(kc), src)
    for d, (kc, _src) in by_date.items():
        d_dt = datetime(d.year, d.month, d.day)
        idx = int((d_dt - prior_cutoff).total_seconds() // bin_seconds)
        if 0 <= idx < num_bins:
            kills_values[idx] += kc

    body = {
        "window": window,
        "labels": labels,
        "pcu_values": pcu_values,
        "isk_values": isk_values,
        "kills_values": kills_values,
    }
    if window in _SLOW_WINDOWS:
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=_SLOW_TTL_SECONDS)
        _compare_cache[window] = (expires_at, body)
    return JSONResponse(body)
