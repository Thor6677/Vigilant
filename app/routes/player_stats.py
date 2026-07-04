"""Tools → Activity. Full-page PCU + ISK destroyed view with extended
windows (1d / 7d / 30d / 90d / 1y / 5y / all).

The dashboard panel at /dashboard/activity covers short windows (1H/24H/W/M)
backed only by live ESI samples + the killmail stream. This page extends to
years using the third-party historical archives loaded into
player_count_snapshots via the eve-offline.net + eve-offline.com scrapers.
ISK destroyed on day+ windows reads killmail_daily_aggregates (vigilant
rows; T-040 backfill extends coverage to ~2016 — earlier bins show zero).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AsyncSessionLocal,
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
log = logging.getLogger(__name__)


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

# Stale-while-revalidate payload cache, ALL windows (same pattern as
# corp-stats / kill-pulse — cache the context DICT, never rendered HTML,
# so CSP nonces stay per-request). The 30d default costs ~3s of killmail
# scanning even after query consolidation; serving the last payload and
# refreshing in the background makes every click after the first instant.
# Keyed by window → (fresh_until, payload_dict). A stale entry is served
# immediately while one background task (single-flight via _refreshing)
# rebuilds it with its own AsyncSessionLocal session — never the request's
# session, which closes when the response returns. Single-flight is only
# correct because there is no `await` between the _refreshing membership
# check and the add() in the handler — don't insert one.
_WINDOW_TTL_SECONDS = {
    "1h": 300, "1d": 300, "36h": 300, "7d": 600, "30d": 900, "90d": 900,
    "1y": 3600, "5y": 3600, "all": 3600, "history": 3600,
}
_payload_cache: dict[str, tuple[datetime, dict]] = {}
_refreshing: set[str] = set()


async def _refresh_payload(window: str) -> None:
    """Background SWR refresh. Own DB session (async-session safety)."""
    try:
        async with AsyncSessionLocal() as db:
            if window == "history":
                payload = await _build_history_payload(db)
            else:
                payload = await _build_activity_payload(db, window)
        _payload_cache[window] = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=_WINDOW_TTL_SECONDS[window]),
            payload,
        )
    except Exception:
        log.exception("tools/activity: background refresh failed for %s", window)
    finally:
        _refreshing.discard(window)


async def warm_activity_cache() -> None:
    """Startup pre-warm so no user ever pays a cold window compute.

    The long windows were brutal cold before the T-040 aggregate switch
    (measured on prod 2026-07-04: 90d ≈ 11s, 1y ≈ 54s raw ISK scan; the
    5y/all warm even OOM-killed the container). SWR only helps once a
    window has been computed once — this fills every window sequentially
    in the background, most-used first, one at a time so the container's
    2-CPU cap isn't saturated at boot. Called from main.py startup via
    asyncio.create_task.
    """
    await asyncio.sleep(30)  # let boot-time work (SDE check, consumers) settle
    # All 9 windows warm safely now: the 5y/all ISK read hits the daily
    # aggregate (bounded rows), never the raw killmails table. The
    # 2026-07-04 OOM that forced their exclusion is documented in
    # docs/superpowers/specs/2026-07-04-activity-history-browser-design.md.
    for window in ("30d", "7d", "1d", "90d", "36h", "1h", "1y", "5y", "all"):
        if window in _payload_cache:
            continue  # a user hit it first — SWR owns it now
        _refreshing.add(window)
        await _refresh_payload(window)
        await asyncio.sleep(5)
    if "history" not in _payload_cache:
        _refreshing.add("history")
        await _refresh_payload("history")
    log.info("tools/activity: cache pre-warm complete (%d windows)", len(_payload_cache))

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

    if window == "live":
        return templates.TemplateResponse(request, "tools_activity.html", {
            "window": "live",
            "window_label": "Live · Tranquility",
            "window_options": [(k, v[0]) for k, v in _WINDOWS.items()],
            "live_mode": True,
            "peak_pcu": 0, "mean_pcu": 0, "total_kills": 0, "total_isk": 0,
            "source_counts": {},
            "daily_kills_counts": [], "daily_kills_labels": [],
            "breakdowns_available": False, "has_breakdown_data": False,
            "zone_available": False, "has_zone_data": False,
            "has_heatmap_data": False,
        })

    if window not in _WINDOWS:
        window = "30d"

    # SWR: fresh → serve; stale → serve stale + refresh in background;
    # miss (first hit after restart) → compute inline.
    cached = _payload_cache.get(window)
    if cached is not None:
        fresh_until, payload = cached
        if datetime.now(timezone.utc).replace(tzinfo=None) >= fresh_until:
            if window not in _refreshing:
                _refreshing.add(window)
                asyncio.create_task(_refresh_payload(window))
        return templates.TemplateResponse(request, "tools_activity.html", {**payload})

    payload = await _build_activity_payload(db, window)
    _payload_cache[window] = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=_WINDOW_TTL_SECONDS[window]),
        payload,
    )
    return templates.TemplateResponse(request, "tools_activity.html", {**payload})


async def _build_activity_payload(db: AsyncSession, window: str) -> dict:
    """Compute the full /tools/activity template context for one window.

    Called inline on a cache miss and from the SWR background refresher
    (which passes its own AsyncSessionLocal session).
    """
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

    # ── Killmail series: ISK + fleet-size + NPC split + zone split ──
    # ONE window scan instead of four. All four series group the same
    # `killmail_time >= cutoff` rows by the same bin expression; running
    # them as separate queries meant four random-read passes over ~530k
    # rows on the 30d window (~6.3s measured on prod 2026-07-04 — the
    # reason this page was slow). Group by bin × fleet-bucket × npc × zone
    # (≤ num_bins×4×2×5 rows ≈ 10k) and decompose in Python. The LEFT JOIN
    # to sde_systems is a per-row PK lookup that preserves row count, so
    # ISK/count sums match the unjoined originals exactly.
    breakdowns_available = window in ("1h", "1d", "36h", "7d", "30d")
    has_breakdown_data = False
    solo_fleet_series: dict[str, list[int]] = {}
    npc_player_series: dict[str, list[int]] = {}
    isk_buckets: list[float] = [0.0] * num_bins
    zone_available = True
    zone_series: dict[str, list[int]] = {z: [0] * num_bins for z in _ZONES}
    zone_isk_series: dict[str, list[float]] = {z: [0.0] * num_bins for z in _ZONES}

    if bin_seconds < 86400:
        bucket_expr = case(
            (Killmail.attacker_count <= 1, "solo"),
            (Killmail.attacker_count <= 10, "small"),
            (Killmail.attacker_count <= 50, "medium"),
            else_="large",
        ).label("bucket")
        zone_expr = case(
            (Killmail.solar_system_id >= 31000000, "wormhole"),
            (SDESystem.security.is_(None), "unknown"),
            (func.round(SDESystem.security, 1) >= 0.5, "highsec"),
            (func.round(SDESystem.security, 1) > 0.0, "lowsec"),
            else_="nullsec",
        ).label("zone")
        combined_q = (
            select(
                _bin_expr(Killmail.killmail_time).label("b"),
                bucket_expr,
                Killmail.is_npc.label("npc"),
                zone_expr,
                func.count().label("n"),
                func.sum(Killmail.total_value).label("isk"),
            )
            .select_from(Killmail)
            .join(SDESystem, SDESystem.system_id == Killmail.solar_system_id, isouter=True)
            .where(Killmail.killmail_time >= cutoff)
            .group_by("b", "bucket", "npc", "zone")
        )
        if breakdowns_available:
            for name in ("solo", "small", "medium", "large"):
                solo_fleet_series[name] = [0] * num_bins
            npc_player_series["npc"] = [0] * num_bins
            npc_player_series["player"] = [0] * num_bins
        for b, bucket, is_npc, zone, n, isk in (await db.execute(combined_q)).all():
            if b is None:
                continue
            i = int(b)
            if not (0 <= i < num_bins):
                continue
            cnt = int(n or 0)
            isk_f = float(isk or 0.0)
            isk_buckets[i] += isk_f
            if breakdowns_available:
                if bucket in solo_fleet_series:
                    solo_fleet_series[bucket][i] += cnt
                npc_player_series["npc" if is_npc else "player"][i] += cnt
            # 'unknown' zone rows still count toward ISK/breakdowns above,
            # they just don't chart in the 4-zone split (as before).
            if zone in zone_series:
                zone_series[zone][i] += cnt
                zone_isk_series[zone][i] += isk_f
        if breakdowns_available:
            has_breakdown_data = (
                any(sum(v) > 0 for v in solo_fleet_series.values())
                or any(sum(v) > 0 for v in npc_player_series.values())
            )
    else:
        # Day+ bins (1y/5y/all): ISK from the daily aggregate — NEVER a raw
        # killmails scan (the unwindowed GROUP BY temp b-tree OOM-killed the
        # container on 2026-07-04; see docs/superpowers/specs/
        # 2026-07-04-activity-history-browser-design.md). Only vigilant rows
        # carry ISK; dates the T-040 backfill hasn't reached yet simply
        # contribute 0 to their bin.
        isk_rows = (await db.execute(
            select(KillmailDailyAggregate.date,
                   KillmailDailyAggregate.total_isk_destroyed)
            .where(
                KillmailDailyAggregate.date >= cutoff.date(),
                KillmailDailyAggregate.total_isk_destroyed.isnot(None),
            )
        )).all()
        for d, isk in isk_rows:
            d_dt = datetime(d.year, d.month, d.day)
            idx = int((d_dt - cutoff).total_seconds() // bin_seconds)
            if 0 <= idx < num_bins:
                isk_buckets[idx] += float(isk or 0.0)
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
        "live_mode": False,
    }
    return payload


async def _build_history_payload(db: AsyncSession) -> dict:
    """Full daily timeline for the History browser: 2003-05-28 → today,
    parallel arrays, None where a series has no coverage. ~8,400 rows of
    pre-aggregated dailies — bounded regardless of killmail volume."""
    start = _FIRST_PCU.date()
    today = datetime.now(timezone.utc).date()
    n = (today - start).days + 1
    dates = [start + timedelta(days=i) for i in range(n)]

    pcu_avg: list[int | None] = [None] * n
    pcu_peak: list[int | None] = [None] * n
    kills: list[int | None] = [None] * n
    isk: list[float | None] = [None] * n

    best_pcu: dict = {}
    for d, src, avg_pc, peak_pc in (await db.execute(
        select(PlayerCountDailyAggregate.date, PlayerCountDailyAggregate.source,
               PlayerCountDailyAggregate.avg_pc, PlayerCountDailyAggregate.peak_pc)
    )).all():
        pri = _PCU_SOURCE_PRIORITY.get(src, 99)
        cur = best_pcu.get(d)
        if cur is None or pri < cur[0]:
            best_pcu[d] = (pri, avg_pc, peak_pc)
    for d, (_pri, avg_pc, peak_pc) in best_pcu.items():
        i = (d - start).days
        if 0 <= i < n:
            pcu_avg[i] = round(float(avg_pc)) if avg_pc is not None else None
            pcu_peak[i] = int(peak_pc) if peak_pc is not None else None

    best_kda: dict = {}
    for d, src, kc, isk_v in (await db.execute(
        select(KillmailDailyAggregate.date, KillmailDailyAggregate.source,
               KillmailDailyAggregate.kill_count,
               KillmailDailyAggregate.total_isk_destroyed)
    )).all():
        cur = best_kda.get(d)
        if cur is None or (cur[0] == "zkb-totals" and src == "vigilant"):
            best_kda[d] = (src, kc, isk_v)
    for d, (_src, kc, isk_v) in best_kda.items():
        i = (d - start).days
        if 0 <= i < n:
            kills[i] = int(kc) if kc is not None else None
            isk[i] = float(isk_v) if isk_v is not None else None

    return {
        "dates": [d.isoformat() for d in dates],
        "pcu_avg": pcu_avg, "pcu_peak": pcu_peak, "kills": kills, "isk": isk,
    }


@router.get("/tools/activity/history.json")
async def tools_activity_history(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "auth"}, status_code=401)
    cached = _payload_cache.get("history")
    if cached is not None:
        fresh_until, payload = cached
        if datetime.now(timezone.utc).replace(tzinfo=None) >= fresh_until:
            if "history" not in _refreshing:
                _refreshing.add("history")
                asyncio.create_task(_refresh_payload("history"))
        return JSONResponse(payload)
    try:
        payload = await _build_history_payload(db)
    except Exception:
        log.exception("tools/activity: history build failed")
        return JSONResponse({"error": "history unavailable"}, status_code=500)
    _payload_cache["history"] = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=_WINDOW_TTL_SECONDS["history"]),
        payload,
    )
    return JSONResponse(payload)


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

    # Prior-period data is historical, so it changes only as `now` drifts —
    # cache every window with the same TTLs as the main payload.
    cached = _compare_cache.get(window)
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
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        seconds=_WINDOW_TTL_SECONDS[window]
    )
    _compare_cache[window] = (expires_at, body)
    return JSONResponse(body)
