import html as html_module
import json
import re
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache, CharacterAssetCache
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])
templates = Jinja2Templates(directory="app/templates")


def _camel_to_space(s: str) -> str:
    return re.sub(r'([A-Z])', r' \1', s or '').strip()


templates.env.filters["camel_to_space"] = _camel_to_space


@router.get("", response_class=HTMLResponse)
async def assets_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    active_id = request.session.get("active_character_id")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())
    character_ids = [c.character_id for c in characters]

    # Load location cache for dropdown display
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(character_ids))
    )
    caches = {c.character_id: c for c in cache_result.scalars().all()}

    chars_with_loc = []
    for char in sorted(characters, key=lambda c: (c.sort_order, c.character_name)):
        cache = caches.get(char.character_id)
        loc = json.loads(cache.location_json) if cache and cache.location_json else None
        chars_with_loc.append({
            "character_id": char.character_id,
            "character_name": char.character_name,
            "system_name": loc.get("system_name") if loc else None,
        })

    return templates.TemplateResponse(request, "assets.html", {"characters": chars_with_loc,
        "active_char_id": active_id})


@router.get("/search", response_class=HTMLResponse)
async def assets_search(
    request: Request,
    q: str = "",
    from_char: str = "",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")
    active_id = request.session.get("active_character_id")

    if len(q.strip()) < 2:
        return HTMLResponse('<div class="b-empty">Enter at least 2 characters to search...</div>')

    # Load characters
    chars_result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = {c.character_id: c for c in chars_result.scalars().all()}
    character_ids = list(characters.keys())

    # Load asset caches
    asset_caches_result = await db.execute(
        select(CharacterAssetCache).where(CharacterAssetCache.character_id.in_(character_ids))
    )
    asset_caches = {c.character_id: c for c in asset_caches_result.scalars().all()}

    # Determine reference character for jump distance
    try:
        from_char_id = int(from_char) if from_char else active_id
    except (ValueError, TypeError):
        from_char_id = active_id

    if from_char_id not in characters:
        from_char_id = active_id

    origin_system_id = None
    loc_cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == from_char_id)
    )
    loc_cache = loc_cache_result.scalar_one_or_none()
    if loc_cache and loc_cache.location_json:
        try:
            loc_data = json.loads(loc_cache.location_json)
            origin_system_id = loc_data.get("system_id")
        except Exception:
            pass

    # Filter assets across all characters
    q_lower = q.strip().lower()
    char_matches: dict[int, list] = {}
    unique_system_ids: set[int] = set()

    for cid in characters:
        ac = asset_caches.get(cid)
        if not ac or not ac.assets_json:
            continue
        try:
            assets = json.loads(ac.assets_json)
        except Exception:
            continue
        matches = [
            a for a in assets
            if q_lower in (a.get("type_name") or "").lower()
        ]
        if matches:
            char_matches[cid] = matches
            for a in matches:
                sid = a.get("system_id")
                if sid:
                    unique_system_ids.add(sid)

    # Compute jump distances in one BFS pass from origin
    all_distances: dict[int, int] = {}
    if origin_system_id and unique_system_ids:
        all_distances = await sde.jump_distances_from(db, origin_system_id)

    # Group results by account_group → character
    groups: dict[str, dict] = {}
    for cid, matches in char_matches.items():
        char = characters.get(cid)
        if not char:
            continue
        group = char.account_group or "Ungrouped"
        if group not in groups:
            groups[group] = {}

        enriched = []
        for a in sorted(matches, key=lambda x: (x.get("type_name") or "", -(x.get("quantity") or 0))):
            sid = a.get("system_id")
            if not origin_system_id:
                jump_dist = None
                has_origin = False
            elif sid is None:
                jump_dist = None
                has_origin = True
            elif sid == origin_system_id:
                jump_dist = 0
                has_origin = True
            else:
                jump_dist = all_distances.get(sid)  # None if unreachable (wormhole)
                has_origin = True
            enriched.append({**a, "jump_dist": jump_dist, "has_origin": has_origin})

        groups[group][cid] = {"char": char, "assets": enriched}

    # Sort groups: alphabetical, "Ungrouped" last
    sorted_group_names = sorted(groups.keys(), key=lambda g: (g == "Ungrouped", g.lower()))

    # Within each group, sort characters by sort_order
    for group_name in sorted_group_names:
        groups[group_name] = dict(
            sorted(groups[group_name].items(), key=lambda kv: kv[1]["char"].sort_order)
        )

    if not groups:
        return HTMLResponse(f'<div class="b-empty">No results for &quot;{html_module.escape(q)}&quot;</div>')

    return templates.TemplateResponse(request, "partials/assets_results.html", {"groups": groups,
        "sorted_group_names": sorted_group_names})
