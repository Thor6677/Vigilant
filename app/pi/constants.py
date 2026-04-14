"""Static PI reference data.

These values are game-static: the planet-type → P0 resource mapping has been
stable across expansions. Keeping them in-code avoids an extra SDE table for
data that never changes.

Source: EVE-Uni wiki "Planetary Commodities" and "Identifying valuable planets".
"""

# Canonical planet types (ESI planet_type string values + SDE invType ids).
# Shattered planets (2100) don't support PI — omitted.
PLANET_TYPE_NAMES: dict[int, str] = {
    11:   "Temperate",
    12:   "Ice",
    13:   "Gas",
    2014: "Oceanic",
    2015: "Lava",
    2016: "Barren",
    2017: "Storm",
    2063: "Plasma",
}

PLANET_TYPES: list[str] = [
    "Barren", "Gas", "Ice", "Lava", "Oceanic", "Plasma", "Storm", "Temperate",
]

# P0 raw materials each planet type produces (5 per planet).
P0_BY_PLANET_TYPE: dict[str, list[str]] = {
    "barren":    ["Aqueous Liquids", "Base Metals", "Carbon Compounds", "Micro Organisms", "Noble Metals"],
    "gas":       ["Aqueous Liquids", "Base Metals", "Ionic Solutions", "Noble Gas", "Reactive Gas"],
    "ice":       ["Aqueous Liquids", "Heavy Metals", "Micro Organisms", "Noble Gas", "Planktic Colonies"],
    "lava":      ["Base Metals", "Felsic Magma", "Heavy Metals", "Non-CS Crystals", "Suspended Plasma"],
    "oceanic":   ["Aqueous Liquids", "Carbon Compounds", "Complex Organisms", "Micro Organisms", "Planktic Colonies"],
    "plasma":    ["Base Metals", "Heavy Metals", "Noble Metals", "Non-CS Crystals", "Suspended Plasma"],
    "storm":     ["Aqueous Liquids", "Base Metals", "Ionic Solutions", "Noble Gas", "Suspended Plasma"],
    "temperate": ["Aqueous Liquids", "Autotrophs", "Carbon Compounds", "Complex Organisms", "Micro Organisms"],
}

# Flat list of all P0 raw material names (15 total)
P0_MATERIALS: list[str] = sorted({m for mats in P0_BY_PLANET_TYPE.values() for m in mats})

# P0 raw materials — canonical type IDs (game-stable, used as tier-0 leaves).
# Higher tiers are derived at runtime from the planetSchematics SDE tables:
# if a commodity is only produced by a schematic, its tier = max(inputs.tier) + 1.
P0_TYPE_IDS: dict[str, int] = {
    "Aqueous Liquids":    2268,
    "Autotrophs":         2305,
    "Base Metals":        2267,
    "Carbon Compounds":   2288,
    "Complex Organisms":  2287,
    "Felsic Magma":       2307,
    "Heavy Metals":       2272,
    "Ionic Solutions":    2309,
    "Micro Organisms":    2286,
    "Noble Gas":          2310,
    "Noble Metals":       2270,
    "Non-CS Crystals":    2306,
    "Planktic Colonies":  2311,
    "Reactive Gas":       2308,
    "Suspended Plasma":   2073,
}

# type_id → tier — seeded with P0 only. Non-P0 tiers are filled at chain-build
# time from the SDE schematic recipes (P1 has P0 inputs, P2 has P1 inputs, etc).
TIER_FOR_TYPE: dict[int, int] = {tid: 0 for tid in P0_TYPE_IDS.values()}

TIER_NAMES = {0: "P0 — Raw", 1: "P1 — Basic", 2: "P2 — Refined", 3: "P3 — Specialized", 4: "P4 — Advanced"}

# Pin kind labels for the planet detail partial. Actual pin classification is
# done in the route using behavior (presence of extractor_details / schematic_id /
# contents) — this is just the display dict.
PIN_GROUP_NAMES: dict[str, str] = {
    "command_center": "Command Center",
    "extractor":      "Extractor",
    "factory":        "Factory",
    "launchpad":      "Launchpad",
    "storage":        "Storage",
    "link":           "Link",
    "other":          "Structure",
}

# Type IDs for each pin "kind" as installed on a planet. Used purely for sort
# ordering — the kind is ultimately inferred from the pin's behavior fields.
# Range-based heuristic, not exhaustive:
TYPE_TO_PIN_KIND: dict[int, str] = {
    # Command centers (one per planet type)
    2254: "command_center", 2256: "command_center", 2257: "command_center",
    2273: "command_center", 2274: "command_center", 2517: "command_center",
    2524: "command_center", 2525: "command_center",
    # Extractor control units
    2848: "extractor", 3060: "extractor", 3061: "extractor", 3062: "extractor",
    3063: "extractor", 3064: "extractor", 3067: "extractor", 3068: "extractor",
    # Basic/Advanced/High-Tech Industrial Facilities
    2470: "factory", 2471: "factory", 2472: "factory", 2473: "factory", 2474: "factory",
    2475: "factory", 2481: "factory", 2483: "factory", 2484: "factory",
    # Launchpads
    2543: "launchpad", 2544: "launchpad", 2549: "launchpad", 2552: "launchpad",
    2555: "launchpad", 2556: "launchpad", 2557: "launchpad", 2558: "launchpad",
    # Storage facilities
    2535: "storage", 2536: "storage", 2537: "storage", 2538: "storage",
    2539: "storage", 2540: "storage", 2541: "storage", 2542: "storage",
}
