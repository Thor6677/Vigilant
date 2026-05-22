"""Intel → Kill Feed.

Live universe-wide kill feed from killmail.stream's _recent_kills buffer.
Filters: space class (HS/LS/NS/WH + sub-classes + Shattered modifier),
ship search, attacker entity search, victim entity search.

Click a row to expand the detail panel (victim + fitting + ISK + attackers).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.cache import cache_get, cache_set
from app.db.models import get_db
from app.intel.killmail_stream import _sys_meta_cache, get_recent_kills
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import search_ship_types, type_ids_to_names

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


def _apply_space_filter(
    kills: list[dict],
    spaces: set[str],
    wh_classes: set[str],
    shattered_only: bool,
) -> list[dict]:
    """Filter kills by space-class chips.

    `spaces`: short codes {hs, ls, ns, wh} — OR within set.
    `wh_classes`: {c1..c6, thera, drifter, pochven} — OR within set; AND'd
        with being in WH band.
    `shattered_only`: AND'd on top — requires the system's group_label to
        contain "shattered".
    All-empty = no constraint.

    Within-category OR / cross-category AND: e.g. {hs} + {c5} means
    "(band==hs) AND (band==wh AND first_token==c5)" — impossible, so empty.

    To disambiguate C1 vs C13: split group_label on whitespace and compare
    the first token equality (so "C13 (Shattered)" → "c13", not matched by
    substring "c1").
    """
    if not spaces and not wh_classes and not shattered_only:
        return kills
    out = []
    for k in kills:
        sid = k.get("solar_system_id") or 0
        meta = _sys_meta_cache.get(sid) or {}
        raw_band = meta.get("band") or "Unknown"
        band_norm = _BAND_NORMALIZE.get(raw_band, "unknown")
        group_label = meta.get("group_label") or ""
        first_token = group_label.split(" ")[0].lower() if group_label else ""
        is_wh = band_norm == "wh"

        ok = True
        if spaces:
            ok = ok and (band_norm in spaces)
        if wh_classes:
            ok = ok and is_wh and (first_token in wh_classes)
        if shattered_only:
            ok = ok and ("shattered" in group_label.lower())
        if ok:
            out.append(k)
    return out


@router.get("/intel/kills/feed", response_class=HTMLResponse)
async def intel_kills_feed(
    request: Request,
    since: int | None = None,
    space: str = "",
    wh_class: str = "",
    shattered: int = 0,
    ship_id: str = "",
    attacker_char: str = "",
    attacker_corp: str = "",
    attacker_alli: str = "",
    victim_char: str = "",
    victim_corp: str = "",
    victim_alli: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Live tail — reads _recent_kills in memory, renders the row partial.

    `since`: if provided, return only kills with killmail_id > since (for
    incremental htmx prepends). Otherwise return up to MAX_ROWS_INITIAL.
    `space`/`wh_class`: comma-separated short codes (e.g. "hs,ns" or
    "c5,c6"). `shattered=1` to require shattered systems only.
    `ship_id`: comma-separated SDE type IDs — OR-multiselect over
    victim.ship_type_id.
    `attacker_char`/`attacker_corp`/`attacker_alli`: comma-separated entity
    IDs — OR within group; AND across the three (any attacker on the kill
    must match at least one of the three groups). Cross-category with
    victim_* / ship_id / space is AND.
    `victim_char`/`victim_corp`/`victim_alli`: same shape, matched against
    the kill's victim.character_id/corporation_id/alliance_id.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    spaces = {s.strip() for s in space.split(",") if s.strip()}
    wh_classes = {c.strip() for c in wh_class.split(",") if c.strip()}
    shattered_only = bool(shattered)
    ship_ids = {int(s) for s in ship_id.split(",") if s.strip().isdigit()}

    def _ids(s: str) -> set[int]:
        return {int(x) for x in s.split(",") if x.strip().isdigit()}

    a_chars = _ids(attacker_char)
    a_corps = _ids(attacker_corp)
    a_allis = _ids(attacker_alli)
    v_chars = _ids(victim_char)
    v_corps = _ids(victim_corp)
    v_allis = _ids(victim_alli)

    kills = get_recent_kills()
    kills = sorted(kills, key=lambda k: k.get("killmail_id") or 0, reverse=True)

    if since:
        kills = [k for k in kills if (k.get("killmail_id") or 0) > since]

    kills = _apply_space_filter(kills, spaces, wh_classes, shattered_only)

    if ship_ids:
        kills = [
            k for k in kills
            if ((k.get("victim") or {}).get("ship_type_id") in ship_ids)
        ]

    if a_chars or a_corps or a_allis:
        kills = [
            k for k in kills
            if any(
                (a.get("character_id") in a_chars)
                or (a.get("corporation_id") in a_corps)
                or (a.get("alliance_id") in a_allis)
                for a in (k.get("attackers") or [])
            )
        ]

    if v_chars or v_corps or v_allis:
        kills = [
            k for k in kills
            if ((k.get("victim") or {}).get("character_id") in v_chars)
            or ((k.get("victim") or {}).get("corporation_id") in v_corps)
            or ((k.get("victim") or {}).get("alliance_id") in v_allis)
        ]

    if not since:
        kills = kills[:MAX_ROWS_INITIAL]

    if not kills:
        return HTMLResponse("")

    enriched = await _enrich_kills(kills, db)
    # total_in_buffer reflects the raw count (unfiltered) so the user sees
    # how much is being hidden by the active filter.
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


@router.get("/intel/kills/resolve")
async def intel_kills_resolve(
    request: Request,
    q: str = "",
    kind: str = "ship",
    db: AsyncSession = Depends(get_db),
):
    """Autocomplete proxy for kill-feed filters.

    - `kind=ship`: local SDE substring match against published ships
      (categoryID=6). Returns up to 8 `{id, name, kind}` rows.
    - `kind=entity`: ESI /universe/ids autocomplete. Returns up to 5 of
      each kind (character / corporation / alliance) labeled `kind`.
      Cached 24h via the shared ESI cache (TTL keyed on
      `/intel_kills/resolve_entity` in `_ttl_for_path`).

    Requires auth; returns [] for unknown kinds.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse([], status_code=401)
    q = (q or "").strip()
    if not q or len(q) < 2:
        return JSONResponse([])
    if kind == "ship":
        return JSONResponse(await search_ship_types(db, q))
    if kind == "entity":
        return JSONResponse(await _resolve_entity(q))
    return JSONResponse([])


async def _resolve_entity(q: str) -> list[dict]:
    """ESI `/universe/ids/` autocomplete, cached 24h.

    Returns up to 5 entries from each of {character, corporation, alliance}
    with a `kind` label so the client can disambiguate same-numeric IDs
    across kinds.
    """
    cache_path = f"/intel_kills/resolve_entity/{q.lower()}"
    cached = await cache_get(None, cache_path)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://esi.evetech.net/latest/universe/ids/",
                json=[q],
                headers={"User-Agent": "Vigilant/1.0 (happyfun.fatman@gmail.com)"},
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
    except Exception as e:
        log.warning("intel_kills: resolve entity failed for %r: %s", q, e)
        return []

    out: list[dict] = []
    for kind_key, list_key in (
        ("character", "characters"),
        ("corporation", "corporations"),
        ("alliance", "alliances"),
    ):
        for item in (data.get(list_key) or [])[:5]:
            if item.get("id") and item.get("name"):
                out.append({"id": item["id"], "name": item["name"], "kind": kind_key})

    await cache_set(None, cache_path, out)
    return out
