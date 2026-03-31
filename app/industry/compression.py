"""Ore compression calculator — yield computation and LP solver."""

import math
from scipy.optimize import linprog

# ── Mineral type IDs ──────────────────────────────────────────────────────────

MINERALS = {
    "Tritanium": 34,
    "Pyerite": 35,
    "Mexallon": 36,
    "Isogen": 37,
    "Nocxium": 38,
    "Zydrine": 39,
    "Megacyte": 40,
}
MINERAL_IDS = list(MINERALS.values())
MINERAL_NAMES = {v: k for k, v in MINERALS.items()}

# ── Reprocessing skills ───────────────────────────────────────────────────────

SKILL_REPROCESSING = 3385
SKILL_REPROCESSING_EFFICIENCY = 3389

# Ore processing skills (post-Equinox tier system)
SKILL_SIMPLE_ORE = 60377
SKILL_COHERENT_ORE = 60378
SKILL_VARIEGATED_ORE = 60379
SKILL_COMPLEX_ORE = 60380
SKILL_ABYSSAL_ORE = 60381
SKILL_MERCOXIT = 12189

# All ore-specific processing skill IDs for character skill lookup
ORE_PROCESSING_SKILLS = [
    SKILL_SIMPLE_ORE, SKILL_COHERENT_ORE, SKILL_VARIEGATED_ORE,
    SKILL_COMPLEX_ORE, SKILL_ABYSSAL_ORE, SKILL_MERCOXIT,
]

# Ore group_id → processing skill type_id
ORE_GROUP_SKILL = {
    # Simple Ore Processing
    462: SKILL_SIMPLE_ORE,     # Veldspar
    460: SKILL_SIMPLE_ORE,     # Scordite
    459: SKILL_SIMPLE_ORE,     # Pyroxeres
    458: SKILL_SIMPLE_ORE,     # Plagioclase
    4513: SKILL_SIMPLE_ORE,    # Mordunium
    # Coherent Ore Processing
    469: SKILL_COHERENT_ORE,   # Omber
    457: SKILL_COHERENT_ORE,   # Kernite
    456: SKILL_COHERENT_ORE,   # Jaspet
    455: SKILL_COHERENT_ORE,   # Hemorphite
    454: SKILL_COHERENT_ORE,   # Hedbergite
    4514: SKILL_COHERENT_ORE,  # Ytirium
    4756: SKILL_COHERENT_ORE,  # Nocxite
    4759: SKILL_COHERENT_ORE,  # Griemeer
    # Variegated Ore Processing
    467: SKILL_VARIEGATED_ORE, # Gneiss
    453: SKILL_VARIEGATED_ORE, # Dark Ochre
    452: SKILL_VARIEGATED_ORE, # Crokite
    4755: SKILL_VARIEGATED_ORE,# Kylixium
    # Complex Ore Processing
    451: SKILL_COMPLEX_ORE,    # Bistot
    450: SKILL_COMPLEX_ORE,    # Arkonor
    461: SKILL_COMPLEX_ORE,    # Spodumain
    4515: SKILL_COMPLEX_ORE,   # Eifyrium
    4516: SKILL_COMPLEX_ORE,   # Ducinium
    4757: SKILL_COMPLEX_ORE,   # Ueganite
    4758: SKILL_COMPLEX_ORE,   # Hezorime
    # Abyssal Ore Processing
    4029: SKILL_ABYSSAL_ORE,   # Talassonite
    4030: SKILL_ABYSSAL_ORE,   # Rakovene
    4031: SKILL_ABYSSAL_ORE,   # Bezdnacine
    # Mercoxit
    468: SKILL_MERCOXIT,       # Mercoxit
    # Moon ores (Bitumens/Zeolites etc use moon ore skills, not standard)
    1884: SKILL_SIMPLE_ORE,    # Bitumens (fallback)
}

# ── Structure presets ─────────────────────────────────────────────────────────

STRUCTURES = {
    "npc_station":       {"label": "NPC Station (50%)",           "base": 0.50, "role": 0.00},
    "athanor":           {"label": "Athanor",                     "base": 0.50, "role": 0.02},
    "tatara":            {"label": "Tatara",                      "base": 0.50, "role": 0.04},
}

RIGS = {
    "none":      {"label": "None",       "bonus": 0.00},
    "t1":        {"label": "T1 Rig",     "bonus": 0.01},
    "t2":        {"label": "T2 Rig",     "bonus": 0.03},
}

SECURITY = {
    "highsec":  {"label": "Highsec",   "mult": 1.0},
    "lowsec":   {"label": "Lowsec",    "mult": 1.9},
    "nullsec":  {"label": "Null / WH", "mult": 2.1},
}

IMPLANTS = {
    "none":   {"label": "None",            "bonus": 0.00},
    "rx801":  {"label": "RX-801 (+1%)",    "bonus": 0.01},
    "rx802":  {"label": "RX-802 (+2%)",    "bonus": 0.02},
    "rx804":  {"label": "RX-804 (+4%)",    "bonus": 0.04},
}

TRADE_HUBS = {
    "jita":     {"label": "Jita",     "region_id": 10000002},
    "amarr":    {"label": "Amarr",    "region_id": 10000043},
    "dodixie":  {"label": "Dodixie",  "region_id": 10000032},
    "hek":      {"label": "Hek",      "region_id": 10000042},
    "rens":     {"label": "Rens",     "region_id": 10000030},
}


def compute_yield(
    structure: str = "npc_station",
    rig: str = "none",
    security: str = "highsec",
    repro_level: int = 0,
    efficiency_level: int = 0,
    ore_skill_level: int = 0,
    implant: str = "none",
) -> float:
    """Calculate effective reprocessing yield as a fraction (0.0-1.0)."""
    s = STRUCTURES.get(structure, STRUCTURES["npc_station"])
    r = RIGS.get(rig, RIGS["none"])
    sec = SECURITY.get(security, SECURITY["highsec"])
    imp = IMPLANTS.get(implant, IMPLANTS["none"])

    rig_bonus = r["bonus"] * sec["mult"]

    y = s["base"]
    y *= (1 + s["role"])
    y *= (1 + rig_bonus)
    y *= (1 + repro_level * 0.03)
    y *= (1 + efficiency_level * 0.02)
    y *= (1 + ore_skill_level * 0.02)
    y *= (1 + imp["bonus"])
    return min(y, 1.0)


def solve_compression(
    target_minerals: dict[int, int],
    ore_data: dict[int, dict],
    ore_prices: dict[int, float],
    yield_per_ore: dict[int, float],
    mode: str = "isk",
    mineral_prices: dict[int, float] | None = None,
) -> dict:
    """Solve the LP to find optimal compressed ores to buy.

    Args:
        target_minerals: {mineral_type_id: quantity_needed}
        ore_data: {ore_type_id: {name, volume, minerals: {mat_id: base_qty}}}
        ore_prices: {ore_type_id: price_per_unit}
        yield_per_ore: {ore_type_id: effective_yield_fraction}
        mode: "isk", "volume", or "waste"
        mineral_prices: {mineral_type_id: price} — needed for "waste" mode

    Returns dict with ores list, totals, surplus.
    """
    _empty = {"ores": [], "total_isk": 0, "total_volume": 0,
              "minerals_produced": {}, "minerals_surplus": {}}

    # Input validation
    for mid, qty in target_minerals.items():
        if qty < 0:
            return {**_empty, "error": f"Negative quantity for mineral {mid}"}
    for oid, price in ore_prices.items():
        if isinstance(price, float) and (math.isnan(price) or math.isinf(price)):
            return {**_empty, "error": f"Invalid price for ore {oid}"}
    if mode not in ("isk", "volume", "waste"):
        return {**_empty, "error": f"Unknown mode: {mode}"}

    # Filter to ores that produce at least one needed mineral and have a price
    relevant_minerals = {mid for mid, qty in target_minerals.items() if qty > 0}
    if not relevant_minerals:
        return {"ores": [], "total_isk": 0, "total_volume": 0,
                "minerals_produced": {}, "minerals_surplus": {}}

    ore_ids = []
    for oid, data in ore_data.items():
        if ore_prices.get(oid, 0) <= 0:
            continue
        ore_minerals = data.get("minerals", {})
        if any(mid in relevant_minerals for mid in ore_minerals):
            ore_ids.append(oid)

    if not ore_ids:
        return {"ores": [], "total_isk": 0, "total_volume": 0,
                "minerals_produced": {}, "minerals_surplus": {}}

    n_ores = len(ore_ids)

    # Precompute effective minerals per BATCH (not per unit!)
    # typeMaterials gives base minerals per reprocessing batch
    # portion_size = how many units per batch (1 for old batch compressed, 100 for new compressed)
    effective = {}
    for oid in ore_ids:
        y = yield_per_ore.get(oid, 0.5)
        mins = ore_data[oid]["minerals"]
        effective[oid] = {mid: math.floor(qty * y) for mid, qty in mins.items()}

    # LP variables are in BATCHES — cost/volume per batch accounts for portion_size
    portion_sizes = {oid: ore_data[oid].get("portion_size", 1) or 1 for oid in ore_ids}

    active_minerals = [mid for mid in MINERAL_IDS if target_minerals.get(mid, 0) > 0]

    try:
        if mode == "waste":
            # Waste mode: variables are [x_0..x_n, s_0..s_6] where s_m = surplus for mineral m
            # Objective: minimize cost + surplus weighted by mineral prices
            n_minerals = len(active_minerals)
            mp = mineral_prices or {}
            c = [ore_prices.get(oid, 1e12) * portion_sizes[oid] for oid in ore_ids]
            c += [mp.get(mid, 1.0) for mid in active_minerals]

            A_eq = []
            b_eq = []
            for j, mid in enumerate(active_minerals):
                row = [effective[oid].get(mid, 0) for oid in ore_ids]
                surplus_cols = [0] * n_minerals
                surplus_cols[j] = -1
                A_eq.append(row + surplus_cols)
                b_eq.append(target_minerals[mid])

            bounds = [(0, None) for _ in ore_ids] + [(0, None) for _ in active_minerals]
            result = linprog(c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method='highs')
        else:
            if mode == "volume":
                c = [ore_data[oid]["volume"] * portion_sizes[oid] for oid in ore_ids]
            else:
                c = [ore_prices.get(oid, 1e12) * portion_sizes[oid] for oid in ore_ids]

            A_ub = []
            b_ub = []
            for mid in active_minerals:
                row = [-(effective[oid].get(mid, 0)) for oid in ore_ids]
                A_ub.append(row)
                b_ub.append(-target_minerals[mid])

            bounds = [(0, None) for _ in ore_ids]
            result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method='highs')
        if not result.success:
            return {"ores": [], "total_isk": 0, "total_volume": 0,
                    "minerals_produced": {}, "minerals_surplus": {},
                    "error": "No feasible solution found"}
        # Extract only ore quantities (first n_ores vars), ignore surplus vars
        quantities = [math.ceil(max(0, x)) for x in result.x[:n_ores]]
    except Exception as e:
        return {"ores": [], "total_isk": 0, "total_volume": 0,
                "minerals_produced": {}, "minerals_surplus": {},
                "error": str(e)}

    # Build results — convert batches to units
    ores_result = []
    total_isk = 0.0
    total_volume = 0.0
    minerals_produced = {mid: 0 for mid in MINERAL_IDS}

    for i, oid in enumerate(ore_ids):
        batches = quantities[i]
        if batches <= 0:
            continue
        ps = portion_sizes[oid]
        units = batches * ps
        price = ore_prices.get(oid, 0)
        vol = ore_data[oid]["volume"]
        line_isk = price * units
        line_vol = vol * units
        total_isk += line_isk
        total_volume += line_vol

        for mid in MINERAL_IDS:
            minerals_produced[mid] += effective[oid].get(mid, 0) * batches

        ores_result.append({
            "type_id": oid,
            "name": ore_data[oid]["name"],
            "quantity": units,
            "price_each": price,
            "total_price": line_isk,
            "volume": line_vol,
        })

    ores_result.sort(key=lambda x: x["total_price"], reverse=True)

    minerals_surplus = {
        MINERAL_NAMES.get(mid, str(mid)): minerals_produced[mid] - target_minerals.get(mid, 0)
        for mid in MINERAL_IDS
        if minerals_produced[mid] - target_minerals.get(mid, 0) > 0
    }

    minerals_produced_named = {
        MINERAL_NAMES.get(mid, str(mid)): minerals_produced[mid]
        for mid in MINERAL_IDS if minerals_produced[mid] > 0
    }

    return {
        "ores": ores_result,
        "total_isk": total_isk,
        "total_volume": total_volume,
        "minerals_produced": minerals_produced_named,
        "minerals_surplus": minerals_surplus,
    }
