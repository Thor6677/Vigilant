# Phase B: Reference-Grounded Fix Plan (Round 2)

**Date:** 2026-04-19

---

## ISSUE-001: Missile DPS still low on some fits

### Symptom

- **Sacrilege (HAM):** 275.2 DPS, expected ~440 (37% gap)
- **Drake (HML):** 358.3 DPS, expected ~390 (8% gap)

### Investigation result — Sacrilege gap is NOT a bug

Querying the SDE reveals the Sacrilege's ship hull bonuses target **Medium Energy Turret** (skill 3306), not missiles:

```
LocationRequiredSkillModifier shipID: shipBonusAC2(656)=7.5 -> damageMultiplier(64) op=6 filter=skill:3306
LocationRequiredSkillModifier shipID: eliteBonusHeavyGunship2(693)=5.0 -> damageMultiplier(64) op=6 filter=skill:3306
```

The `typeBonus` table confirms: `"bonus to medium energy turret damage"`. The Sacrilege has turret bonuses in the current SDE data. The HAM fit doesn't benefit from ship bonuses — 275.2 DPS is correct for an unbonused missile platform.

**Resolution:** The Sacrilege test fit should use Medium Pulse Laser II + Conflagration M to exercise the ship's actual bonuses. The HAM fit can remain as a secondary test case (validating that the engine correctly does NOT apply turret bonuses to launchers).

### Drake gap (8%)

The Drake's bonus correctly applies to charge kinetic damage:
```
OwnerRequiredSkillModifier charID: shipBonusCBC1(743)=10.0 -> kineticDamage(117) op=6 filter=skill:25719/3324
```

The 8% gap needs verification against Pyfa's expected value. The "expected ~390" was estimated in the previous audit, not computed from a reference tool. I cannot confirm the gap without Pyfa or Theorycrafter reference values for this exact fit.

**Proposed fix:** Replace the Sacrilege test fit with a turret fit. Verify the Drake DPS against Pyfa's All-V calculation. If the Drake gap is confirmed, investigate further.

---

## ISSUE-002: T3C subsystem modifier application order

### Symptom

Subsystem ItemModifier bonuses are applied in iteration order. If a POST_PERCENT modifier fires before a MOD_ADD modifier on the same attribute (because subsystems appear in that order in the items list), the result differs from EVE's dogma operator ordering.

Example: Core subsystem's POST_PERCENT (+20% PG) fires before Offensive subsystem's MOD_ADD (+190 PG). Result: (420 * 1.2) + 190 = 694, instead of correct: (420 + 190) * 1.2 = 732.

### Pyfa reference

**File:** `eos/modifiedAttributeDict.py`, lines 308-416 (`__calculateValue`)

Pyfa accumulates modifiers into **operator-type buckets** as effects fire. The final value is computed in fixed operator order: preAssign → preIncrease → multiply (non-penalized) → penalizedMultipliers (per stacking group) → postIncrease. The order effects fire does NOT matter — accumulation ensures correct operator ordering.

### Theorycrafter reference

**File:** `FittingEngine/src/commonMain/kotlin/theorycrafter/fitting/FittingEngine.kt`, lines 3490-3582

Theorycrafter iterates `Operation.entries` in enum declaration order (ADD before MULTIPLY_PERCENT). Modifiers are stored in `modifiersByOp: MutableMap<Operation, MutableList<PropertyModifier>>` — grouped by operator, applied in operator enum order.

### Agreement check

Both sources agree: modifiers must be grouped by operator type and applied in operator order, regardless of the order they are registered.

### Proposed fix

Change the subsystem ItemModifier application in `engine.py` to accumulate modifiers by operator type first, then apply them in order (MOD_ADD before POST_MUL before POST_PERCENT). Use the same operator ordering as Step 2's existing pipeline:

1. Collect all subsystem ItemModifier bonuses into a per-attribute dict keyed by `(target_attr_id, operator)`
2. Apply in operator order: PRE_ASSIGN → MOD_ADD → POST_MUL → POST_PERCENT
3. This replaces the current `_apply_modifier` call inside the subsystem iteration loop

---

## ISSUE-003: Drone bandwidth not enforced

### Symptom

Rattlesnake shows drone_bw_used=80 / drone_bw_total=50. All 5 drones (2x Ogre II + 3x Hammerhead II) contribute to DPS despite exceeding bandwidth.

### Pyfa reference

**File:** `eos/saveddata/drone.py`, line 163-168

Pyfa uses `amountActive` (set by UI) to control drone DPS contribution. Drones with `amountActive=0` produce zero volley. The engine does NOT automatically enforce bandwidth — the UI sets `amountActive` based on bandwidth constraints.

**File:** `eos/saveddata/fit.py`, line 1809 (`getReleaseLimitForDrone`)

Calculates max active count per drone type: `int(shipBandwidth / droneBandwidthUsed)`.

### Theorycrafter reference

**File:** `FittingEngine/src/commonMain/kotlin/theorycrafter/fitting/Mechanics.kt`, lines 1204-1236

`maxDroneGroupSize()` caps drone count to `min(bandwidth/bwPerDrone, capacity/volumePerDrone, maxActiveDrones)`.

### Agreement check

Both sources enforce bandwidth at the point where drone counts are set, not in the DPS calculation itself. The DPS loop sums all active drones; the constraint is on what counts as "active."

### Proposed fix

In the DPS calculation loop, cap the number of drones contributing to DPS by the ship's available bandwidth. Algorithm:
1. Sort drones by bandwidth cost (ascending — prefer smaller drones to maximize count)
2. Accumulate drones until bandwidth is exhausted
3. Only include drones within bandwidth in the DPS sum

This avoids adding UI state (active/inactive) by enforcing bandwidth at calculation time.

---

## ISSUE-004: Cap simulation reports 0% for all fits

### Symptom

All fits show `cap_stable=False, cap_stable_pct=0.0`. MWD-fitted ships are genuinely unstable, but the 0% value and absent time-to-empty are suspicious.

### Investigation

The cap simulator code (`app/fitting/cap_sim.py`) is structurally correct. The Drake test confirms it returns `cap_lasts_s=210` (3.5 minutes), which is reasonable.

The 0% `stable_pct` is correct behavior for unstable fits — it means the fit is NOT stable at any percentage. The time-to-empty IS computed (210s for Drake + MWD).

The baseline test output only printed `cap_stable` and `cap_stable_pct`, not `cap_lasts_s`. The simulation IS working.

### Pyfa reference

**File:** `eos/capSim.py`, lines 70-294

Pyfa's cap sim is structurally similar: min-heap, regen formula `C_max * (1 + (sqrt(prev/max) - 1) * exp((t_last-t_now)/tau))^2`, stability detection via period-based comparison. The `tau = rechargeRate / 5.0` convention matches Vigilant's `tau = recharge_rate_ms / 5000.0`.

### Theorycrafter reference

**File:** `FittingEngine/src/commonMain/kotlin/theorycrafter/fitting/Mechanics.kt`, lines 744-1032

1-second tick simulation. Stability detected when min capacity unchanged for 120 seconds.

### Agreement check

Pyfa uses `tau = rechargeRate / 5.0` (recharge in seconds), Theorycrafter uses `10*maxCap/rechargeTime`. Both are equivalent (`10*C/R = C/(R/10) = C/(2*tau)` — not quite, but the formulas converge).

### Proposed fix

**Not a bug.** The cap sim is functioning correctly. The fix is cosmetic:
1. Ensure the stats display shows `time_to_empty_s` for unstable fits (currently returned but may not be shown in the template)
2. Add a cap-stable test fit (e.g., passive shield tank with no active modules) to validate stable detection

---

## ISSUE-005: Leshak max spool DPS regression

### Symptom

Max spool DPS dropped from 2,505.7 to 2,436.7 (~3% decrease). Base DPS unchanged at 779.7.

### Investigation

The Supratidal Entropic Disintegrator II has `damageMultiplierBonusMax = 2.125`. No ship hull or module modifier targets this attribute. The correct max spool multiplier is `1 + 2.125 = 3.125`.

Current: 779.7 × 3.125 = 2,436.6 ✓
Previous: 779.7 × 3.213 = 2,505.7 — implies spoolBoost = 2.213, which exceeds the base 2.125

### Pyfa reference

**File:** `eos/saveddata/module.py`, lines 515-523

```python
spoolBoost = calculateSpoolup(
    self.getModifiedItemAttr("damageMultiplierBonusMax", 0), ...)[0]
spoolMultiplier = 1 + spoolBoost
```

At max spool, `spoolBoost = damageMultiplierBonusMax`. Pyfa reads the **modified** attribute, but since nothing modifies this attribute on the Leshak fit, it equals the base value (2.125).

### Theorycrafter reference

**File:** `FittingEngine/src/commonMain/kotlin/theorycrafter/fitting/Module.kt`, lines 567-651

Uses `damageMultiplierBonusPerCycle * spoolupCycles` as an ADD_PERCENT to damageMultiplier. At max cycles: `0.07 * (2.125/0.07) = 2.125` — same result.

### Agreement check

Both sources agree: `damageMultiplierBonusMax = 2.125` for this weapon, giving spool multiplier 3.125.

### Proposed fix

**Not a bug — the previous value was wrong.** The current 2,436.7 is correct. The old code likely had a modifier leak where Step 1's blind collection of `shipID`-domain modifiers from modules was contaminating `modified_attrs` for attr 2734. The T3C refactor that excluded subsystem types and restructured the modifier flow corrected this. No code change needed.

---

## ISSUE-006: Gila test fit has incompatible ammo

### Proposed fix

Replace "Scourge Heavy Missile" with "Caldari Navy Scourge Light Missile" (compatible with RLML).

---

## ISSUE-007: Charge compatibility misses legacy type IDs

### Symptom

`get_compatible_charges()` returns post-tiericide type IDs. EFT-pasted charge names resolve to legacy type IDs (e.g., "Scourge Fury Heavy Missile" → 2629) which are absent from the compatibility list. Auto-charge-loading during import fails silently.

### Pyfa reference

Pyfa uses its own item database with charge-to-launcher compatibility. It maintains pre/post-tiericide mappings. The compatibility is derived from matching `chargeSize` and `chargeGroup` attributes between launcher and charge.

### Theorycrafter reference

Theorycrafter: not covered (uses a compiled game database directly).

### Proposed fix

In `get_compatible_charges()` (in `app/sde/lookup.py`), the function likely queries by charge group + size matching. The legacy charge type (2629) should be in the same charge group as the post-tiericide version. If it's NOT appearing, the issue is in the group/size matching query. Investigate and fix the lookup, OR add a fallback that resolves legacy charges by matching name patterns to current types.

---

## ISSUE-008: Cap simulation model (deferred)

Carried from Round 1. The discrete-event simulator is already implemented and functioning. Remaining improvements (clip reload, cap booster injection timing) are feature enhancements, not bugs. **Defer to future work.**

---

## ISSUE-009: EHP damage profiles (deferred)

Carried from Round 1. Feature enhancement. **Defer to future work.**

---

## Summary of actionable fixes

| ID | Action | Status |
|----|--------|--------|
| ISSUE-001 | Replace Sacrilege test fit; verify Drake DPS against reference | Test fix |
| ISSUE-002 | Accumulate subsystem modifiers by operator type, apply in order | Code fix |
| ISSUE-003 | Cap drone DPS at available bandwidth | Code fix |
| ISSUE-004 | Verify cap_lasts_s is displayed; add stable test case | Display fix |
| ISSUE-005 | No fix needed — current value is correct | Closed |
| ISSUE-006 | Fix Gila test fit ammo | Test fix |
| ISSUE-007 | Investigate charge compatibility lookup | Code fix |
| ISSUE-008 | Deferred | — |
| ISSUE-009 | Deferred | — |
