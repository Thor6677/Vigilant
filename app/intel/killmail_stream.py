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
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import AsyncSessionLocal, DetectedBattle
from app.db.sde_models import SDESystem, SDEWormholeClass
from app.intel.killmail_store import store_killmail, get_our_char_ids
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
        whrow = (
            await db.execute(
                select(SDEWormholeClass.wormhole_class_id).where(
                    SDEWormholeClass.location_id == system_id
                )
            )
        ).first()
        wh_label = wh_class_label(whrow[0]) if whrow else None
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
    victim_corps: dict[int, int] = {}
    ship_counter: dict[int, int] = {}
    total_isk = 0.0
    for k in kills:
        pilots |= k["attacker_ids"]
        if k.get("victim_id"):
            pilots.add(k["victim_id"])
        vc = k.get("victim_corp_id")
        if vc:
            victim_corps[vc] = victim_corps.get(vc, 0) + 1
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
    top_victim_corp_id = 0
    top_victim_corp_kills = 0
    if victim_corps:
        tv = max(victim_corps.items(), key=lambda x: x[1])
        top_victim_corp_id, top_victim_corp_kills = tv[0], tv[1]

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
        "top_attacker_corp_id": None,
        "top_attacker_corp_name": None,
        "top_attacker_corp_kills": 0,
        "top_victim_corp_id": top_victim_corp_id or None,
        "top_victim_corp_name": None,
        "top_victim_corp_kills": top_victim_corp_kills,
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
                "top_victim_corp_id": stmt.excluded.top_victim_corp_id,
                "top_victim_corp_kills": stmt.excluded.top_victim_corp_kills,
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

    victim = ev.get("victim") or {}
    attackers = ev.get("attackers") or []
    attacker_ids = {a.get("character_id") for a in attackers if a.get("character_id")}

    rec = {
        "kill_id": kid,
        "kill_time": kt,
        "victim_id": victim.get("character_id"),
        "victim_corp_id": victim.get("corporation_id"),
        "victim_ship_id": victim.get("ship_type_id"),
        "attacker_ids": attacker_ids,
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
