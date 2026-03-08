"""
Executes tool calls from Claude against the ESI API.
Uses local SDE lookups where possible, falls back to ESI.
"""
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import Character
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import assets as esi_assets
from app.esi import industry as esi_industry
from app.esi import market as esi_market
from app.esi import universe as esi_universe
from app.esi import corporation as esi_corp
from app.sde import lookup as sde


async def _get_character(character_id: int, db: AsyncSession) -> tuple[Character, ESIClient]:
    result = await db.execute(select(Character).where(Character.character_id == character_id))
    char = result.scalar_one_or_none()
    if not char:
        raise ValueError(f"Character {character_id} not found in database.")
    token = await refresh_token(char, db)
    return char, ESIClient(token, db=db)


async def _any_client(db: AsyncSession) -> ESIClient:
    result = await db.execute(select(Character).limit(1))
    char = result.scalar_one_or_none()
    return ESIClient(char.access_token if char else "", db=db)


async def execute_tool(tool_name: str, tool_input: dict, db: AsyncSession) -> str:
    """Execute a tool call and return result as JSON string."""
    try:
        result = await _dispatch(tool_name, tool_input, db)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _dispatch(tool_name: str, inp: dict, db: AsyncSession):

    if tool_name == "get_character_location":
        char, client = await _get_character(inp["character_id"], db)
        location = await esi_char.get_location(client, inp["character_id"])
        system_id = location["solar_system_id"]

        # SDE lookup first, fall back to ESI
        sys_info = await sde.system_info(db, system_id)
        if sys_info:
            result = {
                "solar_system_id": system_id,
                "solar_system_name": sys_info["system_name"],
                "security_status": sys_info["security"],
                "constellation": sys_info.get("constellation"),
                "region": sys_info.get("region"),
            }
        else:
            system = await esi_universe.get_system(client, system_id)
            result = {
                "solar_system_id": system_id,
                "solar_system_name": system.get("name"),
                "security_status": round(system.get("security_status", 0), 2),
            }

        if "station_id" in location:
            station = await esi_universe.get_station(client, location["station_id"])
            result["station_id"] = location["station_id"]
            result["station_name"] = station.get("name")
        if "structure_id" in location:
            result["structure_id"] = location["structure_id"]
            try:
                struct = await esi_universe.get_structure(client, location["structure_id"])
                result["structure_name"] = struct.get("name")
            except Exception:
                result["structure_name"] = "Unknown Structure"
        return result

    elif tool_name == "get_character_assets":
        char, client = await _get_character(inp["character_id"], db)
        assets = await esi_assets.get_character_assets(client, inp["character_id"])

        # Resolve type names via SDE first
        type_ids = list({a["type_id"] for a in assets[:200]})
        sde_names = await sde.type_ids_to_names(db, type_ids)

        # For any not in SDE, fall back to ESI resolve
        missing_ids = [tid for tid in type_ids if tid not in sde_names]
        if missing_ids:
            try:
                esi_names = await esi_universe.resolve_ids(client, missing_ids)
                for n in esi_names:
                    sde_names[n["id"]] = n["name"]
            except Exception:
                pass

        # Resolve location names
        location_ids = list({a["location_id"] for a in assets[:100]})[:20]
        try:
            loc_names_raw = await esi_universe.resolve_ids(client, location_ids)
            loc_map = {n["id"]: n["name"] for n in loc_names_raw}
        except Exception:
            loc_map = {}

        for asset in assets:
            asset["type_name"] = sde_names.get(asset["type_id"], f"Type {asset['type_id']}")
            asset["location_name"] = loc_map.get(asset["location_id"], str(asset["location_id"]))

        return {"total_items": len(assets), "assets": assets[:200]}

    elif tool_name == "find_item_in_assets":
        char, client = await _get_character(inp["character_id"], db)
        item_name = inp["item_name"]

        # Find matching type_ids from SDE
        matches = await sde.search_types(db, item_name, limit=20)
        if not matches:
            return {"error": f"No item type matching '{item_name}' found in SDE."}
        matching_type_ids = {m["type_id"] for m in matches}

        # Fetch ALL assets
        assets = await esi_assets.get_character_assets(client, inp["character_id"])

        # Filter to matching types
        found = [a for a in assets if a["type_id"] in matching_type_ids]
        if not found:
            return {
                "result": f"No '{item_name}' found in assets for {char.character_name}.",
                "total_assets_searched": len(assets),
            }

        # Resolve type names
        type_name_map = await sde.type_ids_to_names(db, list({a["type_id"] for a in found}))

        # Resolve location names — NPC stations via resolve_ids, player structures via get_structure
        location_ids = list({a["location_id"] for a in found})
        npc_ids = [lid for lid in location_ids if lid < 1_000_000_000_000]
        structure_ids = [lid for lid in location_ids if lid >= 1_000_000_000_000]

        loc_map: dict[int, dict] = {}

        if npc_ids:
            try:
                raw = await esi_universe.resolve_ids(client, npc_ids)
                for n in raw:
                    loc_map[n["id"]] = {"name": n["name"]}
            except Exception:
                pass

        for sid in structure_ids:
            try:
                struct = await esi_universe.get_structure(client, sid)
                sys_info = await sde.system_info(db, struct.get("solar_system_id", 0))
                loc_map[sid] = {
                    "name": struct.get("name", f"Structure {sid}"),
                    "system": sys_info["system_name"] if sys_info else None,
                    "security": sys_info["security"] if sys_info else None,
                    "region": sys_info.get("region") if sys_info else None,
                }
            except Exception:
                loc_map[sid] = {"name": "Unknown Structure"}

        # Enrich NPC station locations with system info from SDE
        for lid in npc_ids:
            if lid not in loc_map:
                continue
            try:
                station = await esi_universe.get_station(client, lid)
                sys_info = await sde.system_info(db, station.get("system_id", 0))
                if sys_info:
                    loc_map[lid]["system"] = sys_info["system_name"]
                    loc_map[lid]["security"] = sys_info["security"]
                    loc_map[lid]["region"] = sys_info.get("region")
            except Exception:
                pass

        results = []
        for asset in found:
            loc = loc_map.get(asset["location_id"], {})
            entry = {
                "type_name": type_name_map.get(asset["type_id"], f"Type {asset['type_id']}"),
                "quantity": asset.get("quantity", 1),
                "location_name": loc.get("name", str(asset["location_id"])),
                "location_flag": asset.get("location_flag"),
            }
            if loc.get("system"):
                entry["system"] = loc["system"]
            if loc.get("security") is not None:
                entry["security"] = loc["security"]
            if loc.get("region"):
                entry["region"] = loc["region"]
            results.append(entry)

        return {
            "character": char.character_name,
            "item_search": item_name,
            "total_assets_searched": len(assets),
            "found": len(results),
            "results": results,
        }

    elif tool_name == "get_industry_jobs":
        char, client = await _get_character(inp["character_id"], db)
        jobs = await esi_industry.get_character_jobs(
            client, inp["character_id"], inp.get("include_completed", False)
        )
        # Enrich job blueprint/product names from SDE
        for job in jobs:
            if "blueprint_type_id" in job:
                name = await sde.type_id_to_name(db, job["blueprint_type_id"])
                job["blueprint_name"] = name or f"Type {job['blueprint_type_id']}"
            if "product_type_id" in job:
                name = await sde.type_id_to_name(db, job["product_type_id"])
                job["product_name"] = name or f"Type {job['product_type_id']}"
        return {"jobs": jobs}

    elif tool_name == "get_corporation_industry_jobs":
        char, client = await _get_character(inp["character_id"], db)
        if not char.corporation_id:
            return {"error": "Character has no corporation."}
        jobs = await esi_industry.get_corporation_jobs(
            client, char.corporation_id, inp.get("include_completed", False)
        )
        for job in jobs:
            if "blueprint_type_id" in job:
                name = await sde.type_id_to_name(db, job["blueprint_type_id"])
                job["blueprint_name"] = name or f"Type {job['blueprint_type_id']}"
            if "product_type_id" in job:
                name = await sde.type_id_to_name(db, job["product_type_id"])
                job["product_name"] = name or f"Type {job['product_type_id']}"
        return {"jobs": jobs}

    elif tool_name == "get_market_prices":
        client = await _any_client(db)
        item_name = inp["item_name"]
        region_name = inp.get("region_name", "The Forge").lower()
        region_id = esi_market.REGIONS.get(region_name, 10000002)

        # Try SDE lookup first
        type_id = await sde.type_name_to_id(db, item_name)
        if type_id is None:
            # Partial SDE search
            matches = await sde.search_types(db, item_name, limit=5)
            if matches:
                type_id = matches[0]["type_id"]
                item_name = matches[0]["type_name"]
        if type_id is None:
            # Fall back to ESI search
            search = await esi_universe.search_universe(client, item_name, ["inventory_type"])
            type_ids = search.get("inventory_type", [])
            if not type_ids:
                return {"error": f"Item '{item_name}' not found."}
            type_id = type_ids[0]
            type_info = await esi_universe.get_type(client, type_id)
            item_name = type_info.get("name", item_name)
        else:
            confirmed_name = await sde.type_id_to_name(db, type_id)
            item_name = confirmed_name or item_name

        orders = await esi_market.get_market_orders(client, region_id, type_id=type_id)
        buy_orders = [o for o in orders if o["is_buy_order"]]
        sell_orders = [o for o in orders if not o["is_buy_order"]]

        return {
            "item_name": item_name,
            "type_id": type_id,
            "region": region_name.title(),
            "best_buy_price": max((o["price"] for o in buy_orders), default=0),
            "best_sell_price": min((o["price"] for o in sell_orders), default=0),
            "buy_orders": len(buy_orders),
            "sell_orders": len(sell_orders),
        }

    elif tool_name == "find_nearest_cloning_facility":
        char, client = await _get_character(inp["character_id"], db)
        location = await esi_char.get_location(client, inp["character_id"])
        current_system_id = location["solar_system_id"]
        max_jumps = inp.get("max_jumps", 15)

        # Use full BFS via SDE if loaded
        if await sde.sde_is_loaded(db):
            results = await sde.nearest_cloning_facilities(db, current_system_id, max_jumps=max_jumps)
            if results:
                return {"nearest_cloning_facilities": results}

        # ESI fallback
        return {"error": "SDE not loaded yet. Please try again in a few minutes while the database initialises."}

    elif tool_name == "get_character_clones":
        char, client = await _get_character(inp["character_id"], db)
        clones = await esi_char.get_clones(client, inp["character_id"])
        return clones

    elif tool_name == "get_route":
        client = await _any_client(db)

        # Resolve system names via SDE
        origin_id = await sde.system_name_to_id(db, inp["origin"])
        dest_id = await sde.system_name_to_id(db, inp["destination"])

        # Fall back to ESI search if not found
        if not origin_id:
            s = await esi_universe.search_universe(client, inp["origin"], ["solar_system"])
            ids = s.get("solar_system", [])
            origin_id = ids[0] if ids else None
        if not dest_id:
            s = await esi_universe.search_universe(client, inp["destination"], ["solar_system"])
            ids = s.get("solar_system", [])
            dest_id = ids[0] if ids else None

        if not origin_id or not dest_id:
            return {"error": "Could not find one or both systems."}

        route = await esi_universe.get_route(client, origin_id, dest_id, inp.get("flag", "shortest"))

        # Resolve system names from SDE
        route_names = []
        for sid in route:
            info = await sde.system_info(db, sid)
            route_names.append(info["system_name"] if info else str(sid))

        return {"jumps": len(route) - 1, "route": route_names}

    elif tool_name == "get_system_info":
        # Try SDE first
        system_id = await sde.system_name_to_id(db, inp["system_name"])
        if system_id:
            info = await sde.system_info(db, system_id)
            if info:
                return info

        # ESI fallback
        client = await _any_client(db)
        search = await esi_universe.search_universe(client, inp["system_name"], ["solar_system"])
        ids = search.get("solar_system", [])
        if not ids:
            return {"error": f"System '{inp['system_name']}' not found."}
        system = await esi_universe.get_system(client, ids[0])
        constellation = await esi_universe.get_constellation(client, system["constellation_id"])
        region = await esi_universe.get_region(client, constellation["region_id"])
        return {
            "name": system["name"],
            "security_status": round(system["security_status"], 2),
            "constellation": constellation["name"],
            "region": region["name"],
            "planets": len(system.get("planets", [])),
        }

    elif tool_name == "get_corporation_assets":
        char, client = await _get_character(inp["character_id"], db)
        if not char.corporation_id:
            return {"error": "Character has no corporation."}
        assets = await esi_assets.get_corporation_assets(client, char.corporation_id)
        type_ids = list({a["type_id"] for a in assets[:200]})
        sde_names = await sde.type_ids_to_names(db, type_ids)
        for asset in assets:
            asset["type_name"] = sde_names.get(asset["type_id"], f"Type {asset['type_id']}")
        return {"total_items": len(assets), "assets": assets[:200]}

    elif tool_name == "get_wallet_balance":
        char, client = await _get_character(inp["character_id"], db)
        balance = await esi_char.get_wallet(client, inp["character_id"])
        return {"balance_isk": balance, "formatted": f"{balance:,.2f} ISK"}

    elif tool_name == "search_item_types":
        matches = await sde.search_types(db, inp["query"], limit=10)
        if not matches:
            return {"result": f"No EVE item types match '{inp['query']}'."}
        return {"matches": matches}

    elif tool_name == "resolve_type_names":
        sde_names = await sde.type_ids_to_names(db, inp["type_ids"])
        missing = [tid for tid in inp["type_ids"] if tid not in sde_names]
        if missing:
            client = await _any_client(db)
            try:
                esi_names = await esi_universe.resolve_ids(client, missing)
                for n in esi_names:
                    sde_names[n["id"]] = n["name"]
            except Exception:
                pass
        return {"names": [{"id": tid, "name": sde_names.get(tid, f"Type {tid}")} for tid in inp["type_ids"]]}

    else:
        return {"error": f"Unknown tool: {tool_name}"}
