"""Recent battle discovery + persistence.

This is the most expensive piece of the killmail subsystem. Design rules
tightened vs. the reverted first attempt:

- Hard cap: 100 ESI hydrations per 15-min run (was 400).
- Disk-first: killmails already in our `killmails` table are reused, no
  ESI re-fetch. After ~48h steady state the hit rate is very high.
- `detected_battles` is the ONLY thing the dashboard widget reads. Discovery
  writes aggregated rows; render cost is cheap.
- Gated by `killmails_enabled AND killmail_battles_enabled` — the scheduler
  skips this entirely when flags are off.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import (
    AsyncSessionLocal,
    DetectedBattle,
    Killmail,
    KillmailAttacker,
    SystemActivitySnapshot,
)
from app.db.sde_models import SDESystem
from app.intel.killmail_store import fetch_killmail, store_killmail, get_our_char_ids
from app.sde.lookup import get_system_wh_class

log = logging.getLogger(__name__)

ZKB_BASE = "https://zkillboard.com/api"
ZKB_HEADERS = {
    "User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)",
    "Accept-Encoding": "gzip",
    "Accept": "application/json",
}

BATTLE_GAP_SECONDS = 30 * 60
BATTLE_MIN_KILLS = 3
LOOKBACK_HOURS = 4
BUSY_SYSTEM_THRESHOLD = 5
MAX_BUSY_SYSTEMS = 20
MAX_HYDRATIONS_PER_RUN = 100
BATTLES_PER_GROUP = 10
# Minimum unique pilots (attackers ∪ victim) for a cluster to persist.
# K-space is saturated; keep the bar at real-fleet-fight scale. W-space
# is sparsely populated, so a handful of pilots is already meaningful.
MIN_PILOTS_KSPACE = 50
MIN_PILOTS_WSPACE = 5


def wh_class_label(wc: int | None) -> str | None:
    if wc is None:
        return None
    if 1 <= wc <= 6:
        return f"C{wc}"
    if wc == 12:
        return "Thera"
    if wc == 13:
        return "C13 (Shattered)"
    if 14 <= wc <= 18:
        return "Drifter"
    if wc == 25:
        return "Pochven"
    return None


WH_CLASS_ORDER = ["C5", "C6", "C4", "C3", "C2", "C1", "Thera", "C13 (Shattered)", "Drifter", "Pochven"]
SEC_BAND_ORDER = ["Nullsec", "Lowsec", "Highsec"]


def sec_band(sec: float | None) -> str:
    if sec is None:
        return "Unknown"
    if sec >= 0.5:
        return "Highsec"
    if sec >= 0.0:
        return "Lowsec"
    return "Nullsec"


# Process-level cache for entity ID → name. Alliance/corp names are stable
# enough that a single fetch per process is plenty for our purposes.
_entity_name_cache: dict[int, str | None] = {}


async def resolve_entity_names(ids: list[int]) -> dict[int, str]:
    """Resolve a mixed list of corp/alliance IDs to names via ESI bulk
    /universe/names/. Returns {id: name} for resolved IDs only. Caches in
    process; safe to call with overlapping IDs from many call sites."""
    out: dict[int, str] = {}
    missing: list[int] = []
    for i in ids:
        if not i:
            continue
        if i in _entity_name_cache:
            n = _entity_name_cache[i]
            if n:
                out[i] = n
        else:
            missing.append(i)
    if not missing:
        return out
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            for chunk_start in range(0, len(missing), 1000):
                chunk = missing[chunk_start:chunk_start + 1000]
                resp = await http.post(
                    "https://esi.evetech.net/latest/universe/names/",
                    json=chunk,
                    headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"},
                )
                if resp.status_code != 200:
                    # Negative-cache the chunk so we don't keep retrying bad IDs
                    for i in chunk:
                        _entity_name_cache.setdefault(i, None)
                    continue
                seen: set[int] = set()
                for entry in resp.json():
                    eid = entry.get("id")
                    name = entry.get("name")
                    if isinstance(eid, int) and isinstance(name, str):
                        _entity_name_cache[eid] = name
                        out[eid] = name
                        seen.add(eid)
                for i in chunk:
                    if i not in seen:
                        _entity_name_cache.setdefault(i, None)
    except Exception as e:
        log.debug("resolve_entity_names: %s", e)
    return out


def pick_top_entities(
    kill_pairs: list[tuple[int | None, int | None, int | None, int | None]],
) -> dict:
    """Identify two opposing sides from per-kill (att_alli, att_corp, vic_alli, vic_corp).

    In fleet battles both sides appear as attackers across different kills, so
    tallying all attackers vs all victims conflates both coalitions. Instead we
    build a directed kill matrix: att_key → vic_key → count. The undirected pair
    with the highest total conflict is the two main combatants. The side that
    inflicted more kills gets the 'attacker' label; the other gets 'victim'.
    Returns top_attacker_* and top_victim_* keys for DetectedBattle upsert.
    """
    _empty = {
        "top_attacker_corp_id": None, "top_attacker_corp_kills": 0,
        "top_attacker_alliance_id": None,
        "top_victim_corp_id": None, "top_victim_corp_kills": 0,
        "top_victim_alliance_id": None,
    }
    if not kill_pairs:
        return _empty

    from collections import defaultdict

    directed: dict[tuple[int, int], int] = defaultdict(int)
    # entity_key → (corp_id, alliance_id) for name resolution later
    entity_meta: dict[int, tuple[int | None, int | None]] = {}

    for att_alli, att_corp, vic_alli, vic_corp in kill_pairs:
        a_key = att_alli if att_alli else att_corp
        v_key = vic_alli if vic_alli else vic_corp
        if not a_key or not v_key or a_key == v_key:
            continue
        directed[(a_key, v_key)] += 1
        entity_meta.setdefault(a_key, (att_corp, att_alli))
        entity_meta.setdefault(v_key, (vic_corp, vic_alli))

    if not directed:
        return _empty

    # Find the undirected pair with the most total mutual kills
    seen: set[tuple[int, int]] = set()
    best_pair: tuple[int, int] | None = None
    best_total = 0
    for a_key, v_key in list(directed):
        pair = (min(a_key, v_key), max(a_key, v_key))
        if pair in seen:
            continue
        seen.add(pair)
        total = directed.get((a_key, v_key), 0) + directed.get((v_key, a_key), 0)
        if total > best_total:
            best_total = total
            best_pair = (a_key, v_key)

    if not best_pair:
        return _empty

    a, b = best_pair
    a_kills = directed.get((a, b), 0)
    b_kills = directed.get((b, a), 0)
    att_key, vic_key = (a, b) if a_kills >= b_kills else (b, a)

    att_corp_id, att_alli_id = entity_meta.get(att_key, (None, None))
    vic_corp_id, vic_alli_id = entity_meta.get(vic_key, (None, None))
    return {
        "top_attacker_corp_id": att_corp_id,
        "top_attacker_corp_kills": max(a_kills, b_kills),
        "top_attacker_alliance_id": att_alli_id,
        "top_victim_corp_id": vic_corp_id,
        "top_victim_corp_kills": min(a_kills, b_kills),
        "top_victim_alliance_id": vic_alli_id,
    }


async def _find_busy_systems() -> list[int]:
    """Seed the discovery from SystemActivitySnapshot — pick systems above the
    kill threshold. Avoids zKB's entity-filter restriction entirely."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        q = (
            select(
                SystemActivitySnapshot.system_id,
                func.sum(SystemActivitySnapshot.ship_kills).label("kills"),
            )
            .where(SystemActivitySnapshot.captured_at >= cutoff)
            .group_by(SystemActivitySnapshot.system_id)
            .having(func.sum(SystemActivitySnapshot.ship_kills) >= BUSY_SYSTEM_THRESHOLD)
            .order_by(func.sum(SystemActivitySnapshot.ship_kills).desc())
            .limit(MAX_BUSY_SYSTEMS)
        )
        rows = (await db.execute(q)).all()
    return [sid for sid, _ in rows]


_zkb_sem = asyncio.Semaphore(3)


async def _fetch_system_kills(http: httpx.AsyncClient, system_id: int) -> list[dict]:
    async with _zkb_sem:
        url = f"{ZKB_BASE}/systemID/{system_id}/"
        try:
            resp = await http.get(url, headers=ZKB_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            elif resp.status_code == 429:
                await asyncio.sleep(2)
        except Exception as e:
            log.debug("recent_battles: zKB system %s error: %s", system_id, e)
        return []


async def _cluster_system(
    system_id: int,
    kills: list[dict],
    sys_meta: dict,
    hydration_budget: list[int],
    our_ids: set[int],
) -> list[dict]:
    """Cluster kills in one system by 30-min gap. Produces dicts with the
    aggregate fields DetectedBattle needs. Hydrates via killmails table
    first; ESI only when budget remains and kill not already stored.

    hydration_budget is a mutable list wrapping [int] so we can decrement.
    """
    if len(kills) < BATTLE_MIN_KILLS:
        return []

    # zKB's /api/systemID/ payload only has {killmail_id, zkb{hash,totalValue,...}}
    # — NO killmail_time. Time comes from our own killmails table (disk-first) or
    # from ESI hydration. Hence the lookback filter must run AFTER hydration, not
    # before. Do not re-introduce a pre-hydration time filter here.
    parsed_raw: list[dict] = []
    for km in kills:
        kid = km.get("killmail_id")
        zkb = km.get("zkb", {}) or {}
        khash = zkb.get("hash")
        if kid and khash:
            parsed_raw.append({
                "killmail_id": kid,
                "killmail_hash": khash,
                "zkb": zkb,
            })
    if len(parsed_raw) < BATTLE_MIN_KILLS:
        return []

    # Disk-first hydration (includes killmail_time — the field zKB doesn't give us)
    km_ids = [p["killmail_id"] for p in parsed_raw]
    hydrated: dict[int, dict] = {}
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(
                Killmail.killmail_id,
                Killmail.killmail_time,
                Killmail.victim_ship_type_id,
                Killmail.victim_corporation_id,
                Killmail.total_value,
                Killmail.attacker_count,
                Killmail.final_blow_character_id,
            ).where(Killmail.killmail_id.in_(km_ids))
        )
        for r in rows.all():
            hydrated[r[0]] = {
                "killmail_time": r[1],
                "victim_ship_type_id": r[2],
                "victim_corporation_id": r[3],
                "total_value": r[4] or 0.0,
                "attacker_count": r[5] or 1,
                "final_blow_character_id": r[6],
            }

    # ESI-fetch misses (zKB returns newest-first, so walk in order). Per-system
    # cap prevents one big system from starving the other 19 of the 100-budget.
    PER_SYSTEM_HYDRATION_CAP = 15
    missing = [p for p in parsed_raw if p["killmail_id"] not in hydrated]
    if missing and hydration_budget[0] > 0:
        take = min(hydration_budget[0], PER_SYSTEM_HYDRATION_CAP, len(missing))
        to_fetch = missing[:take]
        hydration_budget[0] -= len(to_fetch)

        async def _grab(entry):
            kid = entry["killmail_id"]
            khash = entry["killmail_hash"]
            zkb = entry["zkb"]
            full = await fetch_killmail(kid, khash)
            if not full:
                return kid, None
            await store_killmail(full, zkb, our_ids)
            victim = full.get("victim") or {}
            attackers = full.get("attackers") or []
            fb = next((a for a in attackers if a.get("final_blow")), None)
            try:
                kt = datetime.fromisoformat(
                    (full.get("killmail_time") or "").replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (ValueError, AttributeError):
                kt = None
            return kid, {
                "killmail_time": kt,
                "victim_ship_type_id": victim.get("ship_type_id"),
                "victim_corporation_id": victim.get("corporation_id"),
                "total_value": float(zkb.get("totalValue") or 0),
                "attacker_count": len(attackers),
                "final_blow_character_id": (fb or {}).get("character_id"),
            }

        results = await asyncio.gather(*[_grab(e) for e in to_fetch], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                continue
            kid, data = r
            if data and data.get("killmail_time") is not None:
                hydrated[kid] = data

    # Apply lookback filter using disk/ESI-sourced times (tz-naive throughout)
    cutoff_naive = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).replace(tzinfo=None)
    parsed: list[dict] = []
    for p in parsed_raw:
        h = hydrated.get(p["killmail_id"])
        if not h or h.get("killmail_time") is None:
            continue
        if h["killmail_time"] < cutoff_naive:
            continue
        parsed.append({"killmail_id": p["killmail_id"], "kill_time": h["killmail_time"]})
    if len(parsed) < BATTLE_MIN_KILLS:
        return []
    parsed.sort(key=lambda x: x["kill_time"])

    # Cluster by 30-min gap
    clusters: list[list[dict]] = []
    current: list[dict] = []
    for km in parsed:
        if not current:
            current = [km]
            continue
        if (km["kill_time"] - current[-1]["kill_time"]).total_seconds() <= BATTLE_GAP_SECONDS:
            current.append(km)
        else:
            if len(current) >= BATTLE_MIN_KILLS:
                clusters.append(current)
            current = [km]
    if len(current) >= BATTLE_MIN_KILLS:
        clusters.append(current)

    if not clusters:
        return []

    # Batched lookup: unique pilots per kill plus corp/alliance for each
    # pilot so we can derive the dominant attacker/victim entities.
    all_cluster_km_ids = [km["killmail_id"] for c in clusters for km in c]
    attackers_by_km: dict[int, set[int]] = {}
    victims_by_km: dict[int, int] = {}
    # character_id -> (corp_id, alliance_id) for each side, scoped per cluster
    attacker_pilot_orgs: dict[int, dict[int, tuple[int | None, int | None]]] = {}
    victim_pilot_orgs: dict[int, dict[int, tuple[int | None, int | None]]] = {}
    if all_cluster_km_ids:
        async with AsyncSessionLocal() as db:
            arows = await db.execute(
                select(
                    KillmailAttacker.killmail_id,
                    KillmailAttacker.character_id,
                    KillmailAttacker.corporation_id,
                    KillmailAttacker.alliance_id,
                )
                .where(KillmailAttacker.killmail_id.in_(all_cluster_km_ids))
                .where(KillmailAttacker.character_id.is_not(None))
            )
            attacker_orgs_by_km: dict[int, list[tuple[int, int | None, int | None]]] = {}
            for kid, cid, corp, alli in arows.all():
                attackers_by_km.setdefault(kid, set()).add(cid)
                attacker_orgs_by_km.setdefault(kid, []).append((cid, corp, alli))
            vrows = await db.execute(
                select(
                    Killmail.killmail_id,
                    Killmail.victim_character_id,
                    Killmail.victim_corporation_id,
                    Killmail.victim_alliance_id,
                )
                .where(Killmail.killmail_id.in_(all_cluster_km_ids))
                .where(Killmail.victim_character_id.is_not(None))
            )
            victim_orgs_by_km: dict[int, tuple[int, int | None, int | None]] = {}
            for kid, cid, corp, alli in vrows.all():
                victims_by_km[kid] = cid
                victim_orgs_by_km[kid] = (cid, corp, alli)

    band_is_wspace = sys_meta.get("band") == "w-space"
    min_pilots = MIN_PILOTS_WSPACE if band_is_wspace else MIN_PILOTS_KSPACE

    # Build the DetectedBattle-shaped dicts
    out: list[dict] = []
    for c in clusters:
        start = c[0]["kill_time"]
        end = c[-1]["kill_time"]
        duration = max(1, int((end - start).total_seconds() / 60))
        kill_count = len(c)
        total_isk = 0.0
        ship_counter: Counter = Counter()
        pilots: set = set()
        # Per-kill (att_alli, att_corp, vic_alli, vic_corp) tuples for conflict matrix
        kill_pairs: list[tuple[int | None, int | None, int | None, int | None]] = []
        for km in c:
            kid = km["killmail_id"]
            h = hydrated.get(kid)
            if not h:
                continue  # couldn't hydrate under budget — skip this kill's detail
            total_isk += h.get("total_value") or 0
            sid = h.get("victim_ship_type_id")
            if sid:
                ship_counter[sid] += 1
            pilots.update(attackers_by_km.get(kid, set()))
            v_cid = victims_by_km.get(kid)
            if v_cid:
                pilots.add(v_cid)
            # Build directed kill pair: dominant attacker alliance vs victim alliance
            v = victim_orgs_by_km.get(kid)
            vic_alli = v[2] if v else None
            vic_corp = v[1] if v else None
            att_orgs = attacker_orgs_by_km.get(kid, [])
            alli_counts: dict[int, int] = {}
            corp_counts: dict[int, int] = {}
            for _, corp, alli in att_orgs:
                if alli and alli != vic_alli:
                    alli_counts[alli] = alli_counts.get(alli, 0) + 1
                elif corp and corp != vic_corp and not alli:
                    corp_counts[corp] = corp_counts.get(corp, 0) + 1
            if alli_counts:
                att_alli = max(alli_counts, key=alli_counts.get)
                att_corp = next((corp for _, corp, al in att_orgs if al == att_alli and corp), None)
            elif corp_counts:
                att_alli = None
                att_corp = max(corp_counts, key=corp_counts.get)
            else:
                att_alli = att_corp = None
            if (att_alli or att_corp) and (vic_alli or vic_corp):
                kill_pairs.append((att_alli, att_corp, vic_alli, vic_corp))
        if not ship_counter:
            continue
        if len(pilots) < min_pilots:
            continue
        top_ships = [
            {"id": sid, "count": n}
            for sid, n in ship_counter.most_common(5)
        ]
        ents = pick_top_entities(kill_pairs)
        names = await resolve_entity_names([
            i for i in (
                ents["top_attacker_corp_id"], ents["top_attacker_alliance_id"],
                ents["top_victim_corp_id"], ents["top_victim_alliance_id"],
            ) if i
        ])
        out.append({
            "system_id": system_id,
            "system_name": sys_meta.get("system_name"),
            "security": sys_meta.get("security"),
            "group_label": sys_meta.get("group_label") or sec_band(sys_meta.get("security")),
            "band": sys_meta.get("band") or sec_band(sys_meta.get("security")),
            "start_time": start,
            "end_time": end,
            "duration_minutes": duration,
            "kill_count": kill_count,
            "pilots_involved": len(pilots),
            "total_isk": total_isk,
            "top_attacker_corp_id": ents["top_attacker_corp_id"],
            "top_attacker_corp_name": names.get(ents["top_attacker_corp_id"]),
            "top_attacker_corp_kills": ents["top_attacker_corp_kills"],
            "top_attacker_alliance_id": ents["top_attacker_alliance_id"],
            "top_attacker_alliance_name": names.get(ents["top_attacker_alliance_id"]),
            "top_victim_corp_id": ents["top_victim_corp_id"],
            "top_victim_corp_name": names.get(ents["top_victim_corp_id"]),
            "top_victim_corp_kills": ents["top_victim_corp_kills"],
            "top_victim_alliance_id": ents["top_victim_alliance_id"],
            "top_victim_alliance_name": names.get(ents["top_victim_alliance_id"]),
            "top_ships_json": json.dumps(top_ships),
            "killmail_ids_json": json.dumps([km["killmail_id"] for km in c]),
        })
    return out


async def discover_and_persist_battles() -> dict:
    """One discovery pass. Returns run stats."""
    busy = await _find_busy_systems()
    if not busy:
        return {"systems": 0, "battles": 0}

    # Resolve SDE system metadata. wormholeClassID in the SDE is mostly stored
    # at the constellation/region level, not per-system — so use the lookup
    # helper which walks system → constellation → region.
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(
                SDESystem.system_id,
                SDESystem.system_name,
                SDESystem.security,
            ).where(SDESystem.system_id.in_(busy))
        )
        sys_rows = rows.all()

        sys_meta: dict[int, dict] = {}
        for sid, name, sec in sys_rows:
            wc = await get_system_wh_class(db, sid)
            wh_label = wh_class_label(wc)
            sys_meta[sid] = {
                "system_name": name,
                "security": sec,
                "group_label": wh_label or sec_band(sec),
                "band": "w-space" if wh_label else sec_band(sec),
            }

    our_ids = await get_our_char_ids()
    hydration_budget = [MAX_HYDRATIONS_PER_RUN]

    battles_out: list[dict] = []
    async with httpx.AsyncClient() as http:
        for sid in busy:
            kills = await _fetch_system_kills(http, sid)
            if not kills:
                continue
            meta = sys_meta.get(sid, {})
            clusters = await _cluster_system(sid, kills, meta, hydration_budget, our_ids)
            battles_out.extend(clusters)
            if hydration_budget[0] <= 0:
                log.info("recent_battles: hydration budget exhausted after %d systems", len(battles_out))
                break

    if not battles_out:
        return {"systems": len(busy), "battles": 0}

    async with AsyncSessionLocal() as db:
        inserted = 0
        for b in battles_out:
            stmt = sqlite_insert(DetectedBattle).values(**b)
            stmt = stmt.on_conflict_do_update(
                index_elements=["system_id", "start_time"],
                set_={
                    "end_time": stmt.excluded.end_time,
                    "duration_minutes": stmt.excluded.duration_minutes,
                    "kill_count": stmt.excluded.kill_count,
                    "pilots_involved": stmt.excluded.pilots_involved,
                    "total_isk": stmt.excluded.total_isk,
                    "top_attacker_corp_id": stmt.excluded.top_attacker_corp_id,
                    "top_attacker_corp_name": stmt.excluded.top_attacker_corp_name,
                    "top_attacker_corp_kills": stmt.excluded.top_attacker_corp_kills,
                    "top_attacker_alliance_id": stmt.excluded.top_attacker_alliance_id,
                    "top_attacker_alliance_name": stmt.excluded.top_attacker_alliance_name,
                    "top_victim_corp_id": stmt.excluded.top_victim_corp_id,
                    "top_victim_corp_name": stmt.excluded.top_victim_corp_name,
                    "top_victim_corp_kills": stmt.excluded.top_victim_corp_kills,
                    "top_victim_alliance_id": stmt.excluded.top_victim_alliance_id,
                    "top_victim_alliance_name": stmt.excluded.top_victim_alliance_name,
                    "top_ships_json": stmt.excluded.top_ships_json,
                    "killmail_ids_json": stmt.excluded.killmail_ids_json,
                },
            )
            try:
                await db.execute(stmt)
                inserted += 1
            except Exception as e:
                log.debug("recent_battles: upsert skipped: %s", e)
        await db.commit()

    log.info(
        "recent_battles: systems=%d battles=%d budget_left=%d",
        len(busy), len(battles_out), hydration_budget[0],
    )
    return {"systems": len(busy), "battles": len(battles_out), "budget_left": hydration_budget[0]}


# ── Read-side for the dashboard widget ─────────────────────────────────────

async def query_battles_window(days: int = 7, per_group: int = BATTLES_PER_GROUP) -> dict:
    """Return a dict keyed by WH class label / sec band → list of battle dicts.
    Pulls strictly from detected_battles; no computation on page render.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    out: dict[str, list[dict]] = defaultdict(list)
    async with AsyncSessionLocal() as db:
        q = (
            select(
                DetectedBattle.system_id,
                DetectedBattle.system_name,
                DetectedBattle.security,
                DetectedBattle.group_label,
                DetectedBattle.band,
                DetectedBattle.start_time,
                DetectedBattle.end_time,
                DetectedBattle.duration_minutes,
                DetectedBattle.kill_count,
                DetectedBattle.pilots_involved,
                DetectedBattle.total_isk,
                DetectedBattle.top_ships_json,
                DetectedBattle.top_attacker_corp_name,
                DetectedBattle.top_attacker_alliance_name,
                DetectedBattle.top_victim_corp_name,
                DetectedBattle.top_victim_alliance_name,
            )
            .where(DetectedBattle.start_time >= cutoff)
            .order_by(DetectedBattle.kill_count.desc())
        )
        for row in (await db.execute(q)).all():
            group = row[3]
            if len(out[group]) >= per_group:
                continue
            out[group].append({
                "system_id": row[0],
                "system_name": row[1],
                "security": row[2],
                "group_label": row[3],
                "band": row[4],
                "start_time": row[5],
                "end_time": row[6],
                "duration_minutes": row[7],
                "kill_count": row[8],
                "pilots_involved": row[9],
                "total_isk": row[10],
                "top_ships": json.loads(row[11] or "[]"),
                "attacker_label": row[14] or row[13],  # alliance over corp
                "victim_label": row[16] or row[15],
            })
    return dict(out)


BIG_BATTLE_KSPACE_KILLS = 25
BIG_BATTLE_KSPACE_PILOTS = 25
BIG_BATTLE_WSPACE_KILLS = 15
BIG_BATTLE_WSPACE_PILOTS = 15


BANNER_CATEGORIES = ["Nullsec", "Lowsec", "w-space"]
BANNER_MAX = 3


async def active_big_battles() -> list[dict]:
    """Return up to 3 active big battles (ended <30 min ago), balanced across
    Nullsec / Lowsec / w-space. Picks the top battle per category first, then
    fills remaining slots from the largest leftovers (any of the three bands).
    Highsec is excluded — fleet brawls there are rare/CONCORD-shaped."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
    from sqlalchemy import or_, and_
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                DetectedBattle.system_id,
                DetectedBattle.system_name,
                DetectedBattle.group_label,
                DetectedBattle.band,
                DetectedBattle.start_time,
                DetectedBattle.end_time,
                DetectedBattle.kill_count,
                DetectedBattle.pilots_involved,
                DetectedBattle.total_isk,
                DetectedBattle.top_attacker_corp_name,
                DetectedBattle.top_attacker_alliance_name,
                DetectedBattle.top_victim_corp_name,
                DetectedBattle.top_victim_alliance_name,
            )
            .where(DetectedBattle.end_time >= cutoff)
            .where(DetectedBattle.band.in_(BANNER_CATEGORIES))
            .where(or_(
                and_(
                    DetectedBattle.band == "w-space",
                    DetectedBattle.kill_count >= BIG_BATTLE_WSPACE_KILLS,
                    DetectedBattle.pilots_involved >= BIG_BATTLE_WSPACE_PILOTS,
                ),
                and_(
                    DetectedBattle.band != "w-space",
                    DetectedBattle.kill_count >= BIG_BATTLE_KSPACE_KILLS,
                    DetectedBattle.pilots_involved >= BIG_BATTLE_KSPACE_PILOTS,
                ),
            ))
            .order_by(DetectedBattle.kill_count.desc())
            .limit(BANNER_MAX * len(BANNER_CATEGORIES))
        )).all()

    def _row_to_dict(r) -> dict:
        return {
            "system_id": r[0],
            "system_name": r[1],
            "group_label": r[2],
            "band": r[3],
            "start_time": r[4],
            "end_time": r[5],
            "kill_count": r[6],
            "pilots_involved": r[7],
            "total_isk": r[8],
            "attacker_label": r[10] or r[9],
            "victim_label": r[12] or r[11],
        }

    by_cat: dict[str, list] = {c: [] for c in BANNER_CATEGORIES}
    for r in rows:
        band = r[3]
        if band in by_cat:
            by_cat[band].append(r)

    selected: list = []
    used_keys: set = set()
    # Pass 1: top from each category if present
    for cat in BANNER_CATEGORIES:
        if by_cat[cat]:
            r = by_cat[cat][0]
            selected.append(r)
            used_keys.add((r[0], r[4]))
            if len(selected) >= BANNER_MAX:
                break
    # Pass 2: fill remaining slots from leftover rows by kill_count desc
    if len(selected) < BANNER_MAX:
        leftovers = [r for r in rows if (r[0], r[4]) not in used_keys]
        for r in leftovers:
            selected.append(r)
            if len(selected) >= BANNER_MAX:
                break

    return [_row_to_dict(r) for r in selected]
