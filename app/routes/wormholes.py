"""Wormhole Reference — System database, connection matrix, effects reference.

Provides searchable/filterable J-space system database, wormhole type lookup,
system detail pages with celestials and zKillboard activity, and system effects.
"""
import asyncio
import json
import logging
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal, get_db
from app.esi.client import ESIClient
from app.sde import lookup as sde
from app.intel.safety import zkb_get, fetch_killmail

router = APIRouter(tags=["wormholes"])
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger(__name__)

# Load community wormhole data once at import time
_WH_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "wormholes.json"
_wh_data: dict = {}
try:
    _wh_data = json.loads(_WH_DATA_PATH.read_text())
except Exception as e:
    log.warning("Failed to load wormholes.json: %s", e)


def _class_label(wh_class: int | None) -> str:
    if wh_class is None:
        return "?"
    return _wh_data.get("class_labels", {}).get(str(wh_class), f"C{wh_class}")


def _class_color(wh_class: int | None) -> str:
    if wh_class is None:
        return "var(--muted)"
    return _wh_data.get("class_colors", {}).get(str(wh_class), "var(--text)")


def _effect_label(effect: str | None) -> str:
    if not effect:
        return ""
    effects = _wh_data.get("effects", {})
    info = effects.get(effect)
    return info["name"] if info else effect.replace("_", " ").title()


def _format_mass(kg: float | None) -> str:
    if kg is None:
        return "—"
    if kg >= 1_000_000_000:
        return f"{kg / 1_000_000_000:,.1f}B kg"
    if kg >= 1_000_000:
        return f"{kg / 1_000_000:,.0f}M kg"
    return f"{kg:,.0f} kg"


def _format_time(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    hours = minutes / 60
    if hours == int(hours):
        return f"{int(hours)} hours"
    return f"{hours:.1f} hours"


def _ship_size_hint(max_jump_mass: float | None) -> str:
    if max_jump_mass is None:
        return ""
    m = max_jump_mass
    if m <= 5_000_000:
        return "frigate"
    if m <= 20_000_000:
        return "destroyer"
    if m <= 62_000_000:
        return "battlecruiser"
    if m <= 375_000_000:
        return "battleship"
    if m <= 1_000_000_000:
        return "capital"
    if m <= 1_800_000_000:
        return "capital"
    return "supercapital"


# ── System Database ─────────────────────────────────────────────────────────

EFFECT_COLORS = {
    "pulsar": "#5dffd2",
    "black_hole": "#4a9eda",
    "cataclysmic_variable": "#d4a06a",
    "magnetar": "#cc4433",
    "red_giant": "#e76363",
    "wolf_rayet": "#a8cb70",
}


def _finder_context() -> dict:
    """Everything wormholes.html (the system finder) needs. Shared by the
    finder page and the system page's not-found branch, which re-renders
    the finder under an error banner (ISS-123: that branch once passed only
    part of this and the template's colour lookups raised a 500)."""
    return {
        "effects_list": list(_wh_data.get("effects", {}).keys()),
        "effects_labels": {k: v["name"] for k, v in _wh_data.get("effects", {}).items()},
        "effect_colors": EFFECT_COLORS,
        "class_colors": _wh_data.get("class_colors", {}),
    }


@router.get("/wormholes", response_class=HTMLResponse)
async def wormhole_systems_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "wormholes.html", _finder_context())


@router.get("/wormholes/search", response_class=HTMLResponse)
async def wormhole_systems_search(
    request: Request,
    q: str = Query(""),
    wh_class: str = Query(""),
    effect: str = Query(""),
    static_dest: str = Query(""),
    planets: str = Query(""),
    perfect_pi: str = Query(""),
    shattered: str = Query(""),
    page: int = Query(1),
    db: AsyncSession = Depends(get_db),
):
    per_page = 50
    offset = (page - 1) * per_page
    search_q = q.strip() if q else ""
    effect_val = effect.strip() if effect.strip() else None

    # Parse multi-select class filter (comma-separated). Supports integer
    # class IDs (1-13) plus two class-set sentinels:
    #   "drifter"      → classes 14-18 (the 5 named drifter complexes).
    #   "uncatalogued" → systems missing from system_statics map.
    # Both OR with the integer class buttons.
    raw_class_tokens = [c.strip() for c in wh_class.split(",") if c.strip()] if wh_class.strip() else []
    class_list = [int(c) for c in raw_class_tokens if c.isdigit()]
    include_drifter = "drifter" in raw_class_tokens
    include_uncatalogued = "uncatalogued" in raw_class_tokens

    # Shattered is a separate modifier param that ANDs with whatever class
    # filter is active. C6 + shattered=1 returns C6 systems that are also
    # shattered (5 systems), not their union. Lives in its own row in the
    # UI; backend lives on its own query string key for the same reason.
    shattered_on = shattered.strip() == "1"

    # Parse static destination filter (comma-separated class IDs)
    static_dest_list = [int(c) for c in static_dest.split(",") if c.strip().isdigit()] if static_dest.strip() else []

    # Parse planet type filter (comma-separated names)
    planet_list = [p.strip() for p in planets.split(",") if p.strip()] if planets.strip() else []

    perfect_pi_on = perfect_pi.strip() == "1"

    # Require at least one filter or 4+ char search
    has_filter = class_list or include_drifter or include_uncatalogued or shattered_on or effect_val or static_dest_list or planet_list or perfect_pi_on
    if not has_filter and len(search_q) < 4:
        return HTMLResponse(
            '<div class="b-empty">Select filters above or type at least 4 characters to search.</div>'
        )

    systems, total = await sde.get_wormhole_systems(
        db,
        class_filter=class_list or None,
        include_drifter=include_drifter,
        include_uncatalogued=include_uncatalogued,
        require_shattered=shattered_on,
        effect_filter=effect_val,
        static_dest_filter=static_dest_list or None,
        planet_filter=planet_list or None,
        perfect_pi=perfect_pi_on,
        search=search_q if search_q else None,
        limit=per_page,
        offset=offset,
        wh_data=_wh_data,
    )

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return templates.TemplateResponse(request, "partials/wormhole_system_list.html", {"systems": systems,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "class_label": _class_label,
        "class_color": _class_color,
        "effect_label": _effect_label,
        "wh_data": _wh_data,
        # Active filter values, threaded into pagination links server-side so
        # paging doesn't drop the active toggle-button filters (there's no
        # persistent form element for hx-include to pull from — filters live
        # in ephemeral .wf-btn.is-on state on the parent page).
        "q": search_q,
        "wh_class": wh_class,
        "effect": effect,
        "static_dest": static_dest,
        "planets": planets,
        "perfect_pi": perfect_pi,
        "shattered": shattered})


# ── System Detail ───────────────────────────────────────────────────────────

async def build_wh_system_context(db: AsyncSession, name: str) -> dict | None:
    """Shared J-system context: SDE detail + celestials + statics/effect/
    wandering resolution. Used by the system detail page and the live
    tracker (app/routes/wh_tracker.py, T-034). Returns None when the name
    isn't a known wormhole system."""
    sys_detail = await sde.get_wormhole_system_detail(db, name)
    if not sys_detail:
        return None

    celestials = await sde.get_system_celestials(db, sys_detail["system_id"])

    # Community data
    system_name = sys_detail["system_name"]
    # ISS-014: 37 J-systems are absent from the community-sourced
    # system_statics map (5 named drifter complexes + 32 shattered
    # class-13 J-numbered). The key being missing vs being present-
    # but-empty is the difference between "data not curated" and
    # "system has no statics" — surface that to the template so the
    # UI can render an explicit note instead of silently omitting
    # the section.
    statics_map = _wh_data.get("system_statics", {})
    statics = statics_map.get(system_name, [])
    statics_known = system_name in statics_map
    effect_key = _wh_data.get("system_effects", {}).get(system_name)
    effect_info = None
    if effect_key:
        effects_data = _wh_data.get("effects", {})
        effect_info = effects_data.get(effect_key)
        if effect_info:
            effect_info = dict(effect_info)
            effect_info["key"] = effect_key

    # Resolve statics to wormhole type info
    static_details = []
    for static_code in statics:
        wh_type = await sde.get_wormhole_type_by_name(db, static_code)
        meta = _wh_data.get("wormhole_meta", {}).get(static_code, {})
        static_details.append({
            "code": static_code,
            "target_class": wh_type["target_class"] if wh_type else None,
            "respawn": meta.get("respawn", "unknown"),
        })

    # Possible wandering connections for this class
    wh_class = sys_detail.get("wh_class")
    wandering = []
    seen_wandering: set[str] = set()
    if wh_class:
        class_key = f"c{wh_class}" if wh_class <= 6 else {7: "hs", 8: "ls", 9: "ns"}.get(wh_class, "")
        matrix = _wh_data.get("connection_matrix", {})
        # Collect all wormhole types that can appear FROM other classes TO this class
        for from_class, destinations in matrix.items():
            for dest_class, codes in destinations.items():
                if dest_class == class_key:
                    for code in codes:
                        if code in seen_wandering:
                            continue
                        meta = _wh_data.get("wormhole_meta", {}).get(code, {})
                        if meta.get("respawn") != "static" or from_class == "?":
                            seen_wandering.add(code)
                            wh_type = await sde.get_wormhole_type_by_name(db, code)
                            wandering.append({
                                "code": code,
                                "target_class": wh_type["target_class"] if wh_type else None,
                                "from_class": from_class,
                            })

    return {"system": sys_detail,
        "celestials": celestials,
        "statics": static_details,
        "statics_known": statics_known,
        "wandering": wandering,
        "effect": effect_info,
        "wh_class": wh_class,
        "class_label": _class_label,
        "class_color": _class_color,
        "effect_label": _effect_label,
        "wh_data": _wh_data}


@router.get("/wormholes/system/{name}", response_class=HTMLResponse)
async def wormhole_system_detail(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    ctx = await build_wh_system_context(db, name)
    if ctx is None:
        return templates.TemplateResponse(
            request, "wormholes.html",
            {**_finder_context(), "error": f"System '{name}' not found."},
            status_code=404)

    return templates.TemplateResponse(request, "wormhole_system.html", ctx)


@router.get("/wormholes/system/{name}/kills", response_class=HTMLResponse)
async def wormhole_system_kills(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=30, le=90),
):
    """Lazy-loaded kill activity for a wormhole system."""
    # Only login-only pages embed this, and a build spends the ESI budget
    # every signed-in sync shares (ISS-167).
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    sys_detail = await sde.get_wormhole_system_detail(db, name)
    if not sys_detail:
        return HTMLResponse('<div class="b-empty">System not found.</div>')

    # Normalise to one of the supported ranges
    if days not in (30, 60, 90):
        days = 30

    ctx = await _kill_activity(sys_detail["system_id"], days)
    # The range buttons link back with the name as this request spelled it.
    return templates.TemplateResponse(request, "partials/wormhole_kills.html",
                                      {**ctx, "system_name": name})


# ── Kill activity (ISS-167) ─────────────────────────────────────────────────
# A build costs one zKillboard call and one ESI fetch per listed kill, from
# the ESI budget and throttle that every signed-in sync shares. So a build is
# reused for a few minutes, runs once per (system, range) however many
# requests want it, stops fetching at the range's cutoff, and all builds
# together hold at most KILLMAIL_FETCH_CONCURRENCY of the ten ESI slots
# app.intel.safety allows, which the gate check uses too.
KILLS_CACHE_TTL = 300              # seconds; zkb_get keeps the kill list as long
KILLS_CACHE_ENTRIES = 256
MAX_KILLMAIL_FETCHES = 200         # zKillboard lists no more than this anyway
KILLMAIL_FETCH_CONCURRENCY = 5     # what one request used to run on its own
KILLMAIL_FETCH_BATCH = 10

_NO_KILLS = {"kill_count": 0, "kills": [], "heatmap": [], "corps": [],
             "alliances": [], "most_recent": None}

_kills_cache: "OrderedDict[tuple[int, int], tuple[float, dict]]" = OrderedDict()
_kills_building: dict[tuple[int, int], asyncio.Future] = {}
_kills_fetch_sem: asyncio.Semaphore | None = None
_kills_loop = None


def _kills_fetch_gate() -> asyncio.Semaphore:
    """The fetch gate for the running loop. asyncio objects belong to one
    loop: production has one, each test client its own, so builds left by
    another loop are dropped with its gate."""
    global _kills_fetch_sem, _kills_loop
    loop = asyncio.get_running_loop()
    if _kills_loop is not loop:
        _kills_building.clear()
        _kills_fetch_sem, _kills_loop = asyncio.Semaphore(KILLMAIL_FETCH_CONCURRENCY), loop
    return _kills_fetch_sem


async def _kill_activity(system_id: int, days: int) -> dict:
    """The kill partial's context for one system and range."""
    gate = _kills_fetch_gate()
    key = (system_id, days)
    hit = _kills_cache.get(key)
    if hit and hit[0] > time.monotonic():
        _kills_cache.move_to_end(key)
        return hit[1]
    build = _kills_building.get(key)
    if build is None:
        build = asyncio.ensure_future(_build_kill_activity(system_id, days, gate))
        _kills_building[key] = build
        build.add_done_callback(lambda done: _kill_activity_built(key, done))
    # Shielded, so a request that goes away leaves the build running for
    # the others waiting on it and for the cache.
    ctx = await asyncio.shield(build)
    return ctx if ctx is not None else {**_NO_KILLS, "days": days}


def _kill_activity_built(key: tuple[int, int], build: asyncio.Future) -> None:
    if _kills_building.get(key) is build:
        del _kills_building[key]
    # exception() also marks a failure as seen when no request is left.
    if build.cancelled() or build.exception() is not None or build.result() is None:
        return
    _kills_cache[key] = (time.monotonic() + KILLS_CACHE_TTL, build.result())
    _kills_cache.move_to_end(key)
    while len(_kills_cache) > KILLS_CACHE_ENTRIES:
        _kills_cache.popitem(last=False)


def _kill_time(km: dict | None) -> datetime | None:
    if not km or not km.get("killmail_time"):
        return None
    try:
        return datetime.fromisoformat(km["killmail_time"].replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def _stub_id(stub) -> int:
    kid = stub.get("killmail_id") if isinstance(stub, dict) else None
    return kid if isinstance(kid, int) else 0


async def _build_kill_activity(system_id: int, days: int, gate: asyncio.Semaphore) -> dict | None:
    """Fetch and aggregate a system's kills. None when zKillboard lists none
    (or failed), which is not worth caching."""
    # Fetch recent killmails from zKillboard (up to 200 most recent)
    try:
        kills_data = await zkb_get(f"/systemID/{system_id}/")
    except Exception:
        kills_data = []

    if not kills_data:
        return None

    # Build npc flag lookup from zkb data
    npc_flags: dict[int, bool] = {}
    for km in kills_data:
        kid = km.get("killmail_id")
        if kid:
            npc_flags[kid] = km.get("zkb", {}).get("npc", False)

    async def _fetch_km(km_stub):
        async with gate:
            kid = km_stub.get("killmail_id")
            khash = km_stub.get("zkb", {}).get("hash")
            if kid and khash:
                try:
                    return await fetch_killmail(kid, khash)
                except Exception:
                    pass
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Fetch full killmail details from ESI (for timestamps + corp/alliance),
    # newest first and a batch at a time. Killmail ids rise with time, so
    # once a batch reaches past the cutoff everything after it is older
    # still; a batch with nothing fetched means ESI is failing, so stop
    # asking it.
    stubs = sorted(kills_data, key=_stub_id, reverse=True)[:MAX_KILLMAIL_FETCHES]
    full_kms = []
    for start in range(0, len(stubs), KILLMAIL_FETCH_BATCH):
        batch = await asyncio.gather(
            *[_fetch_km(km) for km in stubs[start:start + KILLMAIL_FETCH_BATCH]])
        full_kms.extend(batch)
        times = [t for t in map(_kill_time, batch) if t]
        if not times or min(times) < cutoff:
            break

    # Age bucket boundaries (thirds of the range)
    third = days // 3
    cutoff_recent = now - timedelta(days=third)
    cutoff_mid = now - timedelta(days=third * 2)

    # Build activity heatmap (day-of-week × hour) with kill IDs per cell
    heatmap = [[0] * 24 for _ in range(7)]
    heatmap_ids: dict[str, list[int]] = {}
    heatmap_npc: dict[str, bool] = {}
    heatmap_age: dict[str, int] = {}
    most_recent = None
    recent_kills: list[dict] = []
    victim_type_ids: set[int] = set()
    kill_char_ids: set[int] = set()
    kill_corp_ids: set[int] = set()
    kill_alliance_ids: set[int] = set()

    # Corp/alliance aggregation (player orgs only)
    corp_kills: Counter = Counter()
    alliance_kills: Counter = Counter()

    for km in full_kms:
        kill_time = _kill_time(km)
        if kill_time is None or kill_time < cutoff:
            continue
        kid = km.get("killmail_id")

        d, h = kill_time.weekday(), kill_time.hour
        heatmap[d][h] += 1
        key = f"{d},{h}"

        if kill_time >= cutoff_recent:
            age = 0
        elif kill_time >= cutoff_mid:
            age = 1
        else:
            age = 2
        if key not in heatmap_age or age < heatmap_age[key]:
            heatmap_age[key] = age

        if kid:
            heatmap_ids.setdefault(key, []).append(kid)
            is_npc = npc_flags.get(kid, False)
            if key not in heatmap_npc:
                heatmap_npc[key] = is_npc
            elif not is_npc:
                heatmap_npc[key] = False

        if most_recent is None or kill_time > most_recent:
            most_recent = kill_time

        # Aggregate corp/alliance involvement (player orgs only)
        victim = km.get("victim", {})
        v_corp = victim.get("corporation_id")
        v_alliance = victim.get("alliance_id")
        if v_corp and v_corp >= 98_000_000:
            corp_kills[v_corp] += 1
        if v_alliance and v_alliance >= 99_000_000:
            alliance_kills[v_alliance] += 1
        for att in km.get("attackers", []):
            a_corp = att.get("corporation_id")
            a_alliance = att.get("alliance_id")
            if a_corp and a_corp >= 98_000_000:
                corp_kills[a_corp] += 1
            if a_alliance and a_alliance >= 99_000_000:
                alliance_kills[a_alliance] += 1

        # Build recent kills list (up to 15)
        if len(recent_kills) < 15:
            ship_tid = victim.get("ship_type_id")
            if ship_tid:
                victim_type_ids.add(ship_tid)
            zkb_stub = None
            for stub in kills_data:
                if stub.get("killmail_id") == kid:
                    zkb_stub = stub.get("zkb", {})
                    break
            delta_kill = now - kill_time
            if delta_kill.days >= 1:
                time_ago = f"{delta_kill.days}d ago"
            else:
                hrs = delta_kill.seconds // 3600
                time_ago = f"{hrs}h ago" if hrs > 0 else "just now"
            v_char_id = victim.get("character_id")
            v_corp_id = victim.get("corporation_id")
            v_ally_id = victim.get("alliance_id")
            if v_char_id:
                kill_char_ids.add(v_char_id)
            if v_corp_id:
                kill_corp_ids.add(v_corp_id)
            if v_ally_id:
                kill_alliance_ids.add(v_ally_id)
            recent_kills.append({
                "killmail_id": kid,
                "time": kill_time,
                "time_ago": time_ago,
                "ship_type_id": ship_tid,
                "ship_name": None,
                "character_id": v_char_id,
                "character_name": None,
                "corporation_id": v_corp_id,
                "corporation_name": None,
                "alliance_id": v_ally_id,
                "alliance_name": None,
                "value": zkb_stub.get("totalValue", 0) if zkb_stub else 0,
                "is_npc": npc_flags.get(kid, False),
            })

    # Resolve ship names
    if victim_type_ids:
        # Its own session: the build can outlive the request that started it.
        async with AsyncSessionLocal() as db:
            ship_names = await sde.type_ids_to_names(db, list(victim_type_ids))
        for kill in recent_kills:
            if kill["ship_type_id"]:
                kill["ship_name"] = ship_names.get(kill["ship_type_id"], f"Type {kill['ship_type_id']}")

    # Resolve names from ESI: top corps/alliances + kill list chars/corps/alliances
    top_corps: list[dict] = []
    top_alliances: list[dict] = []
    top_corp_ids = [cid for cid, _ in corp_kills.most_common(5)]
    top_alliance_ids = [aid for aid, _ in alliance_kills.most_common(5)]

    # Merge IDs: top active + kill list victims
    all_corp_ids = set(top_corp_ids) | kill_corp_ids
    all_alliance_ids = set(top_alliance_ids) | kill_alliance_ids

    # Resolve all IDs in one bulk POST /universe/names/ call (≤1000 mixed
    # IDs per call, returns category-tagged entries). 30-day cache TTL via
    # _ttl_for_path so repeat resolutions are free. Matches the dashboard
    # kill-pulse pattern (dashboard.py:1908). Replaces three per-ID ESI
    # endpoints that previously fired one request per character/corp/alliance.
    char_names: dict[int, str] = {}
    corp_names: dict[int, str] = {}
    alliance_names: dict[int, str] = {}

    all_ids = list(set(kill_char_ids) | all_corp_ids | all_alliance_ids)
    if all_ids:
        try:
            pub = ESIClient("")
            pub.cache_enabled = True
            for chunk_start in range(0, len(all_ids), 1000):
                chunk = all_ids[chunk_start:chunk_start + 1000]
                resolved = await pub.post_public("/universe/names/", chunk)
                for entry in resolved or []:
                    cat = entry.get("category", "")
                    eid = entry.get("id")
                    name = entry.get("name", "")
                    if not (isinstance(eid, int) and name):
                        continue
                    if cat == "character":
                        char_names[eid] = name
                    elif cat == "corporation":
                        corp_names[eid] = name
                    elif cat == "alliance":
                        alliance_names[eid] = name
        except Exception as e:
            log.warning("wormhole_kills name resolution failed: %s", e)

    # Fallback placeholders for any IDs that didn't resolve (biomassed
    # characters, dissolved corps, or a poisoned bulk batch from one bad ID).
    for cid in kill_char_ids:
        char_names.setdefault(cid, f"Char {cid}")
    for cid in all_corp_ids:
        corp_names.setdefault(cid, f"Corp {cid}")
    for aid in all_alliance_ids:
        alliance_names.setdefault(aid, f"Alliance {aid}")

    top_corps = [{"id": cid, "name": corp_names.get(cid, f"Corp {cid}"), "count": corp_kills[cid]} for cid in top_corp_ids]
    top_alliances = [{"id": aid, "name": alliance_names.get(aid, f"Alliance {aid}"), "count": alliance_kills[aid]} for aid in top_alliance_ids]

    # Backfill resolved names into recent kills
    for kill in recent_kills:
        if kill["character_id"]:
            kill["character_name"] = char_names.get(kill["character_id"])
        if kill["corporation_id"]:
            kill["corporation_name"] = corp_names.get(kill["corporation_id"])
        if kill["alliance_id"]:
            kill["alliance_name"] = alliance_names.get(kill["alliance_id"])

    filtered_count = sum(sum(row) for row in heatmap)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    max_kills = max(max(row) for row in heatmap) if filtered_count else 1

    days_ago = None
    if most_recent:
        delta = now - most_recent
        days_ago = round(delta.total_seconds() / 86400, 1)

    # Age bucket labels for the legend
    age_labels = [f"<{third}d", f"{third}-{third*2}d", f"{third*2}-{days}d"]

    return {"kill_count": filtered_count,
        "heatmap": heatmap,
        "heatmap_ids": heatmap_ids,
        "heatmap_npc": heatmap_npc,
        "heatmap_age": heatmap_age,
        "recent_kills": recent_kills,
        "day_names": day_names,
        "max_kills": max_kills,
        "most_recent": most_recent,
        "days_ago": days_ago,
        "days": days,
        "age_labels": age_labels,
        "top_corps": top_corps,
        "top_alliances": top_alliances}


# ── Wormhole Types / Connection Matrix ──────────────────────────────────────

@router.get("/wormholes/types", response_class=HTMLResponse)
async def wormhole_types_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    # Load all wormhole types from SDE for the detail lookup
    all_types = await sde.get_all_wormhole_types(db)
    # Build name -> type dict for quick lookup (strip "Wormhole " prefix)
    type_lookup = {}
    for t in all_types:
        short = t["type_name"].replace("Wormhole ", "")
        type_lookup[short] = t

    return templates.TemplateResponse(request, "wormhole_types.html", {"matrix": _wh_data.get("connection_matrix", {}),
        "wh_meta": _wh_data.get("wormhole_meta", {}),
        "type_lookup": type_lookup,
        "class_label": _class_label,
        "class_color": _class_color,
        "format_mass": _format_mass,
        "format_time": _format_time,
        "ship_size_hint": _ship_size_hint,
        "wh_data": _wh_data,
    })


@router.get("/wormholes/types/{code}", response_class=HTMLResponse)
async def wormhole_type_detail(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Detail for a wormhole type. Returns partial for htmx, full page for direct nav."""
    wh_type = await sde.get_wormhole_type_by_name(db, code)
    meta = _wh_data.get("wormhole_meta", {}).get(code, {})

    ctx = {
        "code": code,
        "wh_type": wh_type,
        "meta": meta,
        "class_label": _class_label,
        "class_color": _class_color,
        "format_mass": _format_mass,
        "format_time": _format_time,
        "ship_size_hint": _ship_size_hint,
        "wh_data": _wh_data,
    }

    # htmx request → return partial; direct navigation → full page
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/wormhole_type_detail.html", ctx)
    return templates.TemplateResponse(request, "wormhole_type_page.html", ctx)


# ── System Effects Reference ────────────────────────────────────────────────

@router.get("/wormholes/effects", response_class=HTMLResponse)
async def wormhole_effects_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    return templates.TemplateResponse(request, "wormhole_effects.html", {"effects": _wh_data.get("effects", {}),
    })
