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

from sqlalchemy import Integer, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard.attention import pi_expiry_state
from app.db.models import NetWorthSnapshot, WalletSnapshot

SPARKLINE_WINDOW = timedelta(days=7)
# T-080: bucket the window into 3h buckets (at most ~56 points/pilot) instead
# of returning every ~2-minute snapshot (~126k rows for 24 pilots measured on
# a real install) for Python to group. See load_wallet_sparkline_points.
SPARKLINE_BUCKET = timedelta(hours=3)
# SQLite's default compound-select limit is 500 terms; stay well clear of it.
# See _build_latest_networth_stmt's docstring — load_latest_networth() chunks
# character_ids by this many per UNION ALL statement.
_MAX_IDS_PER_STATEMENT = 200


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
    produces, derived from the endpoints of the sparkline's own window
    instead of the exact-7d baseline query.

    NOT what the route wires up next to the sparkline — a Detailed card
    shows one wallet delta (app.dashboard.walletdelta's, the same figure
    the Wallet row's arrow uses), never two numbers that could disagree
    because their windows differ slightly (the sparkline's oldest point
    might be 6 days old, not exactly 7). Kept as a tested, independent pure
    function for a caller that genuinely wants the sparkline's own window
    reflected back, rather than deleted outright."""
    if not points or len(points) < 2:
        return None
    first, last = points[0][1], points[-1][1]
    delta = last - first
    direction = "flat" if delta == 0 else ("up" if delta > 0 else "down")
    return {"direction": direction, "amount": abs(delta)}


# ── I/O: one batched statement each, for every character at once ────────────

def _build_latest_networth_stmt(character_ids: list[int]):
    """The query itself, split out of load_latest_networth() so
    tests/test_dashboard_detail_perf.py can run EXPLAIN QUERY PLAN on
    exactly what production executes. `character_ids` must be non-empty —
    the public function handles the empty-list short-circuit.

    T-080: same UNION ALL-of-per-character-seeks shape as
    app.dashboard.walletdelta._build_wallet_baselines_stmt, for the same
    reason — a ROW_NUMBER() window here scans every NetWorthSnapshot row
    for the given characters before it can rank anything, with no date
    bound at all. NetWorthSnapshot writes once a day per character (not
    every ~2 minutes like WalletSnapshot), so this was never close to
    wallet_snapshots' cost — but the fix is the same one line of reasoning,
    it's just as cheap to apply, and NetWorthSnapshot's composite PK
    (character_id, date) already gives each branch's `ORDER BY date DESC
    LIMIT 1` its own index seek for free (SEARCH ... USING INDEX
    sqlite_autoindex_net_worth_snapshots_1).

    Callers get a single chunk of at most `_MAX_IDS_PER_STATEMENT` ids —
    load_latest_networth() does the chunking (see its docstring), same as
    app.dashboard.walletdelta.load_wallet_baselines, to stay clear of
    SQLite's compound-select term limit.
    """
    return union_all(*[
        select(
            literal(cid).label("character_id"),
            (
                select(NetWorthSnapshot.total)
                .where(NetWorthSnapshot.character_id == cid)
                .order_by(NetWorthSnapshot.date.desc())
                .limit(1)
                .scalar_subquery()
            ).label("total"),
        )
        for cid in character_ids
    ])


async def load_latest_networth(db: AsyncSession, character_ids: list[int]) -> dict[int, float]:
    """{character_id: total} for each character's most recent
    NetWorthSnapshot (one daily row per character), in ONE statement — or,
    past `_MAX_IDS_PER_STATEMENT` ids, one statement per chunk (SQLite caps
    a UNION ALL at 500 terms; see _build_latest_networth_stmt)."""
    if not character_ids:
        return {}
    out: dict[int, float] = {}
    for i in range(0, len(character_ids), _MAX_IDS_PER_STATEMENT):
        chunk = character_ids[i:i + _MAX_IDS_PER_STATEMENT]
        rows = (await db.execute(_build_latest_networth_stmt(chunk))).all()
        out.update((cid, total) for cid, total in rows if total is not None)
    return out


async def load_wallet_sparkline_points(
    db: AsyncSession, character_ids: list[int], now: datetime | None = None,
) -> dict[int, list[tuple[datetime, float]]]:
    """{character_id: [(recorded_at, balance), ...]} (chronological) over the
    last 7 days, for every id in `character_ids`, in ONE statement.

    T-080: a real install writes a WalletSnapshot roughly every 2 minutes, so
    the naive "every row in the window" query measured ~126k rows for 24
    pilots, all shipped to Python just to be grouped by character. Downsample
    in SQL instead: bucket the window into SPARKLINE_BUCKET-wide buckets and
    keep only the last (highest recorded_at) balance per
    (character_id, bucket), via SQLite's documented "bare column" behaviour —
    a bare, non-aggregated, non-GROUP-BY column in a query with a single
    MIN()/MAX() takes its value from the row that produced that MIN/MAX. At
    most ~56 points per pilot come back, not ~4200.

    The WHERE clause alone (character_id IN (...) AND recorded_at >= cutoff)
    is what makes SQLite seek ix_wallet_snapshots_char_recorded per
    character id instead of scanning the table; GROUP BY only reduces what
    comes back afterwards. See tests/test_dashboard_detail_perf.py for the
    EXPLAIN QUERY PLAN and timing.
    """
    if not character_ids:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - SPARKLINE_WINDOW
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

    rows = (await db.execute(_build_wallet_sparkline_stmt(character_ids, cutoff))).all()
    out: dict[int, list[tuple[datetime, float]]] = {}
    for cid, recorded_at, balance in rows:
        out.setdefault(cid, []).append((recorded_at, balance))
    return out


def _build_wallet_sparkline_stmt(character_ids: list[int], cutoff: datetime):
    """The query itself, split out of load_wallet_sparkline_points() so
    tests/test_dashboard_detail_perf.py can run EXPLAIN QUERY PLAN on
    exactly what production executes. `character_ids` must be non-empty —
    the public function handles the empty-list short-circuit."""
    bucket_seconds = int(SPARKLINE_BUCKET.total_seconds())
    epoch_secs = cast(func.strftime("%s", WalletSnapshot.recorded_at), Integer)
    # `.op("/")` (SQL integer division), not Python's `/` — SQLAlchemy's `/`
    # on an Integer coerces the RHS to NUMERIC to mimic Python true-division,
    # which would turn the bucket into a near-unique fraction per row and
    # defeat the GROUP BY entirely.
    bucket = epoch_secs.op("/")(bucket_seconds).label("bucket")
    latest_at = func.max(WalletSnapshot.recorded_at)

    return (
        select(WalletSnapshot.character_id, latest_at.label("recorded_at"), WalletSnapshot.balance)
        .where(WalletSnapshot.character_id.in_(character_ids))
        .where(WalletSnapshot.recorded_at >= cutoff)
        .group_by(WalletSnapshot.character_id, bucket)
        .order_by(WalletSnapshot.character_id, latest_at)
    )
