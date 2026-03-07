"""
Downloads and imports EVE SDE data from Fuzzwork into local SQLite.
Only re-downloads if data is older than 30 days.
"""
import bz2
import csv
import io
import logging
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal
from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEMeta

log = logging.getLogger(__name__)

FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest"
REFRESH_DAYS = 30

TABLES = [
    "invTypes",
    "mapSolarSystems",
    "mapSolarSystemJumps",
    "mapRegions",
    "mapConstellations",
    "staStations",
    "staOperationServices",
]


async def _get_meta(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(text("SELECT value FROM sde_meta WHERE key = :key"), {"key": key})
    row = result.fetchone()
    return row[0] if row else None


async def _set_meta(db: AsyncSession, key: str, value: str):
    await db.execute(
        text("INSERT OR REPLACE INTO sde_meta (key, value) VALUES (:key, :value)"),
        {"key": key, "value": value},
    )
    await db.commit()


async def needs_update(db: AsyncSession) -> bool:
    last = await _get_meta(db, "last_updated")
    if not last:
        return True
    updated = datetime.fromisoformat(last)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated > timedelta(days=REFRESH_DAYS)


async def _fetch_csv(url: str) -> list[dict]:
    log.info(f"Downloading {url}")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = bz2.decompress(resp.content).decode("utf-8")
    reader = csv.DictReader(io.StringIO(data))
    return list(reader)


async def download_and_import(db: AsyncSession):
    log.info("Starting SDE import from Fuzzwork...")

    # --- invTypes ---
    log.info("Importing invTypes...")
    rows = await _fetch_csv(f"{FUZZWORK_BASE}/invTypes.csv.bz2")
    await db.execute(text("DELETE FROM sde_types"))
    batch = []
    for r in rows:
        if r.get("published", "0") != "1":
            continue
        batch.append({
            "type_id": int(r["typeID"]),
            "type_name": r["typeName"],
            "group_id": int(r["groupID"]) if r.get("groupID") else None,
            "category_id": None,
            "published": True,
        })
        if len(batch) >= 1000:
            await db.execute(SDEType.__table__.insert(), batch)
            batch = []
    if batch:
        await db.execute(SDEType.__table__.insert(), batch)
    await db.commit()
    log.info(f"Imported {len([r for r in rows if r.get('published','0')=='1'])} types")

    # --- mapRegions ---
    log.info("Importing mapRegions...")
    rows = await _fetch_csv(f"{FUZZWORK_BASE}/mapRegions.csv.bz2")
    await db.execute(text("DELETE FROM sde_regions"))
    batch = [{"region_id": int(r["regionID"]), "region_name": r["regionName"]} for r in rows if r.get("regionName")]
    if batch:
        await db.execute(SDERegion.__table__.insert(), batch)
    await db.commit()
    log.info(f"Imported {len(batch)} regions")

    # --- mapConstellations ---
    log.info("Importing mapConstellations...")
    rows = await _fetch_csv(f"{FUZZWORK_BASE}/mapConstellations.csv.bz2")
    await db.execute(text("DELETE FROM sde_constellations"))
    batch = [
        {
            "constellation_id": int(r["constellationID"]),
            "constellation_name": r["constellationName"],
            "region_id": int(r["regionID"]) if r.get("regionID") else None,
        }
        for r in rows if r.get("constellationName")
    ]
    if batch:
        await db.execute(SDEConstellation.__table__.insert(), batch)
    await db.commit()
    log.info(f"Imported {len(batch)} constellations")

    # --- mapSolarSystems ---
    log.info("Importing mapSolarSystems...")
    rows = await _fetch_csv(f"{FUZZWORK_BASE}/mapSolarSystems.csv.bz2")
    await db.execute(text("DELETE FROM sde_systems"))
    batch = []
    for r in rows:
        try:
            batch.append({
                "system_id": int(r["solarSystemID"]),
                "system_name": r["solarSystemName"],
                "security": float(r["security"]) if r.get("security") else None,
                "constellation_id": int(r["constellationID"]) if r.get("constellationID") else None,
                "region_id": int(r["regionID"]) if r.get("regionID") else None,
            })
        except (ValueError, KeyError):
            continue
    if batch:
        await db.execute(SDESystem.__table__.insert(), batch)
    await db.commit()
    log.info(f"Imported {len(batch)} systems")

    # --- mapSolarSystemJumps ---
    log.info("Importing mapSolarSystemJumps...")
    rows = await _fetch_csv(f"{FUZZWORK_BASE}/mapSolarSystemJumps.csv.bz2")
    await db.execute(text("DELETE FROM sde_jumps"))
    batch = []
    for r in rows:
        try:
            batch.append({
                "from_system_id": int(r["fromSolarSystemID"]),
                "to_system_id": int(r["toSolarSystemID"]),
            })
        except (ValueError, KeyError):
            continue
        if len(batch) >= 1000:
            await db.execute(SDEJump.__table__.insert(), batch)
            batch = []
    if batch:
        await db.execute(SDEJump.__table__.insert(), batch)
    await db.commit()
    log.info(f"Imported {len(rows)} jump edges")

    # --- staStations + staOperationServices (for cloning detection) ---
    log.info("Importing staStations...")
    op_services = await _fetch_csv(f"{FUZZWORK_BASE}/staOperationServices.csv.bz2")
    # serviceID 60 = Clone Bay
    cloning_operations = {int(r["operationID"]) for r in op_services if r.get("serviceID") == "60"}
    log.info(f"Found {len(cloning_operations)} operation types with cloning")

    rows = await _fetch_csv(f"{FUZZWORK_BASE}/staStations.csv.bz2")
    await db.execute(text("DELETE FROM sde_stations"))
    batch = []
    for r in rows:
        try:
            op_id = int(r["operationID"]) if r.get("operationID") else None
            batch.append({
                "station_id": int(r["stationID"]),
                "station_name": r["stationName"],
                "system_id": int(r["solarSystemID"]),
                "has_cloning": op_id in cloning_operations,
            })
        except (ValueError, KeyError):
            continue
        if len(batch) >= 500:
            await db.execute(SDEStation.__table__.insert(), batch)
            batch = []
    if batch:
        await db.execute(SDEStation.__table__.insert(), batch)
    await db.commit()
    cloning_count = sum(1 for r in rows if r.get("operationID") and int(r["operationID"]) in cloning_operations)
    log.info(f"Imported {len(rows)} stations, {cloning_count} with cloning")

    await _set_meta(db, "last_updated", datetime.now(timezone.utc).isoformat())
    log.info("SDE import complete.")


async def ensure_sde_loaded():
    """Called at startup — imports SDE if missing or stale."""
    async with AsyncSessionLocal() as db:
        if await needs_update(db):
            try:
                await download_and_import(db)
            except Exception as e:
                log.error(f"SDE import failed: {e}. App will use ESI fallbacks.")
        else:
            log.info("SDE data is current, skipping download.")
