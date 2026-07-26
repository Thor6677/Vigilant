"""Hauling calculator — ship capacity, trip calculation, item categorization."""

import math
import re

# ── Hauling ship database ─────────────────────────────────────────────────────
# Base capacities from EVE Online (verified against EVE University wiki).
# Values are base (with role bonuses applied, before per-level skill bonuses).
# "expandable" bays are affected by Expanded Cargohold modules and rigs.

HAULING_SHIPS = {
    # ── T1 Industrials ────────────────────────────────────────────────────────
    648: {
        "name": "Badger",
        "group": "Industrial",
        "bays": {"cargo": {"base": 3900, "expandable": True}},
        "low_slots": 4, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    1944: {
        "name": "Bestower",
        "group": "Industrial",
        "bays": {"cargo": {"base": 4800, "expandable": True}},
        "low_slots": 6, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    650: {
        "name": "Nereus",
        "group": "Industrial",
        "bays": {"cargo": {"base": 2700, "expandable": True}},
        "low_slots": 5, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    3871: {
        "name": "Sigil",
        "group": "Industrial",
        "bays": {"cargo": {"base": 2100, "expandable": True}},
        "low_slots": 6, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    652: {
        "name": "Tayra",
        "group": "Industrial",
        "bays": {"cargo": {"base": 7300, "expandable": True}},
        "low_slots": 4, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    651: {
        "name": "Wreathe",
        "group": "Industrial",
        "bays": {"cargo": {"base": 2900, "expandable": True}},
        "low_slots": 5, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    654: {
        "name": "Mammoth",
        "group": "Industrial",
        "bays": {"cargo": {"base": 5500, "expandable": True}},
        "low_slots": 5, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    2530: {
        "name": "Iteron Mark V",
        "group": "Industrial",
        "bays": {"cargo": {"base": 5800, "expandable": True}},
        "low_slots": 5, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },

    # ── Specialized Industrials ───────────────────────────────────────────────
    653: {
        "name": "Hoarder",
        "group": "Industrial",
        "bays": {
            "cargo": {"base": 500, "expandable": True},
            "ammo": {"base": 41000, "expandable": False},
            "gas": {"base": 30000, "expandable": False},
        },
        "low_slots": 3, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.10, "bay": "ammo"},
        "extra_skill_bonus": [
            {"per_level": 0.10, "bay": "gas"},
        ],
    },
    2998: {
        "name": "Epithal",
        "group": "Industrial",
        "bays": {
            "cargo": {"base": 550, "expandable": True},
            "planetary": {"base": 45000, "expandable": False},
            "command_center": {"base": 6000, "expandable": False},
        },
        "low_slots": 4, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.10, "bay": "planetary"},
    },
    2999: {
        "name": "Kryos",
        "group": "Industrial",
        "bays": {
            "cargo": {"base": 550, "expandable": True},
            "mineral": {"base": 50000, "expandable": False},
            "ice": {"base": 30000, "expandable": False},
        },
        "low_slots": 4, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.10, "bay": "mineral"},
        "extra_skill_bonus": [
            {"per_level": 0.10, "bay": "ice"},
        ],
    },
    3000: {
        "name": "Miasmos",
        "group": "Industrial",
        "bays": {
            "cargo": {"base": 550, "expandable": True},
            "ore": {"base": 42000, "expandable": False},
        },
        "low_slots": 4, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.10, "bay": "ore"},
    },

    # ── Deep Space Transports ─────────────────────────────────────────────────
    11190: {
        "name": "Occator",
        "group": "Deep Space Transport",
        "bays": {
            "cargo": {"base": 3900, "expandable": True},
            "fleet_hangar": {"base": 50000, "expandable": False},
        },
        "low_slots": 6, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "fleet_hangar"},
    },
    11196: {
        "name": "Mastodon",
        "group": "Deep Space Transport",
        "bays": {
            "cargo": {"base": 4500, "expandable": True},
            "fleet_hangar": {"base": 50000, "expandable": False},
        },
        "low_slots": 4, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "fleet_hangar"},
    },
    11184: {
        "name": "Impel",
        "group": "Deep Space Transport",
        "bays": {
            "cargo": {"base": 3100, "expandable": True},
            "fleet_hangar": {"base": 50000, "expandable": False},
        },
        "low_slots": 7, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "fleet_hangar"},
    },
    11198: {
        "name": "Bustard",
        "group": "Deep Space Transport",
        "bays": {
            "cargo": {"base": 5000, "expandable": True},
            "fleet_hangar": {"base": 50000, "expandable": False},
        },
        "low_slots": 3, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "fleet_hangar"},
    },

    # ── Blockade Runners ──────────────────────────────────────────────────────
    11188: {
        "name": "Viator",
        "group": "Blockade Runner",
        "bays": {"cargo": {"base": 3600, "expandable": True}},
        "low_slots": 3, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    11194: {
        "name": "Crane",
        "group": "Blockade Runner",
        "bays": {"cargo": {"base": 4300, "expandable": True}},
        "low_slots": 2, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    11186: {
        "name": "Prorator",
        "group": "Blockade Runner",
        "bays": {"cargo": {"base": 2900, "expandable": True}},
        "low_slots": 4, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    11192: {
        "name": "Prowler",
        "group": "Blockade Runner",
        "bays": {"cargo": {"base": 3500, "expandable": True}},
        "low_slots": 3, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },

    # ── Freighters ────────────────────────────────────────────────────────────
    # 3 low slots, 0 rig slots. Use Capital Expanded Cargohold modules.
    20183: {
        "name": "Providence",
        "group": "Freighter",
        "bays": {"cargo": {"base": 435000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    20187: {
        "name": "Obelisk",
        "group": "Freighter",
        "bays": {"cargo": {"base": 440000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    20189: {
        "name": "Fenrir",
        "group": "Freighter",
        "bays": {"cargo": {"base": 435000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    20185: {
        "name": "Charon",
        "group": "Freighter",
        "bays": {"cargo": {"base": 465000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },

    # ── Jump Freighters ───────────────────────────────────────────────────────
    # 3 low slots, 0 rig slots. Cargo bonus from racial freighter skill.
    28848: {
        "name": "Nomad",
        "group": "Jump Freighter",
        "bays": {"cargo": {"base": 165000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    28850: {
        "name": "Anshar",
        "group": "Jump Freighter",
        "bays": {"cargo": {"base": 171000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    28846: {
        "name": "Ark",
        "group": "Jump Freighter",
        "bays": {"cargo": {"base": 168800, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },
    28844: {
        "name": "Rhea",
        "group": "Jump Freighter",
        "bays": {"cargo": {"base": 180000, "expandable": True}},
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
    },

    # ── Industrial Command Ships ──────────────────────────────────────────────
    28606: {
        "name": "Orca",
        "group": "Industrial Command Ship",
        "bays": {
            "cargo": {"base": 30000, "expandable": True},
            "fleet_hangar": {"base": 40000, "expandable": False},
            "ore_hold": {"base": 150000, "expandable": False},
            "ship_maintenance": {"base": 400000, "expandable": False},
        },
        "low_slots": 2, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
        "extra_skill_bonus": [
            {"per_level": 0.05, "bay": "ore_hold"},
        ],
    },
    33515: {
        "name": "Porpoise",
        "group": "Industrial Command Ship",
        "bays": {
            "cargo": {"base": 500, "expandable": True},
            "fleet_hangar": {"base": 5000, "expandable": False},
            "ore_hold": {"base": 50000, "expandable": False},
        },
        "low_slots": 2, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.05, "bay": "cargo"},
        "extra_skill_bonus": [
            {"per_level": 0.05, "bay": "ore_hold"},
        ],
    },

    # ── Upwell Haulers ────────────────────────────────────────────────────────
    81008: {
        "name": "Squall",
        "group": "Upwell Industrial",
        "bays": {
            "cargo": {"base": 3000, "expandable": True},
            "infrastructure": {"base": 45000, "expandable": False},
        },
        "low_slots": 3, "rig_slots": 3,
        "skill_bonus": {"per_level": 0.10, "bay": "infrastructure"},
    },
    81047: {
        "name": "Torrent",
        "group": "Upwell Deep Space Transport",
        "bays": {
            "cargo": {"base": 3000, "expandable": True},
            "infrastructure": {"base": 60000, "expandable": False},
            "fleet_hangar": {"base": 30000, "expandable": False},
        },
        "low_slots": 3, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.10, "bay": "infrastructure"},
        "extra_skill_bonus": [
            {"per_level": 0.05, "bay": "fleet_hangar"},
        ],
    },
    81046: {
        "name": "Deluge",
        "group": "Upwell Blockade Runner",
        "bays": {
            "cargo": {"base": 4000, "expandable": True},
            "infrastructure": {"base": 30000, "expandable": False},
        },
        "low_slots": 2, "rig_slots": 2,
        "skill_bonus": {"per_level": 0.10, "bay": "infrastructure"},
    },
    81040: {
        "name": "Avalanche",
        "group": "Upwell Freighter",
        "bays": {
            "cargo": {"base": 205000, "expandable": True},
            "infrastructure": {"base": 2000000, "expandable": False},
        },
        "low_slots": 3, "rig_slots": 0,
        "skill_bonus": {"per_level": 0.10, "bay": "infrastructure"},
    },
}

# Build sorted ship list for template rendering
SHIP_GROUPS_ORDER = [
    "Industrial", "Deep Space Transport", "Blockade Runner",
    "Freighter", "Jump Freighter", "Industrial Command Ship",
    "Upwell Industrial", "Upwell Deep Space Transport",
    "Upwell Blockade Runner", "Upwell Freighter",
]


def get_ships_by_group() -> dict[str, list[dict]]:
    """Return ships organized by group for the template dropdown."""
    groups: dict[str, list[dict]] = {g: [] for g in SHIP_GROUPS_ORDER}
    for type_id, ship in HAULING_SHIPS.items():
        g = ship["group"]
        if g not in groups:
            groups[g] = []
        groups[g].append({"type_id": type_id, **ship})
    for g in groups:
        groups[g].sort(key=lambda s: s["name"])
    return {g: ships for g, ships in groups.items() if ships}


# ── Bay display names ─────────────────────────────────────────────────────────

BAY_LABELS = {
    "cargo": "Cargo Hold",
    "fleet_hangar": "Fleet Hangar",
    "ore": "Ore Hold",
    "ore_hold": "Ore Hold",
    "mineral": "Mineral Hold",
    "ice": "Ice Hold",
    "planetary": "Planetary Commodities Hold",
    "command_center": "Command Center Hold",
    "ammo": "Ammo Hold",
    "gas": "Gas Hold",
    "ship_maintenance": "Ship Maintenance Bay",
    "infrastructure": "Infrastructure Hold",
}

# ── Cargo modification modules and rigs ───────────────────────────────────────

CARGO_MODULES = {
    "none":       {"label": "None",                        "bonus": 0.0},
    "t1":         {"label": "Expanded Cargohold I",        "bonus": 0.175},
    "compact":    {"label": "Compact Expanded Cargohold",  "bonus": 0.200},
    "restrained": {"label": "Restrained Exp. Cargohold",   "bonus": 0.150},
    "t2":         {"label": "Expanded Cargohold II",       "bonus": 0.275},
    "cap_t1":     {"label": "Capital Exp. Cargohold I",    "bonus": 0.185},
    "cap_t2":     {"label": "Capital Exp. Cargohold II",   "bonus": 0.28},
}

CARGO_RIGS = {
    "none": {"label": "None",                       "bonus": 0.0},
    "t1":   {"label": "Cargohold Optimization I",   "bonus": 0.15},
    "t2":   {"label": "Cargohold Optimization II",  "bonus": 0.20},
}

# ── Ore group IDs (for bay categorization) ────────────────────────────────────
ORE_GROUP_IDS = {
    462, 460, 459, 458, 4513,  # Simple ores
    469, 457, 456, 455, 454, 4514, 4756, 4759,  # Coherent
    467, 453, 452, 4755,  # Variegated
    451, 450, 461, 4515, 4516, 4757, 4758,  # Complex
    4029, 4030, 4031,  # Abyssal
    468,  # Mercoxit
    1884,  # Moon ores
    465, 466, 4051,  # Ice
}

# PI commodity group IDs (category 43 in EVE)
PI_GROUP_IDS = {
    1332, 1333, 1334, 1335, 1336, 1337,  # P0-P4 planetary commodities
    1406, 1407, 1408,  # Additional PI groups
}

# Minerals (category 4, group 18)
MINERAL_GROUP_ID = 18


# ── Stacking penalty ─────────────────────────────────────────────────────────

def stacking_penalty(n: int) -> float:
    """EVE stacking penalty for the nth module (1-indexed).

    Uses the canonical Pyfa/in-game formula `exp(-(n-1)^2 / 7.1289)`. The
    older `0.87^((n-1)^2)` approximation is close at n=2,3 but diverges at
    n>=4 (e.g. n=4 → 0.260 vs correct 0.283). Kept 1-indexed to preserve
    callers in this module.
    """
    if n <= 0:
        return 0.0
    return math.exp(-((n - 1) ** 2) / 7.1289)


# ── Capacity calculation ──────────────────────────────────────────────────────

def calculate_effective_capacity(
    ship_type_id: int,
    num_cargo_mods: int = 0,
    cargo_mod_key: str = "none",
    num_cargo_rigs: int = 0,
    cargo_rig_key: str = "none",
    skill_level: int = 5,
) -> dict[str, float]:
    """Calculate effective capacity for each bay on the ship."""
    ship = HAULING_SHIPS.get(ship_type_id)
    if not ship:
        return {}

    mod_bonus = CARGO_MODULES.get(cargo_mod_key, CARGO_MODULES["none"])["bonus"]
    rig_bonus = CARGO_RIGS.get(cargo_rig_key, CARGO_RIGS["none"])["bonus"]

    result = {}
    for bay_name, bay_info in ship["bays"].items():
        base = bay_info["base"]

        # Apply primary skill bonus
        if ship["skill_bonus"] and ship["skill_bonus"]["bay"] == bay_name:
            base *= (1 + ship["skill_bonus"]["per_level"] * skill_level)

        # Apply extra skill bonuses (e.g. Orca ore hold, Kryos ice hold)
        for extra in ship.get("extra_skill_bonus", []):
            if extra["bay"] == bay_name:
                base *= (1 + extra["per_level"] * skill_level)

        if bay_info["expandable"]:
            cargo_mult = 1.0
            for i in range(1, num_cargo_mods + 1):
                cargo_mult *= (1 + mod_bonus * stacking_penalty(i))
            for i in range(1, num_cargo_rigs + 1):
                cargo_mult *= (1 + rig_bonus * stacking_penalty(i))
            base *= cargo_mult

        result[bay_name] = round(base, 1)

    return result


# ── Trip calculation ──────────────────────────────────────────────────────────

def calculate_trips(volume_by_bay: dict[str, float], capacity_by_bay: dict[str, float]) -> int:
    """Calculate minimum trips needed."""
    if not volume_by_bay or not capacity_by_bay:
        return 0
    max_trips = 0
    for bay, volume in volume_by_bay.items():
        if volume <= 0:
            continue
        cap = capacity_by_bay.get(bay, 0)
        if cap <= 0:
            return -1
        max_trips = max(max_trips, math.ceil(volume / cap))
    return max_trips


# ── Item categorization ───────────────────────────────────────────────────────

def categorize_item(group_id: int | None) -> str:
    """Determine which bay type an item belongs to based on its group_id."""
    if group_id is None:
        return "cargo"
    if group_id in ORE_GROUP_IDS:
        return "ore"
    if group_id in PI_GROUP_IDS:
        return "planetary"
    if group_id == MINERAL_GROUP_ID:
        return "mineral"
    return "cargo"


# ── Ship recommendation ──────────────────────────────────────────────────────

def _distribute_volume(total_volume: float, capacities: dict[str, float]) -> dict[str, float]:
    """Distribute total volume across bays proportional to their capacity."""
    total_cap = sum(capacities.values())
    if total_cap <= 0:
        return {}
    return {bay: total_volume * (cap / total_cap) for bay, cap in capacities.items() if cap > 0}


def recommend_ships(
    items_by_bay: dict[str, float],
    skill_level: int = 5,
    top_n: int = 5,
) -> list[dict]:
    """Given total m3 per bay category, recommend ships sorted by fewest trips.
    Deduplicates ships within the same group that have the same trip count."""
    all_results = []
    for type_id, ship in HAULING_SHIPS.items():
        is_capital = ship["group"] in ("Freighter", "Jump Freighter")
        mod_key = "cap_t2" if is_capital else "t2"

        max_cap = calculate_effective_capacity(
            type_id,
            num_cargo_mods=ship["low_slots"],
            cargo_mod_key=mod_key,
            num_cargo_rigs=ship["rig_slots"],
            cargo_rig_key="t2",
            skill_level=skill_level,
        )

        # Map specialized items to matching bays, general cargo to all bays
        volume_mapped: dict[str, float] = {}
        general_volume = 0.0  # volume with no specific bay requirement

        for bay, vol in items_by_bay.items():
            if vol <= 0:
                continue
            # Direct match to ship bay
            if bay in max_cap and max_cap[bay] > 0:
                volume_mapped[bay] = volume_mapped.get(bay, 0) + vol
            elif bay == "ore" and "ore_hold" in max_cap:
                volume_mapped["ore_hold"] = volume_mapped.get("ore_hold", 0) + vol
            elif bay == "ore_hold" and "ore" in max_cap:
                volume_mapped["ore"] = volume_mapped.get("ore", 0) + vol
            else:
                # No specific bay match — distribute across all bays
                general_volume += vol

        if general_volume > 0:
            # Distribute general cargo across all bays proportionally
            distributed = _distribute_volume(general_volume, max_cap)
            for bay, vol in distributed.items():
                volume_mapped[bay] = volume_mapped.get(bay, 0) + vol

        trips = calculate_trips(volume_mapped, max_cap)
        if trips < 0:
            continue

        total_cap = sum(max_cap.values())
        all_results.append({
            "type_id": type_id,
            "name": ship["name"],
            "group": ship["group"],
            "trips": trips,
            "total_capacity": total_cap,
            "capacities": max_cap,
        })

    all_results.sort(key=lambda r: (r["trips"], -r["total_capacity"]))

    # Deduplicate: within a group, keep only the best ship per trip count
    seen: dict[tuple, bool] = {}
    results = []
    for r in all_results:
        key = (r["group"], r["trips"])
        if key in seen:
            continue
        seen[key] = True
        results.append(r)
        if len(results) >= top_n:
            break

    return results


# ── Paste parser ──────────────────────────────────────────────────────────────

def parse_eve_paste(text: str) -> list[dict]:
    """Parse EVE clipboard formats into [{name, qty}]."""
    items: dict[str, int] = {}
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        name = None
        qty = 1

        if "\t" in line:
            parts = line.split("\t")
            name = parts[0].strip()
            for part in parts[1:]:
                cleaned = part.strip().replace(",", "").replace(".", "")
                if cleaned.isdigit():
                    qty = int(cleaned)
                    break
        else:
            m = re.match(r'^(.+?)\s+x\s*([\d,]+)\s*$', line, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                qty = int(m.group(2).replace(",", ""))
            else:
                m = re.match(r'^(.+?)\s+([\d,]+)\s*$', line)
                if m and not m.group(2).startswith("0"):
                    name = m.group(1).strip()
                    qty = int(m.group(2).replace(",", ""))
                else:
                    name = line

        if name:
            items[name] = items.get(name, 0) + qty

    return [{"name": n, "qty": q} for n, q in items.items()]
