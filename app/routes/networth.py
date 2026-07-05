"""Tools -> Net-worth tracker (Phase 5 Task 1).

Surfaces, all auth-gated:

  * `/tools/networth`              — stacked per-character net-worth chart page.
  * `/tools/networth/data.json`    — JSON feed for the chart, range-sliced.
  * `/tools/networth/snapshot`     — htmx POST: take a snapshot RIGHT NOW for
                                     the logged-in user's characters, so the
                                     first data point doesn't wait a day.

Data comes from the `net_worth_snapshots` table, filled daily by the
`_background_scheduler` tick (see `app/networth/snapshot.py` for the valuation
rationale + what's included/excluded). Reads filter snapshots to the user's own
character_ids (Character.user_id is nullable, so we resolve char ids first
rather than trusting the denormalized snapshot.user_id). The snapshot table is
tiny (one row per character per day), so range slicing + per-day summing happen
in Python after a single indexed range query — no killmail-scale concern.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, NetWorthSnapshot, get_db
from app.networth.snapshot import snapshot_for_characters

router = APIRouter(tags=["networth"])
# MUST be named `templates` — main.py's sys.modules loop pushes the nav globals
# onto every Jinja2Templates instance named `templates` under app.routes.*.
templates = Jinja2Templates(directory="app/templates")

# Range toggle -> lookback days.
_RANGES = {"30d": 30, "90d": 90, "1y": 365}


async def _user_characters(db: AsyncSession, user_id: int) -> list[Character]:
    rows = (await db.execute(
        select(Character).where(Character.user_id == user_id)
    )).scalars().all()
    return list(rows)


@router.get("/tools/networth", response_class=HTMLResponse)
async def networth_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Net-worth chart landing page."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "networth.html", {})


@router.get("/tools/networth/data.json")
async def networth_data(
    request: Request, range: str = "90d", db: AsyncSession = Depends(get_db),
):
    """JSON feed for the chart: one series per character (per-day total) plus a
    per-day account total. Range-sliced by snapshot date."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "auth"}, status_code=401)

    days = _RANGES.get(range, 90)
    rng = range if range in _RANGES else "90d"

    chars = await _user_characters(db, user_id)
    name_by_cid = {c.character_id: c.character_name for c in chars}
    cids = list(name_by_cid.keys())
    if not cids:
        return JSONResponse({"range": rng, "dates": [], "characters": [],
                             "total": [], "unpriced_count": 0})

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows = (await db.execute(
        select(
            NetWorthSnapshot.character_id, NetWorthSnapshot.date,
            NetWorthSnapshot.total, NetWorthSnapshot.unpriced_count,
        )
        .where(
            NetWorthSnapshot.character_id.in_(cids),
            NetWorthSnapshot.date >= cutoff,
        )
        .order_by(NetWorthSnapshot.date)
    )).all()

    # Build the shared date axis (sorted union of every date seen) and a
    # per-character {date -> total} map so each series aligns to that axis.
    dates: list[str] = []
    seen: set[str] = set()
    per_char: dict[int, dict[str, float]] = {cid: {} for cid in cids}
    unpriced_latest: dict[int, int] = {}
    for cid, d, total, unpriced in rows:
        iso = d.isoformat()
        if iso not in seen:
            seen.add(iso)
            dates.append(iso)
        per_char[cid][iso] = float(total or 0.0)
        unpriced_latest[cid] = unpriced or 0  # rows are date-ascending -> last wins

    # Only surface characters that actually have a data point in the window.
    active_cids = [cid for cid in cids if per_char[cid]]
    series = [
        {
            "character_id": cid,
            "name": name_by_cid.get(cid, str(cid)),
            "total": [per_char[cid].get(iso) for iso in dates],
        }
        for cid in active_cids
    ]
    account_total = [
        sum(per_char[cid].get(iso) or 0.0 for cid in active_cids) for iso in dates
    ]
    unpriced_total = sum(unpriced_latest.get(cid, 0) for cid in active_cids)

    return JSONResponse({
        "range": rng,
        "dates": dates,
        "characters": series,
        "total": account_total,
        "unpriced_count": unpriced_total,
    })


@router.post("/tools/networth/snapshot", response_class=HTMLResponse)
async def networth_snapshot_now(request: Request, db: AsyncSession = Depends(get_db)):
    """htmx POST: take a net-worth snapshot for the current user's characters
    immediately (same code path as the daily job). Returns a small status
    partial and fires an `HX-Trigger` so the page reloads the chart."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    chars = await _user_characters(db, user_id)
    result = await snapshot_for_characters(db, chars)

    resp = templates.TemplateResponse(
        request, "partials/networth_snapshot_status.html",
        {"result": result},
    )
    resp.headers["HX-Trigger"] = "networthSnapshot"
    return resp
