"""Fitting viewer — display saved in-game ship fittings."""

import asyncio
import logging

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import universe as esi_universe
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fittings"])
templates = Jinja2Templates(directory="app/templates")

# ── Slot flag mapping ─────────────────────────────────────────────────────────

SLOT_CATEGORY = {}
for f in range(11, 19): SLOT_CATEGORY[f] = "low"
for f in range(19, 27): SLOT_CATEGORY[f] = "med"
for f in range(27, 35): SLOT_CATEGORY[f] = "high"
for f in range(92, 95): SLOT_CATEGORY[f] = "rig"
for f in range(125, 129): SLOT_CATEGORY[f] = "subsystem"
SLOT_CATEGORY[87] = "drone"
SLOT_CATEGORY[5] = "cargo"
SLOT_CATEGORY[158] = "fighter"

SLOT_ORDER = {"high": 0, "med": 1, "low": 2, "rig": 3, "subsystem": 4, "drone": 5, "cargo": 6, "fighter": 7}
SLOT_LABELS = {
    "high": "High Slots", "med": "Mid Slots", "low": "Low Slots",
    "rig": "Rigs", "subsystem": "Subsystems", "drone": "Drones",
    "cargo": "Cargo", "fighter": "Fighters",
}

# Dogma attribute IDs for ship slot counts
DGMA_HI = 12
DGMA_MED = 13
DGMA_LOW = 14
DGMA_RIG = 1137


def _parse_fitting(raw: dict, type_names: dict, ship_name: str, ship_slots: dict) -> dict:
    """Parse a raw ESI fitting into structured slot groups."""
    groups: dict[str, list] = {k: [] for k in SLOT_ORDER}

    for item in raw.get("items", []):
        cat = SLOT_CATEGORY.get(item.get("flag"), "cargo")
        name = type_names.get(item["type_id"], f"Type {item['type_id']}")
        groups[cat].append({
            "type_id": item["type_id"],
            "name": name,
            "quantity": item.get("quantity", 1),
            "flag": item.get("flag"),
        })

    # Sort within each group by flag
    for cat in groups:
        groups[cat].sort(key=lambda x: (x.get("flag") or 0, x["name"]))

    return {
        "fitting_id": raw.get("fitting_id"),
        "name": raw.get("name", "Unnamed"),
        "description": raw.get("description", ""),
        "ship_type_id": raw.get("ship_type_id"),
        "ship_name": ship_name,
        "groups": groups,
        "ship_slots": ship_slots,
        "total_modules": sum(len(v) for k, v in groups.items() if k not in ("drone", "cargo", "fighter")),
    }


def _to_eft(fit: dict) -> str:
    """Convert a parsed fitting to EFT text format."""
    lines = [f"[{fit['ship_name']}, {fit['name']}]"]

    for cat in ["low", "med", "high", "rig", "subsystem"]:
        items = fit["groups"].get(cat, [])
        for item in items:
            lines.append(item["name"])
        lines.append("")  # blank line between groups

    drones = fit["groups"].get("drone", [])
    if drones:
        for item in drones:
            if item["quantity"] > 1:
                lines.append(f"{item['name']} x{item['quantity']}")
            else:
                lines.append(item["name"])
        lines.append("")

    cargo = fit["groups"].get("cargo", [])
    if cargo:
        for item in cargo:
            if item["quantity"] > 1:
                lines.append(f"{item['name']} x{item['quantity']}")
            else:
                lines.append(item["name"])

    return "\n".join(lines).rstrip()


async def _get_ship_info(client: ESIClient, ship_type_id: int) -> tuple[str, dict]:
    """Get ship name and slot layout from ESI type endpoint."""
    try:
        data = await esi_universe.get_type(client, ship_type_id)
        name = data.get("name", f"Ship {ship_type_id}")
        attrs = {a["attribute_id"]: a["value"] for a in (data.get("dogma_attributes") or [])}
        slots = {
            "high": int(attrs.get(DGMA_HI, 0)),
            "med": int(attrs.get(DGMA_MED, 0)),
            "low": int(attrs.get(DGMA_LOW, 0)),
            "rig": int(attrs.get(DGMA_RIG, 0)),
        }
        return name, slots
    except Exception:
        return f"Ship {ship_type_id}", {"high": 0, "med": 0, "low": 0, "rig": 0}


@router.get("/character/{character_id}/fittings", response_class=HTMLResponse)
async def fittings_list(
    request: Request,
    character_id: int,
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
    }

    scope = "esi-fittings.read_fittings.v1"
    if scope not in (char.scopes or ""):
        return templates.TemplateResponse(request, "fittings.html", {"char": char_info, "fittings": [],
            "error": "Fittings scope not available — re-authorize this character to grant the fittings permission.",
            "ship_groups": {}})

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        raw_fittings = await esi_char.get_fittings(client, character_id)
        if not raw_fittings:
            return templates.TemplateResponse(request, "fittings.html", {"char": char_info, "fittings": [],
                "error": None, "ship_groups": {}})

        # Collect all type IDs (ships + modules)
        all_type_ids = set()
        ship_type_ids = set()
        for f in raw_fittings:
            ship_type_ids.add(f["ship_type_id"])
            all_type_ids.add(f["ship_type_id"])
            for item in f.get("items", []):
                all_type_ids.add(item["type_id"])

        # Resolve names from SDE + ship info from ESI (parallel)
        type_names = await sde.type_ids_to_names(db, list(all_type_ids))

        # Fetch ship slot layouts
        ship_info_tasks = {sid: _get_ship_info(client, sid) for sid in ship_type_ids}
        ship_results = await asyncio.gather(*ship_info_tasks.values())
        ship_data = dict(zip(ship_info_tasks.keys(), ship_results))

        # Parse fittings
        fittings = []
        for raw in raw_fittings:
            sid = raw["ship_type_id"]
            ship_name, ship_slots = ship_data.get(sid, (type_names.get(sid, f"Ship {sid}"), {}))
            if not ship_name or ship_name.startswith("Ship "):
                ship_name = type_names.get(sid, ship_name)
            fittings.append(_parse_fitting(raw, type_names, ship_name, ship_slots))

        # Group by ship name
        ship_groups: dict[str, list] = {}
        for fit in sorted(fittings, key=lambda f: (f["ship_name"], f["name"])):
            ship_groups.setdefault(fit["ship_name"], []).append(fit)

    except Exception as exc:
        logger.warning("Fittings fetch failed for char %s: %s", character_id, exc, exc_info=True)
        return templates.TemplateResponse(request, "fittings.html", {"char": char_info, "fittings": [],
            "error": f"Failed to load fittings: {type(exc).__name__}",
            "ship_groups": {}})

    return templates.TemplateResponse(request, "fittings.html", {"char": char_info,
        "fittings": fittings,
        "ship_groups": ship_groups,
        "error": None,
        "slot_labels": SLOT_LABELS})


@router.get("/character/{character_id}/fittings/{fitting_id}/eft", response_class=PlainTextResponse)
async def fitting_eft(
    request: Request,
    character_id: int,
    fitting_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return a single fitting in EFT format (for clipboard copy)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return PlainTextResponse("")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return PlainTextResponse("")

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        raw_fittings = await esi_char.get_fittings(client, character_id)
        raw = next((f for f in raw_fittings if f.get("fitting_id") == fitting_id), None)
        if not raw:
            return PlainTextResponse("Fitting not found")

        all_ids = {raw["ship_type_id"]} | {i["type_id"] for i in raw.get("items", [])}
        type_names = await sde.type_ids_to_names(db, list(all_ids))
        ship_name = type_names.get(raw["ship_type_id"], f"Ship {raw['ship_type_id']}")
        fit = _parse_fitting(raw, type_names, ship_name, {})
        return PlainTextResponse(_to_eft(fit))
    except Exception:
        return PlainTextResponse("Error generating EFT")
