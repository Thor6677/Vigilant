"""
Downloads and imports EVE SDE data from the official CCP source into local SQLite.
Only re-downloads if data is older than 30 days.
"""
import asyncio
import io
import json
import logging
import math
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
    SDEPlanet, SDEPlanetSchematic, SDEPlanetSchematicMaterial,
    SDEWormholeClass, SDEWormholeType, SDEMoon, SDEStar,
    SDEDogmaAttribute, SDETypeDogmaAttribute, SDEModuleSlot,
)

# Planet type IDs that support PI (shattered / exotic types excluded).
PI_PLANET_TYPE_IDS = {11, 12, 13, 2014, 2015, 2016, 2017, 2063}

# Wormhole type dogma attribute IDs
WH_ATTR_TARGET_CLASS = 1381
WH_ATTR_MAX_STABLE_TIME = 1382   # lifetime in minutes (despite the ID ordering)
WH_ATTR_MAX_STABLE_MASS = 1383   # total mass in kg
WH_ATTR_MASS_REGEN = 1384
WH_ATTR_MAX_JUMP_MASS = 1385
WH_GROUP_ID = 988  # invGroups group for "Wormhole" items

# Dogma effect IDs that determine module slot type
EFFECT_LO_POWER = 11
EFFECT_HI_POWER = 12
EFFECT_MED_POWER = 13
EFFECT_RIG_SLOT = 2663
EFFECT_SUBSYSTEM_SLOT = 3772
EFFECT_TURRET_FITTED = 42
EFFECT_LAUNCHER_FITTED = 40

SLOT_EFFECT_MAP = {
    EFFECT_HI_POWER: "high",
    EFFECT_MED_POWER: "mid",
    EFFECT_LO_POWER: "low",
    EFFECT_RIG_SLOT: "rig",
    EFFECT_SUBSYSTEM_SLOT: "subsystem",
}


def _roman(n: int) -> str:
    """Convert integer to Roman numeral (used for planet names: 'Jita IV')."""
    if n is None or n <= 0:
        return ""
    mapping = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
               (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
               (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for val, sym in mapping:
        while n >= val:
            out += sym
            n -= val
    return out

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
    if datetime.now(timezone.utc) - updated > timedelta(days=REFRESH_DAYS):
        return True

    # Also force update if any NEW table (added after last import) is still empty
    # while the base sde_types is populated. This catches additions like the PI
    # tables without requiring manual `DELETE FROM sde_meta` steps.
    try:
        types_result = await db.execute(text("SELECT COUNT(1) FROM sde_types"))
        types_count = types_result.scalar() or 0
        if types_count > 0:
            for table in ("sde_planets", "sde_planet_schematics", "sde_wormhole_classes", "sde_wormhole_types", "sde_moons", "sde_stars", "sde_dogma_attributes", "sde_type_dogma_attrs", "sde_module_slots"):
                try:
                    r = await db.execute(text(f"SELECT COUNT(1) FROM {table}"))
                    if (r.scalar() or 0) == 0:
                        log.info(f"{table} is empty but sde_types is populated — forcing SDE reimport.")
                        return True
                except Exception:
                    # Table doesn't exist yet — create_all will make it, next startup will populate
                    return True
    except Exception:
        pass
    return False


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

    # Build set of published type IDs for filtering dogma data later
    pub_result = await db.execute(text("SELECT type_id FROM sde_types"))
    published_type_ids: set[int] = {row[0] for row in pub_result.fetchall()}
    log.info(f"Built published type set: {len(published_type_ids)} types")

    # --- mapRegions ---
    log.info("Importing regions...")
    await db.execute(text("DELETE FROM sde_regions"))
    await db.commit()
    count, batch = 0, []
    wh_class_batch: list[dict] = []  # collect wormholeClassID from regions, constellations, systems
    for item in _iter_jsonl(zf, "mapRegions.jsonl"):
        name = item.get("name", {}).get("en")
        if not name:
            continue
        try:
            rid = int(item["_key"])
            batch.append({"region_id": rid, "region_name": name})
            wh_class = item.get("wormholeClassID")
            if wh_class is not None:
                wh_class_batch.append({"location_id": rid, "wormhole_class_id": int(wh_class)})
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
            cid = int(item["_key"])
            batch.append({
                "constellation_id": cid,
                "constellation_name": name,
                "region_id": item.get("regionID"),
            })
            wh_class = item.get("wormholeClassID")
            if wh_class is not None:
                wh_class_batch.append({"location_id": cid, "wormhole_class_id": int(wh_class)})
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
    # Also extract wormholeClassID for the wormhole classes table.
    log.info("Importing solar systems...")
    await db.execute(text("DELETE FROM sde_systems"))
    await db.commit()
    count, batch = 0, []
    for item in _iter_jsonl(zf, "mapSolarSystems.jsonl"):
        try:
            sys_id = int(item["_key"])
            batch.append({
                "system_id": sys_id,
                "system_name": item.get("name", {}).get("en"),
                "security": item.get("securityStatus"),
                "constellation_id": item.get("constellationID"),
                "region_id": item.get("regionID"),
            })
            # Collect wormhole class if present
            wh_class = item.get("wormholeClassID")
            if wh_class is not None:
                wh_class_batch.append({
                    "location_id": sys_id,
                    "wormhole_class_id": int(wh_class),
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

    # Insert wormhole class mappings collected during system import
    if wh_class_batch:
        await db.execute(text("DELETE FROM sde_wormhole_classes"))
        await db.commit()
        wh_cls_count = 0
        for i in range(0, len(wh_class_batch), 500):
            chunk = wh_class_batch[i:i+500]
            await _bulk_insert(db, SDEWormholeClass.__table__, chunk)
            wh_cls_count += len(chunk)
        log.info(f"Imported {wh_cls_count} wormhole class mappings (from mapSolarSystems)")

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

    # --- backfill category_id on sde_types from sde_groups ---
    log.info("Backfilling category_id on types...")
    await db.execute(text(
        "UPDATE sde_types SET category_id = ("
        "  SELECT category_id FROM sde_groups WHERE sde_groups.group_id = sde_types.group_id"
        ") WHERE group_id IS NOT NULL"
    ))
    await db.commit()
    log.info("Backfilled category_id on types")

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
    log.info("Importing typeDogma (skill requirements + skill info + all dogma attrs + module slots)...")
    await db.execute(text("DELETE FROM sde_type_skill_reqs"))
    await db.execute(text("DELETE FROM sde_skill_info"))
    await db.execute(text("DELETE FROM sde_type_dogma_attrs"))
    await db.execute(text("DELETE FROM sde_module_slots"))
    await db.commit()
    req_count, info_count, dogma_count, slot_count = 0, 0, 0, 0
    req_batch, info_batch, dogma_batch, slot_batch = [], [], [], []
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

        # Store ALL dogma attributes for published types (fitting tool)
        if type_id in published_type_ids:
            for a in item.get("dogmaAttributes", []):
                try:
                    dogma_batch.append({
                        "type_id": type_id,
                        "attribute_id": int(a["attributeID"]),
                        "value": float(a["value"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            if len(dogma_batch) >= 5000:
                await _bulk_insert(db, SDETypeDogmaAttribute.__table__, dogma_batch)
                dogma_count += len(dogma_batch)
                dogma_batch = []

            # Extract module slot type from dogma effects
            effects = {e["effectID"] for e in item.get("dogmaEffects", [])}
            slot_type = None
            for eff_id, stype in SLOT_EFFECT_MAP.items():
                if eff_id in effects:
                    slot_type = stype
                    break
            if slot_type:
                slot_batch.append({
                    "type_id": type_id,
                    "slot_type": slot_type,
                    "is_turret": EFFECT_TURRET_FITTED in effects,
                    "is_launcher": EFFECT_LAUNCHER_FITTED in effects,
                })
                if len(slot_batch) >= 1000:
                    await _bulk_insert(db, SDEModuleSlot.__table__, slot_batch)
                    slot_count += len(slot_batch)
                    slot_batch = []

    await _bulk_insert(db, SDETypeSkillReq.__table__, req_batch)
    req_count += len(req_batch)
    await _bulk_insert(db, SDESkillInfo.__table__, info_batch)
    info_count += len(info_batch)
    await _bulk_insert(db, SDETypeDogmaAttribute.__table__, dogma_batch)
    dogma_count += len(dogma_batch)
    await _bulk_insert(db, SDEModuleSlot.__table__, slot_batch)
    slot_count += len(slot_batch)
    log.info(f"Imported {req_count} skill requirements, {info_count} skill info entries")
    log.info(f"Imported {dogma_count} type dogma attributes, {slot_count} module slot entries")

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

    # --- mapPlanets → planets supporting PI ---
    # Fetch system name map first so we can materialize "Jita IV"-style planet names at load time.
    log.info("Importing planets...")
    sys_name_result = await db.execute(text("SELECT system_id, system_name FROM sde_systems"))
    sys_name_map: dict[int, str] = {row[0]: row[1] for row in sys_name_result.fetchall()}

    await db.execute(text("DELETE FROM sde_planets"))
    await db.commit()
    count, batch = 0, []
    try:
        for item in _iter_jsonl(zf, "mapPlanets.jsonl"):
            try:
                type_id = int(item.get("typeID") or 0)
                if type_id == 0:
                    continue
                system_id = int(item["solarSystemID"])
                idx = int(item.get("celestialIndex") or 0)
                sys_name = sys_name_map.get(system_id, f"System {system_id}")
                planet_name = f"{sys_name} {_roman(idx)}".strip() if idx else sys_name
                # Calculate orbital distance from star in AU
                pos = item.get("position", {})
                px, py, pz = pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)
                dist_m = math.sqrt(px*px + py*py + pz*pz)
                dist_au = round(dist_m / 149_597_870_700, 2) if dist_m > 0 else None
                batch.append({
                    "planet_id": int(item["_key"]),
                    "system_id": system_id,
                    "planet_type_id": type_id,
                    "planet_name": planet_name,
                    "planet_index": idx,
                    "radius": item.get("radius"),
                    "distance_au": dist_au,
                })
            except (KeyError, ValueError, TypeError):
                continue
            if len(batch) >= 1000:
                await _bulk_insert(db, SDEPlanet.__table__, batch)
                count += len(batch)
                batch = []
        await _bulk_insert(db, SDEPlanet.__table__, batch)
        count += len(batch)
        log.info(f"Imported {count} planets")
    except KeyError:
        log.warning("mapPlanets.jsonl not present in SDE zip — skipping planet import")

    # --- planetSchematics → PI recipes (name, cycle time, inputs/outputs) ---
    log.info("Importing PI schematics...")
    await db.execute(text("DELETE FROM sde_planet_schematics"))
    await db.execute(text("DELETE FROM sde_planet_schematic_materials"))
    await db.commit()
    sch_count, mat_count = 0, 0
    sch_batch, mat_batch = [], []
    try:
        for item in _iter_jsonl(zf, "planetSchematics.jsonl"):
            try:
                sid = int(item["_key"])
                name = item.get("name", {})
                if isinstance(name, dict):
                    name = name.get("en") or f"Schematic {sid}"
                sch_batch.append({
                    "schematic_id": sid,
                    "schematic_name": name,
                    "cycle_time": item.get("cycleTime"),
                })
                for t in item.get("types", []):
                    try:
                        mat_batch.append({
                            "schematic_id": sid,
                            "type_id": int(t["_key"]),
                            "quantity": int(t.get("quantity") or 0),
                            "is_input": bool(t.get("isInput")),
                        })
                    except (KeyError, ValueError):
                        continue
            except (KeyError, ValueError):
                continue
            if len(sch_batch) >= 200:
                await _bulk_insert(db, SDEPlanetSchematic.__table__, sch_batch)
                sch_count += len(sch_batch)
                sch_batch = []
            if len(mat_batch) >= 500:
                await _bulk_insert(db, SDEPlanetSchematicMaterial.__table__, mat_batch)
                mat_count += len(mat_batch)
                mat_batch = []
        await _bulk_insert(db, SDEPlanetSchematic.__table__, sch_batch)
        sch_count += len(sch_batch)
        await _bulk_insert(db, SDEPlanetSchematicMaterial.__table__, mat_batch)
        mat_count += len(mat_batch)
        log.info(f"Imported {sch_count} PI schematics with {mat_count} material rows")
    except KeyError:
        log.warning("planetSchematics.jsonl not present in SDE zip — skipping")

    # --- mapMoons → moon data (for moon counts per planet) ---
    log.info("Importing moons...")
    await db.execute(text("DELETE FROM sde_moons"))
    await db.commit()
    count, batch = 0, []
    try:
        for item in _iter_jsonl(zf, "mapMoons.jsonl"):
            try:
                batch.append({
                    "moon_id": int(item["_key"]),
                    "planet_id": int(item.get("orbitID") or item.get("planetID") or 0),
                    "system_id": int(item.get("solarSystemID") or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue
            if len(batch) >= 1000:
                await _bulk_insert(db, SDEMoon.__table__, batch)
                count += len(batch)
                batch = []
        await _bulk_insert(db, SDEMoon.__table__, batch)
        count += len(batch)
        log.info(f"Imported {count} moons")
    except KeyError:
        log.warning("mapMoons.jsonl not present in SDE zip — skipping moon import")

    # --- mapStars → star data per system ---
    # Pre-build star type name lookup (star types are unpublished in SDE)
    log.info("Importing stars...")
    star_type_names: dict[int, str] = {}
    for item in _iter_jsonl(zf, "types.jsonl"):
        gid = item.get("groupID")
        # Star groups: 6 (Sun), various star type groups
        if gid in (6,) or (item.get("name", {}).get("en", "").startswith("Sun ")):
            star_type_names[int(item["_key"])] = item.get("name", {}).get("en", "Star")
    log.info(f"Found {len(star_type_names)} star type names")

    await db.execute(text("DELETE FROM sde_stars"))
    await db.commit()
    count, batch = 0, []
    try:
        for item in _iter_jsonl(zf, "mapStars.jsonl"):
            try:
                tid = int(item.get("typeID") or 0) or None
                batch.append({
                    "system_id": int(item.get("solarSystemID") or item.get("_key")),
                    "type_id": tid,
                    "star_name": star_type_names.get(tid) if tid else None,
                })
            except (KeyError, ValueError, TypeError):
                continue
            if len(batch) >= 500:
                await _bulk_insert(db, SDEStar.__table__, batch)
                count += len(batch)
                batch = []
        await _bulk_insert(db, SDEStar.__table__, batch)
        count += len(batch)
        log.info(f"Imported {count} stars")
    except KeyError:
        log.warning("mapStars.jsonl not present in SDE zip — skipping star import")

    # --- Wormhole types from types.jsonl (group 988) + typeDogma attributes ---
    log.info("Importing wormhole types...")
    await db.execute(text("DELETE FROM sde_wormhole_types"))
    await db.commit()

    # First pass: collect wormhole type IDs from types.jsonl
    # Note: wormhole types are all published=false in the SDE, so we skip that check
    wh_type_ids: set[int] = set()
    wh_type_names: dict[int, str] = {}
    for item in _iter_jsonl(zf, "types.jsonl"):
        if item.get("groupID") == WH_GROUP_ID:
            tid = int(item["_key"])
            wh_type_ids.add(tid)
            wh_type_names[tid] = item.get("name", {}).get("en", f"Wormhole {tid}")

    # Second pass: extract dogma attributes for wormhole types
    wh_attrs: dict[int, dict] = {}  # type_id -> {attr_id: value}
    for item in _iter_jsonl(zf, "typeDogma.jsonl"):
        tid = int(item["_key"])
        if tid not in wh_type_ids:
            continue
        attrs = {a["attributeID"]: a["value"] for a in item.get("dogmaAttributes", [])}
        wh_attrs[tid] = attrs

    count, batch = 0, []
    for tid in wh_type_ids:
        attrs = wh_attrs.get(tid, {})
        batch.append({
            "type_id": tid,
            "type_name": wh_type_names.get(tid, f"Wormhole {tid}"),
            "target_class": attrs.get(WH_ATTR_TARGET_CLASS),
            "max_stable_mass": attrs.get(WH_ATTR_MAX_STABLE_MASS),
            "max_stable_time": attrs.get(WH_ATTR_MAX_STABLE_TIME),
            "mass_regen": attrs.get(WH_ATTR_MASS_REGEN),
            "max_jump_mass": attrs.get(WH_ATTR_MAX_JUMP_MASS),
        })
        if len(batch) >= 500:
            await _bulk_insert(db, SDEWormholeType.__table__, batch)
            count += len(batch)
            batch = []
    await _bulk_insert(db, SDEWormholeType.__table__, batch)
    count += len(batch)
    log.info(f"Imported {count} wormhole types with dogma attributes")

    # --- dogmaAttributes (attribute definitions for fitting tool) ---
    log.info("Importing dogma attribute definitions...")
    await db.execute(text("DELETE FROM sde_dogma_attributes"))
    await db.commit()
    count, batch = 0, []
    try:
        for item in _iter_jsonl(zf, "dogmaAttributes.jsonl"):
            try:
                attr_id = int(item["_key"])
                name = item.get("name", "")
                display = item.get("displayName")
                if isinstance(display, dict):
                    display = display.get("en")
                batch.append({
                    "attribute_id": attr_id,
                    "attribute_name": name,
                    "display_name": display,
                    "default_value": item.get("defaultValue"),
                    "stackable": bool(item.get("stackable", True)),
                    "high_is_good": bool(item.get("highIsGood", True)),
                    "unit_id": item.get("unitID"),
                })
            except (KeyError, ValueError, TypeError):
                continue
            if len(batch) >= 500:
                await _bulk_insert(db, SDEDogmaAttribute.__table__, batch)
                count += len(batch)
                batch = []
        await _bulk_insert(db, SDEDogmaAttribute.__table__, batch)
        count += len(batch)
        log.info(f"Imported {count} dogma attribute definitions")
    except KeyError:
        log.warning("dogmaAttributes.jsonl not present in SDE zip — skipping")

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
