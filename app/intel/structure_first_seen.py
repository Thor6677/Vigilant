"""Read side of structure_first_seen (T-090): the EVE Ref upper bound for
structure ages, above the newest calibration point (PR 1) and between two
calibration points (PR 2), and the admin shadow stats.

first_seen is when EVE Ref first LISTED a structure, so it can only be later
than the anchor date. It tightens the latest side of an estimate and pulls the
midpoint down. Above the calibration range the earliest side stays as wide as
the extrapolation made it, or wider by the measured lag; inside it the window
never grows and never leaves the neighbouring anchors. Nothing here is "exact"
and nothing here touches structure_age_calibration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (StructureAgeCalibration, StructureFirstSeen,
                           StructureFirstSeenImport)

MIN_REFINED_DAYS = 2         # in range, a refined window is never narrower than this
K_VOTES = 3                  # U(X) = k-th smallest non-censored first_seen at ids >= X
DEFAULT_LAG_DAYS = 14.0
MIN_LAG_SAMPLES = 20
LAG_WINDOW_DAYS = 180
MAX_LAG_DAYS = 90.0
STALE_AFTER_DAYS = 3

FS = StructureFirstSeen
Cal = StructureAgeCalibration


async def own_first_seen(db: AsyncSession, structure_id: int) -> datetime | None:
    return await db.scalar(select(FS.first_seen).where(FS.structure_id == structure_id))


async def soft_upper_bound(db: AsyncSession, structure_id: int,
                           below: int | None = None) -> datetime | None:
    """The K_VOTES-th smallest non-censored first_seen among ids >= structure_id
    (a later id was listed no earlier than this one existed, give or take the
    noise in id order, so the k-th smallest tolerates a few early outliers).
    With `below`, only ids under it vote (the gap up to a neighbouring
    calibration point). None when fewer than K_VOTES such ids exist."""
    q = select(FS.first_seen).where(FS.structure_id >= structure_id, FS.censored.is_(False))
    if below is not None:
        q = q.where(FS.structure_id < below)
    rows = (await db.execute(q.order_by(FS.first_seen).limit(K_VOTES))).scalars().all()
    return rows[-1] if len(rows) == K_VOTES else None


async def measured_lag_days(db: AsyncSession) -> float:
    """Median days between a recent triff anchor and EVE Ref first listing it
    (non-censored, positive lags only). DEFAULT_LAG_DAYS with too few samples."""
    newest = await db.scalar(select(func.max(Cal.anchor_mid)).where(Cal.source == "triff"))
    if newest is None:
        return DEFAULT_LAG_DAYS
    rows = (await db.execute(
        select(Cal.anchor_mid, FS.first_seen).join(FS, FS.structure_id == Cal.structure_id)
        .where(Cal.source == "triff", FS.censored.is_(False),
               Cal.anchor_mid >= newest - timedelta(days=LAG_WINDOW_DAYS)))).all()
    lags = [(f - a).total_seconds() / 86400 for a, f in rows if f > a]
    if len(lags) < MIN_LAG_SAMPLES:
        return DEFAULT_LAG_DAYS
    return min(max(median(lags), 1.0), MAX_LAG_DAYS)


def refine_estimate(est: dict, own: datetime | None, soft: datetime | None,
                    lag_days: float) -> dict:
    """Tighten a tail extrapolation with EVE Ref: latest = min(extrapolated
    latest, own first_seen, soft bound). When that is earlier than the
    midpoint, the midpoint moves to bound minus the measured lag and the
    earliest side widens to match; it never moves later. Returns est itself
    when EVE Ref adds nothing."""
    bounds = [b for b in (own, soft) if b is not None]
    if not bounds:
        return est
    upper = min(bounds)
    out = dict(est)
    if upper < est["high"]:
        out["high"] = upper
    if upper - timedelta(days=lag_days) < est["mid"]:
        out["mid"] = max(upper - timedelta(days=lag_days), datetime(2016, 4, 26))
        out["low"] = min(est["low"], out["mid"] - timedelta(days=lag_days))
    out["high"] = max(out["high"], out["mid"])
    out["low"] = min(out["low"], out["mid"])
    out["days_wide"] = round((out["high"] - out["low"]).total_seconds() / 86400, 1)
    if own is not None:
        out["note"] = (f"EVE Ref first saw it on {own.strftime('%d %b %Y')}, so it is at most "
                       "that old; the earliest date is a soft estimate, not a measurement")
    else:
        out["note"] = est.get("note", "") + " — latest date bounded by EVE Ref's first sightings of newer structures"
    out["source"] = "everef"
    return out


async def tail_refinement(db: AsyncSession, structure_id: int, est: dict) -> dict:
    """est (from neighbour_estimate, for an id above every calibration point)
    refined with EVE Ref data. Unchanged when the table has nothing for it."""
    own = await own_first_seen(db, structure_id)
    soft = await soft_upper_bound(db, structure_id)
    if own is None and soft is None:
        return est
    return refine_estimate(est, own, soft, await measured_lag_days(db))


def refine_between(est: dict, own: datetime | None, soft: datetime | None,
                   floor: datetime) -> dict:
    """Tighten an in-range estimate with EVE Ref. B = min(own, soft) is a
    latest date. The window slides earlier so its latest side meets B, keeping
    its width, but never below `floor` (the lowest neighbouring anchor date)
    and never narrower than MIN_REFINED_DAYS; it never grows, never moves
    later, and the midpoint only moves down. Returns est itself when B is no
    earlier than est's latest date. Never "exact"."""
    bounds = [b for b in (own, soft) if b is not None]
    if not bounds:
        return est
    bound = min(bounds)
    if bound >= est["high"]:
        return est
    width = est["high"] - est["low"]
    keep = min(width, timedelta(days=MIN_REFINED_DAYS))
    high = max(bound, floor + keep)
    low = max(floor, high - width)
    mid = min(est["mid"], low + (high - low) / 2)
    return {**est, "low": low, "mid": mid, "high": high,
            "days_wide": round((high - low).total_seconds() / 86400, 1),
            "source": "everef"}


async def range_refinement(db: AsyncSession, structure_id: int, est: dict,
                           lower, upper) -> dict:
    """est (an interpolation between calibration points `lower` and `upper`,
    neither on the id itself) refined with EVE Ref. Own first sighting counts
    even when censored, as in the tail; the soft bound only takes votes from
    ids inside this gap. Unchanged when the table has nothing for it."""
    own = await own_first_seen(db, structure_id)
    soft = await soft_upper_bound(db, structure_id, below=upper.structure_id)
    if own is None and soft is None:
        return est
    floor = min(lower.anchor_low, upper.anchor_low, est["low"])
    out = refine_between(est, own, soft, floor)
    if out is est:
        return est
    if own is not None and (soft is None or own <= soft):
        text = (f"EVE Ref first saw it on {own.strftime('%d %b %Y')}, so it is at most "
                "that old; the range stays between the neighbouring calibration points "
                "(not a measurement)")
    else:
        text = "latest date bounded by EVE Ref's first sightings of nearby structures"
    bound = min(b for b in (own, soft) if b is not None)
    if bound < floor + min(est["high"] - est["low"], timedelta(days=MIN_REFINED_DAYS)):
        text = ("EVE Ref first saw it earlier than its neighbours suggest; ID order is "
                "noisy, so the range is held at the lower neighbour")
    base = est.get("note")
    out["note"] = f"{base} — {text}" if base else text
    return out


# ── Admin ─────────────────────────────────────────────────────────────────

async def ingest_status(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last = await db.scalar(select(func.max(StructureFirstSeenImport.imported_at)))
    age = (now - last).total_seconds() / 86400 if last else None
    return {
        "last": last.strftime("%Y-%m-%d %H:%M") if last else None,
        "age_days": round(age, 1) if age is not None else None,
        "stale": age is None or age > STALE_AFTER_DAYS,
        "files": await db.scalar(select(func.count()).select_from(StructureFirstSeenImport)),
        "ids": await db.scalar(select(func.count()).select_from(FS)),
        "censored_ids": await db.scalar(
            select(func.count()).select_from(FS).where(FS.censored.is_(True))),
    }


async def shadow_stats(db: AsyncSession) -> dict:
    """How EVE Ref first_seen compares with the frozen triff points it also
    lists (non-censored only). `after_share` is the share where first_seen is
    after the triff anchor date, as it must be if first_seen is an upper
    bound; the lag figures are first_seen minus anchor in days."""
    rows = (await db.execute(
        select(Cal.anchor_mid, FS.first_seen).join(FS, FS.structure_id == Cal.structure_id)
        .where(Cal.source == "triff", FS.censored.is_(False)))).all()
    if not rows:
        return {"matched": 0}
    lags = sorted((f - a).total_seconds() / 86400 for a, f in rows)
    n = len(lags)
    return {
        "matched": n,
        "after_share": round(100 * sum(1 for x in lags if x > 0) / n, 1),
        "median_lag": round(lags[n // 2], 1),
        "p90_lag": round(lags[min(n - 1, int(n * 0.9))], 1),
    }
