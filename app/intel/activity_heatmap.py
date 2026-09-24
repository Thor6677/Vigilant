"""Hour-of-week activity grids for the /tools/activity heatmap.

Pure query + math layer. Every function takes an AsyncSession (or plain
grids) and returns lists/dicts, so the route module owns caching and the
template context while tests can drive this without a FastAPI app.

WHY TWO SERIES SHARE ONE GRID
-----------------------------
* **Kills** are the only location-aware measure we have.
  `killmails.solar_system_id` joins `sde_systems` for security + region, so
  kills can be sliced by security zone or by region. That is what the scope
  selector filters, and it is what colours the cells.
* **Pilots online** (`player_count_snapshots`) is a server-wide concurrency
  count with no location dimension at all — there is no per-region PCU to
  be had from ESI. So it can only ever be the *presence* baseline, and it
  stays as a fixed backdrop no matter what scope is selected.

Blending the two into a single "activity" number would destroy the signal
the page exists to show: a null-sec 04:00 UTC bucket is busy by kills and
quiet by presence (timers land when the defender's timezone is asleep).
Two series, one grid, keeps that readable.

COST
----
The zone query is ONE aggregate over the trailing 90 days of `killmails`
joined to `sde_systems`, grouped by (zone, day-of-week, hour). It returns at
most 5 x 7 x 24 = 840 rows and fills all five zone grids at once — `all` is
the column-wise sum. Per-scope queries would have meant five range scans of
the same rows, and the scope predicate cannot use an index anyway (it is a
CASE over a joined column). The route caches the result and
`warm_activity_cache()` precomputes it, honouring the standing rule in
app/routes/player_stats.py against scanning raw killmails per request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail
from app.db.sde_models import SDERegion, SDESystem

# Trailing window for every grid on this panel. Matches the pilots-online
# heatmap that already shipped, so the two series cover the same days.
HEATMAP_DAYS = 90

# Below this many kills in the whole 90-day window a grid is noise — a
# handful of kills scattered over 168 buckets produces a near-empty square
# that reads as "this region is dead" when it really means "we have no
# sample". Say "not enough data" instead of drawing it.
MIN_KILLS_FOR_GRID = 200

# EVE's wormhole systems occupy a dedicated id range. Same rule the daily
# zone rollup uses — see zone_case_expr().
WORMHOLE_SYSTEM_ID_MIN = 31_000_000

# Scope keys are the URL vocabulary (?scope=ns). "all" is every zone,
# including the 'unknown' bucket for kills in systems the SDE has no row
# for, so the four zone grids need not sum to the all grid.
SCOPES = ("all", "hs", "ls", "ns", "j")
SCOPE_LABELS = {
    "all": "All",
    "hs": "High-sec",
    "ls": "Low-sec",
    "ns": "Null-sec",
    "j": "J-space",
}
# Scope key -> the zone label produced by zone_case_expr().
ZONE_FOR_SCOPE = {"hs": "highsec", "ls": "lowsec", "ns": "nullsec", "j": "wormhole"}
_SCOPE_FOR_ZONE = {zone: scope for scope, zone in ZONE_FOR_SCOPE.items()}

MODES = ("kills", "pilots", "percap")
MODE_LABELS = {
    "kills": "Kills",
    "pilots": "Pilots online",
    "percap": "Per capita",
}

# Prime-time bands as (label, start_hour_utc, end_hour_utc), end exclusive,
# wrap-around allowed (start > end means the band crosses midnight UTC).
#
# Each band brackets the local evening — roughly 18:00-23:00 local, when a
# player group is home from work and logging in — converted to UTC at that
# region's standard-time offset:
#
#   US West  UTC-8   18:00-22:00 local  ->  02:00-06:00 UTC
#   US East  UTC-5   18:00-22:00 local  ->  23:00-03:00 UTC  (wraps midnight)
#   EU       UTC+1   18:00-23:00 local  ->  17:00-22:00 UTC
#   AU       UTC+10  18:00-22:00 local  ->  08:00-12:00 UTC
#   CN       UTC+8   19:00-23:00 local  ->  11:00-15:00 UTC
#
# Standard time only: daylight saving moves the US and EU bands an hour for
# part of the year and Australia's the other way, which is why the legend
# says "approximate · DST not applied". These are a fixed reference overlay
# describing the player base, not a function of the data, so they do not
# change with scope, region or mode.
TZ_BANDS = [
    ("US West", 2, 6),
    ("US East", 23, 3),
    ("EU", 17, 22),
    ("AU", 8, 12),
    ("CN", 11, 15),
]


def zone_case_expr():
    """Security-zone classifier, in SQL, shared by every caller.

    Single source of truth for the thresholds: J-systems by id range
    regardless of their (negative) security, then security rounded to one
    decimal — >= 0.5 high-sec, > 0.0 low-sec, else null-sec. Systems with no
    SDE row land in 'unknown' rather than being dropped, so counts still add
    up while the SDE is mid-import.

    Lives here because this module is the one place that needs it as a
    reusable expression; app/intel/killmail_daily_rollup.py imports it so a
    threshold can never drift between the daily rollup and this panel.
    """
    return case(
        (Killmail.solar_system_id >= WORMHOLE_SYSTEM_ID_MIN, "wormhole"),
        (SDESystem.security.is_(None), "unknown"),
        (func.round(SDESystem.security, 1) >= 0.5, "highsec"),
        (func.round(SDESystem.security, 1) > 0.0, "lowsec"),
        else_="nullsec",
    ).label("zone")


def empty_grid(fill=0) -> list[list]:
    """7 rows (Mon..Sun) x 24 columns (UTC hour)."""
    return [[fill] * 24 for _ in range(7)]


def grid_row_index(dow: str | int) -> int:
    """SQLite strftime('%w') is Sun=0..Sat=6; the rendered grid is Mon-first.

    Same rotation the pilots-online grid already uses — keep them identical
    or the two series would be drawn a day apart.
    """
    return (int(dow) + 6) % 7


def grid_total(grid) -> float:
    return sum(v for row in grid for v in row if v is not None)


def grid_max(grid) -> float:
    """Largest value in this grid, 0 when empty.

    Colour is normalised against a grid's OWN max, never a global one: a
    region with a hundredth of null-sec's kill volume still needs to show
    its own internal shape rather than rendering uniformly black.
    """
    values = [v for row in grid for v in row if v is not None]
    return max(values) if values else 0


def per_capita_grid(kills_grid, pcu_grid) -> list[list[float | None]]:
    """Kills per 1,000 pilots online, per hour-of-week bucket.

    Units are deliberately mixed and the label says so: the numerator is a
    SUM of kills across the ~13 occurrences of that weekday-hour in 90 days,
    the denominator an AVERAGE concurrent headcount for the same bucket. The
    ratio is still the right shape for "how dangerous is this hour per
    player around", it just isn't a probability.

    Asymmetric None handling, on purpose: a bucket with no kills really is
    zero kills, but a bucket with no pilots sample is unknown, not zero
    players — so the cell stays None rather than dividing by a guess.
    """
    out: list[list[float | None]] = [[None] * 24 for _ in range(7)]
    for dow in range(7):
        for hr in range(24):
            pilots = pcu_grid[dow][hr] if pcu_grid else None
            if pilots is None or pilots <= 0:
                continue
            kills = kills_grid[dow][hr] or 0
            out[dow][hr] = round(kills * 1000.0 / float(pilots), 2)
    return out


def _cutoff(days: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - timedelta(days=days)


async def zone_hour_of_week_kills(
    db: AsyncSession, days: int = HEATMAP_DAYS, now: datetime | None = None
) -> dict[str, list[list[int]]]:
    """All five scope grids from ONE aggregate. Keys are SCOPES.

    Grouping by (zone, dow, hour) caps the result at 840 rows, so the whole
    fan-out happens in SQLite's GROUP BY rather than in Python over ~1.4M
    killmail rows. The join to sde_systems is a per-row primary-key lookup
    that preserves row count, so the 'all' grid equals an unjoined count.
    """
    rows = (
        await db.execute(
            select(
                zone_case_expr(),
                func.strftime("%w", Killmail.killmail_time).label("dow"),
                func.strftime("%H", Killmail.killmail_time).label("hr"),
                func.count().label("n"),
            )
            .select_from(Killmail)
            .join(
                SDESystem,
                SDESystem.system_id == Killmail.solar_system_id,
                isouter=True,
            )
            .where(Killmail.killmail_time >= _cutoff(days, now))
            .group_by("zone", "dow", "hr")
        )
    ).all()

    grids = {scope: empty_grid() for scope in SCOPES}
    for zone, dow, hr, n in rows:
        if dow is None or hr is None:
            continue
        row = grid_row_index(dow)
        col = int(hr)
        if not (0 <= row < 7 and 0 <= col < 24):
            continue
        count = int(n or 0)
        grids["all"][row][col] += count
        scope = _SCOPE_FOR_ZONE.get(zone)
        if scope is not None:
            grids[scope][row][col] += count
    return grids


async def region_hour_of_week_kills(
    db: AsyncSession,
    region_id: int,
    days: int = HEATMAP_DAYS,
    now: datetime | None = None,
) -> list[list[int]]:
    """One region's grid. Computed on demand, cached by the caller.

    The filter is `solar_system_id IN (systems of this region)` rather than
    a join predicate on region_id: that shape keeps the killmails side on
    ix_killmail_system_time (solar_system_id, killmail_time), and a region
    is a few hundred system ids at most.
    """
    system_ids = select(SDESystem.system_id).where(SDESystem.region_id == region_id)
    rows = (
        await db.execute(
            select(
                func.strftime("%w", Killmail.killmail_time).label("dow"),
                func.strftime("%H", Killmail.killmail_time).label("hr"),
                func.count().label("n"),
            )
            .where(
                Killmail.solar_system_id.in_(system_ids),
                Killmail.killmail_time >= _cutoff(days, now),
            )
            .group_by("dow", "hr")
        )
    ).all()

    grid = empty_grid()
    for dow, hr, n in rows:
        if dow is None or hr is None:
            continue
        row = grid_row_index(dow)
        col = int(hr)
        if 0 <= row < 7 and 0 <= col < 24:
            grid[row][col] += int(n or 0)
    return grid


async def region_options(db: AsyncSession) -> list[dict]:
    """Every region that has systems, as {region_id, region_name, jspace}.

    `jspace` is derived from the data rather than an assumed id range for
    the regions themselves: a region counts as J-space when it contains a
    system in the wormhole id range. That keeps shattered and drifter
    regions on the right side of the split without hardcoding which region
    ids CCP happened to use.
    """
    rows = (
        await db.execute(
            select(
                SDERegion.region_id,
                SDERegion.region_name,
                func.max(SDESystem.system_id).label("max_system_id"),
            )
            .select_from(SDERegion)
            .join(SDESystem, SDESystem.region_id == SDERegion.region_id)
            .group_by(SDERegion.region_id, SDERegion.region_name)
        )
    ).all()
    options = [
        {
            "region_id": int(region_id),
            "region_name": name,
            "jspace": int(max_system_id or 0) >= WORMHOLE_SYSTEM_ID_MIN,
        }
        for region_id, name, max_system_id in rows
    ]
    options.sort(key=lambda option: option["region_name"])
    return options
