"""Public ambient-background data: recent kill locations for login-page blips.

Intentionally unauthenticated: returns only solar system IDs of recent kills
(public data — the same kills are on zKillboard). No names, values, or IDs
beyond the system. Read-only.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.models import AsyncSessionLocal, Killmail

logger = logging.getLogger(__name__)
router = APIRouter()

_WINDOW_S = 120
_LIMIT = 50


async def _recent_kill_systems(session, window_s: int = _WINDOW_S) -> list[int]:
    cutoff = datetime.utcnow() - timedelta(seconds=window_s)
    rows = await session.execute(
        select(Killmail.solar_system_id)
        .where(Killmail.killmail_time >= cutoff)
        .group_by(Killmail.solar_system_id)
        .limit(_LIMIT)
    )
    return [r[0] for r in rows.all()]


@router.get("/api/ambient/kills")
async def ambient_kills():
    try:
        async with AsyncSessionLocal() as session:
            systems = await _recent_kill_systems(session)
    except Exception:  # this endpoint must never 500 the login/landing path
        logger.exception("ambient kills query failed")
        systems = []
    return JSONResponse(
        [{"system_id": s} for s in systems],
        headers={"Cache-Control": "public, max-age=15"},
    )
