"""Intel tool — D-Scan parser and Local Scan analyzer. Paste, parse, share."""

import asyncio
import json
import re
import secrets
import string
from datetime import datetime, timezone, timedelta
from collections import Counter

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.models import get_db, DScanResult, AsyncSessionLocal
from app.esi.client import ESIClient
from app.sde import lookup as sde

router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")

# ── Ship categorization by group_id ───────────────────────────────────────────

SHIP_GROUP_CATEGORIES = {
    25: "Frigate", 324: "Assault Frigate", 830: "Covert Ops", 831: "Blockade Runner",
    834: "Stealth Bomber", 893: "Electronic Attack Ship", 1283: "Expedition Frigate",
    1527: "Logistics Frigate", 1534: "Command Destroyer", 2001: "Flag Cruiser",
    420: "Destroyer", 541: "Interdictor", 1305: "Tactical Destroyer",
    26: "Cruiser", 358: "Heavy Assault Cruiser", 380: "Deep Space Transport",
    832: "Logistics Cruiser", 833: "Force Recon", 894: "Heavy Interdictor",
    906: "Combat Recon", 1972: "Flag Cruiser",
    419: "Battlecruiser", 540: "Command Ship", 1201: "Attack Battlecruiser",
    27: "Battleship", 381: "Elite Battleship", 898: "Black Ops", 900: "Marauder",
    485: "Dreadnought", 547: "Carrier", 659: "Supercarrier",
    883: "Capital Industrial Ship", 1538: "Force Auxiliary", 30: "Titan",
    513: "Freighter", 902: "Jump Freighter",
    28: "Industrial", 941: "Industrial Command Ship", 463: "Mining Barge",
    543: "Exhumer", 1022: "Expedition Frigate",
    29: "Capsule", 31: "Shuttle", 237: "Rookie Ship",
}

DRONE_GROUPS = {100, 101, 299, 549, 1023, 1537}
STRUCTURE_GROUPS = {365, 444, 1250, 1404, 1657, 1312, 1406, 1407, 1408}
DEPLOYABLE_GROUPS = {1246, 1247, 1249, 1250, 1273, 1274, 1275, 1276, 1297}
FIGHTER_GROUPS = {1537, 1652, 1653}
CELESTIAL_NAMES = {"Sun", "Planet", "Moon", "Asteroid Belt", "Stargate", "Wormhole"}

SHIP_CLASS_GROUP = {
    "Frigate": "Frigates", "Assault Frigate": "Frigates", "Covert Ops": "Frigates",
    "Electronic Attack Ship": "Frigates", "Expedition Frigate": "Frigates",
    "Logistics Frigate": "Frigates", "Stealth Bomber": "Frigates",
    "Destroyer": "Destroyers", "Interdictor": "Destroyers",
    "Command Destroyer": "Destroyers", "Tactical Destroyer": "Destroyers",
    "Cruiser": "Cruisers", "Heavy Assault Cruiser": "Cruisers",
    "Logistics Cruiser": "Cruisers", "Force Recon": "Cruisers",
    "Combat Recon": "Cruisers", "Heavy Interdictor": "Cruisers",
    "Deep Space Transport": "Cruisers", "Flag Cruiser": "Cruisers",
    "Battlecruiser": "Battlecruisers", "Command Ship": "Battlecruisers",
    "Attack Battlecruiser": "Battlecruisers",
    "Battleship": "Battleships", "Elite Battleship": "Battleships",
    "Black Ops": "Battleships", "Marauder": "Battleships",
    "Dreadnought": "Capitals", "Carrier": "Capitals", "Supercarrier": "Capitals",
    "Force Auxiliary": "Capitals", "Titan": "Capitals",
    "Capital Industrial Ship": "Capitals",
    "Freighter": "Industrial", "Jump Freighter": "Industrial",
    "Industrial": "Industrial", "Industrial Command Ship": "Industrial",
    "Mining Barge": "Industrial", "Exhumer": "Industrial",
    "Capsule": "Capsules", "Shuttle": "Shuttles", "Rookie Ship": "Shuttles",
}

SHIP_GROUP_ORDER = [
    "Capitals", "Battleships", "Battlecruisers", "Cruisers",
    "Destroyers", "Frigates", "Industrial", "Capsules", "Shuttles",
]

EXPIRY_OPTIONS = {
    "24h":    timedelta(hours=24),
    "48h":    timedelta(hours=48),
    "1week":  timedelta(weeks=1),
    "1month": timedelta(days=30),
    "1year":  timedelta(days=365),
}


def _generate_id() -> str:
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


# ── Paste type detection ──────────────────────────────────────────────────────

def detect_paste_type(text: str) -> str:
    """Detect whether paste is a d-scan or local scan.
    D-scan lines start with a number (type_id) followed by a tab.
    Local scan lines are just character names, one per line."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return "unknown"

    dscan_count = 0
    for line in lines[:20]:  # Check first 20 lines
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                int(parts[0])
                dscan_count += 1
            except ValueError:
                pass

    # If most lines have tab-separated type_id, it's a d-scan
    if dscan_count >= len(lines[:20]) * 0.5:
        return "dscan"
    return "local"


# ── D-Scan parsing ────────────────────────────────────────────────────────────

def _parse_distance(raw: str) -> float | None:
    if not raw:
        return None
    m = re.match(r'^([\d,.\s]+)\s*(m|km|AU)$', raw.strip(), re.IGNORECASE)
    if not m:
        return None
    num_str = m.group(1).replace(",", "").replace(" ", "")
    unit = m.group(2).lower()
    try:
        val = float(num_str)
    except ValueError:
        return None
    if unit == "km":
        val *= 1000
    elif unit == "au":
        val *= 149_597_870_700
    return val


def _format_distance(meters: float | None) -> str:
    if meters is None:
        return "—"
    if meters >= 149_597_870_700 * 0.01:
        return f"{meters / 149_597_870_700:.1f} AU"
    elif meters >= 1000:
        return f"{meters / 1000:,.1f} km"
    else:
        return f"{meters:,.0f} m"


def parse_dscan(text: str) -> list[dict]:
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            type_id = int(parts[0])
        except (ValueError, IndexError):
            continue
        type_name = parts[1].strip()
        distance_raw = parts[2].strip() if len(parts) > 2 else ""
        items.append({
            "type_id": type_id,
            "type_name": type_name,
            "distance_m": _parse_distance(distance_raw),
            "distance_str": _format_distance(_parse_distance(distance_raw)),
        })
    return items


def categorize_item(type_name: str, group_id: int | None) -> str:
    if group_id is None:
        for c in CELESTIAL_NAMES:
            if c.lower() in type_name.lower():
                return "Celestial"
        return "Other"
    if group_id in SHIP_GROUP_CATEGORIES:
        return SHIP_GROUP_CATEGORIES[group_id]
    if group_id in DRONE_GROUPS:
        return "Drone"
    if group_id in FIGHTER_GROUPS:
        return "Fighter"
    if group_id in STRUCTURE_GROUPS or group_id in DEPLOYABLE_GROUPS:
        return "Structure"
    for c in CELESTIAL_NAMES:
        if c.lower() in type_name.lower():
            return "Celestial"
    return "Other"


def build_dscan_summary(items: list[dict]) -> dict:
    categories = Counter()
    ship_classes = Counter()
    ship_types = Counter()
    for item in items:
        cat = item.get("category", "Other")
        hull = item.get("hull_name", item.get("type_name", "Unknown"))
        categories[cat] += 1
        if cat in SHIP_GROUP_CATEGORIES.values():
            ship_classes[cat] += 1
            ship_types[hull] += 1

    total_ships = sum(ship_classes.values())

    ships_by_group: dict[str, dict] = {}
    for item in items:
        cat = item.get("category", "Other")
        if cat not in SHIP_GROUP_CATEGORIES.values():
            continue
        hull = item.get("hull_name", item.get("type_name", "Unknown"))
        group = SHIP_CLASS_GROUP.get(cat, "Other")
        ships_by_group.setdefault(group, {})
        if hull not in ships_by_group[group]:
            ships_by_group[group][hull] = {"name": hull, "type_id": item["type_id"], "count": 0, "class": cat}
        ships_by_group[group][hull]["count"] += 1

    ships_grouped = []
    for group_name in SHIP_GROUP_ORDER:
        if group_name not in ships_by_group:
            continue
        ship_list = sorted(ships_by_group[group_name].values(), key=lambda s: -s["count"])
        ships_grouped.append({
            "group": group_name,
            "total": sum(s["count"] for s in ship_list),
            "ships": ship_list,
        })

    return {
        "type": "dscan",
        "total": len(items),
        "ships": total_ships,
        "non_ships": len(items) - total_ships,
        "categories": dict(categories.most_common()),
        "ship_classes": dict(ship_classes.most_common()),
        "ship_types": dict(ship_types.most_common()),
        "ships_grouped": ships_grouped,
    }


# ── Local Scan parsing ────────────────────────────────────────────────────────

def parse_local_names(text: str) -> list[str]:
    """Extract character names from local chat paste (one per line)."""
    names = []
    for line in text.strip().splitlines():
        name = line.strip()
        if name and len(name) >= 2:
            names.append(name)
    return names


async def resolve_local_scan(names: list[str]) -> dict:
    """Resolve character names to IDs, then fetch corp/alliance info from ESI.

    Uses aggressive caching and batched lookups to handle 1000+ names efficiently.
    Character info is cached for 1 hour, corp/alliance names cached for 24 hours.
    """
    char_ids: dict[str, int] = {}
    unresolved: list[str] = []
    sem = asyncio.Semaphore(15)

    # Step 1: Batch resolve names to IDs (500 per call, cached by post_public)
    async def resolve_batch(batch: list[str]):
        async with sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    result = await c.post_public("/universe/ids/", batch)
                    if isinstance(result, dict):
                        for char in result.get("characters", []):
                            char_ids[char["name"]] = char["id"]
                except Exception:
                    pass

    batches = [names[i:i+500] for i in range(0, len(names), 500)]
    await asyncio.gather(*[resolve_batch(b) for b in batches])

    # Find unresolved names
    resolved_lower = {n.lower() for n in char_ids}
    for name in names:
        if name not in char_ids and name.lower() not in resolved_lower:
            unresolved.append(name)

    # Step 2: Fetch character info in waves (corp_id, alliance_id)
    # Process in chunks to avoid overwhelming ESI
    char_info: dict[int, dict] = {}
    all_char_ids = list(char_ids.values())

    async def fetch_char(char_id: int):
        async with sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/characters/{char_id}/")
                    if isinstance(data, dict):
                        char_info[char_id] = {
                            "name": data.get("name", ""),
                            "corporation_id": data.get("corporation_id"),
                            "alliance_id": data.get("alliance_id"),
                        }
                except Exception:
                    pass

    # Process character lookups in waves of 100 to avoid timeout cascades
    for i in range(0, len(all_char_ids), 100):
        wave = all_char_ids[i:i+100]
        await asyncio.gather(*[fetch_char(cid) for cid in wave])

    # Step 3: Collect unique corp/alliance IDs and resolve names
    corp_ids = set()
    alliance_ids = set()
    for info in char_info.values():
        if info.get("corporation_id"):
            corp_ids.add(info["corporation_id"])
        if info.get("alliance_id"):
            alliance_ids.add(info["alliance_id"])

    corp_names: dict[int, str] = {}
    alliance_names: dict[int, str] = {}

    async def fetch_corp(corp_id: int):
        async with sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/corporations/{corp_id}/")
                    if isinstance(data, dict):
                        corp_names[corp_id] = data.get("name", f"Corp {corp_id}")
                except Exception:
                    corp_names[corp_id] = f"Corp {corp_id}"

    async def fetch_alliance(alliance_id: int):
        async with sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/alliances/{alliance_id}/")
                    if isinstance(data, dict):
                        alliance_names[alliance_id] = data.get("name", f"Alliance {alliance_id}")
                except Exception:
                    alliance_names[alliance_id] = f"Alliance {alliance_id}"

    # Corp/alliance lookups are much smaller sets — do them all at once
    await asyncio.gather(
        *[fetch_corp(cid) for cid in corp_ids],
        *[fetch_alliance(aid) for aid in alliance_ids],
    )

    # Build character list with corp/alliance names
    characters = []
    for name, char_id in char_ids.items():
        info = char_info.get(char_id, {})
        corp_id = info.get("corporation_id")
        alliance_id = info.get("alliance_id")
        characters.append({
            "name": name,
            "character_id": char_id,
            "corporation_id": corp_id,
            "corporation_name": corp_names.get(corp_id, "Unknown Corp") if corp_id else "Unknown",
            "alliance_id": alliance_id,
            "alliance_name": alliance_names.get(alliance_id) if alliance_id else None,
        })

    # Build summary by corp and alliance
    by_corp = Counter()
    by_alliance = Counter()
    corp_to_alliance: dict[str, str | None] = {}

    for char in characters:
        corp_name = char["corporation_name"]
        by_corp[corp_name] += 1
        corp_to_alliance[corp_name] = char.get("alliance_name")
        if char["alliance_name"]:
            by_alliance[char["alliance_name"]] += 1

    return {
        "type": "local",
        "total": len(names),
        "resolved": len(characters),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:20],  # Cap to avoid huge payloads
        "characters": characters,
        "by_corp": dict(by_corp.most_common()),
        "by_alliance": dict(by_alliance.most_common()),
        "corp_to_alliance": corp_to_alliance,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/intel/dscan", response_class=HTMLResponse)
async def intel_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    history = []
    if user_id:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(DScanResult)
            .where(DScanResult.user_id == user_id, DScanResult.expires_at > now)
            .order_by(DScanResult.created_at.desc())
            .limit(50)
        )
        rows = result.scalars().all()
        for r in rows:
            summary = json.loads(r.summary_json) if r.summary_json else {}
            scan_type = summary.get("type", "dscan")
            total = summary.get("total", 0)
            ships = summary.get("ships", 0)
            if scan_type == "dscan":
                detail = f"{ships} ships / {total} total"
            else:
                detail = f"{total} pilots"
            expires = r.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            remaining = expires - now
            if remaining.days > 0:
                exp_str = f"{remaining.days}d"
            elif remaining.seconds >= 3600:
                exp_str = f"{remaining.seconds // 3600}h"
            else:
                exp_str = f"{(remaining.seconds % 3600) // 60}m"
            history.append({
                "id": r.id,
                "label": r.label,
                "scan_type": scan_type,
                "detail": detail,
                "created_at": r.created_at,
                "expires_in": exp_str,
            })
    return templates.TemplateResponse(request, "intel.html", {"history": history})


# Keep old /dscan URL working
@router.get("/dscan", response_class=HTMLResponse)
async def dscan_redirect(request: Request):
    return RedirectResponse("/intel/dscan")


@router.post("/intel/parse")
async def intel_parse(request: Request, db: AsyncSession = Depends(get_db)):
    """Auto-detect paste type (d-scan vs local), parse, store, redirect."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    paste_text = form.get("paste_text", "")
    label = form.get("label", "").strip()[:128] or None

    if not paste_text.strip():
        return templates.TemplateResponse(request, "intel.html", {"error": "Paste your d-scan or local scan above."})

    paste_type = detect_paste_type(paste_text)

    if paste_type == "dscan":
        items = parse_dscan(paste_text)
        if not items:
            return templates.TemplateResponse(request, "intel.html", {"error": "Could not parse d-scan. Make sure you paste from the d-scan window."})

        type_ids = list(set(item["type_id"] for item in items))
        group_ids = await sde.get_type_group_ids(db, type_ids)
        sde_names = await sde.type_ids_to_names(db, type_ids)

        for item in items:
            tid = item["type_id"]
            gid = group_ids.get(tid)
            hull_name = sde_names.get(tid, item["type_name"])
            item["group_id"] = gid
            item["hull_name"] = hull_name
            item["category"] = categorize_item(hull_name, gid)

        summary = build_dscan_summary(items)
        parsed_json = json.dumps(items)

    elif paste_type == "local":
        names = parse_local_names(paste_text)
        if not names:
            return templates.TemplateResponse(request, "intel.html", {"error": "Could not parse any character names."})

        summary = await resolve_local_scan(names)
        parsed_json = json.dumps(summary.get("characters", []))

    else:
        return templates.TemplateResponse(request, "intel.html", {"error": "Could not detect paste type. Paste d-scan or local chat."})

    scan_id = _generate_id()
    now = datetime.now(timezone.utc)

    dscan = DScanResult(
        id=scan_id,
        paste_data=paste_text,
        parsed_json=parsed_json,
        summary_json=json.dumps(summary),
        label=label,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(dscan)
    await db.commit()

    return RedirectResponse(f"/intel/{scan_id}", status_code=303)


@router.get("/intel/{scan_id}", response_class=HTMLResponse)
async def intel_view(scan_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Public view — no auth required."""
    result = await db.execute(select(DScanResult).where(DScanResult.id == scan_id))
    dscan = result.scalar_one_or_none()

    if not dscan:
        return templates.TemplateResponse(request, "intel.html", {"error": "Intel not found or has expired."})

    now = datetime.now(timezone.utc)
    expires = dscan.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        await db.execute(delete(DScanResult).where(DScanResult.id == scan_id))
        await db.commit()
        return templates.TemplateResponse(request, "intel.html", {"error": "This intel has expired."})

    summary = json.loads(dscan.summary_json) if dscan.summary_json else {}
    items = json.loads(dscan.parsed_json) if dscan.parsed_json else []
    scan_type = summary.get("type", "dscan")

    remaining = expires - now
    if remaining.days > 0:
        expires_str = f"{remaining.days}d {remaining.seconds // 3600}h"
    else:
        expires_str = f"{remaining.seconds // 3600}h {(remaining.seconds % 3600) // 60}m"

    # For d-scan: split ship vs non-ship items
    ship_class_values = set(SHIP_GROUP_CATEGORIES.values())
    non_ship_items = [i for i in items if i.get("category") not in ship_class_values] if scan_type == "dscan" else []

    template = "intel_dscan.html" if scan_type == "dscan" else "intel_local.html"

    return templates.TemplateResponse(request, template, {
        "dscan": dscan,
        "items": items,
        "non_ship_items": non_ship_items,
        "summary": summary,
        "expires_str": expires_str,
        "scan_id": scan_id,
        "expiry_options": EXPIRY_OPTIONS,
    })


# Keep old /dscan/{id} URLs working
@router.get("/dscan/{scan_id}", response_class=HTMLResponse)
async def dscan_view_redirect(scan_id: str, request: Request):
    return RedirectResponse(f"/intel/{scan_id}")


@router.post("/intel/{scan_id}/merge", response_class=HTMLResponse)
async def intel_merge(scan_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Merge additional d-scan paste into an existing scan result."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    paste_text = form.get("paste_text", "")
    dedup = form.get("dedup", "on") == "on"

    result = await db.execute(select(DScanResult).where(DScanResult.id == scan_id))
    dscan = result.scalar_one_or_none()
    if not dscan:
        return RedirectResponse("/intel", status_code=303)
    if dscan.user_id is not None and dscan.user_id != user_id:
        return RedirectResponse(f"/intel/{scan_id}", status_code=303)

    # Parse the new paste
    new_items = parse_dscan(paste_text)
    if not new_items:
        return RedirectResponse(f"/intel/{scan_id}", status_code=303)

    # Enrich new items with SDE data
    type_ids = list(set(item["type_id"] for item in new_items))
    group_ids = await sde.get_type_group_ids(db, type_ids)
    sde_names = await sde.type_ids_to_names(db, type_ids)
    for item in new_items:
        tid = item["type_id"]
        gid = group_ids.get(tid)
        hull_name = sde_names.get(tid, item["type_name"])
        item["group_id"] = gid
        item["hull_name"] = hull_name
        item["category"] = categorize_item(hull_name, gid)

    existing_items = json.loads(dscan.parsed_json) if dscan.parsed_json else []

    if dedup:
        # For each type_id, keep max(existing_count, new_count)
        # This assumes overlapping ships are the same ones seen from a different position
        existing_counts: dict[int, int] = Counter()
        for item in existing_items:
            existing_counts[item["type_id"]] += 1

        new_counts: dict[int, list] = {}
        for item in new_items:
            new_counts.setdefault(item["type_id"], []).append(item)

        for type_id, new_list in new_counts.items():
            existing_n = existing_counts.get(type_id, 0)
            extras = len(new_list) - existing_n
            if extras > 0:
                # Add only the difference — these are ships not in the first scan
                existing_items.extend(new_list[:extras])
    else:
        existing_items.extend(new_items)

    # Append new paste to raw data
    dscan.paste_data = dscan.paste_data + "\n--- Merged Scan ---\n" + paste_text

    # Rebuild summary and save
    summary = build_dscan_summary(existing_items)
    dscan.parsed_json = json.dumps(existing_items)
    dscan.summary_json = json.dumps(summary)
    await db.commit()

    return RedirectResponse(f"/intel/{scan_id}", status_code=303)


@router.post("/intel/{scan_id}/extend", response_class=HTMLResponse)
async def intel_extend(scan_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    duration = form.get("duration", "24h")
    result = await db.execute(select(DScanResult).where(DScanResult.id == scan_id))
    dscan = result.scalar_one_or_none()
    if not dscan:
        return RedirectResponse("/intel", status_code=303)
    if dscan.user_id is not None and dscan.user_id != user_id:
        return RedirectResponse(f"/intel/{scan_id}", status_code=303)
    delta = EXPIRY_OPTIONS.get(duration, timedelta(hours=24))
    dscan.expires_at = datetime.now(timezone.utc) + delta
    await db.commit()
    return RedirectResponse(f"/intel/{scan_id}", status_code=303)
