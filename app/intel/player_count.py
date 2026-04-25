"""ESI /status/ live sampler.

Polled by app/routes/dashboard.py::_background_scheduler every 60s to feed
the dashboard "Activity" overlay and the /tools/activity page. Stores into
player_count_snapshots with source='esi'.

Historical backfill from third-party archives (eve-offline.net /
eve-offline.com) lives in separate modules and writes the same table with
source='eve-offline-net' or 'eve-offline-com'.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import AsyncSessionLocal, PlayerCountSnapshot

log = logging.getLogger(__name__)

ESI_STATUS_URL = "https://esi.evetech.net/latest/status/"
# CCP requires a contactable User-Agent. The contact email is local to this
# helper rather than a project-global default so other call sites aren't
# accidentally re-using a personal address.
ESI_UA = "Vigilant/1.0 (happyfun.fatman@gmail.com)"


async def sample_status_from_esi() -> bool:
    """Fetch /status/ once and persist a PlayerCountSnapshot. Returns True on
    successful insert. False on transport failure / non-200 / VIP-no-players /
    duplicate timestamp."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                ESI_STATUS_URL,
                headers={"User-Agent": ESI_UA, "Accept": "application/json"},
            )
            if r.status_code != 200:
                log.debug("player_count: ESI HTTP %s", r.status_code)
                return False
            data = r.json()
    except Exception as e:
        log.debug("player_count: ESI fetch failed: %s", e)
        return False

    players = data.get("players")
    if players is None:
        return False

    # Parse server_start_time (ISO 8601 with Z) into tz-naive UTC for storage
    sst_raw = data.get("start_time")
    server_start_time = None
    if isinstance(sst_raw, str):
        try:
            server_start_time = datetime.fromisoformat(
                sst_raw.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except (ValueError, AttributeError):
            pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        stmt = sqlite_insert(PlayerCountSnapshot).values(
            recorded_at=now,
            player_count=int(players),
            source="esi",
            granularity="60s",
            server_version=str(data.get("server_version") or "")[:32] or None,
            server_start_time=server_start_time,
            vip_mode=bool(data.get("vip", False)) if "vip" in data else None,
        ).on_conflict_do_nothing(index_elements=["source", "recorded_at"])
        try:
            await db.execute(stmt)
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            log.debug("player_count: insert skipped: %s", e)
            return False
