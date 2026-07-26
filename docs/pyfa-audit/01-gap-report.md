# Phase 1: Gap Audit Report

**Pyfa reference commit:** `2651316f980fa9e36e638938e6e01eb3fe3c0e2e`
**Audit date:** 2026-04-18
**Vigilant commit:** `fcb9f13` (includes spool-up DPS fix from earlier today)

---

## Test Fits

Six fits covering different hulls, weapon systems, tank types, and doctrines.

### Fit 1 — Shield Drake (BC, missiles, shield)
```
[Drake, Shield Drake]
Heavy Missile Launcher II x6, Scourge Fury Heavy Missile
Large Shield Extender II x2
Multispectrum Shield Hardener II x2
50MN Microwarpdrive II
Ballistic Control System II x3
Damage Control II
Medium Core Defense Field Extender I x3
```

### Fit 2 — Passive Gila (cruiser, drones, passive shield)
```
[Gila, Passive Gila]
Rapid Light Missile Launcher II x4, Scourge Heavy Missile
Large Shield Extender II x2
Multispectrum Shield Hardener II
50MN Microwarpdrive II
Drone Damage Amplifier II x3
Medium Core Defense Field Extender I x3
Hammerhead II x2
```

### Fit 3 — Pulse Retribution (AF, small lasers, armor)
```
[Retribution, Pulse Retri]
Small Focused Pulse Laser II x4, Conflagration S
10MN Afterburner II
Stasis Webifier II
Heat Sink II x2
Multispectrum Energized Membrane II
Small Ancillary Armor Repairer
Damage Control II
```

### Fit 4 — Leshak (BS, Triglavian spool-up)
```
[Leshak, Spool Test]
Supratidal Entropic Disintegrator II, Occult L
Large Shield Extender II
Multispectrum Shield Hardener II x2
Veles Entropic Radiation Sink x3
Damage Control II
```

### Fit 5 — HAM Sacrilege (HAC, HAMs, armor)
```
[Sacrilege, HAM Sac]
Heavy Assault Missile Launcher II x5, Scourge Heavy Assault Missile
10MN Afterburner II
Stasis Webifier II
Warp Scrambler II
Multispectrum Energized Membrane II x2
Ballistic Control System II x2
Damage Control II
Medium Armor Repairer II
```

### Fit 6 — Rattlesnake (BS, missiles + drones, shield)
```
[Rattlesnake, Rattle]
Cruise Missile Launcher II x5, Scourge Fury Cruise Missile
Large Shield Extender II x2
Multispectrum Shield Hardener II x2
500MN Microwarpdrive II
Cap Recharger II
Ballistic Control System II x3
Drone Damage Amplifier II x2
Damage Control II
Large Core Defense Field Extender I x3
Ogre II x2
Hammerhead II x3
```

---

## Comparison Tables

Values marked with `!!` have significant error (>5%). Expected values are computed
from SDE attribute data and the correct modifier pipeline per Pyfa's algorithm.

### Fit 1: Shield Drake

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS | 171.8 | ~390 | -56% !! | Missing missile damage bonuses |
| Drone DPS | 0.0 | 0.0 | OK | No drones fitted |
| Total EHP | 62,379 | ~63,000 | -1% | Close (hull resist gap) |
| Shield EHP | 52,964 | ~53,000 | <1% | OK |
| Hull Resists | 0/0/0/0% | 40/40/40/40% | !! | DC II hull bonus not applied |
| Cap Stable | No (0%) | ~No | — | Simple model, direction correct |
| Sig Radius | 388.8 | ~389 | OK | MWD sig penalty applying |

**Root cause of DPS gap:** Drake's ship hull bonuses use `OwnerRequiredSkillModifier`
with `domain=charID` to boost missile charge kinetic damage (+10%/level = +50%).
Engine skips all `OwnerRequiredSkillModifier` modifiers. Additionally, missile
damage skills (Warhead Upgrades: +2%/level, Heavy Missiles: +5%/level) use the
same modifier type. BCU II's damage bonus is also absent from SDE modifierInfo
(only ROF bonus present). Combined missing multipliers: ~2.27x.

### Fit 2: Passive Gila

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS | 124.9 | ~200 | -38% !! | Missing missile charge bonuses |
| Drone DPS | 30.7 | ~185 | -83% !! | Missing Gila 500% drone bonus + DDAs |
| Total DPS | 155.6 | ~385 | -60% !! | Both weapon and drone DPS wrong |
| Total EHP | 31,855 | ~32,000 | <1% | OK |
| Hull Resists | 0% all | 0% all | OK | No DC II fitted |
| Shield Recharge | 25.6 | ~25.6 | OK | Passive regen formula correct |

**Root cause of DPS gap:** Gila's role bonus is 500% to drone `damageMultiplier`
via `OwnerRequiredSkillModifier` (domain=charID, skill=33699). Engine skips
this entirely. DDA II also uses `OwnerRequiredSkillModifier` for its damage
bonus. Gila's per-level missile damage bonus (+10%/level to charge kin/therm)
also uses `OwnerRequiredSkillModifier`. None of these are applied.

### Fit 3: Pulse Retribution

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS | 394.1 | ~390 | +1% | OK (turret bonuses work) |
| Total EHP | 6,102 | ~6,400 | -5% | Hull resist gap |
| Armor EHP | 4,434 | ~4,500 | -1% | Close |
| Hull Resists | 0% all | 40/40/40/40% | !! | DC II hull bonus |
| Cap Stable | No | No | OK | Direction correct |

**Notes:** Turret-based fits are much more accurate. Ship hull bonuses for
turrets use `LocationRequiredSkillModifier` (domain=shipID) which the engine
handles correctly. Skill bonuses (Surgical Strike, Rapid Firing, weapon skills)
also use domain=shipID patterns. The only gap is hull resists from DC II.

### Fit 4: Leshak (Occult)

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS (base) | 779.7 | ~780 | <1% | OK (spool fix working) |
| Max Spool DPS | 2,505.7 | ~2,500 | <1% | OK (spool fix working) |
| Spool Time | 108s | ~108s | OK | Correct cycle count |
| Total EHP | 58,385 | ~59,000 | -1% | Hull resist gap |
| Hull Resists | 0% all | 40/40/40/40% | !! | DC II hull bonus |

**Notes:** Triglavian turret DPS is now accurate after the spool-up fix from
this session. The `LocationGroupModifier` pattern used by Entropic Radiation
Sinks and the `LocationRequiredSkillModifier` pattern for ship/skill bonuses
are both handled correctly. Only remaining gap is hull resists.

### Fit 5: HAM Sacrilege

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS | 167.4 | ~440 | -62% !! | Missing missile damage bonuses |
| Total EHP | 14,406 | ~15,000 | -4% | Hull resist gap |
| Armor EHP | 10,456 | ~10,500 | <1% | OK |
| Hull Resists | 0% all | 40/40/40/40% | !! | DC II hull bonus |

**Root cause:** Same as Drake — Sacrilege ship hull bonus (+5% HAM damage/level)
uses `OwnerRequiredSkillModifier` to boost charge damage. Missile skills
(Warhead Upgrades, HAM skill) also use this pattern. BCU damage bonus missing
from SDE. Combined missing multipliers: ~2.6x.

### Fit 6: Rattlesnake

| Stat | Vigilant | Expected | Delta | Notes |
|------|----------|----------|-------|-------|
| Weapon DPS | 271.3 | ~530 | -49% !! | Missing missile damage bonuses |
| Drone DPS | 107.5 | ~360 | -70% !! | Missing Rattlesnake drone bonus + DDAs |
| Total DPS | 378.9 | ~890 | -57% !! | Both sources wrong |
| Total EHP | 148,998 | ~150,000 | <1% | OK |
| Shield EHP | 120,902 | ~121,000 | <1% | OK |
| Hull Resists | 0% all | 40/40/40/40% | !! | DC II hull bonus |
| Drone BW Used | 80.0 | 50.0 | !! | Exceeds ship bandwidth (50) |

**Root cause:** Rattlesnake's role bonus (+50% drone damage and +100% drone HP)
uses `OwnerRequiredSkillModifier`. DDA II damage bonus uses same pattern. Missile
bonuses use same pattern. Additionally, drone bandwidth is over-allocated (80 of
50 available) — engine doesn't enforce bandwidth limits in DPS calc.

---

## Categorized Gap List

### CRITICAL — OwnerRequiredSkillModifier not handled

**Impact:** Missile DPS 40-60% too low on every missile ship. Drone DPS 70-83% too
low on drone-bonused ships. Affects the majority of PvE and many PvP fits.

**What it is:** The EVE dogma system has four modifier function types:
- `LocationGroupModifier` — targets by item group (used by turret bonuses) ✅ handled
- `LocationRequiredSkillModifier` — targets by skill requirement (used by turret bonuses) ✅ handled
- `OwnerRequiredSkillModifier` — targets items owned by character requiring a skill ❌ NOT handled
- `ItemModifier` — targets the item itself or the ship ✅ partially handled

`OwnerRequiredSkillModifier` is used for:
1. Ship hull missile damage bonuses (modifies charge damage attrs like kineticDamage)
2. Ship hull drone damage bonuses (modifies drone damageMultiplier)
3. Drone Damage Amplifier damage bonus
4. Missile damage skills (Warhead Upgrades, weapon-specific skills)
5. Drone damage skills (Drone Interfacing, etc.)

These all use `domain=charID` which the engine's cross-module and skill-bonus
functions filter out (they only query `domain=shipID`).

**Pyfa reference:** `eos/effectHandlerHelpers.py` — `filteredChargeMultiply()`,
`filteredItemMultiply()`. Pyfa uses hand-coded effect handlers that iterate items
matching a filter predicate. The SDE `modifierInfo` field describes the intent
(OwnerRequiredSkillModifier) but Pyfa bypasses it with its own logic.

**Fix approach:** Extend the engine to handle `OwnerRequiredSkillModifier` in both
the cross-module step (for damage mods like DDA) and the ship-hull-bonus step
(for ship bonuses). Also handle in skill-bonus step for skills like Warhead Upgrades.
For charges, the bonus modifies charge damage attributes directly — need to apply
before DPS calculation reads charge damage.

### CRITICAL — BCU missile damage bonus missing from SDE

**Impact:** Missile ships lose ~10% DPS per BCU fitted (only ROF bonus applies).

**What it is:** Ballistic Control System II (type 22291) has only two modifiers in
the SDE modifierInfo:
- `ItemModifier` for scanner attribute (irrelevant)
- `LocationRequiredSkillModifier` for rate-of-fire bonus

The damage multiplier bonus (+10% per BCU II) has no corresponding modifier in the
SDE data. In Pyfa, this is handled by a hard-coded effect handler.

**Pyfa reference:** Effect handlers in `eos/effects.py` use `filteredChargeMultiply`
to apply the BCU's `damageMultiplier` attribute as a stacking-penalized multiplier
on charge damage. This is defined per-effect-ID, not derived from modifierInfo.

**Fix approach:** Either:
- Add a mapping of known effect IDs → behavior for cases where modifierInfo is incomplete
- Or detect modules with `damageMultiplier` attribute that have
  `LocationRequiredSkillModifier` for ROF targeting a weapon skill, and infer
  they should also apply their `damageMultiplier` to charges requiring that skill

### MODERATE — Hull resonance attribute ID mismatch

**Impact:** Hull resists always show 0%. Underreports EHP by 2-5% on fits with DC II.

**What it is:** The engine reads hull resists from attrs 974-977
(`hullEmDamageResonance` etc.). But Damage Control II modifies attrs 109-113
(the generic `emDamageResonance` etc.) using the DC II's own 974-977 values
as source. The ship's base hull resonances may be stored in 109-113 rather
than 974-977, or the DC II may target the wrong IDs for the engine's lookup.

**Pyfa reference:** Pyfa reads hull resonances from the generic resonance
attribute names (not IDs), and the DC II effect handler targets the correct
ship-level attributes.

**Fix approach:** Verify which attribute IDs ships actually store hull resonances
in, then update either the engine constants or the modifier handling to match.

### MODERATE — Capacitor simulation oversimplified

**Impact:** Cap stability percentage can be significantly off. Binary stable/unstable
is usually correct, but the percentage and time-to-empty are approximations.

**What it is:** Vigilant uses a simple comparison: peak cap recharge rate (at 25%
capacity) vs. total module drain rate. If peak recharge >= drain, the fit is
cap-stable. Cap-stable percentage is found via binary search on the recharge curve.

Pyfa uses a discrete-event simulation: each module activation is an event in a
priority queue. The simulation tracks actual cap level over time, handles module
staggering, clip reload, cap booster injection with intelligent timing, and detects
stability via period analysis (LCM of all cycle times).

**Pyfa reference:** `eos/capSim.py` — `CapSimulator` class. Uses min-heap,
cap regen formula `((1 + (sqrt(c/cMax) - 1) * exp((tLast - tNow) / tau))^2) * cMax`,
stability detection at period boundaries.

**Fix approach:** Implement a discrete-event cap simulator. The Vigilant approach
can remain as a fast estimate, with the full sim running when detailed cap stats
are requested.

### MODERATE — Stacking penalty groups

**Impact:** Minor DPS/stat errors when multiple module types modify the same attribute.
Currently all modifiers to the same attribute are in one stacking group.

**What it is:** Pyfa uses named penalty groups. Modifiers in different groups are
penalized independently. For example, Bastion's ROF bonus uses penalty group
"postPerc" while damage mods use "default". This prevents Bastion and damage mods
from stacking-penalizing each other.

Vigilant currently has no stacking at all in the cross-module step (was removed
to prevent Bastion/sink cross-penalization), but earlier this session stacking
was re-added using the `stackable` flag. The issue is that all modifiers to the
same attribute from different sources share one penalty pool.

**Pyfa reference:** `eos/modifiedAttributeDict.py` — `penaltyGroup` parameter
in `multiply()` method. Each group is a separate sorted list.

**Fix approach:** Add penalty group support. Use the source module's type or effect
as the group key. Alternatively, use effect category as the group key.

### LOW — No damage application math

**Impact:** DPS is always maximum theoretical. No tracking, no missile application,
no transversal/range reduction.

**What it is:** Neither Vigilant nor Pyfa apply tracking formulas or missile
application to the displayed DPS values. Pyfa displays "paper DPS" (full
theoretical) by default. Target profiles only apply resist-based reductions.

**Pyfa reference:** Pyfa calculates tracking via `calculateRangeFactor` in
`eos/calc.py` for projected effects, but NOT for DPS display. Missile application
(sig/explo formula) is not applied either.

**Fix approach:** This is feature parity with Pyfa. Can be added later as an
optional "applied DPS" display with user-specified target parameters.

### LOW — No active tank (rep/s) calculation

**Impact:** No rep-per-second display for armor/shield repair modules.

**Pyfa reference:** `fit.tank` sums armorRepair/shieldRepair/hullRepair attributes.
`fit.calculateSustainableTank` factors in cap sustainability and orders reps by
efficiency.

### LOW — Single damage profile for EHP

**Impact:** EHP always assumes uniform 25/25/25/25 damage split.

**Pyfa reference:** `eos/saveddata/damagePattern.py` — 190+ built-in profiles
for specific ammo types, NPC factions, abyssal weather variants.

### LOW — No lock time calculation

**Impact:** Missing stat display.

**Formula:** `min(40000 / scanResolution / asinh(targetSigRadius)^2, 1800)`

---

## Pyfa File References

For each gap, the relevant Pyfa source (commit `2651316f`):

| Gap | Pyfa File | Description |
|-----|-----------|-------------|
| OwnerRequiredSkillModifier | `eos/effectHandlerHelpers.py` | `filteredItemMultiply`, `filteredChargeMultiply` |
| OwnerRequiredSkillModifier | `eos/saveddata/module.py:~907-953` | Effect state checking and application |
| BCU damage bonus | `eos/effects.py` (effect-specific handlers) | Hard-coded per effect ID |
| Cap simulation | `eos/capSim.py` | `CapSimulator` class, discrete event sim |
| Cap integration | `eos/saveddata/fit.py:~1400-1500` | `simulateCap` method |
| Stacking groups | `eos/modifiedAttributeDict.py:~350-420` | `penaltyGroup` in multiply() |
| Active tank | `eos/saveddata/fit.py:~1577-1700` | `tank`, `calculateSustainableTank` |
| Damage profiles | `eos/saveddata/damagePattern.py` | `calculateEhp`, `effectivify` |
| DPS calculation | `eos/saveddata/module.py:~477-550` | `getVolleyParameters`, `getDps` |
| Drone DPS | `eos/saveddata/drone.py:~163-200` | `getVolleyParameters`, `getDps` |
| Spool-up | `eos/utils/spoolSupport.py` | `calculateSpoolup` |
| Hull resists | `eos/effects.py` (DC effect handler) | Targets correct ship attrs |
| Lock time | `eos/calc.py:~30` | `calculateLockTime` |

---

## Priority Summary

| Priority | Gap | Impact | Effort |
|----------|-----|--------|--------|
| P0 | OwnerRequiredSkillModifier | Missile/drone DPS 40-83% wrong | Medium |
| P0 | BCU damage bonus | Missile DPS ~10% per BCU | Medium |
| P1 | Hull resonance IDs | Hull resists always 0% | Small |
| P1 | Cap simulation | Stability % inaccurate | Large |
| P2 | Stacking penalty groups | Minor stat errors | Small |
| P2 | Active tank display | Missing feature | Medium |
| P3 | Damage profiles | Missing feature | Small |
| P3 | Lock time | Missing stat | Trivial |
| P3 | Damage application | Missing feature | Medium |
