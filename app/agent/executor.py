"""
Executes tool calls from Claude against the ESI API.
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

# Stations known to have cloning services (NPC stations — simplified list of major hubs)
# In production this would be seeded from the SDE (Static Data Export)
CLONING_STATION_IDS = {
    60003760,  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    60008494,  # Amarr VIII (Oris) - Emperor Family Academy
    60011866,  # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
    60004588,  # Rens VI - Moon 8 - Brutor Tribe Treasury
    60005686,  # Hek VIII - Moon 12 - Boundless Creation Factory
}


async def _get_character(character_id: int, db: AsyncSession) -> tuple[Character, ESIClient]:
    result = await db.execute(select(Character).where(Character.character_id == character_id))
    char = result.scalar_one_or_none()
    if not char:
        raise ValueError(f"Character {character_id} not found in database.")
    token = await refresh_token(char, db)
    return char, ESIClient(token, db=db)


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
        system = await esi_universe.get_system(client, location["solar_system_id"])
        result = {
            "solar_system_id": location["solar_system_id"],
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
        # Resolve location names for top 20 unique locations to keep response manageable
        location_ids = list({a["location_id"] for a in assets[:100]})[:20]
        try:
            names = await esi_universe.resolve_ids(client, location_ids)
            name_map = {n["id"]: n["name"] for n in names}
        except Exception:
            name_map = {}
        for asset in assets:
            asset["location_name"] = name_map.get(asset["location_id"], str(asset["location_id"]))
        return {"total_items": len(assets), "assets": assets[:200]}

    elif tool_name == "get_industry_jobs":
        char, client = await _get_character(inp["character_id"], db)
        jobs = await esi_industry.get_character_jobs(
            client, inp["character_id"], inp.get("include_completed", False)
        )
        return {"jobs": jobs}

    elif tool_name == "get_corporation_industry_jobs":
        char, client = await _get_character(inp["character_id"], db)
        if not char.corporation_id:
            return {"error": "Character has no corporation."}
        jobs = await esi_industry.get_corporation_jobs(
            client, char.corporation_id, inp.get("include_completed", False)
        )
        return {"jobs": jobs}

    elif tool_name == "get_market_prices":
        char_result = await db.execute(select(Character).limit(1))
        any_char = char_result.scalar_one_or_none()
        client = ESIClient(any_char.access_token if any_char else "", db=db)

        item_name = inp["item_name"]
        region_name = inp.get("region_name", "The Forge").lower()
        region_id = esi_market.REGIONS.get(region_name, 10000002)

        search = await esi_universe.search_universe(client, item_name, ["inventory_type"])
        type_ids = search.get("inventory_type", [])
        if not type_ids:
            return {"error": f"Item '{item_name}' not found."}

        type_id = type_ids[0]
        type_info = await esi_universe.get_type(client, type_id)
        orders = await esi_market.get_market_orders(client, region_id, type_id=type_id)

        buy_orders = [o for o in orders if o["is_buy_order"]]
        sell_orders = [o for o in orders if not o["is_buy_order"]]

        best_buy = max((o["price"] for o in buy_orders), default=0)
        best_sell = min((o["price"] for o in sell_orders), default=0)

        return {
            "item_name": type_info.get("name", item_name),
            "type_id": type_id,
            "region": region_name.title(),
            "best_buy_price": best_buy,
            "best_sell_price": best_sell,
            "buy_orders": len(buy_orders),
            "sell_orders": len(sell_orders),
        }

    elif tool_name == "find_nearest_cloning_facility":
        char, client = await _get_character(inp["character_id"], db)
        location = await esi_char.get_location(client, inp["character_id"])
        current_system_id = location["solar_system_id"]

        # Check if already docked at cloning station
        if location.get("station_id") in CLONING_STATION_IDS:
            station = await esi_universe.get_station(client, location["station_id"])
            return {"message": "You are already docked at a cloning facility.", "station": station.get("name")}

        # Try routing to known cloning stations
        max_jumps = inp.get("max_jumps", 10)
        results = []
        for station_id in CLONING_STATION_IDS:
            try:
                station = await esi_universe.get_station(client, station_id)
                dest_system_id = station.get("system_id")
                if dest_system_id:
                    route = await esi_universe.get_route(client, current_system_id, dest_system_id)
                    jumps = len(route) - 1
                    if jumps <= max_jumps:
                        results.append({
                            "station_name": station.get("name"),
                            "system_id": dest_system_id,
                            "jumps": jumps,
                        })
            except Exception:
                continue

        results.sort(key=lambda x: x["jumps"])
        return {"nearest_cloning_facilities": results[:5]}

    elif tool_name == "get_character_clones":
        char, client = await _get_character(inp["character_id"], db)
        clones = await esi_char.get_clones(client, inp["character_id"])
        return clones

    elif tool_name == "get_route":
        char_result = await db.execute(select(Character).limit(1))
        any_char = char_result.scalar_one_or_none()
        client = ESIClient(any_char.access_token if any_char else "", db=db)

        origin_search = await esi_universe.search_universe(client, inp["origin"], ["solar_system"])
        dest_search = await esi_universe.search_universe(client, inp["destination"], ["solar_system"])

        origin_ids = origin_search.get("solar_system", [])
        dest_ids = dest_search.get("solar_system", [])
        if not origin_ids or not dest_ids:
            return {"error": "Could not find one or both systems."}

        route = await esi_universe.get_route(client, origin_ids[0], dest_ids[0], inp.get("flag", "shortest"))
        names = await esi_universe.resolve_ids(client, route)
        name_map = {n["id"]: n["name"] for n in names}
        return {
            "jumps": len(route) - 1,
            "route": [name_map.get(sid, str(sid)) for sid in route],
        }

    elif tool_name == "get_system_info":
        char_result = await db.execute(select(Character).limit(1))
        any_char = char_result.scalar_one_or_none()
        client = ESIClient(any_char.access_token if any_char else "", db=db)

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
            "star_id": system.get("star_id"),
            "planets": len(system.get("planets", [])),
        }

    elif tool_name == "get_corporation_assets":
        char, client = await _get_character(inp["character_id"], db)
        if not char.corporation_id:
            return {"error": "Character has no corporation."}
        assets = await esi_assets.get_corporation_assets(client, char.corporation_id)
        return {"total_items": len(assets), "assets": assets[:200]}

    elif tool_name == "get_wallet_balance":
        char, client = await _get_character(inp["character_id"], db)
        balance = await esi_char.get_wallet(client, inp["character_id"])
        return {"balance_isk": balance, "formatted": f"{balance:,.2f} ISK"}

    elif tool_name == "resolve_type_names":
        char_result = await db.execute(select(Character).limit(1))
        any_char = char_result.scalar_one_or_none()
        client = ESIClient(any_char.access_token if any_char else "", db=db)
        names = await esi_universe.resolve_ids(client, inp["type_ids"])
        return {"names": names}

    else:
        return {"error": f"Unknown tool: {tool_name}"}
