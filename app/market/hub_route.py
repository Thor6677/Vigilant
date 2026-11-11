"""Jump counts from a starting system to the trade hubs.

The route is the one EVE's own route planner gives for "prefer safer"
(`/route/{origin}/{destination}/?flag=secure`) — the same call Gate Check
makes, so the two pages agree. Jump counts only — no hauling cost.

A route is labelled highsec when every system on it, both ends included, is
highsec; otherwise it "leaves highsec" (the planner falls back to such a
route when no safer one exists).

Each hub's answer is one of:
  * {"jumps": int, "highsec": bool}
  * None               — the planner says there is no route
  * UNAVAILABLE        — the planner could not be reached

Routes between fixed systems change only when the map does, so answers are
remembered in this process for a day; failures are not remembered."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal
from app.db.sde_models import SDESystem
from app.esi.client import ESIClient

logger = logging.getLogger(__name__)

# The client shows security rounded to one decimal, so 0.45 displays as 0.5.
HIGHSEC_MIN = 0.45
ROUTE_FLAG = "secure"
ROUTE_TTL = timedelta(hours=24)
MAX_REMEMBERED = 2000

UNAVAILABLE = {"unavailable": True}

# {(origin, destination): (when, [system ids] | None)}
_routes: dict[tuple[int, int], tuple[datetime, list[int] | None]] = {}


def _now() -> datetime:
    """Clock seam — monkeypatched in tests."""
    return datetime.now(timezone.utc)


def is_highsec(security: float | None) -> bool:
    return security is not None and security >= HIGHSEC_MIN


def describe(path: list[int], security: dict[int, float | None]) -> dict:
    """{"jumps", "highsec"} for a route given as its systems, ends included."""
    return {"jumps": max(len(path) - 1, 0),
            "highsec": all(is_highsec(security.get(s)) for s in path)}


async def _fetch_route(origin: int, destination: int) -> list[int] | None:
    """The planner's route as system ids (ends included), or None when it
    says there is none (404). Any other failure raises."""
    async with AsyncSessionLocal() as esi_db:
        client = ESIClient("", db=esi_db)
        try:
            route = await client.get_public(
                f"/route/{origin}/{destination}/", params={"flag": ROUTE_FLAG})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
    if not isinstance(route, list) or not route:
        raise ValueError("unexpected route response")
    return [int(s) for s in route]


async def _route(origin: int, destination: int):
    """A remembered or freshly fetched route: a list, None (no route), or
    UNAVAILABLE."""
    if origin == destination:
        return [origin]
    key = (origin, destination)
    hit = _routes.get(key)
    if hit is not None and _now() - hit[0] < ROUTE_TTL:
        return hit[1]
    try:
        path = await _fetch_route(origin, destination)
    except Exception:
        logger.warning("hub comparison: route %s -> %s unavailable", origin, destination,
                       exc_info=True)
        return UNAVAILABLE
    if len(_routes) >= MAX_REMEMBERED:
        _routes.clear()
    _routes[key] = (_now(), path)
    return path


async def hub_routes(db: AsyncSession, origin: int,
                     hub_systems: dict[str, int]) -> dict[str, dict | None]:
    """{hub_key: {"jumps", "highsec"} | None | UNAVAILABLE}."""
    keys = list(hub_systems)
    paths = await asyncio.gather(*(_route(origin, hub_systems[k]) for k in keys))
    on_route = {s for p in paths if isinstance(p, list) for s in p}
    security: dict[int, float | None] = {}
    if on_route:
        rows = (await db.execute(
            select(SDESystem.system_id, SDESystem.security)
            .where(SDESystem.system_id.in_(on_route)))).all()
        security = {sid: sec for sid, sec in rows}
    out: dict[str, dict | None] = {}
    for key, path in zip(keys, paths):
        out[key] = describe(path, security) if isinstance(path, list) else path
    return out
