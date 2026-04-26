#!/usr/bin/env python3
"""Build the natural Anoikis layout from EVE SDE positions.

J-space systems (system_id >= 31000000) have real 3-D coordinates in the
SDE — CCP placed them with intentional spatial structure (Drifter complex
cluster, C6 stripe, Drifter shattered group, etc.) even though there is
no static jump graph. Projecting those positions to 2-D gives the
natural layout that anoikis.info-style maps display, in contrast to
the synthetic hex-flower we used previously.

Projection: top-down (look down +Y axis), so screen-X = world-X and
screen-Y = world-Z. This is the same convention K-space falls back to
(`build_map_data.py` lines 169-171). Z is flipped on the screen Y axis
because EVE's Y-up world coords map to screen Y-down.

Output: app/data/wormhole_positions.json with the shape
    {
      "projection": "XZ",
      "canvas_size": 10000,
      "systems": [{"id", "x", "y", "wh_class"}, ...]
    }

The runtime layout in app/intel/wormhole_layout.py loads this file and
joins it against the DB for system metadata (name, security).

Usage:
  python scripts/build_wormhole_positions.py            # download SDE
  python scripts/build_wormhole_positions.py --cache    # reuse SDE zip
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
CACHE_PATH = Path("scripts/.sde_cache.zip")
OUTPUT_PATH = Path("app/data/wormhole_positions.json")

CANVAS_SIZE = 10_000
# Margin (canvas units) so points near the cluster edge aren't pinned
# to the literal canvas border — keeps room for hover labels & glow.
PADDING = 400

# J-space system IDs: 31000000–31999999.
J_MIN = 31_000_000
J_MAX = 32_000_000


def download_sde(use_cache: bool) -> zipfile.ZipFile:
    if use_cache and CACHE_PATH.exists():
        log.info("Using cached SDE at %s", CACHE_PATH)
        return zipfile.ZipFile(CACHE_PATH)
    log.info("Downloading SDE from %s ...", SDE_URL)
    req = urllib.request.Request(SDE_URL, headers={"User-Agent": "Vigilant-MapBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    log.info("Downloaded %s bytes", f"{len(data):,}")
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(data)
    return zipfile.ZipFile(io.BytesIO(data))


def iter_jsonl(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def build(use_cache: bool) -> None:
    zf = download_sde(use_cache)

    # Wormhole class lives at three levels in the SDE: system, constellation,
    # region. Build region/constellation lookups first so we can fall back
    # if a system has no direct wormholeClassID (most systems inherit).
    region_class: dict[int, int] = {}
    for r in iter_jsonl(zf, "mapRegions.jsonl"):
        wc = r.get("wormholeClassID")
        if wc is not None:
            region_class[int(r["_key"])] = int(wc)

    con_class: dict[int, int] = {}
    con_region: dict[int, int] = {}
    for c in iter_jsonl(zf, "mapConstellations.jsonl"):
        cid = int(c["_key"])
        if c.get("wormholeClassID") is not None:
            con_class[cid] = int(c["wormholeClassID"])
        if c.get("regionID"):
            con_region[cid] = int(c["regionID"])

    # Pull J-space systems with positions and resolved wh_class.
    raw: list[dict] = []
    skipped_no_pos = 0
    skipped_no_class = 0
    for s in iter_jsonl(zf, "mapSolarSystems.jsonl"):
        try:
            sid = int(s["_key"])
        except (KeyError, ValueError):
            continue
        if sid < J_MIN or sid >= J_MAX:
            continue
        pos = s.get("position", {})
        if pos.get("x") is None or pos.get("z") is None:
            skipped_no_pos += 1
            continue
        wh_class = s.get("wormholeClassID")
        if wh_class is None:
            cid = s.get("constellationID")
            rid = s.get("regionID")
            if cid is not None:
                wh_class = con_class.get(cid)
                if wh_class is None:
                    rid_via_con = con_region.get(cid)
                    if rid_via_con is not None:
                        wh_class = region_class.get(rid_via_con)
            if wh_class is None and rid is not None:
                wh_class = region_class.get(rid)
        if wh_class is None:
            skipped_no_class += 1
        raw.append({
            "id": sid,
            "px": float(pos["x"]),
            "pz": float(pos["z"]),
            "wh_class": int(wh_class) if wh_class is not None else None,
        })

    if skipped_no_pos:
        log.warning("Skipped %d J-space systems with no position", skipped_no_pos)
    if skipped_no_class:
        log.warning("%d systems missing wh_class (will render uncolored)", skipped_no_class)
    log.info("Loaded %d J-space systems", len(raw))
    if not raw:
        raise RuntimeError("No J-space systems parsed — SDE missing or filtered out")

    # Top-down XZ projection: world-X → screen-X, world-Z → screen-Y.
    # Normalise to a 0..CANVAS_SIZE square preserving aspect, then flip
    # screen-Y so Z increasing maps to "down" on screen — same convention
    # K-space uses (build_map_data.py:223-225).
    raw_x = [s["px"] for s in raw]
    raw_y = [s["pz"] for s in raw]
    min_x, max_x = min(raw_x), max(raw_x)
    min_y, max_y = min(raw_y), max(raw_y)
    range_x = max_x - min_x or 1.0
    range_y = max_y - min_y or 1.0

    usable = CANVAS_SIZE - 2 * PADDING
    scale = usable / max(range_x, range_y)
    extent_x = range_x * scale
    extent_y = range_y * scale
    offset_x = (CANVAS_SIZE - extent_x) / 2
    offset_y = (CANVAS_SIZE - extent_y) / 2

    systems_out: list[dict] = []
    for s in raw:
        nx = round((s["px"] - min_x) * scale + offset_x, 1)
        ny = round(CANVAS_SIZE - ((s["pz"] - min_y) * scale + offset_y), 1)
        systems_out.append({
            "id": s["id"],
            "x": nx,
            "y": ny,
            "wh_class": s["wh_class"],
        })
    systems_out.sort(key=lambda r: r["id"])

    # Diagnostics: per-class system counts and centroids in canvas units.
    by_class: dict[int | None, list[dict]] = defaultdict(list)
    for r in systems_out:
        by_class[r["wh_class"]].append(r)
    log.info("Per-class breakdown:")
    for cls in sorted(by_class.keys(), key=lambda c: (c is None, c if c is not None else -1)):
        members = by_class[cls]
        cx_p = sum(m["x"] for m in members) / len(members)
        cy_p = sum(m["y"] for m in members) / len(members)
        log.info("  class=%s n=%4d centroid=(%6.0f,%6.0f)",
                 str(cls), len(members), cx_p, cy_p)

    payload = {
        "projection": "XZ",
        "canvas_size": CANVAS_SIZE,
        "padding": PADDING,
        "systems": systems_out,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    size = OUTPUT_PATH.stat().st_size
    log.info("Wrote %s (%s bytes, %d systems)", OUTPUT_PATH, f"{size:,}", len(systems_out))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cache", action="store_true", help="Reuse cached SDE zip if present")
    args = p.parse_args()
    build(args.cache)
