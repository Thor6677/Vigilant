"""Synthetic 2D layout for wormhole space.

J-systems have no canonical 2D positions in the SDE — wormhole connections
rotate constantly so there's no static graph. This module groups the ~3000
J-systems by wormhole class into a 3×3 grid of cells, hex-packs systems
within each cell, and exposes the result in the same JSON shape as the
k-space map's static bundles (`systems.json`, `edges.json`, `regions.json`)
so the frontend can render it through the existing StarMap component.

Layout is deterministic (sorted by system_id within each class) and built
once at startup — cached in memory and served by /api/map/wormholes-data/*.

Class buckets (3×3 grid, top-left → bottom-right):
  C1  | C2  | C3
  C4  | C5  | C6
  Drft| Thra| Pchv

Where:
- "Drft" cell: shattered (class 13) + small drifter complexes (14–18)
- "Thra" cell: Thera (class 12)
- "Pchv" cell: Pochven (class 25) + any leftover specials
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

# Layout constants. Cell size and grid spacing chosen so the whole map is
# visually similar in scale to k-space (~5000 units across).
_CELL_SIZE = 1500.0
_CELL_GAP = 200.0
_GRID_ORIGIN_X = -((_CELL_SIZE * 3 + _CELL_GAP * 2) / 2)
_GRID_ORIGIN_Y = -((_CELL_SIZE * 3 + _CELL_GAP * 2) / 2)

# Bucket → (col, row, label)
_BUCKETS: dict[str, tuple[int, int, str]] = {
    "c1":      (0, 0, "C1"),
    "c2":      (1, 0, "C2"),
    "c3":      (2, 0, "C3"),
    "c4":      (0, 1, "C4"),
    "c5":      (1, 1, "C5"),
    "c6":      (2, 1, "C6"),
    "drifter": (0, 2, "Drifter / Shattered"),
    "thera":   (1, 2, "Thera"),
    "pochven": (2, 2, "Pochven / Special"),
}


def _classify(wh_class: int | None) -> str | None:
    """Map a raw wormhole class id to one of our 9 buckets, or None to drop."""
    if wh_class is None:
        return None
    if 1 <= wh_class <= 6:
        return f"c{wh_class}"
    if wh_class == 12:
        return "thera"
    if wh_class in (13, 14, 15, 16, 17, 18, 19):
        return "drifter"
    if wh_class == 25:
        return "pochven"
    if wh_class in (10, 11, 20, 21, 22, 23):
        return "pochven"
    return None


def _cell_origin(col: int, row: int) -> tuple[float, float]:
    return (
        _GRID_ORIGIN_X + col * (_CELL_SIZE + _CELL_GAP),
        _GRID_ORIGIN_Y + row * (_CELL_SIZE + _CELL_GAP),
    )


def _hex_pack(n: int) -> list[tuple[float, float]]:
    """Return n positions hex-packed inside a unit square [0,1]×[0,1].

    Picks a square-ish grid, applies row-offset for hex packing, scales to
    fit within the unit square with a small margin.
    """
    if n <= 0:
        return []
    # Roughly square grid: cols ≈ rows ≈ sqrt(n) but biased to slightly more
    # cols since the cell is square and hex offsets cost vertical room.
    cols = max(1, int(math.ceil(math.sqrt(n * 1.15))))
    rows = max(1, int(math.ceil(n / cols)))
    margin = 0.06
    avail = 1.0 - 2 * margin
    dx = avail / max(1, cols - 1) if cols > 1 else 0.0
    dy = avail / max(1, rows - 1) if rows > 1 else 0.0
    pos: list[tuple[float, float]] = []
    for i in range(n):
        r = i // cols
        c = i % cols
        x = margin + c * dx
        if r % 2 == 1:
            x += dx * 0.5  # hex offset
            x = min(x, 1.0 - margin)
        y = margin + r * dy
        pos.append((x, y))
    return pos


_layout_cache: dict[str, Any] | None = None


async def build_wormhole_layout() -> dict[str, Any]:
    """Compute the wormhole-space layout. Idempotent — caches the first
    result. Returns a dict with three keys: systems, edges, regions —
    matching the shape of the static k-space bundles.
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

    # Access through the module so we see the populated dict (importing the
    # name directly would bind to None forever — Python doesn't re-bind
    # `from x import y` when the module reassigns y).
    cache = sde_lookup._wh_class_cache or {}

    # Group J-systems into buckets, remembering each system's resolved
    # wormhole_class_id so the frontend can render a per-class overlay.
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
    for bucket, (col, row, label) in _BUCKETS.items():
        ox, oy = _cell_origin(col, row)
        members = grouped[bucket]
        positions = _hex_pack(len(members))
        for (sid, name, sec, con_id, reg_id, wh), (px, py) in zip(members, positions):
            systems.append({
                "id": int(sid),
                "name": name,
                "x": ox + px * _CELL_SIZE,
                "y": oy + py * _CELL_SIZE,
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
        # Region label centered in the cell.
        regions.append({
            "id": -col - row * 3 - 1,  # synthetic negative id, won't collide with SDE
            "name": label,
            "cx": ox + _CELL_SIZE / 2,
            "cy": oy + _CELL_SIZE / 2,
        })

    log.info("wormhole layout: %d systems placed across %d cells (skipped %d)",
             len(systems), len(_BUCKETS), skipped)
    _layout_cache = {"systems": systems, "edges": [], "regions": regions}
    return _layout_cache
