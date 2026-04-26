"""Natural Anoikis layout from real SDE coordinates.

J-space systems have real 3-D positions in the SDE — CCP placed them
with intentional spatial structure (the C6 stripe, the Drifter complex
cluster, the Drifter shattered group). Projecting those positions to
2-D top-down (XZ) gives the recognisable Anoikis cluster that
anoikis.info-style maps render.

Positions are precomputed offline by `scripts/build_wormhole_positions.py`
and shipped as `app/data/wormhole_positions.json`. This module loads
that file once, joins it against the SDE systems table for metadata
(name, security), and emits region labels at the centroid of each
spatially-distinct group.

Output shape matches the K-space `useMapData` bundle so the frontend
hook is space-agnostic.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models import AsyncSessionLocal
from app.db.sde_models import SDESystem
from app.sde import lookup as sde_lookup

log = logging.getLogger(__name__)

_POSITIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "wormhole_positions.json"

# Big floating labels on the map. C1-C5 all overlap in the bulk so one
# combined label keeps the legend readable. C6, Thera, Drifter Shattered
# and the named Drifter complexes form spatially-distinct clusters that
# get their own labels.
_REGION_LABELS: list[tuple[str, list[int]]] = [
    ("Anoikis (C1–C5)", [1, 2, 3, 4, 5]),
    ("C6",              [6]),
    ("Thera",           [12]),
    ("Drifter Shattered", [13]),
    ("Drifter Complex", [14, 15, 16, 17, 18]),
]


# Per-system pseudo-region label used in hover tooltips, search results
# and the region-grouping mode. Distinct IDs per class so grouping works;
# negative so they don't collide with real SDE region ids.
def _region_for_class(wh_class: int | None) -> tuple[int, str]:
    if wh_class is None:
        return (0, "")
    if 1 <= wh_class <= 6:
        return (-wh_class, f"C{wh_class}")
    if wh_class == 12:
        return (-12, "Thera")
    if wh_class == 13:
        return (-13, "Drifter Shattered")
    if 14 <= wh_class <= 18:
        return (-19, "Drifter Complex")
    return (0, f"C{wh_class}")


_positions_cache: dict[int, dict[str, Any]] | None = None


def _load_positions() -> dict[int, dict[str, Any]]:
    """Load the precomputed XZ projection. Cached after first call."""
    global _positions_cache
    if _positions_cache is not None:
        return _positions_cache
    with open(_POSITIONS_PATH) as f:
        payload = json.load(f)
    _positions_cache = {int(s["id"]): s for s in payload["systems"]}
    log.info("wormhole_layout: loaded %d positions from %s",
             len(_positions_cache), _POSITIONS_PATH.name)
    return _positions_cache


_layout_cache: dict[str, Any] | None = None


async def build_wormhole_layout() -> dict[str, Any]:
    """Build the Anoikis bundle. Idempotent — caches the first result.
    Returns {systems, edges, regions} matching the K-space schema.
    """
    global _layout_cache
    if _layout_cache is not None:
        return _layout_cache

    positions = _load_positions()

    async with AsyncSessionLocal() as db:
        # Warm the wh_class cache; system metadata fall-back uses it for
        # systems missing from the precomputed file (e.g. SDE drift).
        await sde_lookup._ensure_wh_class_cache(db)
        rows = (await db.execute(
            select(
                SDESystem.system_id,
                SDESystem.system_name,
                SDESystem.security,
                SDESystem.constellation_id,
                SDESystem.region_id,
            ).where(SDESystem.system_id >= 31000000)
             .where(SDESystem.system_id < 32000000)
        )).all()

    wh_cache = sde_lookup._wh_class_cache or {}

    systems: list[dict] = []
    skipped = 0
    by_class_pos: dict[int, list[tuple[float, float]]] = defaultdict(list)

    for sid, name, sec, con_id, reg_id in rows:
        pos = positions.get(int(sid))
        if pos is None:
            # SDE has a system the precomputed file doesn't; skip
            # rather than guess a position.
            skipped += 1
            continue
        wh_class = pos.get("wh_class")
        if wh_class is None:
            # Cascade lookup as a safety net (matches the build script).
            wh_class = (
                wh_cache.get(sid)
                or (wh_cache.get(con_id) if con_id else None)
                or (wh_cache.get(reg_id) if reg_id else None)
            )
        x = float(pos["x"])
        y = float(pos["y"])
        if wh_class is not None:
            by_class_pos[int(wh_class)].append((x, y))
        pseudo_reg_id, pseudo_reg_name = _region_for_class(
            int(wh_class) if wh_class is not None else None
        )
        systems.append({
            "id": int(sid),
            "name": name,
            "x": x,
            "y": y,
            "sec": float(sec) if sec is not None else 0.0,
            "conId": int(con_id) if con_id else 0,
            "conName": "",
            "regId": pseudo_reg_id,
            "regName": pseudo_reg_name,
            "hasStation": False,
            "stns": 0,
            "svcs": [],
            "x3": 0.0,
            "y3": 0.0,
            "z3": 0.0,
            "whClass": int(wh_class) if wh_class is not None else None,
        })

    # Region labels live at the centroid of the named class members.
    regions: list[dict] = []
    for idx, (label, classes) in enumerate(_REGION_LABELS):
        pts: list[tuple[float, float]] = []
        for c in classes:
            pts.extend(by_class_pos.get(c, []))
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        regions.append({
            "id": -(idx + 1),     # negative so it can't collide with SDE region ids
            "name": label,
            "cx": round(cx, 1),
            "cy": round(cy, 1),
        })

    log.info(
        "wormhole layout: placed %d systems (skipped %d), %d region labels",
        len(systems), skipped, len(regions),
    )
    _layout_cache = {"systems": systems, "edges": [], "regions": regions}
    return _layout_cache
