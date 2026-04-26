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

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, CharacterAssetCache, CharacterCorpRoles, AsyncSessionLocal
from app.db.cache import cache_stats
from app.routes.characters import _process_skillqueue, group_skill_data
from app.utils.perf import perf_log, perf_enabled, ms_since
from time import perf_counter as _perf_now
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import market as esi_market
from app.esi import industry as esi_industry
from app.esi import universe as esi_universe
from app.esi import assets as esi_assets
from app.esi import corporation as esi_corp
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

# App start time for uptime calculation
_app_start_time: datetime = datetime.now(timezone.utc)

# Character IDs that are either actively syncing or queued to sync.
# Maps character_id -> timestamp when added to queue.
# Prevents duplicate _sync_all_task spawns across rapid page loads.
# Timestamps allow detection of stuck characters (in queue > 5 minutes).
_queued_sync: dict[int, datetime] = {}

# ── Browser notification events ───────────────────────────────────────────────
# Per-user event queue. Cleared when polled by the client.
_notification_events: dict[int, list] = {}
_NOTIFICATION_MAX = 50


def _emit_notification(user_id: int, event: dict):
    """Add a notification event for a user."""
    if user_id not in _notification_events:
        _notification_events[user_id] = []
    q = _notification_events[user_id]
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    q.append(event)
    if len(q) > _NOTIFICATION_MAX:
        _notification_events[user_id] = q[-_NOTIFICATION_MAX:]


# Track which skills we've already notified about: {character_id: set(skill_id_level)}
_notified_skills: dict[int, set] = {}

# Track which ESI notification IDs we've already emitted browser alerts for
_notified_esi_ids: dict[int, set] = {}
_NOTIFIED_ESI_MAX = 200  # cap per-char to prevent unbounded growth

# Dedup corp-wide alerts across characters: {user_id: set((type, timestamp))}
_emitted_alert_keys: dict[int, set] = {}
_EMITTED_ALERT_MAX = 200

# ESI notification types that trigger browser alerts
_STRUCTURE_ALERT_TYPES = {
    "StructureUnderAttack", "StructureLostShields", "StructureLostArmor",
    "StructureDestroyed", "StructureFuelAlert", "StructureServicesOffline",
    "StructureAnchoring", "StructureUnanchoring", "StructureOnline",
    "TowerAlertMsg", "TowerResourceAlertMsg",
    "OrbitalAttacked", "OrbitalReinforced",
    "MoonminingExtractionStarted", "MoonminingExtractionFinished",
    "MoonminingAutomaticFracture", "MoonminingLaserFired",
    "SkyhookDestructionImminent",
    "SovStructureReinforced", "SovCommandNodeEventStarted",
}

_STRUCTURE_ALERT_LABELS = {
    "StructureUnderAttack": "Structure Under Attack",
    "StructureLostShields": "Structure Lost Shields",
    "StructureLostArmor": "Structure Lost Armor",
    "StructureDestroyed": "Structure Destroyed",
    "StructureFuelAlert": "Fuel Alert",
    "StructureServicesOffline": "Services Offline",
    "StructureAnchoring": "Structure Anchoring",
    "StructureUnanchoring": "Structure Unanchoring",
    "StructureOnline": "Structure Online",
    "TowerAlertMsg": "POS Alert",
    "TowerResourceAlertMsg": "POS Fuel Alert",
    "OrbitalAttacked": "POCO Attacked",
    "OrbitalReinforced": "POCO Reinforced",
    "MoonminingExtractionStarted": "Moon Extraction Started",
    "MoonminingExtractionFinished": "Moon Chunk Ready",
    "MoonminingAutomaticFracture": "Moon Auto-Fracture",
    "MoonminingLaserFired": "Moon Laser Fired",
    "SkyhookDestructionImminent": "Skyhook Threatened",
    "SovStructureReinforced": "Sov Reinforced",
    "SovCommandNodeEventStarted": "Sov Node Event",
}


async def _detect_notifications(char: 'Character', old_cache: dict, new_cache: dict, db):
    """Compare old vs new cached data and emit notification events.
    Skills are resolved to names via SDE. Only fires once per skill completion."""
    user_id = char.user_id
    char_name = char.character_name
    cid = char.character_id
    portrait = f"https://images.evetech.net/characters/{cid}/portrait?size=64"

    # 1. Skill training complete
    # Compare the currently-training skill between old and new queue.
    # If the old active skill is different from the new one, the old one finished.
    old_sq = old_cache.get("skillqueue")
    new_sq = new_cache.get("skillqueue")
    if old_sq and new_sq and isinstance(old_sq, list) and isinstance(new_sq, list):
        now = datetime.now(timezone.utc)
        if cid not in _notified_skills:
            _notified_skills[cid] = set()

        def _find_active(queue):
            """Find the currently training skill (start <= now < finish)."""
            for entry in queue:
                start_raw = entry.get("start_date")
                finish_raw = entry.get("finish_date")
                if not start_raw or not finish_raw:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    finish_dt = datetime.fromisoformat(finish_raw.replace("Z", "+00:00"))
                    if start_dt <= now and finish_dt > now:
                        return entry
                except Exception:
                    continue
            return None

        old_active = _find_active(old_sq)
        new_active = _find_active(new_sq)

        # The old active skill finished if: there WAS an active skill before,
        # and now either there's a different one or none at all
        if old_active:
            old_sid = old_active.get("skill_id")
            old_level = old_active.get("finished_level")
            old_key = f"{old_sid}_{old_level}"
            new_sid = new_active.get("skill_id") if new_active else None

            if old_sid != new_sid and old_key not in _notified_skills[cid]:
                # Old skill is no longer active — it completed
                _notified_skills[cid].add(old_key)
                skill_name = f"Skill {old_sid}"
                try:
                    names = await sde.type_ids_to_names(db, [old_sid])
                    skill_name = names.get(old_sid, skill_name)
                except Exception:
                    pass
                _emit_notification(user_id, {
                    "type": "skill_complete",
                    "title": "Skill Complete",
                    "body": f"{char_name} — {skill_name} {old_level} finished",
                    "icon": portrait,
                })

        # Clean up notified set when skills leave the queue entirely
        current_keys = {f"{e.get('skill_id')}_{e.get('finished_level')}" for e in new_sq if e.get("skill_id")}
        _notified_skills[cid] = _notified_skills[cid] & current_keys

    # 2. Industry job ready
    old_ind = old_cache.get("industry")
    new_ind = new_cache.get("industry")
    if isinstance(old_ind, dict) and isinstance(new_ind, dict):
        old_ready = old_ind.get("ready_count", 0)
        new_ready = new_ind.get("ready_count", 0)
        if new_ready > old_ready:
            diff = new_ready - old_ready
            product = new_ind.get("soonest_product", "")
            body = f"{char_name} — {diff} job{'s' if diff > 1 else ''} ready for delivery"
            if diff == 1 and product:
                body = f"{char_name} — {product} ready for delivery"
            _emit_notification(user_id, {
                "type": "job_ready",
                "title": "Industry Job Ready",
                "body": body,
                "icon": portrait,
            })

    # 3. PI extractors expiring
    old_pi = old_cache.get("pi")
    new_pi = new_cache.get("pi")
    if isinstance(old_pi, list) and isinstance(new_pi, list):
        old_expired = {p.get("planet_id") for p in old_pi if p.get("expiry_warning") in ("critical", "expired")}
        for planet in new_pi:
            pid = planet.get("planet_id")
            warning = planet.get("expiry_warning")
            if warning in ("critical", "expired") and pid not in old_expired:
                pname = planet.get("planet_name", f"Planet {pid}")
                _emit_notification(user_id, {
                    "type": "pi_expiring",
                    "title": "PI Expiring" if warning == "critical" else "PI Expired",
                    "body": f"{char_name} — extractors {'expiring soon' if warning == 'critical' else 'expired'} on {pname}",
                    "icon": portrait,
                })

    # 4. New mail
    old_mail = old_cache.get("mail")
    new_mail = new_cache.get("mail")
    if isinstance(old_mail, dict) and isinstance(new_mail, dict):
        old_unread = old_mail.get("unread_count", 0)
        new_unread = new_mail.get("unread_count", 0)
        if new_unread > old_unread:
            diff = new_unread - old_unread
            _emit_notification(user_id, {
                "type": "new_mail",
                "title": "New Mail",
                "body": f"{char_name} — {diff} new message{'s' if diff > 1 else ''}",
                "icon": portrait,
            })

    # 5. Structure / POS / moon / sov alerts from ESI notifications
    new_notifs = new_cache.get("notifications")
    if isinstance(new_notifs, dict):
        notif_list = new_notifs.get("notifications", [])
        if cid not in _notified_esi_ids:
            # First sync: seed most IDs as "seen", but leave recent structure
            # alerts (< 48h) unseeded so they fire on the NEXT sync cycle.
            now_ts = datetime.now(timezone.utc)
            seed_ids = set()
            for n in notif_list:
                nid = n.get("notification_id")
                if not nid:
                    continue
                ntype = n.get("type", "")
                if ntype in _STRUCTURE_ALERT_TYPES:
                    try:
                        ts = datetime.fromisoformat(n.get("timestamp", "").replace("Z", "+00:00"))
                        if (now_ts - ts).total_seconds() < 48 * 3600:
                            continue  # Don't seed — let it fire as new
                    except Exception:
                        pass
                seed_ids.add(nid)
            _notified_esi_ids[cid] = seed_ids
        else:
            seen = _notified_esi_ids[cid]
            if user_id not in _emitted_alert_keys:
                _emitted_alert_keys[user_id] = set()
            emitted = _emitted_alert_keys[user_id]
            # Collect new alerts, dedup by type (not per-event) to avoid spam
            new_alerts: dict[str, int] = {}  # type -> count of new events
            for n in notif_list:
                nid = n.get("notification_id")
                ntype = n.get("type", "")
                if nid and nid not in seen and ntype in _STRUCTURE_ALERT_TYPES:
                    new_alerts[ntype] = new_alerts.get(ntype, 0) + 1
                if nid:
                    seen.add(nid)
            # Map ESI notification types to subcategories for filtering
            _ALERT_SUBCATEGORY = {
                "StructureUnderAttack": "structure_attack",
                "StructureLostShields": "structure_attack",
                "StructureLostArmor": "structure_attack",
                "StructureDestroyed": "structure_attack",
                "StructureFuelAlert": "structure_fuel",
                "StructureServicesOffline": "structure_fuel",
                "StructureAnchoring": "structure_change",
                "StructureUnanchoring": "structure_change",
                "StructureOnline": "structure_change",
                "TowerAlertMsg": "structure_attack",
                "TowerResourceAlertMsg": "structure_fuel",
                "OrbitalAttacked": "poco",
                "OrbitalReinforced": "poco",
                "MoonminingExtractionStarted": "moonmining",
                "MoonminingExtractionFinished": "moonmining",
                "MoonminingAutomaticFracture": "moonmining",
                "MoonminingLaserFired": "moonmining",
                "SkyhookDestructionImminent": "structure_attack",
                "SovStructureReinforced": "sovereignty",
                "SovCommandNodeEventStarted": "sovereignty",
            }
            for ntype, count in new_alerts.items():
                if ntype not in emitted:
                    emitted.add(ntype)
                    label = _STRUCTURE_ALERT_LABELS.get(ntype, ntype)
                    subcat = _ALERT_SUBCATEGORY.get(ntype, "structure_alert")
                    body = f"{label} ({count})" if count > 1 else label
                    _emit_notification(user_id, {
                        "type": subcat,
                        "title": label,
                        "body": body,
                        "icon": portrait,
                    })
            # Cap emitted keys
            if len(emitted) > _EMITTED_ALERT_MAX:
                _emitted_alert_keys[user_id] = set()
            # Cap the set size
            if len(seen) > _NOTIFIED_ESI_MAX:
                # Keep only IDs still present in the current notification list
                current_ids = {n.get("notification_id") for n in notif_list if n.get("notification_id")}
                _notified_esi_ids[cid] = current_ids

# Semaphore for concurrent character syncing — allows up to N characters
# to sync in parallel instead of strict serialisation.
_sync_semaphore: asyncio.Semaphore | None = None
_SYNC_CONCURRENCY = 5


def get_scheduler_state() -> dict:
    """Return scheduler state for admin dashboard."""
    sem = _sync_semaphore
    return {
        "queue_depth": len(_queued_sync),
        "queued_characters": dict(_queued_sync),
        "sync_concurrency": _SYNC_CONCURRENCY,
        "semaphore_available": sem._value if sem else _SYNC_CONCURRENCY,
        "last_inv_check": getattr(_background_scheduler, '_last_inv_check', None),
        "app_start_time": _app_start_time,
        "notification_queues": sum(len(v) for v in _notification_events.values()),
    }


def _get_sync_semaphore() -> asyncio.Semaphore:
    global _sync_semaphore
    if _sync_semaphore is None:
        _sync_semaphore = asyncio.Semaphore(_SYNC_CONCURRENCY)
    return _sync_semaphore

# Per-character locks for token refresh. Serialises concurrent _client_for()
# calls for the same character so asyncio.gather() can't trigger two
# simultaneous SSO refresh requests (EVE SSO rotates refresh tokens, so a
# second concurrent request with the old token would fail).
_token_locks: dict[int, asyncio.Lock] = {}

# Per-character sync locks — prevents the same character from syncing twice
# concurrently (e.g. manual resync while background scheduler is running).
_char_sync_locks: dict[int, asyncio.Lock] = {}


def _get_char_sync_lock(character_id: int) -> asyncio.Lock:
    if character_id not in _char_sync_locks:
        _char_sync_locks[character_id] = asyncio.Lock()
    return _char_sync_locks[character_id]


def _get_token_lock(character_id: int) -> asyncio.Lock:
    if character_id not in _token_locks:
        _token_locks[character_id] = asyncio.Lock()
    return _token_locks[character_id]

# Track (character_id, corp_id) pairs that returned 403 on corp endpoints.
# Avoids burning ESI requests on characters lacking in-game Director roles.
# Resets on container restart; cleared for a character on re-auth.
_corp_403_cache: set[tuple[int, int]] = set()

# ── ESI cache timers (seconds) — from ESI swagger Cache-Control: max-age ─────
# https://esi.evetech.net/latest/swagger.json
FIELD_CACHE_SECONDS: dict[str, int] = {
    "wallet":        120,   # ESI max-age: 120s
    "location":       60,   # ESI max-age:   5s  — 60s is adequate for a dashboard
    "industry":      300,   # ESI max-age: 300s
    "clones":       3600,   # ESI max-age: 3600s
    "orders":        300,   # ESI max-age: 300s
    "mail":          120,   # ESI max-age:  30s  — 120s reduces churn without missing much
    "notifications": 600,   # ESI max-age: 600s
    "contracts":     300,   # ESI max-age: 300s
    "pi":            600,   # ESI max-age: 600s
    "skillqueue":    120,   # ESI max-age: 120s
    "zkill":        3600,   # zkillboard — 1h is plenty
    "assets":       3600,   # ESI max-age: 3600s
    "roles":        3600,   # corp roles — rarely change, cached for permission checks
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
    "roles":         "esi-characters.read_corporation_roles.v1",
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
    "roles":         None,   # roles stored in CharacterCorpRoles (separate table)
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


def _format_isk_py(amount: float) -> str:
    if amount >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f}T ISK"
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B ISK"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M ISK"
    return f"{amount:,.0f} ISK"


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
    from app.esi.client import get_client_safe, TokenRevoked
    async with _get_token_lock(char.character_id):
        try:
            client = await get_client_safe(char)
            client.cache_enabled = True
            return client, None
        except TokenRevoked as e:
            logger.warning("Token revoked for char %s — user must re-authenticate: %s", char.character_id, e)
            return None, "token_revoked"
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
    async def _safe_ship(client, char_id):
        """Fetch ship data, returning {} on 404 instead of failing the whole location fetch."""
        try:
            return await esi_char.get_ship(client, char_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {}
            raise

    async def _get(char):
        if not _has_scope(char, "esi-location.read_location.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            loc, ship_data, online_data = await asyncio.gather(
                esi_char.get_location(client, char.character_id),
                _safe_ship(client, char.character_id) if _has_scope(char, "esi-location.read_ship_type.v1") else asyncio.sleep(0, result={}),
                esi_char.get_online(client, char.character_id) if _has_scope(char, "esi-location.read_online.v1") else asyncio.sleep(0, result={}),
            )
            system_id = loc.get("solar_system_id")
            result = {"system_id": system_id, "system_name": None, "security": None, "region": None, "docked_at": None,
                      "ship_type_id": None, "ship_type_name": None, "ship_name": None, "is_online": False}
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
                    struct = await esi_universe.get_structure(client, loc["structure_id"], db=db)
                    result["docked_at"] = struct.get("name", "Unknown Structure")
                except Exception:
                    result["docked_at"] = await esi_universe.get_cached_structure_name(db, loc["structure_id"]) or "Unknown Structure"
                if result["docked_at"] == "Unknown Structure" and result.get("system_name"):
                    result["docked_at"] = f"Unknown Structure ({result['system_name']})"
            if ship_data:
                ship_type_id = ship_data.get("ship_type_id")
                result["ship_type_id"] = ship_type_id
                result["ship_name"] = ship_data.get("ship_name")
                if ship_type_id:
                    result["ship_type_name"] = await sde.type_id_to_name(db, ship_type_id)
            result["is_online"] = online_data.get("online", False) if isinstance(online_data, dict) else False
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
            unread = sum(1 for m in headers if not m.get("is_read", False))
            return char.character_id, {
                "unread_count": unread,
                "headers": [
                    {
                        "mail_id": m.get("mail_id"),
                        "subject": m.get("subject", "(No Subject)"),
                        "from": m.get("from"),
                        "timestamp": m.get("timestamp"),
                        "is_read": m.get("is_read", False),
                        "labels": m.get("labels", []),
                    }
                    for m in headers[:20]
                ],
            }, None
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
            return char.character_id, {
                "unread_count": len(unread),
                "recent_types": types,
                "notifications": [
                    {
                        "notification_id": n.get("notification_id"),
                        "type": n.get("type"),
                        "text": n.get("text", ""),
                        "timestamp": n.get("timestamp"),
                        "is_read": n.get("is_read", True),
                        "sender_id": n.get("sender_id"),
                        "sender_type": n.get("sender_type"),
                    }
                    for n in notifs[:30]
                ],
            }, None
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
    """Fetch PI summary + full pin detail for each character's planets.

    Returns list of planet dicts including `pins`, `extractor_products`, timing,
    and expiry state. The dashboard uses the top-level expiry fields; the
    `/industry/planetary` page uses the nested pin detail.
    """
    async def _get_planet_detail(client, character_id, planet_id):
        try:
            details = await esi_char.get_planet_details(client, character_id, planet_id)
            return planet_id, details
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
            detail_map = dict(await asyncio.gather(*[
                _get_planet_detail(client, char.character_id, p["planet_id"]) for p in planets
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
                details = detail_map.get(pid) or {}
                raw_pins = details.get("pins") or []

                expiry_times = [p["expiry_time"] for p in raw_pins if p.get("expiry_time")]
                expiry_raw = min(expiry_times) if expiry_times else None
                expiry_time_str, expiry_warning = None, None
                if expiry_raw:
                    try:
                        expiry_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                        delta = (expiry_dt - now).total_seconds()
                        expiry_time_str = _format_duration(delta)
                        expiry_warning = "expired" if delta <= 0 else "critical" if delta < 3600 else "warning" if delta < 86400 else "ok"
                    except Exception:
                        pass

                # Condense pin detail — keep only fields we render, skip geometry noise.
                pins = []
                for p in raw_pins:
                    ext = p.get("extractor_details") or {}
                    factory = p.get("factory_details") or {}
                    pins.append({
                        "pin_id": p.get("pin_id"),
                        "type_id": p.get("type_id"),
                        "schematic_id": p.get("schematic_id") or factory.get("schematic_id"),
                        "expiry_time": p.get("expiry_time"),
                        "install_time": p.get("install_time"),
                        "last_cycle_start": p.get("last_cycle_start"),
                        "extractor_product_type_id": ext.get("product_type_id"),
                        "extractor_cycle_time": ext.get("cycle_time"),
                        "extractor_qty_per_cycle": ext.get("qty_per_cycle"),
                        "extractor_head_count": len(ext.get("heads") or []) if ext else 0,
                        "contents": [
                            {"type_id": c.get("type_id"), "amount": c.get("amount")}
                            for c in (p.get("contents") or [])
                        ],
                    })

                result.append({
                    "planet_id": pid,
                    "planet_type": planet.get("planet_type", "unknown"),
                    "num_pins": planet.get("num_pins", 0),
                    "solar_system_id": planet.get("solar_system_id"),
                    "system_name": sys_name_map.get(planet.get("solar_system_id")),
                    "upgrade_level": planet.get("upgrade_level", 0),
                    "last_update": planet.get("last_update"),
                    "expiry_time": expiry_raw,
                    "expiry_time_str": expiry_time_str,
                    "expiry_warning": expiry_warning,
                    "pins": pins,
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


def _dashboard_pulse_enabled() -> bool:
    from app.config import get_settings as _gs
    cfg = _gs()
    return cfg.killmails_enabled and cfg.killmail_dashboard_enabled


def _dashboard_battles_enabled() -> bool:
    from app.config import get_settings as _gs
    cfg = _gs()
    return cfg.killmails_enabled and cfg.killmail_battles_enabled


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
                data = await esi_universe.get_structure(client, struct_id, db=db)
                sys_id = data.get("solar_system_id")
                name = data.get("name", "Unknown Structure")
                return struct_id, {"system_id": sys_id, "structure_name": name}
            except Exception:
                cached = await esi_universe.get_cached_structure(db, struct_id)
                if cached:
                    return struct_id, {"system_id": cached.get("solar_system_id"), "structure_name": cached["name"]}
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
            struct_name = struct.get("structure_name", "Unknown Structure")
            if struct_name == "Unknown Structure" and si:
                struct_name = f"Unknown Structure ({si.get('system_name', '')})"
            root_resolved[root_id] = {
                "location_kind": "structure",
                "system_id": sys_id,
                "system_name": si.get("system_name") if si else None,
                "security": si.get("security") if si else None,
                "region": si.get("region") if si else None,
                "location_name": struct_name,
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


async def fetch_corp_roles_data(characters: list[Character], db: AsyncSession) -> dict:
    """Fetch GET /characters/{id}/roles/ per character.
    Returns {character_id: (roles_payload_dict | None, warning | None)}.
    Roles payload shape: {"roles": [...], "roles_at_hq": [...], ...}.
    """
    async def _get(char):
        if not _has_scope(char, "esi-characters.read_corporation_roles.v1"):
            return char.character_id, None, "missing_scope"
        client, err = await _client_for(char, db)
        if not client:
            return char.character_id, None, err
        try:
            payload = await esi_char.get_roles(client, char.character_id)
            return char.character_id, payload, None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                # Character not in a corp that grants roles (or NPC corp) — treat as no roles
                return char.character_id, {"roles": [], "roles_at_hq": [],
                                           "roles_at_base": [], "roles_at_other": []}, None
            logger.warning("Roles fetch failed for char %s: %s", char.character_id, e)
            return char.character_id, None, f"esi_error: {type(e).__name__}"
        except Exception as e:
            logger.warning("Roles fetch failed for char %s: %s", char.character_id, e)
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
    "roles":         fetch_corp_roles_data,
}


# ── Background sync task ──────────────────────────────────────────────────────

_SYNC_TIMEOUT = 300  # 5 minutes — if the entire sync for a character takes
                     # longer than this, it's killed and the status is reset.


async def _sync_task(character_id: int):
    """Sync one character. Acquires semaphore slot + per-character lock.

    Safety mechanisms:
      - asyncio.wait_for wraps the core work with a hard timeout
      - try/finally guarantees sync_status resets to idle/error regardless
        of how the function exits (including CancelledError, TimeoutError)
      - per-character lock prevents concurrent syncs of the same character
    """
    if _get_char_sync_lock(character_id).locked():
        return  # Already syncing this character
    async with _get_sync_semaphore():
        async with _get_char_sync_lock(character_id):
            await _sync_task_inner(character_id)


async def _sync_task_inner(character_id: int):
    """Inner sync body — always resets DB sync_status on exit."""
    async with AsyncSessionLocal() as db:
        cache = None
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

            cache.sync_status = "syncing"
            await db.commit()

            # Run the actual field-fetch work under a hard timeout so hung
            # ESI calls don't block the character permanently.
            await asyncio.wait_for(
                _sync_fields(character_id, char, cache, asset_cache, db),
                timeout=_SYNC_TIMEOUT,
            )

            logger.info("Sync complete for char %s", character_id)

        except asyncio.TimeoutError:
            logger.warning("Sync for char %s timed out after %ds", character_id, _SYNC_TIMEOUT)
            try:
                if cache:
                    cache.sync_status = "error"
                    cache.sync_error = f"timeout after {_SYNC_TIMEOUT}s"
                    await db.commit()
            except Exception:
                pass
        except (asyncio.CancelledError, BaseException) as e:
            # CancelledError (Python 3.9+) inherits BaseException, not
            # Exception — the old `except Exception` missed it, leaving
            # sync_status permanently stuck at "syncing".
            logger.warning("Sync for char %s cancelled/crashed: %s", character_id, type(e).__name__)
            try:
                if cache:
                    cache.sync_status = "error"
                    cache.sync_error = f"{type(e).__name__}: {str(e)[:300]}"
                    await db.commit()
            except Exception:
                pass
            if isinstance(e, asyncio.CancelledError):
                raise  # re-raise so asyncio cancellation propagates
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
        finally:
            # Last-resort guard: if we exited by any path that didn't
            # explicitly set a terminal status, force it to idle so the
            # character never stays stuck at "syncing" in the DB.
            try:
                cache_check = await db.execute(
                    select(CharacterDashboardCache).where(
                        CharacterDashboardCache.character_id == character_id
                    )
                )
                c = cache_check.scalar_one_or_none()
                if c and c.sync_status == "syncing":
                    c.sync_status = "idle"
                    await db.commit()
            except Exception:
                pass


async def _sync_fields(character_id: int, char, cache, asset_cache, db):
    """Core field-fetch logic, extracted so _sync_task_inner can wrap it
    with asyncio.wait_for for a hard timeout."""
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
            continue
        stale_fields.append(field)

    logger.info("Syncing char %s — stale fields: %s", character_id, stale_fields or "none")

    # Snapshot old data for notification detection
    _old_cache = {}
    if stale_fields:
        for sf in stale_fields:
            col = _FIELD_DB_COLUMN.get(sf)
            json_attr = f"{sf}_json" if sf != "wallet" else None
            if json_attr and sf in ("skillqueue", "industry", "pi", "mail", "notifications"):
                raw = getattr(cache, json_attr, None)
                if raw:
                    try:
                        _old_cache[sf] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning("Corrupt %s cache for char %s: %s", sf, character_id, e)

    if stale_fields:
        results = await asyncio.gather(
            *[_FIELD_FETCHERS[field]([char], db) for field in stale_fields],
            return_exceptions=True,
        )
        for field, result in zip(stale_fields, results):
            if isinstance(result, Exception):
                logger.warning("Field %s raised for char %s: %s", field, character_id, result)
                warnings[field] = f"exception: {type(result).__name__}"
                field_synced[field] = now.isoformat()
                continue
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
            if field == "roles":
                if val is not None:
                    roles_list = val.get("roles", []) if isinstance(val, dict) else []
                    roles_res = await db.execute(
                        select(CharacterCorpRoles).where(
                            CharacterCorpRoles.character_id == character_id
                        )
                    )
                    roles_row = roles_res.scalar_one_or_none()
                    if roles_row is None:
                        db.add(CharacterCorpRoles(
                            character_id=character_id,
                            roles_json=json.dumps(roles_list),
                            fetched_at=now,
                        ))
                    else:
                        roles_row.roles_json = json.dumps(roles_list)
                        roles_row.fetched_at = now
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

    # Refresh corp/alliance info from public endpoint (no scope needed)
    if "location" in stale_fields:
        try:
            pub_client = ESIClient("")
            pub_info = await esi_char.get_public_info(pub_client, character_id)
            new_corp_id = pub_info.get("corporation_id")
            new_alliance_id = pub_info.get("alliance_id")
            if new_corp_id and new_corp_id != char.corporation_id:
                try:
                    corp_info = await esi_corp.get_corporation_info(pub_client, new_corp_id)
                    char.corporation_id = new_corp_id
                    char.corporation_name = corp_info.get("name")
                except Exception:
                    char.corporation_id = new_corp_id
                    char.corporation_name = None
                logger.info("Corp change for char %s: now %s (%s)", character_id, char.corporation_name, new_corp_id)
            if new_alliance_id != char.alliance_id:
                char.alliance_id = new_alliance_id
                if new_alliance_id:
                    try:
                        ally_info = await esi_corp.get_alliance_info(pub_client, new_alliance_id)
                        char.alliance_name = ally_info.get("name")
                    except Exception:
                        char.alliance_name = None
                else:
                    char.alliance_name = None
        except Exception as pub_err:
            logger.debug("Public info refresh failed for char %s: %s", character_id, pub_err)

    cache.field_synced_json = json.dumps(field_synced)
    cache.sync_warnings_json = json.dumps(warnings) if warnings else None
    cache.last_synced = now
    cache.sync_status = "idle"
    cache.sync_error = None
    await db.commit()

    # Detect notification events by comparing old vs new data
    if _old_cache and char.user_id:
        _new_cache = {}
        for field in ("skillqueue", "industry", "pi", "mail", "notifications"):
            raw = getattr(cache, f"{field}_json", None)
            if raw:
                try:
                    _new_cache[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Corrupt %s cache for char %s: %s", field, character_id, e)
        try:
            await _detect_notifications(char, _old_cache, _new_cache, db)
        except Exception as notif_err:
            logger.debug("Notification detection error for char %s: %s", character_id, notif_err)


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


async def _check_all_inventory_thresholds():
    """Check all users' inventory thresholds against live corp assets."""
    from app.db.models import CorpInventoryThreshold
    from app.routes.corporations import check_corp_inventory
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                CorpInventoryThreshold.user_id,
                CorpInventoryThreshold.corp_id,
            ).distinct()
        )
        pairs = result.fetchall()
        for user_id, corp_id in pairs:
            try:
                await check_corp_inventory(user_id, corp_id, db, emit_notifications=True)
            except Exception as e:
                logger.debug("Inventory check failed for user %s corp %s: %s", user_id, corp_id, e)


async def _check_all_contract_thresholds():
    """Check all users' contract thresholds against live corp contracts."""
    from app.db.models import CorpContractThreshold
    from app.routes.corporations import check_corp_contracts
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                CorpContractThreshold.user_id,
                CorpContractThreshold.corp_id,
            ).distinct()
        )
        pairs = result.fetchall()
        for user_id, corp_id in pairs:
            try:
                await check_corp_contracts(user_id, corp_id, db, emit_notifications=True)
            except Exception as e:
                logger.debug("Contract check failed for user %s corp %s: %s", user_id, corp_id, e)


async def _sync_all_task(character_ids: list[int]):
    """Process a batch of characters concurrently (up to _SYNC_CONCURRENCY at a time).

    The semaphore inside _sync_task limits how many run in parallel.
    """
    async def _sync_and_cleanup(cid: int):
        try:
            await _sync_task(cid)
        finally:
            _queued_sync.pop(cid, None)

    try:
        await asyncio.gather(*[_sync_and_cleanup(cid) for cid in character_ids])
    finally:
        for cid in character_ids:
            _queued_sync.pop(cid, None)

async def _clean_stuck_characters():
    """Remove characters stuck in sync for >5 minutes.

    Cleans BOTH the in-memory _queued_sync dict AND the DB sync_status column.
    The old version only cleaned the in-memory dict, so a character whose sync
    task crashed would have sync_status="syncing" in the DB forever —
    _collect_stale() skipped it, and only a full restart would unstick it.
    """
    now = datetime.now(timezone.utc)
    stuck_threshold = timedelta(minutes=5)
    stuck_cids = []

    # 1. Clean the in-memory queue (same as before)
    for cid, queued_at in list(_queued_sync.items()):
        qa = queued_at if queued_at.tzinfo else queued_at.replace(tzinfo=timezone.utc)
        if (now - qa) > stuck_threshold:
            stuck_cids.append(cid)
            _queued_sync.pop(cid, None)

    # 2. Also scan the DB for characters stuck at sync_status="syncing" whose
    #    last_synced is stale and who are NOT in the in-memory queue (i.e. their
    #    asyncio task has finished/crashed but the DB was never reset).
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import or_
            stale_cutoff = now - stuck_threshold
            stale_cutoff_naive = stale_cutoff.replace(tzinfo=None)
            stuck_rows = (await db.execute(
                select(CharacterDashboardCache).where(
                    CharacterDashboardCache.sync_status == "syncing",
                    or_(
                        CharacterDashboardCache.last_synced < stale_cutoff_naive,
                        CharacterDashboardCache.last_synced.is_(None),
                    ),
                )
            )).scalars().all()
            db_stuck = []
            for row in stuck_rows:
                if row.character_id not in _queued_sync:
                    row.sync_status = "idle"
                    row.sync_error = "auto-unstuck: was syncing for >5m with no active task"
                    db_stuck.append(row.character_id)
                    if row.character_id not in stuck_cids:
                        stuck_cids.append(row.character_id)
            if db_stuck:
                await db.commit()
                logger.warning("Reset %d DB-stuck character(s) from syncing→idle: %s", len(db_stuck), db_stuck)
    except Exception as e:
        logger.warning("DB stuck-character cleanup failed: %s", e)

    if stuck_cids:
        logger.warning("Detected %d character(s) stuck in sync (>5m): %s — force cleaned",
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
                        stuck_cids = await _clean_stuck_characters()

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

            # ESI cache GC (every hour) — drops rows whose expires_at has passed.
            now = datetime.now(timezone.utc)
            if not hasattr(_background_scheduler, '_last_cache_gc') or \
               (now - _background_scheduler._last_cache_gc).total_seconds() >= 3600:
                try:
                    from app.db.cache import cache_gc
                    removed = await cache_gc()
                    if removed:
                        logger.info("ESI cache GC removed %d expired rows", removed)
                    _background_scheduler._last_cache_gc = now
                except Exception as e:
                    logger.warning("Cache GC error: %s", e)

            # Inventory threshold check (every 5 minutes)
            if not hasattr(_background_scheduler, '_last_inv_check') or \
               (now - _background_scheduler._last_inv_check).total_seconds() >= 300:
                try:
                    await _check_all_inventory_thresholds()
                    _background_scheduler._last_inv_check = now
                except Exception as e:
                    logger.warning("Inventory check error: %s", e)

            # Contract threshold check (same 5-minute cycle)
            if not hasattr(_background_scheduler, '_last_contract_check') or \
               (now - _background_scheduler._last_contract_check).total_seconds() >= 300:
                try:
                    await _check_all_contract_thresholds()
                    _background_scheduler._last_contract_check = now
                except Exception as e:
                    logger.warning("Contract check error: %s", e)

            # Killmail backfill — one character per 2-minute tick (gated).
            # Inside backfill_character there is a 10-minute startup grace
            # window, so this is a no-op right after deploy.
            from app.config import get_settings as _get_settings
            _km_settings = _get_settings()
            if _km_settings.killmails_enabled:
                if not hasattr(_background_scheduler, '_last_killmail_tick') or \
                   (now - _background_scheduler._last_killmail_tick).total_seconds() >= 120:
                    try:
                        from app.intel.killmail_ingest import (
                            backfill_character, find_pending_backfill_chars,
                        )
                        pending = await find_pending_backfill_chars(limit=1)
                        if pending:
                            asyncio.create_task(backfill_character(pending[0]))
                        _background_scheduler._last_killmail_tick = now
                    except Exception as e:
                        logger.warning("Killmail backfill scheduling error: %s", e)

                # Daily rollup of universe-wide killmails into per-day
                # aggregate rows. Runs BEFORE the GC tick below so we capture
                # ISK + kill counts before discovery rows are deleted.
                if not hasattr(_background_scheduler, '_last_kill_rollup') or \
                   (now - _background_scheduler._last_kill_rollup).total_seconds() >= 86400:
                    try:
                        from app.intel.killmail_daily_rollup import rollup_recent_days
                        res = await rollup_recent_days(days=35)
                        logger.info("killmail daily rollup: %s", res)
                        _background_scheduler._last_kill_rollup = now
                    except Exception as e:
                        logger.warning("Killmail daily rollup error: %s", e)

                # Daily GC of discovery-scope killmails (>30d, not-our-char)
                if not hasattr(_background_scheduler, '_last_killmail_gc') or \
                   (now - _background_scheduler._last_killmail_gc).total_seconds() >= 86400:
                    try:
                        from app.intel.killmail_store import gc_discovery_killmails
                        removed = await gc_discovery_killmails(retention_days=30)
                        if removed:
                            logger.info("killmail GC: removed %d discovery rows", removed)
                        _background_scheduler._last_killmail_gc = now
                    except Exception as e:
                        logger.warning("Killmail GC error: %s", e)

                # Recent battle discovery — every 15 min, also gated by battles flag.
                # Hard-capped to 100 ESI hydrations per run (see recent_battles.py).
                if _km_settings.killmail_battles_enabled:
                    if not hasattr(_background_scheduler, '_last_battle_discovery') or \
                       (now - _background_scheduler._last_battle_discovery).total_seconds() >= 900:
                        try:
                            from app.intel.recent_battles import discover_and_persist_battles
                            asyncio.create_task(discover_and_persist_battles())
                            _background_scheduler._last_battle_discovery = now
                        except Exception as e:
                            logger.warning("Battle discovery scheduling error: %s", e)

            # ESI /status/ player-count sample (every 60s; runs regardless of
            # killmail flag — it's a tiny global health stat).
            if not hasattr(_background_scheduler, '_last_player_count_tick') or \
               (now - _background_scheduler._last_player_count_tick).total_seconds() >= 60:
                try:
                    from app.intel.player_count import sample_status_from_esi
                    asyncio.create_task(sample_status_from_esi())
                    _background_scheduler._last_player_count_tick = now
                except Exception as e:
                    logger.warning("Player count sample error: %s", e)

            # Daily rollup of player_count_snapshots → daily aggregate.
            # Trailing-7-day refresh keeps today + recent days fresh as new
            # samples land. Raw snapshots are preserved.
            if not hasattr(_background_scheduler, '_last_pcu_rollup') or \
               (now - _background_scheduler._last_pcu_rollup).total_seconds() >= 86400:
                try:
                    from app.intel.pcu_daily_rollup import rollup_pcu
                    res = await rollup_pcu(days=7)
                    logger.info("pcu daily rollup: %s", res)
                    _background_scheduler._last_pcu_rollup = now
                except Exception as e:
                    logger.warning("PCU daily rollup error: %s", e)

            # Daily GC of archived ESI rate-limit events (>30 days old).
            # Runs regardless of flags — small query, safe cheap.
            if not hasattr(_background_scheduler, '_last_esi_events_gc') or \
               (now - _background_scheduler._last_esi_events_gc).total_seconds() >= 86400:
                try:
                    from app.db.models import ESIRateLimitEvent
                    from sqlalchemy import delete as _delete
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
                    async with AsyncSessionLocal() as gc_db:
                        res = await gc_db.execute(
                            _delete(ESIRateLimitEvent).where(
                                ESIRateLimitEvent.archived_at.is_not(None),
                                ESIRateLimitEvent.archived_at < cutoff,
                            )
                        )
                        await gc_db.commit()
                        if res.rowcount:
                            logger.info("esi_events GC: removed %d archived events >30d", res.rowcount)
                    _background_scheduler._last_esi_events_gc = now
                except Exception as e:
                    logger.warning("ESI events GC error: %s", e)

            # Daily WalletSnapshot cleanup
            if _last_cleanup is None or (now - _last_cleanup).total_seconds() >= 86400:
                try:
                    await _cleanup_old_snapshots()
                    _last_cleanup = now
                except Exception as e:
                    logger.warning("Snapshot cleanup error: %s", e)
                try:
                    from app.routes.images import cleanup_expired_images
                    async with AsyncSessionLocal() as cleanup_db:
                        removed = await cleanup_expired_images(cleanup_db)
                        if removed:
                            logger.info("Cleaned up %d expired hosted image(s)", removed)
                except Exception as e:
                    logger.warning("Hosted image cleanup error: %s", e)

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
async def dashboard(request: Request, sort: str = "custom", db: AsyncSession = Depends(get_db)):
    _t0 = _perf_now() if perf_enabled() else 0.0
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Refresh admin flag in session
    from app.db.models import User
    user_obj = await db.execute(select(User).where(User.id == user_id))
    user_row = user_obj.scalar_one_or_none()
    if user_row:
        request.session["is_admin"] = user_row.role in ("admin", "manager")
        request.session["role"] = user_row.role

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
    _cache_stats_ms = 0.0
    _skillqueue_ms = 0.0

    async def _timed_cache_stats():
        nonlocal _cache_stats_ms
        t = _perf_now()
        r = await cache_stats(db)
        _cache_stats_ms = ms_since(t)
        return r

    async def _timed_skillqueue():
        nonlocal _skillqueue_ms
        t = _perf_now()
        r = await _process_skillqueue(list(characters), data["skillqueue_raw"], db)
        _skillqueue_ms = ms_since(t)
        return r

    _t_live = _perf_now() if perf_enabled() else 0.0
    stats, skill_data = await asyncio.gather(_timed_cache_stats(), _timed_skillqueue())
    _live_ms = ms_since(_t_live) if perf_enabled() else 0.0
    skill_groups = group_skill_data(skill_data)
    # Build skill_map for per-character lookup in template
    skill_map = {item["char"].character_id: item for item in skill_data}

    # Sort characters
    if sort == "name":
        characters = sorted(characters, key=lambda c: c.character_name.lower())
    elif sort == "corp":
        characters = sorted(characters, key=lambda c: (c.corporation_name or "").lower())
    elif sort == "training":
        characters = sorted(characters, key=lambda c: 0 if skill_map.get(c.character_id, {}).get("current_skill") else 1)
    elif sort == "queue":
        from datetime import datetime as _dt
        characters = sorted(characters, key=lambda c: skill_map.get(c.character_id, {}).get("queue_end") or _dt.max.replace(tzinfo=timezone.utc))
    else:  # custom (default)
        # Use saved group order from session if available
        saved_group_order = request.session.get("group_order", [])
        group_rank = {name: idx for idx, name in enumerate(saved_group_order)}
        characters = sorted(characters, key=lambda c: (
            group_rank.get(c.account_group or "Ungrouped", 999),
            c.account_group or "Ungrouped",
            c.sort_order,
        ))

    # Build groups for custom view (dict preserves insertion order)
    char_groups = {}
    for char in characters:
        group = char.account_group or "Ungrouped"
        char_groups.setdefault(group, []).append(char)

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

    # Detect characters needing re-auth
    # Flag if: token refresh failed OR character has fewer scopes than the most-scoped character
    needs_reauth: dict[int, bool] = {}
    max_scopes = max((len((c.scopes or "").split()) for c in characters), default=0)
    for char in characters:
        cid = char.character_id
        warns = sync_warnings.get(cid, {})
        token_failed = any("token_refresh_failed" in str(v) for v in warns.values())
        missing_scopes = len((char.scopes or "").split()) < max_scopes
        needs_reauth[cid] = token_failed or missing_scopes

    # Corporation aggregates
    corp_ids = set()
    for char in characters:
        if char.corporation_id:
            corp_ids.add(char.corporation_id)
    total_corporations = len(corp_ids)

    # Find best character per corp for ESI calls (one with most scopes)
    corp_chars: dict[int, Character] = {}
    for char in characters:
        if not char.corporation_id:
            continue
        existing = corp_chars.get(char.corporation_id)
        if not existing or len((char.scopes or "").split()) > len((existing.scopes or "").split()):
            corp_chars[char.corporation_id] = char

    # Build char_rows for wealth breakdown
    char_rows = []
    for char in characters:
        cid = char.character_id
        w = wallets.get(cid)
        loc = locations.get(cid)
        char_rows.append({
            "char": char,
            "wallet": w,
            "wallet_str": _format_isk_py(w) if w else "—",
            "is_online": loc.get("is_online", False) if isinstance(loc, dict) else False,
        })
    char_rows.sort(key=lambda x: x["wallet"] or 0, reverse=True)

    if perf_enabled():
        perf_log(
            "dashboard",
            total_ms=ms_since(_t0),
            live_gather=_live_ms,
            cache_stats=_cache_stats_ms,
            skillqueue=_skillqueue_ms,
        )
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "characters": characters,
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
        "needs_reauth": needs_reauth,
        "total_corporations": total_corporations,
        "char_rows": char_rows,
        "skill_map": skill_map,
        "sort": sort,
        "char_groups": char_groups,
        "killmails_enabled": _dashboard_pulse_enabled(),
        "battles_enabled": _dashboard_battles_enabled(),
    })


@router.get("/dashboard/kill-pulse", response_class=HTMLResponse)
async def dashboard_kill_pulse(
    request: Request,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """Pilot Pulse v2 + Frequent Wingmen + Your Hunters. Lazy-loaded via htmx
    so the dashboard renders without waiting on these queries."""
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_dashboard_enabled):
        return HTMLResponse("")

    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("<div class='b-empty'>Forbidden.</div>", status_code=403)

    char_result = await db.execute(
        select(Character.character_id, Character.character_name).where(Character.user_id == user_id)
    )
    char_rows_pulse = char_result.all()
    char_ids = [r[0] for r in char_rows_pulse]
    if not char_ids:
        return HTMLResponse("<div class='b-empty' style='padding:0.75rem;'>No characters linked.</div>")
    char_name_map = {r[0]: r[1] for r in char_rows_pulse}

    from app.intel import kill_queries as kq
    pulse_30, pulse_lifetime, per_char, wingmen, hunters = await asyncio.gather(
        kq.multi_character_summary(char_ids, days=days),
        kq.multi_character_summary(char_ids, days=None),
        kq.per_character_summary(char_ids, days=days),
        kq.frequent_wingmen(char_ids, days=90, limit=8),
        kq.your_hunters(char_ids, days=90, limit=8),
    )

    # Resolve wingmen (characters) and hunters (corps) to names via the
    # bulk /universe/names/ endpoint. Cached 30 days server-side by
    # _ttl_for_path, so after the first hit per window this is free.
    char_names: dict[int, str] = {}
    corp_names: dict[int, str] = {}
    name_ids = list(
        {w["character_id"] for w in wingmen} | {h["id"] for h in hunters}
    )
    if name_ids:
        try:
            from app.esi.client import ESIClient as _PubClient
            pub = _PubClient("")
            pub.cache_enabled = True
            # /universe/names/ accepts up to 1000 per call; we're well under.
            resolved = await pub.post_public("/universe/names/", name_ids)
            for entry in resolved or []:
                cat = entry.get("category", "")
                if cat == "character":
                    char_names[entry["id"]] = entry.get("name", "")
                elif cat == "corporation":
                    corp_names[entry["id"]] = entry.get("name", "")
        except Exception as e:
            logger.warning("Kill-pulse name resolution failed: %s", e)

    most_active_cid = None
    if per_char:
        ranked = sorted(
            per_char.items(),
            key=lambda kv: (kv[1]["kills"] + kv[1]["losses"], kv[1]["isk_destroyed"]),
            reverse=True,
        )
        if ranked and (ranked[0][1]["kills"] + ranked[0][1]["losses"]) > 0:
            most_active_cid = ranked[0][0]

    return templates.TemplateResponse("partials/dashboard_kill_pulse.html", {
        "request": request,
        "pulse_30": pulse_30,
        "pulse_lifetime": pulse_lifetime,
        "per_char": per_char,
        "wingmen": wingmen,
        "hunters": hunters,
        "char_name_map": char_name_map,
        "char_names": char_names,
        "corp_names": corp_names,
        "most_active_cid": most_active_cid,
        "days": days,
    })


@router.get("/dashboard/combat-profile", response_class=HTMLResponse)
async def dashboard_combat_profile(
    request: Request,
    year: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """All-pilots Combat Profile — aggregates the same panels as the
    character-detail Combat Profile across every character the user owns.
    Lazy-loaded via htmx so it doesn't block the dashboard handler."""
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_dashboard_enabled):
        return HTMLResponse("")

    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("<div class='b-empty'>Forbidden.</div>", status_code=403)

    char_rows = await db.execute(
        select(Character.character_id).where(Character.user_id == user_id)
    )
    char_ids = [r[0] for r in char_rows.all()]
    if not char_ids:
        return HTMLResponse("<div class='b-empty' style='padding:0.75rem;'>No characters linked.</div>")

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
        gang,
        cal_buckets,
        untouchable,
        profitability,
        ship_timeseries,
    ) = await asyncio.gather(
        kq.multi_character_summary(char_ids, days=90),
        kq.top_ships_used_multi(char_ids, days=90, limit=10),
        kq.top_weapons_used_multi(char_ids, days=90, limit=10),
        kq.top_systems_multi(char_ids, days=90, limit=10),
        kq.loss_autopsy_multi(char_ids, days=90),
        kq.solo_gang_split_multi(char_ids, days=90),
        kq.calendar_buckets_multi(char_ids, year=year),
        kq.untouchable_ships_multi(char_ids, min_uses=5),
        kq.profitability_by_ship_multi(char_ids, days=90),
        kq.ship_usage_timeseries_multi(char_ids, days=180),
    )

    profitability = sorted(profitability, key=lambda x: abs(x["net"]), reverse=True)[:10]

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

    # Calendar 53×7 padded grid
    year_start = datetime(year, 1, 1)
    start_weekday = year_start.weekday()
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

    # Radar — same six axes as per-character version
    avg_target_value = (summary["isk_destroyed"] / summary["kills"]) if summary["kills"] else 0
    tot_isk = summary["isk_destroyed"] + summary["isk_lost"]
    isk_efficiency = (summary["isk_destroyed"] / tot_isk * 100) if tot_isk > 0 else 0

    def _log_norm(v: float, ceiling_at: float) -> float:
        if v <= 0:
            return 0.0
        return float(min(100, _math.log1p(v) / _math.log1p(ceiling_at) * 100))

    radar_labels = ["Kill Volume", "ISK Eff %", "Solo %", "Gang %", "Avg Target", "Spread"]
    radar_values = [
        _log_norm(summary["kills"], 500),  # higher ceiling since summed across chars
        round(isk_efficiency, 1),
        round((gang["solo"] / gang_total * 100) if gang_total else 0, 1),
        round(((gang["small"] + gang["medium"] + gang["fleet"]) / gang_total * 100) if gang_total else 0, 1),
        _log_norm(avg_target_value, 1_000_000_000),
        _log_norm(len(system_ids), 50),
    ]
    radar_raw = [
        summary["kills"],
        round(isk_efficiency, 1),
        gang["solo"],
        gang["small"] + gang["medium"] + gang["fleet"],
        avg_target_value,
        len(system_ids),
    ]

    ts_datasets = []
    for tid, weekly in ship_timeseries.items():
        ts_datasets.append({
            "label": type_names.get(tid, f"Type {tid}"),
            "data": weekly,
            "fill": True,
            "tension": 0.3,
        })
    ts_weeks = max((len(d["data"]) for d in ts_datasets), default=0)

    return templates.TemplateResponse("partials/dashboard_combat_profile.html", {
        "request": request,
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
        "char_count": len(char_ids),
    })


@router.get("/dashboard/recent-battles", response_class=HTMLResponse)
async def dashboard_recent_battles(request: Request, db: AsyncSession = Depends(get_db)):
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_battles_enabled):
        return HTMLResponse("")
    from app.intel.recent_battles import query_battles_window, SEC_BAND_ORDER
    groups = await query_battles_window(days=7)
    # Top tier: only render WH classes that actually have battles. C1-C6 are
    # listed first in their natural order, then Thera/C13/Drifter/Pochven.
    wh_order = ["C1", "C2", "C3", "C4", "C5", "C6", "Thera", "C13 (Shattered)", "Drifter", "Pochven"]
    wh_cards: list[tuple[str, list]] = []
    for k in wh_order:
        if groups.get(k):
            wh_cards.append((k, groups[k][:2]))
    # Bottom tier: top-3 per band, then merged + sorted desc by kill_count.
    kspace_pool: list = []
    for band in SEC_BAND_ORDER:
        kspace_pool.extend((groups.get(band) or [])[:3])
    kspace_rows = sorted(kspace_pool, key=lambda b: b["kill_count"], reverse=True)
    return templates.TemplateResponse("partials/dashboard_recent_battles.html", {
        "request": request,
        "wh_cards": wh_cards,
        "kspace_rows": kspace_rows,
    })


@router.get("/dashboard/activity", response_class=HTMLResponse)
async def dashboard_activity(
    request: Request,
    window: str = "24h",
    db: AsyncSession = Depends(get_db),
):
    """Activity overlay: ISK destroyed + concurrent player count over a
    selectable window. Same chart, dual y-axis. Universe-wide ISK from the
    killmails table; PCU from player_count_snapshots (source='esi')."""
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_dashboard_enabled):
        return HTMLResponse("")

    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("<div class='b-empty'>Forbidden.</div>", status_code=403)

    # Window → (cutoff_delta, bin_seconds, label_fmt)
    windows = {
        "1h":    (timedelta(hours=1),    5 * 60,       "%H:%M"),
        "24h":   (timedelta(hours=24),   60 * 60,      "%H:00"),
        "week":  (timedelta(days=7),     6 * 3600,     "%b %d %H:00"),
        "month": (timedelta(days=30),    24 * 3600,    "%b %d"),
    }
    if window not in windows:
        window = "24h"
    delta, bin_seconds, label_fmt = windows[window]

    from app.db.models import Killmail, PlayerCountSnapshot
    from sqlalchemy import Integer, func as _func
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - delta
    total_seconds = int(delta.total_seconds())
    num_bins = max(1, total_seconds // bin_seconds)
    bin_starts: list[datetime] = [
        now - timedelta(seconds=total_seconds - i * bin_seconds) for i in range(num_bins)
    ]
    labels = [bs.strftime(label_fmt) for bs in bin_starts]

    # SQL-side binning — see comment in app/routes/player_stats.py. PCU
    # source filter ('esi') keeps short-window queries narrow, but we
    # group-aggregate either way so memory stays bounded.
    def _bin_expr(time_col):
        return _func.cast(
            (_func.julianday(time_col) - _func.julianday(cutoff))
            * 86400.0 / float(bin_seconds),
            Integer,
        )

    # ISK destroyed — sum(total_value) per bin
    isk_buckets = [0.0] * num_bins
    for b, isk in (await db.execute(
        select(_bin_expr(Killmail.killmail_time).label("b"),
               _func.sum(Killmail.total_value).label("isk"))
        .where(Killmail.killmail_time >= cutoff)
        .group_by("b")
    )).all():
        if b is None:
            continue
        i = int(b)
        if 0 <= i < num_bins:
            isk_buckets[i] = float(isk or 0.0)
    total_isk = sum(isk_buckets)

    # PCU — average per bin (concurrent count, not throughput)
    pcu_values: list[int | None] = [None] * num_bins
    for b, avg_pc in (await db.execute(
        select(_bin_expr(PlayerCountSnapshot.recorded_at).label("b"),
               _func.avg(PlayerCountSnapshot.player_count).label("avg_pc"))
        .where(PlayerCountSnapshot.recorded_at >= cutoff)
        .where(PlayerCountSnapshot.source == "esi")
        .group_by("b")
    )).all():
        if b is None or avg_pc is None:
            continue
        i = int(b)
        if 0 <= i < num_bins:
            pcu_values[i] = round(float(avg_pc))
    peak_pcu = max((v for v in pcu_values if v is not None), default=0)

    return templates.TemplateResponse(
        "partials/dashboard_activity.html",
        {
            "request": request,
            "window": window,
            "labels": labels,
            "isk_values": isk_buckets,
            "pcu_values": pcu_values,
            "total_isk": total_isk,
            "peak_pcu": peak_pcu,
        },
    )


@router.get("/dashboard/big-battle-banner", response_class=HTMLResponse)
async def dashboard_big_battle_banner(request: Request):
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_battles_enabled):
        return HTMLResponse("")
    from app.intel.recent_battles import active_big_battles
    battles = await active_big_battles()
    if not battles:
        return HTMLResponse("")
    return templates.TemplateResponse("partials/dashboard_big_battle_banner.html", {
        "request": request,
        "battles": battles,
    })


@router.post("/dashboard/group-order")
async def save_group_order(request: Request):
    """Save the visual order of account groups in the session."""
    data = await request.json()
    if isinstance(data, list):
        request.session["group_order"] = data
    return JSONResponse({"ok": True})


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


# In-memory cache for corp stats (avoids hitting ESI on every dashboard load)
_corp_stats_cache: dict[int, dict] = {}  # user_id -> {html, expires_at}
_CORP_STATS_TTL = 300  # 5 minutes

_structure_banner_cache: dict[int, dict] = {}  # user_id -> {html, expires_at}
_STRUCTURE_BANNER_TTL = 30  # seconds


@router.get("/notifications/poll")
async def notifications_poll(request: Request):
    """Return pending notification events for the current user and clear them."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse([])
    events = _notification_events.pop(user_id, [])
    return JSONResponse(events)


@router.post("/notifications/dismiss")
async def notifications_dismiss(request: Request):
    """Clear all pending notification events."""
    user_id = request.session.get("user_id")
    if user_id:
        _notification_events.pop(user_id, None)
    return JSONResponse({"ok": True})


def _parse_notif_text(text: str) -> dict:
    """Extract key-value pairs from ESI notification YAML text."""
    fields: dict[str, str] = {}
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


@router.get("/alerts/structure-banners", response_class=HTMLResponse)
async def structure_alert_banners(request: Request, db: AsyncSession = Depends(get_db)):
    """Return persistent banner HTML for active structure/fuel alerts."""
    _empty = '<div id="structure-alerts" hx-get="/alerts/structure-banners" hx-trigger="every 60s" hx-swap="outerHTML"></div>'
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse(_empty)

    cached = _structure_banner_cache.get(user_id)
    if cached and cached["expires_at"] > datetime.now(timezone.utc):
        return HTMLResponse(cached["html"])

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = result.scalars().all()
    char_ids = [c.character_id for c in characters]

    def _memo(html: str) -> None:
        _structure_banner_cache[user_id] = {
            "html": html,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_STRUCTURE_BANNER_TTL),
        }

    if not char_ids:
        _memo(_empty)
        return HTMLResponse(_empty)

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(char_ids))
    )
    caches = cache_result.scalars().all()

    DANGER_TYPES = {
        "StructureUnderAttack", "StructureLostShields", "StructureLostArmor",
        "StructureDestroyed", "TowerAlertMsg", "OrbitalAttacked",
    }
    WARN_TYPES = {
        "StructureFuelAlert", "StructureServicesOffline", "TowerResourceAlertMsg",
    }
    BANNER_TYPES = DANGER_TYPES | WARN_TYPES

    # Collect all banner-worthy notifications, dedup by (type, timestamp)
    seen_keys: dict[tuple, dict] = {}  # (type, timestamp) -> first notification data
    for cache in caches:
        if not cache.notifications_json:
            continue
        try:
            data = json.loads(cache.notifications_json)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for n in data.get("notifications", []):
            ntype = n.get("type", "")
            if ntype not in BANNER_TYPES:
                continue
            dedup_key = (ntype, n.get("timestamp", ""))
            if dedup_key in seen_keys:
                continue  # Already have this event from another character
            seen_keys[dedup_key] = n

    # Collect IDs we need to resolve from SDE
    system_ids: set[int] = set()
    type_ids: set[int] = set()
    for n in seen_keys.values():
        fields = _parse_notif_text(n.get("text", ""))
        sid = fields.get("solarsystemID") or fields.get("solarSystemID")
        if sid:
            try: system_ids.add(int(sid))
            except ValueError: pass
        tid = fields.get("structureTypeID")
        if tid:
            try: type_ids.add(int(tid))
            except ValueError: pass

    # Batch resolve names
    system_names: dict[int, str] = {}
    type_names: dict[int, str] = {}
    if system_ids:
        from app.db.sde_models import SDESystem
        sr = await db.execute(
            select(SDESystem.system_id, SDESystem.system_name)
            .where(SDESystem.system_id.in_(system_ids))
        )
        system_names = {r.system_id: r.system_name for r in sr.fetchall()}
    if type_ids:
        type_names = await sde.type_ids_to_names(db, list(type_ids))

    # Build enriched alert list
    alerts = []
    for (ntype, ts), n in seen_keys.items():
        severity = "danger" if ntype in DANGER_TYPES else "warn"
        label = _STRUCTURE_ALERT_LABELS.get(ntype, ntype)
        fields = _parse_notif_text(n.get("text", ""))

        # Resolve system name
        sid_str = fields.get("solarsystemID") or fields.get("solarSystemID")
        system_name = None
        if sid_str:
            try: system_name = system_names.get(int(sid_str))
            except ValueError: pass

        # Resolve structure type name
        tid_str = fields.get("structureTypeID")
        struct_type = None
        if tid_str:
            try: struct_type = type_names.get(int(tid_str))
            except ValueError: pass

        # Attacker info (for StructureUnderAttack)
        attacker = fields.get("corpName")
        alliance = fields.get("allianceName")
        attacker_label = None
        if attacker:
            attacker_label = f"{attacker} [{alliance}]" if alliance else attacker

        # Shield/armor/hull percentages
        shield = fields.get("shieldPercentage")
        armor = fields.get("armorPercentage")
        hull = fields.get("hullPercentage")
        hp_label = None
        if shield is not None and ntype in ("StructureUnderAttack",):
            try:
                hp_label = f"S:{float(shield):.0f}% A:{float(armor):.0f}% H:{float(hull):.0f}%"
            except (ValueError, TypeError):
                pass

        alerts.append({
            "id": n.get("notification_id"),
            "severity": severity,
            "label": label,
            "timestamp": ts,
            "system_name": system_name,
            "struct_type": struct_type,
            "attacker": attacker_label,
            "hp": hp_label,
        })

    alerts.sort(key=lambda a: a["timestamp"], reverse=True)
    html = templates.get_template("partials/structure_alert_banners.html").render(
        request=request, alerts=alerts,
    )
    _memo(html)
    return HTMLResponse(html)


@router.get("/alerts/inventory-banners", response_class=HTMLResponse)
async def inventory_alert_banners(request: Request, db: AsyncSession = Depends(get_db)):
    """Persistent banners for critical inventory levels."""
    _empty = '<div id="inventory-alerts" hx-get="/alerts/inventory-banners" hx-trigger="every 60s" hx-swap="outerHTML"></div>'
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse(_empty)

    from app.db.models import CorpInventoryThreshold
    result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.alert_state == "critical",
        )
    )
    critical_items = result.scalars().all()

    if not critical_items:
        return HTMLResponse(_empty)

    alerts = [{
        "id": t.id,
        "type_id": t.type_id,
        "type_name": t.type_name or f"Type {t.type_id}",
        "location_name": t.location_name or f"Location {t.location_id}",
        "current_quantity": t.current_quantity or 0,
        "threshold_critical": t.threshold_critical,
        "corp_id": t.corp_id,
    } for t in critical_items]

    return templates.TemplateResponse("partials/inventory_alert_banners.html", {
        "request": request, "alerts": alerts,
    })


@router.get("/alerts/contract-banners", response_class=HTMLResponse)
async def contract_alert_banners(request: Request, db: AsyncSession = Depends(get_db)):
    """Persistent banners for critical contract thresholds."""
    _empty = '<div id="contract-alerts" hx-get="/alerts/contract-banners" hx-trigger="every 60s" hx-swap="outerHTML"></div>'
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse(_empty)

    from app.db.models import CorpContractThreshold
    result = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.user_id == user_id,
            CorpContractThreshold.alert_state.in_(["critical", "low"]),
        )
    )
    alerts_list = result.scalars().all()

    if not alerts_list:
        return HTMLResponse(_empty)

    return templates.TemplateResponse("partials/contract_alert_banners.html", {
        "request": request, "alerts": alerts_list,
    })


@router.get("/alerts/timer-banners", response_class=HTMLResponse)
async def timer_alert_banners(request: Request, db: AsyncSession = Depends(get_db)):
    """Persistent banners for structure timers expiring within 24 hours."""
    _empty = '<div id="timer-alerts" hx-get="/alerts/timer-banners" hx-trigger="every 60s" hx-swap="outerHTML"></div>'
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse(_empty)

    from app.db.models import StructureTimer, TimerACLEntry
    from app.routes.structure_timers import _visible_group_ids, _timer_visible

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now + timedelta(hours=24)
    grace = now - timedelta(minutes=30)  # Show 0m for 30 min after expiry

    result = await db.execute(
        select(StructureTimer).where(
            StructureTimer.is_archived == False,
            StructureTimer.timer_expires > grace,
            StructureTimer.timer_expires <= cutoff,
        ).order_by(StructureTimer.timer_expires.asc())
    )
    timers = result.scalars().all()

    if not timers:
        return HTMLResponse(_empty)

    # Filter by ACL visibility
    visible_groups = await _visible_group_ids(db, user_id)
    timers = [t for t in timers if _timer_visible(t, visible_groups)]

    if not timers:
        return HTMLResponse(_empty)

    return templates.TemplateResponse("partials/timer_alert_banners.html", {
        "request": request, "timers": timers,
    })


@router.get("/dashboard/corp-stats", response_class=HTMLResponse)
async def corp_stats_partial(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX lazy-loaded corporation stats for the dashboard summary bar."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    # Check in-memory cache first
    cached = _corp_stats_cache.get(user_id)
    if cached and cached["expires_at"] > datetime.now(timezone.utc):
        return HTMLResponse(cached["html"])

    char_result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = char_result.scalars().all()

    # Build list of characters per player corp (skip NPC corps)
    corp_char_lists: dict[int, list[Character]] = {}
    for char in characters:
        if not char.corporation_id or char.corporation_id < 2000000:
            continue
        corp_char_lists.setdefault(char.corporation_id, []).append(char)

    corp_wallet_total = 0.0
    corp_active_jobs = 0
    corp_done_jobs = 0
    corp_expiring_jobs = 0
    corp_active_orders = 0
    now = datetime.now(timezone.utc)
    threshold_48h = now + timedelta(hours=48)

    async def _try_corp_call(chars: list[Character], api_func, corp_id: int):
        """Try an ESI corp call with each character until one succeeds (has in-game role)."""
        for char in chars:
            if (char.character_id, corp_id) in _corp_403_cache:
                continue  # Already know this char lacks the role
            try:
                client, err = await _client_for(char, db)
                if err or not client:
                    continue
                return await api_func(client, corp_id)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    _corp_403_cache.add((char.character_id, corp_id))
                    continue
                raise
            except Exception:
                continue
        return None

    async def fetch_corp_data(corp_id: int, chars: list[Character]):
        nonlocal corp_wallet_total, corp_active_jobs, corp_done_jobs, corp_expiring_jobs, corp_active_orders
        all_scopes = set()
        for c in chars:
            all_scopes.update((c.scopes or "").split())

        try:
            tasks = []
            if "esi-wallet.read_corporation_wallets.v1" in all_scopes:
                tasks.append(("wallet", _try_corp_call(chars, esi_corp.get_corporation_wallets, corp_id)))
            if "esi-industry.read_corporation_jobs.v1" in all_scopes:
                tasks.append(("industry", _try_corp_call(chars, esi_corp.get_corporation_jobs, corp_id)))
            if "esi-markets.read_corporation_orders.v1" in all_scopes:
                tasks.append(("orders", _try_corp_call(chars, esi_corp.get_corporation_orders, corp_id)))

            if not tasks:
                return

            results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

            for (label, _), result in zip(tasks, results):
                if result is None or isinstance(result, Exception):
                    continue
                if label == "wallet" and isinstance(result, list):
                    corp_wallet_total += sum(d.get("balance", 0) for d in result)
                elif label == "industry" and isinstance(result, list):
                    for job in result:
                        status = job.get("status", "")
                        if status == "active":
                            corp_active_jobs += 1
                            end_str = job.get("end_date")
                            if end_str:
                                try:
                                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                    if end_dt <= threshold_48h:
                                        corp_expiring_jobs += 1
                                except Exception:
                                    pass
                        elif status == "ready":
                            corp_done_jobs += 1
                elif label == "orders" and isinstance(result, list):
                    corp_active_orders += len(result)
        except Exception:
            pass

    await asyncio.gather(*[fetch_corp_data(cid, chars) for cid, chars in corp_char_lists.items()])

    # Format wallet
    if corp_wallet_total >= 1e12:
        wallet_str = f"{corp_wallet_total / 1e12:.2f}T ISK"
    elif corp_wallet_total >= 1e9:
        wallet_str = f"{corp_wallet_total / 1e9:.2f}B ISK"
    elif corp_wallet_total >= 1e6:
        wallet_str = f"{corp_wallet_total / 1e6:.2f}M ISK"
    else:
        wallet_str = f"{corp_wallet_total:,.0f} ISK"

    # Build jobs label
    jobs_parts = []
    if corp_active_jobs:
        jobs_parts.append(f"{corp_active_jobs} active")
    if corp_expiring_jobs:
        jobs_parts.append(f"{corp_expiring_jobs} <48h")
    if corp_done_jobs:
        jobs_parts.append(f"{corp_done_jobs} done")
    jobs_str = " · ".join(jobs_parts) if jobs_parts else "0"

    html = f"""
    <a href="/corporations" class="b-stat" style="text-decoration:none;cursor:pointer;">
        <div class="b-stat-val is-accent">{wallet_str}</div>
        <div class="b-stat-label">Corp Wallet</div>
    </a>
    <a href="/corporations" class="b-stat" style="text-decoration:none;cursor:pointer;">
        <div class="b-stat-val" style="font-size:{'11px' if len(jobs_str) > 10 else '14px'};">{jobs_str}</div>
        <div class="b-stat-label">Corp Jobs</div>
    </a>
    <a href="/corporations" class="b-stat" style="text-decoration:none;cursor:pointer;">
        <div class="b-stat-val">{corp_active_orders}</div>
        <div class="b-stat-label">Corp Orders</div>
    </a>
    """
    # Cache the result
    _corp_stats_cache[user_id] = {
        "html": html,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_CORP_STATS_TTL),
    }
    return HTMLResponse(html)


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
