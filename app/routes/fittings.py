"""Fitting viewer — display saved in-game ship fittings."""

import asyncio
import logging
import re

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

# Legacy integer inventory flags — kept as a fallback. The live ESI fittings
# endpoint (per its OpenAPI spec) hands back the string enum below instead,
# but old cached data or a future ESI regression could still hand us ints.
SLOT_CATEGORY = {}
for f in range(11, 19): SLOT_CATEGORY[f] = "low"
for f in range(19, 27): SLOT_CATEGORY[f] = "med"
for f in range(27, 35): SLOT_CATEGORY[f] = "high"
for f in range(92, 95): SLOT_CATEGORY[f] = "rig"
for f in range(125, 129): SLOT_CATEGORY[f] = "subsystem"
SLOT_CATEGORY[87] = "drone"
SLOT_CATEGORY[5] = "cargo"
SLOT_CATEGORY[158] = "fighter"

# String flag → category. Exact matches for the bay-style flags, prefix
# matches for the numbered slot flags (HiSlot0-7, MedSlot0-7, ...) since the
# trailing digit is the slot index, not part of the category.
_STRING_FLAG_EXACT = {
    "Cargo": "cargo",
    "DroneBay": "drone",
    "FighterBay": "fighter",
    "Invalid": "cargo",
}
_STRING_FLAG_PREFIXES = [
    ("HiSlot", "high"),
    ("MedSlot", "med"),
    ("LoSlot", "low"),
    ("RigSlot", "rig"),
    ("ServiceSlot", "service"),
    ("SubSystemSlot", "subsystem"),
]


def _category_for_flag(flag) -> str:
    """Map a raw ESI item flag (string enum or legacy int) to a display group."""
    if isinstance(flag, str):
        if flag in _STRING_FLAG_EXACT:
            return _STRING_FLAG_EXACT[flag]
        for prefix, cat in _STRING_FLAG_PREFIXES:
            if flag.startswith(prefix):
                return cat
        return "cargo"  # unrecognized string flag
    return SLOT_CATEGORY.get(flag, "cargo")  # legacy integer flag, or unknown


_TRAILING_DIGITS = re.compile(r"(\d+)$")


def _slot_index(flag) -> int:
    """Numeric slot index used only to order items within one group.

    Both flag shapes already sort correctly on their own value within a
    single category (HiSlot0..7, or the legacy contiguous int ranges), so
    this just needs one consistent key across the two: the trailing digits
    of a string flag, or the int itself. Flags with no digit (Cargo,
    DroneBay, FighterBay, Invalid) sort first within their group.
    """
    if isinstance(flag, str):
        m = _TRAILING_DIGITS.search(flag)
        return int(m.group(1)) if m else 0
    if isinstance(flag, int):
        return flag
    return 0


SLOT_ORDER = {
    "high": 0, "med": 1, "low": 2, "rig": 3, "subsystem": 4, "service": 5,
    "drone": 6, "cargo": 7, "fighter": 8,
}
SLOT_LABELS = {
    "high": "High Slots", "med": "Mid Slots", "low": "Low Slots",
    "rig": "Rigs", "subsystem": "Subsystems", "service": "Service Slots",
    "drone": "Drones", "cargo": "Cargo", "fighter": "Fighters",
}

# Dogma attribute IDs for ship slot counts (12 lowSlots, 13 medSlots,
# 14 hiSlots, 1137 rigSlots, per ESI's /dogma/attributes/{id}/).
DGMA_HI = 14
DGMA_MED = 13
DGMA_LOW = 12
DGMA_RIG = 1137


def _parse_fitting(raw: dict, type_names: dict, ship_name: str, ship_slots: dict) -> dict:
    """Parse a raw ESI fitting into structured slot groups."""
    groups: dict[str, list] = {k: [] for k in SLOT_ORDER}

    for item in raw.get("items", []):
        flag = item.get("flag")
        cat = _category_for_flag(flag)
        name = type_names.get(item["type_id"], f"Type {item['type_id']}")
        groups[cat].append({
            "type_id": item["type_id"],
            "name": name,
            "quantity": item.get("quantity", 1),
            "flag": flag,
        })

    # Sort within each group by slot index (from the flag), then by name.
    # Works whether the fit's items carry the string flag enum, the legacy
    # int flags, or (in theory) a mix of the two across items.
    for cat in groups:
        groups[cat].sort(key=lambda x: (_slot_index(x.get("flag")), x["name"]))

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

    # Service modules only exist on structure fits — unlike the slot
    # categories above, skip the block (and its blank line) entirely when
    # there are none, so a plain ship's EFT text is unchanged from before
    # service slots existed.
    service_items = fit["groups"].get("service", [])
    if service_items:
        for item in service_items:
            lines.append(item["name"])
        lines.append("")

    # Drones, fighters and cargo carry stack quantities ("Name xN") rather
    # than one line per unit — the fitting tool's own EFT import expects the
    # same "xN" suffix for these bay-style groups. The import parser has no
    # dedicated fighter slot (it falls back to treating an unrecognized item
    # category as cargo), so a fighter line round-trips as a cargo item
    # rather than vanishing, which is what dropping it outright used to do.
    for cat in ["drone", "fighter", "cargo"]:
        items = fit["groups"].get(cat, [])
        if not items:
            continue
        for item in items:
            if item["quantity"] > 1:
                lines.append(f"{item['name']} x{item['quantity']}")
            else:
                lines.append(item["name"])
        lines.append("")

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

        # Fetch ship slot layouts (cap ESI fan-out — a user can have 30+ distinct hulls)
        _ship_sem = asyncio.Semaphore(5)

        async def _sem_ship_info(sid):
            async with _ship_sem:
                return await _get_ship_info(client, sid)

        ship_info_tasks = {sid: _sem_ship_info(sid) for sid in ship_type_ids}
        ship_results = await asyncio.gather(*ship_info_tasks.values())
        ship_data = dict(zip(ship_info_tasks.keys(), ship_results))

        # Local import avoids a circular dep between fittings.py and
        # fitting.py (the reverse direction — fitting.py importing from
        # fittings.py — already happens at call time a few lines below in
        # the character-import routes).
        from app.routes.fitting import _already_imported_lookup, _normalize_fit_name
        already_src, already_saved_pairs = await _already_imported_lookup(db, user_id, character_id)

        # Parse fittings
        fittings = []
        for raw in raw_fittings:
            sid = raw["ship_type_id"]
            ship_name, ship_slots = ship_data.get(sid, (type_names.get(sid, f"Ship {sid}"), {}))
            if not ship_name or ship_name.startswith("Ship "):
                ship_name = type_names.get(sid, ship_name)
            fit = _parse_fitting(raw, type_names, ship_name, ship_slots)
            # Compare against the normalized name the save endpoint actually
            # stores (stripped, "Unnamed" default) — _parse_fitting's own
            # "name" is the raw ESI value, unnormalized, and the two must
            # agree on what "matches" means or this flag and the POST's
            # skip rule could disagree on the same fit.
            already_imported = (
                fit["fitting_id"] in already_src
                or (sid, _normalize_fit_name(raw.get("name"))) in already_saved_pairs
            )
            fit["already_imported"] = already_imported
            fittings.append(fit)

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
