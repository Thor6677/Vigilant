import asyncio
import json
import logging
import httpx
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, CharacterAssetCache, AsyncSessionLocal
from app.db.cache import cache_stats
from app.routes.characters import _process_skillqueue, group_skill_data
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import market as esi_market
from app.esi import industry as esi_industry
from app.esi import universe as esi_universe
from app.esi import assets as esi_assets
from app.sde import lookup as sde

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _sec_color_val(sec: float | None) -> str:
    """Tailwind colour class for a raw security status float."""
    if sec is None:
        return "text-eve-muted"
    if sec >= 5.0:
        return "text-sky-400"
    if sec >= 0.0:
        return "text-yellow-400"
    return "text-eve-danger"

templates.env.globals["sec_color_val"] = _sec_color_val

# Character IDs that are either actively syncing or queued to sync.
# Maps character_id -> timestamp when added to queue.
# Prevents duplicate _sync_all_task spawns across rapid page loads.
# Timestamps allow detection of stuck characters (in queue > 5 minutes).
_queued_sync: dict[int, datetime] = {}

# Hard serialisation lock — only one _sync_task can execute at a time,
# regardless of how many coroutines have been created (manual syncs, etc.).
_sync_lock: asyncio.Lock | None = None

# Per-character locks for token refresh. Serialises concurrent _client_for()
# calls for the same character so asyncio.gather() can't trigger two
# simultaneous SSO refresh requests (EVE SSO rotates refresh tokens, so a
# second concurrent request with the old token would fail).
_token_locks: dict[int, asyncio.Lock] = {}


def _get_sync_lock() -> asyncio.Lock:
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()
    return _sync_lock


def _get_token_lock(character_id: int) -> asyncio.Lock:
    if character_id not in _token_locks:
        _token_locks[character_id] = asyncio.Lock()
    return _token_locks[character_id]

# ── ESI cache timers (seconds) — from ESI swagger Cache-Control: max-age ─────
# https://esi.evetech.net/latest/swagger.json
FIELD_CACHE_SECONDS: dict[str, int] = {
    "wallet":        120,   # ESI max-age: 120s
    "location":       30,   # ESI max-age:   5s  — 30s is adequate for a dashboard
    "industry":      300,   # ESI max-age: 300s
    "clones":       3600,   # ESI max-age: 3600s
    "orders":        300,   # ESI max-age: 300s
    "mail":           30,   # ESI max-age:  30s
    "notifications": 600,   # ESI max-age: 600s
    "contracts":     300,   # ESI max-age: 300s
    "pi":            600,   # ESI max-age: 600s
    "skillqueue":    120,   # ESI max-age: 120s
    "zkill":        3600,   # zkillboard — 1h is plenty
    "assets":       3600,   # ESI max-age: 3600s
}

FIELD_SCOPES: dict[str, str] = {
    "wallet":        "esi-wallet.read_character_wallet.v1",
    "location":      "esi-location.read_location.v1",
    "industry":      "esi-industry.read_character_jobs.v1",
    "clones":        "esi-clones.read_clones.v1",
    "orders":        "esi-markets.read_character_orders.v1",
    "mail":          "esi-mail.read_mail.v1",
    "notifications": "esi-characters.read_notifications.v1",
    "contracts":     "esi-contracts.read_character_contracts.v1",
    "pi":            "esi-planets.manage_planets.v1",
    "skillqueue":    "esi-skills.read_skillqueue.v1",
    "zkill":         None,   # no ESI scope required
    "assets":        "esi-assets.read_assets.v1",
}

# DB column for each field (None = special handling — wallet Float or assets separate table)
_FIELD_DB_COLUMN: dict[str, str | None] = {
    "wallet":        None,
    "location":      "location_json",
    "industry":      "industry_json",
    "clones":        "clones_json",
    "orders":        "orders_json",
    "mail":          "mail_json",
    "notifications": "notifications_json",
    "contracts":     "contracts_json",
    "pi":            "pi_json",
    "skillqueue":    "skillqueue_json",
    "zkill":         "zkill_json",
    "assets":        None,
}

# UI staleness thresholds (based on last_synced, for indicator colours)
STALE_WARNING_SECONDS = 900   # 15 min: yellow indicator
STALE_CRITICAL_SECONDS = 1800  # 30 min: red indicator + manual resync button


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_scope(char: Character, scope: str) -> bool:
    return scope in (char.scopes or "")


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "soon"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _age_str(last_synced: datetime | None) -> str | None:
    if last_synced is None:
        return None
    ls = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ls).total_seconds()
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h {int((age % 3600) // 60)}m ago"
    return f"{int(age // 86400)}d ago"


def _staleness(last_synced: datetime | None) -> str:
    if last_synced is None:
        return "never"
    ls = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ls).total_seconds()
    if age > STALE_CRITICAL_SECONDS:
        return "critical"
    if age > STALE_WARNING_SECONDS:
        return "warning"
    return "fresh"


def _any_field_stale(char: Character, cache: CharacterDashboardCache | None) -> bool:
    """Return True if any scoped field is past its ESI cache window (or never fetched)."""
    if cache is None:
        return True
    field_synced: dict = json.loads(cache.field_synced_json) if cache.field_synced_json else {}
    now = datetime.now(timezone.utc)
    scopes = char.scopes or ""
    for field, cache_secs in FIELD_CACHE_SECONDS.items():
        scope = FIELD_SCOPES[field]
        if scope and scope not in scopes:
            continue
        last_str = field_synced.get(field)
        if not last_str:
            return True
        last_dt = datetime.fromisoformat(last_str)
        if (now - last_dt).total_seconds() >= cache_secs:
            return True
    return False


async def _client_for(char: Character, db: AsyncSession) -> tuple[ESIClient | None, str | None]:
    """Return an authenticated ESI client for the character.

    Uses a per-character lock + independent DB session so concurrent field
    fetches inside asyncio.gather() don't race on the same SQLAlchemy session
    or fire duplicate SSO refresh requests (EVE rotates refresh tokens).
    """
    async with _get_token_lock(char.character_id):
        try:
            async with AsyncSessionLocal() as token_db:
                result = await token_db.execute(
                    select(Character).where(Character.character_id == char.character_id)
                )
                char_fresh = result.scalar_one_or_none()
                if char_fresh is None:
                    return None, "character_not_found"
                token = await refresh_token(char_fresh, token_db)
            return ESIClient(token, db=db), None
        except Exception as e:
            logger.warning("Token refresh failed for char %s: %s", char.character_id, e)
            return None, f"token_refresh_failed: {type(e).__name__}"


# ── ESI fetch functions ───────────────────────────────────────────────────────
# These are called by the background sync task.

async def fetch_wallet_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-wallet.read_character_wallet.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            balance = await esi_char.get_wallet(client, char.character_id)
            return char.character_id, float(balance), None
        except Exception as e:
            logger.warning("Wallet fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_location_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-location.read_location.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            loc, ship_data = await asyncio.gather(
                esi_char.get_location(client, char.character_id),
                esi_char.get_ship(client, char.character_id) if _has_scope(char, "esi-location.read_ship_type.v1") else asyncio.sleep(0, result={}),
            )
            system_id = loc.get("solar_system_id")
            result = {"system_id": system_id, "system_name": None, "security": None, "region": None, "docked_at": None,
                      "ship_type_id": None, "ship_type_name": None, "ship_name": None}
            if system_id:
                sys_info = await sde.system_info(db, system_id)
                if sys_info:
                    result["system_name"] = sys_info["system_name"]
                    result["security"] = round(float(sys_info["security"]), 1)
                    result["region"] = sys_info.get("region")
            if "station_id" in loc:
                try:
                    station = await esi_universe.get_station(client, loc["station_id"])
                    result["docked_at"] = station.get("name")
                except Exception:
                    pass
            elif "structure_id" in loc:
                try:
                    struct = await esi_universe.get_structure(client, loc["structure_id"])
                    result["docked_at"] = struct.get("name", "Unknown Structure")
                except Exception:
                    result["docked_at"] = "Unknown Structure"
            if ship_data:
                ship_type_id = ship_data.get("ship_type_id")
                result["ship_type_id"] = ship_type_id
                result["ship_name"] = ship_data.get("ship_name")
                if ship_type_id:
                    result["ship_type_name"] = await sde.type_id_to_name(db, ship_type_id)
            return char.character_id, result, None
        except Exception as e:
            logger.warning("Location fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_industry_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-industry.read_character_jobs.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            jobs = await esi_industry.get_character_jobs(client, char.character_id, include_completed=False)
            active = [j for j in jobs if j.get("status") == "active"]
            if not active:
                return char.character_id, {"active_count": 0, "soonest_time_str": None, "soonest_product": None}, None
            now = datetime.now(timezone.utc)
            soonest_end, soonest_job = None, None
            for job in active:
                end_raw = job.get("end_date")
                if end_raw:
                    end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                    if soonest_end is None or end < soonest_end:
                        soonest_end, soonest_job = end, job
            soonest_product, soonest_time_str = None, None
            if soonest_job:
                pid = soonest_job.get("product_type_id")
                if pid:
                    soonest_product = await sde.type_id_to_name(db, pid)
                if soonest_end:
                    soonest_time_str = _format_duration((soonest_end - now).total_seconds())
            return char.character_id, {"active_count": len(active), "soonest_time_str": soonest_time_str, "soonest_product": soonest_product}, None
        except Exception as e:
            logger.warning("Industry fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_clone_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-clones.read_clones.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            clone_data = await esi_char.get_clones(client, char.character_id)
            now = datetime.now(timezone.utc)
            jump_cooldown_str = None
            last_jump_raw = clone_data.get("last_clone_jump_date")
            if last_jump_raw:
                last_jump = datetime.fromisoformat(last_jump_raw.replace("Z", "+00:00"))
                next_available = last_jump + timedelta(hours=24)
                if next_available > now:
                    jump_cooldown_str = _format_duration((next_available - now).total_seconds())
            jump_clones = clone_data.get("jump_clones", [])
            return char.character_id, {"jump_cooldown_str": jump_cooldown_str, "jump_clones_count": len(jump_clones)}, None
        except Exception as e:
            logger.warning("Clone fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_orders_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-markets.read_character_orders.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            orders = await esi_market.get_character_orders(client, char.character_id)
            sell = sum(1 for o in orders if not o.get("is_buy_order"))
            buy = sum(1 for o in orders if o.get("is_buy_order"))
            return char.character_id, {"active_count": len(orders), "sell_count": sell, "buy_count": buy}, None
        except Exception as e:
            logger.warning("Orders fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_mail_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-mail.read_mail.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            headers = await esi_char.get_mail_headers(client, char.character_id)
            unread = sum(1 for m in headers if not m.get("is_read", True))
            return char.character_id, {"unread_count": unread}, None
        except Exception as e:
            logger.warning("Mail fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_notification_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-characters.read_notifications.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            notifs = await esi_char.get_notifications(client, char.character_id)
            unread = [n for n in notifs if not n.get("is_read", True)]
            types = list({n.get("type") for n in unread[:20] if n.get("type")})[:5]
            return char.character_id, {"unread_count": len(unread), "recent_types": types}, None
        except Exception as e:
            logger.warning("Notifications fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_contracts_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-contracts.read_character_contracts.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            contracts = await esi_char.get_contracts(client, char.character_id)
            outstanding = sum(1 for c in contracts if c.get("status") == "outstanding")
            in_progress = sum(1 for c in contracts if c.get("status") == "in_progress")
            return char.character_id, {"outstanding_count": outstanding, "in_progress_count": in_progress}, None
        except Exception as e:
            logger.warning("Contracts fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_pi_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get_planet_expiry(client, character_id, planet_id):
        try:
            details = await esi_char.get_planet_details(client, character_id, planet_id)
            expiry_times = [pin["expiry_time"] for pin in details.get("pins", []) if pin.get("expiry_time")]
            return planet_id, min(expiry_times) if expiry_times else None
        except Exception:
            return planet_id, None

    async def _get(char):
        if not _has_scope(char, "esi-planets.manage_planets.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            planets = await esi_char.get_planets(client, char.character_id)
            if not planets:
                return char.character_id, [], None
            expiry_map = dict(await asyncio.gather(*[
                _get_planet_expiry(client, char.character_id, p["planet_id"]) for p in planets
            ]))
            system_ids = {p.get("solar_system_id") for p in planets if p.get("solar_system_id")}
            sys_name_map = {}
            for sid in system_ids:
                info = await sde.system_info(db, sid)
                if info:
                    sys_name_map[sid] = info["system_name"]
            now = datetime.now(timezone.utc)
            result = []
            for planet in planets:
                pid = planet["planet_id"]
                expiry_raw = expiry_map.get(pid)
                expiry_time_str, expiry_warning = None, None
                if expiry_raw:
                    try:
                        expiry_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                        delta = (expiry_dt - now).total_seconds()
                        expiry_time_str = _format_duration(delta)
                        expiry_warning = "expired" if delta <= 0 else "critical" if delta < 3600 else "warning" if delta < 86400 else "ok"
                    except Exception:
                        pass
                result.append({
                    "planet_id": pid,
                    "planet_type": planet.get("planet_type", "unknown"),
                    "num_pins": planet.get("num_pins", 0),
                    "system_name": sys_name_map.get(planet.get("solar_system_id")),
                    "expiry_time_str": expiry_time_str,
                    "expiry_warning": expiry_warning,
                })
            return char.character_id, result, None
        except Exception as e:
            logger.warning("PI fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_server_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://esi.evetech.net/latest/status/", headers={"User-Agent": "Vigilant/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                return {"online": True, "players": data.get("players", 0)}
            return {"online": False, "players": None}
    except Exception:
        return {"online": False, "players": None}


@router.get("/api/server-status")
async def api_server_status():
    return JSONResponse(await fetch_server_status())


async def fetch_skillqueue_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-skills.read_skillqueue.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            queue = await esi_char.get_skill_queue(client, char.character_id)
            return char.character_id, queue, None
        except Exception as e:
            logger.warning("Skillqueue fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def fetch_zkillboard_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    f"https://zkillboard.com/api/characterID/{char.character_id}/page/1/",
                    headers={"User-Agent": "Vigilant/1.0", "Accept-Encoding": "gzip"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tagged = []
                    for km in data[:5]:
                        is_loss = km.get("victim", {}).get("character_id") == char.character_id
                        tagged.append({"is_loss": is_loss, "killmail_id": km.get("killmail_id")})
                    return char.character_id, tagged, None
        except Exception:
            pass
        return char.character_id, [], None

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


async def _resolve_assets_for_character(
    char: Character, raw_assets: list, client, db: AsyncSession
) -> list[dict]:
    """Walk item chains, resolve locations + type names into flat asset dicts."""
    if not raw_assets:
        return []

    # Build item_id → asset lookup for chain walking
    item_map = {a["item_id"]: a for a in raw_assets}

    # Walk each asset's location chain to find its root location
    # root_for[item_id] = (root_location_id, root_type)
    root_for: dict[int, tuple[int, str]] = {}
    for asset in raw_assets:
        current_id = asset["location_id"]
        current_type = asset["location_type"]
        for _ in range(20):
            if current_type != "item":
                break
            if current_id not in item_map:
                # Points outside our item list — classify by ID range
                if current_id > 1_000_000_000_000:
                    current_type = "structure"
                break
            parent = item_map[current_id]
            current_id = parent["location_id"]
            current_type = parent["location_type"]
        root_for[asset["item_id"]] = (current_id, current_type)

    # Partition unique root IDs by kind
    system_ids: set[int] = set()
    station_ids: set[int] = set()
    structure_ids: set[int] = set()
    for root_id, root_type in root_for.values():
        if root_type == "solar_system":
            system_ids.add(root_id)
        elif root_type == "station":
            station_ids.add(root_id)
        elif root_type == "structure":
            structure_ids.add(root_id)
        else:
            # Unknown — attempt system lookup as fallback
            system_ids.add(root_id)

    # Resolve NPC stations in bulk from SDE
    station_cache = await sde.stations_by_ids(db, list(station_ids))
    # ESI fallback for any station not in SDE
    for sid in station_ids:
        if sid not in station_cache:
            try:
                st = await esi_universe.get_station(client, sid)
                station_cache[sid] = {"system_id": st.get("system_id"), "station_name": st.get("name")}
            except Exception:
                station_cache[sid] = {"system_id": None, "station_name": "Unknown Station"}
    # Add station system_ids to system resolution set
    for st_info in station_cache.values():
        if st_info.get("system_id"):
            system_ids.add(st_info["system_id"])

    # Resolve player structures via ESI (concurrent)
    structure_cache: dict[int, dict] = {}
    if structure_ids:
        async def _fetch_structure(struct_id):
            try:
                data = await esi_universe.get_structure(client, struct_id)
                sys_id = data.get("solar_system_id")
                return struct_id, {"system_id": sys_id, "structure_name": data.get("name", "Unknown Structure")}
            except Exception:
                return struct_id, {"system_id": None, "structure_name": "Unknown Structure"}

        results = await asyncio.gather(*[_fetch_structure(sid) for sid in structure_ids])
        for struct_id, info in results:
            structure_cache[struct_id] = info
            if info.get("system_id"):
                system_ids.add(info["system_id"])

    # Resolve system info from SDE
    sys_info_cache: dict[int, dict | None] = {}
    for sys_id in system_ids:
        sys_info_cache[sys_id] = await sde.system_info(db, sys_id)

    # Build resolved location info per root ID
    root_resolved: dict[int, dict] = {}
    for root_id, root_type in set(root_for.values()):
        if root_type == "solar_system":
            si = sys_info_cache.get(root_id)
            root_resolved[root_id] = {
                "location_kind": "system",
                "system_id": root_id,
                "system_name": si.get("system_name") if si else None,
                "security": si.get("security") if si else None,
                "region": si.get("region") if si else None,
                "location_name": si.get("system_name") if si else str(root_id),
            }
        elif root_type == "station":
            st = station_cache.get(root_id, {})
            sys_id = st.get("system_id")
            si = sys_info_cache.get(sys_id) if sys_id else None
            root_resolved[root_id] = {
                "location_kind": "station",
                "system_id": sys_id,
                "system_name": si.get("system_name") if si else None,
                "security": si.get("security") if si else None,
                "region": si.get("region") if si else None,
                "location_name": st.get("station_name"),
            }
        elif root_type == "structure":
            struct = structure_cache.get(root_id, {})
            sys_id = struct.get("system_id")
            si = sys_info_cache.get(sys_id) if sys_id else None
            root_resolved[root_id] = {
                "location_kind": "structure",
                "system_id": sys_id,
                "system_name": si.get("system_name") if si else None,
                "security": si.get("security") if si else None,
                "region": si.get("region") if si else None,
                "location_name": struct.get("structure_name", "Unknown Structure"),
            }
        else:
            # Unknown — try system info fallback
            si = sys_info_cache.get(root_id)
            root_resolved[root_id] = {
                "location_kind": "system" if si else "unknown",
                "system_id": root_id if si else None,
                "system_name": si.get("system_name") if si else None,
                "security": si.get("security") if si else None,
                "region": si.get("region") if si else None,
                "location_name": si.get("system_name") if si else "Unknown",
            }

    # Batch resolve all type names in one DB call
    all_type_ids = list({a["type_id"] for a in raw_assets})
    type_names = await sde.type_ids_to_names(db, all_type_ids)

    # Assemble final resolved list
    resolved = []
    for asset in raw_assets:
        root_id, _ = root_for.get(asset["item_id"], (None, None))
        loc = root_resolved.get(root_id, {}) if root_id is not None else {}
        resolved.append({
            "type_id": asset["type_id"],
            "type_name": type_names.get(asset["type_id"]),
            "quantity": asset.get("quantity", 1),
            "location_flag": asset.get("location_flag", ""),
            "is_singleton": asset.get("is_singleton", False),
            "system_id": loc.get("system_id"),
            "system_name": loc.get("system_name"),
            "security": loc.get("security"),
            "region": loc.get("region"),
            "location_name": loc.get("location_name"),
            "location_kind": loc.get("location_kind", "unknown"),
        })
    return resolved


async def fetch_assets_data(characters: list[Character], db: AsyncSession) -> dict:
    async def _get(char):
        if not _has_scope(char, "esi-assets.read_assets.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            raw = await esi_assets.get_character_assets(client, char.character_id)
            resolved = await _resolve_assets_for_character(char, raw, client, db)
            return char.character_id, resolved, None
        except Exception as e:
            logger.warning("Assets fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"

    return {cid: (val, warn) for cid, val, warn in await asyncio.gather(*[_get(c) for c in characters])}


# Dispatch table — defined after all fetch functions
_FIELD_FETCHERS = {
    "wallet":        fetch_wallet_data,
    "location":      fetch_location_data,
    "industry":      fetch_industry_data,
    "clones":        fetch_clone_data,
    "orders":        fetch_orders_data,
    "mail":          fetch_mail_data,
    "notifications": fetch_notification_data,
    "contracts":     fetch_contracts_data,
    "pi":            fetch_pi_data,
    "skillqueue":    fetch_skillqueue_data,
    "zkill":         fetch_zkillboard_data,
    "assets":        fetch_assets_data,
}


# ── Background sync task ──────────────────────────────────────────────────────

async def _sync_task(character_id: int):
    """Sync one character. Acquires _sync_lock so only one character runs at a time."""
    async with _get_sync_lock():
        async with AsyncSessionLocal() as db:
            try:
                char_result = await db.execute(select(Character).where(Character.character_id == character_id))
                char = char_result.scalar_one_or_none()
                if not char:
                    return

                # Load or create cache rows
                cache_result = await db.execute(
                    select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
                )
                cache = cache_result.scalar_one_or_none()
                if cache is None:
                    cache = CharacterDashboardCache(character_id=character_id)
                    db.add(cache)

                asset_cache_result = await db.execute(
                    select(CharacterAssetCache).where(CharacterAssetCache.character_id == character_id)
                )
                asset_cache = asset_cache_result.scalar_one_or_none()
                if asset_cache is None:
                    asset_cache = CharacterAssetCache(character_id=character_id)
                    db.add(asset_cache)

                # Mark syncing now that the lock is held (idempotent if already set)
                cache.sync_status = "syncing"
                await db.commit()

                field_synced: dict = json.loads(cache.field_synced_json) if cache.field_synced_json else {}
                warnings: dict = json.loads(cache.sync_warnings_json) if cache.sync_warnings_json else {}
                now = datetime.now(timezone.utc)
                scopes = char.scopes or ""

                # Determine which fields need refreshing
                stale_fields = []
                for field, cache_secs in FIELD_CACHE_SECONDS.items():
                    scope = FIELD_SCOPES[field]
                    if scope and scope not in scopes:
                        continue
                    last_str = field_synced.get(field)
                    if last_str and (now - datetime.fromisoformat(last_str)).total_seconds() < cache_secs:
                        continue  # Still within cache window
                    stale_fields.append(field)

                logger.info("Syncing char %s — stale fields: %s", character_id, stale_fields or "none")

                if stale_fields:
                    results = await asyncio.gather(
                        *[_FIELD_FETCHERS[field]([char], db) for field in stale_fields]
                    )
                    for field, result in zip(stale_fields, results):
                        val, warn = result.get(character_id, (None, None))
                        col = _FIELD_DB_COLUMN[field]
                        if field == "assets":
                            if val is not None:
                                asset_cache.assets_json = json.dumps(val)
                                asset_cache.last_fetched = now
                                warnings.pop(field, None)
                            elif warn and warn != "missing_scope":
                                warnings[field] = warn
                            field_synced[field] = now.isoformat()
                            continue
                        if val is not None:
                            if col is None:  # wallet is a Float column
                                cache.wallet = val
                                if field == "wallet":
                                    db.add(WalletSnapshot(
                                        character_id=character_id,
                                        balance=val,
                                        recorded_at=now,
                                    ))
                            else:
                                setattr(cache, col, json.dumps(val))
                            warnings.pop(field, None)
                        elif warn and warn != "missing_scope":
                            warnings[field] = warn
                        field_synced[field] = now.isoformat()

                cache.field_synced_json = json.dumps(field_synced)
                cache.sync_warnings_json = json.dumps(warnings) if warnings else None
                cache.last_synced = now
                cache.sync_status = "idle"
                cache.sync_error = None
                await db.commit()
                logger.info("Sync complete for char %s", character_id)

            except Exception as e:
                try:
                    cache_result = await db.execute(
                        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
                    )
                    cache = cache_result.scalar_one_or_none()
                    if cache:
                        cache.sync_status = "error"
                        cache.sync_error = str(e)[:500]
                        await db.commit()
                except Exception:
                    pass


async def _cleanup_old_snapshots():
    """Delete WalletSnapshot rows older than 1 year to prevent unbounded DB growth."""
    from sqlalchemy import delete as sa_delete
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    cutoff_naive = cutoff.replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_delete(WalletSnapshot).where(WalletSnapshot.recorded_at < cutoff_naive)
        )
        await db.commit()
        if result.rowcount:
            logger.info("Cleaned up %d old WalletSnapshot rows", result.rowcount)


async def _sync_all_task(character_ids: list[int]):
    """Process a batch of characters sequentially.

    _sync_task holds _sync_lock for the duration of each character, so
    characters are strictly one-at-a-time even if concurrent callers exist.
    """
    try:
        for character_id in character_ids:
            await _sync_task(character_id)
            _queued_sync.pop(character_id, None)
    finally:
        for cid in character_ids:
            _queued_sync.pop(cid, None)

def _clean_stuck_characters():
    """Remove characters that have been queued for sync for >5 minutes (stuck detection).

    This safety mechanism prevents characters from getting permanently stuck in the
    _queued_sync queue if a sync task crashes or is cancelled without proper cleanup.
    """
    now = datetime.now(timezone.utc)
    stuck_threshold = timedelta(minutes=5)
    stuck_cids = []

    for cid, queued_at in list(_queued_sync.items()):
        # Make queued_at timezone-aware if needed
        qa = queued_at if queued_at.tzinfo else queued_at.replace(tzinfo=timezone.utc)
        if (now - qa) > stuck_threshold:
            stuck_cids.append(cid)
            _queued_sync.pop(cid, None)

    if stuck_cids:
        logger.warning("Detected %d character(s) stuck in sync queue (>5m): %s — force cleaned",
                      len(stuck_cids), stuck_cids)

    return stuck_cids


async def _background_scheduler():
    """Runs forever. Every 60s, find all characters with stale fields and sync them.

    This is the *only* place automatic syncs are triggered — the dashboard
    route no longer spawns syncs on page load.
    """
    try:
        await asyncio.sleep(15)  # Allow app startup (SDE load, etc.) to settle first
        _last_cleanup: datetime | None = None
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    char_result = await db.execute(select(Character))
                    characters = char_result.scalars().all()
                    if characters:
                        cids = [c.character_id for c in characters]
                        cache_result = await db.execute(
                            select(CharacterDashboardCache).where(
                                CharacterDashboardCache.character_id.in_(cids)
                            )
                        )
                        char_caches = {c.character_id: c for c in cache_result.scalars().all()}

                        # Check for and clean up characters stuck in queue (>5 minutes)
                        stuck_cids = _clean_stuck_characters()

                        stale_ids = _collect_stale(list(characters), char_caches)

                        # Re-queue any stuck characters if they have stale fields
                        for stuck_cid in stuck_cids:
                            stuck_char = next((c for c in characters if c.character_id == stuck_cid), None)
                            if stuck_char and _any_field_stale(stuck_char, char_caches.get(stuck_cid)):
                                stale_ids.append(stuck_cid)
                                logger.info("Re-queuing stuck character %s for sync", stuck_cid)

                        if stale_ids:
                            now = datetime.now(timezone.utc)
                            for cid in stale_ids:
                                _queued_sync[cid] = now
                            asyncio.create_task(_sync_all_task(stale_ids))
                            logger.info("Scheduler queued %d character(s) for sync: %s", len(stale_ids), stale_ids)
            except Exception as e:
                logger.warning("Background scheduler error: %s", e)

            # Daily WalletSnapshot cleanup
            now = datetime.now(timezone.utc)
            if _last_cleanup is None or (now - _last_cleanup).total_seconds() >= 86400:
                try:
                    await _cleanup_old_snapshots()
                    _last_cleanup = now
                except Exception as e:
                    logger.warning("Snapshot cleanup error: %s", e)

            await asyncio.sleep(60)
    except Exception as e:
        logger.error("Background scheduler crashed during initialization: %s", e, exc_info=True)
        raise


def _collect_stale(characters: list[Character], char_caches: dict) -> list[int]:
    """Return IDs of characters that have at least one stale field and are not
    already queued or actively syncing."""
    stale_ids = []
    for char in characters:
        if char.character_id in _queued_sync:
            continue
        cache = char_caches.get(char.character_id)
        if cache and cache.sync_status == "syncing":
            continue
        if _any_field_stale(char, cache):
            stale_ids.append(char.character_id)
    return stale_ids


# ── Build data dicts from cache ───────────────────────────────────────────────

def _build_data_from_caches(characters: list[Character], char_caches: dict) -> dict:
    """Unpack JSON columns from cache rows into the same dict structure the template expects."""
    wallets, locations, industry, clones, orders = {}, {}, {}, {}, {}
    mail, notifications, contracts, pi = {}, {}, {}, {}
    skillqueue_raw, zkill = {}, {}
    sync_warnings = {}

    for char in characters:
        cid = char.character_id
        cache = char_caches.get(cid)
        scopes = char.scopes or ""

        def _load(field):
            val = getattr(cache, field, None) if cache else None
            return json.loads(val) if val else None

        wallets[cid] = cache.wallet if cache else None
        locations[cid] = _load("location_json")
        industry[cid] = _load("industry_json")
        clones[cid] = _load("clones_json")
        orders[cid] = _load("orders_json")

        mail[cid] = "no_scope" if "esi-mail.read_mail.v1" not in scopes else _load("mail_json")
        notifications[cid] = "no_scope" if "esi-characters.read_notifications.v1" not in scopes else _load("notifications_json")
        contracts[cid] = "no_scope" if "esi-contracts.read_character_contracts.v1" not in scopes else _load("contracts_json")
        pi[cid] = "no_scope" if "esi-planets.manage_planets.v1" not in scopes else _load("pi_json")

        skillqueue_raw[cid] = (
            "no_scope" if "esi-skills.read_skillqueue.v1" not in scopes
            else _load("skillqueue_json")
        )
        zkill[cid] = _load("zkill_json") or []

        raw_warn = getattr(cache, "sync_warnings_json", None) if cache else None
        sync_warnings[cid] = json.loads(raw_warn) if raw_warn else {}

    return dict(wallets=wallets, locations=locations, industry=industry, clones=clones,
                orders=orders, mail=mail, notifications=notifications, contracts=contracts, pi=pi,
                skillqueue_raw=skillqueue_raw, zkill=zkill,
                sync_warnings=sync_warnings)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    active_id = request.session.get("active_character_id")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)
    if not active_char and characters:
        # Fallback: use the main character if active_id is stale.
        active_char = next((c for c in characters if c.is_main), characters[0])
        request.session["active_character_id"] = active_char.character_id
        active_id = active_char.character_id

    character_ids = [c.character_id for c in characters]

    # Load all cached data (fast DB reads)
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(character_ids))
    )
    char_caches = {c.character_id: c for c in cache_result.scalars().all()}

    # Build data dicts from cache
    data = _build_data_from_caches(list(characters), char_caches)
    wallets = data["wallets"]
    locations = data["locations"]
    industry = data["industry"]
    clones = data["clones"]
    orders = data["orders"]
    mail = data["mail"]
    notifications = data["notifications"]
    contracts = data["contracts"]
    pi = data["pi"]
    sync_warnings = data["sync_warnings"]

    # Live fetches (cache stats and skillqueue processing; server status loaded via AJAX)
    stats, skill_data = await asyncio.gather(
        cache_stats(db),
        _process_skillqueue(list(characters), data["skillqueue_raw"], db),
    )
    skill_groups = group_skill_data(skill_data)
    server_status = {"online": None, "players": None}  # loaded client-side
    zkill = data["zkill"]

    # Aggregates
    total_wallet = sum(v for v in wallets.values() if v is not None)
    total_unread_mail = sum(v.get("unread_count", 0) for v in mail.values() if isinstance(v, dict))
    total_unread_notifs = sum(v.get("unread_count", 0) for v in notifications.values() if isinstance(v, dict))
    total_active_jobs = sum(v.get("active_count", 0) for v in industry.values() if isinstance(v, dict))
    total_active_orders = sum(v.get("active_count", 0) for v in orders.values() if isinstance(v, dict))

    needs_mail_scope = any(v == "no_scope" for v in mail.values())
    needs_notif_scope = any(v == "no_scope" for v in notifications.values())
    needs_contracts_scope = any(v == "no_scope" for v in contracts.values())
    needs_pi_scope = any(v == "no_scope" for v in pi.values())

    # Per-character sync metadata
    now = datetime.now(timezone.utc)
    sync_statuses, staleness_map, last_synced_strs = {}, {}, {}
    for char in characters:
        cid = char.character_id
        cache = char_caches.get(cid)
        db_status = cache.sync_status if cache else "idle"
        # Treat queued-but-not-yet-started characters as syncing so the HTMX
        # poller stays active for the full batch.
        sync_statuses[cid] = "syncing" if cid in _queued_sync else db_status
        staleness_map[cid] = _staleness(cache.last_synced if cache else None)
        last_synced_strs[cid] = _age_str(cache.last_synced if cache else None)

    any_syncing = any(s == "syncing" for s in sync_statuses.values())

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "characters": characters,
        "active_char": active_char,
        "cache_stats": stats,
        "skill_groups": skill_groups,
        "wallets": wallets,
        "locations": locations,
        "industry": industry,
        "clones": clones,
        "orders": orders,
        "mail": mail,
        "notifications": notifications,
        "contracts": contracts,
        "pi": pi,
        "server_status": server_status,
        "zkill": zkill,
        "total_wallet": total_wallet,
        "total_unread_mail": total_unread_mail,
        "total_unread_notifs": total_unread_notifs,
        "total_active_jobs": total_active_jobs,
        "total_active_orders": total_active_orders,
        "needs_mail_scope": needs_mail_scope,
        "needs_notif_scope": needs_notif_scope,
        "needs_contracts_scope": needs_contracts_scope,
        "needs_pi_scope": needs_pi_scope,
        "sync_statuses": sync_statuses,
        "staleness": staleness_map,
        "last_synced_strs": last_synced_strs,
        "any_syncing": any_syncing,
        "sync_warnings": sync_warnings,
    })


@router.post("/dashboard/sync/{character_id}")
async def trigger_sync(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Manual resync for a single character."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard", status_code=303)
    ownership = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    if not ownership.scalar_one_or_none():
        return RedirectResponse("/dashboard", status_code=303)

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()

    if cache and cache.sync_status == "syncing":
        return RedirectResponse("/dashboard", status_code=303)  # Already running

    if cache is None:
        cache = CharacterDashboardCache(character_id=character_id, sync_status="syncing")
        db.add(cache)
    else:
        cache.sync_status = "syncing"
        cache.field_synced_json = None  # Force re-fetch of all fields
    await db.commit()

    asyncio.create_task(_sync_task(character_id))
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/sync-status", response_class=HTMLResponse)
async def sync_status_poll(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX polling endpoint. Returns spinner while syncing; triggers page refresh when done."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse('<div id="sync-poller"></div>')

    char_result = await db.execute(select(Character).where(Character.user_id == user_id))
    character_ids = [c.character_id for c in char_result.scalars().all()]

    result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(character_ids))
    )
    caches = list(result.scalars().all())
    # Still syncing if any character is actively syncing in DB OR queued in memory
    still_syncing = (
        any(c.sync_status == "syncing" for c in caches)
        or bool(_queued_sync.keys() & set(character_ids))
    )

    if not still_syncing:
        resp = HTMLResponse('<div id="sync-poller"></div>')
        resp.headers["HX-Refresh"] = "true"
        return resp

    count = len(
        {c.character_id for c in caches if c.sync_status == "syncing"}
        | (_queued_sync.keys() & set(character_ids))
    )
    plural = "s" if count != 1 else ""
    return HTMLResponse(f"""
<div id="sync-poller"
     hx-get="/dashboard/sync-status"
     hx-trigger="every 3s"
     hx-swap="outerHTML"
     class="flex items-center gap-2 text-xs text-eve-muted px-1">
    <svg class="w-3 h-3 animate-spin text-eve-accent flex-shrink-0" viewBox="0 0 24 24" fill="none">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z"/>
    </svg>
    Syncing {count} character{plural}...
</div>
""")
