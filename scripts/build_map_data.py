#!/usr/bin/env python3
"""
Downloads the official CCP SDE JSONL and produces optimized static JSON files
for the Vigilant star-map frontend.

Output (written to frontend/public/data/):
  systems.json  — ~5,200 K-space systems with normalized 2-D coordinates
  edges.json    — ~6,900 deduplicated stargate pairs
  regions.json  — region centroids for label placement

Usage:
  python scripts/build_map_data.py              # download + generate
  python scripts/build_map_data.py --cache      # reuse previously downloaded ZIP
"""
import argparse
import io
import json
import logging
import math
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
CACHE_PATH = Path("scripts/.sde_cache.zip")
# CLI default: write to the frontend's Vite static input dir (build-time seed).
# Runtime callers (app/sde/loader.py post-import) pass `/data/map/` instead so
# the running app can refresh K-space JSONs without a redeploy. See ISS-019.
DEFAULT_OUTPUT_DIR = Path("frontend/public/data")

# K-space system IDs are <= 30999999 (31xxxxxx = wormhole, 32xxxxxx = abyssal)
MAX_KSPACE_ID = 30_999_999
# Coordinate canvas size (systems normalized into 0..CANVAS_SIZE)
CANVAS_SIZE = 10_000
# 1 light-year in meters (EVE's definition)
LY_IN_METERS = 9.461e15


def download_sde(use_cache: bool) -> zipfile.ZipFile:
    if use_cache and CACHE_PATH.exists():
        log.info("Using cached SDE ZIP at %s", CACHE_PATH)
        return zipfile.ZipFile(CACHE_PATH)

    log.info("Downloading SDE from %s ...", SDE_URL)
    req = urllib.request.Request(SDE_URL, headers={"User-Agent": "Vigilant-MapBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    log.info("Downloaded %s bytes", f"{len(data):,}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(data)
    return zipfile.ZipFile(io.BytesIO(data))


def iter_jsonl(zf: zipfile.ZipFile, filename: str):
    with zf.open(filename) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def build(use_cache: bool, output_dir: Path = DEFAULT_OUTPUT_DIR):
    zf = download_sde(use_cache)
    build_from_zip(zf, output_dir)


def build_from_zip(zf: zipfile.ZipFile, output_dir: Path):
    """Project the SDE zip to K-space map JSON files in `output_dir`.

    Reusable entry point for both the CLI and the runtime SDE-update flow.
    Called from `app/sde/loader.py` after a successful import so the
    K-space star map refreshes whenever the admin "Force Update SDE"
    button runs. See ISS-019.
    """
    # ── 1. Regions & Constellations ──────────────────────────────────────
    log.info("Parsing regions...")
    regions: dict[int, str] = {}
    for item in iter_jsonl(zf, "mapRegions.jsonl"):
        name = item.get("name", {}).get("en")
        if name:
            regions[int(item["_key"])] = name

    log.info("Parsing constellations...")
    constellations: dict[int, dict] = {}
    for item in iter_jsonl(zf, "mapConstellations.jsonl"):
        name = item.get("name", {}).get("en")
        if name:
            constellations[int(item["_key"])] = {
                "name": name,
                "regionId": item.get("regionID"),
            }

    # ── 2. NPC stations + services per system ─────────────────────────
    # Build operation → services mapping
    log.info("Parsing station operations and services...")
    service_names: dict[int, str] = {}
    for item in iter_jsonl(zf, "stationServices.jsonl"):
        service_names[int(item["_key"])] = item.get("serviceName", {}).get("en", "")

    op_services: dict[int, set[int]] = {}
    for item in iter_jsonl(zf, "stationOperations.jsonl"):
        op_services[int(item["_key"])] = set(item.get("services", []))

    # Key services to track (IDs from stationServices.jsonl)
    TRACKED_SERVICES = {
        5: "reprocessing",  # Reprocessing Plant
        6: "refinery",      # Refinery
        7: "market",        # Market
        10: "cloning",      # Cloning
        13: "repair",       # Repair Facilities
        14: "factory",      # Factory (manufacturing)
        15: "lab",          # Laboratory (research)
        24: "jumpClone",    # Jump Clone Facility
    }

    log.info("Parsing NPC stations...")
    systems_with_stations: set[int] = set()
    system_services: dict[int, set[str]] = defaultdict(set)
    system_station_count: dict[int, int] = defaultdict(int)

    for item in iter_jsonl(zf, "npcStations.jsonl"):
        try:
            sid = int(item["solarSystemID"])
            op_id = item.get("operationID")
        except (KeyError, ValueError):
            continue
        systems_with_stations.add(sid)
        system_station_count[sid] += 1
        if op_id and op_id in op_services:
            for svc_id in op_services[op_id]:
                if svc_id in TRACKED_SERVICES:
                    system_services[sid].add(TRACKED_SERVICES[svc_id])

    log.info("  %d systems have NPC stations", len(systems_with_stations))

    # ── 3. Solar systems ─────────────────────────────────────────────────
    log.info("Parsing solar systems...")
    raw_systems: list[dict] = []
    missing_pos = 0

    for item in iter_jsonl(zf, "mapSolarSystems.jsonl"):
        try:
            sid = int(item["_key"])
        except (KeyError, ValueError):
            continue
        if sid > MAX_KSPACE_ID:
            continue

        name = item.get("name", {})
        if isinstance(name, dict):
            name = name.get("en", f"J{sid}")

        sec = item.get("securityStatus", 0.0)
        if sec is None:
            sec = 0.0
        sec = round(sec, 2)

        con_id = item.get("constellationID")
        reg_id = item.get("regionID")

        # 3-D position (always present — needed for jump drive distance calculations)
        pos = item.get("position", {})
        pos3_x = float(pos.get("x", 0))
        pos3_y = float(pos.get("y", 0))
        pos3_z = float(pos.get("z", 0))

        # 2-D position: prefer position2D (CCP's hand-curated 2-D map coords)
        pos2d = item.get("position2D")
        if pos2d and pos2d.get("x") is not None and pos2d.get("y") is not None:
            raw_x = float(pos2d["x"])
            raw_y = float(pos2d["y"])
        else:
            # Fallback: project 3-D position (x → horizontal, z → vertical)
            if pos.get("x") is not None and pos.get("z") is not None:
                raw_x = pos3_x
                raw_y = pos3_z
            else:
                missing_pos += 1
                continue

        con_info = constellations.get(con_id, {})
        raw_systems.append({
            "id": sid,
            "name": name,
            "rawX": raw_x,
            "rawY": raw_y,
            "pos3": (pos3_x, pos3_y, pos3_z),
            "sec": sec,
            "conId": con_id,
            "conName": con_info.get("name", ""),
            "regId": reg_id,
            "regName": regions.get(reg_id, ""),
            "hasStation": sid in systems_with_stations,
            "stns": system_station_count.get(sid, 0),
            "svcs": sorted(system_services.get(sid, [])),
        })

    if missing_pos:
        log.warning("  Skipped %d systems with no position data", missing_pos)
    log.info("  Parsed %d K-space systems", len(raw_systems))

    # ── 4. Normalize coordinates to 0..CANVAS_SIZE ───────────────────────
    if not raw_systems:
        log.error("No systems parsed — cannot continue")
        sys.exit(1)

    min_x = min(s["rawX"] for s in raw_systems)
    max_x = max(s["rawX"] for s in raw_systems)
    min_y = min(s["rawY"] for s in raw_systems)
    max_y = max(s["rawY"] for s in raw_systems)

    range_x = max_x - min_x or 1.0
    range_y = max_y - min_y or 1.0

    # Preserve aspect ratio: scale both axes by the larger range
    scale = CANVAS_SIZE / max(range_x, range_y)

    # Center the shorter axis
    extent_x = range_x * scale
    extent_y = range_y * scale
    offset_x = (CANVAS_SIZE - extent_x) / 2
    offset_y = (CANVAS_SIZE - extent_y) / 2

    system_id_set: set[int] = set()
    systems_out: list[dict] = []

    for s in raw_systems:
        nx = round((s["rawX"] - min_x) * scale + offset_x, 1)
        # Flip Y: EVE's Y increases northward, screen Y increases downward
        ny = round(CANVAS_SIZE - ((s["rawY"] - min_y) * scale + offset_y), 1)
        # 3D position in light-years for jump drive calculations
        p3 = s["pos3"]
        system_id_set.add(s["id"])
        systems_out.append({
            "id": s["id"],
            "name": s["name"],
            "x": nx,
            "y": ny,
            "sec": s["sec"],
            "conId": s["conId"],
            "conName": s["conName"],
            "regId": s["regId"],
            "regName": s["regName"],
            "hasStation": s["hasStation"],
            "stns": s["stns"],
            "svcs": s["svcs"],
            "x3": round(p3[0] / LY_IN_METERS, 4),
            "y3": round(p3[1] / LY_IN_METERS, 4),
            "z3": round(p3[2] / LY_IN_METERS, 4),
        })

    systems_out.sort(key=lambda s: s["id"])

    # ── 5. Stargate edges ────────────────────────────────────────────────
    log.info("Parsing stargates...")
    edge_set: set[tuple[int, int]] = set()
    for item in iter_jsonl(zf, "mapStargates.jsonl"):
        try:
            src = int(item["solarSystemID"])
            dst = int(item["destination"]["solarSystemID"])
        except (KeyError, ValueError):
            continue
        if src not in system_id_set or dst not in system_id_set:
            continue
        pair = (min(src, dst), max(src, dst))
        edge_set.add(pair)

    edges_out = sorted(edge_set)
    log.info("  %d deduplicated stargate pairs", len(edges_out))

    # Validate: all edge IDs exist in systems
    for src, dst in edges_out:
        assert src in system_id_set and dst in system_id_set, f"Edge references unknown system: {src}-{dst}"

    # ── 6. Region centroids ──────────────────────────────────────────────
    region_points: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for s in systems_out:
        region_points[s["regId"]].append((s["x"], s["y"]))

    regions_out = []
    for reg_id, pts in region_points.items():
        cx = round(sum(p[0] for p in pts) / len(pts), 1)
        cy = round(sum(p[1] for p in pts) / len(pts), 1)
        regions_out.append({
            "id": reg_id,
            "name": regions.get(reg_id, ""),
            "cx": cx,
            "cy": cy,
        })
    regions_out.sort(key=lambda r: r["id"])

    # ── 7. Write output ─────────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)

    def write_json(filename: str, data):
        path = output_dir / filename
        with open(path, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        size = path.stat().st_size
        log.info("  Wrote %s (%s)", path, f"{size:,} bytes")

    write_json("systems.json", systems_out)
    write_json("edges.json", edges_out)
    write_json("regions.json", regions_out)

    # ── Summary ──────────────────────────────────────────────────────────
    log.info("Done! %d systems, %d edges, %d regions",
             len(systems_out), len(edges_out), len(regions_out))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build star-map JSON data from EVE SDE")
    parser.add_argument("--cache", action="store_true", help="Reuse previously downloaded SDE ZIP")
    args = parser.parse_args()
    build(use_cache=args.cache)
