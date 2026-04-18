"""Ship fitting tool — build and analyze ship fittings locally."""

import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, UserFitting
from app.sde import lookup as sde
from app.fitting.engine import calculate_fitting_stats, get_type_dogma_attrs
from app.fitting.constants import ATTR_CPU, ATTR_POWER, ATTR_UPGRADE_COST, ATTR_DRONE_BW_USED
from app.db.sde_models import SDEModuleSlot, SDEType, SDEGroup

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fitting"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/tools/fitting", response_class=HTMLResponse)
async def fitting_tool(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    saved_fittings = []
    if user_id:
        result = await db.execute(
            select(UserFitting)
            .where(UserFitting.user_id == user_id)
            .order_by(UserFitting.updated_at.desc())
        )
        saved_fittings = [
            {"id": f.id, "name": f.name, "ship_type_id": f.ship_type_id,
             "description": f.description, "updated_at": f.updated_at}
            for f in result.scalars().all()
        ]
    return templates.TemplateResponse("fitting_tool.html", {
        "request": request,
        "saved_fittings": saved_fittings,
    })


@router.get("/tools/fitting/search/ships", response_class=HTMLResponse)
async def search_ships(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_ships(db, q, limit=15)
    return templates.TemplateResponse("partials/fitting_search_results.html", {
        "request": request,
        "results": results,
        "search_type": "ship",
    })


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

    return templates.TemplateResponse("partials/fitting_search_results.html", {
        "request": request,
        "results": results,
        "search_type": "module",
    })


@router.get("/tools/fitting/search/drones", response_class=HTMLResponse)
async def search_drones(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_drones(db, q, limit=15)
    return templates.TemplateResponse("partials/fitting_search_results.html", {
        "request": request,
        "results": results,
        "search_type": "drone",
    })


@router.get("/tools/fitting/search/charges", response_class=HTMLResponse)
async def search_charges(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    results = await sde.search_charges(db, q, limit=15)
    return templates.TemplateResponse("partials/fitting_search_results.html", {
        "request": request,
        "results": results,
        "search_type": "charge",
    })


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
    stats = await calculate_fitting_stats(db, int(ship_type_id), items)

    # Get ship name
    ship_name = await sde.type_id_to_name(db, int(ship_type_id))

    return templates.TemplateResponse("partials/fitting_stats.html", {
        "request": request,
        "stats": stats,
        "ship_name": ship_name or f"Ship {ship_type_id}",
        "ship_type_id": ship_type_id,
    })


@router.get("/tools/fitting/ship-slots/{ship_type_id}")
async def ship_slots(
    request: Request,
    ship_type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return slot counts for a ship type."""
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    from app.fitting.constants import (
        ATTR_HI_SLOTS, ATTR_MED_SLOTS, ATTR_LOW_SLOTS,
        ATTR_RIG_SLOTS, ATTR_TURRET_SLOTS, ATTR_LAUNCHER_SLOTS,
    )
    return {
        "high": int(attrs.get(ATTR_HI_SLOTS, 0)),
        "med": int(attrs.get(ATTR_MED_SLOTS, 0)),
        "low": int(attrs.get(ATTR_LOW_SLOTS, 0)),
        "rig": int(attrs.get(ATTR_RIG_SLOTS, 0)),
        "turret": int(attrs.get(ATTR_TURRET_SLOTS, 0)),
        "launcher": int(attrs.get(ATTR_LAUNCHER_SLOTS, 0)),
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

        # Skip empty slot markers
        if item_name.startswith("[Empty ") or item_name.startswith("[empty "):
            continue

        # Resolve type
        type_id = await sde.type_name_to_id(db, item_name)
        if not type_id:
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

        items.append({
            "type_id": type_id,
            "type_name": item_name,
            "slot": slot_type,
            "quantity": quantity,
        })

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
            await db.commit()
            return {"id": fitting.id, "status": "updated"}

    fitting = UserFitting(
        user_id=user_id,
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
