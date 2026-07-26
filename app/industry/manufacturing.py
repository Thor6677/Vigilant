"""Pure manufacturing cost/time primitives — the ME/TE/structure/rig/security
math shared by the manufacturing calculator (`app/routes/industry.py`) and the
build-profitability finder (`app/industry/build_finder.py`).

These were originally defined inline in `app/routes/industry.py`; they were
lifted here verbatim (behavior-identical) so the finder can reuse the exact same
cost engine without importing the routes module (which would be circular) and
without duplicating the modifier math. `routes/industry.py` now imports these
names, so every existing endpoint and template context is unchanged.
"""
from __future__ import annotations

import math

# ── Manufacturing modifier tables ─────────────────────────────────────────────

STRUCTURES = {
    "npc_station": {"label": "NPC Station",  "mat": 1.00, "time": 1.00},
    "raitaru":     {"label": "Raitaru",       "mat": 0.99, "time": 0.85},
    "azbel":       {"label": "Azbel",         "mat": 0.99, "time": 0.80},
    "sotiyo":      {"label": "Sotiyo",        "mat": 0.99, "time": 0.70},
}

RIGS = {
    "none":            {"label": "None",             "mat": 0.0,    "time": 0.0},
    "t1_basic":        {"label": "T1 Basic",         "mat": 0.020,  "time": 0.20},
    "t2_basic":        {"label": "T2 Basic",         "mat": 0.024,  "time": 0.24},
    "t1_specialized":  {"label": "T1 Specialized",   "mat": 0.042,  "time": 0.20},
    "t2_specialized":  {"label": "T2 Specialized",   "mat": 0.0504, "time": 0.24},
}

SEC_STATUS = {
    "highsec":  {"label": "Highsec",      "mult": 1.0},
    "lowsec":   {"label": "Lowsec",       "mult": 1.9},
    "nullsec":  {"label": "Null / WH",    "mult": 2.1},
}


def calc_material(base_qty: int, runs: int, me: int,
                  struct_mat: float, rig_mat_base: float, sec_mult: float) -> int:
    """Adjusted material quantity after ME + structure + rig (security-scaled)
    bonuses. Never drops below `runs` (one unit per run minimum)."""
    me_mod = 1.0 - me / 100.0
    rig_mod = 1.0 - (rig_mat_base * sec_mult)
    adjusted = runs * base_qty * me_mod * struct_mat * rig_mod
    adjusted = round(adjusted, 2)
    return max(runs, math.ceil(adjusted))


def calc_time(base_time: int, te: int, struct_time: float, rig_time_base: float,
              sec_mult: float, industry: int = 5, adv_industry: int = 5) -> int:
    """Calculate manufacturing time in seconds with all modifiers."""
    if not base_time:
        return 0
    t = base_time
    t *= (1 - te / 100.0)
    t *= struct_time
    t *= (1 - rig_time_base * sec_mult)
    t *= (1 - industry * 0.04)
    t *= (1 - adv_industry * 0.03)
    return max(1, round(t))


def format_time(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds <= 0:
        return "—"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# Backwards-compatible private aliases — `routes/industry.py` historically
# referenced these with a leading underscore; keep them so nothing else in the
# module (or its tests) has to change beyond the import line.
_calc_material = calc_material
_calc_time = calc_time
_format_time = format_time
