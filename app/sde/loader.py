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


async def _fetch_csv(url: str):
    """Stream-decompress a bz2 CSV from Fuzzwork, yielding one dict per row."""
    log.info(f"Downloading {url}")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    # Decompress and stream line by line to avoid loading full file into RAM
    decompressed = bz2.decompress(resp.content).decode("utf-8")
    reader = csv.DictReader(io.StringIO(decompressed))
    for row in reader:
        yield row


async def _import_table(db, table, rows_gen, batch_size=200):
    """Generic streaming batch insert — never holds more than batch_size rows in memory."""
    count = 0
    batch = []
    async for row in rows_gen:
        if row is None:
            continue
        batch.append(row)
        if len(batch) >= batch_size:
            await db.execute(table.insert(), batch)
            await db.commit()
            count += len(batch)
            batch = []
    if batch:
        await db.execute(table.insert(), batch)
        await db.commit()
        count += len(batch)
    return count


async def download_and_import(db: AsyncSession):
    log.info("Starting SDE import from Fuzzwork (streaming mode)...")

    # --- invTypes ---
    log.info("Importing invTypes...")
    await db.execute(text("DELETE FROM sde_types"))
    await db.commit()

    async def type_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/invTypes.csv.bz2"):
            if r.get("published") != "1":
                continue
            try:
                yield {
                    "type_id": int(r["typeID"]),
                    "type_name": r["typeName"],
                    "group_id": int(r["groupID"]) if r.get("groupID") else None,
                    "category_id": None,
                    "published": True,
                }
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDEType.__table__, type_rows())
    log.info(f"Imported {count} types")

    # --- mapRegions ---
    log.info("Importing mapRegions...")
    await db.execute(text("DELETE FROM sde_regions"))
    await db.commit()

    async def region_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/mapRegions.csv.bz2"):
            if not r.get("regionName"):
                continue
            try:
                yield {"region_id": int(r["regionID"]), "region_name": r["regionName"]}
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDERegion.__table__, region_rows())
    log.info(f"Imported {count} regions")

    # --- mapConstellations ---
    log.info("Importing mapConstellations...")
    await db.execute(text("DELETE FROM sde_constellations"))
    await db.commit()

    async def const_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/mapConstellations.csv.bz2"):
            if not r.get("constellationName"):
                continue
            try:
                yield {
                    "constellation_id": int(r["constellationID"]),
                    "constellation_name": r["constellationName"],
                    "region_id": int(r["regionID"]) if r.get("regionID") else None,
                }
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDEConstellation.__table__, const_rows())
    log.info(f"Imported {count} constellations")

    # --- mapSolarSystems ---
    log.info("Importing mapSolarSystems...")
    await db.execute(text("DELETE FROM sde_systems"))
    await db.commit()

    async def system_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/mapSolarSystems.csv.bz2"):
            try:
                yield {
                    "system_id": int(r["solarSystemID"]),
                    "system_name": r["solarSystemName"],
                    "security": float(r["security"]) if r.get("security") else None,
                    "constellation_id": int(r["constellationID"]) if r.get("constellationID") else None,
                    "region_id": int(r["regionID"]) if r.get("regionID") else None,
                }
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDESystem.__table__, system_rows())
    log.info(f"Imported {count} systems")

    # --- mapSolarSystemJumps ---
    log.info("Importing mapSolarSystemJumps...")
    await db.execute(text("DELETE FROM sde_jumps"))
    await db.commit()

    async def jump_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/mapSolarSystemJumps.csv.bz2"):
            try:
                yield {
                    "from_system_id": int(r["fromSolarSystemID"]),
                    "to_system_id": int(r["toSolarSystemID"]),
                }
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDEJump.__table__, jump_rows())
    log.info(f"Imported {count} jump edges")

    # --- staOperationServices (small file — collect cloning op IDs) ---
    log.info("Loading staOperationServices...")
    cloning_operations: set[int] = set()
    async for r in _fetch_csv(f"{FUZZWORK_BASE}/staOperationServices.csv.bz2"):
        if r.get("serviceID") == "60":
            try:
                cloning_operations.add(int(r["operationID"]))
            except (ValueError, KeyError):
                pass
    log.info(f"Found {len(cloning_operations)} operation types with cloning")

    # --- staStations ---
    log.info("Importing staStations...")
    await db.execute(text("DELETE FROM sde_stations"))
    await db.commit()

    async def station_rows():
        async for r in _fetch_csv(f"{FUZZWORK_BASE}/staStations.csv.bz2"):
            try:
                op_id = int(r["operationID"]) if r.get("operationID") else None
                yield {
                    "station_id": int(r["stationID"]),
                    "station_name": r["stationName"],
                    "system_id": int(r["solarSystemID"]),
                    "has_cloning": op_id in cloning_operations,
                }
            except (ValueError, KeyError):
                continue

    count = await _import_table(db, SDEStation.__table__, station_rows())
    log.info(f"Imported {count} stations")

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
