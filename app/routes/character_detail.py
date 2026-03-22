"""
Character detail page with wallet history chart and journal.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, AsyncSessionLocal
from app.esi.client import ESIClient, refresh_token
from app.esi.character import get_wallet_journal
from dateutil import parser as iso_parser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["character_detail"])
templates = Jinja2Templates(directory="app/templates")

_RANGE_DAYS = {"1d": 1, "5d": 5, "1w": 7, "1m": 30, "6m": 180, "1y": 365}
_MAX_CHART_POINTS = 400

# Implant helpers (shared between active clone and jump clones)
_ATTR_ENHANCER_GROUP = 745
_ATTR_SLOT = {
    "Ocular Filter":           ("Perception",   1),
    "Memory Augmentation":     ("Memory",       2),
    "Neural Boost":            ("Willpower",    3),
    "Cybernetic Subprocessor": ("Intelligence", 4),
    "Social Adaptation Chip":  ("Charisma",     5),
}
_ATTR_GRADE = {
    "Basic": "+1", "Standard": "+2", "Improved": "+3",
    "Enhanced": "+4", "Strong": "+5",
}


def _enrich_implants(type_ids: list, type_map: dict) -> list:
    """Return enriched implant dicts sorted: attribute enhancers first, then hardwirings."""
    result = []
    for tid in type_ids:
        t = type_map.get(tid)
        name = t.type_name if t else "Type " + str(tid)
        group_id = t.group_id if t else None
        entry = {"type_id": tid, "name": name, "is_attr": False,
                 "slot": 99, "label": name}
        if group_id == _ATTR_ENHANCER_GROUP:
            entry["is_attr"] = True
            for key, (attr, slot) in _ATTR_SLOT.items():
                if key in name:
                    grade = next((g for g in _ATTR_GRADE if g in name), None)
                    bonus = _ATTR_GRADE.get(grade, "")
                    entry["slot"] = slot
                    entry["label"] = attr + " " + bonus if bonus else name
                    break
        result.append(entry)
    result.sort(key=lambda x: (0 if x["is_attr"] else 1, x["slot"], x["name"]))
    return result


def _downsample(snapshots: list, target: int) -> list:
    """Return at most `target` evenly-spaced snapshots."""
    if len(snapshots) <= target:
        return snapshots
    step = len(snapshots) / target
    return [snapshots[int(i * step)] for i in range(target)]


async def _get_chart_data(character_id: int, range_key: str, db: AsyncSession) -> dict:
    days = _RANGE_DAYS.get(range_key, 7)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # recorded_at is stored as naive UTC
    since_naive = since.replace(tzinfo=None)

    result = await db.execute(
        select(WalletSnapshot)
        .where(
            WalletSnapshot.character_id == character_id,
            WalletSnapshot.recorded_at >= since_naive,
        )
        .order_by(WalletSnapshot.recorded_at)
    )
    snapshots = result.scalars().all()
    snapshots = _downsample(snapshots, _MAX_CHART_POINTS)

    labels = [s.recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") for s in snapshots]
    values = [s.balance for s in snapshots]
    return {"labels": labels, "values": values}


async def _fetch_assets(character_id: int, char: Character, client: ESIClient, db: AsyncSession) -> list:
    """Fetch all assets from ESI, resolve names, group by location.

    Returns a list of dicts: [{"location": str, "items": [{"name": str, "quantity": int}]}]
    sorted by location name.
    """
    from app.db.sde_models import SDEStation, SDESystem, SDEType

    # Fetch all pages
    all_assets = []
    page = 1
    while True:
        try:
            page_data = await client.get(
                "/characters/" + str(character_id) + "/assets/",
                {"page": page},
            )
            if not page_data:
                break
            all_assets.extend(page_data)
            if len(page_data) < 1000:
                break
            page += 1
        except Exception:
            break

    if not all_assets:
        return []

    # Build item_id -> item map to resolve nested containers
    item_map = {item["item_id"]: item for item in all_assets}

    def get_root_location(item):
        """Walk up container chain to find root location_id and location_type."""
        seen = set()
        current = item
        while current.get("location_type") == "item":
            parent_id = current["location_id"]
            if parent_id in seen:
                break
            seen.add(parent_id)
            parent = item_map.get(parent_id)
            if not parent:
                # Parent not in our assets (e.g. player structure) - return its ID as "other"
                return parent_id, "other"
            current = parent
        return current["location_id"], current.get("location_type", "other")

    # Group by root location
    location_groups = {}
    for item in all_assets:
        root_id, root_type = get_root_location(item)
        if root_id not in location_groups:
            location_groups[root_id] = {"type": root_type, "items": []}
        location_groups[root_id]["items"].append(item)

    # Resolve location names
    station_ids = [lid for lid, ld in location_groups.items() if ld["type"] == "station"]
    system_ids = [lid for lid, ld in location_groups.items() if ld["type"] == "solar_system"]

    location_names = {}

    if station_ids:
        st_result = await db.execute(
            select(SDEStation).where(SDEStation.station_id.in_(station_ids))
        )
        for st in st_result.scalars().all():
            location_names[st.station_id] = st.station_name

    if system_ids:
        sys_result = await db.execute(
            select(SDESystem).where(SDESystem.system_id.in_(system_ids))
        )
        for sys in sys_result.scalars().all():
            location_names[sys.system_id] = sys.system_name + " (Space)"

    # Player structures and unknowns
    for loc_id, loc_data in location_groups.items():
        if loc_id not in location_names:
            if loc_data["type"] == "other" and loc_id > 100_000_000:
                try:
                    struct_data = await client.get("/universe/structures/" + str(loc_id) + "/")
                    location_names[loc_id] = struct_data.get("name", "Structure " + str(loc_id))
                except Exception:
                    location_names[loc_id] = "Structure " + str(loc_id)
            else:
                location_names[loc_id] = "Location " + str(loc_id)

    # Resolve item type names from SDE
    type_ids = list({item["type_id"] for item in all_assets})
    type_result = await db.execute(
        select(SDEType).where(SDEType.type_id.in_(type_ids))
    )
    type_name_map = {t.type_id: t.type_name for t in type_result.scalars().all()}

    # Build final structure
    result_locations = []
    for loc_id, loc_data in location_groups.items():
        loc_name = location_names.get(loc_id, "Location " + str(loc_id))
        items = []
        for item in loc_data["items"]:
            type_id = item["type_id"]
            type_name = type_name_map.get(type_id, "Type " + str(type_id))
            items.append({
                "name": type_name,
                "quantity": item.get("quantity", 1),
                "type_id": type_id,
            })
        items.sort(key=lambda x: x["name"])
        result_locations.append({"location": loc_name, "items": items})

    result_locations.sort(key=lambda x: x["location"])
    return result_locations


@router.get("/character/{character_id}", response_class=HTMLResponse)
async def character_detail(
    character_id: int,
    request: Request,
    range: str = "1w",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard")

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()

    # Fetch total trained SP from ESI
    total_trained_sp = 0
    try:
        if "esi-skills.read_skills.v1" in (char.scopes or ""):
            async with AsyncSessionLocal() as skills_db:
                sc_result = await skills_db.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                sc_char = sc_result.scalar_one_or_none()
                sc_token = await refresh_token(sc_char, skills_db)
            sc_client = ESIClient(sc_token, db=db)
            skills_path = "/characters/" + str(character_id) + "/skills/"
            raw_skills = await sc_client.get(skills_path)
            if raw_skills and isinstance(raw_skills, dict):
                for s in raw_skills.get("skills", []):
                    total_trained_sp += s.get("skillpoints_in_skill", 0)
    except Exception as e:
        logger.warning("Failed to fetch total SP for char %s: %s", character_id, e)

    queue_remaining = 0
    # Parse cached data
    skillqueue = []
    total_sp_in_queue = 0
    active_skill = None
    if cache and cache.skillqueue_json:
        try:
            sq = json.loads(cache.skillqueue_json)
            if isinstance(sq, list):
                skillqueue = sq
                if skillqueue:
                    # Batch-resolve all skill names from SDE
                    from app.db.sde_models import SDEType
                    all_skill_ids = list({s.get("skill_id") for s in skillqueue if s.get("skill_id")})
                    sde_result = await db.execute(
                        select(SDEType).where(SDEType.type_id.in_(all_skill_ids))
                    )
                    skill_name_map = {t.type_id: t.type_name for t in sde_result.scalars().all()}

                    now_utc = datetime.now(timezone.utc)
                    for entry in skillqueue:
                        sid = entry.get("skill_id")
                        entry["skill_name"] = skill_name_map.get(sid, "Skill " + str(sid)) if sid else "Unknown"
                        fin = entry.get("finish_date")
                        if fin:
                            try:
                                fd = iso_parser.isoparse(fin)
                                entry["remaining_seconds"] = max(0, (fd - now_utc).total_seconds())
                            except Exception:
                                entry["remaining_seconds"] = None
                        else:
                            entry["remaining_seconds"] = None

                    active_skill = skillqueue[0]
                    total_sp_in_queue = sum(s.get("level_end_sp", 0) for s in skillqueue)

                    # Total queue remaining (last entry's finish_date)
                    last_fin = skillqueue[-1].get("finish_date")
                    if last_fin:
                        try:
                            lf = iso_parser.isoparse(last_fin)
                            queue_remaining = max(0, (lf - now_utc).total_seconds())
                        except Exception:
                            pass
            else:
                skillqueue = sq.get("skills", [])
                total_sp_in_queue = sq.get("total_sp", 0)
                active_skill = sq.get("active", None)
        except Exception as e:
            logger.warning("Failed to parse skillqueue for char %s: %s", character_id, e)

    zkill = []
    if cache and cache.zkill_json:
        try:
            zkill = json.loads(cache.zkill_json)
        except Exception as e:
            logger.warning("Failed to parse zkill for char %s: %s", character_id, e)

    # Fetch active clone implants + jump clones (share one ESI session and SDE batch lookup)
    implants = []
    jump_clones = []
    if "esi-clones.read_implants.v1" in (char.scopes or "") or \
       "esi-clones.read_clones.v1" in (char.scopes or ""):
        try:
            async with AsyncSessionLocal() as impl_db:
                ic_result = await impl_db.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                ic_char = ic_result.scalar_one_or_none()
                ic_token = await refresh_token(ic_char, impl_db)
            ic_client = ESIClient(ic_token, db=db)

            from app.db.sde_models import SDEType as _SDEType, SDEStation as _SDEStation

            # Active clone implants
            impl_ids = []
            if "esi-clones.read_implants.v1" in (char.scopes or ""):
                impl_ids = await ic_client.get(
                    "/characters/" + str(character_id) + "/implants/"
                ) or []

            # Jump clone data
            clone_data = {}
            if "esi-clones.read_clones.v1" in (char.scopes or ""):
                clone_data = await ic_client.get(
                    "/characters/" + str(character_id) + "/clones/"
                ) or {}

            # Collect all type IDs for one batch SDE lookup
            jc_list = clone_data.get("jump_clones", [])
            all_type_ids = set(impl_ids)
            for jc in jc_list:
                all_type_ids.update(jc.get("implants", []))

            type_map = {}
            if all_type_ids:
                sde_r = await db.execute(
                    select(_SDEType).where(_SDEType.type_id.in_(all_type_ids))
                )
                type_map = {t.type_id: t for t in sde_r.scalars().all()}

            # Enrich active clone implants
            implants = _enrich_implants(impl_ids, type_map)

            # Resolve jump clone locations
            loc_ids = {jc["location_id"] for jc in jc_list}
            station_ids = [lid for lid in loc_ids if jc_list and
                           next((j for j in jc_list if j["location_id"] == lid), {})
                           .get("location_type") == "station"]
            structure_ids = [lid for lid in loc_ids if lid not in station_ids]

            loc_names = {}
            if station_ids:
                st_r = await db.execute(
                    select(_SDEStation).where(_SDEStation.station_id.in_(station_ids))
                )
                for s in st_r.scalars().all():
                    loc_names[s.station_id] = s.station_name

            for sid in structure_ids:
                try:
                    sd = await ic_client.get("/universe/structures/" + str(sid) + "/")
                    loc_names[sid] = sd.get("name", "Structure " + str(sid))
                except Exception:
                    loc_names[sid] = "Structure " + str(sid)

            for jc in jc_list:
                loc_id = jc["location_id"]
                loc_name = loc_names.get(loc_id, "Location " + str(loc_id))
                jump_clones.append({
                    "location": loc_name,
                    "implants": _enrich_implants(jc.get("implants", []), type_map),
                })

        except Exception as e:
            logger.warning("Failed to fetch implants/clones for char %s: %s", character_id, e)

    # Assets are loaded lazily via /character/{id}/assets.json when the user clicks "View Assets"
    has_assets_scope = "esi-assets.read_assets.v1" in (char.scopes or "")

    # Current docked location name (for asset highlighting)
    docked_at = None
    if cache and cache.location_json:
        try:
            loc = json.loads(cache.location_json)
            docked_at = loc.get("docked_at")
        except Exception:
            pass

    # Calculate kill/loss stats from zkill data
    kills = sum(1 for km in zkill if not km.get("is_loss"))
    losses = sum(1 for km in zkill if km.get("is_loss"))

    # Fetch wallet journal (live ESI call)
    journal = []
    journal_error = None
    if "esi-wallet.read_character_wallet.v1" in (char.scopes or ""):
        try:
            async with AsyncSessionLocal() as token_db:
                char_result2 = await token_db.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                char_fresh = char_result2.scalar_one_or_none()
                token = await refresh_token(char_fresh, token_db)
            client = ESIClient(token, db=db)
            raw = await get_wallet_journal(client, character_id, page=1)
            journal = raw[:20] if raw else []
        except Exception as e:
            logger.warning("Wallet journal fetch failed for char %s: %s", character_id, e)
            journal_error = "fetch_failed"
    else:
        journal_error = "missing_scope"

    # Initial chart data (default range)
    chart_data = await _get_chart_data(character_id, range, db)

    current_wallet = cache.wallet if cache else None

    return templates.TemplateResponse("character_detail.html", {
        "request": request,
        "char": char,
        "current_wallet": current_wallet,
        "journal": journal,
        "journal_error": journal_error,
        "chart_data_json": json.dumps(chart_data),
        "active_range": range,
        "ranges": list(_RANGE_DAYS.keys()),
        "active_skill": active_skill,
        "skillqueue": skillqueue,
        "total_sp_in_queue": total_sp_in_queue,
        "total_trained_sp": total_trained_sp,
        "queue_remaining": queue_remaining,
        "zkill": zkill,
        "kills": kills,
        "losses": losses,
        "has_assets_scope": has_assets_scope,
        "docked_at": docked_at,
        "implants": implants,
        "jump_clones": jump_clones,
        "now": datetime.utcnow(),
    })


@router.get("/character/{character_id}/assets.json")
async def character_assets_json(
    character_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    ownership = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    if not ownership.scalar_one_or_none():
        return JSONResponse({"error": "forbidden"}, status_code=403)

    # Read from the background-sync cache (CharacterAssetCache) — fast DB read
    from app.db.models import CharacterAssetCache
    cache_result = await db.execute(
        select(CharacterAssetCache).where(CharacterAssetCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()
    if not cache or not cache.assets_json:
        return JSONResponse({"locations": []})

    try:
        all_items = json.loads(cache.assets_json)

        # Group hangar-level items by location, aggregate stacks of same type
        groups: dict[str, dict[str, int]] = {}
        for item in all_items:
            if item.get("location_flag") != "Hangar":
                continue
            loc = item.get("location_name", "Unknown")
            name = item.get("type_name", "Unknown")
            qty = item.get("quantity", 1)
            if loc not in groups:
                groups[loc] = {}
            groups[loc][name] = groups[loc].get(name, 0) + qty

        locations = []
        for loc_name, item_counts in sorted(groups.items()):
            items = sorted(
                [{"name": n, "quantity": q} for n, q in item_counts.items()],
                key=lambda x: x["name"],
            )
            locations.append({"location": loc_name, "items": items})

        return JSONResponse({"locations": locations})
    except Exception as e:
        logger.warning("Assets JSON read failed for char %s: %s", character_id, e)
        return JSONResponse({"error": "fetch_failed"}, status_code=500)


@router.get("/character/{character_id}/assets-partial", response_class=HTMLResponse)
async def character_assets_partial(
    character_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse('<div style="color:var(--muted);font-size:10px;padding:1rem 0.75rem;">Unauthorized.</div>', status_code=403)
    ownership = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    if not ownership.scalar_one_or_none():
        return HTMLResponse('<div style="color:var(--muted);font-size:10px;padding:1rem 0.75rem;">Not found.</div>', status_code=403)

    # Look up current docked location from cache
    from app.db.models import CharacterAssetCache
    dash_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    dash = dash_result.scalar_one_or_none()
    docked_at = None
    if dash and dash.location_json:
        try:
            loc_data = json.loads(dash.location_json)
            docked_at = loc_data.get("docked_at")
        except Exception:
            pass

    cache_result = await db.execute(
        select(CharacterAssetCache).where(CharacterAssetCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()
    if not cache or not cache.assets_json:
        return HTMLResponse('<div style="color:var(--muted);font-size:10px;padding:1rem 0.75rem;">No assets found.</div>')

    try:
        all_items = json.loads(cache.assets_json)
        groups: dict[str, dict[str, int]] = {}
        for item in all_items:
            if item.get("location_flag") != "Hangar":
                continue
            loc = item.get("location_name") or "Unknown"
            name = item.get("type_name") or "Unknown"
            qty = item.get("quantity", 1)
            if loc not in groups:
                groups[loc] = {}
            groups[loc][name] = groups[loc].get(name, 0) + qty

        locations = []
        for loc_name, item_counts in sorted(groups.items()):
            items_list = sorted(
                [{"name": n, "quantity": q} for n, q in item_counts.items()],
                key=lambda x: x["name"],
            )
            locations.append({"location": loc_name, "items": items_list})

        if docked_at:
            locations.sort(key=lambda x: (0 if x["location"] == docked_at else 1, x["location"] or ""))

        return templates.TemplateResponse("partials/assets_partial.html", {
            "request": request,
            "locations": locations,
            "docked_at": docked_at,
        })
    except Exception as e:
        logger.warning("Assets partial failed for char %s: %s", character_id, e)
        return HTMLResponse('<div style="color:var(--muted);font-size:10px;padding:1rem 0.75rem;">Failed to load assets.</div>', status_code=500)


@router.get("/character/{character_id}/wallet/chart.json")
async def wallet_chart_json(
    character_id: int,
    request: Request,
    range: str = "1w",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    ownership = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    if not ownership.scalar_one_or_none():
        return JSONResponse({"error": "forbidden"}, status_code=403)

    data = await _get_chart_data(character_id, range, db)
    return JSONResponse(data)


# Helper function to get skill name from skill_id
async def get_skill_name(skill_id: int, db: AsyncSession) -> str:
    """Look up skill name from SDE database"""
    from app.db.sde_models import SDEType
    try:
        result = await db.execute(
            select(SDEType).where(SDEType.type_id == skill_id)
        )
        skill_type = result.scalar_one_or_none()
        return skill_type.type_name if skill_type else "Skill " + str(skill_id)
    except Exception:
        return "Skill " + str(skill_id)
