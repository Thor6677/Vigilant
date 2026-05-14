"""Ship fitting tool — build and analyze ship fittings locally."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, UserFitting, UserFittingFolder, Character
from app.sde import lookup as sde
from app.fitting.engine import calculate_fitting_stats, get_type_dogma_attrs
from app.fitting.constants import ATTR_CPU, ATTR_POWER, ATTR_UPGRADE_COST, ATTR_DRONE_BW_USED
from app.db.sde_models import (
    SDEModuleSlot, SDEType, SDEGroup, SDETypeDogmaAttribute, SDEDogmaAttribute,
    SDETypeSkillReq,
)
from app.esi.client import ESIClient, refresh_token
from app.esi import universe as esi_universe
from app.esi import character as esi_char
from app.esi import market as esi_market
from app.db.models import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fitting"])
templates = Jinja2Templates(directory="app/templates")


def _folder_path_map(folders: list[dict]) -> dict[int, str]:
    """Flatten folders into id→'A / B / C' path labels for pickers."""
    by_id = {f["id"]: f for f in folders}
    out: dict[int, str] = {}
    for f in folders:
        parts = []
        cur = f
        while cur is not None:
            parts.append(cur["name"])
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        out[f["id"]] = " / ".join(reversed(parts))
    return out


@router.get("/tools/fitting", response_class=HTMLResponse)
async def fitting_tool(request: Request, db: AsyncSession = Depends(get_db)):
    """Fitting builder. The saved-fits list moved to /tools/fitting/saved;
    the builder now only needs the flat folder list for its 'Save to folder'
    picker."""
    user_id = request.session.get("user_id")
    folders: list[dict] = []
    if user_id:
        folder_rows = await db.execute(
            select(UserFittingFolder)
            .where(UserFittingFolder.user_id == user_id)
            .order_by(UserFittingFolder.name)
        )
        folders = [
            {"id": f.id, "parent_id": f.parent_id, "name": f.name}
            for f in folder_rows.scalars().all()
        ]
    path_map = _folder_path_map(folders)
    folder_paths = sorted(
        [{"id": fid, "path": p} for fid, p in path_map.items()],
        key=lambda x: x["path"].lower(),
    )
    return templates.TemplateResponse(request, "fitting_tool.html", {"folder_paths": folder_paths})


@router.get("/tools/fitting/saved", response_class=HTMLResponse)
async def saved_fittings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Dedicated list view for saved fittings.

    Computes DPS and cost per fit at request time, in parallel.  Prices
    are pulled from the global /markets/prices/ endpoint (a single ESI
    call batched across all type_ids in the table).  Clicking a row
    navigates to /tools/fitting?load=<id> which the builder reads on
    init.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # --- Folders + fits -----------------------------------------------------
    folder_rows = await db.execute(
        select(UserFittingFolder)
        .where(UserFittingFolder.user_id == user_id)
        .order_by(UserFittingFolder.name)
    )
    folders = [
        {"id": f.id, "parent_id": f.parent_id, "name": f.name}
        for f in folder_rows.scalars().all()
    ]
    path_map = _folder_path_map(folders)

    fit_rows = await db.execute(
        select(UserFitting)
        .where(UserFitting.user_id == user_id)
        .order_by(UserFitting.updated_at.desc())
    )
    fits = list(fit_rows.scalars().all())

    # --- Gather all type_ids for name + price resolution -------------------
    items_by_fit: dict[int, list[dict]] = {}
    all_type_ids: set[int] = set()
    for f in fits:
        try:
            items = json.loads(f.items_json) if f.items_json else []
        except Exception:
            items = []
        items_by_fit[f.id] = items
        if f.ship_type_id:
            all_type_ids.add(f.ship_type_id)
        for item in items:
            tid = item.get("type_id")
            if tid:
                all_type_ids.add(int(tid))
            cid = item.get("charge_type_id")
            if cid:
                all_type_ids.add(int(cid))

    ship_names = await sde.type_ids_to_names(db, [f.ship_type_id for f in fits if f.ship_type_id]) if fits else {}

    # --- Prices + DPS (parallel) -------------------------------------------
    async def _dps_for(f: UserFitting) -> tuple[int, float]:
        items = items_by_fit.get(f.id, [])
        if not items:
            return f.id, 0.0
        try:
            async with AsyncSessionLocal() as fdb:
                stats = await calculate_fitting_stats(fdb, f.ship_type_id, items)
            return f.id, float(stats.get("total_dps") or 0.0)
        except Exception as e:
            logger.info("DPS calc failed for fit %s: %s", f.id, e)
            return f.id, 0.0

    async def _price_map() -> dict[int, float]:
        if not all_type_ids:
            return {}
        try:
            client = ESIClient("", db=db)
            prices = await esi_market.get_market_prices(client)
            m: dict[int, float] = {}
            for p in prices or []:
                tid = p.get("type_id")
                if tid in all_type_ids:
                    m[tid] = float(p.get("average_price") or p.get("adjusted_price") or 0)
            return m
        except Exception as e:
            logger.info("market price fetch failed: %s", e)
            return {}

    dps_task = asyncio.gather(*[_dps_for(f) for f in fits], return_exceptions=True)
    price_task = _price_map()
    dps_results, price_map = await asyncio.gather(dps_task, price_task)

    dps_by_fit: dict[int, float] = {}
    for r in dps_results:
        if isinstance(r, Exception):
            continue
        fid, dps = r
        dps_by_fit[fid] = dps

    # --- Compose rows ------------------------------------------------------
    rows = []
    for f in fits:
        items = items_by_fit.get(f.id, [])
        cost = price_map.get(f.ship_type_id, 0.0)
        for item in items:
            qty = item.get("quantity", 1) or 1
            tid = item.get("type_id")
            if tid:
                cost += price_map.get(int(tid), 0.0) * qty
            cid = item.get("charge_type_id")
            if cid:
                cost += price_map.get(int(cid), 0.0)
        rows.append({
            "id": f.id,
            "name": f.name,
            "ship_type_id": f.ship_type_id,
            "ship_name": ship_names.get(f.ship_type_id, f"Type {f.ship_type_id}"),
            "folder_id": f.folder_id,
            "folder_path": path_map.get(f.folder_id) if f.folder_id else "",
            "dps": round(dps_by_fit.get(f.id, 0.0), 1),
            "cost": round(cost, 2),
            "updated_at": f.updated_at,
        })

    # Sort by folder path then name for predictability
    rows.sort(key=lambda r: (r["folder_path"].lower(), r["name"].lower()))

    folder_paths = sorted(
        [{"id": fid, "path": p} for fid, p in path_map.items()],
        key=lambda x: x["path"].lower(),
    )

    return templates.TemplateResponse(request, "fitting_saved.html", {"rows": rows,
        "folders": folders,
        "folder_paths": folder_paths,
        "total": len(rows)})


@router.get("/tools/fitting/search/ships", response_class=HTMLResponse)
async def search_ships(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_ships(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "ship"})


@router.get("/tools/fitting/search/modules", response_class=HTMLResponse)
async def search_modules(
    request: Request,
    q: str = Query("", min_length=2),
    slot: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    slot_filter = slot if slot in ("high", "mid", "low", "rig", "subsystem") else None
    results = await sde.search_modules(db, q, slot_type=slot_filter, limit=20)

    # Attach slot type and fitting info to each result
    for r in results:
        slot_result = await db.execute(
            select(SDEModuleSlot.slot_type, SDEModuleSlot.is_turret, SDEModuleSlot.is_launcher)
            .where(SDEModuleSlot.type_id == r["type_id"])
        )
        slot_row = slot_result.fetchone()
        r["slot_type"] = slot_row.slot_type if slot_row else "unknown"
        r["is_turret"] = slot_row.is_turret if slot_row else False
        r["is_launcher"] = slot_row.is_launcher if slot_row else False

    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "module"})


@router.get("/tools/fitting/search/drones", response_class=HTMLResponse)
async def search_drones(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_drones(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "drone"})


@router.get("/tools/fitting/search/charges", response_class=HTMLResponse)
async def search_charges(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_charges(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "charge"})


@router.post("/tools/fitting/stats", response_class=HTMLResponse)
async def fitting_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Calculate and return fitting stats as an HTML partial."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    ship_type_id = body.get("ship_type_id")
    if not ship_type_id:
        return HTMLResponse("<div class='b-empty'>Select a ship to see stats</div>")

    items = body.get("items", [])
    damage_profile = body.get("damage_profile", "uniform")

    # Optional: scale by a specific character's trained skills instead of All V.
    user_id = request.session.get("user_id")
    character_id = body.get("character_id")
    skill_levels: dict[int, int] | None = None
    character_name: str | None = None
    if character_id and user_id:
        r = await db.execute(
            select(Character)
            .where(Character.character_id == int(character_id))
            .where(Character.user_id == user_id)
        )
        char = r.scalar_one_or_none()
        if char and _SKILLS_SCOPE in (char.scopes or ""):
            try:
                skill_levels = await _character_skills_map(db, char)
                character_name = char.character_name
            except Exception as e:
                logger.info("fitting_stats: skills fetch failed for %s: %s", character_id, e)

    stats = await calculate_fitting_stats(
        db, int(ship_type_id), items, damage_profile,
        skill_levels=skill_levels,
    )

    # Get ship name
    ship_name = await sde.type_id_to_name(db, int(ship_type_id))

    return templates.TemplateResponse(request, "partials/fitting_stats.html", {"stats": stats,
        "ship_name": ship_name or f"Ship {ship_type_id}",
        "ship_type_id": ship_type_id,
        "character_name": character_name})


@router.get("/tools/fitting/ship-slots/{ship_type_id}")
async def ship_slots(
    request: Request,
    ship_type_id: int,
    subsystems: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Return slot counts for a ship type, including subsystem modifiers."""
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    from app.fitting.constants import (
        ATTR_HI_SLOTS, ATTR_MED_SLOTS, ATTR_LOW_SLOTS,
        ATTR_RIG_SLOTS, ATTR_TURRET_SLOTS, ATTR_LAUNCHER_SLOTS,
        ATTR_HI_SLOT_MODIFIER, ATTR_MED_SLOT_MODIFIER, ATTR_LOW_SLOT_MODIFIER,
        ATTR_TURRET_HARDPOINT_MODIFIER, ATTR_LAUNCHER_HARDPOINT_MODIFIER,
    )
    hi = attrs.get(ATTR_HI_SLOTS, 0)
    med = attrs.get(ATTR_MED_SLOTS, 0)
    low = attrs.get(ATTR_LOW_SLOTS, 0)
    rig = attrs.get(ATTR_RIG_SLOTS, 0)
    turret = attrs.get(ATTR_TURRET_SLOTS, 0)
    launcher = attrs.get(ATTR_LAUNCHER_SLOTS, 0)

    # Apply subsystem slot modifiers if provided
    sub_ids = [int(x) for x in subsystems.split(",") if x.strip()] if subsystems else []
    for sub_id in sub_ids:
        sub_attrs = await get_type_dogma_attrs(db, sub_id)
        hi += sub_attrs.get(ATTR_HI_SLOT_MODIFIER, 0)
        med += sub_attrs.get(ATTR_MED_SLOT_MODIFIER, 0)
        low += sub_attrs.get(ATTR_LOW_SLOT_MODIFIER, 0)
        turret += sub_attrs.get(ATTR_TURRET_HARDPOINT_MODIFIER, 0)
        launcher += sub_attrs.get(ATTR_LAUNCHER_HARDPOINT_MODIFIER, 0)

    # Detect T3C (Strategic Cruiser, group 963) for subsystem slot count
    GROUP_STRATEGIC_CRUISER = 963
    ship_result = await db.execute(
        select(SDEType.group_id).where(SDEType.type_id == ship_type_id)
    )
    group_id = ship_result.scalar_one_or_none()
    subsystem_slots = 4 if group_id == GROUP_STRATEGIC_CRUISER else 0

    return {
        "high": int(hi),
        "med": int(med),
        "low": int(low),
        "rig": int(rig),
        "turret": int(turret),
        "launcher": int(launcher),
        "subsystem": subsystem_slots,
    }


@router.post("/tools/fitting/import-eft")
async def import_eft(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Parse EFT format text and return fitting state as JSON."""
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    eft_text = body.get("eft", "").strip()
    if not eft_text:
        return {"error": "No EFT text provided"}

    # Aggressively normalize Unicode that breaks name matching.
    # EVE client, Discord, and browsers inject invisible chars on copy/paste.
    import unicodedata
    cleaned = []
    for ch in eft_text:
        if ch in ('\n', '\r', '\t'):
            cleaned.append(ch)
        elif ch == '\u2019' or ch == '\u2018':
            cleaned.append("'")
        elif ch == '\u201c' or ch == '\u201d':
            cleaned.append('"')
        elif ch == '\u2013' or ch == '\u2014':
            cleaned.append('-')
        elif ch == '\u00a0':
            cleaned.append(' ')  # non-breaking space → space
        elif unicodedata.category(ch).startswith('C') and ch not in ('\n', '\r', '\t'):
            continue  # strip all control/format chars (ZWSP, BOM, etc.)
        else:
            cleaned.append(ch)
    eft_text = ''.join(cleaned)

    lines = eft_text.split("\n")
    if not lines:
        return {"error": "Empty EFT text"}

    # Parse header: [Ship Name, Fitting Name]
    header = lines[0].strip()
    match = re.match(r'^\[(.+?),\s*(.+?)\]$', header)
    if not match:
        return {"error": "Invalid EFT header — expected [Ship, Name]"}

    ship_name = match.group(1).strip()
    fitting_name = match.group(2).strip()

    # Resolve ship type
    ship_type_id = await sde.type_name_to_id(db, ship_name)
    if not ship_type_id:
        return {"error": f"Unknown ship: {ship_name}"}

    # Parse items
    items = []
    current_slot_group = 0  # Track blank-line-separated groups
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("["):
            current_slot_group += 1
            continue

        # Handle quantity suffix: "Module Name x5"
        qty_match = re.match(r'^(.+?)\s+x(\d+)$', line)
        if qty_match:
            item_name = qty_match.group(1).strip()
            quantity = int(qty_match.group(2))
        else:
            item_name = line
            quantity = 1

        # Handle comma-separated charge: "Module Name, Charge Name"
        charge_name_eft = None
        if ", " in item_name and not item_name.startswith("["):
            parts = item_name.rsplit(", ", 1)
            item_name = parts[0].strip()
            charge_name_eft = parts[1].strip()

        # Skip empty slot markers
        if item_name.startswith("[Empty ") or item_name.startswith("[empty "):
            continue

        # Resolve type
        type_id = await sde.type_name_to_id(db, item_name)
        if not type_id:
            logger.warning("EFT import: unresolved name '%s' (hex: %s)",
                           item_name, item_name.encode('unicode_escape').decode())
            continue

        # Determine slot from module slot table
        slot_type = await sde.get_module_slot_type(db, type_id)
        if not slot_type:
            # Check if it's a drone
            from app.db.sde_models import SDEGroup, SDEType
            type_result = await db.execute(
                select(SDEType.group_id).where(SDEType.type_id == type_id)
            )
            group_id = type_result.scalar_one_or_none()
            if group_id:
                group_result = await db.execute(
                    select(SDEGroup.category_id).where(SDEGroup.group_id == group_id)
                )
                cat_id = group_result.scalar_one_or_none()
                if cat_id == 18:
                    slot_type = "drone"
                elif cat_id == 8:
                    slot_type = "cargo"
                else:
                    slot_type = "cargo"
            else:
                slot_type = "cargo"

        item_entry = {
            "type_id": type_id,
            "type_name": item_name,
            "slot": slot_type,
            "quantity": quantity,
        }

        # Resolve inline charge if present
        if charge_name_eft and slot_type in ("high", "mid"):
            charge_id = await sde.type_name_to_id(db, charge_name_eft)
            if charge_id:
                item_entry["charge_type_id"] = charge_id
                item_entry["charge_name"] = charge_name_eft

        items.append(item_entry)

    # Auto-load charges from cargo onto compatible weapons
    cargo_charges = [i for i in items if i["slot"] == "cargo"]
    weapon_items = [i for i in items if i["slot"] in ("high", "mid") and not i.get("charge_type_id")]
    if cargo_charges and weapon_items:
        for weapon in weapon_items:
            if weapon.get("charge_type_id"):
                continue
            compatible = await sde.get_compatible_charges(db, weapon["type_id"])
            compat_ids = {c["type_id"] for c in compatible}
            for cargo in cargo_charges:
                if cargo["type_id"] in compat_ids:
                    weapon["charge_type_id"] = cargo["type_id"]
                    weapon["charge_name"] = cargo["type_name"]
                    break

    # Remove cargo items that are charges (already loaded onto weapons)
    loaded_charge_ids = {i.get("charge_type_id") for i in items if i.get("charge_type_id")}
    items = [i for i in items if not (i["slot"] == "cargo" and i["type_id"] in loaded_charge_ids)]

    return {
        "ship_type_id": ship_type_id,
        "ship_name": ship_name,
        "fitting_name": fitting_name,
        "items": items,
    }


@router.post("/tools/fitting/export-eft")
async def export_eft(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Generate EFT format text from fitting state."""
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("")

    ship_type_id = body.get("ship_type_id")
    fitting_name = body.get("name", "Unnamed")
    items = body.get("items", [])

    if not ship_type_id:
        return PlainTextResponse("")

    ship_name = await sde.type_id_to_name(db, int(ship_type_id))
    if not ship_name:
        ship_name = f"Ship {ship_type_id}"

    # Resolve item names
    type_ids = list({i["type_id"] for i in items})
    type_names = await sde.type_ids_to_names(db, type_ids)

    lines = [f"[{ship_name}, {fitting_name}]"]

    for slot in ["low", "med", "high", "rig", "subsystem"]:
        slot_items = [i for i in items if i.get("slot") == slot]
        for item in slot_items:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            lines.append(name)
        lines.append("")

    drones = [i for i in items if i.get("slot") == "drone"]
    if drones:
        for item in drones:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            qty = item.get("quantity", 1)
            if qty > 1:
                lines.append(f"{name} x{qty}")
            else:
                lines.append(name)
        lines.append("")

    cargo = [i for i in items if i.get("slot") == "cargo"]
    if cargo:
        for item in cargo:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            qty = item.get("quantity", 1)
            if qty > 1:
                lines.append(f"{name} x{qty}")
            else:
                lines.append(name)

    return PlainTextResponse("\n".join(lines).rstrip())


@router.post("/tools/fitting/save")
async def save_fitting(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    ship_type_id = body.get("ship_type_id")
    name = body.get("name", "Unnamed").strip()
    description = body.get("description", "").strip()
    items = body.get("items", [])
    fitting_id = body.get("fitting_id")
    folder_id = body.get("folder_id")
    if folder_id is not None:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            folder_id = None
    if folder_id is not None:
        owner_check = await db.execute(
            select(UserFittingFolder.id)
            .where(UserFittingFolder.id == folder_id)
            .where(UserFittingFolder.user_id == user_id)
        )
        if not owner_check.scalar_one_or_none():
            folder_id = None

    if not ship_type_id:
        return {"error": "No ship selected"}
    if not name:
        return {"error": "Name required"}

    now = datetime.now(timezone.utc)

    if fitting_id:
        result = await db.execute(
            select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
        )
        fitting = result.scalar_one_or_none()
        if fitting:
            fitting.name = name
            fitting.description = description
            fitting.ship_type_id = int(ship_type_id)
            fitting.items_json = json.dumps(items)
            fitting.updated_at = now
            if "folder_id" in body:
                fitting.folder_id = folder_id
            await db.commit()
            return {"id": fitting.id, "status": "updated"}

    fitting = UserFitting(
        user_id=user_id,
        folder_id=folder_id,
        name=name,
        description=description,
        ship_type_id=int(ship_type_id),
        items_json=json.dumps(items),
        created_at=now,
        updated_at=now,
    )
    db.add(fitting)
    await db.commit()
    await db.refresh(fitting)
    return {"id": fitting.id, "status": "saved"}


@router.get("/tools/fitting/load/{fitting_id}")
async def load_fitting(
    request: Request,
    fitting_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    result = await db.execute(
        select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
    )
    fitting = result.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}

    ship_name = await sde.type_id_to_name(db, fitting.ship_type_id)
    items = json.loads(fitting.items_json)

    # Resolve item names
    type_ids = list({i["type_id"] for i in items})
    type_names = await sde.type_ids_to_names(db, type_ids)
    for item in items:
        if "type_name" not in item:
            item["type_name"] = type_names.get(item["type_id"], f"Type {item['type_id']}")

    return {
        "id": fitting.id,
        "name": fitting.name,
        "description": fitting.description or "",
        "ship_type_id": fitting.ship_type_id,
        "ship_name": ship_name or f"Ship {fitting.ship_type_id}",
        "items": items,
    }


@router.delete("/tools/fitting/{fitting_id}")
async def delete_fitting(
    request: Request,
    fitting_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    result = await db.execute(
        select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
    )
    fitting = result.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}

    await db.delete(fitting)
    await db.commit()
    return {"status": "deleted"}


# ── Folder management ──────────────────────────────────────────────────────

async def _owned_folder(db: AsyncSession, folder_id: int, user_id: int) -> UserFittingFolder | None:
    r = await db.execute(
        select(UserFittingFolder)
        .where(UserFittingFolder.id == folder_id)
        .where(UserFittingFolder.user_id == user_id)
    )
    return r.scalar_one_or_none()


@router.post("/tools/fitting/folders")
async def create_folder(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "Name required"}
    parent_id = body.get("parent_id")
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            return {"error": "Invalid parent"}
        if not await _owned_folder(db, parent_id, user_id):
            return {"error": "Parent folder not found"}
    folder = UserFittingFolder(user_id=user_id, parent_id=parent_id, name=name[:128])
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return {"id": folder.id, "parent_id": folder.parent_id, "name": folder.name}


@router.patch("/tools/fitting/folders/{folder_id}")
async def update_folder(request: Request, folder_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    folder = await _owned_folder(db, folder_id, user_id)
    if not folder:
        return {"error": "Folder not found"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    if "name" in body:
        new_name = (body.get("name") or "").strip()
        if not new_name:
            return {"error": "Name required"}
        folder.name = new_name[:128]
    if "parent_id" in body:
        pid = body.get("parent_id")
        if pid is None:
            folder.parent_id = None
        else:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                return {"error": "Invalid parent"}
            if pid == folder.id:
                return {"error": "Cannot parent folder to itself"}
            # Walk up parent chain to detect cycles
            parent = await _owned_folder(db, pid, user_id)
            if not parent:
                return {"error": "Parent folder not found"}
            cursor = parent
            seen = {folder.id}
            while cursor is not None:
                if cursor.id in seen:
                    return {"error": "Would create a folder cycle"}
                seen.add(cursor.id)
                if cursor.parent_id is None:
                    break
                cursor = await _owned_folder(db, cursor.parent_id, user_id)
                if cursor is None:
                    break
            folder.parent_id = pid
    folder.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": folder.id, "parent_id": folder.parent_id, "name": folder.name}


@router.delete("/tools/fitting/folders/{folder_id}")
async def delete_folder(request: Request, folder_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    folder = await _owned_folder(db, folder_id, user_id)
    if not folder:
        return {"error": "Folder not found"}

    # MVP: refuse delete if the folder (or any descendant) contains fits or subfolders.
    sub_count = await db.execute(
        select(UserFittingFolder.id).where(UserFittingFolder.parent_id == folder_id)
    )
    if sub_count.scalar_one_or_none() is not None:
        return {"error": "Folder contains subfolders; empty it first"}
    fit_count = await db.execute(
        select(UserFitting.id)
        .where(UserFitting.folder_id == folder_id)
        .where(UserFitting.user_id == user_id)
    )
    if fit_count.scalar_one_or_none() is not None:
        return {"error": "Folder contains fittings; move or delete them first"}
    await db.delete(folder)
    await db.commit()
    return {"status": "deleted"}


@router.patch("/tools/fitting/{fitting_id}/folder")
async def move_fitting(request: Request, fitting_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    r = await db.execute(
        select(UserFitting)
        .where(UserFitting.id == fitting_id)
        .where(UserFitting.user_id == user_id)
    )
    fitting = r.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}
    folder_id = body.get("folder_id")
    if folder_id is None:
        fitting.folder_id = None
    else:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            return {"error": "Invalid folder"}
        if not await _owned_folder(db, folder_id, user_id):
            return {"error": "Folder not found"}
        fitting.folder_id = folder_id
    fitting.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": fitting.id, "folder_id": fitting.folder_id}


# ── Import from character (ESI in-game fittings) ────────────────────────

_FITTINGS_SCOPE = "esi-fittings.read_fittings.v1"


@router.get("/tools/fitting/import-character/characters")
async def import_char_list(request: Request, db: AsyncSession = Depends(get_db)):
    """Return the logged-in user's characters that have the fittings scope."""
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "characters": []}
    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    chars = []
    for c in r.scalars().all():
        if _FITTINGS_SCOPE in (c.scopes or ""):
            chars.append({"id": c.character_id, "name": c.character_name})
    return {"characters": chars}


@router.get("/tools/fitting/import-character/{character_id}/fittings")
async def import_char_fittings(
    request: Request, character_id: int, db: AsyncSession = Depends(get_db),
):
    """Return the character's in-game fittings as EFT strings + metadata."""
    # Local import avoids a circular dep between fitting.py and fittings.py
    from app.routes.fittings import _parse_fitting, _to_eft

    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "fittings": []}
    r = await db.execute(
        select(Character)
        .where(Character.character_id == character_id)
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found", "fittings": []}
    if _FITTINGS_SCOPE not in (char.scopes or ""):
        return {"error": "Fittings scope missing — re-authorize this character", "fittings": []}
    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await esi_char.get_fittings(client, character_id)
    except Exception as e:
        logger.warning("import-char fittings fetch failed: %s", e, exc_info=True)
        return {"error": f"ESI error: {type(e).__name__}", "fittings": []}

    if not raw:
        return {"fittings": []}

    all_ids: set[int] = set()
    for f in raw:
        all_ids.add(f["ship_type_id"])
        for item in f.get("items", []):
            all_ids.add(item["type_id"])
    type_names = await sde.type_ids_to_names(db, list(all_ids))

    fittings = []
    for f in sorted(raw, key=lambda x: (x.get("ship_type_id"), x.get("name", ""))):
        sid = f["ship_type_id"]
        ship_name = type_names.get(sid, f"Ship {sid}")
        parsed = _parse_fitting(f, type_names, ship_name, {})
        fittings.append({
            "fitting_id": f.get("fitting_id"),
            "name": f.get("name", "Unnamed"),
            "ship_type_id": sid,
            "ship_name": ship_name,
            "eft": _to_eft(parsed),
        })
    return {"fittings": fittings}


# ── Module browser endpoints ─────────────────────────────────────────────


# Root market group IDs relevant to ship fitting
FITTING_ROOT_GROUPS = {
    "modules": 9,      # Ship Equipment
    "rigs": 955,        # Ship Modifications
    "drones": 157,      # Drones
    "charges": 11,      # Ammunition & Charges
}


@router.get("/tools/fitting/browse/groups")
async def browse_groups(
    request: Request,
    parent: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get child market groups for the browser tree."""
    if parent is None:
        # Return the top-level fitting categories
        groups = []
        for label, gid in FITTING_ROOT_GROUPS.items():
            children = await sde.get_market_group_children(db, gid)
            groups.append({
                "market_group_id": gid,
                "market_group_name": label.replace("_", " ").title(),
                "has_children": len(children) > 0,
            })
        return groups
    children = await sde.get_market_group_children(db, parent)
    return children


@router.get("/tools/fitting/browse/items/{market_group_id}")
async def browse_items(
    request: Request,
    market_group_id: int,
    ship_type_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get items in a market group with fit restriction info."""
    items = await sde.get_market_group_items(db, market_group_id)

    # Check fit restrictions if a ship is selected
    if ship_type_id:
        for item in items:
            item["can_fit"] = await sde.can_module_fit_ship(
                db, item["type_id"], ship_type_id
            )
    else:
        for item in items:
            item["can_fit"] = True

    return items


@router.get("/tools/fitting/browse/path/{market_group_id}")
async def browse_path(
    request: Request,
    market_group_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get breadcrumb path for a market group."""
    return await sde.get_market_group_path(db, market_group_id)


@router.get("/tools/fitting/check-fit")
async def check_module_fit(
    request: Request,
    module_type_id: int = Query(...),
    ship_type_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Check if a module can fit a specific ship."""
    can_fit = await sde.can_module_fit_ship(db, module_type_id, ship_type_id)
    restrictions = await sde.get_module_fit_restrictions(db, module_type_id)
    return {"can_fit": can_fit, "restrictions": restrictions}


OVERLOAD_ATTR_IDS = [1210, 1205, 1223, 1208, 1230, 1231, 1206, 1222]


@router.get("/tools/fitting/can-overheat")
async def can_overheat(
    request: Request,
    type_ids: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Check which module types can be overheated."""
    if not type_ids:
        return {}
    ids = [int(x) for x in type_ids.split(",") if x.strip()]
    if not ids:
        return {}
    result = await db.execute(
        select(SDETypeDogmaAttribute.type_id)
        .where(SDETypeDogmaAttribute.type_id.in_(ids))
        .where(SDETypeDogmaAttribute.attribute_id.in_(OVERLOAD_ATTR_IDS))
        .distinct()
    )
    overheatable = {row[0] for row in result.fetchall()}
    return {str(tid): tid in overheatable for tid in ids}


# ── Character skills + fit skill-check ──────────────────────────────────

_SKILLS_SCOPE = "esi-skills.read_skills.v1"


async def _character_skills_map(db: AsyncSession, char: Character) -> dict[int, int]:
    """Return {skill_type_id: active_skill_level} for this character."""
    token = await refresh_token(char, db)
    client = ESIClient(token, db=db)
    data = await esi_char.get_skills(client, char.character_id)
    return {
        int(s["skill_id"]): int(s.get("active_skill_level", 0))
        for s in (data or {}).get("skills", [])
    }


@router.get("/tools/fitting/characters")
async def list_fitting_characters(request: Request, db: AsyncSession = Depends(get_db)):
    """Dropdown source — characters the user owns that have the skills scope."""
    user_id = request.session.get("user_id")
    if not user_id:
        return {"characters": []}
    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    return {
        "characters": [
            {"id": c.character_id, "name": c.character_name}
            for c in r.scalars().all()
            if _SKILLS_SCOPE in (c.scopes or "")
        ],
    }


@router.post("/tools/fitting/skill-check")
async def fit_skill_check(request: Request, db: AsyncSession = Depends(get_db)):
    """For a ship + list of items + character, return missing skills per type_id.

    Response shape:
        {
          "missing": {
            "<type_id>": [{"skill_id", "skill_name", "need", "have"}, ...],
            ...
          },
          "skills": {<skill_id>: level, ...}
        }

    A type_id only appears in `missing` if the character is short on at
    least one of its required skills.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "missing": {}}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request", "missing": {}}

    character_id = body.get("character_id")
    ship_type_id = body.get("ship_type_id")
    items = body.get("items", []) or []
    if not character_id:
        return {"missing": {}, "skills": {}}

    r = await db.execute(
        select(Character)
        .where(Character.character_id == int(character_id))
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found", "missing": {}}
    if _SKILLS_SCOPE not in (char.scopes or ""):
        return {
            "error": "Character is missing esi-skills.read_skills.v1 — re-authorize it.",
            "missing": {},
        }

    try:
        skills = await _character_skills_map(db, char)
    except Exception as e:
        logger.warning("skills fetch failed for char %s: %s", character_id, e)
        return {"error": f"Could not load skills: {type(e).__name__}", "missing": {}}

    # Collect every type_id whose requirements we need to check
    type_ids: set[int] = set()
    if ship_type_id:
        type_ids.add(int(ship_type_id))
    for item in items:
        tid = item.get("type_id")
        if tid:
            type_ids.add(int(tid))
        # Drones and charges are also items the char needs skills to use;
        # skip cargo since it doesn't affect "can I fly this" materially.
        if item.get("charge_type_id"):
            type_ids.add(int(item["charge_type_id"]))

    if not type_ids:
        return {"missing": {}, "skills": skills}

    req_rows = await db.execute(
        select(
            SDETypeSkillReq.type_id,
            SDETypeSkillReq.skill_type_id,
            SDETypeSkillReq.required_level,
        ).where(SDETypeSkillReq.type_id.in_(type_ids))
    )

    missing_by_type: dict[int, list[dict]] = {}
    missing_skill_ids: set[int] = set()
    for row in req_rows.fetchall():
        have = int(skills.get(row.skill_type_id, 0))
        if have < int(row.required_level):
            missing_by_type.setdefault(row.type_id, []).append({
                "skill_id": int(row.skill_type_id),
                "need": int(row.required_level),
                "have": have,
            })
            missing_skill_ids.add(row.skill_type_id)

    skill_names = await sde.type_ids_to_names(db, list(missing_skill_ids)) if missing_skill_ids else {}
    for tid, missing in missing_by_type.items():
        missing.sort(key=lambda m: (-m["need"], m["skill_id"]))
        for m in missing:
            m["skill_name"] = skill_names.get(m["skill_id"], f"Skill {m['skill_id']}")

    # Serialize keys as strings for JSON friendliness (JS uses type_id as key)
    return {
        "missing": {str(k): v for k, v in missing_by_type.items()},
        "skills_trained": len(skills),
    }


@router.get("/tools/fitting/charges/{module_type_id}")
async def get_charges(
    request: Request,
    module_type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get compatible charges for a module."""
    return await sde.get_compatible_charges(db, module_type_id)


# ── Module info modal ────────────────────────────────────────────────────────

# unit_id → (suffix, optional value transform)
# Values are per CCP's dogmaUnits table.
_UNIT_FMT: dict[int, tuple[str, callable]] = {
    1:   (" m",      lambda v: f"{v:,.1f}"),          # length
    2:   (" kg",     lambda v: f"{v:,.0f}"),          # mass
    3:   (" s",      lambda v: f"{v:,.1f}"),          # time (seconds)
    9:   ("",        lambda v: f"{v:g}"),             # enum
    101: (" s",      lambda v: f"{v/1000:,.2f}"),     # milliseconds → s
    102: (" mm",     lambda v: f"{v:,.0f}"),          # mm
    104: (" AU",     lambda v: f"{v:,.2f}"),          # AU
    105: ("%",       lambda v: f"{v:+,.1f}"),         # percent
    106: (" tf",     lambda v: f"{v:,.0f}"),          # CPU
    107: (" MW",     lambda v: f"{v:,.0f}"),          # PG
    108: (" x",      lambda v: f"{v:,.3f}"),          # inverse-absolute-percent multiplier
    109: (" x",      lambda v: f"{v:,.3f}"),          # multiplier
    111: ("%",       lambda v: f"{v*100:+,.1f}"),     # inverse percent 0.05 → 5%
    113: (" HP",     lambda v: f"{v:,.0f}"),          # HP
    114: (" GJ",     lambda v: f"{v:,.2f}"),          # GJ
    119: ("",        lambda v: f"{int(v)}"),          # level
    120: ("",        lambda v: f"{int(v)}"),          # slot
    121: ("",        lambda v: f"{int(v)}"),          # item
    122: (" ISK",    lambda v: f"{v:,.2f}"),          # ISK
    123: ("%",       lambda v: f"{v:+,.1f}"),         # abs percent
    124: ("%",       lambda v: f"{(1-v)*100:+,.1f}"), # inverse resonance
    125: (" m³/hr",  lambda v: f"{v:,.0f}"),
    126: ("%",       lambda v: f"{v:+,.1f}"),         # speed %
    127: ("%",       lambda v: f"{v:+,.1f}"),
    128: ("%",       lambda v: f"{v:+,.1f}"),
    129: ("%",       lambda v: f"{v:+,.1f}"),
    130: (" rad/s",  lambda v: f"{v:,.3f}"),
    137: ("",        lambda v: "yes" if v else "no"), # bool
    139: ("",        lambda v: f"{v:,.0f}"),          # units
    140: ("",        lambda v: f"{int(v)}"),          # level
    141: ("",        lambda v: f"{int(v)}"),          # hardpoints
    143: ("",        lambda v: f"{v:g}"),             # datetime-ish
    204: (" rad/s²", lambda v: f"{v:,.4f}"),
}


def _format_attr(value: float, unit_id: int | None) -> str:
    fmt = _UNIT_FMT.get(unit_id or 0)
    if fmt:
        suffix, conv = fmt
        try:
            return f"{conv(value)}{suffix}"
        except Exception:
            pass
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


# Attributes we always hide even if CCP gave them a display_name
_INFO_ATTR_BLACKLIST = {
    182, 183, 184, 1285, 1289, 1290,      # required skill IDs (we don't resolve names here)
    277, 278, 279, 1286, 1287, 1288,      # required skill levels
    1768,                                   # typeColorScheme
    633,                                    # metaLevelOld (duplicate of 1692)
}


async def _resolve_type_names(db: AsyncSession, type_ids: list[int]) -> dict[int, str]:
    if not type_ids:
        return {}
    r = await db.execute(
        select(SDEType.type_id, SDEType.type_name).where(SDEType.type_id.in_(type_ids))
    )
    return {row.type_id: row.type_name for row in r.fetchall()}


async def _resolve_group_names(db: AsyncSession, group_ids: list[int]) -> dict[int, str]:
    if not group_ids:
        return {}
    r = await db.execute(
        select(SDEGroup.group_id, SDEGroup.group_name).where(SDEGroup.group_id.in_(group_ids))
    )
    return {row.group_id: row.group_name for row in r.fetchall()}


@router.get("/tools/fitting/info/{type_id}", response_class=HTMLResponse)
async def fitting_info(
    request: Request,
    type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Show essential info about a ship/module/charge — modal body partial."""
    # Type + group
    t = (await db.execute(
        select(SDEType).where(SDEType.type_id == type_id)
    )).scalar_one_or_none()
    if not t:
        return HTMLResponse(
            "<div style='padding:1rem;color:var(--muted);font-size:11px;'>Type not found.</div>"
        )
    group_name = None
    if t.group_id:
        g = (await db.execute(
            select(SDEGroup.group_name).where(SDEGroup.group_id == t.group_id)
        )).scalar_one_or_none()
        group_name = g

    # Description — best-effort via public ESI (cached)
    description = ""
    try:
        esi = ESIClient("", db=db)
        info = await esi_universe.get_type(esi, type_id)
        description = (info or {}).get("description", "") or ""
    except Exception as e:
        logger.info("fitting_info: ESI description fetch failed for %d: %s", type_id, e)

    # Dogma attrs with display_name only — plus a few meta attrs collected for ref
    rows = (await db.execute(
        select(
            SDETypeDogmaAttribute.attribute_id,
            SDETypeDogmaAttribute.value,
            SDEDogmaAttribute.display_name,
            SDEDogmaAttribute.attribute_name,
            SDEDogmaAttribute.unit_id,
        )
        .join(SDEDogmaAttribute,
              SDEDogmaAttribute.attribute_id == SDETypeDogmaAttribute.attribute_id)
        .where(SDETypeDogmaAttribute.type_id == type_id)
    )).fetchall()

    meta_level = None
    for r in rows:
        if r.attribute_id == 1692:  # metaLevel
            meta_level = r.value

    # Collect group/type IDs referenced in unit_id 115/116 so we can resolve to names
    referenced_groups: list[int] = []
    referenced_types: list[int] = []
    for r in rows:
        if r.unit_id == 115:
            referenced_groups.append(int(r.value))
        elif r.unit_id == 116:
            referenced_types.append(int(r.value))
    group_map = await _resolve_group_names(db, referenced_groups)
    type_map = await _resolve_type_names(db, referenced_types)

    attrs = []
    for r in rows:
        if r.attribute_id in _INFO_ATTR_BLACKLIST:
            continue
        if not r.display_name:
            continue
        if r.value == 0:
            continue
        # Resolve IDs-as-values into names
        if r.unit_id == 115:
            value_display = group_map.get(int(r.value), f"Group {int(r.value)}")
        elif r.unit_id == 116:
            value_display = type_map.get(int(r.value), f"Type {int(r.value)}")
        else:
            value_display = _format_attr(r.value, r.unit_id)
        attrs.append({
            "label": r.display_name,
            "value_display": value_display,
            "_sort": r.display_name.lower(),
        })
    attrs.sort(key=lambda a: a["_sort"])

    return templates.TemplateResponse(request, "partials/fitting_info.html", {"type_id": type_id,
        "type_name": t.type_name,
        "group_name": group_name,
        "meta_level": meta_level,
        "volume": t.volume,
        "description": description,
        "attrs": attrs})
