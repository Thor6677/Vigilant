"""
Projects the SDE JSONL zip to the K-space star-map JSON files
(systems.json, edges.json, regions.json).

Lives in app/ because the running app regenerates the files after every SDE
import (ISS-019) and the image only ships app/. The offline CLI wrapper is
scripts/build_map_data.py. Standard library only.
"""
import json
import logging
import os
import zipfile
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

# K-space system IDs are <= 30999999 (31xxxxxx = wormhole, 32xxxxxx = abyssal)
MAX_KSPACE_ID = 30_999_999
# Coordinate canvas size (systems normalized into 0..CANVAS_SIZE)
CANVAS_SIZE = 10_000
# 1 light-year in meters (EVE's definition)
LY_IN_METERS = 9.461e15

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


def iter_jsonl(zf: zipfile.ZipFile, filename: str):
    with zf.open(filename) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def build_from_zip(zf: zipfile.ZipFile, output_dir: Path):
    """Project the SDE zip to K-space map JSON files in `output_dir`.

    Reusable entry point for both the CLI (scripts/build_map_data.py) and the
    runtime SDE-update flow. Called from `app/sde/loader.py` after a
    successful import so the K-space star map refreshes whenever the admin
    "Force Update SDE" button runs. See ISS-019.

    Raises ValueError when no K-space systems parse (e.g. a schema change).
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
        raise ValueError("no K-space systems parsed")

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

    # Stage all three files beside their targets, then swap them in together,
    # so a failure part-way never leaves a mix of old and new files and a
    # concurrent /api/map/kspace-data request never sees a half-written one.
    outputs = [
        ("systems.json", systems_out),
        ("edges.json", edges_out),
        ("regions.json", regions_out),
    ]
    staged = [(output_dir / name, output_dir / (name + ".tmp"), data)
              for name, data in outputs]
    try:
        for _path, tmp, data in staged:
            with open(tmp, "w") as f:
                json.dump(data, f, separators=(",", ":"))
        for path, tmp, _data in staged:
            os.replace(tmp, path)
            log.info("  Wrote %s (%s)", path, f"{path.stat().st_size:,} bytes")
    finally:
        for _path, tmp, _data in staged:
            tmp.unlink(missing_ok=True)

    # ── Summary ──────────────────────────────────────────────────────────
    log.info("Done! %d systems, %d edges, %d regions",
             len(systems_out), len(edges_out), len(regions_out))
