"""Public ambient-background data: recent kill locations for login-page blips.

Intentionally unauthenticated: returns only solar system IDs of recent kills
(public data — the same kills are on zKillboard). No names, values, or IDs
beyond the system. Read-only.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.models import AsyncSessionLocal, Killmail

logger = logging.getLogger(__name__)
router = APIRouter()

_WINDOW_S = 120
_LIMIT = 50

# Module-level 15s in-process cache. This is the real DoS guard for this
# public, unauthenticated endpoint — edge nginx has no microcache here.
_cache: dict = {"t": 0.0, "payload": None}
_CACHE_TTL_S = 15.0


async def _recent_kill_systems(session, window_s: int = _WINDOW_S) -> list[int]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=window_s)
    # Order by killmail_time and limit BEFORE distincting so SQLite can
    # satisfy this with an index scan on ix_killmails_killmail_time instead
    # of a full covering-index scan (this table is 192 GB).
    inner = (
        select(Killmail.solar_system_id)
        .where(Killmail.killmail_time >= cutoff)
        .order_by(Killmail.killmail_time.desc())
        .limit(500)
        .subquery()
    )
    rows = await session.execute(
        select(inner.c.solar_system_id).distinct().limit(_LIMIT)
    )
    return [r[0] for r in rows.all()]


@router.get("/api/ambient/kills")
async def ambient_kills():
    now = time.monotonic()
    if _cache["payload"] is not None and now - _cache["t"] < _CACHE_TTL_S:
        return JSONResponse(
            _cache["payload"],
            headers={"Cache-Control": "public, max-age=15"},
        )

    try:
        async with AsyncSessionLocal() as session:
            systems = await _recent_kill_systems(session)
    except Exception:  # this endpoint must never 500 the login/landing path
        logger.exception("ambient kills query failed")
        return JSONResponse(
            [],
            headers={"Cache-Control": "public, max-age=15"},
        )

    payload = [{"system_id": s} for s in systems]
    _cache["t"] = now
    _cache["payload"] = payload
    return JSONResponse(
        payload,
        headers={"Cache-Control": "public, max-age=15"},
    )
