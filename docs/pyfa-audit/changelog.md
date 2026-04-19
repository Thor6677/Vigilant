# Pyfa Audit Changelog

## Round 2 (2026-04-19)

### ISSUE-002 — Subsystem modifier operator ordering
- **Fixed:** Subsystem ItemModifier bonuses to ship attributes are now accumulated by operator type and applied in dogma order (MOD_ADD → POST_MUL → POST_PERCENT), regardless of subsystem iteration order.
- **References:** pyfa `eos/modifiedAttributeDict.py:308-416`; theorycrafter `FittingEngine.kt:3490-3582`
- **Result:** Tengu PG total: 867.5 → 915.0 (correct: `(420+190)*1.2*1.25 = 915`). No regressions on non-T3C fits.

### ISSUE-007 — Charge compatibility for legacy type IDs
- **Fixed:** `ATTR_CHARGE_GROUP_4` was defined as 607 (nonexistent attribute). Correct SDE attribute ID is 609 (`chargeGroup4`). This caused `get_compatible_charges()` to miss Advanced Heavy/Assault/Light Missiles (Fury, Precision, Javelin variants), breaking EFT import auto-loading for T2 ammo.
- **References:** pyfa only — theorycrafter uses compiled game database directly.
- **Result:** "Scourge Fury Heavy Missile" (type 2629) now appears in HML II's compatible charges list. EFT imports with T2 missile ammo auto-load correctly.

### ISSUE-003 — Drone bandwidth enforcement in DPS
- **Fixed:** Drone DPS now only counts drones within the ship's available bandwidth. Drones sorted by per-drone DPS (descending) so highest-DPS drones fill bandwidth first.
- **References:** pyfa `eos/saveddata/drone.py:163-168`; theorycrafter `Mechanics.kt:1204-1236`
- **Result:** Rattlesnake drone DPS: 588.8 → 490.6 (2x Ogre II fill 50 BW; 3x Hammerhead excluded).

### ISSUE-005 — Leshak max spool DPS (closed, not a bug)
- **Finding:** Previous value 2,505.7 was wrong — no modifier targets `damageMultiplierBonusMax` (2734) on the Leshak fit. Current 2,436.7 = `779.7 × (1+2.125)` is mathematically correct per pyfa `eos/saveddata/module.py:515-523`.

### ISSUE-001 — Sacrilege DPS gap (closed, test fit error)
- **Finding:** SDE confirms Sacrilege has Medium Energy Turret bonuses (skill 3306), not missile bonuses. The HAM test fit was exercising an unbonused weapon system. Test fit needs replacement.

## Phase 3.1-3.6 (2026-04-18)

### 3.1 — Hull resonance attribute IDs
- **Fixed:** `ATTR_HULL_*_RESONANCE` constants pointed to attrs 974-977 (hull-tanking module source attrs), but ships store hull resonance in attrs 109-113 (generic `emDamageResonance`, etc.).
- **Result:** Hull resists now show correctly. Drake with DC II: 0% → 59.8%.

### 3.2 — Ship hull OwnerRequiredSkillModifier
- **Fixed:** `_apply_ship_hull_bonuses()` now handles `OwnerRequiredSkillModifier` with `domain=charID`. Matches items (modules, drones, charges) by skill requirement and applies bonuses to the correct attribute map.
- **Also fixed:** Per-level detection now excludes attribute names containing "Role" (e.g., `shipBonusRole7` is a role bonus, not per-level).
- **Result:** Drake missile DPS: 171.8 → 358.3 (+108%). Gila drone DPS: 30.7 → 438.4.

### 3.3 — DDA OwnerRequiredSkillModifier
- **Fixed:** Cross-module step now queries `OwnerRequiredSkillModifier` with `domain=charID` alongside the existing `LocationGroupModifier`/`LocationRequiredSkillModifier` queries. DDA `droneDamageBonus` (attr 1255) now applies to drones requiring the Drones skill.
- **Result:** DDA damage bonus now contributes to drone DPS.

### 3.4 — Skill OwnerRequiredSkillModifier
- **Fixed:** `_apply_all_v_skill_bonuses()` now also queries `domain=charID` and `OwnerRequiredSkillModifier`. Skills like Drone Interfacing (+10%/level drone damage) and Warhead Upgrades (-5%/level missile explosion radius) now apply.
- **Result:** Drone DPS includes Drone Interfacing V (+50%). Missile application improved via Warhead Upgrades V.

### 3.5 — Character-level missile damage multiplier (BCU)
- **Fixed:** BCU II applies its damage bonus via `ItemModifier` with `domain=charID`, targeting character attr 212 (`missileDamageMultiplier`). The engine now collects these modifiers from all fitted BCUs, applies stacking penalties, and scales missile charge damage in the DPS loop.
- **Result:** Rattlesnake with 3x BCU II: missile DPS properly scaled by BCU damage bonus.

### 3.6 — Stacking penalty groups
- **Fixed:** Cross-module multiplier collectors now keyed by `(target_tid, target_attr, source_type_id)`. Stacking penalties applied per source type independently, then group products multiplied together. Bastion and damage mods no longer penalize each other.

### Shared infrastructure
- **New:** `_apply_modifier()` helper function centralizes attribute modification logic (handles damageMultiplier defaults, operator dispatch).
- **Changed:** `charge_attrs_map` created early in the pipeline (before modifier steps) and deep-copied for mutation. Passed to `_apply_ship_hull_bonuses()` and `_apply_all_v_skill_bonuses()` so charge damage bonuses can be applied.

### Before/After comparison

| Fit | DPS Before | DPS After | Delta |
|-----|-----------|-----------|-------|
| Shield Drake | 171.8 | 358.3 | +108% |
| Passive Gila | 155.6 | 644.4 | +314% |
| Retribution | 394.1 | 394.1 | 0% (turrets already correct) |
| Leshak (spool) | 2,505.7 | 2,505.7 | 0% (spool already fixed) |
| HAM Sacrilege | 167.4 | 275.2 | +64% |
| Rattlesnake | 378.9 | 1,154.6 | +205% |

| Fit | EHP Before | EHP After | Delta |
|-----|-----------|-----------|-------|
| Shield Drake | 62,379 | 67,957 | +9% (hull resists) |
| Retribution | 6,102 | 7,618 | +25% (hull resists) |
| Leshak | 58,385 | 71,476 | +22% (hull resists) |

### Remaining gaps (deferred to later phases)
- Cap simulation (3.7): still using simple peak-recharge model
- Active tank display (3.8): no rep/s stats
- EHP damage profiles (3.9): still 25/25/25/25 only
- Lock time (3.10): not calculated
