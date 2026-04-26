"""Route safety / intel — shared kill analysis used by both /intel/gatecheck
and /api/map/route-safety.

Pulls killmail data from zKillboard (with a 5-minute in-memory cache and
rate-limited concurrency), filters to gate kills only, fetches the full
killmail bodies from ESI, and produces a per-system threat assessment with
smartbomb / interdictor / heavy-interdictor warning flags.
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal
from app.esi.client import ESIClient, get_http_client
from app.sde import lookup as sde

log = logging.getLogger(__name__)

# ── zKillboard client with caching ──────────────────────────────────────────

ZKB_BASE = "https://zkillboard.com/api"
ZKB_HEADERS = {
    "Accept-Encoding": "gzip",
    "User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)",
    "Accept": "application/json",
}

from collections import OrderedDict

_zkb_cache: OrderedDict[str, tuple[list, float]] = OrderedDict()
_ZKB_CACHE_MAX = 500
_ZKB_CACHE_TTL = 300  # 5 minutes
_zkb_sem = asyncio.Semaphore(5)


async def zkb_get(path: str) -> list:
    """Fetch from zKillboard API with 5-minute cache and rate limiting."""
    url = f"{ZKB_BASE}{path}"
    now = time.time()
    cached = _zkb_cache.get(url)
    if cached and cached[1] > now:
        _zkb_cache.move_to_end(url)
        return cached[0]

    async with _zkb_sem:
        cached = _zkb_cache.get(url)
        if cached and cached[1] > now:
            _zkb_cache.move_to_end(url)
            return cached[0]

        client = get_http_client()
        try:
            resp = await client.get(url, headers=ZKB_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    data = []
                _zkb_cache[url] = (data, now + _ZKB_CACHE_TTL)
                _zkb_cache.move_to_end(url)
                # Bounded LRU: evict oldest until under the cap. Previous
                # cleanup only dropped expired entries — under sustained
                # traffic with refresh > expiry, the cache could grow past
                # the cap indefinitely.
                while len(_zkb_cache) > _ZKB_CACHE_MAX:
                    _zkb_cache.popitem(last=False)
                return data
            if resp.status_code == 429:
                await asyncio.sleep(2)
            return []
        except Exception as e:
            log.warning("zKB %s: %s", path, e)
            return []


# ── ESI helpers ─────────────────────────────────────────────────────────────

_esi_sem = asyncio.Semaphore(10)


async def get_system_gates(system_id: int) -> set[int]:
    """Get stargate IDs for a system from ESI."""
    async with AsyncSessionLocal() as db:
        client = ESIClient("", db=db)
        try:
            data = await client.get_public(f"/universe/systems/{system_id}/")
            if isinstance(data, dict):
                return set(data.get("stargates", []))
        except Exception:
            pass
    return set()


async def fetch_killmail(killmail_id: int, km_hash: str) -> dict | None:
    """Fetch full killmail from ESI using the hash from zKillboard."""
    async with _esi_sem:
        async with AsyncSessionLocal() as db:
            client = ESIClient("", db=db)
            try:
                data = await client.get_public(
                    f"/killmails/{killmail_id}/{km_hash}/",
                )
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def is_gate_location(location_id: int) -> bool:
    """Heuristic: stargate IDs are in the 50_000_000 range."""
    return 50_000_000 <= location_id <= 59_999_999


# ── Ship group constants for threat classification ──────────────────────────

DICTOR_GROUPS = {541}   # Interdictor
HIC_GROUPS = {894}      # Heavy Interdictor


# ── Formatting helpers ───────────────────────────────────────────────────────

def sec_color(sec: float) -> str:
    if sec >= 0.9:
        return "#33aa55"
    if sec >= 0.7:
        return "#55cc55"
    if sec >= 0.5:
        return "#88bb44"
    if sec >= 0.3:
        return "#cc8844"
    if sec >= 0.1:
        return "#cc5533"
    return "#cc3333"


def format_isk(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def time_ago(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h {mins % 60}m ago"
    except Exception:
        return "?"


# ── Kill analysis ────────────────────────────────────────────────────────────

def analyze_kills(
    kills: list,
    type_names: dict[int, str],
    group_ids: dict[int, int | None],
) -> dict:
    """Analyze full killmails (ESI format + merged zkb) for threat indicators."""
    if not kills:
        return {
            "kill_count": 0, "pvp_kills": 0, "threat": "safe",
            "has_smartbombs": False, "has_dictors": False, "has_hics": False,
            "total_value": 0, "total_value_str": "0", "kills": [],
        }

    has_sb = has_dic = has_hic = False
    total_val = 0
    analyzed = []

    for km in kills:
        victim = km.get("victim", {})
        attackers = km.get("attackers", [])
        zkb = km.get("zkb", {})
        is_npc = zkb.get("npc", False)

        v_ship_id = victim.get("ship_type_id", 0)
        v_ship = type_names.get(v_ship_id, f"Unknown ({v_ship_id})")
        kill_val = zkb.get("totalValue", 0)
        total_val += kill_val

        att_ships: dict[int, int] = {}  # type_id → count
        att_weapons = Counter()

        for att in attackers:
            sid = att.get("ship_type_id", 0)
            wid = att.get("weapon_type_id", 0)
            gid = group_ids.get(sid)

            if sid:
                att_ships[sid] = att_ships.get(sid, 0) + 1
                if gid in DICTOR_GROUPS:
                    has_dic = True
                if gid in HIC_GROUPS:
                    has_hic = True

            if wid:
                wname = type_names.get(wid, "")
                if wname:
                    att_weapons[wname] += 1
                    if "smartbomb" in wname.lower():
                        has_sb = True

        # Build attacker ships as list of dicts with type_id for linking
        top_att = sorted(att_ships.items(), key=lambda x: -x[1])[:8]
        att_ships_list = [
            {"name": type_names.get(tid, f"Unknown ({tid})"), "type_id": tid, "count": cnt}
            for tid, cnt in top_att
        ]

        analyzed.append({
            "killmail_id": km.get("killmail_id"),
            "time_str": time_ago(km.get("killmail_time", "")),
            "victim_ship": v_ship,
            "victim_ship_id": v_ship_id,
            "victim_char_id": victim.get("character_id"),
            "attacker_count": len(attackers),
            "attacker_ships": att_ships_list,
            "attacker_weapons": dict(att_weapons.most_common(8)),
            "value": kill_val,
            "value_str": format_isk(kill_val),
            "is_npc": is_npc,
        })

    pvp = [k for k in analyzed if not k["is_npc"]]
    n = len(pvp)
    threat = "safe" if n == 0 else ("caution" if n <= 2 else "dangerous")
    if has_sb:
        threat = "smartbomb"

    return {
        "kill_count": len(kills),
        "pvp_kills": n,
        "threat": threat,
        "has_smartbombs": has_sb,
        "has_dictors": has_dic,
        "has_hics": has_hic,
        "total_value": total_val,
        "total_value_str": format_isk(total_val),
        "kills": analyzed,
    }


# ── Bulk type resolution ────────────────────────────────────────────────────

async def resolve_type_ids(
    db: AsyncSession, kills_by_key: dict,
) -> tuple[dict[int, str], dict[int, int | None]]:
    """Collect all type IDs from full killmails and resolve names + groups."""
    tids: set[int] = set()
    for kills in kills_by_key.values():
        for km in kills:
            v = km.get("victim", {})
            if v.get("ship_type_id"):
                tids.add(v["ship_type_id"])
            for a in km.get("attackers", []):
                if a.get("ship_type_id"):
                    tids.add(a["ship_type_id"])
                if a.get("weapon_type_id"):
                    tids.add(a["weapon_type_id"])
    tid_list = list(tids)
    names = await sde.type_ids_to_names(db, tid_list) if tid_list else {}
    groups = await sde.get_type_group_ids(db, tid_list) if tid_list else {}
    return names, groups


# ── Fetch full killmail data for a set of zKB entries ────────────────────────

async def enrich_kills(zkb_kills: list, max_per_call: int = 15) -> list[dict]:
    """Fetch full killmail data from ESI for a list of zKB entries.

    Returns merged killmails (ESI data + zkb section). Caps at max_per_call
    to avoid excessive API requests.
    """
    to_fetch = []
    for km in zkb_kills[:max_per_call]:
        km_id = km.get("killmail_id")
        km_hash = km.get("zkb", {}).get("hash", "")
        if km_id and km_hash:
            to_fetch.append((km_id, km_hash, km.get("zkb", {})))

    if not to_fetch:
        return []

    async def fetch_one(km_id: int, km_hash: str, zkb: dict):
        full = await fetch_killmail(km_id, km_hash)
        if full:
            full["zkb"] = zkb
        return full

    results = await asyncio.gather(
        *[fetch_one(km_id, km_hash, zkb) for km_id, km_hash, zkb in to_fetch]
    )
    return [r for r in results if r]


# ── Route checking ───────────────────────────────────────────────────────────

async def check_route_systems(route: list[int], db: AsyncSession) -> list[dict]:
    """Per-system threat analysis for a route. Backed by the killmail.stream
    rolling 1h buffer (app/intel/killmail_stream.py::get_recent_kills) instead
    of per-system zKB + ESI calls. Zero external API calls at request time —
    analysis runs on in-memory data that's kept warm by the live consumer.

    Gate-kill filter uses the 50M-59.9M stargate ID heuristic (is_gate_location)
    instead of a per-system ESI stargate lookup.
    """
    from app.intel.killmail_stream import get_recent_kills

    route_set = set(route)
    recent = get_recent_kills(window_seconds=3600, systems=route_set)

    gate_kills_by_sys: dict[int, list] = {sid: [] for sid in route}
    for km in recent:
        sid = km.get("solar_system_id")
        if sid not in route_set:
            continue
        loc_id = km.get("zkb", {}).get("locationID") or 0
        if is_gate_location(loc_id):
            gate_kills_by_sys.setdefault(sid, []).append(km)

    type_names, group_ids = await resolve_type_ids(db, gate_kills_by_sys)

    sys_info: dict[int, dict] = {}
    for sid in route:
        info = await sde.system_info(db, sid)
        if info:
            sys_info[sid] = info

    out = []
    for i, sid in enumerate(route):
        info = sys_info.get(sid, {})
        sec = info.get("security", 0)
        analysis = analyze_kills(
            gate_kills_by_sys.get(sid, []), type_names, group_ids,
        )
        out.append({
            "waypoint": i + 1,
            "system_id": sid,
            "system_name": info.get("system_name", str(sid)),
            "security": sec,
            "sec_color": sec_color(sec),
            "region": info.get("region", "?"),
            **analysis,
        })
    return out
