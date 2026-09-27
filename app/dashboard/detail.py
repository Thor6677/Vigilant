"""T-076 Detailed-mode (and Table-mode) per-pilot extras: PI, industry,
market escrow, net worth, a 7-day wallet sparkline, and synced SP. Computed
ONLY for Detailed/Table modes (never Compact/Cards) — see the perf note in
app/routes/dashboard.py's dashboard() route.

Pure functions here take already-parsed cache data (the same `pi`/`contracts`-
shaped dicts app.routes.dashboard._build_data_from_caches produces, plus raw
`industry_json`/`orders_json` lists) and read no clock/DB of their own except
`now`, which every caller passes explicitly — same discipline as
app.dashboard.attention. The two async functions at the bottom are the only
I/O in this module, and each is one batched statement for every character.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard.attention import pi_expiry_state
from app.db.models import NetWorthSnapshot, WalletSnapshot

SPARKLINE_WINDOW = timedelta(days=7)


def _format_duration(seconds: float) -> str:
    """Local copy — see app.routes.dashboard._format_duration /
    app.routes.pi._format_duration for the other two of this codebase's
    established per-module duplicates."""
    if seconds <= 0:
        return "expired"
    seconds = int(seconds)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def pi_detail(planets, now: datetime | None = None) -> dict | None:
    """`planets` is the raw pi_json list for one character (or "no_scope" /
    None / anything else not-a-list, all of which mean "nothing to show").
    Returns None, or {"colonies": int, "expiry_str": str, "expired": bool} —
    "expired" wins over a soonest-upcoming expiry once any colony has one.
    """
    if not isinstance(planets, list) or not planets:
        return None
    now = now or datetime.now(timezone.utc)
    soonest: datetime | None = None
    any_expired = False
    for planet in planets:
        warning, expiry_dt = pi_expiry_state(planet.get("expiry_time"), now)
        if warning == "expired":
            any_expired = True
        if expiry_dt is not None and (soonest is None or expiry_dt < soonest):
            soonest = expiry_dt
    if any_expired:
        expiry_str = "expired"
    elif soonest is not None:
        expiry_str = _format_duration((soonest - now).total_seconds())
    else:
        expiry_str = "—"
    return {"colonies": len(planets), "expiry_str": expiry_str, "expired": any_expired}


def industry_detail(jobs) -> dict | None:
    """`jobs` is the raw industry_json list (trimmed: activity_id,
    blueprint_type_id, product_type_id, runs, status — no end_date, see
    CharacterDashboardCache.industry_json). None for no_scope/not-synced/empty."""
    if not isinstance(jobs, list) or not jobs:
        return None
    active = sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "active")
    ready = sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "ready")
    return {"active": active, "ready": ready}


def market_detail(orders) -> dict | None:
    """`orders` is the raw orders_json list. Escrow here is literally the
    ISK ESI reports withheld against buy orders (`o["escrow"]`) — not the
    full net-worth "market-locked value" estimate app.networth.snapshot
    computes (which prices sell-order goods too and needs a price map this
    module deliberately never fetches — no ESI/price call on a page
    request). None for no_scope/not-synced/empty."""
    if not isinstance(orders, list) or not orders:
        return None
    escrow = sum(float(o.get("escrow") or 0.0) for o in orders if isinstance(o, dict))
    return {"open_orders": len(orders), "escrow": escrow}


def sp_detail(summary) -> dict | str | None:
    """`summary` is an app.character_skills.skill_summary() result:
    "no_scope" | None (not synced yet) | {"total_sp", "unallocated_sp", ...}.
    Passed straight through as the first two cases; a dict summary reduces
    to just the two counts the card/table need."""
    if summary == "no_scope":
        return "no_scope"
    if not isinstance(summary, dict):
        return None
    return {
        "total_sp": int(summary.get("total_sp") or 0),
        "unallocated_sp": int(summary.get("unallocated_sp") or 0),
    }


def wallet_sparkline_svg(points: list[tuple[datetime, float]], width: int = 100, height: int = 28) -> str | None:
    """Pure: a minimal inline SVG sparkline (line + area fill + an
    emphasised endpoint dot), coloured by net direction over the window.
    `points` chronological (oldest first). All-numeric output — safe to
    mark `| safe` in the template, since nothing here is user text.
    Fewer than 2 points (no real trend to draw) returns None."""
    if not points or len(points) < 2:
        return None
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    xs = [width * i / (n - 1) for i in range(n)]
    ys = [height - ((v - lo) / span) * (height - 4) - 2 for v in values]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"0,{height} " + line + f" {width:.1f},{height}"
    up = values[-1] >= values[0]
    color = "var(--success)" if up else "var(--danger)"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon points="{area}" fill="{color}" fill-opacity="0.15"></polygon>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.5"></polyline>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2" fill="{color}"></circle>'
        f"</svg>"
    )


def sparkline_delta(points: list[tuple[datetime, float]]) -> dict | None:
    """The same {"direction", "amount"} shape app.dashboard.walletdelta
    produces, derived from the endpoints of the sparkline's own window —
    kept independent so a pilot's card can show a sparkline even on a day
    the 7d-exact baseline query missed (e.g. the oldest point in range is
    6 days old, not exactly 7)."""
    if not points or len(points) < 2:
        return None
    first, last = points[0][1], points[-1][1]
    delta = last - first
    direction = "flat" if delta == 0 else ("up" if delta > 0 else "down")
    return {"direction": direction, "amount": abs(delta)}


# ── I/O: one batched statement each, for every character at once ────────────

async def load_latest_networth(db: AsyncSession, character_ids: list[int]) -> dict[int, float]:
    """{character_id: total} for each character's most recent
    NetWorthSnapshot (one daily row per character) — one statement via a
    ROW_NUMBER() window, not one query per pilot."""
    if not character_ids:
        return {}
    row_number = (
        func.row_number()
        .over(partition_by=NetWorthSnapshot.character_id, order_by=NetWorthSnapshot.date.desc())
        .label("rn")
    )
    subq = (
        select(NetWorthSnapshot.character_id, NetWorthSnapshot.total, row_number)
        .where(NetWorthSnapshot.character_id.in_(character_ids))
        .subquery()
    )
    rows = (await db.execute(select(subq.c.character_id, subq.c.total).where(subq.c.rn == 1))).all()
    return {cid: total for cid, total in rows}


async def load_wallet_sparkline_points(
    db: AsyncSession, character_ids: list[int], now: datetime | None = None,
) -> dict[int, list[tuple[datetime, float]]]:
    """{character_id: [(recorded_at, balance), ...]} (chronological) over the
    last 7 days, for every id in `character_ids`, in ONE statement — every
    row is fetched together and grouped in Python, not queried per pilot."""
    if not character_ids:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - SPARKLINE_WINDOW
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

    rows = (
        await db.execute(
            select(WalletSnapshot.character_id, WalletSnapshot.recorded_at, WalletSnapshot.balance)
            .where(WalletSnapshot.character_id.in_(character_ids))
            .where(WalletSnapshot.recorded_at >= cutoff)
            .order_by(WalletSnapshot.character_id, WalletSnapshot.recorded_at)
        )
    ).all()
    out: dict[int, list[tuple[datetime, float]]] = {}
    for cid, recorded_at, balance in rows:
        out.setdefault(cid, []).append((recorded_at, balance))
    return out
