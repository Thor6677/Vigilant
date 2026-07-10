"""Pure invention probability + expected-cost math.

No DB imports — all inputs are plain values resolved by the caller (route/lookup
layer). This keeps the module trivially unit-testable and reusable.

Key formulas (single source of truth; mirrored in the design spec §2):

    P             = base_prob × (1 + E/40 + (S1+S2)/30) × decryptor_prob_mult
                    (skill levels clamped 0–5, result clamped to (0.0, 1.0])
    attempt_cost  = Σ(datacore_qty × price) + decryptor_price   (consumed per attempt)
    invented_runs = base_runs + decryptor_run_mod               (floored at 1)
    invented_me   = 2 + decryptor_me_mod                        (floored at 0)
    overhead/unit = attempt_cost / P / (invented_runs × per_run_output_qty)

ME2/TE4 base note: an invented T2 BPC starts at ME 2 / TE 4 before any decryptor
modifier is applied — that is why invented ME is `2 + decryptor_me_mod`.

Copy-cost exclusion note: the ISK cost of copying the source T1 blueprint into the
BPC consumed by the invention attempt is deliberately out of scope (v1). It is
negligible for most items; datacores + decryptor + failed-attempt loss dominate.

Decryptor modifier values are hard-coded from the SDE dogma attributes — verified
against the production SDE 2026-07-10 (group_id 1304 types, dogma attribute ids
1112 inventionPropabilityMultiplier / 1113 inventionMEModifier /
1114 inventionTEModifier / 1124 inventionMaxRunModifier).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decryptor:
    """One invention decryptor and its modifiers.

    prob_mult multiplies the invention probability; me_mod/te_mod adjust the
    invented BPC's material/time efficiency; run_mod adjusts the number of runs
    on the invented BPC. te_mod is stored for completeness (invented TE = 4 +
    te_mod) but is not consumed by the Task-2 cost math.
    """

    type_id: int
    name: str
    prob_mult: float
    me_mod: int
    te_mod: int
    run_mod: int


# Verified against prod SDE 2026-07-10 — group_id 1304 published types, dogma
# attribute ids 1112 (prob_mult) / 1113 (me_mod) / 1114 (te_mod) / 1124 (run_mod).
# The "no decryptor" case is represented by None at call sites, NOT an entry here.
DECRYPTORS: dict[str, Decryptor] = {
    "accelerant": Decryptor(34201, "Accelerant Decryptor", 1.2, 2, 10, 1),
    "attainment": Decryptor(34202, "Attainment Decryptor", 1.8, -1, 4, 4),
    "augmentation": Decryptor(34203, "Augmentation Decryptor", 0.6, -2, 2, 9),
    "parity": Decryptor(34204, "Parity Decryptor", 1.5, 1, -2, 3),
    "process": Decryptor(34205, "Process Decryptor", 1.1, 3, 6, 0),
    "symmetry": Decryptor(34206, "Symmetry Decryptor", 1.0, 1, 8, 2),
    "optimized-attainment": Decryptor(34207, "Optimized Attainment Decryptor", 1.9, 1, -2, 2),
    "optimized-augmentation": Decryptor(34208, "Optimized Augmentation Decryptor", 0.9, 2, 0, 7),
}


def _clamp_level(level: int) -> int:
    """Clamp a skill level to the valid EVE range 0–5."""
    if level < 0:
        return 0
    if level > 5:
        return 5
    return level


def invention_probability(
    base_prob: float,
    encryption_lvl: int,
    sci1_lvl: int,
    sci2_lvl: int,
    decryptor_prob_mult: float = 1.0,
) -> float:
    """Invention success probability for one attempt.

    P = base_prob × (1 + E/40 + (S1+S2)/30) × decryptor_prob_mult

    Skill levels are clamped to 0–5. The result is clamped to (0.0, 1.0].
    Returns 0.0 only when base_prob ≤ 0 (caller treats that as un-inventable).
    """
    if base_prob <= 0:
        return 0.0
    e = _clamp_level(encryption_lvl)
    s1 = _clamp_level(sci1_lvl)
    s2 = _clamp_level(sci2_lvl)
    p = base_prob * (1 + e / 40 + (s1 + s2) / 30) * decryptor_prob_mult
    return min(p, 1.0)


def attempt_cost(
    datacores: list[dict],
    price_map: dict[int, float],
    decryptor_price: float = 0.0,
) -> float | None:
    """Total ISK consumed per invention attempt (datacores + decryptor).

    `datacores` is a list of {"material_type_id", "quantity"}. Returns None if
    any datacore is unpriced (site-wide unpriced convention — the row is then
    excluded-and-counted upstream rather than costed at zero).
    """
    total = 0.0
    for dc in datacores:
        price = price_map.get(dc["material_type_id"])
        if price is None:
            return None
        total += price * dc["quantity"]
    return total + decryptor_price


def invented_bpc(base_runs: int, decryptor) -> tuple[int, int]:
    """Invented T2 BPC (runs, ME) under an optional decryptor.

    runs = base_runs + decryptor.run_mod   (floored at 1)
    me   = 2 + decryptor.me_mod            (floored at 0; T2 BPC base ME2)

    `decryptor` is None (no decryptor) or any object exposing run_mod/me_mod
    (a Decryptor instance, or a stub in tests) — duck-typed, not isinstance.
    """
    run_mod = 0 if decryptor is None else decryptor.run_mod
    me_mod = 0 if decryptor is None else decryptor.me_mod
    runs = max(1, base_runs + run_mod)
    me = max(0, 2 + me_mod)
    return runs, me


def invention_overhead_per_unit(
    attempt_cost: float,
    probability: float,
    invented_runs: int,
    per_run_output_qty: int,
) -> float | None:
    """Expected invention cost amortised over one produced unit.

    overhead/unit = attempt_cost / P / (invented_runs × per_run_output_qty)

    Returns None when probability ≤ 0 or runs/output < 1 (nothing to amortise).
    """
    if probability <= 0 or invented_runs < 1 or per_run_output_qty < 1:
        return None
    units = invented_runs * per_run_output_qty
    return attempt_cost / probability / units
