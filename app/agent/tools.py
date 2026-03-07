"""
Tool definitions for Claude. Each tool maps to ESI API calls.
"""

TOOLS = [
    {
        "name": "get_character_location",
        "description": (
            "Get the current solar system and station/structure location of a character. "
            "Returns system_id, station_id (if docked), and structure_id (if in a player structure)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to query."},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_character_assets",
        "description": (
            "Get the full asset list for a character. Returns items with type_id, quantity, "
            "location_id, and location_flag. Use resolve_type_names to get human-readable item names. "
            "Use this to answer questions like 'where is my tritanium' or 'what ships do I have'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to query."},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_industry_jobs",
        "description": "Get active and optionally completed industry jobs for a character.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to query."},
                "include_completed": {"type": "boolean", "description": "Include completed jobs.", "default": False},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_corporation_industry_jobs",
        "description": "Get active industry jobs for the corporation of a character. Character must have corp roles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID (used to identify corporation)."},
                "include_completed": {"type": "boolean", "default": False},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_market_prices",
        "description": (
            "Get buy and sell order prices for an item in a specific region or trade hub. "
            "Common trade hubs: Jita (The Forge), Amarr (Domain), Dodixie (Sinq Laison), "
            "Hek (Metropolis), Rens (Heimatar)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {"type": "string", "description": "Name of the item to price check."},
                "region_name": {
                    "type": "string",
                    "description": "Region name. E.g. 'The Forge', 'Domain', 'Sinq Laison'.",
                    "default": "The Forge",
                },
            },
            "required": ["item_name"],
        },
    },
    {
        "name": "find_nearest_cloning_facility",
        "description": (
            "Find the nearest station with cloning services relative to a character's current location. "
            "Returns a list of nearby stations with clone services and jump distance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to base location from."},
                "max_jumps": {"type": "integer", "description": "Maximum jumps to search. Default 10.", "default": 10},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_character_clones",
        "description": "Get jump clone locations and implants for a character.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to query."},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_route",
        "description": "Calculate the route between two solar systems.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Origin system name."},
                "destination": {"type": "string", "description": "Destination system name."},
                "flag": {
                    "type": "string",
                    "enum": ["shortest", "secure", "insecure"],
                    "description": "Routing preference.",
                    "default": "shortest",
                },
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "get_system_info",
        "description": "Get information about a solar system including security status, constellation, and region.",
        "input_schema": {
            "type": "object",
            "properties": {
                "system_name": {"type": "string", "description": "Name of the solar system."},
            },
            "required": ["system_name"],
        },
    },
    {
        "name": "get_corporation_assets",
        "description": "Get assets held by the corporation. Character must have corp roles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "Character ID (used to identify corporation)."},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "get_wallet_balance",
        "description": "Get the ISK wallet balance for a character.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer", "description": "The character ID to query."},
            },
            "required": ["character_id"],
        },
    },
    {
        "name": "resolve_type_names",
        "description": "Resolve EVE type IDs to human-readable item names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of type IDs to resolve.",
                },
            },
            "required": ["type_ids"],
        },
    },
]
