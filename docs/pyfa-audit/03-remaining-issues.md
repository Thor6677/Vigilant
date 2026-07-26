# Phase A: Remaining Issues (Round 2)

**Baseline commit:** `693080e` (includes T3C subsystem support from today)
**Date:** 2026-04-19

## Current Baseline

Fits tested with All-V skills, uniform damage profile, charges manually loaded.

| Fit | Weapon DPS | Drone DPS | Total DPS | EHP | Notes |
|-----|-----------|-----------|-----------|-----|-------|
| Drake (6x HML, Scourge Fury) | 358.3 | 0.0 | 358.3 | 78,512 | |
| Gila (4x RLML, no charge) | 0.0 | 438.4 | 438.4 | 37,311 | Test fit has wrong ammo |
| Retribution (4x SFPL, Conflag S) | 394.1 | 0.0 | 394.1 | 9,523 | |
| Leshak (Supra II, Occult L) | 779.7 | 0.0 | 779.7 | 85,114 | Max spool: 2,436.7 |
| Sacrilege (5x HAM II, Scourge) | 275.2 | 0.0 | 275.2 | 21,150 | |
| Rattlesnake (5x CML, Scourge Fury) | 565.8 | 588.8 | 1,154.6 | 193,750 | DBW: 80/50 |
| Tengu (6x HAM II, Scourge Rage, 4 subs) | 782.7 | 0.0 | 782.7 | 25,257 | Slots: 7/6/4 ✓ |

## Issue List

### ISSUE-001: Missile DPS still low on multiple fits

**Symptom:** Drake weapon DPS is 358.3 (previous audit expected ~390, ~8% gap). Sacrilege is 275.2 (expected ~440, ~37% gap). The Sacrilege gap is much larger and suggests bonuses are not being applied.

**Observed on:** Drake, Sacrilege. Rattlesnake weapon DPS (565.8) may also be low but hard to verify without reference values.

**Hypothesis:** Some ship hull bonuses or skill bonuses may still not be reaching missile charge damage. The Sacrilege is a HAC with per-level damage bonuses — if the HAC elite bonus (`eliteBonusHeavyAssaultShip`) uses a modifier pattern the engine doesn't handle, the DPS would be significantly low.

### ISSUE-002: T3C subsystem modifier application order

**Symptom:** Subsystem ItemModifier bonuses (MOD_ADD for CPU/PG/HP, POST_PERCENT for velocity/cap/agility) are applied to ship_attrs in iteration order — if a POST_PERCENT modifier is encountered before a MOD_ADD to the same attribute (because the subsystem that provides the percentage comes before the subsystem that provides the flat add), the result differs from EVE's dogma operator ordering.

**Observed on:** Tengu. CPU total = 587.5, PG total = 867.5. Core subsystem's POST_PERCENT to PG is applied before Offensive subsystem's MOD_ADD to PG, giving a different result than if MOD_ADD were applied first per the dogma operator order.

**Impact:** CPU/PG totals for T3C ships may be off by a few percent depending on subsystem fitting order.

### ISSUE-003: Drone bandwidth not enforced

**Symptom:** Rattlesnake shows drone_bw_used = 80 with drone_bw_total = 50. The engine counts all fitted drones in DPS without enforcing bandwidth limits. Users can fit more drones than bandwidth allows, and the DPS reflects all of them.

**Observed on:** Rattlesnake (2x Ogre II = 50 BW + 3x Hammerhead II = 30 BW = 80 total).

**Impact:** Drone DPS is overreported when users fit more drones than bandwidth allows.

### ISSUE-004: Cap simulation reports 0% for all fits

**Symptom:** All 7 test fits show cap_stable=False, cap_stable_pct=0.0. While many of these fits are genuinely cap-unstable (MWD-fitted), the 0% stable percentage and absent time-to-empty suggest the cap simulation is not functioning correctly.

**Observed on:** All fits.

**Impact:** Users cannot see meaningful cap stability information.

### ISSUE-005: Leshak max spool DPS regression

**Symptom:** Leshak max spool DPS was 2,505.7 in the previous round, now shows 2,436.7 (~3% drop). Base DPS (779.7) is unchanged.

**Observed on:** Leshak.

**Hypothesis:** The code change excluding subsystem types from the cross-module section changed the condition from `if module_type_ids:` to `if non_sub_module_type_ids:`. For the Leshak (no subsystems), these should be identical. The spool max calculation may be affected by a subtle ordering change in the modifier pipeline, or this is a pre-existing numerical imprecision that appeared when reordering code.

### ISSUE-006: Gila test fit has incompatible ammo

**Symptom:** Gila weapon DPS = 0.0 because the test fit specifies "Scourge Heavy Missile" cargo for "Rapid Light Missile Launcher II". Heavy Missiles are not compatible with Rapid Light launchers.

**Observed on:** Gila test fit only.

**Impact:** Test coverage gap — Gila weapon DPS is untested. Need to fix the test fit to use correct ammo (e.g., Caldari Navy Scourge Light Missile).

### ISSUE-007: Charge compatibility misses legacy type IDs in EFT import

**Symptom:** `get_compatible_charges()` returns post-tiericide type IDs (27xxx range) for launchers. EFT-pasted charge names resolve to legacy type IDs (e.g., "Scourge Fury Heavy Missile" → 2629) which are not in the compatibility list. Auto-charge-loading during EFT import fails silently.

**Observed on:** Drake, Rattlesnake when using cargo-based auto-loading in the EFT import path.

**Impact:** Users importing EFT fits may see 0 weapon DPS until they manually load charges. The inline comma-separated EFT format (e.g., "Heavy Missile Launcher II, Scourge Fury Heavy Missile") bypasses this issue since it sets the charge directly.

### ISSUE-008: Deferred from Round 1 — Cap simulation model

**Symptom:** Using simple peak-recharge comparison instead of discrete-event simulation. Cap-stable percentage and time-to-empty are approximations.

**Carried from:** Round 1, gap report section "MODERATE — Capacitor simulation oversimplified".

### ISSUE-009: Deferred from Round 1 — EHP damage profiles

**Symptom:** EHP uses uniform 25/25/25/25 damage profile only. No NPC faction profiles.

**Carried from:** Round 1, gap report section "LOW — Single damage profile for EHP".

## Priority Assessment

| ID | Priority | Impact | Effort |
|----|----------|--------|--------|
| ISSUE-001 | P0 | Missile DPS 8-37% wrong | Medium |
| ISSUE-002 | P1 | T3C CPU/PG few % off | Medium |
| ISSUE-003 | P1 | Drone DPS overreported | Small |
| ISSUE-004 | P1 | No cap stability info | Medium |
| ISSUE-005 | P1 | Spool DPS regression | Small |
| ISSUE-006 | P2 | Test coverage gap | Trivial |
| ISSUE-007 | P2 | EFT import charge load | Medium |
| ISSUE-008 | P2 | Cap sim accuracy | Large |
| ISSUE-009 | P3 | Missing feature | Small |
