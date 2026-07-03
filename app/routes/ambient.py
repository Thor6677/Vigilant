"""Public ambient-background data: recent kill locations for login-page blips.

Intentionally unauthenticated: returns only solar system IDs of recent kills
(public data — the same kills are on zKillboard). No names, values, or IDs
beyond the system. Read-only.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
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

# Runtime/bundled system-data locations. Mirrors app/routes/starmap.py's
# _KSPACE_RUNTIME_DIR / FRONTEND_DIST resolution (kspace-data route,
# ~line 372) — replicated here rather than imported to avoid a
# cross-module dependency between starmap.py and ambient.py.
_SYSTEMS_RUNTIME_PATH = Path("/data/map/systems.json")
_SYSTEMS_BUNDLED_PATH = (
    Path(__file__).resolve().parent.parent.parent / "frontend" / "dist" / "data" / "systems.json"
)

# Slimmed + serialized once per process, keyed by (path, mtime). The
# ambient module only ever needs id/name/x3/y3/z3 — the full systems.json
# is ~1.27MB with sec/conId/conName/regId/regName/hasStation/stns/svcs
# that the login-page renderer never touches.
_systems_cache: dict = {"key": None, "body": None}


def _slim_systems(raw: list) -> list:
    """Project each system dict down to the fields normalizeSystems() reads."""
    return [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "x3": s.get("x3"),
            "y3": s.get("y3"),
            "z3": s.get("z3"),
        }
        for s in raw
    ]


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


@router.get("/api/ambient/systems.json")
async def ambient_systems():
    """Slim, long-cacheable system-position feed for the ambient module.

    Star positions effectively never change, so this is safe to cache
    hard client-side (max-age=86400) — unlike the star map's full
    /api/map/kspace-data/systems.json (~1.27MB, no-store, meant for a
    force-update to take effect immediately).
    """
    try:
        path = _SYSTEMS_RUNTIME_PATH if _SYSTEMS_RUNTIME_PATH.is_file() else _SYSTEMS_BUNDLED_PATH
        mtime = path.stat().st_mtime
        key = (str(path), mtime)

        if _systems_cache["key"] == key:
            body = _systems_cache["body"]
        else:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            slim = _slim_systems(raw)
            body = json.dumps(slim, separators=(",", ":"))
            _systems_cache["key"] = key
            _systems_cache["body"] = body

        return Response(
            content=body,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception:  # this endpoint must never 500 the login/landing path
        logger.exception("ambient systems load failed")
        return JSONResponse([], headers={"Cache-Control": "public, max-age=60"})
