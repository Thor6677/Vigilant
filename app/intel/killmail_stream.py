"""Real-time killmail stream consumer (killmail.stream /poll endpoint).

Complements the 15-minute zKB poller in recent_battles.py. Where the poller
is reflective ("what happened over the last 7 days"), this is live: detects
active fleet battles within seconds so the big-battle banner fires while
fights are still on-grid.

Design:
- Persistent asyncio task, long-polls /poll/{queueID} in a loop (queueID is
  stable so killmail.stream replays missed events within its 24h window
  after a restart).
- Each event is an ESI-shaped killmail with killmail_time / victim / attackers
  already bundled — no ESI round-trip needed.
- Per-system 30-min sliding window in memory. When a cluster meets the same
  pilot thresholds as _cluster_system in recent_battles.py, a DetectedBattle
  row is upserted. The banner reads DetectedBattle; the 7-day panel reads
  DetectedBattle; nothing downstream needs to change.
- DB writes are rate-limited to once per 30s per system so a 100-kill minute
  in a big timer doesn't write 100x.
- Runs in parallel with the 15-min poller as belt-and-suspenders: if the
  stream drops, the poller still fills the 7-day history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import (
    AsyncSessionLocal,
    Character,
    DetectedBattle,
    KillAlertEvent,
    UserHunterWatch,
    UserSystemWatch,
)
from app.db.sde_models import SDESystem
from app.intel.killmail_store import store_killmail, get_our_char_ids
from app.sde.lookup import get_system_wh_class
from app.intel.recent_battles import (
    BATTLE_GAP_SECONDS,
    BATTLE_MIN_KILLS,
    MIN_PILOTS_KSPACE,
    MIN_PILOTS_WSPACE,
    sec_band,
    wh_class_label,
)

log = logging.getLogger(__name__)

STREAM_POLL_URL = "https://killmail.stream/poll/{queue}"
# Stable queueID so killmail.stream replays missed events after a restart.
# Change this (or set the env var) if you want a fresh 24h window.
QUEUE_ID_DEFAULT = "vigilant-prod-v1"
# Cap memory: drop least-recently-touched system windows beyond this.
MAX_TRACKED_SYSTEMS = 200
# Minimum gap between DB writes per system (seconds).
DB_WRITE_INTERVAL = 30
# Log a heartbeat on empty polls every N seconds so we can tell the stream is alive.
HEARTBEAT_INTERVAL = 300

# In-memory state ----------------------------------------------------------
_sliding_window: dict[int, list[dict]] = defaultdict(list)
_sys_meta_cache: dict[int, dict | None] = {}
_last_db_flush: dict[int, datetime] = {}

# Flat 1-hour kill buffer feeding the gate-camp finder + route threat score.
# Compact shape (no items[], small per-kill footprint) — targets ~40 kills/min
# × 60 min ≈ 2400 entries, ~1-2 MB.
RECENT_KILLS_WINDOW_SECONDS = 3600
_recent_kills: list[dict] = []


def get_recent_kills(
    window_seconds: int = RECENT_KILLS_WINDOW_SECONDS,
    systems: set[int] | None = None,
) -> list[dict]:
    """Snapshot of recent kills from the stream's rolling buffer. Optionally
    filtered to a set of system_ids. Returns compact dicts matching the
    ESI killmail shape that analyze_kills() expects (victim, attackers, zkb,
    solar_system_id, killmail_time) minus heavy fields like items[]."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=window_seconds)
    out = []
    for rec in _recent_kills:
        if rec["_kt"] < cutoff:
            continue
        if systems is not None and rec["solar_system_id"] not in systems:
            continue
        out.append(rec)
    return out


def _append_recent_kill(ev: dict, kt: datetime) -> None:
    """Push a compact record into the rolling buffer + prune the front by time."""
    victim = ev.get("victim") or {}
    attackers = ev.get("attackers") or []
    zkb = ev.get("zkb") or {}
    rec = {
        "_kt": kt,  # internal tz-naive datetime for pruning/filtering
        "killmail_id": ev.get("killmail_id"),
        "killmail_time": ev.get("killmail_time"),  # keep ISO string for time_ago()
        "solar_system_id": ev.get("solar_system_id"),
        "victim": {
            "character_id": victim.get("character_id"),
            "corporation_id": victim.get("corporation_id"),
            "alliance_id": victim.get("alliance_id"),
            "ship_type_id": victim.get("ship_type_id"),
        },
        "attackers": [
            {
                "character_id": a.get("character_id"),
                "corporation_id": a.get("corporation_id"),
                "alliance_id": a.get("alliance_id"),
                "ship_type_id": a.get("ship_type_id"),
                "weapon_type_id": a.get("weapon_type_id"),
                "final_blow": a.get("final_blow"),
            }
            for a in attackers
        ],
        "zkb": {
            "totalValue": zkb.get("totalValue"),
            "locationID": zkb.get("locationID"),
            "npc": zkb.get("npc"),
            "solo": zkb.get("solo"),
            "labels": zkb.get("labels"),
            "hash": zkb.get("hash"),
        },
    }
    _recent_kills.append(rec)
    # Prune old entries from the front. Bounded scan — most pruning happens
    # at the start of the list since appends are chronological.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=RECENT_KILLS_WINDOW_SECONDS)
    while _recent_kills and _recent_kills[0]["_kt"] < cutoff:
        _recent_kills.pop(0)


async def _resolve_sys_meta(system_id: int) -> dict | None:
    """Cache system metadata (name, security, band). Negative cache miss too
    since not every ID from the firehose is a known SDE system (abyssal,
    test, etc.) — avoids hot-path lookups."""
    if system_id in _sys_meta_cache:
        return _sys_meta_cache[system_id]
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(SDESystem.system_name, SDESystem.security).where(
                    SDESystem.system_id == system_id
                )
            )
        ).first()
        if not row:
            _sys_meta_cache[system_id] = None
            return None
        # wormholeClassID is mostly stored at constellation/region level —
        # use the resolver that walks system → constellation → region.
        wc = await get_system_wh_class(db, system_id)
        wh_label = wh_class_label(wc)
        meta = {
            "system_name": row[0],
            "security": row[1],
            "group_label": wh_label or sec_band(row[1]),
            "band": "w-space" if wh_label else sec_band(row[1]),
        }
        _sys_meta_cache[system_id] = meta
        return meta


async def _maybe_upsert_battle(system_id: int, kills: list[dict]) -> None:
    """If the cluster meets band-specific pilot thresholds, upsert a
    DetectedBattle row. Schema matches _cluster_system in recent_battles.py
    so the dashboard widget reads both poller + stream rows identically."""
    if len(kills) < BATTLE_MIN_KILLS:
        return
    meta = await _resolve_sys_meta(system_id)
    if not meta:
        return
    min_pilots = MIN_PILOTS_WSPACE if meta["band"] == "w-space" else MIN_PILOTS_KSPACE

    pilots: set[int] = set()
    ship_counter: dict[int, int] = {}
    total_isk = 0.0
    attacker_pilots: dict[int, tuple[int | None, int | None]] = {}
    victim_pilots: dict[int, tuple[int | None, int | None]] = {}
    for k in kills:
        pilots |= k["attacker_ids"]
        if k.get("victim_id"):
            pilots.add(k["victim_id"])
            victim_pilots.setdefault(
                k["victim_id"],
                (k.get("victim_corp_id"), k.get("victim_alliance_id")),
            )
        for cid, corp, alli in k.get("attacker_orgs") or []:
            if cid:
                attacker_pilots.setdefault(cid, (corp, alli))
        sid = k.get("victim_ship_id")
        if sid:
            ship_counter[sid] = ship_counter.get(sid, 0) + 1
        total_isk += k.get("value") or 0

    if len(pilots) < min_pilots:
        return

    start = min(k["kill_time"] for k in kills)
    end = max(k["kill_time"] for k in kills)
    duration = max(1, int((end - start).total_seconds() / 60))
    top_ships = sorted(ship_counter.items(), key=lambda x: -x[1])[:5]
    from app.intel.recent_battles import pick_top_entities, resolve_entity_names
    ents = pick_top_entities(attacker_pilots, victim_pilots)
    names = await resolve_entity_names([
        i for i in (
            ents["top_attacker_corp_id"], ents["top_attacker_alliance_id"],
            ents["top_victim_corp_id"], ents["top_victim_alliance_id"],
        ) if i
    ])

    row = {
        "system_id": system_id,
        "system_name": meta["system_name"],
        "security": meta["security"],
        "group_label": meta["group_label"],
        "band": meta["band"],
        "start_time": start,
        "end_time": end,
        "duration_minutes": duration,
        "kill_count": len(kills),
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
        "top_ships_json": json.dumps([{"id": sid, "count": n} for sid, n in top_ships]),
        "killmail_ids_json": json.dumps([k["kill_id"] for k in kills]),
    }

    # The sliding window's start_time drifts forward as old kills age out,
    # which would miss the (system_id, start_time) conflict index and create
    # a new row for the same ongoing battle. Reuse an existing row's start_time
    # if one is active (last seen within BATTLE_GAP_SECONDS) so we update in
    # place.
    gap = timedelta(seconds=BATTLE_GAP_SECONDS)
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(
                select(DetectedBattle.start_time)
                .where(
                    DetectedBattle.system_id == system_id,
                    DetectedBattle.end_time >= start - gap,
                )
                .order_by(DetectedBattle.end_time.desc())
                .limit(1)
            )
        ).first()
        if existing:
            row["start_time"] = existing[0]
            # Keep the widest time span: earliest start wins so duration stays coherent
            row["duration_minutes"] = max(
                1, int((end - existing[0]).total_seconds() / 60)
            )

        stmt = sqlite_insert(DetectedBattle).values(**row)
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
        await db.execute(stmt)
        await db.commit()


def _cap_tracked_systems() -> None:
    """Evict least-recently-flushed systems when over the cap."""
    if len(_sliding_window) <= MAX_TRACKED_SYSTEMS:
        return
    ranked = sorted(
        _sliding_window.keys(),
        key=lambda s: _last_db_flush.get(s, datetime.min),
    )
    for sid in ranked[: len(_sliding_window) - MAX_TRACKED_SYSTEMS]:
        _sliding_window.pop(sid, None)
        _last_db_flush.pop(sid, None)


async def _fire_watch_alerts(
    ev: dict,
    kill_time: datetime,
    sys_id: int,
    attacker_char_ids: set[int],
    attacker_corp_ids: set[int],
    attacker_alliance_ids: set[int],
) -> None:
    """Match this kill against user_system_watches + user_hunter_watches and
    emit KillAlertEvent rows. Pushes a notification to each user's poll queue
    so the UI picks it up in seconds. Dedupes via (user_id, killmail_id, kind)
    unique constraint — safe against killmail.stream's 24h replay."""
    kid = ev["killmail_id"]
    sys_name: str | None = None
    meta = _sys_meta_cache.get(sys_id) if sys_id in _sys_meta_cache else None
    if meta:
        sys_name = meta.get("system_name")
    # Lazy resolve system name on miss so the alert payload is human-readable
    if sys_name is None:
        meta = await _resolve_sys_meta(sys_id)
        if meta:
            sys_name = meta.get("system_name")

    # Collect matched (user_id, kind, matched_entity_id, matched_label) tuples.
    matches: list[tuple[int, str, int | None, str | None]] = []

    async with AsyncSessionLocal() as db:
        # System watch: any user watching this system_id
        sys_rows = (
            await db.execute(
                select(
                    UserSystemWatch.user_id,
                    UserSystemWatch.label,
                ).where(UserSystemWatch.system_id == sys_id)
            )
        ).all()
        for user_id, label in sys_rows:
            matches.append((user_id, "system_watch", None, label))

        # Hunter watch: any user watching any of this kill's attacker entities
        hunter_conds = []
        if attacker_char_ids:
            hunter_conds.append(
                and_(
                    UserHunterWatch.kind == "character",
                    UserHunterWatch.entity_id.in_(attacker_char_ids),
                )
            )
        if attacker_corp_ids:
            hunter_conds.append(
                and_(
                    UserHunterWatch.kind == "corporation",
                    UserHunterWatch.entity_id.in_(attacker_corp_ids),
                )
            )
        if attacker_alliance_ids:
            hunter_conds.append(
                and_(
                    UserHunterWatch.kind == "alliance",
                    UserHunterWatch.entity_id.in_(attacker_alliance_ids),
                )
            )
        if hunter_conds:
            hrows = (
                await db.execute(
                    select(
                        UserHunterWatch.user_id,
                        UserHunterWatch.kind,
                        UserHunterWatch.entity_id,
                        UserHunterWatch.label,
                    ).where(or_(*hunter_conds))
                )
            ).all()
            for user_id, kind, entity_id, label in hrows:
                matches.append((user_id, "hunter_watch", entity_id, label))

        if not matches:
            return

        # Persist alert events (dedup on unique constraint) and build payload list
        triggered_at = datetime.now(timezone.utc).replace(tzinfo=None)
        for user_id, kind, matched_entity_id, matched_label in matches:
            stmt = sqlite_insert(KillAlertEvent).values(
                user_id=user_id,
                kind=kind,
                killmail_id=kid,
                system_id=sys_id,
                matched_entity_id=matched_entity_id,
                matched_label=matched_label,
                triggered_at=triggered_at,
            )
            # Unique (user_id, killmail_id, kind) — existing rows silently skip
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["user_id", "killmail_id", "kind"]
            )
            try:
                await db.execute(stmt)
            except Exception as e:
                log.debug("killmail_stream: alert insert skipped: %s", e)
        await db.commit()

    # Push to the in-memory notification queue used by /notifications/poll.
    # Import inside the function to avoid circular imports.
    try:
        from app.routes.dashboard import _emit_notification
        zkb = ev.get("zkb") or {}
        for user_id, kind, matched_entity_id, matched_label in matches:
            _emit_notification(
                user_id,
                {
                    "type": "kill_alert",
                    "kind": kind,
                    "killmail_id": kid,
                    "system_id": sys_id,
                    "system_name": sys_name,
                    "matched_entity_id": matched_entity_id,
                    "matched_label": matched_label,
                    "total_value": float(zkb.get("totalValue") or 0),
                    "zkb_url": f"https://zkillboard.com/kill/{kid}/",
                },
            )
    except Exception as e:
        log.debug("killmail_stream: emit notification failed: %s", e)


async def _handle_kill(ev: dict, our_ids: set[int]) -> None:
    kid = ev.get("killmail_id")
    sys_id = ev.get("solar_system_id")
    kt_str = ev.get("killmail_time")
    if not (kid and sys_id and kt_str):
        return
    try:
        kt = datetime.fromisoformat(kt_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return

    # Reuse the existing storage layer — killmail.stream payloads are
    # ESI-shaped so store_killmail works as-is. Idempotent on kid.
    zkb = ev.get("zkb") or {}
    try:
        await store_killmail(ev, zkb, our_ids)
    except Exception as e:
        log.debug("killmail_stream: store failed for %s: %s", kid, e)

    # Also stash into the rolling 1h flat buffer for gate-camp / route-threat
    _append_recent_kill(ev, kt)

    victim = ev.get("victim") or {}
    attackers = ev.get("attackers") or []
    attacker_ids = {a.get("character_id") for a in attackers if a.get("character_id")}
    attacker_corp_ids = {a.get("corporation_id") for a in attackers if a.get("corporation_id")}
    attacker_alliance_ids = {a.get("alliance_id") for a in attackers if a.get("alliance_id")}

    # Fan out watch matches. Don't block the cluster/sliding-window update
    # on this — errors here shouldn't kill the consumer loop.
    try:
        await _fire_watch_alerts(
            ev, kt, sys_id, attacker_ids, attacker_corp_ids, attacker_alliance_ids
        )
    except Exception as e:
        log.exception("killmail_stream: watch fan-out failed for %s: %s", kid, e)

    rec = {
        "kill_id": kid,
        "kill_time": kt,
        "victim_id": victim.get("character_id"),
        "victim_corp_id": victim.get("corporation_id"),
        "victim_alliance_id": victim.get("alliance_id"),
        "victim_ship_id": victim.get("ship_type_id"),
        "attacker_ids": attacker_ids,
        "attacker_orgs": [
            (a.get("character_id"), a.get("corporation_id"), a.get("alliance_id"))
            for a in attackers if a.get("character_id")
        ],
        "value": float(zkb.get("totalValue") or 0),
    }

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=BATTLE_GAP_SECONDS)
    window = _sliding_window[sys_id]
    window[:] = [k for k in window if k["kill_time"] >= cutoff]
    window.append(rec)
    _cap_tracked_systems()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = _last_db_flush.get(sys_id)
    if last is None or (now - last).total_seconds() >= DB_WRITE_INTERVAL:
        _last_db_flush[sys_id] = now
        try:
            await _maybe_upsert_battle(sys_id, list(window))
        except Exception as e:
            log.exception("killmail_stream: upsert failed for sys %s: %s", sys_id, e)


async def run_consumer() -> None:
    """Long-poll loop. Exponential backoff on errors; stable queueID for
    24h replay across restarts."""
    queue = os.getenv("KILLMAIL_STREAM_QUEUE_ID", QUEUE_ID_DEFAULT)
    url = STREAM_POLL_URL.format(queue=queue)
    log.info("killmail_stream: consumer starting queue=%s", queue)
    backoff = 1.0
    empty_since = datetime.now(timezone.utc)
    total_events = 0

    timeout = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                our_ids = await get_our_char_ids()
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.warning(
                        "killmail_stream: HTTP %s (%s)", resp.status_code, resp.text[:200]
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1.0

                data = resp.json()
                items = data if isinstance(data, list) else [data]
                items = [x for x in items if x]

                if items:
                    total_events += len(items)
                    empty_since = datetime.now(timezone.utc)
                    for ev in items:
                        try:
                            await _handle_kill(ev, our_ids)
                        except Exception as e:
                            log.exception("killmail_stream: handle error: %s", e)
                else:
                    # Heartbeat log so we can tell the stream is alive during
                    # quiet periods (daybreak, downtime, etc.)
                    silence = (datetime.now(timezone.utc) - empty_since).total_seconds()
                    if silence >= HEARTBEAT_INTERVAL:
                        log.info(
                            "killmail_stream: alive, %ds silent, %d events total",
                            int(silence),
                            total_events,
                        )
                        empty_since = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                log.info("killmail_stream: consumer cancelled")
                raise
            except Exception as e:
                log.warning("killmail_stream: loop error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def run_sweeper() -> None:
    """Periodic cleanup: prune stale sliding windows so abandoned systems
    don't hold memory. Runs every 60s."""
    while True:
        try:
            await asyncio.sleep(60)
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=BATTLE_GAP_SECONDS)
            stale = [
                sid
                for sid, w in list(_sliding_window.items())
                if not w or all(k["kill_time"] < cutoff for k in w)
            ]
            for sid in stale:
                _sliding_window.pop(sid, None)
                _last_db_flush.pop(sid, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("killmail_stream: sweep error: %s", e)
