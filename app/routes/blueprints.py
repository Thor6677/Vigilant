"""Blueprint library — character and corporation blueprints with ME/TE display."""

import asyncio
import logging

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, AsyncSessionLocal
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.routes.corporations import _try_api_call_with_fallback
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["blueprints"])
templates = Jinja2Templates(directory="app/templates")

# Corp hangar flag labels
LOCATION_FLAGS = {
    "Hangar": "Personal Hangar",
    "CorpSAG1": "Corp Hangar 1",
    "CorpSAG2": "Corp Hangar 2",
    "CorpSAG3": "Corp Hangar 3",
    "CorpSAG4": "Corp Hangar 4",
    "CorpSAG5": "Corp Hangar 5",
    "CorpSAG6": "Corp Hangar 6",
    "CorpSAG7": "Corp Hangar 7",
    "Deliveries": "Deliveries",
    "CorpDeliveries": "Corp Deliveries",
    "Unlocked": "Unlocked",
    "AutoFit": "AutoFit",
}


async def _fetch_all_pages(fetch_fn, *args, max_pages=10) -> list:
    """Fetch multiple pages from a paginated ESI endpoint."""
    all_items = []
    for page in range(1, max_pages + 1):
        data = await fetch_fn(*args, page=page)
        if not data or not isinstance(data, list):
            break
        all_items.extend(data)
        if len(data) < 1000:
            break
    return all_items


def _process_blueprints(raw: list, type_names: dict) -> list[dict]:
    """Parse raw ESI blueprint entries into enriched dicts."""
    result = []
    for bp in raw:
        qty = bp.get("quantity", 0)
        is_bpo = qty == -1
        is_bpc = qty == -2
        me = bp.get("material_efficiency", 0)
        te = bp.get("time_efficiency", 0)
        runs = bp.get("runs", -1)
        tid = bp.get("type_id")
        name = type_names.get(tid, f"Blueprint {tid}")

        result.append({
            "type_id": tid,
            "item_id": bp.get("item_id"),
            "name": name,
            "is_bpo": is_bpo,
            "is_bpc": is_bpc,
            "label": "BPO" if is_bpo else "BPC" if is_bpc else "?",
            "me": me,
            "te": te,
            "runs": runs if is_bpc else None,
            "location_id": bp.get("location_id"),
            "location_flag": bp.get("location_flag", ""),
            "location_label": LOCATION_FLAGS.get(bp.get("location_flag", ""), bp.get("location_flag", "")),
            "me_maxed": me == 10,
            "te_maxed": te == 20,
        })

    result.sort(key=lambda x: (0 if x["is_bpo"] else 1, x["name"]))
    return result


def _group_blueprints(bps: list, by: str = "type") -> dict:
    """Group blueprints by type or location."""
    groups: dict[str, list] = {}
    for bp in bps:
        if by == "location":
            key = bp["location_label"] or "Unknown"
        else:
            key = "Originals (BPO)" if bp["is_bpo"] else "Copies (BPC)" if bp["is_bpc"] else "Other"
        groups.setdefault(key, []).append(bp)
    return groups


def _compute_stats(bps: list) -> dict:
    bpo_count = sum(1 for b in bps if b["is_bpo"])
    bpc_count = sum(1 for b in bps if b["is_bpc"])
    researched = sum(1 for b in bps if b["is_bpo"] and (b["me"] > 0 or b["te"] > 0))
    maxed = sum(1 for b in bps if b["is_bpo"] and b["me_maxed"] and b["te_maxed"])
    return {
        "total": len(bps),
        "bpo": bpo_count,
        "bpc": bpc_count,
        "researched": researched,
        "maxed": maxed,
    }


@router.get("/character/{character_id}/blueprints", response_class=HTMLResponse)
async def character_blueprints(
    request: Request,
    character_id: int,
    filter: str = Query("all"),
    group_by: str = Query("type"),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard")

    char_info = {
        "character_id": char.character_id,
        "character_name": char.character_name,
        "corporation_id": char.corporation_id,
        "corporation_name": char.corporation_name,
    }

    scope = "esi-characters.read_blueprints.v1"
    if scope not in (char.scopes or ""):
        return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": [], "groups": {},
            "stats": _compute_stats([]),
            "error": "Blueprints scope not available — re-authorize this character.",
            "is_corp": False, "corp_id": None, "filter": filter, "group_by": group_by})

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        raw = await _fetch_all_pages(esi_char.get_blueprints, client, character_id)

        all_type_ids = list({bp["type_id"] for bp in raw})
        type_names = await sde.type_ids_to_names(db, all_type_ids)

        blueprints = _process_blueprints(raw, type_names)

        # Apply filter
        if filter == "bpo":
            blueprints = [b for b in blueprints if b["is_bpo"]]
        elif filter == "bpc":
            blueprints = [b for b in blueprints if b["is_bpc"]]
        elif filter == "unresearched":
            blueprints = [b for b in blueprints if b["is_bpo"] and b["me"] == 0 and b["te"] == 0]

        stats = _compute_stats(_process_blueprints(raw, type_names))
        groups = _group_blueprints(blueprints, group_by)

    except Exception as exc:
        logger.warning("Blueprints fetch failed for char %s: %s", character_id, exc, exc_info=True)
        return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": [], "groups": {},
            "stats": _compute_stats([]),
            "error": f"Failed to load blueprints: {type(exc).__name__}",
            "is_corp": False, "corp_id": None, "filter": filter, "group_by": group_by})

    return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": blueprints,
        "groups": groups, "stats": stats, "error": None,
        "is_corp": False, "corp_id": None, "filter": filter, "group_by": group_by})


@router.get("/corporations/{corp_id}/blueprints", response_class=HTMLResponse)
async def corp_blueprints(
    request: Request,
    corp_id: int,
    filter: str = Query("all"),
    group_by: str = Query("type"),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())

    scope = "esi-corporations.read_blueprints.v1"
    corp_chars = [c for c in characters if c.corporation_id == corp_id and scope in (c.scopes or "")]

    char_info = {
        "character_id": corp_chars[0].character_id if corp_chars else None,
        "character_name": corp_chars[0].character_name if corp_chars else None,
        "corporation_name": corp_chars[0].corporation_name if corp_chars else None,
    }

    if not corp_chars:
        return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": [], "groups": {},
            "stats": _compute_stats([]),
            "error": "No character with corp blueprint access. Requires Director role and re-authorization.",
            "is_corp": True, "corp_id": corp_id, "filter": filter, "group_by": group_by})

    try:
        scope_chars = {"blueprints": corp_chars}

        async def _fetch_bp(client, cid):
            return await _fetch_all_pages(esi_corp.get_corporation_blueprints, client, cid)

        raw, bp_error = await _try_api_call_with_fallback("blueprints", scope_chars, _fetch_bp, corp_id, db)
        if raw is None:
            raise Exception(bp_error or "All characters returned 403 — Director role required in-game.")

        all_type_ids = list({bp["type_id"] for bp in raw})
        type_names = await sde.type_ids_to_names(db, all_type_ids)

        blueprints = _process_blueprints(raw, type_names)

        if filter == "bpo":
            blueprints = [b for b in blueprints if b["is_bpo"]]
        elif filter == "bpc":
            blueprints = [b for b in blueprints if b["is_bpc"]]
        elif filter == "unresearched":
            blueprints = [b for b in blueprints if b["is_bpo"] and b["me"] == 0 and b["te"] == 0]

        stats = _compute_stats(_process_blueprints(raw, type_names))
        groups = _group_blueprints(blueprints, group_by)

    except Exception as exc:
        logger.warning("Corp blueprints fetch failed for corp %s: %s", corp_id, exc, exc_info=True)
        return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": [], "groups": {},
            "stats": _compute_stats([]),
            "error": f"Failed to load corp blueprints: {type(exc).__name__}",
            "is_corp": True, "corp_id": corp_id, "filter": filter, "group_by": group_by})

    return templates.TemplateResponse(request, "blueprints.html", {"char": char_info, "blueprints": blueprints,
        "groups": groups, "stats": stats, "error": None,
        "is_corp": True, "corp_id": corp_id, "filter": filter, "group_by": group_by})
