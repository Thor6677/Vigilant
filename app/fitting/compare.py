"""Side-by-side fit comparison — pure presentation math (Phase 5 Task 4b).

Both fits' stats come from the SAME engine call used everywhere else
(``calculate_fitting_stats``); this module only diffs two already-computed
stat dicts into display rows with per-stat delta direction.

The single source of truth for *which* stats are compared and *which
direction is "better"* is ``COMPARE_STAT_SECTIONS``. The template renders from
it and the tests assert against it, so the coloring shown on the page and the
coloring the tests check can never drift apart. This matters most for the
inverted stats (sig radius, align/lock time, cap drain, spool time) where lower
is better — a hard-coded template class would otherwise green a worse fit.
"""

# direction: "higher" = bigger is better, "lower" = smaller is better,
#            "neutral" = show delta but never color, "bool" = yes/no, no delta.
# fmt: "int" | "f1" | "pct" (already 0-100) — controls display + delta string.
# guard: optional key; "spool" rows are dropped when neither fit is spooling.
COMPARE_STAT_SECTIONS: list[dict] = [
    {"name": "Defense", "rows": [
        ("Shield EHP", "shield_ehp", "higher", "int", None),
        ("Armor EHP", "armor_ehp", "higher", "int", None),
        ("Hull EHP", "hull_ehp", "higher", "int", None),
        ("Total EHP", "total_ehp", "higher", "int", None),
        ("Shield rep/s", "shield_rep_rate", "higher", "f1", None),
        ("Armor rep/s", "armor_rep_rate", "higher", "f1", None),
        ("Passive shield/s", "peak_shield_recharge", "higher", "f1", None),
    ]},
    {"name": "Resists — Shield", "rows": [
        ("EM", "shield_em_resist", "higher", "pct", None),
        ("Thermal", "shield_therm_resist", "higher", "pct", None),
        ("Kinetic", "shield_kin_resist", "higher", "pct", None),
        ("Explosive", "shield_expl_resist", "higher", "pct", None),
    ]},
    {"name": "Resists — Armor", "rows": [
        ("EM", "armor_em_resist", "higher", "pct", None),
        ("Thermal", "armor_therm_resist", "higher", "pct", None),
        ("Kinetic", "armor_kin_resist", "higher", "pct", None),
        ("Explosive", "armor_expl_resist", "higher", "pct", None),
    ]},
    {"name": "Offense", "rows": [
        ("Weapon DPS", "weapon_dps", "higher", "f1", None),
        ("Drone DPS", "drone_dps", "higher", "f1", None),
        ("Total DPS", "total_dps", "higher", "f1", None),
        ("Total DPS (max spool)", "total_dps_max_spool", "higher", "f1", "spool"),
        ("Alpha (volley)", "weapon_volley", "higher", "int", None),
    ]},
    {"name": "Navigation", "rows": [
        ("Max velocity", "max_velocity", "higher", "f1", None),
        ("Align time", "align_time", "lower", "f1", None),
        ("Sig radius", "sig_radius", "lower", "f1", None),
        ("Lock time", "lock_time", "lower", "f1", None),
        ("Warp speed", "warp_speed_au_s", "higher", "f1", None),
    ]},
    {"name": "Capacitor", "rows": [
        ("Cap stable", "cap_stable", "bool", "int", None),
        ("Cap stable %", "cap_stable_pct", "higher", "f1", None),
        ("Cap lasts (s)", "cap_lasts_s", "higher", "int", None),
        ("Cap drain/s", "cap_drain_rate", "lower", "f1", None),
    ]},
]


def _fmt(val, fmt: str) -> str:
    if val is None:
        return "—"
    if fmt == "int":
        return f"{val:,.0f}"
    if fmt == "pct":
        return f"{val:,.1f}%"
    return f"{val:,.1f}"


def _fmt_delta(delta: float, fmt: str) -> str:
    if fmt == "int":
        return f"{delta:+,.0f}"
    if fmt == "pct":
        return f"{delta:+,.1f}%"
    return f"{delta:+,.1f}"


def _num(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _spool_shown(stats: dict) -> bool:
    """Does this fit spool up (Triglavian ramp)? True when max-spool DPS
    meaningfully exceeds base DPS."""
    return _num(stats.get("total_dps_max_spool")) - _num(stats.get("total_dps")) > 0.05


def build_compare_sections(stats_a: dict, stats_b: dict) -> list[dict]:
    """Diff two engine stat dicts into rendered sections.

    Each row: label, a (display), b (display), delta (display), cls
    (one of 'better' | 'worse' | 'same' | 'neutral' | 'bool') — where cls
    describes fit B relative to fit A, per the stat's own direction.
    """
    show_spool = _spool_shown(stats_a) or _spool_shown(stats_b)
    out: list[dict] = []
    for section in COMPARE_STAT_SECTIONS:
        rows_out = []
        for label, key, direction, fmt, guard in section["rows"]:
            if guard == "spool" and not show_spool:
                continue
            a_raw = stats_a.get(key)
            b_raw = stats_b.get(key)

            if direction == "bool":
                rows_out.append({
                    "label": label,
                    "a": "Stable" if a_raw else "Unstable",
                    "b": "Stable" if b_raw else "Unstable",
                    "delta": "",
                    "cls": "bool",
                })
                continue

            av, bv = _num(a_raw), _num(b_raw)
            delta = bv - av
            if abs(delta) < 1e-9:
                cls = "same"
            elif direction == "lower":
                cls = "better" if delta < 0 else "worse"
            elif direction == "higher":
                cls = "better" if delta > 0 else "worse"
            else:
                cls = "neutral"
            rows_out.append({
                "label": label,
                "a": _fmt(a_raw, fmt),
                "b": _fmt(b_raw, fmt),
                "delta": _fmt_delta(delta, fmt) if cls != "same" else "—",
                "cls": cls,
            })
        if rows_out:
            out.append({"name": section["name"], "rows": rows_out})
    return out
