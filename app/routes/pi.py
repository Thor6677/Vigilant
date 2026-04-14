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

    # Pull planets + region_id from SDE — region_id drives the wormhole flag.
    from app.db.sde_models import SDEPlanet, SDESystem
    try:
        result = await db.execute(
            select(SDEPlanet).where(SDEPlanet.system_id == sys_id).order_by(SDEPlanet.planet_index)
        )
        planets = list(result.scalars().all())
    except Exception:
        planets = []

    rid_result = await db.execute(select(SDESystem.region_id).where(SDESystem.system_id == sys_id))
    region_id = rid_result.scalar_one_or_none()
    space_type = _space_type(region_id, sys_info.get("security") if sys_info else None)

    rendered_planets = []
    system_p0_names: set[str] = set()
    for p in planets:
        ptype = pi_const.PLANET_TYPE_NAMES.get(p.planet_type_id, f"type {p.planet_type_id}")
        p0_list = pi_const.P0_BY_PLANET_TYPE.get(ptype.lower(), [])
        system_p0_names.update(p0_list)
        rendered_planets.append({
            "planet_id": p.planet_id,
            "planet_name": p.planet_name,
            "planet_type": ptype,
            "planet_index": p.planet_index,
            "p0_materials": p0_list,
        })

    # Full-tier producibility view: every PI commodity P0-P4, flagged as
    # producible or not based on this system's combined P0 pool.
    tier_view = await _tier_view_for_system(db, system_p0_names)

    return templates.TemplateResponse("partials/planetary_lookup_system.html", {
        "request": request,
        "system": sys_info,
        "planets": rendered_planets,
        "no_sde": not planets,
        "all_tiers": tier_view["all_tiers"],
        "tier_counts": tier_view["counts"],
        "system_p0_names": sorted(system_p0_names),
        "space_type": space_type,
    })


def _space_type(region_id: int | None, security: float | None) -> str:
    """Classify a system as highsec / lowsec / nullsec / wormhole / pochven.

    Region ID 11000000+ is CCP's w-space range (wormhole regions).
    Pochven = region 10000070 (Triglavian, behaves like low-/null- for PI).
    """
    if region_id is not None and region_id >= 11000000:
        return "wormhole"
    if region_id == 10000070:
        return "pochven"
    if security is None:
        return "unknown"
    if security >= 0.5:
        return "highsec"
    if security > 0.0:
        return "lowsec"
    return "nullsec"


async def _load_schematic_graph(db: AsyncSession) -> dict | None:
    """Load the full PI schematic graph once. Shared by _tier_view_for_system
    and _expand_bom so they stay in lock-step on canonical recipes and tiers.

    Returns None if the SDE PI tables aren't populated yet.

    Shape:
      {
        "input_map":           {schematic_id: [(in_type_id, qty), ...]},
        "output_map":          {schematic_id: (out_type_id, qty)},
        "schematics":          {schematic_id: SDEPlanetSchematic row},
        "canonical_schematic": {type_id: schematic_id that produces it (first-seen)},
        "name_map":            {type_id: human name},
        "all_ids":             set of every type_id appearing in PI (P0..P4),
        "tier_of":             {type_id: 0..4},
      }
    """
    from app.db.sde_models import SDEPlanetSchematic, SDEPlanetSchematicMaterial
    try:
        mat_result = await db.execute(select(SDEPlanetSchematicMaterial))
        mats = list(mat_result.scalars().all())
        sch_result = await db.execute(select(SDEPlanetSchematic))
        schematics = {s.schematic_id: s for s in sch_result.scalars().all()}
    except Exception:
        return None
    if not mats:
        return None

    input_map: dict[int, list[tuple[int, int]]] = {}
    output_map: dict[int, tuple[int, int]] = {}
    for m in mats:
        if m.is_input:
            input_map.setdefault(m.schematic_id, []).append((m.type_id, m.quantity))
        else:
            output_map[m.schematic_id] = (m.type_id, m.quantity)

    all_ids: set[int] = set(pi_const.P0_TYPE_IDS.values())
    for _sch_id, (out_tid, _qty) in output_map.items():
        all_ids.add(out_tid)
    for _sch_id, ins in input_map.items():
        for tid, _ in ins:
            all_ids.add(tid)

    produced_set: set[int] = {t for (t, _) in output_map.values()}

    # Absolute tier: P0 = 0, else 1 + max(input tiers). Converges in ≤4 passes.
    tier_of: dict[int, int] = {tid: 0 for tid in pi_const.P0_TYPE_IDS.values()}
    for tid in all_ids:
        if tid not in produced_set:
            tier_of.setdefault(tid, 0)
    for _ in range(6):
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

    # Canonical schematic per output (first seen wins). Each PI commodity has
    # exactly one producing recipe in current EVE, but be defensive.
    canonical_schematic: dict[int, int] = {}
    for sch_id, (out_tid, _qty) in output_map.items():
        canonical_schematic.setdefault(out_tid, sch_id)

    name_map = await sde.type_ids_to_names(db, list(all_ids))

    return {
        "input_map": input_map,
        "output_map": output_map,
        "schematics": schematics,
        "canonical_schematic": canonical_schematic,
        "name_map": name_map,
        "all_ids": all_ids,
        "tier_of": tier_of,
    }


async def _tier_view_for_system(db: AsyncSession, p0_names_in_system: set[str]) -> dict:
    """Build the full PI commodity universe with producibility flags for one system.

    Returns:
        {
          "all_tiers": {0..4: [{type_id, name, producible, inputs, missing,
                                cycle_time, output_qty, planet_sources}]},
          "counts":    {0..4: {total, producible}},
        }

    Every PI commodity P0-P4 is included; `producible=True` means the full input
    chain is satisfiable from the given P0 pool. `missing` lists the direct
    input names blocking production (only set when not producible).
    """
    # System P0 type_ids (subset of the 15 known P0 names)
    system_p0_ids: set[int] = {
        tid for name, tid in pi_const.P0_TYPE_IDS.items() if name in p0_names_in_system
    }

    graph = await _load_schematic_graph(db)
    if graph is None:
        return {"all_tiers": {0: [], 1: [], 2: [], 3: [], 4: []}, "counts": {t: {"total": 0, "producible": 0} for t in range(5)}}

    input_map = graph["input_map"]
    output_map = graph["output_map"]
    schematics = graph["schematics"]
    all_ids = graph["all_ids"]
    tier_of = graph["tier_of"]
    canonical_schematic = graph["canonical_schematic"]
    name_map = graph["name_map"]

    # Producibility BFS: seeded with the system's P0s
    producible: set[int] = set(system_p0_ids)
    producing_schematic: dict[int, int] = {}
    if system_p0_ids:
        for _ in range(6):
            changed = False
            for sch_id, (out_tid, _qty) in output_map.items():
                if out_tid in producible:
                    continue
                inputs = input_map.get(sch_id, [])
                if not inputs:
                    continue
                if all(in_tid in producible for in_tid, _ in inputs):
                    producible.add(out_tid)
                    producing_schematic[out_tid] = sch_id
                    changed = True
            if not changed:
                break

    # ── Graph relationships for hover highlighting ──
    # direct_inputs[tid]: immediate recipe ingredients
    # uses_of[tid]: schematics where tid is an input (reverse edge)
    direct_inputs: dict[int, set[int]] = {}
    for sch_id, (out_tid, _qty) in output_map.items():
        direct_inputs.setdefault(out_tid, set())
        for in_tid, _q in input_map.get(sch_id, []):
            direct_inputs[out_tid].add(in_tid)

    uses_of: dict[int, set[int]] = {}  # input_tid -> set of schematic_ids that use it
    for sch_id, ins in input_map.items():
        for in_tid, _q in ins:
            uses_of.setdefault(in_tid, set()).add(sch_id)

    # Transitive ancestors (all materials needed to produce tid, recursively)
    def _ancestors(tid: int) -> set[int]:
        seen: set[int] = set()
        stack = list(direct_inputs.get(tid, set()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(direct_inputs.get(cur, set()) - seen)
        return seen

    # Transitive descendants (every commodity that can be built using tid)
    def _descendants(tid: int) -> set[int]:
        seen: set[int] = set()
        stack: list[int] = []
        for sch_id in uses_of.get(tid, set()):
            out_tid, _ = output_map.get(sch_id, (None, None))
            if out_tid is not None:
                stack.append(out_tid)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for sch_id in uses_of.get(cur, set()):
                out_tid, _ = output_map.get(sch_id, (None, None))
                if out_tid is not None and out_tid not in seen:
                    stack.append(out_tid)
        return seen

    all_tiers: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    for tid in all_ids:
        tier = tier_of.get(tid, 0)
        if tier not in all_tiers:
            continue
        sch_id = canonical_schematic.get(tid)
        sch = schematics.get(sch_id) if sch_id else None
        inputs = []
        if sch_id:
            inputs = [
                {
                    "type_id": in_tid,
                    "name": name_map.get(in_tid, f"Type {in_tid}"),
                    "quantity": qty,
                }
                for in_tid, qty in input_map.get(sch_id, [])
            ]
        # Which direct inputs are blocking production?
        missing = [
            inp["name"] for inp in inputs if inp["type_id"] not in producible
        ] if (system_p0_ids and tid not in producible) else []

        # For P0 commodities, list the planet types that produce them
        planet_sources = None
        name = name_map.get(tid, f"Type {tid}")
        if tier == 0:
            planet_sources = [
                ptype for ptype, mats_list in pi_const.P0_BY_PLANET_TYPE.items()
                if name in mats_list
            ]

        direct_in = sorted(direct_inputs.get(tid, set()))
        ancestors = sorted(_ancestors(tid))
        descendants = sorted(_descendants(tid))

        all_tiers[tier].append({
            "type_id": tid,
            "name": name,
            "producible": tid in producible,
            "inputs": inputs,
            "missing": missing,
            "cycle_time": sch.cycle_time if sch else None,
            "output_qty": output_map.get(sch_id, (None, None))[1] if sch_id else None,
            "planet_sources": planet_sources,
            "direct_input_ids": direct_in,
            "ancestor_ids": ancestors,
            "descendant_ids": descendants,
        })

    for t in all_tiers:
        all_tiers[t].sort(key=lambda x: x["name"])

    counts = {
        t: {
            "total": len(items),
            "producible": sum(1 for i in items if i["producible"]),
        }
        for t, items in all_tiers.items()
    }

    return {"all_tiers": all_tiers, "counts": counts}


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


# ── Phase 4: /industry/planetary/calculator ───────────────────────────────────

@router.get("/industry/planetary/calculator", response_class=HTMLResponse)
async def planetary_calculator_page(
    request: Request,
    target: int | None = Query(None),
    system: str | None = Query(None),
    cycles: int = Query(1, ge=1, le=10000),
    max_chars: int | None = Query(None, ge=1, le=50),
    ipc: int = Query(5, ge=0, le=5),  # Interplanetary Consolidation level
    ccu: int = Query(5, ge=0, le=5),  # Command Center Upgrades level
    db: AsyncSession = Depends(get_db),
):
    """BOM calculator — pick a target PI product + optional system, see the
    full chain expansion with per-tier factory counts, P0 totals, ISK margins,
    and flags for any P0 the selected system can't produce locally.

    Skill overrides:
      - `ipc` (0-5) = Interplanetary Consolidation level → planets per character
        (1 + ipc, i.e. IPC V → 6 planets, IPC 0 → 1 planet).
      - `ccu` (0-5) = Command Center Upgrades level (currently baseline V; lower
        reduces factories-per-hub, but v1 only surfaces it in UI).
      - `max_chars` caps total character count — when below optimal, shows a
        warning with shortfall details.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    graph = await _load_schematic_graph(db)

    # Build the product picker options grouped by tier
    product_options: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    if graph:
        for tid in graph["all_ids"]:
            t = graph["tier_of"].get(tid, 0)
            if 1 <= t <= 4:
                product_options[t].append({
                    "type_id": tid,
                    "name": graph["name_map"].get(tid, f"Type {tid}"),
                })
        for t in product_options:
            product_options[t].sort(key=lambda x: x["name"])

    # Resolve system context if provided
    system_info = None
    system_p0_names: set[str] = set()
    system_planet_types: set[str] = set()  # capitalized names: {"Barren", "Gas", ...}
    system_planets: list[dict] = []        # ordered physical planets in the system
    space_type = None
    if system:
        sys_id = await sde.system_name_to_id(db, system)
        if sys_id:
            system_info = await sde.system_info(db, sys_id)
            from app.db.sde_models import SDEPlanet, SDESystem
            rid_result = await db.execute(select(SDESystem.region_id).where(SDESystem.system_id == sys_id))
            region_id = rid_result.scalar_one_or_none()
            space_type = _space_type(region_id, system_info.get("security") if system_info else None)
            planet_result = await db.execute(
                select(SDEPlanet).where(SDEPlanet.system_id == sys_id).order_by(SDEPlanet.planet_index)
            )
            for p in planet_result.scalars().all():
                ptype = pi_const.PLANET_TYPE_NAMES.get(p.planet_type_id, "")
                if not ptype:
                    continue
                system_planet_types.add(ptype)
                for mat in pi_const.P0_BY_PLANET_TYPE.get(ptype.lower(), []):
                    system_p0_names.add(mat)
                system_planets.append({
                    "planet_id": p.planet_id,
                    "planet_name": p.planet_name,
                    "planet_type": ptype,
                    "planet_index": p.planet_index,
                })

    system_p0_ids: set[int] = {
        tid for name, tid in pi_const.P0_TYPE_IDS.items() if name in system_p0_names
    }

    # Run the BOM if the user picked a target
    bom = None
    colony_plan = None
    if graph and target and target in graph["all_ids"]:
        bom = _expand_bom(graph, target, cycles, system_p0_ids)
        # Enrich with ISK economics
        price_type_ids = set(bom["p0_totals"].keys()) | {target}
        for tier_items in bom["tier_rows"].values():
            for row in tier_items:
                price_type_ids.add(row["type_id"])
        prices = await _get_pi_prices(db, price_type_ids)
        bom["prices"] = prices
        p0_cost = sum((prices.get(tid, 0.0) or 0.0) * data["qty"]
                      for tid, data in bom["p0_totals"].items())
        target_price = prices.get(target, 0.0) or 0.0
        revenue = target_price * bom["total_target_output"]
        bom["p0_input_cost"] = p0_cost
        bom["revenue"] = revenue
        bom["margin"] = revenue - p0_cost
        bom["margin_pct"] = (revenue - p0_cost) / p0_cost * 100 if p0_cost > 0 else None

        # Colony layout recommendation (legacy flat view, retained for backward-
        # compat of debug/dev paths; the template renders the per-character
        # view in its place).
        colony_plan = _plan_colonies(bom, system_p0_names, system_planet_types)

        # Per-character plan: assigns concrete system planets to specific
        # roles, grouped per character. Respects max_chars + ipc overrides.
        planets_per_char = max(1, 1 + ipc)  # IPC V = 6, IPC 0 = 1
        character_plan = _plan_characters(
            bom, graph, system_planets, system_planet_types, system_p0_ids,
            max_chars=max_chars,
            planets_per_char=planets_per_char,
        )

    return templates.TemplateResponse("planetary_calculator.html", {
        "request": request,
        "target": target,
        "system": system,
        "cycles": cycles,
        "max_chars": max_chars,
        "ipc": ipc,
        "ccu": ccu,
        "planets_per_char": max(1, 1 + ipc),
        "product_options": product_options,
        "bom": bom,
        "colony_plan": colony_plan,
        "character_plan": character_plan if bom else None,
        "system_info": system_info,
        "space_type": space_type,
        "system_p0_names": sorted(system_p0_names),
        "no_sde": graph is None,
        "tier_names": {0: "P0 · Raw", 1: "P1 · Basic", 2: "P2 · Basic",
                       3: "P3 · Specialized", 4: "P4 · Advanced"},
    })


def _expand_bom(graph: dict, target_tid: int, target_cycles: int,
                system_p0_ids: set[int]) -> dict:
    """Recursively expand target product into its full P0 requirements.

    Intermediate cycle counts are fractional (no rounding loss on raw-material
    totals). Factory counts use ceil(demand_rate / factory_rate) with each
    schematic's own cycle time so mixed-tier timing (e.g. 30min P1 vs 60min P2)
    computes correctly.

    Returns:
      {
        "target":              {type_id, name, cycles, output_qty, total_output},
        "target_cycle_time":   seconds,
        "total_target_output": int,
        "tier_rows":           {tier: [{type_id, name, qty, cycles, factories,
                                        cycle_time, output_qty, direct_inputs}]},
        "p0_totals":           {type_id: {name, qty, producible, planet_sources}},
        "total_p0_volume":     int (sum across all P0 demand, rounded),
      }
    """
    input_map = graph["input_map"]
    output_map = graph["output_map"]
    schematics = graph["schematics"]
    canonical = graph["canonical_schematic"]
    name_map = graph["name_map"]
    tier_of = graph["tier_of"]

    target_sch = canonical.get(target_tid)
    target_sch_row = schematics.get(target_sch) if target_sch else None
    target_cycle_time = target_sch_row.cycle_time if target_sch_row else 3600
    _t_out_tid, target_output_qty = output_map.get(target_sch, (target_tid, 1))
    total_target_output = target_output_qty * target_cycles

    # demand[tid] = total qty of tid needed (float; fractional if intermediate)
    # cycles_of[tid] = total fractional production cycles required of tid
    demand: dict[int, float] = {}
    cycles_of: dict[int, float] = {target_tid: target_cycles}

    # BFS down the recipe graph. Use queue semantics so we visit each commodity's
    # contribution once; dedupe by accumulating demand additively.
    queue: list[tuple[int, float]] = [(target_tid, float(target_cycles))]
    while queue:
        tid, cycles_needed = queue.pop(0)
        sch_id = canonical.get(tid)
        if sch_id is None:
            continue  # P0 leaf — no recipe to expand
        for in_tid, in_qty in input_map.get(sch_id, []):
            qty_needed = in_qty * cycles_needed
            demand[in_tid] = demand.get(in_tid, 0.0) + qty_needed
            in_sch = canonical.get(in_tid)
            if in_sch is not None:
                _, in_out_qty = output_map.get(in_sch, (in_tid, 1))
                sub_cycles = qty_needed / in_out_qty if in_out_qty else 0
                cycles_of[in_tid] = cycles_of.get(in_tid, 0.0) + sub_cycles
                queue.append((in_tid, sub_cycles))

    # Per-tier rows (P1..P4 intermediates)
    import math
    tier_rows: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    seen_ids: set[int] = set()
    for tid, qty in demand.items():
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        tier = tier_of.get(tid, 0)
        sch_id = canonical.get(tid)
        sch_row = schematics.get(sch_id) if sch_id else None
        cycle_time = sch_row.cycle_time if sch_row else None
        _, out_qty = output_map.get(sch_id, (tid, 1)) if sch_id else (tid, 1)
        item_cycles = cycles_of.get(tid, 0.0)

        # Factory count: sustain the target's production rate (1 target cycle per
        # target_cycle_time) regardless of `target_cycles` — N cycles is a BATCH
        # metric for P0 volumes but factories scale to the per-cycle rate so the
        # same colony supports longer runs. Using (cycles * target_cycle_time) as
        # the time window normalises this.
        factories = 0
        if cycle_time and out_qty and target_cycle_time:
            time_window = max(1, target_cycles) * target_cycle_time
            demand_rate = qty / time_window              # units/sec at sustained rate
            factory_rate = out_qty / cycle_time          # units/sec per factory
            if factory_rate > 0:
                factories = max(1, math.ceil(demand_rate / factory_rate))

        direct_inputs = []
        if sch_id:
            for in_tid, in_qty in input_map.get(sch_id, []):
                direct_inputs.append({
                    "type_id": in_tid,
                    "name": name_map.get(in_tid, f"Type {in_tid}"),
                    "per_cycle": in_qty,
                })

        tier_rows[tier].append({
            "type_id": tid,
            "name": name_map.get(tid, f"Type {tid}"),
            "qty": qty,
            "cycles": item_cycles,
            "factories": factories,
            "cycle_time": cycle_time,
            "output_qty": out_qty,
            "direct_inputs": direct_inputs,
        })

    for t in tier_rows:
        tier_rows[t].sort(key=lambda x: x["name"])

    # P0 totals with system availability
    p0_totals: dict[int, dict] = {}
    for tid, qty in demand.items():
        if tier_of.get(tid, -1) == 0:
            name = name_map.get(tid, f"Type {tid}")
            producible = (tid in system_p0_ids) if system_p0_ids else True
            planet_sources = [
                ptype for ptype, mats in pi_const.P0_BY_PLANET_TYPE.items()
                if name in mats
            ]
            p0_totals[tid] = {
                "name": name,
                "qty": qty,
                "producible": producible,
                "planet_sources": planet_sources,
            }

    total_p0_volume = sum(int(round(d["qty"])) for d in p0_totals.values())

    # Pre-sorted list view: highest qty first, with the type_id available.
    p0_sorted = [
        dict(type_id=tid, **data)
        for tid, data in sorted(p0_totals.items(), key=lambda kv: -kv[1]["qty"])
    ]
    missing_count = sum(1 for d in p0_totals.values() if not d["producible"])

    return {
        "target": {
            "type_id": target_tid,
            "name": name_map.get(target_tid, f"Type {target_tid}"),
            "cycles": target_cycles,
            "output_qty": target_output_qty,
            "total_output": total_target_output,
            "tier": tier_of.get(target_tid, 0),
        },
        "target_cycle_time": target_cycle_time,
        "total_target_output": total_target_output,
        "tier_rows": tier_rows,
        "p0_totals": p0_totals,
        "p0_sorted": p0_sorted,
        "missing_p0_count": missing_count,
        "total_p0_volume": total_p0_volume,
    }


# ── Colony planner ────────────────────────────────────────────────────────────

# Community-standard template constants. Sourced from:
#   - DalShooth EVE_PI_Templates ("Single Factory P2/P3" = 12 AIFs on CCU V,
#     "Single P4 Factory" = 8 HTPPs, "Miner + P1 Factory" = 1-2 ECU + P1 BIF).
#   - EVE-Uni wiki Planetary_buildings (CCU V budget = 25,415 CPU / 19,000 PG;
#     ECU = 400/2600 + 110/550 per head, BIF = 200/800, AIF = 500/700,
#     HTPP = 1100/400, Launchpad = 3600/700).
#   - EVE-Uni Planetary_Industry: BIF outputs 20 P1 per 30 min (40 P1/hr).
#
# Yield per ECU varies 10k-40k P0/hr depending on security status, planet
# richness, and skills — 15k/hr is a balanced midpoint (~6 heads in null/WH
# or 10 heads in highsec). The calculator uses it only for the text footnote;
# actual ECU count derives from P1 slot count (one ECU per P1 slot).
ECU_YIELD_PER_HOUR = 15_000          # P0 units/hr per ECU (balanced avg)
P1_FACTORY_THROUGHPUT_PER_HOUR = 40  # P1 units/hr per Basic Industrial Facility (SDE)
MINER_P1_SLOTS_PER_PLANET = 2        # 1 slot = 1 ECU + 1 P1 factory on an integrated planet
P2_P3_FACTORIES_PER_PLANET = 12      # AIFs per shared P2/P3 hub (DalShooth single-factory)


def _pack_items_into_hubs(items: list[dict], hubs: list[dict]) -> None:
    """Pack `items` (each {type_id, name, tier, factories}) sequentially into
    `hubs` using each hub's own `factory_count` as capacity. Appends to each
    hub's `factories: [{type_id, name, tier, count}]` list in place.

    A single item can span hubs if its factory count exceeds the current hub's
    remaining capacity.
    """
    for hub in hubs:
        hub.setdefault("factories", [])
    caps = [(h, h.get("factory_count", 0)) for h in hubs]
    hub_idx = 0
    current_hub, current_capacity = caps[0] if caps else (None, 0)
    for item in items:
        remaining = item["factories"]
        while remaining > 0 and current_hub is not None:
            if current_capacity <= 0:
                hub_idx += 1
                if hub_idx >= len(caps):
                    return
                current_hub, current_capacity = caps[hub_idx]
                continue
            take = min(remaining, current_capacity)
            current_hub["factories"].append({
                "type_id": item["type_id"],
                "name": item["name"],
                "tier": item["tier"],
                "count": take,
            })
            remaining -= take
            current_capacity -= take


def _distribute_factory_items(bom: dict, colonies: list[dict]) -> None:
    """Assign specific P2/P3/P4 items (with factory counts) to each hub colony.

    Mutates colonies in-place, adding `factories: [{type_id, name, tier, count}]`
    to each p2_p3_factory and p4_factory colony. Greedy fattest-first packs
    items into hubs so a single product's factories cluster on one planet — this
    minimizes cross-character fan-out in the downstream flow graph.
    """
    # P2/P3 hubs hold items from tier_rows[2] + tier_rows[3]
    p2_p3_hubs = [c for c in colonies if c["role"] == "p2_p3_factory"]
    if p2_p3_hubs:
        items = []
        for tier in (2, 3):
            for r in bom.get("tier_rows", {}).get(tier, []):
                if r.get("factories", 0) > 0:
                    items.append({
                        "type_id": r["type_id"],
                        "name": r["name"],
                        "tier": tier,
                        "factories": r["factories"],
                    })
        items.sort(key=lambda it: -it["factories"])
        _pack_items_into_hubs(items, p2_p3_hubs)

    # P4 hubs hold items from tier_rows[4]
    p4_hubs = [c for c in colonies if c["role"] == "p4_factory"]
    if p4_hubs:
        items = []
        for r in bom.get("tier_rows", {}).get(4, []):
            if r.get("factories", 0) > 0:
                items.append({
                    "type_id": r["type_id"],
                    "name": r["name"],
                    "tier": 4,
                    "factories": r["factories"],
                })
        items.sort(key=lambda it: -it["factories"])
        _pack_items_into_hubs(items, p4_hubs)
P4_FACTORIES_PER_PLANET = 8          # HTPPs per P4 hub (DalShooth single-factory)


def _plan_colonies(bom: dict, system_p0_names: set[str],
                   system_planet_types: set[str] | None = None,
                   include_target_factory: bool = True) -> dict:
    """Recommend a colony layout for the BOM's production rate.

    Strategy (per user preference — integrated miner+P1):
      - Each needed P1 spawns `factories_needed` "slots".
      - Each slot = 1 ECU (extract the P0 input) + 1 P1 factory.
      - Slots are assigned to a planet type preferring local availability, then
        planet types that can host the widest variety of needed P0s.
      - Slots of the same planet type are packed 2-per-planet (CCU V budget).
        Within a planet, different P0s are interleaved so the layout shows
        2 distinct extractor programs where possible (not same P0 twice).
      - P2/P3 factories share dedicated "advanced factory hub" planets (6 AIFs).
      - P4 factories live on their own "P4 factory hub" planets (2 HTPPs).
      - Hub planet type defaults to Barren (falls back to Temperate) — both
        are universally common and have no extraction relevance for hubs.

    `system_planet_types` is the authoritative set of planet types actually
    present in the system (e.g. {"Barren","Gas","Lava","Temperate"}). If not
    provided, falls back to inferring from P0 overlap (less accurate).
    """
    import math
    from collections import Counter, defaultdict

    # Planet types the system supports (capitalized)
    if system_planet_types is None:
        system_planet_types = set()
        for ptype, mats in pi_const.P0_BY_PLANET_TYPE.items():
            if any(m in system_p0_names for m in mats):
                system_planet_types.add(ptype.capitalize())
    else:
        # Normalize capitalization
        system_planet_types = {t.capitalize() for t in system_planet_types}

    def _sources_for(p0_name: str) -> list[str]:
        return [pt.capitalize() for pt, mats in pi_const.P0_BY_PLANET_TYPE.items()
                if p0_name in mats]

    target_cycle_time = bom.get("target_cycle_time") or 3600
    target_cycles = max(1, bom.get("target", {}).get("cycles", 1))

    # Step 1 — Build miner+P1 slots from the P1 rows in the BOM plus
    # (if the target itself is P1) the target's own factory needs.
    slots: list[dict] = []
    for p1_row in bom["tier_rows"].get(1, []):
        if not p1_row.get("direct_inputs"):
            continue
        p0 = p1_row["direct_inputs"][0]
        # Rate is normalised by the full run window so N cycles just extend the
        # run instead of inflating factory/ECU counts.
        rate_per_hour = p1_row["qty"] / (target_cycles * target_cycle_time) * 3600
        factories = max(1, math.ceil(rate_per_hour / P1_FACTORY_THROUGHPUT_PER_HOUR))
        for _ in range(factories):
            slots.append({
                "p0_name": p0["name"],
                "p0_tid": p0["type_id"],
                "p1_name": p1_row["name"],
                "p1_tid": p1_row["type_id"],
                "options": _sources_for(p0["name"]),
            })

    # If the target itself is P1, it doesn't appear in tier_rows (target is
    # produced, not consumed). Synthesise one miner+P1 slot for the target.
    target = bom.get("target", {})
    if target.get("tier") == 1:
        # Resolve target's P0 input from the BOM's p0_totals (it has exactly one).
        # If multiple P0 totals exist, pick the one that matches the target's recipe
        # by falling back on P0_BY_PLANET_TYPE lookup.
        target_tid = target.get("type_id")
        target_name = target.get("name", "")
        # The sole P0 input is the one present in bom.p0_totals
        for p0_tid, p0_data in bom.get("p0_totals", {}).items():
            slots.append({
                "p0_name": p0_data["name"],
                "p0_tid": p0_tid,
                "p1_name": target_name,
                "p1_tid": target_tid,
                "options": _sources_for(p0_data["name"]),
            })
            break

    # Step 2 — Assign each slot a planet type. Priority:
    #   (a) local to the selected system,
    #   (b) highest coverage across all slots (greedy set-cover).
    type_coverage = Counter()
    for slot in slots:
        for pt in slot["options"]:
            type_coverage[pt] += 1

    for slot in slots:
        opts = slot["options"]
        if not opts:
            slot["assigned_type"] = "?"
            slot["local"] = False
            continue
        opts_sorted = sorted(
            opts,
            key=lambda t: (0 if t in system_planet_types else 1, -type_coverage[t], t),
        )
        slot["assigned_type"] = opts_sorted[0]
        slot["local"] = opts_sorted[0] in system_planet_types

    # Step 3 — Pack slots (2 per planet) by type, interleaving different P0s
    # so planets get variety rather than 2× same extractor when possible.
    by_type: dict[str, list[dict]] = defaultdict(list)
    for slot in slots:
        by_type[slot["assigned_type"]].append(slot)

    colonies: list[dict] = []
    for ptype, ptype_slots in by_type.items():
        local = ptype in system_planet_types
        # Round-robin interleave by P0 name so successive slots in the packing
        # queue are for different P0s; a planet then naturally pairs distinct
        # extractors. Trailing duplicates still pair together (unavoidable).
        by_p0: dict[str, list[dict]] = defaultdict(list)
        for s in ptype_slots:
            by_p0[s["p0_name"]].append(s)
        interleaved: list[dict] = []
        while any(by_p0.values()):
            for p0_name in list(by_p0.keys()):
                if by_p0[p0_name]:
                    interleaved.append(by_p0[p0_name].pop(0))
        for i in range(0, len(interleaved), MINER_P1_SLOTS_PER_PLANET):
            chunk = interleaved[i:i + MINER_P1_SLOTS_PER_PLANET]
            colonies.append({
                "role": "miner_p1",
                "planet_type": ptype,
                "local": local,
                "slots": [
                    {
                        "p0_name": s["p0_name"],
                        "p0_tid": s["p0_tid"],
                        "p1_name": s["p1_name"],
                        "p1_tid": s["p1_tid"],
                    }
                    for s in chunk
                ],
            })

    # Step 4 — P2/P3 factory hubs (shared). Use target if target is P2/P3.
    # Target's own factory count is rate-based: 1 factory sustains 1 target
    # cycle per target_cycle_time regardless of the user's N-cycle batch size.
    p2_factories = sum(r.get("factories", 0) for r in bom["tier_rows"].get(2, []))
    p3_factories = sum(r.get("factories", 0) for r in bom["tier_rows"].get(3, []))
    if include_target_factory and target.get("tier") == 2:
        p2_factories += 1
    elif include_target_factory and target.get("tier") == 3:
        p3_factories += 1
    mid_total = p2_factories + p3_factories

    # Hub planet type: prefer Barren → Temperate → any local → Barren.
    hub_candidates = ["Barren", "Temperate"]
    hub_type = next((t for t in hub_candidates if t in system_planet_types), hub_candidates[0])
    hub_local = hub_type in system_planet_types

    remaining = mid_total
    while remaining > 0:
        chunk = min(P2_P3_FACTORIES_PER_PLANET, remaining)
        colonies.append({
            "role": "p2_p3_factory",
            "planet_type": hub_type,
            "local": hub_local,
            "factory_count": chunk,
            "factory_tier": "P2/P3",
        })
        remaining -= chunk

    # Step 5 — P4 factory hubs (including the target itself if P4).
    # Target's own factory is rate-based — 1 factory sustains production,
    # larger N extends the run time rather than multiplying the colony.
    p4_factories = sum(r.get("factories", 0) for r in bom["tier_rows"].get(4, []))
    if include_target_factory and target.get("tier") == 4:
        p4_factories += 1

    remaining = p4_factories
    while remaining > 0:
        chunk = min(P4_FACTORIES_PER_PLANET, remaining)
        colonies.append({
            "role": "p4_factory",
            "planet_type": hub_type,
            "local": hub_local,
            "factory_count": chunk,
            "factory_tier": "P4",
        })
        remaining -= chunk

    # Assign specific P2/P3/P4 items to each hub so downstream code (per-char
    # flow chart) can label which factories live on which planet.
    _distribute_factory_items(bom, colonies)

    total_planets = len(colonies)
    # Typical max 6 PI planets per character (CCU V + Interplanetary Consolidation V)
    characters_needed = max(1, math.ceil(total_planets / 6)) if total_planets else 0

    return {
        "colonies": colonies,
        "total_planets": total_planets,
        "characters_needed": characters_needed,
        "assumptions": {
            "ecu_yield_per_hour": ECU_YIELD_PER_HOUR,
            "p1_factory_throughput": P1_FACTORY_THROUGHPUT_PER_HOUR,
            "miner_p1_slots_per_planet": MINER_P1_SLOTS_PER_PLANET,
            "p2_p3_factories_per_planet": P2_P3_FACTORIES_PER_PLANET,
            "p4_factories_per_planet": P4_FACTORIES_PER_PLANET,
        },
    }


MAX_PLANETS_PER_CHARACTER = 6   # CCU V + Interplanetary Consolidation V


def _plan_characters(bom: dict, graph: dict,
                     system_planets: list[dict],
                     system_planet_types: set[str],
                     system_p0_ids: set[int],
                     max_chars: int | None = None,
                     planets_per_char: int = MAX_PLANETS_PER_CHARACTER) -> dict:
    """Group the colony plan into per-character vertical slices with
    concrete system-planet assignments.

    Pattern (community-standard vertical-slice):
      - Each direct input of the target becomes one producer "slice":
        that producer owns the complete P0 → (tier-1) chain for its slice.
      - One assembler character owns only the target's own factory.
      - If the whole plan fits in one character (≤ MAX_PLANETS_PER_CHARACTER),
        collapse into a single 'Full chain' character.
      - Overflow (slice > cap) spawns another producer char with the same
        slice name and an [overflow] suffix.

    Planet assignment:
      - Pop planets from a free list (consumed once each). Extractor slots
      - prefer the colony's originally-chosen type; if that type is
        exhausted in the system, fall back to any other in-system type
        that can extract the same P0 (via P0_BY_PLANET_TYPE). Factory
        hubs take any leftover planet, preferring Barren/Temperate.
      - If no eligible planet remains, the slot gets `planet_name=None`
        and bumps `unassignable_slots`.

    Returns a grouped character structure — see plan file for the full shape.
    """
    import math
    from collections import defaultdict

    target = bom.get("target", {})
    target_tid = target.get("type_id")
    target_tier = target.get("tier", 0)
    target_cycles = max(1, target.get("cycles", 1))
    target_cycle_time = bom.get("target_cycle_time") or 3600
    name_map = graph["name_map"]
    input_map = graph["input_map"]
    output_map = graph["output_map"]
    canonical = graph["canonical_schematic"]

    # ── Planet pool with cross-character sharing ──────────────────────────
    # Multiple characters CAN each have their own colony on the same planet
    # (different resource patches). The only per-character constraint is that
    # one character can't have two colonies on the same planet. So each
    # planet tracks `used_by` (set of char indices) and a planet is available
    # to a given char iff that char isn't already in `used_by`. Preferred
    # over shared: we pick the globally-least-shared planet first.
    planet_pool: list[dict] = [dict(p, used_by=set()) for p in system_planets]

    def _take_planet(char_id: int, preferred_type: str | None,
                     allowed_types: set[str] | None) -> tuple[dict | None, bool, bool]:
        """Pick a planet for this character's colony slot.

        Returns (planet, preferred_hit, shared_with_others).
        Priority:
          1. Unused-by-anyone, preferred type
          2. Unused-by-anyone, fallback type
          3. Used by others (but not this char), preferred type — least-shared first
          4. Used by others (but not this char), fallback type — least-shared first
        """
        def _key(p):
            in_pref = 0 if (preferred_type and p["planet_type"] == preferred_type) else 1
            in_allowed = 0 if (allowed_types and p["planet_type"] in allowed_types) else 1
            # 0 = preferred, 1 = fallback-allowed, 2 = neither
            type_pri = 0 if in_pref == 0 else (1 if in_allowed == 0 else 2)
            return (type_pri, len(p["used_by"]), p.get("planet_index") or 0)

        cands = sorted(
            (p for p in planet_pool if char_id not in p["used_by"]),
            key=_key,
        )
        for p in cands:
            is_pref = preferred_type and p["planet_type"] == preferred_type
            is_allowed = allowed_types and p["planet_type"] in allowed_types
            if not (is_pref or is_allowed):
                continue  # this and later candidates are non-matching types
            shared = bool(p["used_by"])
            p["used_by"].add(char_id)
            return p, bool(is_pref), shared
        return None, False, False

    def _take_hub_planet(char_id: int) -> tuple[dict | None, bool]:
        """Pick a planet for a factory hub on this character.
        Prefer Barren → Temperate → any; least-shared first. Returns (planet, shared)."""
        def _key(p):
            type_pri = {"Barren": 0, "Temperate": 1}.get(p["planet_type"], 2)
            return (type_pri, len(p["used_by"]), p.get("planet_index") or 0)

        cands = sorted(
            (p for p in planet_pool if char_id not in p["used_by"]),
            key=_key,
        )
        if not cands:
            return None, False
        p = cands[0]
        shared = bool(p["used_by"])
        p["used_by"].add(char_id)
        return p, shared

    def _sources_for(p0_name: str) -> list[str]:
        return [pt.capitalize() for pt, mats in pi_const.P0_BY_PLANET_TYPE.items()
                if p0_name in mats]

    unassignable = 0

    def _colony_to_slot(char_id: int, colony: dict) -> dict:
        """Translate one `_plan_colonies` colony into a per-character slot,
        picking a physical planet that this character hasn't already used."""
        nonlocal unassignable
        role = colony["role"]
        if role == "miner_p1":
            preferred = colony["planet_type"]
            colony_p0_names = [s["p0_name"] for s in colony["slots"]]
            allowed: set[str] = set()
            if colony_p0_names:
                allowed = set.intersection(*(set(_sources_for(n)) for n in colony_p0_names))
            planet, pref_hit, shared = _take_planet(char_id, preferred, allowed)
            if planet is None:
                unassignable += 1
                return {
                    "planet_name": None,
                    "planet_type": preferred,
                    "preferred": True,
                    "shared": False,
                    "role": "miner_p1",
                    "slots": colony["slots"],
                }
            return {
                "planet_name": planet["planet_name"],
                "planet_type": planet["planet_type"],
                "preferred": pref_hit,
                "shared": shared,
                "role": "miner_p1",
                "slots": colony["slots"],
            }
        # Factory hub (p2_p3_factory or p4_factory)
        planet, shared = _take_hub_planet(char_id)
        if planet is None:
            unassignable += 1
            return {
                "planet_name": None,
                "planet_type": colony["planet_type"],
                "preferred": True,
                "shared": False,
                "role": role,
                "factory_count": colony["factory_count"],
                "factory_tier": colony["factory_tier"],
                "factories": colony.get("factories", []),
            }
        return {
            "planet_name": planet["planet_name"],
            "planet_type": planet["planet_type"],
            "preferred": True,
            "shared": shared,
            "role": role,
            "factory_count": colony["factory_count"],
            "factory_tier": colony["factory_tier"],
            "factories": colony.get("factories", []),
        }

    def _pack_colonies_into_chars(colonies: list[dict], slice_name: str | None,
                                   char_role: str) -> list[dict]:
        """Split a slice's colonies into one or more characters (≤6 planets each)."""
        chars: list[dict] = []
        # Sort extractors first, then hubs — more intuitive per-planet ordering.
        sorted_cols = sorted(
            colonies,
            key=lambda c: (0 if c["role"] == "miner_p1" else 1 if c["role"] == "p2_p3_factory" else 2,
                           c.get("planet_type", "")),
        )
        for start in range(0, len(sorted_cols), MAX_PLANETS_PER_CHARACTER):
            chunk = sorted_cols[start:start + MAX_PLANETS_PER_CHARACTER]
            overflow_idx = start // MAX_PLANETS_PER_CHARACTER
            suffix = " [overflow]" if overflow_idx > 0 else ""
            if char_role == "full_chain":
                label_tpl = "Char {n} — Full chain"
            elif char_role == "assembler":
                label_tpl = "Char {n} — Assembler"
            else:
                label_tpl = "Char {n} — " + (slice_name or "Producer") + " producer" + suffix
            chars.append({
                "label": label_tpl,  # the {n} placeholder is filled later
                "role": char_role,
                "slice_name": slice_name,
                "overflow_index": overflow_idx,
                "slots": [_colony_to_slot(c) for c in chunk],
            })
        return chars

    # ── Density-packed character plan ──────────────────────────────────
    # User workflow: haul from all characters to a central station, then
    # distribute to builders. Inter-character hauling is expected, so we
    # don't preserve vertical-slice coherence. Instead, fill every
    # character's 6 planet slots with useful work; the target's final
    # factory lands on the last character ("Final assembly").
    characters: list[dict] = []

    all_p0_names: set[str] = set()
    for p in system_planets:
        all_p0_names.update(pi_const.P0_BY_PLANET_TYPE.get(p["planet_type"].lower(), []))

    # Combined BOM colonies WITHOUT the target's own factory (we'll add a
    # dedicated colony for it so we can tag which char holds it).
    combined = _plan_colonies(
        bom, all_p0_names, system_planet_types, include_target_factory=False,
    )
    colonies: list[dict] = list(combined["colonies"])

    # Target factory as a dedicated colony. Assigned in the packing loop
    # like any other colony — cross-char sharing means the assembler char
    # always has ≥ 1 planet available unless the system has <6 planets.
    if target_tier >= 2:
        colonies.append({
            "role": "p4_factory" if target_tier == 4 else "p2_p3_factory",
            "planet_type": "Barren",
            "factory_count": 1,
            "factory_tier": "P4" if target_tier == 4 else "P2/P3",
            "factories": [
                {
                    "type_id": target_tid,
                    "name": name_map.get(target_tid, "Target"),
                    "tier": target_tier,
                    "count": 1,
                },
            ],
            "_is_target": True,
        })

    # Sort: miners first (most common), P2/P3 hubs, P4 hubs, target factory LAST
    # so the last 6-slot chunk contains it and gets the "Final assembly" label.
    def _plan_sort_key(c):
        if c.get("_is_target"):
            return (99, "")
        role = c["role"]
        if role == "miner_p1":
            return (0, c.get("planet_type", ""))
        if role == "p2_p3_factory":
            return (1, "")
        if role == "p4_factory":
            return (2, "")
        return (3, "")
    colonies.sort(key=_plan_sort_key)

    # Pack into characters (6 planets each).
    if not colonies:
        # Nothing to plan (shouldn't happen given the route checks target
        # earlier, but keep the function safe).
        return {
            "characters": [],
            "total_characters": 0,
            "total_planets_assigned": 0,
            "unassignable_slots": 0,
            "system_capacity_warning": None,
        }

    # Optimal char count respects two constraints:
    #   (a) ceil(total / planets_per_char) — per-char slot cap
    #   (b) per-type miner demand: max over types X of ceil(D_X / S_X)
    from collections import Counter as _Counter
    supply_by_type: dict[str, int] = _Counter(p["planet_type"] for p in system_planets)
    miner_demand_by_type: dict[str, int] = _Counter(
        c["planet_type"] for c in colonies
        if c["role"] == "miner_p1" and c["planet_type"] in supply_by_type
    )
    n_capacity = max(1, math.ceil(len(colonies) / planets_per_char))
    n_type = 1
    for ptype, demand in miner_demand_by_type.items():
        s = supply_by_type.get(ptype, 0)
        if s > 0:
            n_type = max(n_type, math.ceil(demand / s))

    optimal_n = max(n_capacity, n_type, 1)
    effective_n = min(optimal_n, max_chars) if (max_chars and max_chars > 0) else optimal_n
    total_chunks = max(1, effective_n)
    capacity_cap = total_chunks * planets_per_char
    is_capped = effective_n < optimal_n

    # If the char-count cap forces us to drop colonies, trim from the
    # miner list (keep hubs + target factory which have downstream
    # criticality). Collect dropped colonies so the UI can surface which
    # P1 products lose factory capacity.
    dropped_colonies: list[dict] = []
    if len(colonies) > capacity_cap:
        overflow = len(colonies) - capacity_cap
        # Re-split into priority tiers so we drop the least-critical first.
        target_cols = [c for c in colonies if c.get("_is_target")]
        hub_cols = [c for c in colonies if c["role"] in ("p2_p3_factory", "p4_factory") and not c.get("_is_target")]
        miner_cols = [c for c in colonies if c["role"] == "miner_p1"]
        # Drop miners from the end of the miner list.
        if overflow >= len(miner_cols):
            # Even dropping all miners isn't enough — drop hubs too.
            dropped_colonies = list(miner_cols)
            remaining_overflow = overflow - len(miner_cols)
            dropped_colonies.extend(hub_cols[-remaining_overflow:])
            hub_cols = hub_cols[:-remaining_overflow] if remaining_overflow > 0 else hub_cols
            miner_cols = []
        else:
            dropped_colonies = miner_cols[-overflow:]
            miner_cols = miner_cols[:-overflow]
        colonies = target_cols + hub_cols + miner_cols

    total = len(colonies)
    buckets: list[list[dict]] = [[] for _ in range(total_chunks)]
    # Target factory should land on the LAST char, so place it before
    # distributing; then everything else round-robins around it.
    target_bucket_index = total_chunks - 1
    target_colony_obj = None
    non_target_colonies = []
    for c in colonies:
        if c.get("_is_target"):
            target_colony_obj = c
        else:
            non_target_colonies.append(c)

    if target_colony_obj is not None:
        buckets[target_bucket_index].append(target_colony_obj)

    # Round-robin: step through chars in order, skipping any that are at capacity.
    # This balances type distribution while respecting the 6-planet cap.
    i = 0
    for c in non_target_colonies:
        attempts = 0
        while attempts < total_chunks:
            idx = (i + attempts) % total_chunks
            if len(buckets[idx]) < planets_per_char:
                buckets[idx].append(c)
                i = (idx + 1) % total_chunks
                break
            attempts += 1
        else:
            # All chars full — shouldn't happen given total_chunks is sized
            # to hold everything, but be defensive.
            buckets[-1].append(c)

    for char_id, bucket in enumerate(buckets):
        has_target = any(c.get("_is_target") for c in bucket)

        slots = []
        for c in bucket:
            if c.get("_is_target"):
                planet, shared = _take_hub_planet(char_id)
                if planet is None:
                    unassignable += 1
                    slots.append({
                        "planet_name": None,
                        "planet_type": c["planet_type"],
                        "preferred": True,
                        "shared": False,
                        "role": c["role"],
                        "factory_count": 1,
                        "factory_tier": c["factory_tier"],
                        "factories": c.get("factories", []),
                    })
                else:
                    slots.append({
                        "planet_name": planet["planet_name"],
                        "planet_type": planet["planet_type"],
                        "preferred": True,
                        "shared": shared,
                        "role": c["role"],
                        "factory_count": 1,
                        "factory_tier": c["factory_tier"],
                        "factories": c.get("factories", []),
                    })
            else:
                slots.append(_colony_to_slot(char_id, c))

        if has_target:
            label = "Char {n} — Final assembly"
            char_role = "assembler"
        elif total_chunks == 1:
            label = "Char {n} — Full chain"
            char_role = "full_chain"
        else:
            label = "Char {n}"
            char_role = "producer"

        characters.append({
            "label": label,
            "role": char_role,
            "slice_name": None,
            "overflow_index": 0,
            "slots": slots,
        })

    # ── Finalize labels with running character number ──────────────────
    for i, ch in enumerate(characters, start=1):
        ch["label"] = ch["label"].replace("{n}", str(i))

    total_planets_assigned = sum(
        1 for ch in characters for s in ch["slots"] if s["planet_name"] is not None
    )
    shared_slots = sum(
        1 for ch in characters for s in ch["slots"] if s.get("shared")
    )

    capacity_warning = None
    if unassignable > 0:
        capacity_warning = (
            f"{unassignable} planet slot{'s' if unassignable != 1 else ''} couldn't be "
            f"assigned — the selected system is at capacity for this plan. "
            f"Reduce target cycles or pick a richer system."
        )

    # Shortfall summary: group dropped miner colonies by the P1 product they
    # would have produced so the UI can list affected outputs.
    shortfall_by_p1: dict[str, dict] = {}
    for c in dropped_colonies:
        if c["role"] == "miner_p1":
            for s in c.get("slots", []):
                p1_name = s.get("p1_name") or "?"
                entry = shortfall_by_p1.setdefault(p1_name, {
                    "p1_name": p1_name,
                    "p0_name": s.get("p0_name"),
                    "dropped_factories": 0,
                })
                entry["dropped_factories"] += 1
        else:
            # A dropped hub — attribute to its factory tier generically
            tier_label = c.get("factory_tier") or "hub"
            key = f"[{tier_label}] hub"
            entry = shortfall_by_p1.setdefault(key, {
                "p1_name": key,
                "p0_name": None,
                "dropped_factories": 0,
            })
            entry["dropped_factories"] += c.get("factory_count", 1)

    shortfall_list = sorted(shortfall_by_p1.values(), key=lambda e: -e["dropped_factories"])

    # Achievable ratio if capped
    achievable_ratio = None
    if is_capped and optimal_n > 0:
        achievable_ratio = round(effective_n / optimal_n, 2)

    result = {
        "characters": characters,
        "total_characters": len(characters),
        "total_planets_assigned": total_planets_assigned,
        "shared_slots": shared_slots,
        "unassignable_slots": unassignable,
        "system_capacity_warning": capacity_warning,
        "optimal_chars": optimal_n,
        "effective_chars": effective_n,
        "planets_per_char": planets_per_char,
        "is_capped": is_capped,
        "dropped_count": len(dropped_colonies),
        "shortfalls": shortfall_list,
        "achievable_ratio": achievable_ratio,
    }
    # Attach flow graph: items per char per tier, edges for cross-char handoffs,
    # and per-char imports/exports. Used by the template's flow chart panel.
    result["flow"] = _build_flow(result, bom, graph)
    return result


def _build_flow(character_plan: dict, bom: dict, graph: dict) -> dict:
    """Compute per-character production flow data for the swim-lane chart.

    Walks each character's slots to build:
      - `items_by_char[cidx][tier]`: list of {tid, name, count} the char
        produces at that tier (miner P0/P1 and hub factories).
      - `edges`: cross-character handoffs, one per (consumer slot, input tid).
        Picks producer via "same char first; else smallest current fan-out"
        to load-balance hauls.
      - `imports[cidx]` / `exports[cidx]`: aggregated per-char chip data.

    Returns dicts that are JSON-safe for embedding via Jinja |tojson.
    """
    name_map = graph["name_map"]
    input_map = graph["input_map"]
    canonical = graph["canonical_schematic"]
    tier_of = graph["tier_of"]

    characters = character_plan.get("characters", [])

    # ── Build items_by_char + producers_by_tid ───────────────────────────
    # items_by_char[cidx][tier] holds deduped nodes {tid, name, count, planets}
    # where `planets` lists the planets contributing (for the hover tooltip).
    items_by_char: list[list[list[dict]]] = [
        [[] for _ in range(5)] for _ in characters
    ]
    # producers_by_tid: tid → list of char indices that produce it
    producers_by_tid: dict[int, list[int]] = {}
    # Intra-char P0→P1 pairs: for every miner ECU, remember its P0→P1 link so
    # we can draw intra-row edges for the extractor chain.
    intra_miner_pairs: list[tuple[int, int, int]] = []  # (char_idx, p0_tid, p1_tid)

    for cidx, char in enumerate(characters):
        for slot in char["slots"]:
            planet_name = slot.get("planet_name") or "—"
            if slot["role"] == "miner_p1":
                for s in slot.get("slots", []):
                    items_by_char[cidx][0].append({
                        "tid": s["p0_tid"],
                        "name": s["p0_name"],
                        "count": 1,
                        "planets": [planet_name],
                    })
                    items_by_char[cidx][1].append({
                        "tid": s["p1_tid"],
                        "name": s["p1_name"],
                        "count": 1,
                        "planets": [planet_name],
                    })
                    producers_by_tid.setdefault(s["p1_tid"], [])
                    if cidx not in producers_by_tid[s["p1_tid"]]:
                        producers_by_tid[s["p1_tid"]].append(cidx)
                    intra_miner_pairs.append((cidx, s["p0_tid"], s["p1_tid"]))
            else:
                for fac in slot.get("factories", []):
                    tier = fac.get("tier", 0)
                    tid = fac.get("type_id")
                    if tid is None or not (0 <= tier <= 4):
                        continue
                    items_by_char[cidx][tier].append({
                        "tid": tid,
                        "name": fac.get("name", f"Type {tid}"),
                        "count": fac.get("count", 1),
                        "planets": [planet_name],
                    })
                    producers_by_tid.setdefault(tid, [])
                    if cidx not in producers_by_tid[tid]:
                        producers_by_tid[tid].append(cidx)

    # Merge duplicate (cidx, tid) — e.g. two miner slots on same char both
    # produce Water; display as one node with combined count + planet list.
    for cidx in range(len(characters)):
        for tier in range(5):
            merged: dict[int, dict] = {}
            for it in items_by_char[cidx][tier]:
                key = it["tid"]
                if key in merged:
                    merged[key]["count"] += it["count"]
                    for p in it.get("planets", []):
                        if p not in merged[key]["planets"]:
                            merged[key]["planets"].append(p)
                else:
                    merged[key] = {
                        "tid": it["tid"],
                        "name": it["name"],
                        "count": it["count"],
                        "planets": list(it.get("planets", [])),
                    }
            items_by_char[cidx][tier] = sorted(merged.values(), key=lambda i: i["name"])

    # ── Build edges ──────────────────────────────────────────────────────
    # Two kinds:
    #   intra=True  — producer + consumer on the SAME char (miner P0→P1, or
    #                  same-char factory→factory). Visually short/muted lines;
    #                  they show the extractor chain so P0 nodes aren't orphans.
    #   intra=False — cross-character hand-offs that require hauling.
    # Cross edges dedupe on (from_char, to_char, tid, consumer_tid).
    # Picker prefers same-char producer (→ intra edge); otherwise picks the
    # cross-char producer with smallest current fan-out.
    edges: list[dict] = []
    fan_out: dict[int, int] = {}
    seen_cross_keys: set[tuple] = set()
    seen_intra_keys: set[tuple] = set()

    # First: intra-miner P0 → P1 edges (one per unique pair on each char,
    # the node merging means the chart has a single P0 and P1 node per tid)
    for (cidx, p0_tid, p1_tid) in intra_miner_pairs:
        key = (cidx, p0_tid, p1_tid)
        if key in seen_intra_keys:
            continue
        seen_intra_keys.add(key)
        edges.append({
            "from_char": cidx,
            "to_char": cidx,
            "tid": p0_tid,
            "name": name_map.get(p0_tid, f"Type {p0_tid}"),
            "consumer_tid": p1_tid,
            "tier_from": 0,
            "tier_to": 1,
            "intra": True,
        })

    # Then: factory→factory edges. For each non-miner consumer slot, pick the
    # best producer of each input; same-char wins (intra) else cross-char.
    for cidx, char in enumerate(characters):
        for slot in char["slots"]:
            if slot["role"] == "miner_p1":
                continue
            for fac in slot.get("factories", []):
                out_tid = fac.get("type_id")
                if out_tid is None:
                    continue
                sch_id = canonical.get(out_tid)
                if sch_id is None:
                    continue
                for in_tid, _in_qty in input_map.get(sch_id, []):
                    producers = producers_by_tid.get(in_tid, [])
                    if not producers:
                        continue  # not produced in-plan (shortfall or P0 leaf)
                    if cidx in producers:
                        # Intra-char edge; dedupe on (cidx, in_tid, out_tid)
                        key = (cidx, in_tid, out_tid)
                        if key in seen_intra_keys:
                            continue
                        seen_intra_keys.add(key)
                        edges.append({
                            "from_char": cidx,
                            "to_char": cidx,
                            "tid": in_tid,
                            "name": name_map.get(in_tid, f"Type {in_tid}"),
                            "consumer_tid": out_tid,
                            "tier_from": tier_of.get(in_tid, 0),
                            "tier_to": tier_of.get(out_tid, 0),
                            "intra": True,
                        })
                        continue
                    from_char = min(producers, key=lambda pc: fan_out.get(pc, 0))
                    key = (from_char, cidx, in_tid, out_tid)
                    if key in seen_cross_keys:
                        continue
                    seen_cross_keys.add(key)
                    fan_out[from_char] = fan_out.get(from_char, 0) + 1
                    edges.append({
                        "from_char": from_char,
                        "to_char": cidx,
                        "tid": in_tid,
                        "name": name_map.get(in_tid, f"Type {in_tid}"),
                        "consumer_tid": out_tid,
                        "tier_from": tier_of.get(in_tid, 0),
                        "tier_to": tier_of.get(out_tid, 0),
                        "intra": False,
                    })

    # ── Aggregate imports/exports per character ─────────────────────────
    # Only cross-char edges count as imports/exports; intra-char edges are
    # on-planet or same-char handoffs that don't require hauling.
    imports_idx: list[dict[int, dict]] = [{} for _ in characters]
    exports_idx: list[dict[int, dict]] = [{} for _ in characters]
    for e in edges:
        if e.get("intra"):
            continue
        ii = imports_idx[e["to_char"]]
        if e["tid"] not in ii:
            ii[e["tid"]] = {"tid": e["tid"], "name": e["name"], "counterparts": []}
        if e["from_char"] + 1 not in ii[e["tid"]]["counterparts"]:
            ii[e["tid"]]["counterparts"].append(e["from_char"] + 1)  # 1-based Char N
        ei = exports_idx[e["from_char"]]
        if e["tid"] not in ei:
            ei[e["tid"]] = {"tid": e["tid"], "name": e["name"], "counterparts": []}
        if e["to_char"] + 1 not in ei[e["tid"]]["counterparts"]:
            ei[e["tid"]]["counterparts"].append(e["to_char"] + 1)

    imports = [sorted(ii.values(), key=lambda x: x["name"]) for ii in imports_idx]
    exports = [sorted(ei.values(), key=lambda x: x["name"]) for ei in exports_idx]

    # Mark "surplus" nodes: items produced on a char but never consumed by
    # any (intra or cross) edge AND not the target. These are dead-end P1/P2
    # factories the packer slotted to fill a planet but the chain doesn't need.
    consumed_by_char: dict[tuple[int, int], bool] = {}
    for e in edges:
        consumed_by_char[(e["from_char"], e["tid"])] = True
    for cidx in range(len(characters)):
        for tier in range(5):
            for node in items_by_char[cidx][tier]:
                if tier == 0:
                    # P0 nodes are always "consumed" locally by their miner's
                    # P1 factory — the intra miner edge exists. The edge key
                    # uses p0_tid so this flag should already be set, but
                    # double-check in case of oddities.
                    node["surplus"] = not consumed_by_char.get((cidx, node["tid"]), False)
                else:
                    node["surplus"] = not consumed_by_char.get((cidx, node["tid"]), False)

    return {
        "items_by_char": items_by_char,
        "edges": edges,
        "imports": imports,
        "exports": exports,
    }


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
