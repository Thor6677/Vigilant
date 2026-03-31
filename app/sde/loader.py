"""
Downloads and imports EVE SDE data from the official CCP source into local SQLite.
Only re-downloads if data is older than 30 days.
"""
import asyncio
import io
import json
import logging
import zipfile
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal
from app.db.sde_models import (
    SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEMeta,
    SDEBlueprintMaterial, SDETypeMaterial, SDECompressible, SDEBlueprintInfo,
    SDEGroup, SDETypeSkillReq, SDESkillInfo, SDECertificate, SDECertificateSkill,
    SDEShipMastery,
)

log = logging.getLogger(__name__)

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
REFRESH_DAYS = 30


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


def _iter_jsonl(zf: zipfile.ZipFile, filename: str):
    """Yield parsed JSON objects from a JSON Lines file inside a zip archive."""
    with zf.open(filename) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


async def _bulk_insert(db: AsyncSession, table, rows: list[dict]):
    if rows:
        await db.execute(table.insert(), rows)
        await db.commit()


async def download_and_import(db: AsyncSession):
    log.info("Downloading official EVE SDE from CCP...")
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        resp = await client.get(SDE_URL)
        resp.raise_for_status()
    log.info(f"Downloaded {len(resp.content):,} bytes, importing...")

    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    # --- types (invTypes equivalent) ---
    log.info("Importing types...")
    await db.execute(text("DELETE FROM sde_types"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "types.jsonl"):
        if not item.get("published"):
            continue
        try:
            batch.append({
                "type_id": int(item["_key"]),
                "type_name": item["name"]["en"],
                "group_id": item.get("groupID"),
                "category_id": None,
                "published": True,
                "volume": item.get("volume"),
                "portion_size": item.get("portionSize"),
            })
        except (KeyError, ValueError, TypeError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDEType.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEType.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} types")

    # --- mapRegions ---
    log.info("Importing regions...")
    await db.execute(text("DELETE FROM sde_regions"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "mapRegions.jsonl"):
        name = item.get("name", {}).get("en")
        if not name:
            continue
        try:
            batch.append({"region_id": int(item["_key"]), "region_name": name})
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDERegion.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDERegion.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} regions")

    # --- mapConstellations ---
    log.info("Importing constellations...")
    await db.execute(text("DELETE FROM sde_constellations"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "mapConstellations.jsonl"):
        name = item.get("name", {}).get("en")
        if not name:
            continue
        try:
            batch.append({
                "constellation_id": int(item["_key"]),
                "constellation_name": name,
                "region_id": item.get("regionID"),
            })
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDEConstellation.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEConstellation.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} constellations")

    # --- mapSolarSystems ---
    # Field name: securityStatus (official SDE) vs security (fuzzworks)
    log.info("Importing solar systems...")
    await db.execute(text("DELETE FROM sde_systems"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "mapSolarSystems.jsonl"):
        try:
            batch.append({
                "system_id": int(item["_key"]),
                "system_name": item.get("name", {}).get("en"),
                "security": item.get("securityStatus"),
                "constellation_id": item.get("constellationID"),
                "region_id": item.get("regionID"),
            })
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDESystem.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDESystem.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} systems")

    # --- mapStargates → jump graph (replaces mapSolarSystemJumps) ---
    # Each stargate has a paired counterpart at the destination, so both directions are present.
    log.info("Importing jump edges from stargates...")
    await db.execute(text("DELETE FROM sde_jumps"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "mapStargates.jsonl"):
        try:
            dest = item["destination"]
            batch.append({
                "from_system_id": int(item["solarSystemID"]),
                "to_system_id": int(dest["solarSystemID"]),
            })
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDEJump.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEJump.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} jump edges")

    # --- stationOperations: build name lookup and cloning op set ---
    # service 10 = "Cloning" (replaces serviceID=60 in fuzzworks staOperationServices)
    log.info("Loading station operations...")
    operation_names: dict[int, str] = {}
    cloning_op_ids: set[int] = set()
    for item in _iter_jsonl(zf, "stationOperations.jsonl"):
        op_id = item["_key"]
        op_name = item.get("operationName", {}).get("en", "Station")
        operation_names[op_id] = op_name
        if 10 in item.get("services", []):
            cloning_op_ids.add(op_id)
    log.info(f"Found {len(cloning_op_ids)} operation types with cloning service")

    # --- npcStations (staStations equivalent) ---
    # Collect station data first, then fetch exact names from ESI.
    log.info("Collecting NPC station data...")
    stations_data: list[dict] = []
    for item in _iter_jsonl(zf, "npcStations.jsonl"):
        try:
            op_id = item.get("operationID")
            stations_data.append({
                "station_id": int(item["_key"]),
                "station_name": "",  # filled in by ESI below
                "system_id": int(item["solarSystemID"]),
                "has_cloning": op_id in cloning_op_ids if op_id is not None else False,
            })
        except (KeyError, ValueError):
            continue
    log.info(f"Fetching {len(stations_data)} station names from ESI...")

    # Fetch names concurrently with a semaphore to avoid overwhelming ESI.
    sem = asyncio.Semaphore(20)

    async def _fetch_name(client: httpx.AsyncClient, station_id: int) -> tuple[int, str]:
        async with sem:
            try:
                r = await client.get(f"/universe/stations/{station_id}/")
                if r.status_code == 200:
                    return station_id, r.json().get("name", "")
            except Exception:
                pass
        return station_id, ""

    async with httpx.AsyncClient(
        base_url="https://esi.evetech.net/latest",
        timeout=30,
        headers={"Accept": "application/json"},
    ) as esi:
        esi_results = await asyncio.gather(*[_fetch_name(esi, s["station_id"]) for s in stations_data])

    name_map: dict[int, str] = {sid: name for sid, name in esi_results if name}
    log.info(f"Got ESI names for {len(name_map)} of {len(stations_data)} stations")

    await db.execute(text("DELETE FROM sde_stations"))
    await db.commit()
    count, batch = 0, []
    for row in stations_data:
        row["station_name"] = name_map.get(row["station_id"], f"Station {row['station_id']}")
        batch.append(row)
        if len(batch) >= 500:
            await _bulk_insert(db, SDEStation.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEStation.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} stations")

    # --- blueprints (manufacturing materials, replaces industryActivityMaterials) ---
    log.info("Importing blueprint materials...")
    await db.execute(text("DELETE FROM sde_blueprint_materials"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "blueprints.jsonl"):
        mfg = item.get("activities", {}).get("manufacturing")
        if not mfg:
            continue
        bp_type_id = item["_key"]
        for mat in mfg.get("materials", []):
            try:
                batch.append({
                    "blueprint_type_id": int(bp_type_id),
                    "activity_id": 1,
                    "material_type_id": int(mat["typeID"]),
                    "quantity": int(mat["quantity"]),
                })
            except (KeyError, ValueError):
                continue
            if len(batch) >= 500:
                await _bulk_insert(db, SDEBlueprintMaterial.__table__, batch)
                count += len(batch)
                batch = []
    await _bulk_insert(db, SDEBlueprintMaterial.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} blueprint material rows")

    # --- blueprint info (manufacturing time + product mapping) ---
    log.info("Importing blueprint info (time + products)...")
    await db.execute(text("DELETE FROM sde_blueprint_info"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "blueprints.jsonl"):
        mfg = item.get("activities", {}).get("manufacturing")
        if not mfg:
            continue
        bp_type_id = int(item["_key"])
        time_secs = mfg.get("time")
        products = mfg.get("products", [])
        product_type_id = None
        product_qty = 1
        if products:
            product_type_id = int(products[0].get("typeID", 0)) or None
            product_qty = int(products[0].get("quantity", 1))
        if time_secs or product_type_id:
            batch.append({
                "blueprint_type_id": bp_type_id,
                "product_type_id": product_type_id,
                "manufacturing_time": time_secs,
                "product_quantity": product_qty,
            })
            if len(batch) >= 500:
                await _bulk_insert(db, SDEBlueprintInfo.__table__, batch)
                count += len(batch)
                batch = []
    await _bulk_insert(db, SDEBlueprintInfo.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} blueprint info rows")

    # --- typeMaterials (reprocessing outputs) ---
    log.info("Importing type materials (reprocessing)...")
    await db.execute(text("DELETE FROM sde_type_materials"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "typeMaterials.jsonl"):
        type_id = int(item["_key"])
        for mat in item.get("materials", []):
            try:
                batch.append({
                    "type_id": type_id,
                    "material_type_id": int(mat["materialTypeID"]),
                    "quantity": int(mat["quantity"]),
                })
            except (KeyError, ValueError):
                continue
            if len(batch) >= 1000:
                await _bulk_insert(db, SDETypeMaterial.__table__, batch)
                count += len(batch)
                batch = []
    await _bulk_insert(db, SDETypeMaterial.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} type material rows")

    # --- compressibleTypes ---
    log.info("Importing compressible types...")
    await db.execute(text("DELETE FROM sde_compressible"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "compressibleTypes.jsonl"):
        try:
            batch.append({
                "type_id": int(item["_key"]),
                "compressed_type_id": int(item["compressedTypeID"]),
            })
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDECompressible.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDECompressible.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} compressible type mappings")

    # --- groups (invGroups — for skill group organization) ---
    log.info("Importing groups...")
    await db.execute(text("DELETE FROM sde_groups"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "groups.jsonl"):
        name = item.get("name", {})
        if isinstance(name, dict):
            name = name.get("en", "")
        if not name:
            continue
        try:
            batch.append({
                "group_id": int(item["_key"]),
                "category_id": item.get("categoryID"),
                "group_name": name,
            })
        except (KeyError, ValueError):
            continue
        if len(batch) >= 500:
            await _bulk_insert(db, SDEGroup.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEGroup.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} groups")

    # --- typeDogma (skill requirements + skill metadata) ---
    # Attribute mapping for skill requirements:
    #   requiredSkill1: 182 (type_id), 277 (level)
    #   requiredSkill2: 183 (type_id), 278 (level)
    #   requiredSkill3: 184 (type_id), 279 (level)
    #   requiredSkill4: 1285 (type_id), 1286 (level)
    #   requiredSkill5: 1289 (type_id), 1287 (level)
    #   requiredSkill6: 1290 (type_id), 1288 (level)
    # Skill info: 180=primaryAttr, 181=secondaryAttr, 275=rank
    SKILL_REQ_PAIRS = [
        (182, 277), (183, 278), (184, 279),
        (1285, 1286), (1289, 1287), (1290, 1288),
    ]
    log.info("Importing typeDogma (skill requirements + skill info)...")
    await db.execute(text("DELETE FROM sde_type_skill_reqs"))
    await db.execute(text("DELETE FROM sde_skill_info"))
    await db.commit()
    req_count, info_count = 0, 0
    req_batch, info_batch = [], []
    for item in _iter_jsonl(zf, "typeDogma.jsonl"):
        type_id = int(item["_key"])
        attrs = {a["attributeID"]: a["value"] for a in item.get("dogmaAttributes", [])}

        # Extract skill requirements for this type
        for skill_attr, level_attr in SKILL_REQ_PAIRS:
            skill_id = attrs.get(skill_attr)
            level = attrs.get(level_attr)
            if skill_id and level and int(skill_id) > 0 and int(level) > 0:
                req_batch.append({
                    "type_id": type_id,
                    "skill_type_id": int(skill_id),
                    "required_level": int(level),
                })
        if len(req_batch) >= 1000:
            await _bulk_insert(db, SDETypeSkillReq.__table__, req_batch)
            req_count += len(req_batch)
            req_batch = []

        # Extract skill metadata (only for skill types: have primary + secondary attrs)
        primary = attrs.get(180)
        secondary = attrs.get(181)
        rank = attrs.get(275)
        if primary and secondary and rank:
            info_batch.append({
                "type_id": type_id,
                "primary_attr": int(primary),
                "secondary_attr": int(secondary),
                "rank": float(rank),
            })
        if len(info_batch) >= 1000:
            await _bulk_insert(db, SDESkillInfo.__table__, info_batch)
            info_count += len(info_batch)
            info_batch = []

    await _bulk_insert(db, SDETypeSkillReq.__table__, req_batch)
    req_count += len(req_batch)
    await _bulk_insert(db, SDESkillInfo.__table__, info_batch)
    info_count += len(info_batch)
    log.info(f"Imported {req_count} skill requirements, {info_count} skill info entries")

    # --- certificates ---
    log.info("Importing certificates...")
    await db.execute(text("DELETE FROM sde_certificates"))
    await db.execute(text("DELETE FROM sde_certificate_skills"))
    await db.commit()
    cert_count, cs_count = 0, 0
    cert_batch, cs_batch = [], []
    for item in _iter_jsonl(zf, "certificates.jsonl"):
        cert_id = int(item["_key"])
        name = item.get("name", {})
        if isinstance(name, dict):
            name = name.get("en", "")
        cert_batch.append({
            "certificate_id": cert_id,
            "group_id": item.get("groupID"),
            "name": name or f"Certificate {cert_id}",
        })
        if len(cert_batch) >= 500:
            await _bulk_insert(db, SDECertificate.__table__, cert_batch)
            cert_count += len(cert_batch)
            cert_batch = []

        for st in item.get("skillTypes", []):
            try:
                cs_batch.append({
                    "certificate_id": cert_id,
                    "skill_type_id": int(st["_key"]),
                    "basic": int(st.get("basic", 0)),
                    "standard": int(st.get("standard", 0)),
                    "improved": int(st.get("improved", 0)),
                    "advanced": int(st.get("advanced", 0)),
                    "elite": int(st.get("elite", 0)),
                })
            except (KeyError, ValueError):
                continue
            if len(cs_batch) >= 1000:
                await _bulk_insert(db, SDECertificateSkill.__table__, cs_batch)
                cs_count += len(cs_batch)
                cs_batch = []

    await _bulk_insert(db, SDECertificate.__table__, cert_batch)
    cert_count += len(cert_batch)
    await _bulk_insert(db, SDECertificateSkill.__table__, cs_batch)
    cs_count += len(cs_batch)
    log.info(f"Imported {cert_count} certificates, {cs_count} certificate skill entries")

    # --- masteries (ship → mastery level → certificate IDs) ---
    log.info("Importing ship masteries...")
    await db.execute(text("DELETE FROM sde_ship_masteries"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "masteries.jsonl"):
        ship_type_id = int(item["_key"])
        for level_data in item.get("_value", []):
            mastery_level = int(level_data["_key"])  # 0-4
            for cert_id in level_data.get("_value", []):
                batch.append({
                    "ship_type_id": ship_type_id,
                    "mastery_level": mastery_level,
                    "certificate_id": int(cert_id),
                })
                if len(batch) >= 1000:
                    await _bulk_insert(db, SDEShipMastery.__table__, batch)
                    count += len(batch)
                    batch = []
    await _bulk_insert(db, SDEShipMastery.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} ship mastery entries")

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
