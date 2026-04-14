"""Planetary Industry — multi-character colony overview, system lookup, schematic chain."""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, CharacterDashboardCache, get_db
from app.esi import market as esi_market
from app.esi.client import ESIClient
from app.pi import constants as pi_const
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["planetary"])
templates = Jinja2Templates(directory="app/templates")

PI_SCOPE = "esi-planets.manage_planets.v1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    """Compact duration: 2d 3h, 4h 12m, 35m, expired."""
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "expired"
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _recompute_expiry(planet: dict) -> dict:
    """Recalculate expiry_time_str + expiry_warning from the stored ISO expiry.

    Cached `expiry_time_str` is stale the moment the sync completes — recompute
    at render time so countdowns are always correct. If `expiry_time` (raw ISO)
    is missing — e.g. cache saved before the fetcher was enriched — fall back
    to whatever pre-formatted strings the old cache already had.
    """
    expiry_raw = planet.get("expiry_time")
    if not expiry_raw:
        # Leave any pre-existing expiry_time_str/warning in place (old cache).
        planet.setdefault("expiry_time_str", None)
        planet.setdefault("expiry_warning", None)
        return planet
    try:
        expiry_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
        delta = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        planet["expiry_time_str"] = _format_duration(delta)
        planet["expiry_warning"] = (
            "expired" if delta <= 0
            else "critical" if delta < 3600
            else "warning" if delta < 86400
            else "ok"
        )
        planet["_expiry_seconds"] = delta
    except Exception:
        pass
    return planet


async def _load_pi(db: AsyncSession, characters: list[Character]) -> dict[int, list | str]:
    """Return {character_id: [planets...] | 'no_scope' | None}, reading from cache."""
    char_ids = [c.character_id for c in characters]
    if not char_ids:
        return {}
    result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(char_ids))
    )
    caches = {c.character_id: c for c in result.scalars().all()}
    out: dict[int, list | str] = {}
    for char in characters:
        scopes = char.scopes or ""
        if PI_SCOPE not in scopes:
            out[char.character_id] = "no_scope"
            continue
        cache = caches.get(char.character_id)
        raw = getattr(cache, "pi_json", None) if cache else None
        if not raw:
            out[char.character_id] = None
            continue
        try:
            planets = json.loads(raw)
        except Exception:
            planets = None
        if isinstance(planets, list):
            out[char.character_id] = [_recompute_expiry(dict(p)) for p in planets]
        else:
            out[char.character_id] = planets
    return out


# ── Phase 1: /industry/planetary ──────────────────────────────────────────────

@router.get("/industry/planetary", response_class=HTMLResponse)
async def planetary_page(
    request: Request,
    sort: str = Query("expiry"),
    filter_type: str = Query(""),
    filter_warn: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())
    if not characters:
        return RedirectResponse("/dashboard")

    pi_by_char = await _load_pi(db, characters)

    # Flatten into one list of {char, planet} rows for the aggregate table.
    rows = []
    missing_scope_chars = []
    for char in characters:
        entry = pi_by_char.get(char.character_id)
        if entry == "no_scope":
            missing_scope_chars.append(char)
            continue
        if not isinstance(entry, list):
            continue
        for planet in entry:
            rows.append({"char": char, "planet": planet})

    # Filter
    if filter_type:
        rows = [r for r in rows if (r["planet"].get("planet_type") or "").lower() == filter_type.lower()]
    if filter_warn == "expiring":
        rows = [r for r in rows if r["planet"].get("expiry_warning") in ("expired", "critical", "warning")]
    elif filter_warn == "expired":
        rows = [r for r in rows if r["planet"].get("expiry_warning") == "expired"]

    # Sort
    def _sort_key(r):
        p = r["planet"]
        if sort == "expiry":
            # Unknown / missing expiry → sort last.
            secs = p.get("_expiry_seconds")
            return (float("inf") if secs is None else secs, r["char"].character_name.lower())
        if sort == "character":
            return (r["char"].character_name.lower(), p.get("planet_id") or 0)
        if sort == "system":
            return ((p.get("system_name") or "").lower(), r["char"].character_name.lower())
        if sort == "type":
            return ((p.get("planet_type") or "").lower(), r["char"].character_name.lower())
        return 0
    rows.sort(key=_sort_key)

    # Collect type filter options (only types the user actually has)
    all_types = sorted({(r["planet"].get("planet_type") or "").lower() for r in rows if r["planet"].get("planet_type")})

    # Summary counts for the header chips
    total_planets = sum(1 for r in rows)
    expiring_soon = sum(1 for r in rows if r["planet"].get("expiry_warning") in ("critical", "expired"))
    warning_cnt = sum(1 for r in rows if r["planet"].get("expiry_warning") == "warning")

    return templates.TemplateResponse("planetary.html", {
        "request": request,
        "rows": rows,
        "missing_scope_chars": missing_scope_chars,
        "sort": sort,
        "filter_type": filter_type,
        "filter_warn": filter_warn,
        "all_types": all_types,
        "total_planets": total_planets,
        "expiring_soon": expiring_soon,
        "warning_cnt": warning_cnt,
        "pin_group_names": pi_const.PIN_GROUP_NAMES,
    })


@router.get("/industry/planetary/planet/{character_id}/{planet_id}", response_class=HTMLResponse)
async def planet_detail_partial(
    request: Request,
    character_id: int,
    planet_id: int,
    db: AsyncSession = Depends(get_db),
):
    """htmx partial — expandable per-planet pin detail."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    # Scope this character to the requesting user
    char_result = await db.execute(
        select(Character).where(
            Character.user_id == user_id,
            Character.character_id == character_id,
        )
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse("")

    pi_by_char = await _load_pi(db, [char])
    entry = pi_by_char.get(character_id)
    if not isinstance(entry, list):
        return HTMLResponse('<div class="b-empty" style="padding:0.5rem;">No PI data cached.</div>')

    planet = next((p for p in entry if p.get("planet_id") == planet_id), None)
    if not planet:
        return HTMLResponse('<div class="b-empty" style="padding:0.5rem;">Planet not found.</div>')

    # Resolve type_ids across pins + extractor products + contents
    type_ids: set[int] = set()
    schematic_ids: set[int] = set()
    for pin in planet.get("pins") or []:
        if pin.get("type_id"):
            type_ids.add(pin["type_id"])
        if pin.get("extractor_product_type_id"):
            type_ids.add(pin["extractor_product_type_id"])
        if pin.get("schematic_id"):
            schematic_ids.add(pin["schematic_id"])
        for c in pin.get("contents") or []:
            if c.get("type_id"):
                type_ids.add(c["type_id"])

    type_names = await sde.type_ids_to_names(db, list(type_ids)) if type_ids else {}

    # Resolve schematics (Phase 2 tables — gracefully fall back if not loaded yet)
    schematic_info: dict[int, dict] = {}
    if schematic_ids:
        try:
            schematic_info = await _get_schematics_info(db, list(schematic_ids))
        except Exception:
            schematic_info = {}

    # Sort pins: extractors first, then factories, then storage
    pin_sort = {"command_center": 0, "extractor": 1, "factory": 2, "launchpad": 3, "storage": 4, "link": 5, "other": 9}

    def _pin_kind(pin):
        """Derive pin kind. Hard-coded type_id table first, else behavior."""
        kind = pi_const.TYPE_TO_PIN_KIND.get(pin.get("type_id"))
        if kind:
            return kind
        if pin.get("extractor_product_type_id") or pin.get("extractor_cycle_time"):
            return "extractor"
        if pin.get("schematic_id"):
            return "factory"
        if pin.get("contents"):
            return "launchpad"
        # Fall back on name pattern — useful when the type-id table is incomplete
        name = (type_names.get(pin.get("type_id")) or "").lower()
        if "command center" in name:
            return "command_center"
        if "extractor" in name:
            return "extractor"
        if "industrial facility" in name or "production plant" in name:
            return "factory"
        if "launchpad" in name or "spaceport" in name:
            return "launchpad"
        if "storage" in name:
            return "storage"
        return "other"

    def _sort_pin(pin):
        return (pin_sort.get(_pin_kind(pin), 9), type_names.get(pin.get("type_id"), ""))

    pins = sorted(planet.get("pins") or [], key=_sort_pin)

    # Recompute per-pin expiry for display
    now = datetime.now(timezone.utc)
    for pin in pins:
        expiry_raw = pin.get("expiry_time")
        if expiry_raw:
            try:
                dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                delta = (dt - now).total_seconds()
                pin["_expiry_str"] = _format_duration(delta)
                pin["_expiry_warning"] = (
                    "expired" if delta <= 0
                    else "critical" if delta < 3600
                    else "warning" if delta < 86400
                    else "ok"
                )
            except Exception:
                pass
        pin["_kind"] = _pin_kind(pin)
        pin["_type_name"] = type_names.get(pin.get("type_id"), f"Type {pin.get('type_id')}")
        if pin.get("extractor_product_type_id"):
            pin["_product_name"] = type_names.get(pin["extractor_product_type_id"], "")
        if pin.get("schematic_id"):
            sch = schematic_info.get(pin["schematic_id"])
            if sch:
                pin["_schematic_name"] = sch.get("name")
                pin["_schematic_cycle"] = sch.get("cycle_time")
                pin["_schematic_output"] = sch.get("output_name")
                pin["_schematic_output_qty"] = sch.get("output_qty")

    return templates.TemplateResponse("partials/planetary_planet_detail.html", {
        "request": request,
        "planet": planet,
        "pins": pins,
        "type_names": type_names,
        "pin_group_names": pi_const.PIN_GROUP_NAMES,
    })


async def _get_schematics_info(db: AsyncSession, schematic_ids: list[int]) -> dict[int, dict]:
    """Look up schematic names, cycle time, and output type from SDE (Phase 2 tables).

    Returns {} if the planet-schematic tables aren't loaded yet — renders fall
    back to raw schematic IDs.
    """
    from app.db.sde_models import SDEPlanetSchematic, SDEPlanetSchematicMaterial
    try:
        result = await db.execute(
            select(SDEPlanetSchematic).where(SDEPlanetSchematic.schematic_id.in_(schematic_ids))
        )
        schematics = {s.schematic_id: s for s in result.scalars().all()}
    except Exception:
        return {}
    if not schematics:
        return {}

    mat_result = await db.execute(
        select(SDEPlanetSchematicMaterial).where(
            SDEPlanetSchematicMaterial.schematic_id.in_(list(schematics.keys())),
            SDEPlanetSchematicMaterial.is_input == False,
        )
    )
    outputs = {m.schematic_id: m for m in mat_result.scalars().all()}
    out_type_ids = [m.type_id for m in outputs.values()]
    out_names = await sde.type_ids_to_names(db, out_type_ids) if out_type_ids else {}

    info = {}
    for sid, sch in schematics.items():
        out_mat = outputs.get(sid)
        info[sid] = {
            "schematic_id": sid,
            "name": sch.schematic_name,
            "cycle_time": sch.cycle_time,
            "output_name": out_names.get(out_mat.type_id) if out_mat else None,
            "output_qty": out_mat.quantity if out_mat else None,
        }
    return info


# ── Phase 2: /industry/planetary/lookup ───────────────────────────────────────

@router.get("/industry/planetary/lookup", response_class=HTMLResponse)
async def planetary_lookup_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Build ordered map for the reference grid (capitalized keys for display)
    ordered_by_type = {
        pt: pi_const.P0_BY_PLANET_TYPE[pt.lower()]
        for pt in pi_const.PLANET_TYPES
    }
    return templates.TemplateResponse("planetary_lookup.html", {
        "request": request,
        "planet_types": pi_const.PLANET_TYPES,
        "p0_materials": pi_const.P0_MATERIALS,
        "p0_by_type": ordered_by_type,
    })


@router.get("/industry/planetary/lookup/search", response_class=JSONResponse)
async def planetary_system_search(q: str = Query(""), db: AsyncSession = Depends(get_db)):
    if len(q) < 2:
        return []
    return await sde.search_systems(db, q, limit=10)


@router.get("/industry/planetary/lookup/system", response_class=HTMLResponse)
async def planetary_lookup_system(
    request: Request,
    name: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """htmx partial — list planets in a system with their P0 outputs."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    sys_id = await sde.system_name_to_id(db, name)
    if not sys_id:
        return HTMLResponse(f'<div class="b-empty">System "{name}" not found.</div>')

    sys_info = await sde.system_info(db, sys_id)

    # Pull planets from SDE — may be empty if Phase 2 loader hasn't run yet.
    from app.db.sde_models import SDEPlanet
    try:
        result = await db.execute(
            select(SDEPlanet).where(SDEPlanet.system_id == sys_id).order_by(SDEPlanet.planet_index)
        )
        planets = list(result.scalars().all())
    except Exception:
        planets = []

    rendered_planets = []
    for p in planets:
        ptype = pi_const.PLANET_TYPE_NAMES.get(p.planet_type_id, f"type {p.planet_type_id}")
        p0_list = pi_const.P0_BY_PLANET_TYPE.get(ptype.lower(), [])
        rendered_planets.append({
            "planet_id": p.planet_id,
            "planet_name": p.planet_name,
            "planet_type": ptype,
            "planet_index": p.planet_index,
            "p0_materials": p0_list,
        })

    return templates.TemplateResponse("partials/planetary_lookup_system.html", {
        "request": request,
        "system": sys_info,
        "planets": rendered_planets,
        "no_sde": not planets,
    })


@router.get("/industry/planetary/lookup/by-material", response_class=HTMLResponse)
async def planetary_lookup_by_material(
    request: Request,
    material: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """htmx partial — show which planet types produce a given P0 raw material."""
    if not request.session.get("user_id"):
        return HTMLResponse("")

    producers = [
        ptype for ptype, mats in pi_const.P0_BY_PLANET_TYPE.items()
        if material.lower() in (m.lower() for m in mats)
    ]
    return templates.TemplateResponse("partials/planetary_lookup_material.html", {
        "request": request,
        "material": material,
        "producers": producers,
    })


# ── Phase 3: /industry/planetary/chain ────────────────────────────────────────

@router.get("/industry/planetary/chain", response_class=HTMLResponse)
async def planetary_chain_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Build tier map from SDE
    chain = await _build_chain_tiers(db)

    # Fetch prices for every PI commodity type id (cheap: one bulk ESI call)
    all_type_ids = {c["type_id"] for tier in chain.values() for c in tier}
    price_map = await _get_pi_prices(db, all_type_ids)

    # Attach price + profitability to each row (always set keys so Jinja
    # comparisons don't choke on Undefined).
    for tier_num, items in chain.items():
        for item in items:
            tid = item["type_id"]
            item["price"] = price_map.get(tid, 0.0)
            item["input_cost_per_cycle"] = None
            item["margin_per_cycle"] = None
            item["margin_pct"] = None
            if item.get("inputs"):
                total_input_cost = sum(
                    (price_map.get(inp["type_id"], 0.0) or 0.0) * (inp["quantity"] or 0)
                    for inp in item["inputs"]
                )
                out_qty = item.get("output_qty") or 0
                revenue = (item["price"] or 0.0) * out_qty
                item["input_cost_per_cycle"] = total_input_cost
                item["margin_per_cycle"] = revenue - total_input_cost
                if total_input_cost > 0:
                    item["margin_pct"] = item["margin_per_cycle"] / total_input_cost * 100

    return templates.TemplateResponse("planetary_chain.html", {
        "request": request,
        "chain": chain,
        "p0_by_planet_type": pi_const.P0_BY_PLANET_TYPE,
    })


@router.get("/industry/planetary/chain/node/{type_id}", response_class=HTMLResponse)
async def planetary_chain_node(
    request: Request,
    type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """htmx partial — detail panel for a single commodity node."""
    if not request.session.get("user_id"):
        return HTMLResponse("")

    from app.db.sde_models import SDEPlanetSchematic, SDEPlanetSchematicMaterial

    name = await sde.type_id_to_name(db, type_id)

    # Find schematic that OUTPUTS this commodity (where this is_input=False)
    schematic_id = None
    inputs: list[dict] = []
    cycle_time = None
    output_qty = None
    try:
        result = await db.execute(
            select(SDEPlanetSchematicMaterial.schematic_id, SDEPlanetSchematicMaterial.quantity)
            .where(
                SDEPlanetSchematicMaterial.type_id == type_id,
                SDEPlanetSchematicMaterial.is_input == False,
            )
            .limit(1)
        )
        row = result.fetchone()
        if row:
            schematic_id = row.schematic_id
            output_qty = row.quantity
        if schematic_id:
            sch_result = await db.execute(
                select(SDEPlanetSchematic).where(SDEPlanetSchematic.schematic_id == schematic_id)
            )
            sch = sch_result.scalar_one_or_none()
            if sch:
                cycle_time = sch.cycle_time

            in_result = await db.execute(
                select(SDEPlanetSchematicMaterial.type_id, SDEPlanetSchematicMaterial.quantity)
                .where(
                    SDEPlanetSchematicMaterial.schematic_id == schematic_id,
                    SDEPlanetSchematicMaterial.is_input == True,
                )
            )
            in_rows = in_result.fetchall()
            in_ids = [r.type_id for r in in_rows]
            in_names = await sde.type_ids_to_names(db, in_ids) if in_ids else {}
            inputs = [
                {"type_id": r.type_id, "name": in_names.get(r.type_id, f"Type {r.type_id}"), "quantity": r.quantity}
                for r in in_rows
            ]
    except Exception:
        pass

    # Find schematics that CONSUME this commodity (where this is_input=True) — downstream uses
    uses: list[dict] = []
    try:
        use_result = await db.execute(
            select(SDEPlanetSchematicMaterial.schematic_id)
            .where(
                SDEPlanetSchematicMaterial.type_id == type_id,
                SDEPlanetSchematicMaterial.is_input == True,
            )
        )
        use_sch_ids = [r.schematic_id for r in use_result.fetchall()]
        if use_sch_ids:
            out_result = await db.execute(
                select(SDEPlanetSchematicMaterial.schematic_id, SDEPlanetSchematicMaterial.type_id)
                .where(
                    SDEPlanetSchematicMaterial.schematic_id.in_(use_sch_ids),
                    SDEPlanetSchematicMaterial.is_input == False,
                )
            )
            out_ids = list({r.type_id for r in out_result.fetchall()})
            out_names = await sde.type_ids_to_names(db, out_ids) if out_ids else {}
            uses = [
                {"type_id": tid, "name": out_names.get(tid, f"Type {tid}")}
                for tid in sorted(out_ids, key=lambda i: out_names.get(i, ""))
            ]
    except Exception:
        pass

    tier = await _compute_tier(db, type_id)
    planet_types = None
    if tier == 0:
        planet_types = [ptype for ptype, mats in pi_const.P0_BY_PLANET_TYPE.items() if name in mats]

    price_map = await _get_pi_prices(db, {type_id} | {inp["type_id"] for inp in inputs})
    price = price_map.get(type_id, 0.0)
    input_cost = sum((price_map.get(inp["type_id"], 0.0) or 0.0) * (inp["quantity"] or 0) for inp in inputs)
    revenue_per_cycle = (price or 0.0) * (output_qty or 0)
    margin_per_cycle = revenue_per_cycle - input_cost if inputs else None

    return templates.TemplateResponse("partials/planetary_chain_node.html", {
        "request": request,
        "type_id": type_id,
        "name": name or f"Type {type_id}",
        "tier": tier,
        "planet_types": planet_types,
        "inputs": inputs,
        "uses": uses,
        "cycle_time": cycle_time,
        "output_qty": output_qty,
        "price": price,
        "input_cost": input_cost,
        "revenue_per_cycle": revenue_per_cycle,
        "margin_per_cycle": margin_per_cycle,
    })


# ── Chain builders & pricing ─────────────────────────────────────────────────

async def _compute_tier(db: AsyncSession, type_id: int) -> int:
    """Walk schematic recipes to compute tier (0=P0...4=P4). Cached in-memory."""
    if type_id in pi_const.TIER_FOR_TYPE and pi_const.TIER_FOR_TYPE[type_id] == 0:
        return 0
    from app.db.sde_models import SDEPlanetSchematicMaterial
    try:
        # Does anything produce this type?
        out_result = await db.execute(
            select(SDEPlanetSchematicMaterial.schematic_id, SDEPlanetSchematicMaterial.quantity)
            .where(
                SDEPlanetSchematicMaterial.type_id == type_id,
                SDEPlanetSchematicMaterial.is_input == False,
            )
            .limit(1)
        )
        row = out_result.fetchone()
        if not row:
            return 0
        # Get the schematic's inputs and recurse
        in_result = await db.execute(
            select(SDEPlanetSchematicMaterial.type_id)
            .where(
                SDEPlanetSchematicMaterial.schematic_id == row.schematic_id,
                SDEPlanetSchematicMaterial.is_input == True,
            )
        )
        in_ids = [r.type_id for r in in_result.fetchall()]
        if not in_ids:
            return 1
        input_tiers = [await _compute_tier(db, tid) for tid in in_ids]
        return min(max(input_tiers) + 1, 4)
    except Exception:
        return 0


async def _build_chain_tiers(db: AsyncSession) -> dict[int, list[dict]]:
    """Group all PI commodities by tier, pulling names + schematics from SDE."""
    from app.db.sde_models import SDEPlanetSchematic, SDEPlanetSchematicMaterial

    # Collect all type IDs that appear on either side of any schematic
    try:
        mat_result = await db.execute(select(SDEPlanetSchematicMaterial))
        mats = list(mat_result.scalars().all())
    except Exception:
        mats = []

    schematic_input_ids: set[int] = set()
    schematic_output_ids: set[int] = set()
    input_map: dict[int, list[tuple[int, int]]] = {}   # schematic_id -> [(type_id, qty)]
    output_map: dict[int, tuple[int, int]] = {}        # schematic_id -> (type_id, qty)
    for m in mats:
        if m.is_input:
            schematic_input_ids.add(m.type_id)
            input_map.setdefault(m.schematic_id, []).append((m.type_id, m.quantity))
        else:
            schematic_output_ids.add(m.type_id)
            output_map[m.schematic_id] = (m.type_id, m.quantity)

    # All PI-related type IDs we care about
    all_ids = schematic_input_ids | schematic_output_ids | set(pi_const.P0_TYPE_IDS.values())
    if not all_ids:
        return {0: [], 1: [], 2: [], 3: [], 4: []}

    name_map = await sde.type_ids_to_names(db, list(all_ids))
    schematics_by_output: dict[int, SDEPlanetSchematic] = {}
    try:
        sch_result = await db.execute(select(SDEPlanetSchematic))
        for sch in sch_result.scalars().all():
            out = output_map.get(sch.schematic_id)
            if out:
                schematics_by_output[out[0]] = sch
    except Exception:
        pass

    # Derive tier for every type by recipe depth from P0 leaves.
    # Iteratively: a commodity's tier = max(input tier) + 1. Converges in ≤4 passes.
    tier_of: dict[int, int] = dict(pi_const.TIER_FOR_TYPE)  # seed with P0s
    for tid in all_ids:
        if tid not in schematic_output_ids:
            tier_of.setdefault(tid, 0)  # anything not produced by a schematic is a leaf

    for _ in range(6):  # 5 tiers max, one extra to be safe
        changed = False
        for sch_id, (out_tid, _qty) in output_map.items():
            in_tiers = [tier_of.get(in_tid, 0) for in_tid, _ in input_map.get(sch_id, [])]
            if not in_tiers:
                continue
            new_tier = min(max(in_tiers) + 1, 4)
            if tier_of.get(out_tid) != new_tier:
                tier_of[out_tid] = new_tier
                changed = True
        if not changed:
            break

    chain: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    for tid in all_ids:
        tier = tier_of.get(tid, 0)
        if tier not in chain:
            continue
        entry = {
            "type_id": tid,
            "name": name_map.get(tid, f"Type {tid}"),
            "inputs": [],
            "output_qty": None,
            "cycle_time": None,
        }
        # Find a schematic that produces this type (if any)
        sch = schematics_by_output.get(tid)
        if sch:
            entry["schematic_id"] = sch.schematic_id
            entry["cycle_time"] = sch.cycle_time
            out_tuple = output_map.get(sch.schematic_id)
            if out_tuple:
                entry["output_qty"] = out_tuple[1]
            for in_type_id, in_qty in input_map.get(sch.schematic_id, []):
                entry["inputs"].append({
                    "type_id": in_type_id,
                    "name": name_map.get(in_type_id, f"Type {in_type_id}"),
                    "quantity": in_qty,
                })
        chain[tier].append(entry)

    # Alpha sort within each tier
    for tier in chain:
        chain[tier].sort(key=lambda x: x["name"])

    return chain


# Cached price map to avoid re-hitting ESI for every partial load.
_PRICE_CACHE: dict[int, float] = {}
_PRICE_CACHE_TS: datetime | None = None
_PRICE_CACHE_TTL = 900  # 15 min


async def _get_pi_prices(db: AsyncSession, type_ids: set[int]) -> dict[int, float]:
    """Fetch ESI average prices for the given type_ids. Cached for 15min."""
    global _PRICE_CACHE, _PRICE_CACHE_TS
    now = datetime.now(timezone.utc)
    stale = _PRICE_CACHE_TS is None or (now - _PRICE_CACHE_TS).total_seconds() > _PRICE_CACHE_TTL
    if stale:
        try:
            client = ESIClient("", db=db)
            all_prices = await esi_market.get_market_prices(client)
            _PRICE_CACHE = {
                p["type_id"]: (p.get("average_price") or p.get("adjusted_price") or 0.0)
                for p in all_prices if p.get("type_id")
            }
            _PRICE_CACHE_TS = now
        except Exception as e:
            logger.warning("PI price fetch failed: %s", e)
    return {tid: _PRICE_CACHE.get(tid, 0.0) for tid in type_ids}
