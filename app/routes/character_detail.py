"""
Character detail page with wallet history chart and journal.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, AsyncSessionLocal
from app.esi.client import ESIClient
from app.esi import character as esi_char
from app.esi.client import refresh_token
from app.esi.character import get_wallet_journal
from app.sde import lookup as sde
from app.utils.perf import perf_log, perf_enabled, ms_since
from time import perf_counter as _perf_now
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
    if target <= 0 or len(snapshots) <= target:
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
    from app.esi import universe as esi_universe
    from app.sde import lookup as sde
    for loc_id, loc_data in location_groups.items():
        if loc_id not in location_names:
            if loc_data["type"] == "other" and loc_id > 100_000_000:
                resolved_name = None
                try:
                    struct_data = await esi_universe.get_structure(client, loc_id, db=db)
                    resolved_name = struct_data.get("name")
                    sys_id = struct_data.get("solar_system_id")
                except Exception:
                    cached = await esi_universe.get_cached_structure(db, loc_id)
                    if cached:
                        resolved_name = cached["name"]
                        sys_id = cached.get("solar_system_id")
                    else:
                        resolved_name = None
                        sys_id = None
                if resolved_name and resolved_name != "Unknown Structure":
                    location_names[loc_id] = resolved_name
                elif sys_id:
                    sys_info = await sde.system_info(db, sys_id)
                    sys_name = sys_info.get("system_name") if sys_info else None
                    location_names[loc_id] = f"Unknown Structure ({sys_name})" if sys_name else "Unknown Structure"
                else:
                    location_names[loc_id] = "Unknown Structure"
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
    _t0 = _perf_now() if perf_enabled() else 0.0
    _t_mark = _t0
    _section_ms: dict[str, float] = {}

    def _mark(label: str) -> None:
        nonlocal _t_mark
        if perf_enabled():
            _section_ms[label] = ms_since(_t_mark)
            _t_mark = _perf_now()

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
    _mark("preamble")

    queue_remaining = 0
    completed_skills = []
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
                    thirty_days_ago = now_utc - timedelta(days=30)
                    completed_skills = []
                    pending_skills = []

                    for entry in skillqueue:
                        sid = entry.get("skill_id")
                        entry["skill_name"] = skill_name_map.get(sid, "Skill " + str(sid)) if sid else "Unknown"
                        fin = entry.get("finish_date")
                        if fin:
                            try:
                                fd = iso_parser.isoparse(fin)
                                entry["remaining_seconds"] = max(0, (fd - now_utc).total_seconds())
                                entry["finish_dt"] = fd
                                if fd <= now_utc:
                                    # Completed — only keep if within 30 days
                                    if fd >= thirty_days_ago:
                                        entry["completed_ago"] = int((now_utc - fd).total_seconds())
                                        completed_skills.append(entry)
                                    continue
                            except Exception:
                                entry["remaining_seconds"] = None
                        else:
                            entry["remaining_seconds"] = None
                        pending_skills.append(entry)

                    skillqueue = pending_skills
                    if skillqueue:
                        active_skill = skillqueue[0]
                    total_sp_in_queue = sum(s.get("level_end_sp", 0) for s in skillqueue)

                    # Total queue remaining (last pending entry's finish_date)
                    if skillqueue:
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
    _mark("skillqueue_parse")

    # Cached-data derivations (no IO)
    has_assets_scope = "esi-assets.read_assets.v1" in (char.scopes or "")
    docked_at = None
    current_system = None
    if cache and cache.location_json:
        try:
            loc = json.loads(cache.location_json)
            docked_at = loc.get("docked_at")
            current_system = loc.get("system_name")
        except Exception:
            pass
    kills = sum(1 for km in zkill if not km.get("is_loss"))
    losses = sum(1 for km in zkill if km.get("is_loss"))

    # Fan out the independent IO that used to run serially (trained SP,
    # implants/clones, wallet journal, corp history, wallet chart). Each
    # helper opens its own AsyncSessionLocal for any DB work per CLAUDE.md's
    # async-session-safety gotcha; helpers that need ESI caching set
    # client.db to a truthy sentinel because cache_get/cache_set always use
    # their own isolated sessions internally.
    scopes = char.scopes or ""

    async def _fetch_trained_sp() -> int:
        if "esi-skills.read_skills.v1" not in scopes:
            return 0
        try:
            async with AsyncSessionLocal() as tdb:
                c = (await tdb.execute(
                    select(Character).where(Character.character_id == character_id)
                )).scalar_one_or_none()
                if not c:
                    return 0
                token = await refresh_token(c, tdb)
            client = ESIClient(token)
            client.db = True
            raw = await client.get(f"/characters/{character_id}/skills/")
            if raw and isinstance(raw, dict):
                return sum(s.get("skillpoints_in_skill", 0) for s in raw.get("skills", []))
        except Exception as e:
            logger.warning("Failed to fetch total SP for char %s: %s", character_id, e)
        return 0

    async def _fetch_implants_clones() -> tuple[list, list]:
        impl_out: list = []
        jc_out: list = []
        if not any(s in scopes for s in ("esi-clones.read_implants.v1", "esi-clones.read_clones.v1")):
            return impl_out, jc_out
        try:
            async with AsyncSessionLocal() as tdb:
                c = (await tdb.execute(
                    select(Character).where(Character.character_id == character_id)
                )).scalar_one_or_none()
                if not c:
                    return impl_out, jc_out
                token = await refresh_token(c, tdb)
            client = ESIClient(token)
            client.db = True

            from app.db.sde_models import SDEType as _SDEType, SDEStation as _SDEStation

            # Implants + clones in parallel
            tasks = []
            want_impl = "esi-clones.read_implants.v1" in scopes
            want_clones = "esi-clones.read_clones.v1" in scopes
            if want_impl:
                tasks.append(client.get(f"/characters/{character_id}/implants/"))
            if want_clones:
                tasks.append(client.get(f"/characters/{character_id}/clones/"))
            fetched = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

            impl_ids: list = []
            clone_data: dict = {}
            idx = 0
            if want_impl:
                v = fetched[idx] if idx < len(fetched) else None
                impl_ids = v if isinstance(v, list) else []
                idx += 1
            if want_clones:
                v = fetched[idx] if idx < len(fetched) else None
                clone_data = v if isinstance(v, dict) else {}

            jc_list = clone_data.get("jump_clones", []) if isinstance(clone_data, dict) else []
            all_type_ids: set = set(impl_ids)
            for jc in jc_list:
                all_type_ids.update(jc.get("implants", []))

            async with AsyncSessionLocal() as sdb:
                type_map: dict = {}
                if all_type_ids:
                    sde_r = await sdb.execute(
                        select(_SDEType).where(_SDEType.type_id.in_(all_type_ids))
                    )
                    type_map = {t.type_id: t for t in sde_r.scalars().all()}

                loc_ids = {jc["location_id"] for jc in jc_list}
                station_ids = [lid for lid in loc_ids if jc_list and
                               next((j for j in jc_list if j["location_id"] == lid), {})
                               .get("location_type") == "station"]
                structure_ids = [lid for lid in loc_ids if lid not in station_ids]

                loc_names: dict = {}
                if station_ids:
                    st_r = await sdb.execute(
                        select(_SDEStation).where(_SDEStation.station_id.in_(station_ids))
                    )
                    for s in st_r.scalars().all():
                        loc_names[s.station_id] = s.station_name

            # Parallel structure-name lookups (bounded concurrency matches the
            # ESI bulk-fetch pattern from CLAUDE.md).
            if structure_ids:
                sem = asyncio.Semaphore(5)

                async def _resolve_struct(sid: int):
                    async with sem:
                        try:
                            sd = await client.get(f"/universe/structures/{sid}/")
                            return sid, sd.get("name", f"Structure {sid}")
                        except Exception:
                            return sid, f"Structure {sid}"

                res = await asyncio.gather(*[_resolve_struct(s) for s in structure_ids])
                for sid, name in res:
                    loc_names[sid] = name

            impl_out = _enrich_implants(impl_ids, type_map)
            for jc in jc_list:
                lid = jc["location_id"]
                jc_out.append({
                    "location": loc_names.get(lid, f"Location {lid}"),
                    "implants": _enrich_implants(jc.get("implants", []), type_map),
                })
        except Exception as e:
            logger.warning("Failed to fetch implants/clones for char %s: %s", character_id, e)
        return impl_out, jc_out

    async def _fetch_wallet_journal() -> tuple[list, str | None]:
        if "esi-wallet.read_character_wallet.v1" not in scopes:
            return [], "missing_scope"
        try:
            from app.esi.client import get_client_safe
            client = await get_client_safe(char)
            client.db = True
            raw = await get_wallet_journal(client, character_id, page=1)
            return (raw[:20] if raw else []), None
        except Exception as e:
            logger.warning("Wallet journal fetch failed for char %s: %s", character_id, e)
            return [], "fetch_failed"

    async def _fetch_corp_history() -> list:
        out: list = []
        try:
            from app.esi.client import ESIClient as _PubClient
            pub = _PubClient("")
            pub.db = True
            raw_history = await pub.get_public(f"/characters/{character_id}/corporationhistory/")
            if raw_history:
                corp_ids = list({h.get("corporation_id") for h in raw_history if h.get("corporation_id")})
                corp_names: dict = {}
                if corp_ids:
                    try:
                        names_data = await pub.post_public("/universe/names/", corp_ids)
                        corp_names = {n["id"]: n["name"] for n in names_data}
                    except Exception:
                        pass
                raw_history.sort(key=lambda x: x.get("record_id", 0), reverse=True)
                now_utc = datetime.now(timezone.utc)
                for i, h in enumerate(raw_history):
                    cid_h = h.get("corporation_id")
                    start = h.get("start_date", "")
                    days_in = None
                    if start:
                        try:
                            start_dt = iso_parser.isoparse(start)
                            if i == 0:
                                days_in = (now_utc - start_dt).days
                            else:
                                prev_start = raw_history[i - 1].get("start_date", "")
                                if prev_start:
                                    prev_dt = iso_parser.isoparse(prev_start)
                                    days_in = (prev_dt - start_dt).days
                        except Exception:
                            pass
                    out.append({
                        "corporation_id": cid_h,
                        "corporation_name": corp_names.get(cid_h, f"Corp {cid_h}"),
                        "start_date": start[:10] if start else "",
                        "days_in": days_in,
                        "is_current": i == 0,
                    })
        except Exception as e:
            logger.warning("Corp history fetch failed for char %s: %s", character_id, e)
        return out

    async def _fetch_chart_data() -> dict:
        try:
            async with AsyncSessionLocal() as cdb:
                return await _get_chart_data(character_id, range, cdb)
        except Exception as e:
            logger.warning("Chart data fetch failed for char %s: %s", character_id, e)
            return {"labels": [], "values": []}

    # Birthday backfill runs off the critical path — the template doesn't
    # wait on it. If the char row was missing a birthday before, it'll be
    # populated for the next page load.
    if not char.birthday:
        async def _bday_backfill():
            try:
                from app.esi.client import ESIClient as _PubClient2
                pub = _PubClient2("")
                pub.db = True
                pub_info = await pub.get_public(f"/characters/{character_id}/")
                bday_str = pub_info.get("birthday") if isinstance(pub_info, dict) else None
                if bday_str:
                    from dateutil import parser as iso_p
                    bday = iso_p.isoparse(bday_str).replace(tzinfo=None)
                    async with AsyncSessionLocal() as bdb:
                        c = (await bdb.execute(
                            select(Character).where(Character.character_id == character_id)
                        )).scalar_one_or_none()
                        if c and not c.birthday:
                            c.birthday = bday
                            await bdb.commit()
            except Exception:
                pass

        asyncio.create_task(_bday_backfill())

    (total_trained_sp,
     implants_clones_res,
     wallet_journal_res,
     corp_history,
     chart_data) = await asyncio.gather(
        _fetch_trained_sp(),
        _fetch_implants_clones(),
        _fetch_wallet_journal(),
        _fetch_corp_history(),
        _fetch_chart_data(),
    )
    implants, jump_clones = implants_clones_res
    journal, journal_error = wallet_journal_res
    _mark("fanout")

    current_wallet = cache.wallet if cache else None

    from app.config import get_settings as _get_settings_km
    _km_cfg = _get_settings_km()

    if perf_enabled():
        perf_log("character_detail", total_ms=ms_since(_t0), **_section_ms)
    return templates.TemplateResponse("character_detail.html", {
        "request": request,
        "char": char,
        "killmails_enabled": _km_cfg.killmails_enabled,
        "current_wallet": current_wallet,
        "journal": journal,
        "journal_error": journal_error,
        "chart_data_json": json.dumps(chart_data),
        "active_range": range,
        "ranges": list(_RANGE_DAYS.keys()),
        "active_skill": active_skill,
        "skillqueue": skillqueue,
        "completed_skills": completed_skills,
        "corp_history": corp_history,
        "total_sp_in_queue": total_sp_in_queue,
        "total_trained_sp": total_trained_sp,
        "queue_remaining": queue_remaining,
        "zkill": zkill,
        "kills": kills,
        "losses": losses,
        "has_assets_scope": has_assets_scope,
        "docked_at": docked_at,
        "current_system": current_system,
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


# ── Notification type labels ──────────────────────────────────────────────────
_NOTIF_LABELS = {
    "AllWarDeclaredMsg": "War Declared",
    "AllWarSurrenderMsg": "War Surrender",
    "AllWarFinishedMsg": "War Ended",
    "AllyJoinedWarMsg": "Ally Joined War",
    "CorpWarDeclaredMsg": "Corp War Declared",
    "EntosisCaptureStarted": "Entosis Started",
    "OrbitalAttacked": "POCO Attacked",
    "OrbitalReinforced": "POCO Reinforced",
    "StructureUnderAttack": "Structure Attacked",
    "StructureLostShields": "Structure Lost Shields",
    "StructureLostArmor": "Structure Lost Armor",
    "StructureDestroyed": "Structure Destroyed",
    "StructureOnline": "Structure Online",
    "StructureFuelAlert": "Fuel Alert",
    "StructureAnchoring": "Structure Anchoring",
    "StructureUnanchoring": "Structure Unanchoring",
    "StructureServicesOffline": "Services Offline",
    "TowerAlertMsg": "POS Alert",
    "TowerResourceAlertMsg": "POS Fuel Alert",
    "SovStructureReinforced": "Sov Reinforced",
    "SovCommandNodeEventStarted": "Sov Node Event",
    "CorpNewCEOMsg": "New CEO",
    "CorpVoteCEORevokedMsg": "CEO Vote Revoked",
    "CharAppAcceptMsg": "Application Accepted",
    "CharAppRejectMsg": "Application Rejected",
    "CharLeftCorpMsg": "Member Left Corp",
    "CorpAppNewMsg": "New Application",
    "InsurancePayoutMsg": "Insurance Payout",
    "InsuranceFirstShipMsg": "First Ship Insurance",
    "BillPaidCorpAllMsg": "Bill Paid",
    "BountyClaimMsg": "Bounty Claimed",
    "KillReportVictim": "Kill Report (Loss)",
    "KillReportFinalBlow": "Kill Report (Final Blow)",
    "MoonminingExtractionStarted": "Moon Extraction Started",
    "MoonminingExtractionFinished": "Moon Chunk Ready",
    "MoonminingAutomaticFracture": "Moon Auto-Fracture",
    "MoonminingLaserFired": "Moon Laser Fired",
    "SkyhookDestructionImminent": "Skyhook Threatened",
    "OrbitalBombardmentComplete": "Orbital Bombardment",
    "CloneActivationMsg": "Clone Activated",
    "CloneMovedMsg": "Clone Moved",
    "JumpCloneDeleteMsg": "Clone Deleted",
    "CorpTaxChangeMsg": "Tax Rate Changed",
}


def _notif_label(notif_type: str) -> str:
    if notif_type in _NOTIF_LABELS:
        return _NOTIF_LABELS[notif_type]
    # Convert CamelCase to spaced words
    import re
    label = re.sub(r"Msg$", "", notif_type)
    label = re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
    return label


def _parse_notif_fields(text: str) -> dict:
    """Parse notification YAML text into a dict of key->value."""
    fields = {}
    if not text:
        return fields
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("-"):
            key, _, val = line.partition(":")
            val = val.strip()
            if val:
                fields[key.strip()] = val
    return fields


async def _enrich_notif_summary(notif_type: str, text: str, db) -> str:
    """Build a human-readable summary from notification YAML text."""
    fields = _parse_notif_fields(text)
    if not fields:
        return ""

    # Format ISK amounts
    def _fmt_isk(val):
        try:
            v = float(val)
            if v >= 1e9:
                return f"{v/1e9:.2f}B ISK"
            if v >= 1e6:
                return f"{v/1e6:.1f}M ISK"
            if v >= 1e3:
                return f"{v/1e3:.0f}K ISK"
            return f"{v:,.0f} ISK"
        except (ValueError, TypeError):
            return val

    # Type-specific formatting
    if notif_type in ("KillReportVictim", "KillReportFinalBlow"):
        ship_id = fields.get("victimShipTypeID") or fields.get("shipTypeID")
        parts = []
        if ship_id:
            try:
                name = await sde.type_id_to_name(db, int(ship_id))
                if name:
                    parts.append(name)
            except (ValueError, TypeError):
                pass
        if not parts:
            return "Ship destroyed"
        return " — ".join(parts)

    if notif_type in ("InsurancePayoutMsg",):
        amount = fields.get("amount")
        if amount:
            return _fmt_isk(amount)
        return ""

    if notif_type == "RaffleFinished":
        type_id = fields.get("type_id")
        ticket_count = fields.get("ticket_count")
        parts = []
        if type_id:
            try:
                name = await sde.type_id_to_name(db, int(type_id))
                if name:
                    parts.append(name)
            except (ValueError, TypeError):
                pass
        if ticket_count:
            parts.append(f"{ticket_count} tickets")
        return " — ".join(parts) if parts else ""

    if notif_type == "GameTimeAdded":
        return "Game time added to account"

    if notif_type in ("RaffleCreated", "RaffleExpired"):
        type_id = fields.get("type_id")
        if type_id:
            try:
                name = await sde.type_id_to_name(db, int(type_id))
                if name:
                    return name
            except (ValueError, TypeError):
                pass
        return ""

    if notif_type in ("CloneActivationMsg", "CloneMovedMsg", "JumpCloneDeleteMsg"):
        loc_id = fields.get("cloneStationID") or fields.get("stationID")
        if loc_id:
            try:
                from app.db.sde_models import SDEStation
                st_result = await db.execute(
                    select(SDEStation).where(SDEStation.station_id == int(loc_id))
                )
                station = st_result.scalar_one_or_none()
                if station:
                    return station.station_name
            except (ValueError, TypeError):
                pass
        return ""

    if notif_type in ("BountyClaimMsg",):
        amount = fields.get("amount")
        return _fmt_isk(amount) if amount else ""

    if notif_type in ("StructureUnderAttack", "StructureLostShields", "StructureLostArmor", "StructureDestroyed"):
        parts = []
        sys_id = fields.get("solarsystemID") or fields.get("solarSystemID")
        struct_type = fields.get("structureTypeID")
        if struct_type:
            name = await sde.type_id_to_name(db, int(struct_type))
            if name:
                parts.append(name)
        if sys_id:
            info = await sde.system_info(db, int(sys_id))
            if info:
                parts.append(info["system_name"])
        shield = fields.get("shieldPercentage")
        armor = fields.get("armorPercentage")
        hull = fields.get("hullPercentage")
        if shield or armor or hull:
            hp = []
            if shield:
                hp.append(f"S:{float(shield):.0f}%")
            if armor:
                hp.append(f"A:{float(armor):.0f}%")
            if hull:
                hp.append(f"H:{float(hull):.0f}%")
            parts.append(" ".join(hp))
        return " — ".join(parts) if parts else ""

    if notif_type in ("StructureFuelAlert", "StructureServicesOffline", "StructureAnchoring",
                       "StructureUnanchoring", "StructureOnline"):
        sys_id = fields.get("solarsystemID") or fields.get("solarSystemID")
        struct_type = fields.get("structureTypeID")
        parts = []
        if struct_type:
            name = await sde.type_id_to_name(db, int(struct_type))
            if name:
                parts.append(name)
        if sys_id:
            info = await sde.system_info(db, int(sys_id))
            if info:
                parts.append(info["system_name"])
        return " in ".join(parts) if parts else ""

    if notif_type in ("SovStructureReinforced", "SovCommandNodeEventStarted"):
        sys_id = fields.get("solarSystemID")
        if sys_id:
            info = await sde.system_info(db, int(sys_id))
            if info:
                return info["system_name"]
        return ""

    if notif_type in ("MoonminingExtractionStarted", "MoonminingExtractionFinished",
                       "MoonminingAutomaticFracture", "MoonminingLaserFired"):
        sys_id = fields.get("solarSystemID")
        struct_type = fields.get("structureTypeID")
        parts = []
        if struct_type:
            name = await sde.type_id_to_name(db, int(struct_type))
            if name:
                parts.append(name)
        if sys_id:
            info = await sde.system_info(db, int(sys_id))
            if info:
                parts.append(info["system_name"])
        return " in ".join(parts) if parts else ""

    if notif_type in ("CharLeftCorpMsg", "CharAppAcceptMsg", "CharAppRejectMsg", "CorpAppNewMsg"):
        char_id = fields.get("charID") or fields.get("applicationCharID")
        corp_id = fields.get("corpID")
        parts = []
        if char_id:
            try:
                from app.esi.client import ESIClient as _PC
                pc = _PC("")
                names = await pc.post_public("/universe/names/", [int(char_id)])
                if names:
                    parts.append(names[0]["name"])
            except Exception:
                pass
        if corp_id:
            try:
                from app.esi.client import ESIClient as _PC
                pc = _PC("")
                names = await pc.post_public("/universe/names/", [int(corp_id)])
                if names:
                    parts.append(names[0]["name"])
            except Exception:
                pass
        return " — ".join(parts) if parts else ""

    if notif_type in ("BillPaidCorpAllMsg",):
        amount = fields.get("amount")
        return _fmt_isk(amount) if amount else ""

    if notif_type in ("CorpTaxChangeMsg",):
        new_rate = fields.get("newTaxRate")
        if new_rate:
            try:
                return f"New rate: {float(new_rate)*100:.0f}%"
            except (ValueError, TypeError):
                pass
        return ""

    # Generic: skip hashes, long IDs, empty values; show first useful field
    skip_keys = {"killMailHash", "killMailID", "notification_id", "hash", "logDateTime",
                  "raffle_id", "itemID", "charID", "corpID", "allianceID", "applicationCharID"}
    import re as _re
    _uuid_pattern = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.IGNORECASE)
    _hex_pattern = _re.compile(r'^[0-9a-f]{20,}$', _re.IGNORECASE)
    for key, val in fields.items():
        if key in skip_keys:
            continue
        if len(val) > 40:
            continue  # Skip long hashes/IDs
        if _uuid_pattern.match(val) or _hex_pattern.match(val):
            continue  # Skip UUIDs and hex hashes
        # Format numbers that look like ISK
        if key.lower().endswith(("amount", "payout", "isk", "tax", "bounty")):
            return f"{_fmt_isk(val)}"
        # Resolve type IDs
        if key.lower().endswith("typeid"):
            try:
                name = await sde.type_id_to_name(db, int(val))
                if name:
                    return name
            except (ValueError, TypeError):
                pass
        # Resolve system IDs
        if key.lower().endswith("systemid"):
            try:
                info = await sde.system_info(db, int(val))
                if info:
                    return info["system_name"]
            except (ValueError, TypeError):
                pass
        # Skip raw numeric IDs
        try:
            int_val = int(float(val))
            if int_val > 10000:
                continue  # Likely an unresolved ID
        except (ValueError, TypeError):
            pass
        return f"{val[:60]}"
    return ""


@router.get("/character/{character_id}/mail-partial", response_class=HTMLResponse)
async def character_mail_partial(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Htmx partial: mail list for a character."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()

    mail_data = json.loads(cache.mail_json) if cache and cache.mail_json else None
    if mail_data is None or mail_data == "no_scope":
        return templates.TemplateResponse("partials/mail_panel.html", {
            "request": request, "character_id": character_id,
            "mail_headers": [], "mail_error": "Mail scope not available — re-authorize to view mail.",
        })

    headers = mail_data.get("headers", []) if isinstance(mail_data, dict) else []
    return templates.TemplateResponse("partials/mail_panel.html", {
        "request": request, "character_id": character_id,
        "mail_headers": headers, "mail_error": None,
    })


@router.get("/character/{character_id}/mail/{mail_id}", response_class=HTMLResponse)
async def character_mail_body(character_id: int, mail_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Htmx partial: fetch and render a single mail body."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse('<div class="b-empty">Character not found</div>')

    try:
        from app.esi.client import refresh_token as _refresh
        token = await _refresh(char, db)
        from app.esi.client import ESIClient
        client = ESIClient(token, db=db)
        mail = await esi_char.get_mail(client, character_id, mail_id)

        body = mail.get("body", "")
        subject = mail.get("subject", "(No Subject)")
        timestamp = mail.get("timestamp", "")
        if timestamp:
            timestamp = timestamp[:16].replace("T", " ")

        # Resolve sender name
        sender_id = mail.get("from")
        sender_name = None
        if sender_id:
            try:
                info = await client.post_public("/universe/names/", [sender_id])
                if info:
                    sender_name = info[0].get("name")
            except Exception:
                sender_name = str(sender_id)

        # Strip HTML tags from body for clean display
        import re
        body = re.sub(r"<br\s*/?>", "\n", body)
        body = re.sub(r"<[^>]+>", "", body)
        body = body.strip()

        return templates.TemplateResponse("partials/mail_body.html", {
            "request": request, "subject": subject, "body": body,
            "timestamp": timestamp, "sender_name": sender_name, "recipients": [],
        })
    except Exception as e:
        return HTMLResponse(f'<div class="b-empty">Failed to load mail: {type(e).__name__}</div>')


@router.get("/character/{character_id}/notifications-partial", response_class=HTMLResponse)
async def character_notifications_partial(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Htmx partial: notification list for a character."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()

    notif_data = json.loads(cache.notifications_json) if cache and cache.notifications_json else None
    if notif_data is None or notif_data == "no_scope":
        return templates.TemplateResponse("partials/notifications_panel.html", {
            "request": request,
            "notifications": [], "notif_error": "Notification scope not available — re-authorize.",
        })

    raw_notifs = notif_data.get("notifications", []) if isinstance(notif_data, dict) else []
    enriched = []
    for n in raw_notifs:
        ntype = n.get("type", "Unknown")
        summary = await _enrich_notif_summary(ntype, n.get("text", ""), db)
        enriched.append({
            "notification_id": n.get("notification_id"),
            "type": ntype,
            "type_label": _notif_label(ntype),
            "summary": summary,
            "timestamp": n.get("timestamp", ""),
            "is_read": n.get("is_read", True),
            "sender_id": n.get("sender_id"),
        })

    return templates.TemplateResponse("partials/notifications_panel.html", {
        "request": request,
        "notifications": enriched, "notif_error": None,
    })


@router.get("/character/{character_id}/kill-stats", response_class=HTMLResponse)
async def character_kill_stats(
    character_id: int,
    request: Request,
    year: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Combat Profile partial — loaded via htmx on the character detail page.
    All panels read from the local killmails table (no ESI)."""
    from app.config import get_settings as _gs
    if not _gs().killmails_enabled:
        return HTMLResponse("")

    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("<div class='b-empty'>Forbidden.</div>", status_code=403)

    char_row = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_row.scalar_one_or_none()
    if not char:
        return HTMLResponse("<div class='b-empty'>Not your character.</div>", status_code=404)

    import asyncio as _asyncio
    import math as _math
    from app.intel import kill_queries as kq

    current_year = datetime.now(timezone.utc).year
    if year is None:
        year = current_year

    (
        summary,
        ships,
        weapons,
        systems,
        autopsy,
        streaks_d,
        gang,
        cal_buckets,
        untouchable,
        profitability,
        ship_timeseries,
    ) = await _asyncio.gather(
        kq.character_summary(character_id, days=90),
        kq.top_ships_used(character_id, days=90, limit=8),
        kq.top_weapons_used(character_id, days=90, limit=8),
        kq.top_systems(character_id, days=90, limit=8),
        kq.loss_autopsy(character_id, days=90),
        kq.streaks(character_id),
        kq.solo_gang_split(character_id, days=90),
        kq.calendar_buckets(character_id, year=year),
        kq.untouchable_ships(character_id, min_uses=5),
        kq.profitability_by_ship(character_id, days=90),
        kq.ship_usage_timeseries(character_id, days=180),
    )

    # Trim profitability to top 10 by absolute net so the bar chart stays readable
    profitability = sorted(profitability, key=lambda x: abs(x["net"]), reverse=True)[:10]

    # Backfill progress hint
    from app.db.models import CharacterKillIngest
    ingest_row = await db.get(CharacterKillIngest, character_id)
    backfill_complete = bool(ingest_row and ingest_row.backfill_complete)

    # Bulk-resolve SDE names + system security
    from app.db.sde_models import SDEType, SDESystem
    type_ids: set[int] = (
        {s["ship_type_id"] for s in ships}
        | {w["weapon_type_id"] for w in weapons}
        | {u["ship_type_id"] for u in untouchable}
        | {p["ship_type_id"] for p in profitability}
        | set(ship_timeseries.keys())
    )
    system_ids = {s["system_id"] for s in systems}

    type_names: dict[int, str] = {}
    system_names: dict[int, str] = {}
    system_security: dict[int, float] = {}
    if type_ids:
        trows = await db.execute(
            select(SDEType.type_id, SDEType.type_name).where(SDEType.type_id.in_(type_ids))
        )
        type_names = {tid: name for tid, name in trows.all()}
    if system_ids:
        srows = await db.execute(
            select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
            .where(SDESystem.system_id.in_(system_ids))
        )
        for sid, name, sec in srows.all():
            system_names[sid] = name
            if sec is not None:
                system_security[sid] = sec

    gang_total = sum(gang.values())

    # ── Calendar grid: 53 weeks × 7 days (GitHub-style) ───────────────
    year_start = datetime(year, 1, 1)
    start_weekday = year_start.weekday()  # 0=Mon .. 6=Sun
    is_leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
    days_in_year = 366 if is_leap else 365
    cal_cells: list[dict] = []
    for _ in range(start_weekday):
        cal_cells.append({"empty": True})
    for day_offset in range(days_in_year):
        d = year_start + timedelta(days=day_offset)
        key = d.strftime("%Y-%m-%d")
        b = cal_buckets.get(key, {})
        cal_cells.append({
            "empty": False,
            "date": d,
            "date_str": key,
            "kills": b.get("kills", 0),
            "losses": b.get("losses", 0),
            "total": b.get("total", 0),
        })
    cal_max = max((c.get("total", 0) for c in cal_cells if not c.get("empty")), default=0)

    # ── Radar profile (6 axes, each normalized to 0–100) ──────────────
    total_engagements = summary["kills"] + summary["losses"]
    avg_target_value = (summary["isk_destroyed"] / summary["kills"]) if summary["kills"] else 0
    distinct_top_systems = len(system_ids)
    tot_isk = summary["isk_destroyed"] + summary["isk_lost"]
    isk_efficiency = (summary["isk_destroyed"] / tot_isk * 100) if tot_isk > 0 else 0

    def _log_norm(v: float, ceiling_at: float) -> float:
        if v <= 0:
            return 0.0
        return float(min(100, _math.log1p(v) / _math.log1p(ceiling_at) * 100))

    radar_labels = ["Kill Volume", "ISK Eff %", "Solo %", "Gang %", "Avg Target", "Spread"]
    radar_values = [
        _log_norm(summary["kills"], 200),
        round(isk_efficiency, 1),
        round((gang["solo"] / gang_total * 100) if gang_total else 0, 1),
        round(((gang["small"] + gang["medium"] + gang["fleet"]) / gang_total * 100) if gang_total else 0, 1),
        _log_norm(avg_target_value, 1_000_000_000),
        _log_norm(distinct_top_systems, 50),
    ]
    radar_raw = [
        summary["kills"],
        round(isk_efficiency, 1),
        gang["solo"],
        gang["small"] + gang["medium"] + gang["fleet"],
        avg_target_value,
        distinct_top_systems,
    ]

    # ── Ship Usage Stream datasets ────────────────────────────────────
    ts_datasets = []
    for tid, weekly in ship_timeseries.items():
        ts_datasets.append({
            "label": type_names.get(tid, f"Type {tid}"),
            "data": weekly,
            "fill": True,
            "tension": 0.3,
        })
    ts_weeks = max((len(d["data"]) for d in ts_datasets), default=0)

    return templates.TemplateResponse("partials/character_kill_stats.html", {
        "request": request,
        "char": char,
        "character_id": character_id,
        "year": year,
        "current_year": current_year,
        "summary": summary,
        "ships": ships,
        "weapons": weapons,
        "systems": systems,
        "autopsy": autopsy,
        "autopsy_total": sum(autopsy.values()),
        "type_names": type_names,
        "system_names": system_names,
        "system_security": system_security,
        "streak": streaks_d,
        "gang_split": gang,
        "gang_total": gang_total,
        "cal_cells": cal_cells,
        "cal_max": cal_max,
        "untouchable": untouchable,
        "profitability": profitability,
        "radar_labels": radar_labels,
        "radar_values": radar_values,
        "radar_raw": radar_raw,
        "ts_datasets": ts_datasets,
        "ts_weeks": ts_weeks,
        "backfill_complete": backfill_complete,
    })
