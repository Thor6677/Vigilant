"""Intel → Kill Feed.

Live universe-wide kill feed from killmail.stream's _recent_kills buffer.
Filters: space class (HS/LS/NS/WH + sub-classes + Shattered modifier),
ship search, attacker entity search, victim entity search.

Click a row to expand the detail panel (victim + fitting + ISK + attackers).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
from app.intel.killmail_stream import _sys_meta_cache, get_recent_kills
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import type_ids_to_names

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_ROWS_INITIAL = 100

# Normalize sec_band's "Highsec"/"Lowsec"/"Nullsec"/"Unknown" return values
# (plus the "w-space" literal set by _resolve_sys_meta) to short codes for
# consistent CSS class names and filter comparisons (used in Task 6 too).
_BAND_NORMALIZE = {
    "Highsec": "hs",
    "Lowsec": "ls",
    "Nullsec": "ns",
    "Unknown": "unknown",
    "w-space": "wh",
}


@router.get("/intel/kills", response_class=HTMLResponse)
async def intel_kills_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Page shell. The feed content is loaded via htmx into the container."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(
        "intel_kills.html",
        {"request": request},
    )


@router.get("/intel/kills/feed", response_class=HTMLResponse)
async def intel_kills_feed(
    request: Request,
    since: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Live tail — reads _recent_kills in memory, renders the row partial.

    `since`: if provided, return only kills with killmail_id > since (for
    incremental htmx prepends). Otherwise return up to MAX_ROWS_INITIAL.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    kills = get_recent_kills()
    kills = sorted(kills, key=lambda k: k.get("killmail_id") or 0, reverse=True)

    if since:
        kills = [k for k in kills if (k.get("killmail_id") or 0) > since]
    else:
        kills = kills[:MAX_ROWS_INITIAL]

    if not kills:
        return HTMLResponse("")

    enriched = await _enrich_kills(kills, db)
    total_in_buffer = len(get_recent_kills())

    return templates.TemplateResponse(
        "partials/intel_kills_feed.html",
        {
            "request": request,
            "kills": enriched,
            "total_in_buffer": total_in_buffer,
            "newest_id": enriched[0]["killmail_id"] if enriched else (since or 0),
        },
    )


async def _resolve_for_feed(
    db: AsyncSession, type_ids: set[int], entity_ids: set[int]
) -> dict[int, str]:
    """Combine SDE ship/type names (local) with ESI char/corp/alliance names
    (cached via resolve_entity_names). Returns one merged {id: name} map.

    Splitting avoids paying an ESI round trip for ship type names that already
    live in our SDE mirror, and avoids confusing the ESI resolver with type IDs
    (different ID namespace, would just negative-cache them)."""
    type_ids = {i for i in type_ids if i}
    entity_ids = {i for i in entity_ids if i}
    out: dict[int, str] = {}
    if type_ids:
        try:
            out.update(await type_ids_to_names(db, list(type_ids)))
        except Exception as e:
            log.debug("intel_kills: type name resolve failed: %s", e)
    if entity_ids:
        try:
            out.update(await resolve_entity_names(list(entity_ids)))
        except Exception as e:
            log.debug("intel_kills: entity name resolve failed: %s", e)
    return out


async def _enrich_kills(kills: list[dict], db: AsyncSession) -> list[dict]:
    """Resolve names + sec band for a batch of kill records from _recent_kills."""
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    for k in kills:
        v = k.get("victim") or {}
        if v.get("ship_type_id"):
            type_ids.add(v["ship_type_id"])
        for key in ("character_id", "corporation_id", "alliance_id"):
            if v.get(key):
                entity_ids.add(v[key])
        attackers = k.get("attackers") or []
        top = next(
            (a for a in attackers if a.get("final_blow")),
            attackers[0] if attackers else {},
        )
        for key in ("character_id", "corporation_id"):
            if top.get(key):
                entity_ids.add(top[key])

    name_map = await _resolve_for_feed(db, type_ids, entity_ids)

    out = []
    for k in kills:
        v = k.get("victim") or {}
        attackers = k.get("attackers") or []
        top = next(
            (a for a in attackers if a.get("final_blow")),
            attackers[0] if attackers else {},
        )
        sid = k.get("solar_system_id") or 0
        meta = _sys_meta_cache.get(sid) or {}
        raw_band = meta.get("band") or "Unknown"
        out.append({
            "killmail_id": k.get("killmail_id"),
            "killmail_time": k.get("killmail_time"),
            "system_name": meta.get("system_name") or f"#{sid}",
            "system_band": _BAND_NORMALIZE.get(raw_band, "unknown"),
            "system_class_label": meta.get("group_label"),
            "victim_pilot": name_map.get(v.get("character_id"), "?"),
            "victim_corp": name_map.get(v.get("corporation_id"), ""),
            "victim_ship": name_map.get(v.get("ship_type_id"), "?"),
            "victim_ship_type_id": v.get("ship_type_id"),
            "top_attacker_pilot": name_map.get(top.get("character_id"), "?"),
            "top_attacker_corp": name_map.get(top.get("corporation_id"), ""),
            "gang_size": len(attackers),
            "isk": float((k.get("zkb") or {}).get("totalValue") or 0),
        })
    return out
