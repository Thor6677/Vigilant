"""Synthetic 2D layout for wormhole space.

J-systems have no canonical 2D positions in the SDE — wormhole connections
rotate constantly so there's no static graph. This module groups the ~3000
J-systems by wormhole class and lays them out as a hex-flower:

   - Center cluster: Thera + Drifter complexes + small shattered systems
     (all the rare specials packed tightly at the origin).
   - Outer hex ring: C1–C6, each its own circular cluster at one of the
     six hexagonal points around the center.
   - Outliers: the bigger shattered groups (class 19, 20–23) get their
     own small clusters tucked between the hex ring and the center.

Within each cluster, systems are distributed by Vogel's phyllotaxis
("sunflower spiral") — no overlap, organic look, no rectangular grid
artifacts.

Layout is deterministic (sorted by system_id) and built once at first
hit, cached in memory and served by /api/map/wormholes-data/*.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import select

from app.db.models import AsyncSessionLocal
from app.db.sde_models import SDESystem
from app.sde import lookup as sde_lookup

log = logging.getLogger(__name__)

# Layout origin in world coordinates. Matches CANVAS_SIZE/2 in the
# frontend (constants.ts CANVAS_SIZE=10000) so the viewport's default
# moveCenter(CANVAS_SIZE/2, CANVAS_SIZE/2) lands on us.
_ORIGIN_X = 5000.0
_ORIGIN_Y = 5000.0

# Hex-ring radius (distance from origin to each C1-C6 cluster center).
_HEX_RING_RADIUS = 1400.0

# Maximum cluster radius (per-class). Picked so clusters don't overlap on
# the hex ring (chord between adjacent hex points = ring radius = 1400).
_CLUSTER_RADIUS_MAX = 600.0

# Center cluster radius for specials (Thera + drifter complexes).
_CENTER_CLUSTER_RADIUS = 280.0


# Bucket → (display_label, optional fixed center offset relative to origin).
# C1–C6 are placed on the hex ring (None means "compute by hex angle").
# Specials cluster around the center.
_BUCKETS: dict[str, tuple[str, tuple[float, float] | None]] = {
    "c1":         ("C1",                 None),  # hex ring slot 0 (north)
    "c2":         ("C2",                 None),  # slot 1 (NE)
    "c3":         ("C3",                 None),  # slot 2 (SE)
    "c4":         ("C4",                 None),  # slot 3 (south)
    "c5":         ("C5",                 None),  # slot 4 (SW)
    "c6":         ("C6",                 None),  # slot 5 (NW)
    "specials":   ("Thera / Drifter",    (0.0, 0.0)),
    "shattered":  ("Shattered",          (-700.0, 0.0)),
    "triglavian": ("Triglavian",         (700.0, 0.0)),
}

# Hex-ring slot order: 0=N, 1=NE, 2=SE, 3=S, 4=SW, 5=NW
_HEX_ORDER = ["c1", "c2", "c3", "c4", "c5", "c6"]


def _classify(wh_class: int | None) -> str | None:
    if wh_class is None:
        return None
    if 1 <= wh_class <= 6:
        return f"c{wh_class}"
    if wh_class == 12:
        return "specials"  # Thera
    if wh_class in (13, 14, 15, 16, 17, 18):
        return "specials"  # Drifter complexes + drifter shattered
    if wh_class == 19:
        return "shattered"
    if wh_class in (20, 21, 22, 23, 25):
        return "triglavian"
    return None


def _hex_ring_position(slot: int) -> tuple[float, float]:
    # slot 0 = north (-y), then clockwise. Pixi uses y-down.
    angle = -math.pi / 2 + slot * (math.pi / 3)
    return (
        _HEX_RING_RADIUS * math.cos(angle),
        _HEX_RING_RADIUS * math.sin(angle),
    )


def _phyllotaxis(n: int, radius: float) -> list[tuple[float, float]]:
    """Vogel's sunflower spiral. Distributes n points evenly within a
    disk of given radius. No overlaps, no obvious rows, looks organic.
    """
    if n <= 0:
        return []
    if n == 1:
        return [(0.0, 0.0)]
    golden_angle = math.pi * (3 - math.sqrt(5))  # ~137.5°
    out: list[tuple[float, float]] = []
    for i in range(n):
        # Vogel's formula. The +0.5 keeps i=0 off the exact origin.
        r = radius * math.sqrt((i + 0.5) / n)
        theta = i * golden_angle
        out.append((r * math.cos(theta), r * math.sin(theta)))
    return out


_layout_cache: dict[str, Any] | None = None


async def build_wormhole_layout() -> dict[str, Any]:
    """Compute the wormhole-space layout. Idempotent — caches the first
    result. Returns {systems, edges, regions} matching the k-space bundle
    shape so the frontend can use the same useMapData hook.
    """
    global _layout_cache
    if _layout_cache is not None:
        return _layout_cache

    async with AsyncSessionLocal() as db:
        await sde_lookup._ensure_wh_class_cache(db)
        rows = (await db.execute(
            select(
                SDESystem.system_id,
                SDESystem.system_name,
                SDESystem.security,
                SDESystem.constellation_id,
                SDESystem.region_id,
            ).where(SDESystem.system_id >= 31000000)
        )).all()

    cache = sde_lookup._wh_class_cache or {}

    # Group J-systems into layout buckets.
    grouped: dict[str, list[tuple[int, str, float | None, int | None, int | None, int | None]]] = {
        k: [] for k in _BUCKETS
    }
    skipped = 0
    for sid, name, sec, con_id, reg_id in rows:
        wh = cache.get(sid) or (cache.get(con_id) if con_id else None) or (cache.get(reg_id) if reg_id else None)
        bucket = _classify(wh)
        if bucket is None:
            skipped += 1
            continue
        grouped[bucket].append((sid, name, sec, con_id, reg_id, wh))

    # Stable sort within each bucket so positions are deterministic.
    for bucket in grouped:
        grouped[bucket].sort(key=lambda r: r[0])

    systems: list[dict] = []
    regions: list[dict] = []

    for bucket, members in grouped.items():
        label, fixed_offset = _BUCKETS[bucket]
        if fixed_offset is not None:
            cx, cy = _ORIGIN_X + fixed_offset[0], _ORIGIN_Y + fixed_offset[1]
            # Shrink secondary cluster radius to ~280 since these are
            # smaller, dense groups crowded near the center.
            if bucket == "specials":
                cluster_r = _CENTER_CLUSTER_RADIUS
            else:
                # Scale by sqrt of count, capped to a sensible max.
                cluster_r = min(_CLUSTER_RADIUS_MAX * 0.7, 80.0 + 18.0 * math.sqrt(len(members)))
        else:
            slot = _HEX_ORDER.index(bucket)
            ox, oy = _hex_ring_position(slot)
            cx, cy = _ORIGIN_X + ox, _ORIGIN_Y + oy
            # Hex-ring clusters: scale by count so big classes get bigger
            # circles, but cap at the layout-safe max.
            cluster_r = min(_CLUSTER_RADIUS_MAX, 80.0 + 22.0 * math.sqrt(len(members)))

        positions = _phyllotaxis(len(members), cluster_r)
        for (sid, name, sec, con_id, reg_id, wh), (px, py) in zip(members, positions):
            systems.append({
                "id": int(sid),
                "name": name,
                "x": cx + px,
                "y": cy + py,
                "sec": float(sec) if sec is not None else 0.0,
                "conId": int(con_id) if con_id else 0,
                "conName": "",
                "regId": int(reg_id) if reg_id else 0,
                "regName": label,
                "hasStation": False,
                "stns": 0,
                "svcs": [],
                "x3": 0.0,
                "y3": 0.0,
                "z3": 0.0,
                "whClass": int(wh) if wh is not None else None,
            })
        # Synthetic region label centered on the cluster. Negative ids so
        # they don't collide with SDE region ids.
        regions.append({
            "id": -(list(_BUCKETS).index(bucket) + 1),
            "name": label,
            "cx": cx,
            "cy": cy - cluster_r - 60.0,  # label sits above the cluster
        })

    log.info(
        "wormhole layout: %d systems placed across %d clusters (skipped %d)",
        len(systems), len(_BUCKETS), skipped,
    )
    _layout_cache = {"systems": systems, "edges": [], "regions": regions}
    return _layout_cache
